# -*- coding: utf-8 -*-
"""
Resumo semanal do boteco pelo Telegram (roda toda terça às 9h pelo racha-resumo.timer).

Manda: quanto vendeu, quem consumiu o quê, quem está devendo, estoque (com alerta do que
está acabando) e o resultado do bar (vendido x custo).

Uso manual:  python resumo_boteco.py            (envia no Telegram)
             python resumo_boteco.py --print    (só mostra na tela)
"""
import os
import sys
import datetime
import collections

import requests
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DESTINOS = [x.strip() for x in str(os.environ["TELEGRAM_ALLOWED_ID"]).replace(";", ",").split(",") if x.strip()]
MESES = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
ESTOQUE_BAIXO = 5


def nf(x):
    try:
        return float(str(x).replace(",", ".") or 0)
    except (TypeError, ValueError):
        return 0.0


def sheets():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SA_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])


def montar(sh):
    hoje = datetime.date.today()
    ini_mes = hoje.replace(day=1)
    semana = hoje - datetime.timedelta(days=7)
    cons = sh.worksheet("BarConsumo").get_all_records()

    sem_tot, mes_tot = 0.0, 0.0
    por_atleta = collections.defaultdict(float)
    itens_at = collections.defaultdict(lambda: collections.defaultdict(int))
    top_prod = collections.defaultdict(int)
    for c in cons:
        try:
            d = datetime.date.fromisoformat(str(c.get("data")).strip())
        except ValueError:
            continue
        v = nf(c.get("total"))
        if d >= ini_mes:
            mes_tot += v
            nome = str(c.get("atleta", "")).strip().title()
            por_atleta[nome] += v
            itens_at[nome][str(c.get("produto", "")).strip()] += int(nf(c.get("qtd")))
            top_prod[str(c.get("produto", "")).strip()] += int(nf(c.get("qtd")))
        if d >= semana:
            sem_tot += v

    prods = sh.worksheet("BarProdutos").get_all_records()
    custo_estq = sum(nf(p["custo"]) * nf(p["estoque"]) for p in prods)
    venda_estq = sum(nf(p["preco"]) * nf(p["estoque"]) for p in prods)
    baixo = [(str(p["produto"]), int(nf(p["estoque"]))) for p in prods
             if 0 < nf(p["estoque"]) <= ESTOQUE_BAIXO]
    zerado = [str(p["produto"]) for p in prods if nf(p["estoque"]) <= 0]

    mes = f"{MESES[hoje.month - 1]} {hoje.year}"
    devendo = []
    for p in sh.worksheet("Pagamentos").get_all_records():
        if str(p.get("mes", "")).strip().upper() != mes:
            continue
        nome = str(p.get("atleta", "")).strip().title()
        consumo = nf(p.get("consumo"))
        if consumo <= 0:
            continue
        fixos = nf(p.get("s_a")) + nf(p.get("mensalidade")) + nf(p.get("multa_chu"))
        sobra = max(0.0, nf(p.get("valor_pago")) - fixos)
        aberto = round(max(0.0, consumo - sobra), 2)
        if aberto > 0:
            devendo.append((nome, aberto))
    devendo.sort(key=lambda x: -x[1])

    L = [f"🍺 <b>Boteco do REA</b> — {hoje.strftime('%d/%m/%Y')}", ""]
    L.append(f"💰 Vendido na semana: <b>R$ {sem_tot:.2f}</b>")
    L.append(f"📅 No mês ({mes}): <b>R$ {mes_tot:.2f}</b>")
    if por_atleta:
        L.append("\n<b>Consumo do mês</b>")
        for nome, v in sorted(por_atleta.items(), key=lambda kv: -kv[1]):
            det = ", ".join(f"{q}x {p}" for p, q in
                            sorted(itens_at[nome].items(), key=lambda kv: -kv[1])[:4])
            L.append(f"  • {nome}: R$ {v:.2f} — {det}")
    if top_prod:
        top = sorted(top_prod.items(), key=lambda kv: -kv[1])[:5]
        L.append("\n🏆 Mais saíram: " + ", ".join(f"{p} ({q})" for p, q in top))
    if devendo:
        tot_dev = sum(v for _, v in devendo)
        L.append(f"\n⚠️ <b>Devendo o boteco (R$ {tot_dev:.2f})</b>")
        L.append("  " + " · ".join(f"{n} R$ {v:.2f}" for n, v in devendo))
    else:
        L.append("\n✅ Ninguém devendo o boteco!")
    # estoque compacto: Item estoque(vendido na semana)
    saiu_sem = collections.defaultdict(int)
    for c in cons:
        try:
            d = datetime.date.fromisoformat(str(c.get("data")).strip())
        except ValueError:
            continue
        if d >= semana:
            saiu_sem[str(c.get("produto", "")).strip()] += int(nf(c.get("qtd")))
    L.append("\n📦 <b>Estoque</b> — item <b>resta</b>(vendido na semana)")
    EMOJI_CAT = {"Cerveja": "🍺", "Refri": "🥤", "Bebida": "💧", "Salgado": "🍟"}
    por_cat = collections.OrderedDict()
    for p in prods:
        cat = str(p.get("categoria", "")).strip()
        por_cat.setdefault(cat, []).append(p)
    for cat, itens in por_cat.items():
        partes = []
        for p in itens:
            nome = str(p["produto"]).strip()
            est = int(nf(p["estoque"]))
            vend = saiu_sem.get(nome, 0)
            if est == 0 and vend == 0:
                continue
            marca = " ⚠️" if 0 < est <= ESTOQUE_BAIXO else ("❌" if est <= 0 else "")
            partes.append(f"{nome} <b>{est}</b>({vend}){marca}")
        if partes:
            L.append(f"{EMOJI_CAT.get(cat, '•')} {cat}: " + " · ".join(partes))
    if zerado:
        L.append("❌ Zerados: " + ", ".join(zerado))
    L.append(f"\n💵 Em estoque: custo R$ {custo_estq:.2f} · venda R$ {venda_estq:.2f}")
    L.append("\n🌐 https://racharea.com.br (aba 🍺 Boteco)")
    L.append("<i>Para corrigir um lançamento: mande \"desfazer NOME\" aqui.</i>")
    return "\n".join(L)


def enviar(texto):
    for chat in DESTINOS:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          json={"chat_id": chat, "text": texto, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30)
        print(chat, r.status_code, r.text[:120])


if __name__ == "__main__":
    txt = montar(sheets())
    if "--print" in sys.argv:
        print(txt)
    else:
        enviar(txt)
