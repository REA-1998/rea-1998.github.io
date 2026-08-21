# -*- coding: utf-8 -*-
"""Bot do Telegram do Racha REA (v1).

Fluxo: Mateus manda a FOTO da súmula -> a IA (Claude) lê e extrai os fatos ->
o bot manda um RESUMO com o que entendeu e o que NÃO entendeu, com botões
[Confirmar]/[Cancelar] -> no Confirmar, grava os lançamentos na aba Lancamentos
do Google Sheets e republica o painel.

Trava: só responde ao TELEGRAM_ALLOWED_ID (o Mateus).

Config via .env (mesma pasta):
    TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_ID, ANTHROPIC_API_KEY,
    GOOGLE_SA_JSON, SHEET_ID
"""
import os
import sys
import json
import base64
import asyncio
import difflib
import unicodedata
import subprocess
import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import anthropic
import gspread
import requests
import montar_times as M
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters, ContextTypes)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_ID = int(os.environ["TELEGRAM_ALLOWED_ID"])
SHEET_ID = os.environ["SHEET_ID"]
SA_JSON = os.environ["GOOGLE_SA_JSON"]
APP_DIR = Path(__file__).parent

anthropic_client = anthropic.Anthropic()  # lê ANTHROPIC_API_KEY do ambiente

# letra do time -> pontos de vitória (o gerador usa AMARELO/AZUL/ROSA)
LETRA = {"AMARELO": "Y", "AZUL": "B", "ROSA": "P"}

PROMPT = """Você é o auxiliar de súmula do racha REA. Recebe a FOTO de uma súmula e extrai os
FATOS em JSON. NÃO invente: o que não tiver certeza, coloque em "incertezas".

A súmula tem uma TABELA DE ATLETAS (uma linha por atleta, nome já impresso à esquerda),
com estas colunas NESTA ORDEM: TIME | ATRASO | GOL | G/C | C.AM | C.AZ | C.V
- TIME: AZ=Azul, AM=Amarelo, RO=Rosa. SÓ jogou quem tem o TIME preenchido; linha com TIME
  em branco = NÃO jogou (ignore o atleta).
- ATRASO: apenas um "X" indica atrasado (chegou depois das 16h). Vazio OU qualquer número
  = PONTUAL. Número NUNCA é atraso.
- GOL: número de gols do atleta (ex.: 1, 2, 3, "01", "02"). É AQUI que fica o gol.
- G/C = gol contra. C.AM = cartão amarelo. C.AZ = cartão azul. C.V = cartão vermelho.
⚠️ NÃO CONFUNDA AS COLUNAS: um número ao lado do nome quase sempre é GOL (coluna 3), não ATRASO.
A coluna GOL pode ter número OU marcas de tally (|=1, "Γ/⌐"=2, "⊓"=3, "□"=4) — conte com atenção.
- Goleiro: o atleta cujo nome traz "(GOL)" E que está com o TIME preenchido.

À DIREITA há:
- PARTIDAS: placares jogo a jogo (ex.: "AZUL 1 x 2 AMARELO"). Leia todos.
- Tabela VIT / EMP / DERR por time (AMARELO, AZUL, ROSA).
- BOLA CHEIA (votos) / BOLA MURCHA (votos): listas de votação (pode ignorar os votos).
- TROFÉU CHEIA: vencedor da bola cheia. TROFÉU MURCHA: vencedor da bola murcha.
- "R$ PAGOU EM DINHEIRO HOJE": pagamentos (ignore por enquanto).

Responda SÓ com JSON neste formato:
{
  "data": "AAAA-MM-DD",
  "times": {"AZUL": ["NOME",...], "AMARELO": [...], "ROSA": [...]},
  "goleiros": {"AZUL": "NOME ou vazio", "AMARELO": "...", "ROSA": "..."},
  "resultado_por_time": {"AZUL": pontos, "AMARELO": pontos, "ROSA": pontos},
  "partidas": ["AZUL 1 x 2 AMARELO", "..."],
  "quadro": [{"time": "AZUL", "v": N, "e": N, "d": N, "gols": N}],
  "gols": [{"atleta": "NOME", "gols": N}],
  "cartoes": [{"atleta": "NOME", "tipo": "amarelo|azul|vermelho"}],
  "bola_cheia": "NOME do TROFÉU CHEIA",
  "bola_murcha": "NOME do TROFÉU MURCHA",
  "todos_pontuais": true/false,
  "atrasados": ["só quem tem X no ATRASO"],
  "incertezas": ["..."]
}
- resultado_por_time = da tabela VIT/EMP/DERR: pontos = VIT×2 + EMP×1 (derrota vale 0). Some por time.
- partidas = TODOS os placares jogo a jogo, na ordem, no formato "TIME1 G1 x G2 TIME2".
- quadro = 1 item por time que jogou, com v/e/d da tabela VIT/EMP/DERR e gols = total de gols
  do time somando as partidas.
- CONFERÊNCIA OBRIGATÓRIA: a soma dos gols dos jogadores de cada time DEVE bater com o total
  de gols do time nas PARTIDAS. Se não bater, revise a coluna GOL (provavelmente perdeu um gol);
  se mesmo assim não fechar, anote em "incertezas"."""

pendentes = {}  # chat_id -> dict extraído (súmula)
pendentes_times = {}  # chat_id -> {"res": resultado, "sabado": "DD/MM/AAAA"}


def sheet():
    creds = Credentials.from_service_account_file(
        SA_JSON, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def so_eu(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ALLOWED_ID


_alias_cache = None


def _chave(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return "".join(ch for ch in s.upper() if ch.isalnum())


def carregar_alias() -> dict:
    """{chave_normalizada: NOME_CANONICO} a partir da aba Atletas (nome + apelidos)."""
    global _alias_cache
    if _alias_cache is not None:
        return _alias_cache
    ws = sheet().worksheet("Atletas")
    m = {}
    for r in ws.get_all_records():
        nome = str(r.get("nome", "")).strip()
        if not nome:
            continue
        m[_chave(nome)] = nome.upper()
        for a in str(r.get("apelidos", "")).replace(",", ";").split(";"):
            a = a.strip()
            if a:
                m.setdefault(_chave(a), nome.upper())
    _alias_cache = m
    return m


def _resolver(raw: str, m: dict):
    """Retorna (NOME_CANONICO, reconhecido?). Usa match exato e depois aproximado."""
    if not raw or not str(raw).strip():
        return raw, True
    k = _chave(raw)
    if k in m:
        return m[k], True
    ap = difflib.get_close_matches(k, list(m.keys()), n=1, cutoff=0.72)
    if ap:
        return m[ap[0]], True
    return str(raw).strip().upper(), False


def normalizar_dados(d: dict) -> dict:
    """Converte todos os nomes lidos para os nomes do cadastro; flag os não reconhecidos."""
    m = carregar_alias()
    nao = []

    def fix(nome):
        c, ok = _resolver(nome, m)
        if not ok and nome:
            nao.append(str(nome).strip())
        return c

    for t, nomes in (d.get("times") or {}).items():
        d["times"][t] = [fix(n) for n in (nomes or [])]
    for t in list((d.get("goleiros") or {}).keys()):
        if d["goleiros"][t]:
            d["goleiros"][t] = fix(d["goleiros"][t])
    for g in (d.get("gols") or []):
        g["atleta"] = fix(g.get("atleta"))
    for c in (d.get("cartoes") or []):
        c["atleta"] = fix(c.get("atleta"))
    if d.get("bola_cheia"):
        d["bola_cheia"] = fix(d["bola_cheia"])
    if d.get("bola_murcha"):
        d["bola_murcha"] = fix(d["bola_murcha"])
    d["atrasados"] = [fix(n) for n in (d.get("atrasados") or [])]
    if nao:
        d.setdefault("incertezas", []).insert(
            0, "⚠️ NÃO reconheci no cadastro (confira): " + ", ".join(sorted(set(nao))))
    return d


def extrair_sumula(img_bytes: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(img_bytes).decode()
    msg = anthropic_client.messages.create(
        model="claude-opus-5",
        max_tokens=16000,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
             "media_type": media_type, "data": b64}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    return _parse_json(next((b.text for b in msg.content if b.type == "text"), ""))


def _parse_json(txt: str) -> dict:
    txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if not txt.startswith("{"):  # pega só o objeto JSON, se vier com texto em volta
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            txt = txt[i:j + 1]
    return json.loads(txt)


def aplicar_correcao(d: dict, texto: str) -> dict:
    """Aplica uma correção em linguagem natural aos dados já extraídos."""
    prompt = (
        "Estes são os dados extraídos de uma súmula de racha (JSON):\n"
        + json.dumps(d, ensure_ascii=False)
        + f'\n\nO usuário pediu esta correção: "{texto}"\n\n'
        "Aplique a correção e responda SÓ com o JSON completo atualizado, no MESMO formato "
        "(mesmas chaves). Não invente outros campos. Se a correção mexer em gols/vitórias, "
        "atualize também 'incertezas' removendo o que já foi resolvido.")
    msg = anthropic_client.messages.create(
        model="claude-opus-5", max_tokens=8000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}])
    return _parse_json(next((b.text for b in msg.content if b.type == "text"), ""))


def formata_resumo(d: dict) -> str:
    L = []
    L.append(f"📋 Súmula {d.get('data') or '(data ilegível)'}")
    for t in ("AZUL", "AMARELO", "ROSA"):
        nomes = d.get("times", {}).get(t) or []
        gk = d.get("goleiros", {}).get(t)
        if nomes or gk:
            extra = f" (GK {gk})" if gk else ""
            L.append(f"🔹 {t}: {', '.join(nomes)}{extra}")
    if d.get("resultado_por_time"):
        L.append("🏆 Vitórias (pts): " + ", ".join(
            f"{t} {p}" for t, p in d["resultado_por_time"].items() if p))
    if d.get("quadro"):
        L.append("📊 Quadro: " + " | ".join(
            f"{q.get('time')} {q.get('v',0)}V {q.get('e',0)}E {q.get('d',0)}D ({q.get('gols',0)} gols)"
            for q in d["quadro"]))
    if d.get("partidas"):
        L.append("🎯 Partidas: " + "; ".join(d["partidas"]))
    gols = d.get("gols") or []
    if gols:
        L.append("⚽ Gols: " + ", ".join(f"{g['atleta']} {g['gols']}" for g in gols))
    cart = d.get("cartoes") or []
    L.append("🟨 Cartões: " + (", ".join(f"{c['atleta']} ({c['tipo']})" for c in cart) or "nenhum"))
    L.append(f"⭐ Bola cheia: {d.get('bola_cheia') or '—'} | Murcha: {d.get('bola_murcha') or '—'}")
    L.append("⏱️ Pontualidade: " + ("todos" if d.get("todos_pontuais")
             else "atrasados: " + (", ".join(d.get("atrasados") or []) or "—")))
    inc = d.get("incertezas") or []
    if inc:
        L.append("\n⚠️ Não tenho certeza de:\n- " + "\n- ".join(inc))
    L.append("\nConfere? Se estiver certo, toque em Confirmar.")
    return "\n".join(L)


def gravar_lancamentos(d: dict):
    """Acrescenta uma linha por atleta presente na aba Lancamentos + a data em Sabados."""
    sh = sheet()
    data = d.get("data") or datetime.date.today().isoformat()
    # proteção contra duplo-lançamento (ex.: Confirmar clicado 2x após um erro)
    ja = [r for r in sh.worksheet("Lancamentos").get_all_values()[1:] if r and r[0] == data]
    if ja:
        raise RuntimeError(
            f"o racha de {data} já tem {len(ja)} lançamentos na planilha — não gravei de novo. "
            "Se precisar relançar, apague as linhas dessa data primeiro.")
    gols = {g["atleta"].strip().upper(): g["gols"] for g in (d.get("gols") or [])}
    cart_map = {"amarelo": -1, "azul": -2, "vermelho": -5}
    cart = {}
    for c in (d.get("cartoes") or []):
        cart[c["atleta"].strip().upper()] = cart.get(c["atleta"].strip().upper(), 0) + cart_map.get(c["tipo"], 0)
    bola = {}
    if d.get("bola_cheia"):
        bola[d["bola_cheia"].strip().upper()] = 2
    if d.get("bola_murcha"):
        bola[d["bola_murcha"].strip().upper()] = bola.get(d["bola_murcha"].strip().upper(), 0) - 1
    atrasados = {a.strip().upper() for a in (d.get("atrasados") or [])}
    vit = d.get("resultado_por_time") or {}

    linhas = []
    for time, nomes in (d.get("times") or {}).items():
        # goleiro do time entra também, se ainda não estiver na lista (evita duplicar)
        gk = (d.get("goleiros", {}).get(time) or "").strip()
        todos = list(nomes or [])
        if gk and gk.upper() not in [str(n).strip().upper() for n in todos]:
            todos.append(gk)
        for nome in todos:
            n = nome.strip().upper()
            presenca_pts = 2 if (d.get("todos_pontuais") is False and n in atrasados) else 3
            linhas.append([data, n, time,
                           presenca_pts,
                           gols.get(n, ""),
                           vit.get(time, "") or "",
                           bola.get(n, ""),
                           cart.get(n, "")])
    ws = sh.worksheet("Lancamentos")
    ws.append_rows(linhas, value_input_option="RAW")

    # Sabados: acrescenta a data se ainda não existir
    wss = sh.worksheet("Sabados")
    datas = [r[0] for r in wss.get_all_values()[1:]]
    if data not in datas:
        tipo = "racha"
        wss.append_row([data, tipo, "sim", "sim", ""], value_input_option="RAW")
    return len(linhas)


def gravar_ultimo_racha(d: dict):
    """Grava data/partidas/quadro/bolas na aba UltimoRacha (o site lê de lá)."""
    sh = sheet()
    try:
        ws = sh.worksheet("UltimoRacha")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="UltimoRacha", rows=5, cols=6)
    data = d.get("data") or datetime.date.today().isoformat()
    if "-" in data:  # AAAA-MM-DD -> DD/MM/AAAA
        a, m, dd = data.split("-")
        data = f"{dd}/{m}/{a}"
    ws.clear()
    ws.update(values=[
        ["data", "partidas_json", "quadro_json", "bola_cheia", "bola_murcha"],
        [data,
         json.dumps(d.get("partidas") or [], ensure_ascii=False),
         json.dumps(d.get("quadro") or [], ensure_ascii=False),
         str(d.get("bola_cheia") or ""),
         str(d.get("bola_murcha") or "")],
    ], range_name="A1", value_input_option="RAW")


def republicar():
    # sys.executable = o Python que roda o bot (funciona no venv do servidor e no Windows)
    subprocess.run([sys.executable, "gerar_painel_sheets.py"], cwd=APP_DIR, check=True)


# ---------------- montagem de times ----------------
def buscar_confirmados():
    """(sabado, [nomes]) do sistema de presença do site."""
    import gerar_painel as G
    r = requests.get(G.RSVP_URL, params={"action": "list"}, timeout=30)
    r.raise_for_status()
    d = r.json()
    return d.get("sabado", ""), list(d.get("vou") or [])


def montar_times_do_dia(n_times=None, lista=None):
    import gerar_painel_sheets as S
    T = S.carregar()
    if lista:
        sabado, nomes = "", lista
        try:
            sabado, _ = buscar_confirmados()
        except Exception:
            pass
    else:
        sabado, nomes = buscar_confirmados()
    res = M.montar(nomes, T, n_times)
    return sabado, res


def ajustar_times(res: dict, texto: str) -> dict:
    """Ajuste em linguagem natural ('troca X com Y', 'põe Z no azul') -> nova atribuição."""
    atual = {t["cor"]: [a["nome"].title() for a in t["atletas"]] for t in res["times"]}
    prompt = (
        "Estes são os times atuais de um racha (JSON {COR: [nomes]}):\n"
        + json.dumps(atual, ensure_ascii=False)
        + f'\n\nO usuário pediu este ajuste: "{texto}"\n\n'
        "Aplique o ajuste e responda SÓ com o JSON completo atualizado no MESMO formato "
        "{COR: [nomes]}, mantendo todos os nomes (não invente nem remova ninguém, a não ser "
        "que o pedido seja explicitamente tirar alguém). Use as mesmas cores.")
    msg = anthropic_client.messages.create(
        model="claude-opus-5", max_tokens=4000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}])
    novo = _parse_json(next((b.text for b in msg.content if b.type == "text"), ""))
    import gerar_painel_sheets as S
    T = S.carregar()
    return M.montar_manual(novo, T)


def gravar_times(res: dict, sabado: str):
    """Grava os times na aba 'Times' (o site lê de lá). Sem níveis."""
    sh = sheet()
    try:
        ws = sh.worksheet("Times")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Times", rows=60, cols=9)
    ws.clear()
    ws.update(values=[["sabado", "cor", "atleta", "posicao", "goleiro", "titular", "fiel_pts", "fiel", "ordem"]]
              + M.para_linhas(res, sabado), range_name="A1", value_input_option="RAW")


def _kb_times():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📣 Publicar no site", callback_data="times_ok"),
        InlineKeyboardButton("❌ Cancelar", callback_data="times_no")]])


async def cmd_times(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """'/times' ou 'times' [2|3|4] [: nome, nome, ...]  -> monta os times dos confirmados."""
    if not so_eu(update):
        return
    texto = (update.message.text or "").strip()
    corpo = texto.split(None, 1)[1] if " " in texto else ""
    n_times, lista = None, None
    if ":" in corpo:
        cab, nomes = corpo.split(":", 1)
        lista = [n.strip() for n in nomes.replace("\n", ",").split(",") if n.strip()]
        corpo = cab
    for tok in corpo.split():
        if tok.isdigit():
            n_times = int(tok)
    await update.message.reply_text("🧮 Buscando confirmados e montando os times...")
    try:
        sabado, res = await asyncio.to_thread(montar_times_do_dia, n_times, lista)
    except Exception as e:
        await update.message.reply_text(f"❌ Não consegui montar: {e}")
        return
    if res["n_confirmados"] < 2:
        await update.message.reply_text(
            f"Só {res['n_confirmados']} confirmado(s) para {sabado or 'sábado'} — ainda não dá pra montar. "
            "Pode mandar a lista manual: `times: Nome, Nome, ...`")
        return
    pendentes_times[update.effective_chat.id] = {"res": res, "sabado": sabado}
    await update.message.reply_text(M.formatar(res, sabado), reply_markup=_kb_times())
    await update.message.reply_text(
        M.formatar_privado(res) + "\n\nPra ajustar, me escreve (ex.: \"troca o Wesley com o Zé\", "
        "\"põe o Enzo no amarelo\", \"times 3\"). Depois toque em Publicar.")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not so_eu(update):
        return
    await update.message.reply_text(
        "⚽ Bot do Racha REA pronto!\nManda a foto da súmula que eu leio, "
        "te mando o resumo pra conferir e, no seu OK, lanço no painel.")


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not so_eu(update):
        return
    await update.message.reply_text("👀 Lendo a súmula...")
    photo = update.message.photo[-1]  # maior resolução
    f = await ctx.bot.get_file(photo.file_id)
    img = bytes(await f.download_as_bytearray())
    try:
        d = await asyncio.to_thread(extrair_sumula, img, "image/jpeg")
        d = await asyncio.to_thread(normalizar_dados, d)
    except Exception as e:
        await update.message.reply_text(f"❌ Não consegui ler: {e}")
        return
    pendentes[update.effective_chat.id] = d
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirmar", callback_data="ok"),
        InlineKeyboardButton("❌ Cancelar", callback_data="no")]])
    await update.message.reply_text(formata_resumo(d), reply_markup=kb)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not so_eu(update):
        return
    cid = update.effective_chat.id
    txt = (update.message.text or "").strip()
    if txt.lower().startswith("times") or txt.lower().startswith("/times"):
        return await cmd_times(update, ctx)
    d = pendentes.get(cid)
    if not d and cid in pendentes_times:
        # ajuste dos times por chat
        await update.message.reply_text("✏️ Ajustando os times...")
        try:
            res = await asyncio.to_thread(ajustar_times, pendentes_times[cid]["res"], txt)
        except Exception as e:
            await update.message.reply_text(f"❌ Não consegui ajustar: {e}")
            return
        pendentes_times[cid]["res"] = res
        await update.message.reply_text(M.formatar(res, pendentes_times[cid]["sabado"]), reply_markup=_kb_times())
        await update.message.reply_text(M.formatar_privado(res))
        return
    if not d:
        await update.message.reply_text(
            "Manda a foto da súmula que eu leio (ou escreve *times* pra eu montar os times dos "
            "confirmados). Depois do resumo, se algo estiver errado, é só me escrever a correção "
            "(ex.: \"Walter 1 gol\", \"troca o Zé com o Enzo\").")
        return
    await update.message.reply_text("✏️ Aplicando sua correção...")
    try:
        d = await asyncio.to_thread(aplicar_correcao, d, update.message.text)
        d = await asyncio.to_thread(normalizar_dados, d)
    except Exception as e:
        await update.message.reply_text(f"❌ Não consegui aplicar a correção: {e}")
        return
    pendentes[cid] = d
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirmar", callback_data="ok"),
        InlineKeyboardButton("❌ Cancelar", callback_data="no")]])
    await update.message.reply_text(formata_resumo(d), reply_markup=kb)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ALLOWED_ID:
        return
    cid = q.message.chat.id
    if q.data in ("times_ok", "times_no"):
        pt = pendentes_times.pop(cid, None)
        if q.data == "times_no" or not pt:
            await q.edit_message_text("❌ Times descartados. Escreve *times* quando quiser montar de novo.")
            return
        await q.edit_message_text("⏳ Publicando os times no site...")
        try:
            await asyncio.to_thread(gravar_times, pt["res"], pt["sabado"])
            await asyncio.to_thread(republicar)
            await q.message.reply_text(
                "✅ Times publicados! Manda o link pro grupo:\nhttps://racharea.com.br (aba 👥 Times)\n\n"
                "Pode copiar a mensagem dos times acima pro WhatsApp também (ela não mostra níveis).")
        except Exception as e:
            await q.message.reply_text(f"❌ Erro ao publicar: {e}")
        return
    d = pendentes.pop(cid, None)
    if q.data == "no" or d is None:
        await q.edit_message_text("❌ Cancelado. Manda a foto de novo quando quiser.")
        return
    await q.edit_message_text("⏳ Lançando no painel...")
    duplicado = False
    n = 0
    try:
        n = await asyncio.to_thread(gravar_lancamentos, d)
    except RuntimeError as e:  # data já lançada — segue só com o quadro do último racha
        duplicado = True
        await q.message.reply_text(f"ℹ️ {e}")
    except Exception as e:
        await q.message.reply_text(f"❌ Erro ao lançar: {e}")
        return
    try:
        await asyncio.to_thread(gravar_ultimo_racha, d)
    except Exception as e:
        await q.message.reply_text(f"⚠️ Não consegui atualizar o quadro 'Último racha': {e}")
    try:
        await asyncio.to_thread(republicar)
        ok = ("✅ Quadro 'Último racha' atualizado e painel republicado.\n" if duplicado else
              f"✅ Lançado! {n} atletas gravados, último racha atualizado e painel republicado.\n")
        await q.message.reply_text(ok + "https://racharea.com.br")
    except Exception as e:
        await q.message.reply_text(
            f"⚠️ Dados GRAVADOS — mas falhou regenerar o painel: {e}\n"
            "Sem pânico: o site atualiza sozinho em até 15 min. NÃO reenvie a súmula.")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("times", cmd_times))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    print("Bot do Racha REA rodando...")
    app.run_polling()


if __name__ == "__main__":
    main()
