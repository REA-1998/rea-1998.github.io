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
import json
import base64
import subprocess
import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import anthropic
import gspread
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

PROMPT = """Você é o auxiliar de súmula de um racha de futebol (REA). Recebe a FOTO de uma
súmula manuscrita e extrai os FATOS em JSON. NÃO invente: se não tiver certeza de algo,
coloque em "incertezas" e deixe o campo vazio.

Regras de pontuação (para referência, NÃO calcule o Craque, só extraia os fatos):
- Times: AZUL, AMARELO, ROSA. Alguns dias jogam só 2 times.
- Cada atleta presente vale presença; pontual = chegou no horário.
- Gols por atleta. Cartões: amarelo/azul/vermelho. Bola cheia (troféu) e bola murcha.

Responda SÓ com JSON neste formato:
{
  "data": "AAAA-MM-DD ou vazio se ilegível",
  "so_dois_times": true/false,
  "times": {"AZUL": ["NOME",...], "AMARELO": [...], "ROSA": [...]},
  "goleiros": {"AZUL": "NOME ou vazio", "AMARELO": "...", "ROSA": "..."},
  "resultado_por_time": {"AZUL": pontos_de_vitoria_no_dia, "AMARELO": ..., "ROSA": ...},
  "gols": [{"atleta": "NOME", "gols": N}],
  "cartoes": [{"atleta": "NOME", "tipo": "amarelo|azul|vermelho"}],
  "bola_cheia": "NOME ou vazio",
  "bola_murcha": "NOME ou vazio",
  "todos_pontuais": true/false,
  "atrasados": ["NOME",...],
  "incertezas": ["texto do que ficou em dúvida"]
}
"resultado_por_time" = total de pontos de vitória do time no dia (vitória=2, empate=1),
somando as partidas; se não der pra ler as partidas, deixe 0 e anote em incertezas."""

pendentes = {}  # chat_id -> dict extraído


def sheet():
    creds = Credentials.from_service_account_file(
        SA_JSON, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def so_eu(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ALLOWED_ID


def extrair_sumula(img_bytes: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(img_bytes).decode()
    msg = anthropic_client.messages.create(
        model="claude-opus-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
             "media_type": media_type, "data": b64}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    txt = next((b.text for b in msg.content if b.type == "text"), "{}")
    txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(txt)


def formata_resumo(d: dict) -> str:
    L = []
    L.append(f"📋 *Súmula {d.get('data') or '(data ilegível)'}*")
    for t in ("AZUL", "AMARELO", "ROSA"):
        nomes = d.get("times", {}).get(t) or []
        gk = d.get("goleiros", {}).get(t)
        if nomes or gk:
            extra = f" (GK {gk})" if gk else ""
            L.append(f"🔹 {t}: {', '.join(nomes)}{extra}")
    if d.get("resultado_por_time"):
        L.append("🏆 Vitórias (pts): " + ", ".join(
            f"{t} {p}" for t, p in d["resultado_por_time"].items() if p))
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
        L.append("\n⚠️ *Não tenho certeza de:*\n- " + "\n- ".join(inc))
    L.append("\nConfere? Se estiver certo, toque em *Confirmar*.")
    return "\n".join(L)


def gravar_lancamentos(d: dict):
    """Acrescenta uma linha por atleta presente na aba Lancamentos + a data em Sabados."""
    sh = sheet()
    data = d.get("data") or datetime.date.today().isoformat()
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
        # goleiro do time entra também
        gk = (d.get("goleiros", {}).get(time) or "").strip()
        todos = list(nomes or []) + ([gk] if gk else [])
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


def republicar():
    subprocess.run(["python", "gerar_painel_sheets.py"], cwd=APP_DIR, check=True)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not so_eu(update):
        return
    await update.message.reply_text(
        "⚽ Bot do Racha REA pronto!\nManda a *foto da súmula* que eu leio, "
        "te mando o resumo pra conferir e, no seu OK, lanço no painel.",
        parse_mode="Markdown")


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not so_eu(update):
        return
    await update.message.reply_text("👀 Lendo a súmula...")
    photo = update.message.photo[-1]  # maior resolução
    f = await ctx.bot.get_file(photo.file_id)
    img = bytes(await f.download_as_bytearray())
    try:
        d = extrair_sumula(img, "image/jpeg")
    except Exception as e:
        await update.message.reply_text(f"❌ Não consegui ler: {e}")
        return
    pendentes[update.effective_chat.id] = d
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirmar", callback_data="ok"),
        InlineKeyboardButton("❌ Cancelar", callback_data="no")]])
    await update.message.reply_text(formata_resumo(d), parse_mode="Markdown", reply_markup=kb)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ALLOWED_ID:
        return
    d = pendentes.pop(q.message.chat.id, None)
    if q.data == "no" or d is None:
        await q.edit_message_text("❌ Cancelado. Manda a foto de novo quando quiser.")
        return
    await q.edit_message_text("⏳ Lançando no painel...")
    try:
        n = gravar_lancamentos(d)
        republicar()
        await q.message.reply_text(
            f"✅ Lançado! {n} atletas gravados e painel republicado.\n"
            "https://rea-1998.github.io")
    except Exception as e:
        await q.message.reply_text(f"❌ Erro ao lançar: {e}")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_button))
    print("Bot do Racha REA rodando...")
    app.run_polling()


if __name__ == "__main__":
    main()
