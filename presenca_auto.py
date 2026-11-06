# -*- coding: utf-8 -*-
"""Fecha a lista de presença: quem não respondeu até a hora do corte entra como "Não vou".

Roda pelo racha-presenca.timer no sábado às 13h. Até ali a lista fica cheia de gente que
simplesmente não abriu o site, e o montador de times não sabe se conta com eles ou não.
Depois do corte, silêncio vale como ausência — quem mudar de ideia ainda pode clicar
"Vou jogar" no site, porque isto só grava uma resposta, não tranca nada.

O RSVP mora num Apps Script (planilha "Presença Racha REA"), não na planilha principal:
por isso a gravação é por HTTP, com a mesma chamada que o botão do site faz.

Uso:
    python presenca_auto.py          # grava
    python presenca_auto.py --dry    # só mostra quem entraria como ausente
"""
import os
import sys
import json
import time
import urllib.parse
import urllib.request

from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

import gspread
from google.oauth2.service_account import Credentials

# mesma URL que o painel usa no botão de presença
RSVP_URL = os.environ.get("RSVP_URL") or (
    "https://script.google.com/macros/s/"
    "AKfycbzqhPslN6qj8MIkwQ18EnvIGNAP3wk9CgUkGwqy203fAjJ11UfO6Ec2-pfn_n9J2FKUXw/exec")

RESPOSTA = "Não vou"
PAUSA = 0.4       # o Apps Script é lento e serializa; não adianta martelar


def _chamar(params, tentativas=3):
    # quote(safe="") escapa igual ao encodeURIComponent do botao do site: espaco vira %20,
    # nao '+'. A URL fica identica a que o Apps Script ja recebe desde sempre.
    url = RSVP_URL + "?" + "&".join(
        "%s=%s" % (k, urllib.parse.quote(str(v), safe="")) for k, v in params.items())
    erro = None
    for n in range(tentativas):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:          # rede/Apps Script instável: tenta de novo
            erro = e
            time.sleep(1 + 2 * n)
    raise RuntimeError("falhou em %s: %s" % (params.get("action"), erro))


def roster():
    """ativo=sim e joga≠não — a mesma regra do roster_ativos do painel."""
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SA_JSON"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])
    nomes = []
    for r in sh.worksheet("Atletas").get_all_records():
        if str(r.get("ativo", "")).strip().lower() != "sim":
            continue
        if str(r.get("joga", "")).strip().lower() == "não":
            continue
        nomes.append(str(r.get("nome", "")).strip().title())
    return sorted(nomes)


def main():
    dry = "--dry" in sys.argv
    lista = _chamar({"action": "list"})
    if not lista.get("ok"):
        print("Apps Script respondeu sem ok:", lista)
        return 1
    ja = {str(n).strip().title() for n in (lista.get("vou") or [])}
    ja |= {str(n).strip().title() for n in (lista.get("naovou") or [])}

    todos = roster()
    faltam = [n for n in todos if n not in ja]

    print("Sábado %s" % lista.get("sabado"))
    print("  elenco ..........: %d" % len(todos))
    print("  já responderam ..: %d (%d vou, %d não vou)"
          % (len(ja), len(lista.get("vou") or []), len(lista.get("naovou") or [])))
    print("  sem resposta ....: %d" % len(faltam))
    if not faltam:
        print("Ninguém pendente — nada a fazer.")
        return 0
    print("  -> %s" % ", ".join(faltam))
    if dry:
        print("(--dry: nada gravado)")
        return 0

    gravados, erros = [], []
    for nome in faltam:
        try:
            r = _chamar({"action": "rsvp", "atleta": nome, "resposta": RESPOSTA})
            (gravados if r.get("ok") else erros).append(nome)
        except RuntimeError as e:
            erros.append("%s (%s)" % (nome, e))
        time.sleep(PAUSA)

    print("\nmarcados como '%s': %d" % (RESPOSTA, len(gravados)))
    if erros:
        print("NÃO gravados: %s" % ", ".join(erros))

    fim = _chamar({"action": "list"})
    print("\nlista final: %d vão, %d não vão, %d sem resposta"
          % (len(fim.get("vou") or []), len(fim.get("naovou") or []),
             len(todos) - len(set(fim.get("vou") or []) | set(fim.get("naovou") or []))))
    return 1 if erros else 0


if __name__ == "__main__":
    sys.exit(main())
