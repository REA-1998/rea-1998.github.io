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

import conta as C  # conta-corrente do atleta (fonte unica da matematica)

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DESTINOS = [x.strip() for x in str(os.environ["TELEGRAM_ALLOWED_ID"]).replace(";", ",").split(",") if x.strip()]
MESES = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
ESTOQUE_BAIXO = 5
EMOJI_CAT = {"Cerveja": "🍺", "Refri": "🥤", "Bebida": "💧", "Salgado": "🍟",
             "Resenha": "🍖", "Água de coco": "🥥"}


def nf(x):
    try:
        return float(str(x).replace(",", ".") or 0)
    except (TypeError, ValueError):
        return 0.0


def sheets():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SA_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])


def montar(sh, detalhe="mes"):
    """Monta o relatorio do boteco.

    detalhe="mes"    -> a lista por atleta cobre o mes (e o que cai na terca as 9h)
    detalhe="semana" -> a lista por atleta cobre so os ultimos 7 dias (comando do bot)

    O que esta em ABERTO continua sempre apurado no mes, porque a divida e mensal.
    """
    hoje = datetime.date.today()
    ini_mes = hoje.replace(day=1)
    semana = hoje - datetime.timedelta(days=7)
    ini_det = semana if detalhe == "semana" else ini_mes
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
        if d >= semana:
            sem_tot += v
        if d >= ini_det:
            nome = str(c.get("atleta", "")).strip().title()
            por_atleta[nome] += v
            itens_at[nome][str(c.get("produto", "")).strip()] += int(nf(c.get("qtd")))
            top_prod[str(c.get("produto", "")).strip()] += int(nf(c.get("qtd")))

    prods = sh.worksheet("BarProdutos").get_all_records()
    com_est = [p for p in prods if com_estoque(p)]
    custo_estq = sum(nf(p["custo"]) * nf(p["estoque"]) for p in com_est)
    venda_estq = sum(nf(p["preco"]) * nf(p["estoque"]) for p in com_est)
    baixo = [(str(p["produto"]), int(nf(p["estoque"]))) for p in com_est
             if 0 < nf(p["estoque"]) <= ESTOQUE_BAIXO]
    zerado = [str(p["produto"]) for p in com_est if nf(p["estoque"]) <= 0]

    mes = f"{MESES[hoje.month - 1]} {hoje.year}"
    cons_ts = C.consumos_por_atleta(cons, ini=ini_mes)
    pgto_ts = C.pagamentos_por_atleta(sh.worksheet("PixCobrancas").get_all_records(), ini=ini_mes)
    devendo = []
    for p in sh.worksheet("Pagamentos").get_all_records():
        if str(p.get("mes", "")).strip().upper() != mes:
            continue
        alvo = str(p.get("atleta", "")).strip().upper()
        if not cons_ts.get(alvo):
            continue
        aberto = C.extrato(p, cons_ts[alvo], pgto_ts.get(alvo, []), ini_mes)["boteco_aberto"]
        if aberto > 0:
            devendo.append((alvo.title(), aberto))
    devendo.sort(key=lambda x: -x[1])

    L = [f"🍺 <b>Boteco do REA</b> — {hoje.strftime('%d/%m/%Y')}", ""]
    L.append(f"💰 Vendido na semana: <b>R$ {sem_tot:.2f}</b>")
    L.append(f"📅 No mês ({mes}): <b>R$ {mes_tot:.2f}</b>")
    if por_atleta:
        titulo = ("Consumo da semana (desde " + semana.strftime("%d/%m") + ")"
                  if detalhe == "semana" else "Consumo do mês")
        L.append("\n<b>" + titulo + "</b>")
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
    bloco_srv = resumo_servicos(prods, cons, semana, ini_mes)
    if bloco_srv:
        L.append("")
        L.append(bloco_srv)
    L.append("\n📦 <b>Estoque</b> — item <b>resta</b>(vendido na semana)")
    por_cat = collections.OrderedDict()
    for p in prods:
        if not com_estoque(p):      # resenha e servico: nao aparece no estoque
            continue
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
    L.append("")
    L.append(confere_estoque(sh, prods, cons))
    L.append(confere_precos(prods))
    L.append("\n🌐 https://racharea.com.br (aba 🍺 Boteco)")
    L.append("<i>Para corrigir um lançamento: mande \"desfazer NOME\" aqui.</i>")
    return "\n".join(L)


def confere_precos(prods):
    """Confere se o preço de venda ainda é custo + margem. Pega preço mexido na mão ou
    custo que mudou sem o preço acompanhar."""
    try:
        import precos as P
    except Exception:
        return ""
    fora = P.conferir(prods)
    if not fora:
        return "✅ Preços conferem: custo + margem, item por item."
    linhas = ["⚠️ <b>Preço fora da margem</b>"]
    for nome, c, m, atual, certo in fora:
        linhas.append(f"• {nome}: está R$ {atual:.2f}, com {m * 100:.0f}% seria R$ {certo:.2f}")
    return chr(10).join(linhas)



def com_estoque(p):
    """Produto que tem contagem de estoque. 'nao' = serviço puro, fica fora de tudo."""
    return str(p.get("controla_estoque", "sim")).strip().lower() not in ("nao", "não", "n")


def e_servico(p):
    """controla_estoque='nao': resenha e afins. Não tem prateleira — o que interessa é
    quantos marcaram o nome, não quanto 'sobrou'."""
    return not com_estoque(p)


def resumo_servicos(prods, cons, semana, ini_mes):
    """Itens sem prateleira (resenha, água de coco...): quantos marcaram, na semana e no
    mês, e quem. Agrupa por categoria — não é tudo resenha."""
    servicos = {str(p["produto"]).strip(): str(p.get("categoria", "")).strip()
                for p in prods if e_servico(p)}
    if not servicos:
        return ""
    sem, mes = collections.defaultdict(int), collections.defaultdict(int)
    quem = collections.defaultdict(list)
    for c in cons:
        nome = str(c.get("produto", "")).strip()
        if nome not in servicos:
            continue
        try:
            d = datetime.date.fromisoformat(str(c.get("data")).strip())
        except ValueError:
            continue
        q = int(nf(c.get("qtd")))
        if d >= ini_mes:
            mes[nome] += q
            quem[nome].append(str(c.get("atleta", "")).strip().title())
        if d >= semana:
            sem[nome] += q
    if not mes:
        cats = sorted(set(servicos.values()))
        return "ℹ️ Ninguém marcou ainda neste mês: " + ", ".join(cats) + "."
    linhas = []
    for cat in sorted(set(servicos.values())):
        itens = [n for n, c in servicos.items() if c == cat and mes.get(n)]
        if not itens:
            continue
        linhas.append(f"{EMOJI_CAT.get(cat, '•')} <b>Quem marcou — {cat}</b>")
        for nome in sorted(itens):
            linhas.append(f"  • {nome}: <b>{sem.get(nome, 0)}</b> na semana · "
                          f"{mes[nome]} no mês")
            linhas.append("     " + ", ".join(sorted(set(quem[nome]))))
    return chr(10).join(linhas)


def confere_estoque(sh, prods, cons):
    """Fecha a conta do estoque: tudo que entrou menos tudo que saiu tem que dar o que está
    na prateleira. Se não der, alguém deu entrada errada ou mexeu no estoque na mão."""
    try:
        entradas = sh.worksheet("BarEntradas").get_all_records()
    except Exception:
        return "⚠️ Sem a aba BarEntradas — não dá para conferir o estoque."
    ent = collections.defaultdict(int)
    for e in entradas:
        ent[str(e.get("produto", "")).strip()] += int(nf(e.get("qtd")))
    saiu = collections.defaultdict(int)
    for c in cons:
        saiu[str(c.get("produto", "")).strip()] += int(nf(c.get("qtd")))
    ruins = []
    for p in prods:
        if not com_estoque(p):      # servico nao tem entrada nem prateleira para conferir
            continue
        nome = str(p.get("produto", "")).strip()
        est = int(nf(p.get("estoque")))
        calc = ent.get(nome, 0) - saiu.get(nome, 0)
        if calc != est:
            ruins.append(f"• {nome}: a conta dá {calc}, na prateleira {est}")
    if not ruins:
        return "✅ Estoque confere: entrou − vendeu = o que tem, item por item."
    return "\n".join(["🚨 <b>Estoque não fecha</b> (entrou − vendeu ≠ estoque)"] + ruins)

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
