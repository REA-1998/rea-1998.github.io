# -*- coding: utf-8 -*-
"""
Mantém as cobranças Pix em dia: cada atleta com dívida tem UMA cobrança ativa, no valor
exato que ele deve HOJE.

Existe porque a dívida muda sozinha depois que o QR é emitido — alguém lança consumo no
boteco, o tesoureiro usa o 'desfazer', entra um pagamento parcial. O QR antigo continua
válido com o valor velho e a pessoa paga errado (foi o que aconteceu com o Vinícius em
19/10/2026: QR de 57,00 depois do desfazer que baixou a conta para 42,30).

Roda pelo racha-cobrancas.timer a cada 15 min. Só age quando acha diferença.
Uso manual:  python reconciliar_cobrancas.py           (aplica)
             python reconciliar_cobrancas.py --dry     (só mostra o que faria)
"""
import os
import io
import sys
import json
import time
import hashlib
import datetime
import collections

import requests

from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))
sys.path.insert(0, BASE)

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

import conta as C
import pix_webhook as W

MESES = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
MIN_VALOR = 1.00      # abaixo disso não vale um Pix
MAX_ACOES = 25        # trava de segurança: se precisar mexer em mais que isso, algo está errado
MAX_VELHAS = 60       # cancelar cobranca velha é a direção segura, então o limite é maior
IDADE_MIN = 120       # segundos — não mexe em cobrança recém-criada (corrida com o webhook)


ARQ_ALERTA = os.path.join(BASE, ".ultimo_alerta_cobrancas.json")
REPETIR_APOS = 6 * 3600        # segundos: o mesmo aviso nao volta antes disso

ALERTAS = []                   # o que o script recusou fazer ou nao conseguiu
AVISOS = []                    # o que ele fez e vale contar (limpeza de mes anterior)


def alerta(msg):
    """Problema que exige olho humano: vai para o log E para o Telegram."""
    print("  " + msg)
    ALERTAS.append(msg)


def sheets():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SA_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])


def _ja_avisei(corpo):
    """True se este mesmo aviso ja foi mandado ha menos de REPETIR_APOS.

    Sem isto, um problema que persiste (a virada que falhou para um atleta) viraria uma
    mensagem a cada 15 minutos ate alguem resolver."""
    chave = hashlib.sha256(corpo.encode("utf-8")).hexdigest()
    agora = time.time()
    try:
        with io.open(ARQ_ALERTA, encoding="utf-8") as f:
            ant = json.load(f)
        if ant.get("hash") == chave and agora - float(ant.get("ts", 0)) < REPETIR_APOS:
            return True
    except Exception:
        pass
    try:
        with io.open(ARQ_ALERTA, "w", encoding="utf-8") as f:
            json.dump({"hash": chave, "ts": agora}, f)
    except Exception as e:
        print("  (nao consegui gravar o controle de repeticao: %s)" % e)
    return False


def avisar_telegram():
    """Manda ALERTAS e AVISOS para o Telegram. Silencioso quando nao ha nada."""
    if not ALERTAS and not AVISOS:
        return
    linhas = []
    if ALERTAS:
        linhas.append("🚨 <b>Cobranças Pix — preciso de você</b>")
        linhas += ["• " + m for m in ALERTAS]
    if AVISOS:
        if linhas:
            linhas.append("")
        linhas.append("ℹ️ <b>Cobranças Pix — limpeza automática</b>")
        linhas += ["• " + m for m in AVISOS]
    corpo = "\n".join(linhas)
    if _ja_avisei(corpo):
        print("  (mesmo aviso ja mandado ha menos de 6h — nao repeti)")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    destinos = [x.strip() for x in str(os.environ.get("TELEGRAM_ALLOWED_ID", ""))
                .replace(";", ",").split(",") if x.strip()]
    if not token or not destinos:
        print("  (sem TELEGRAM_BOT_TOKEN/TELEGRAM_ALLOWED_ID — nao avisei)")
        return
    for chat in destinos:
        try:
            r = requests.post("https://api.telegram.org/bot%s/sendMessage" % token,
                              json={"chat_id": chat, "text": corpo, "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=30)
            print("  telegram %s: %s" % (chat, r.status_code))
        except Exception as e:
            print("  ERRO ao avisar no Telegram: %s" % str(e)[:120])


def ajustar_consumo(sh, mes, ini_mes, dry=False):
    """Recalcula a coluna `consumo` de Pagamentos a partir do BarConsumo.

    Lançar no boteco escreve em três lugares (BarConsumo, estoque e a conta do atleta) e
    isso não é atômico: se a cota do Google estourar no meio, fica lançamento registrado
    sem cobrança — aconteceu em 21/10/2026. Como o BarConsumo é a fonte da verdade, basta
    recalcular; o conserto vem sozinho na próxima passada, em no máximo 15 minutos.
    """
    por = {}
    for c in sh.worksheet("BarConsumo").get_all_records():
        d = C.ts(c.get("data"))
        if d is None or d.date() < ini_mes:
            continue
        nome = str(c.get("atleta", "")).strip().upper()
        por[nome] = round(por.get(nome, 0.0) + C.nf(c.get("total")), 2)

    pg = sh.worksheet("Pagamentos")
    cc = pg.row_values(1).index("consumo") + 1
    ups, mudou, vistos = [], [], set()
    for i, r in enumerate(pg.get_all_records(), start=2):
        if str(r.get("mes", "")).strip().upper() != mes:
            continue
        nome = str(r.get("atleta", "")).strip().upper()
        vistos.add(nome)
        certo = por.get(nome, 0.0)
        atual = C.nf(r.get("consumo"))
        if abs(atual - certo) > 0.005:
            ups.append({"range": rowcol_to_a1(i, cc), "values": [[f"{certo:.2f}"]]})
            mudou.append((nome, atual, certo))
    if ups and not dry:
        pg.batch_update(ups, value_input_option="RAW")
    for nome, v in por.items():
        if nome not in vistos and v > 0:
            alerta(f"{nome} tem R$ {v:.2f} de consumo no boteco e NENHUMA linha em "
                   f"{mes} — não consigo cobrar dele")
    return mudou


def planejar(sh, mes, ini_mes):
    """Devolve (pc, velhas, cancelar, criar) sem tocar em nada.

    `velhas` sao cobrancas ATIVAS de meses ANTERIORES — ver planejar_velhas()."""
    pc = sh.worksheet("PixCobrancas")
    pix_rows = pc.get_all_records()
    pag_rows = sh.worksheet("Pagamentos").get_all_records()
    cons = C.consumos_por_atleta(sh.worksheet("BarConsumo").get_all_records(), ini=ini_mes)
    pgs = C.pagamentos_por_atleta(pix_rows, ini=ini_mes)

    agora = datetime.datetime.now()
    ativas = collections.defaultdict(list)
    for i, p in enumerate(pix_rows, start=2):
        if (str(p.get("mes", "")).strip().upper() != mes
                or str(p.get("status", "")).strip().upper() != "ATIVA"):
            continue
        nasceu = C.ts(p.get("criado_em"))
        nova_demais = nasceu is not None and (agora - nasceu).total_seconds() < IDADE_MIN
        ativas[str(p.get("atleta", "")).strip().upper()].append(
            (i, C.nf(p.get("valor")), str(p.get("txid")), nova_demais))

    cancelar, criar = [], []
    for r in pag_rows:
        if str(r.get("mes", "")).strip().upper() != mes:
            continue
        nome = str(r.get("atleta", "")).strip().upper()
        ext = C.extrato(r, cons.get(nome, []), pgs.get(nome, []), ini_mes)
        devido = round(ext["fixo_aberto"] + ext["boteco_aberto"], 2)
        atv = ativas.get(nome, [])
        if any(nova for *_, nova in atv):
            continue                                   # deixa o webhook terminar o que começou
        certa = next((a for a in atv if abs(a[1] - devido) < 0.01 and devido > 0), None)
        for a in atv:
            if not (certa and a[2] == certa[2]):
                cancelar.append((nome, a[0], a[1], a[2]))
        if not certa and devido >= MIN_VALOR:
            criar.append((nome, devido))
    return pc, planejar_velhas(pix_rows, pag_rows, mes), cancelar, criar


def planejar_velhas(pix_rows, pag_rows, mes):
    """Cobrancas ATIVAS de meses ANTERIORES — todas erradas, por construcao.

    Na virada do mes o que ficou em aberto vira s_a da linha nova e passa a ser cobrado
    junto com a mensalidade do mes. O QR do mes passado nao e cancelado por ninguem e
    continua valido na Efi, cobrando um valor que ja nao existe. Em 27/11/2026 oito
    atletas tinham duas cobrancas ativas; a do Paulinho era de R$ 100,88 que ele ja
    quitara em 14/11.

    So cancela quem JA TEM linha no mes corrente. Sem linha, a virada pode nao ter
    acontecido para ele e cancelar deixaria o atleta sem forma de pagar — nesse caso
    avisa e nao mexe.
    """
    com_linha = {str(r.get("atleta", "")).strip().upper() for r in pag_rows
                 if str(r.get("mes", "")).strip().upper() == mes}
    velhas = []
    for i, p in enumerate(pix_rows, start=2):
        if str(p.get("status", "")).strip().upper() != "ATIVA":
            continue
        m = str(p.get("mes", "")).strip().upper()
        if not m or m == mes:
            continue
        nome = str(p.get("atleta", "")).strip().upper()
        if nome not in com_linha:
            alerta(f"{nome} tem cobrança Pix ATIVA de {m} (R$ {C.nf(p.get('valor')):.2f}) "
                   f"e nenhuma linha em {mes} — não cancelei; confira a virada do mês")
            continue
        velhas.append((f"{nome} ({m})", i, C.nf(p.get("valor")), str(p.get("txid"))))
    return velhas


def aplicar(sh, pc, cancelar, criar):
    hdr = pc.row_values(1)
    col_status = hdr.index("status") + 1
    efi = W._efi()
    ups, feitas = [], 0
    for nome, linha, valor, txid in cancelar:
        try:
            resp = efi.pix_update_charge(params={"txid": txid},
                                         body={"status": "REMOVIDA_PELO_USUARIO_RECEBEDOR"})
            st = resp.get("status") if isinstance(resp, dict) else resp
            if str(st).upper().startswith("REMOVIDA"):
                ups.append({"range": rowcol_to_a1(linha, col_status), "values": [["CANCELADA"]]})
                feitas += 1
                print(f"  cancelada  {nome:<12} R$ {valor:>8.2f}  {txid}")
            else:
                alerta(f"não consegui cancelar a cobrança de {nome} "
                       f"(R$ {valor:.2f}) na Efí: {st}")
        except Exception as e:
            alerta(f"erro ao cancelar a cobrança de {nome} (R$ {valor:.2f}): "
                   f"{str(e)[:100]}")
        time.sleep(0.4)
    if ups:
        pc.batch_update(ups, value_input_option="RAW")
    for nome, valor in criar:
        try:
            if W.nova_cobranca_saldo(sh, nome, _mes(), valor):
                feitas += 1
        except Exception as e:
            alerta(f"erro ao criar a cobrança de {nome} (R$ {valor:.2f}): "
                   f"{str(e)[:100]}")
        time.sleep(1.0)
    return feitas


def _mes():
    h = datetime.date.today()
    return f"{MESES[h.month - 1]} {h.year}"


def main():
    try:
        return _main()
    finally:
        if "--dry" not in sys.argv:
            avisar_telegram()


def _main():
    dry = "--dry" in sys.argv
    hoje = datetime.date.today()
    mes, ini_mes = _mes(), hoje.replace(day=1)
    sh = sheets()
    for nome, antes, agora in ajustar_consumo(sh, mes, ini_mes, dry):
        print(f"[consumo] {nome}: {antes:.2f} -> {agora:.2f} (recalculado do BarConsumo)")
    pc, velhas, cancelar, criar = planejar(sh, mes, ini_mes)
    if velhas:
        print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] "
              f"{len(velhas)} cobranca(s) ATIVA(s) de meses anteriores — cancelando:")
        for nome, _, valor, txid in velhas:
            print(f"  - velha    {nome:<18} R$ {valor:>8.2f}  {txid}")
        if len(velhas) > MAX_VELHAS:
            alerta(f"{len(velhas)} cobranças de meses anteriores passam do limite de "
                   f"{MAX_VELHAS} — não mexi sozinho")
            velhas = []
        elif not dry:
            n = aplicar(sh, pc, velhas, [])
            print("  canceladas:", n)
            if n:
                AVISOS.append("cancelei %d cobrança(s) de mês anterior que continuavam "
                              "válidas na Efí: %s" % (n, ", ".join(
                                  "%s R$ %.2f" % (nm, vl) for nm, _, vl, _ in velhas)))
    if not cancelar and not criar:
        if not velhas:
            print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] cobrancas de {mes}: tudo certo")
        return
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] {mes}: "
          f"{len(cancelar)} a cancelar, {len(criar)} a criar")
    for nome, _, valor, txid in cancelar:
        print(f"  - cancelar {nome:<12} R$ {valor:>8.2f}  {txid}")
    for nome, valor in criar:
        print(f"  + criar    {nome:<12} R$ {valor:>8.2f}")
    if len(cancelar) + len(criar) > MAX_ACOES:
        alerta(f"{len(cancelar) + len(criar)} ações passam do limite de {MAX_ACOES} — "
               "não mexi sozinho; confira e rode na mão")
        return
    if dry:
        print("(--dry: nada aplicado)")
        return
    print("feitas:", aplicar(sh, pc, cancelar, criar))


if __name__ == "__main__":
    main()
