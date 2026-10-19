# -*- coding: utf-8 -*-
"""
Dá entrada de mercadoria no boteco pelos DOIS lados de uma vez: grava a compra na aba
BarEntradas e soma no estoque de BarProdutos.

Existe para o estoque nunca mais divergir do que foi comprado — antes a entrada era feita
só no estoque e não sobrava registro para conferir depois.

Uso:
  python entrada_estoque.py --fornecedor "Bruno" --data 2026-10-16 \
      --obs "Pix 19/10" "Heineken 330=24@6.50" "Skol 300=24@3.00"

  --preco   também atualiza o custo no catálogo e recalcula a venda pela margem
            da categoria (15% bebida, 40% salgado)
  --dry     só mostra o que faria
"""
import os
import sys
import argparse
import datetime
import collections

from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

MARGEM = {"Cerveja": 0.15, "Refri": 0.15, "Bebida": 0.15, "Salgado": 0.40}
HDR = ["data", "produto", "qtd", "custo_un", "total", "fornecedor", "obs", "registrado_em"]


def nf(x):
    try:
        return float(str(x).replace(",", ".") or 0)
    except (TypeError, ValueError):
        return 0.0


def sheets():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SA_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])


def parse_item(txt):
    """'Heineken 330=24@6.50' -> ('Heineken 330', 24, 6.50). O @custo é opcional."""
    if "=" not in txt:
        raise ValueError(f"item sem quantidade: {txt!r} (use Produto=QTD ou Produto=QTD@CUSTO)")
    nome, resto = txt.split("=", 1)
    custo = None
    if "@" in resto:
        resto, c = resto.split("@", 1)
        custo = nf(c)
    return nome.strip(), int(nf(resto)), custo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("itens", nargs="+", help="Produto=QTD[@CUSTO]")
    ap.add_argument("--fornecedor", required=True)
    ap.add_argument("--data", default=datetime.date.today().isoformat())
    ap.add_argument("--obs", default="")
    ap.add_argument("--preco", action="store_true", help="atualiza custo e venda no catálogo")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    sh = sheets()
    pw = sh.worksheet("BarProdutos")
    ph = pw.row_values(1)
    prods = pw.get_all_records()
    linha_de = {str(r.get("produto", "")).strip().lower(): (i, r)
                for i, r in enumerate(prods, start=2)}

    pedidos = [parse_item(t) for t in a.itens]
    desconhecidos = [n for n, _, _ in pedidos if n.lower() not in linha_de]
    if desconhecidos:
        print("Produto não existe no catálogo:", ", ".join(desconhecidos))
        print("Cadastre em BarProdutos antes de dar entrada.")
        return 1

    agora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    novas, ups, total_nota = [], [], 0.0
    print(f"{'PRODUTO':<22}{'qtd':>5}{'custo':>9}{'total':>9}   estoque")
    for nome, qtd, custo in pedidos:
        i, r = linha_de[nome.lower()]
        nome_real = str(r.get("produto", "")).strip()
        c = custo if custo is not None else nf(r.get("custo"))
        est_ant = int(nf(r.get("estoque")))
        est_novo = est_ant + qtd
        total_nota += qtd * c
        novas.append([a.data, nome_real, qtd, f"{c:.2f}", f"{qtd * c:.2f}",
                      a.fornecedor, a.obs, agora])
        ups.append({"range": rowcol_to_a1(i, ph.index("estoque") + 1), "values": [[est_novo]]})
        extra = ""
        if a.preco and custo is not None and abs(custo - nf(r.get("custo"))) > 0.001:
            m = MARGEM.get(str(r.get("categoria", "")).strip(), 0.15)
            venda = round(c * (1 + m) + 1e-9, 2)
            ups.append({"range": rowcol_to_a1(i, ph.index("custo") + 1), "values": [[c]]})
            ups.append({"range": rowcol_to_a1(i, ph.index("preco") + 1), "values": [[venda]]})
            extra = f"   custo {nf(r.get('custo')):.2f}->{c:.2f}  venda ->{venda:.2f}"
        print(f"{nome_real:<22}{qtd:>5}{c:>9.2f}{qtd * c:>9.2f}   {est_ant}->{est_novo}{extra}")
    print(f"\nTOTAL DA ENTRADA: R$ {total_nota:.2f}   fornecedor: {a.fornecedor}   data: {a.data}")
    if a.dry:
        print("(--dry: nada gravado)")
        return 0

    ew = _aba_entradas(sh)
    ew.append_rows(novas, value_input_option="RAW")
    pw.batch_update(ups, value_input_option="RAW")
    print("gravado em BarEntradas e somado no estoque.")
    _conferir(sh)
    return 0


def _aba_entradas(sh):
    try:
        w = sh.worksheet("BarEntradas")
    except gspread.WorksheetNotFound:
        w = sh.add_worksheet(title="BarEntradas", rows=400, cols=len(HDR))
        w.update(values=[HDR], range_name="A1", value_input_option="RAW")
    return w


def _conferir(sh):
    """entrou − vendeu tem que dar o estoque."""
    ent = collections.defaultdict(int)
    for e in sh.worksheet("BarEntradas").get_all_records():
        ent[str(e.get("produto", "")).strip()] += int(nf(e.get("qtd")))
    saiu = collections.defaultdict(int)
    for c in sh.worksheet("BarConsumo").get_all_records():
        saiu[str(c.get("produto", "")).strip()] += int(nf(c.get("qtd")))
    ruins = []
    for p in sh.worksheet("BarProdutos").get_all_records():
        nome = str(p.get("produto", "")).strip()
        est = int(nf(p.get("estoque")))
        if ent.get(nome, 0) - saiu.get(nome, 0) != est:
            ruins.append(f"  {nome}: conta {ent.get(nome, 0) - saiu.get(nome, 0)}, estoque {est}")
    print("conferência: OK, entrou − vendeu = estoque" if not ruins
          else "CONFERÊNCIA FALHOU:\n" + "\n".join(ruins))


if __name__ == "__main__":
    sys.exit(main())
