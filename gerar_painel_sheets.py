# -*- coding: utf-8 -*-
"""Gera o painel do REA lendo os dados do Google Sheets (formato tidy: abas
Atletas, Lancamentos, Pagamentos, Movimentos, Sabados) e calculando as regras
(Craque do ciclo, Artilharia, Atleta Fiel, Estatísticas, Financeiro) no código.

Reaproveita o template HTML e os helpers (logo, Pix) do gerar_painel.py.

Config via arquivo .env na mesma pasta (NUNCA versionado):
    GOOGLE_SA_JSON = caminho do JSON da conta de serviço do Google
    SHEET_ID       = ID da planilha "Racha REA — Dados"
    OUTPUT         = (opcional) pasta onde salvar o index.html; padrão = pasta do script
"""
import os
import io
import json
import base64
import datetime
import collections
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import gerar_painel as G  # TEMPLATE, logo_html, dados_pix, constantes (só import, sem I/O)

SA_JSON = os.environ.get("GOOGLE_SA_JSON", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
OUTPUT = Path(os.environ.get("OUTPUT") or Path(__file__).parent) / "index.html"
ABAS = ["Atletas", "Lancamentos", "Pagamentos", "Movimentos", "Sabados"]
MESES = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]


def carregar():
    """Lê as abas tidy do Google Sheets como listas de dicionários."""
    creds = Credentials.from_service_account_file(
        SA_JSON, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(SHEET_ID)
    out = {a: sh.worksheet(a).get_all_records() for a in ABAS}
    try:  # aba do Pix é opcional (só existe depois que o Pix automático foi ativado)
        out["PixCobrancas"] = sh.worksheet("PixCobrancas").get_all_records()
    except Exception:
        out["PixCobrancas"] = []
    try:  # aba do último racha (o bot grava; se não existir, usa o fixo do gerar_painel)
        out["UltimoRacha"] = sh.worksheet("UltimoRacha").get_all_records()
    except Exception:
        out["UltimoRacha"] = []
    return out


def dados_ultimo(T):
    """Último racha vindo da aba UltimoRacha (gravada pelo bot); fallback: constante fixa."""
    recs = T.get("UltimoRacha") or []
    if not recs or not str(recs[0].get("data", "")).strip():
        return G.ULTIMO_RACHA
    r = recs[0]
    try:
        partidas = json.loads(str(r.get("partidas_json") or "[]"))
        quadro = json.loads(str(r.get("quadro_json") or "[]"))
    except json.JSONDecodeError:
        return G.ULTIMO_RACHA
    quadro_fmt = [{"time": str(q.get("time", "")).strip().title(),
                   "v": int(q.get("v") or 0), "e": int(q.get("e") or 0),
                   "d": int(q.get("d") or 0), "gols": int(q.get("gols") or 0)}
                  for q in quadro]
    return {"data": str(r.get("data", "")).strip(),
            "partidas": [str(p).title() for p in partidas],
            "quadro": quadro_fmt,
            "bola_cheia": str(r.get("bola_cheia", "")).strip().title(),
            "bola_murcha": str(r.get("bola_murcha", "")).strip().title()}


def _mes_key(m):
    try:
        mm, yy = str(m).split()
        return (int(yy), MESES.index(mm.upper()))
    except Exception:
        return (0, 0)


def dados_cobrancas(T):
    """Cobranças Pix ATIVAS do mês mais recente, com QR em data-URI, para o atleta
    pagar direto no site (achando o nome dele). Vazio se o Pix ainda não foi ativado."""
    recs = T.get("PixCobrancas", []) or []
    if not recs:
        return {"mes": "", "itens": []}
    meses = sorted({str(r.get("mes", "")).strip() for r in recs if r.get("mes")}, key=_mes_key)
    mes = meses[-1] if meses else ""
    # quem já está em dia no mês da cobrança (pagou manual/dinheiro) não deve ver QR
    quitados = set()
    for p in T.get("Pagamentos", []):
        if str(p.get("mes", "")).strip().upper() != mes.upper():
            continue
        devido = nf(p.get("s_a")) + nf(p.get("mensalidade")) + nf(p.get("multa_chu"))
        if devido > 0 and nf(p.get("valor_pago")) >= devido:
            quitados.add(str(p.get("atleta", "")).strip().upper())
    try:
        import qrcode
    except ImportError:
        qrcode = None
    ativos = {str(a.get("nome", "")).strip().upper() for a in T.get("Atletas", [])
              if str(a.get("ativo", "")).strip().lower() == "sim"}
    itens = []
    for r in recs:
        if str(r.get("mes", "")).strip() != mes:
            continue
        atleta_up = str(r.get("atleta", "")).strip().upper()
        if atleta_up not in ativos:  # saiu do racha -> não exibe cobrança
            continue
        pago = (str(r.get("status", "")).strip().upper() == "CONCLUIDA"
                or atleta_up in quitados)
        copia = str(r.get("pix_copia_cola", "")).strip()
        qr_uri = ""
        if copia and qrcode and not pago:
            img = qrcode.make(copia)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            qr_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        try:
            valor_fmt = f"{float(str(r.get('valor', '0')).replace(',', '.')):.2f}".replace(".", ",")
        except (TypeError, ValueError):
            valor_fmt = str(r.get("valor", "")).strip()
        itens.append({"nome": str(r.get("atleta", "")).strip().title(),
                      "valor": valor_fmt,
                      "copia": copia, "qr": qr_uri, "pago": pago})
    itens.sort(key=lambda x: x["nome"])
    return {"mes": mes, "itens": itens}


def nf(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def ranking(dic):
    """Lista ordenada por pontos (desc) e nome (asc) — determinística."""
    itens = [(n, v) for n, v in dic.items() if v != 0]
    return [{"nome": n.title(), "total": (int(v) if v == int(v) else round(v, 1))}
            for n, v in sorted(itens, key=lambda x: (-x[1], x[0]))]


def dados_ranking(T):
    craque = collections.defaultdict(float)
    arti = collections.defaultdict(float)
    vit = collections.defaultdict(float)
    presq = collections.defaultdict(int)
    presp = collections.defaultdict(int)
    cart = collections.defaultdict(float)
    cheia = collections.defaultdict(int)
    murcha = collections.defaultdict(int)
    por_data = collections.defaultdict(dict)
    atletas = set()
    for r in T["Lancamentos"]:
        a = str(r["atleta"]).strip().upper()
        atletas.add(a)
        d = datetime.date.fromisoformat(str(r["data"]))
        pp, g, vp, bp, cp = (nf(r["presenca_pts"]), nf(r["gols"]), nf(r["vitoria_pts"]),
                             nf(r["bola_pts"]), nf(r["cartao_pts"]))
        if d >= G.CICLO_INICIO:
            craque[a] += pp + vp + bp + cp
        arti[a] += g
        vit[a] += vp
        if pp >= 2:
            presq[a] += 1
        if pp == 3:
            presp[a] += 1
        cart[a] += cp
        if bp > 0:
            cheia[a] += 1
        if bp < 0:
            murcha[a] += 1
        if pp:
            por_data[str(r["data"])][a] = pp
    # Atleta Fiel: presente (>=2) nas últimas 8 datas com presença
    datas = sorted(por_data)
    ult8 = datas[-8:]
    fieis, fiel_tab = [], []
    for a in atletas:
        p8 = sum(por_data[d].get(a, 0) for d in ult8)
        ok = bool(ult8) and all(por_data[d].get(a, 0) >= 2 for d in ult8)
        if ok:
            fieis.append(a.title())
        fiel_tab.append({"nome": a.title(), "pontos": int(p8), "fiel": ok})
    fiel_tab.sort(key=lambda x: (not x["fiel"], x["nome"]))
    estat = []
    for a in sorted(atletas):
        if craque.get(a, 0) == 0 and presq.get(a, 0) == 0:
            continue
        estat.append({"nome": a.title(), "vitorias": int(vit.get(a, 0)),
                      "presencas": int(presq.get(a, 0)), "pontualidade": int(presp.get(a, 0)),
                      "cartoes": int(cart.get(a, 0)), "cheia": int(cheia.get(a, 0)),
                      "murcha": int(murcha.get(a, 0)), "fiel": a.title() in fieis})
    estat.sort(key=lambda x: -x["presencas"])
    return {"craque": ranking(craque), "artilharia": ranking(arti), "estatisticas": estat,
            "fieis": sorted(fieis), "fiel_tab": fiel_tab}


def dados_financeiro(T):
    hoje = datetime.date.today()
    mes = f"{MESES[hoje.month - 1]} {hoje.year}"
    ativos, isentos = [], set()
    for r in T["Atletas"]:
        nome = str(r["nome"]).strip().upper()
        if str(r.get("ativo", "")).strip().lower() != "sim" or nome in G.SAIRAM:
            continue
        ativos.append(nome)
        if str(r.get("isento_mensalidade", "")).strip().lower() == "sim":
            isentos.add(nome)
    pag = {str(r["atleta"]).strip().upper(): r
           for r in T["Pagamentos"] if str(r["mes"]).strip().upper() == mes}
    linhas = []
    for nome in ativos:
        if nome in isentos:
            linhas.append({"nome": nome.title(), "total": 0.0, "pago": 0.0,
                           "saldo": 0.0, "situacao": "Isento"})
            continue
        r = pag.get(nome)
        if not r:
            continue
        sa, mens = nf(r["s_a"]), nf(r["mensalidade"])
        multa, pago = nf(r["multa_chu"]), nf(r["valor_pago"])
        total = sa + mens + multa
        saldo = total - pago
        resto = max(0.0, sa - pago)
        sit = ("Isento" if total == 0 and saldo == 0 else "Em dia" if saldo <= 0
               else "Atrasado" if resto >= 90 else "Mês em aberto")
        linhas.append({"nome": nome.title(), "total": round(total, 2),
                       "pago": round(pago, 2), "saldo": round(saldo, 2), "situacao": sit})
    ordem = {"Atrasado": 0, "Mês em aberto": 1, "Em dia": 2, "Isento": 3}
    linhas.sort(key=lambda x: (ordem[x["situacao"]], x["nome"]))
    return linhas


def roster_ativos(T):
    """Lista de presença derivada do cadastro: ativo=sim e joga≠não.
    Sair do racha = marcar ativo=não na aba Atletas (some de tudo)."""
    nomes = []
    for r in T["Atletas"]:
        ativo = str(r.get("ativo", "")).strip().lower() == "sim"
        joga = str(r.get("joga", "")).strip().lower() != "não"
        if ativo and joga:
            nomes.append(str(r["nome"]).strip().title())
    return sorted(nomes)


def montar(T):
    return {
        "gerado_em": datetime.datetime.now().strftime("%d/%m/%Y %H:%M"),
        "aviso_topo": G.AVISO_TOPO,
        "ultimo_racha": dados_ultimo(T),
        "ranking": dados_ranking(T),
        "financeiro": dados_financeiro(T),
        "cobrancas": dados_cobrancas(T),
        "rsvp_url": G.RSVP_URL,
        "roster": roster_ativos(T),
    }


def gerar():
    if not SA_JSON or not SHEET_ID:
        raise SystemExit("ERRO: defina GOOGLE_SA_JSON e SHEET_ID (no .env ou variáveis de ambiente).")
    T = carregar()
    dados = montar(T)
    html = G.TEMPLATE.replace("__DADOS__", json.dumps(dados, ensure_ascii=False))
    html = html.replace("__LOGO__", G.logo_html())
    OUTPUT.write_text(html, encoding="utf-8")
    print("Painel gerado (Sheets):", OUTPUT)


if __name__ == "__main__":
    gerar()
