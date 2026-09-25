#!/usr/bin/env python3
"""
Monitor de Preço Teto - Ações da B3
-----------------------------------
1) Calcula o preço teto de cada ação por vários métodos
   (Bazin, Graham, Gordon/DDM, P/L alvo, P/VP alvo).
2) Consulta o preço atual.
3) Envia alerta (Telegram e/ou e-mail e/ou tela) quando o preço estiver
   abaixo do preço teto, com ticker, preço atual e todos os tetos.

Modos de uso:
    python monitor_preco_teto.py --tabela           # mostra os tetos e sai
    python monitor_preco_teto.py --teste-telegram   # manda mensagem de teste
    python monitor_preco_teto.py --uma-vez          # confere 1 vez e sai (nuvem)
    python monitor_preco_teto.py                    # fica rodando em loop (PC/servidor)

Variáveis de ambiente (para receber alertas):
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
    (opcional e-mail) SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_TO

AVISO: ferramenta educacional, não é recomendação de investimento.
Os dados do Yahoo Finance têm atraso (~15 min) e podem conter falhas.
"""

import argparse
import json
import math
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# ============================ CONFIGURAÇÕES ============================

# Lista de ações (sem o ".SA")
ACOES = [
    "CXSE3", "BBAS3", "ITSA4", "TAEE11", "BBSE3", "CMIG4", "CSMG3",
    "PETR4", "VALE3", "BBDC3", "MTRE3", "PSSA3", "ITUB4", "ABCB4",
]

# Parâmetros dos métodos
YIELD_BAZIN = 0.06            # Bazin: dividend yield mínimo desejado (6%)
RETORNO_EXIGIDO = 0.12        # Gordon: taxa de retorno exigida (k) ao ano
CRESCIMENTO_PERPETUO = 0.04   # Gordon: crescimento perpétuo dos dividendos (g)
PL_ALVO = 10                  # P/L máximo que você paga
PVP_ALVO = 1.5                # P/VP máximo que você paga (Graham usa 1,5)
ANOS_PROJECAO_FCD = 5         # FCD: por quantos anos projetar o fluxo de caixa
CRESCIMENTO_FCD = 0.05        # FCD: crescimento anual do fluxo de caixa livre nesses anos

# Monitoramento
INTERVALO_SEGUNDOS = 60       # (modo loop) de quanto em quanto tempo consultar
MIN_METODOS_ABAIXO = 1        # nº mínimo de métodos que o preço deve estar abaixo
                              # (1 = qualquer um; use 5 para exigir todos)
COOLDOWN_HORAS = 6            # não repetir alerta da mesma ação antes disso
FUSO = ZoneInfo("America/Sao_Paulo")
HORA_ABERTURA = (10, 0)       # ajuste conforme o horário do pregão vigente
HORA_FECHAMENTO = (18, 0)

ARQUIVO_ESTADO = "estado.json"  # guarda tetos do dia e alertas já enviados

# ======================================================================

METODOS = ["Bazin", "Graham", "Gordon (DDM)", "P/L alvo", "P/VP alvo", "FCD"]


def brl(v):
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _positivo(x):
    return isinstance(x, (int, float)) and x is not None and not math.isnan(x) and x > 0


# --------------------------- Cálculo dos tetos ------------------------

def dividendos(ativo):
    """Retorna (DPA dos últimos 12 meses, DPA médio anual dos últimos 5 anos)."""
    div = ativo.dividends
    if div is None or div.empty:
        return None, None
    idx = div.index
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    div = pd.Series(div.values, index=idx)
    agora = pd.Timestamp.now()
    d12 = div[div.index > agora - pd.DateOffset(months=12)].sum()
    d5 = div[div.index > agora - pd.DateOffset(years=5)].sum()
    anos = min(5.0, max(1.0, (agora - div.index.min()).days / 365.25))
    return float(d12), float(d5 / anos)


def fluxo_caixa_livre(ativo):
    """Retorna o Fluxo de Caixa Livre (FCL) do último ano disponível, se houver."""
    try:
        cf = ativo.cashflow
    except Exception:
        return None
    if cf is None or cf.empty:
        return None

    # Versões novas do yfinance já trazem a linha pronta
    if "Free Cash Flow" in cf.index:
        serie = cf.loc["Free Cash Flow"].dropna()
        if not serie.empty:
            return float(serie.iloc[0])

    # Senão, calcula: Caixa das Operações - Investimentos (Capex)
    ocf = capex = None
    for rotulo in ["Operating Cash Flow", "Total Cash From Operating Activities"]:
        if rotulo in cf.index:
            serie = cf.loc[rotulo].dropna()
            if not serie.empty:
                ocf = float(serie.iloc[0])
                break
    for rotulo in ["Capital Expenditure", "Capital Expenditures"]:
        if rotulo in cf.index:
            serie = cf.loc[rotulo].dropna()
            if not serie.empty:
                capex = float(serie.iloc[0])
                break
    if ocf is not None and capex is not None:
        return ocf + capex  # capex já vem negativo nos dados do Yahoo
    return None


def calcular_dcf(fcl_atual, n_acoes):
    """Projeta o FCL, desconta a valor presente e soma o valor terminal (perpetuidade)."""
    if not (_positivo(fcl_atual) and _positivo(n_acoes)):
        return None
    if RETORNO_EXIGIDO <= CRESCIMENTO_PERPETUO:
        return None

    valor_presente = 0.0
    fcl = fcl_atual
    for ano in range(1, ANOS_PROJECAO_FCD + 1):
        fcl *= (1 + CRESCIMENTO_FCD)
        valor_presente += fcl / (1 + RETORNO_EXIGIDO) ** ano

    valor_terminal = fcl * (1 + CRESCIMENTO_PERPETUO) / (RETORNO_EXIGIDO - CRESCIMENTO_PERPETUO)
    valor_presente += valor_terminal / (1 + RETORNO_EXIGIDO) ** ANOS_PROJECAO_FCD

    return valor_presente / n_acoes


def calcular_tetos(ticker):
    """Calcula os preços teto de uma ação. Métodos sem dados ficam como None."""
    ativo = yf.Ticker(f"{ticker}.SA")
    try:
        info = ativo.info or {}
    except Exception:
        info = {}

    lpa = info.get("trailingEps")
    vpa = info.get("bookValue")
    n_acoes = info.get("sharesOutstanding")
    try:
        dpa12, dpa_medio = dividendos(ativo)
    except Exception:
        dpa12, dpa_medio = None, None
    try:
        fcl = fluxo_caixa_livre(ativo)
    except Exception:
        fcl = None

    tetos = dict.fromkeys(METODOS)

    # 1) Bazin: dividendo médio anual / yield desejado
    if _positivo(dpa_medio):
        tetos["Bazin"] = dpa_medio / YIELD_BAZIN

    # 2) Graham: raiz(22,5 x LPA x VPA)
    if _positivo(lpa) and _positivo(vpa):
        tetos["Graham"] = math.sqrt(22.5 * lpa * vpa)

    # 3) Gordon: D1 / (k - g)
    if _positivo(dpa12) and RETORNO_EXIGIDO > CRESCIMENTO_PERPETUO:
        tetos["Gordon (DDM)"] = dpa12 * (1 + CRESCIMENTO_PERPETUO) / (
            RETORNO_EXIGIDO - CRESCIMENTO_PERPETUO
        )

    # 4) P/L alvo x LPA
    if _positivo(lpa):
        tetos["P/L alvo"] = PL_ALVO * lpa

    # 5) P/VP alvo x VPA
    if _positivo(vpa):
        tetos["P/VP alvo"] = PVP_ALVO * vpa

    # 6) FCD: fluxo de caixa livre projetado e descontado a valor presente
    tetos["FCD"] = calcular_dcf(fcl, n_acoes)

    return tetos


def preco_atual(ticker):
    try:
        p = yf.Ticker(f"{ticker}.SA").fast_info["last_price"]
        return float(p) if _positivo(p) else None
    except Exception:
        return None


# ------------------------------ Mensagem ------------------------------

def montar_alerta(ticker, preco, tetos):
    linhas = [f"🔔 ALERTA - {ticker}", f"Preço atual: {brl(preco)}", "", "Preços teto:"]
    for nome in METODOS:
        v = tetos.get(nome)
        if v is None:
            linhas.append(f" • {nome}: sem dados")
        else:
            status = "✅ abaixo" if preco < v else "❌ acima"
            linhas.append(f" • {nome}: {brl(v)} ({status})")
    linhas.append("")
    linhas.append(datetime.now(FUSO).strftime("%d/%m/%Y %H:%M:%S"))
    return "\n".join(linhas)


# ------------------------------ Envio --------------------------------

def enviar_telegram(texto):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("[aviso] Telegram não configurado (faltam TELEGRAM_TOKEN / TELEGRAM_CHAT_ID).")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": texto},
            timeout=15,
        )
        if not r.ok:
            print(f"[erro telegram] {r.status_code}: {r.text}")
        return r.ok
    except Exception as e:
        print(f"[erro telegram] {e}")
        return False


def enviar_email(assunto, texto):
    host, to = os.getenv("SMTP_HOST"), os.getenv("EMAIL_TO")
    if not (host and to):
        return
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = assunto, os.getenv("SMTP_USER", ""), to
        msg.set_content(texto)
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as s:
            s.starttls()
            s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
    except Exception as e:
        print(f"[erro e-mail] {e}")


def enviar_alerta(ticker, texto):
    print("\n" + texto + "\n")
    enviar_telegram(texto)
    enviar_email(f"Alerta B3: {ticker} abaixo do preço teto", texto)


# ------------------------------ Estado -------------------------------

def carregar_estado():
    try:
        with open(ARQUIVO_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def salvar_estado(estado):
    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)


# ------------------------------ Lógica -------------------------------

def mercado_aberto():
    agora = datetime.now(FUSO)
    if agora.weekday() >= 5:
        return False
    return HORA_ABERTURA <= (agora.hour, agora.minute) <= HORA_FECHAMENTO


def atualizar_tetos(estado):
    """Recalcula os tetos 1x por dia (e refaz só as ações que vieram sem dados)."""
    hoje = datetime.now(FUSO).date().isoformat()
    tetos = estado.setdefault("tetos", {})
    if estado.get("data_calculo") != hoje:
        tetos.clear()
        print("Calculando preços teto do dia...")
    for t in ACOES:
        if not any(v is not None for v in tetos.get(t, {}).values()):
            tetos[t] = calcular_tetos(t)
    estado["data_calculo"] = hoje
    return tetos


def verificar_precos(estado):
    """Confere o preço de cada ação e envia alerta se estiver abaixo do teto."""
    tetos = atualizar_tetos(estado)
    alertas = estado.setdefault("alertas", {})
    agora = datetime.now(FUSO)

    for t in ACOES:
        p = preco_atual(t)
        if p is None:
            print(f"{t}: preço indisponível")
            continue
        validos = [v for v in tetos[t].values() if v is not None]
        abaixo = sum(1 for v in validos if p < v)
        print(f"{t}: {brl(p)} | abaixo de {abaixo} de {len(validos)} tetos")

        if validos and abaixo >= MIN_METODOS_ABAIXO:
            ultimo = alertas.get(t)
            if ultimo is None or agora - datetime.fromisoformat(ultimo) > timedelta(hours=COOLDOWN_HORAS):
                enviar_alerta(t, montar_alerta(t, p, tetos[t]))
                alertas[t] = agora.isoformat()
        else:
            alertas.pop(t, None)  # rearma: próximo cruzamento alerta na hora


def tabela():
    linhas = []
    for t in ACOES:
        linhas.append({"Ação": t, "Preço": preco_atual(t), **calcular_tetos(t)})
    df = pd.DataFrame(linhas).set_index("Ação")
    print(df.round(2).to_string())


def loop():
    estado = carregar_estado()
    print(f"Monitorando {len(ACOES)} ações. Ctrl+C para parar.")
    while True:
        if mercado_aberto():
            verificar_precos(estado)
            salvar_estado(estado)
        time.sleep(INTERVALO_SEGUNDOS)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tabela", action="store_true", help="mostra os tetos e sai")
    ap.add_argument("--teste-telegram", action="store_true", help="envia mensagem de teste")
    ap.add_argument("--uma-vez", action="store_true", help="confere uma vez e sai")
    ap.add_argument("--ignorar-horario", action="store_true", help="roda mesmo com mercado fechado")
    args = ap.parse_args()

    try:
        if args.teste_telegram:
            ok = enviar_telegram("✅ Teste: o monitor de preço teto está conectado ao seu Telegram!")
            print("Mensagem enviada." if ok else "Falhou: confira o token e o chat id.")
            sys.exit(0 if ok else 1)
        elif args.tabela:
            tabela()
        elif args.uma_vez:
            if not (args.ignorar_horario or mercado_aberto()):
                print("Mercado fechado. Nada a fazer.")
            else:
                estado = carregar_estado()
                verificar_precos(estado)
                salvar_estado(estado)
        else:
            loop()
    except KeyboardInterrupt:
        print("\nEncerrado.")
