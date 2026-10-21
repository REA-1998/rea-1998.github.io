# -*- coding: utf-8 -*-
"""
Preço de venda do boteco = custo + margem. Fonte única da regra.

A margem fica na coluna `margem` da aba BarProdutos, por produto (ex.: 0,15 = 15%).
Produto sem margem preenchida usa o padrão da categoria. Assim dá para ver na planilha
por que cada preço é o que é, e para mexer na margem de um item sem mexer em código.

Uso:
  python precos.py                 tabela de conferência (custo, margem, preço, confere?)
  python precos.py --aplicar       recalcula os preços a partir de custo + margem
  python precos.py --margem 0.20 --produto "Heineken 330" --aplicar
"""
import os
import sys
import argparse

from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

# padrão por categoria, usado quando a linha não tem margem própria
MARGEM_PADRAO = {"Cerveja": 0.15, "Refri": 0.15, "Bebida": 0.15, "Salgado": 0.40,
                 "Resenha": 0.0}   # resenha e rateio de custo, nao tem margem
PADRAO_GERAL = 0.15


def nf(x):
    try:
        return float(str(x).replace("%", "").replace(",", ".") or 0)
    except (TypeError, ValueError):
        return 0.0


def margem_de(linha):
    """Margem do produto: a da planilha se estiver preenchida, senão a da categoria."""
    bruto = str(linha.get("margem", "")).strip()
    if bruto:                      # preenchido vale, inclusive 0 (resenha vai a preço de custo)
        m = nf(bruto)
        if m > 1:                  # alguém digitou "15" em vez de 0,15
            m = m / 100.0
        return max(0.0, m)
    return MARGEM_PADRAO.get(str(linha.get("categoria", "")).strip(), PADRAO_GERAL)


def preco_de(custo, margem):
    """Preço de venda, com a margem exata (sem arredondar para cima)."""
    return round(nf(custo) * (1 + margem) + 1e-9, 2)


def conferir(prods):
    """Devolve [(produto, custo, margem, preco_atual, preco_certo)] do que não bate."""
    fora = []
    for r in prods:
        c = nf(r.get("custo"))
        if c <= 0:
            continue
        m = margem_de(r)
        certo = preco_de(c, m)
        if abs(nf(r.get("preco")) - certo) > 0.005:
            fora.append((str(r.get("produto", "")).strip(), c, m, nf(r.get("preco")), certo))
    return fora


def sheets():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SA_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])


def garantir_coluna(w, hdr, nome):
    """Cria a coluna no fim da aba se ainda não existir."""
    if nome in hdr:
        return hdr, hdr.index(nome) + 1
    col = len(hdr) + 1
    if w.col_count < col:                 # a aba precisa ter a coluna antes de escrever nela
        w.add_cols(col - w.col_count)
    w.update(values=[[nome]], range_name=rowcol_to_a1(1, col), value_input_option="RAW")
    return hdr + [nome], col


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true", help="grava os preços recalculados")
    ap.add_argument("--margem", type=float, help="define a margem (0.15 = 15%%)")
    ap.add_argument("--produto", help="só este produto (usar com --margem)")
    ap.add_argument("--categoria", help="só esta categoria (usar com --margem)")
    a = ap.parse_args()

    sh = sheets()
    w = sh.worksheet("BarProdutos")
    hdr = w.row_values(1)
    hdr, col_margem = garantir_coluna(w, hdr, "margem")
    col_preco = hdr.index("preco") + 1
    prods = w.get_all_records()

    ups = []
    print(f"{'PRODUTO':<22}{'categoria':<10}{'custo':>8}{'margem':>8}{'preço':>8}{'certo':>8}  ")
    print("-" * 70)
    for i, r in enumerate(prods, start=2):
        nome = str(r.get("produto", "")).strip()
        if a.margem is not None and (
                (a.produto and nome.lower() != a.produto.strip().lower())
                or (a.categoria and str(r.get("categoria", "")).strip().lower()
                    != a.categoria.strip().lower())):
            pass
        elif a.margem is not None:
            r = dict(r, margem=a.margem)
            ups.append({"range": rowcol_to_a1(i, col_margem), "values": [[a.margem]]})
        m = margem_de(r)
        if not str(r.get("margem", "")).strip():
            ups.append({"range": rowcol_to_a1(i, col_margem), "values": [[m]]})
        c, atual = nf(r.get("custo")), nf(r.get("preco"))
        certo = preco_de(c, m)
        bate = abs(atual - certo) <= 0.005
        if not bate:
            ups.append({"range": rowcol_to_a1(i, col_preco), "values": [[certo]]})
        print(f"{nome:<22}{str(r.get('categoria','')):<10}{c:>8.2f}{m * 100:>7.0f}%"
              f"{atual:>8.2f}{certo:>8.2f}  {'' if bate else '<<< fora da margem'}")

    fora = conferir(prods)
    print("-" * 70)
    print(f"produtos fora da margem: {len(fora)}")
    if not a.aplicar:
        if ups:
            print(f"({len(ups)} ajustes pendentes — rode com --aplicar para gravar)")
        return 0
    if ups:
        w.batch_update(ups, value_input_option="RAW")
        print(f"gravados {len(ups)} ajustes (margem e/ou preço).")
    else:
        print("nada a ajustar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
