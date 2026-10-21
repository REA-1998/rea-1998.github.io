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
import sys
import time
import datetime
import collections

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
IDADE_MIN = 120       # segundos — não mexe em cobrança recém-criada (corrida com o webhook)


def sheets():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SA_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])


def ajustar_consumo(sh, mes, ini_mes):
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
    if ups:
        pg.batch_update(ups, value_input_option="RAW")
    for nome, v in por.items():
        if nome not in vistos and v > 0:
            print(f"  ATENCAO: {nome} tem R$ {v:.2f} de consumo e nenhuma linha em {mes}")
    return mudou


def planejar(sh, mes, ini_mes):
    """Devolve (cancelar, criar) sem tocar em nada."""
    pc = sh.worksheet("PixCobrancas")
    pix_rows = pc.get_all_records()
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
    for r in sh.worksheet("Pagamentos").get_all_records():
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
    return pc, cancelar, criar


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
                print(f"  ATENCAO    {nome:<12} {txid} nao cancelou: {st}")
        except Exception as e:
            print(f"  ERRO ao cancelar {nome} {txid}: {str(e)[:120]}")
        time.sleep(0.4)
    if ups:
        pc.batch_update(ups, value_input_option="RAW")
    for nome, valor in criar:
        try:
            if W.nova_cobranca_saldo(sh, nome, _mes(), valor):
                feitas += 1
        except Exception as e:
            print(f"  ERRO ao criar {nome} R$ {valor:.2f}: {str(e)[:120]}")
        time.sleep(1.0)
    return feitas


def _mes():
    h = datetime.date.today()
    return f"{MESES[h.month - 1]} {h.year}"


def main():
    dry = "--dry" in sys.argv
    hoje = datetime.date.today()
    mes, ini_mes = _mes(), hoje.replace(day=1)
    sh = sheets()
    for nome, antes, agora in ajustar_consumo(sh, mes, ini_mes):
        print(f"[consumo] {nome}: {antes:.2f} -> {agora:.2f} (recalculado do BarConsumo)")
    pc, cancelar, criar = planejar(sh, mes, ini_mes)
    if not cancelar and not criar:
        print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] cobrancas de {mes}: tudo certo")
        return
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] {mes}: "
          f"{len(cancelar)} a cancelar, {len(criar)} a criar")
    for nome, _, valor, txid in cancelar:
        print(f"  - cancelar {nome:<12} R$ {valor:>8.2f}  {txid}")
    for nome, valor in criar:
        print(f"  + criar    {nome:<12} R$ {valor:>8.2f}")
    if len(cancelar) + len(criar) > MAX_ACOES:
        print(f"!! {len(cancelar) + len(criar)} acoes passa do limite de {MAX_ACOES}. "
              "Nao vou mexer sozinho — rode na mao depois de conferir.")
        return
    if dry:
        print("(--dry: nada aplicado)")
        return
    print("feitas:", aplicar(sh, pc, cancelar, criar))


if __name__ == "__main__":
    main()
