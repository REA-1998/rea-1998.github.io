# -*- coding: utf-8 -*-
"""
Põe no boteco o item da resenha DO DIA, com o nome e o valor daquele evento.

Existe porque o rateio muda a cada resenha e os atletas lançavam um múltiplo do item
"Resenha criança" para fechar o valor combinado: em 14/11/2026 o rateio era R$ 15,00 por
adulto e cada um lançou 3x "Resenha criança" de R$ 5,00. Funciona, mas o histórico fica
ilegível — o BarConsumo de 14/11 lê como se 39 crianças tivessem comido — e o atleta
precisa saber de cabeça quantas unidades marcar. Com um item por evento, ele toca uma vez
no botão certo e a linha nasce com o valor e a data no próprio nome.

Só um evento fica ativo por vez: ao criar o novo, os anteriores (e os itens genéricos)
saem do catálogo. Nada é apagado — os lançamentos antigos continuam com o nome que
tinham, que é justamente o que torna o histórico legível depois.

Uso:
    # na sexta, quando o valor é anunciado no grupo
    python resenha_evento.py --valor 15,00                   # próximo sábado, criança isenta
    python resenha_evento.py --valor 20,00 --crianca 5,00    # adulto e criança
    python resenha_evento.py --valor 18,00 --data 28/11      # outra data

    python resenha_evento.py --listar      # o que está no catálogo agora
    python resenha_evento.py --encerrar    # fim da resenha: tira todos do catálogo
    python resenha_evento.py --generico    # volta aos itens fixos de R$ 20,00 e R$ 5,00
    ... --dry                              # só mostra o que faria
"""
import os
import sys
import argparse
import datetime

from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

ABA = "BarProdutos"
CATEGORIA = "Resenha"
GENERICOS = ("Resenha churrasco", "Resenha criança")


def nf(x):
    """Aceita 15,00 e 15.00 — o valor chega do WhatsApp com vírgula."""
    try:
        return float(str(x).replace(".", "").replace(",", ".").strip()) \
            if ("," in str(x)) else float(str(x).strip() or 0)
    except (TypeError, ValueError):
        raise SystemExit("valor inválido: %r (use 15,00 ou 15.00)" % x)


def sheets():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SA_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])


def proximo_sabado(hoje=None):
    """O sábado que vem; se hoje é sábado, é hoje."""
    hoje = hoje or datetime.date.today()
    return hoje + datetime.timedelta(days=(5 - hoje.weekday()) % 7)


def parse_data(txt):
    """'28/11' ou '28/11/2026' -> date. Sem ano, assume o ano corrente."""
    p = [int(x) for x in str(txt).strip().split("/")]
    ano = p[2] if len(p) > 2 else datetime.date.today().year
    if ano < 100:
        ano += 2000
    return datetime.date(ano, p[1], p[0])


def _ci(hdr, nome):
    return hdr.index(nome)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--valor", help="rateio do adulto, ex: 15,00")
    ap.add_argument("--crianca", help="rateio da criança; omita quando criança é isenta")
    ap.add_argument("--data", help="data da resenha, ex: 28/11 (padrão: próximo sábado)")
    ap.add_argument("--listar", action="store_true")
    ap.add_argument("--encerrar", action="store_true", help="tira todos os itens de resenha")
    ap.add_argument("--generico", action="store_true", help="volta aos itens fixos de 20/5")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    if not (a.valor or a.listar or a.encerrar or a.generico):
        ap.error("informe --valor, ou use --listar / --encerrar / --generico")

    sh = sheets()
    ws = sh.worksheet(ABA)
    vals = ws.get_all_values()
    hdr = vals[0]
    c_prod = _ci(hdr, "produto")
    c_cat = _ci(hdr, "categoria")
    c_custo = _ci(hdr, "custo")
    c_preco = _ci(hdr, "preco")
    c_ativo = _ci(hdr, "ativo")

    # linhas de resenha que existem hoje: {nome: (linha, preco, ativo)}
    atuais = {}
    for i, r in enumerate(vals[1:], start=2):
        if r[c_cat].strip().lower() == CATEGORIA.lower():
            atuais[r[c_prod].strip()] = (i, r[c_preco].strip(), r[c_ativo].strip())

    if a.listar:
        print("itens de resenha no catálogo:")
        for nome, (lin, preco, ativo) in sorted(atuais.items()):
            print("  %-26s R$ %-8s ativo=%-4s  (L%d)" % (nome, preco, ativo, lin))
        ativos = [n for n, (_, _, at) in atuais.items() if at.lower() == "sim"]
        print("\nno boteco agora: %s" % (", ".join(sorted(ativos)) or "nenhum"))
        return 0

    # ------------------------------------------------- quem deve ficar ativo
    if a.encerrar:
        querer = {}
        print("encerrando: nenhum item de resenha fica no boteco")
    elif a.generico:
        querer = {GENERICOS[0]: 20.00, GENERICOS[1]: 5.00}
        print("voltando aos itens fixos: %s" % ", ".join(GENERICOS))
    else:
        d = parse_data(a.data) if a.data else proximo_sabado()
        if d.weekday() != 5:
            print("aviso: %s não é sábado (%s)" % (d.strftime("%d/%m/%Y"),
                                                   "seg ter qua qui sex sáb dom".split()[d.weekday()]))
        rot = d.strftime("%d/%m")
        querer = {"Resenha %s" % rot: nf(a.valor)}
        if a.crianca:
            querer["Resenha %s criança" % rot] = nf(a.crianca)
        print("resenha de %s:" % d.strftime("%d/%m/%Y"))
        for nome, v in querer.items():
            print("   %-26s R$ %.2f" % (nome, v))
        if not a.crianca:
            print("   (criança isenta — nenhum item de criança)")

    # ------------------------------------------------- monta as alterações
    ups, novas = [], []
    for nome, preco in querer.items():
        if nome in atuais:
            lin, antes, ativo = atuais[nome]
            if abs(nf(antes) - preco) > 0.001 or ativo.lower() != "sim":
                print("  atualiza %-26s R$ %s -> R$ %.2f, ativo=%s -> sim"
                      % (nome, antes or "-", preco, ativo or "-"))
                ups.append({"range": rowcol_to_a1(lin, c_custo + 1), "values": [["%.2f" % preco]]})
                ups.append({"range": rowcol_to_a1(lin, c_preco + 1), "values": [["%.2f" % preco]]})
                ups.append({"range": rowcol_to_a1(lin, c_ativo + 1), "values": [["sim"]]})
            else:
                print("  já está certo: %s" % nome)
        else:
            linha = [""] * len(hdr)
            linha[c_prod] = nome
            linha[c_cat] = CATEGORIA
            linha[c_custo] = "%.2f" % preco
            linha[c_preco] = "%.2f" % preco
            linha[_ci(hdr, "estoque")] = "0"
            linha[c_ativo] = "sim"
            linha[_ci(hdr, "margem")] = "0"
            linha[_ci(hdr, "controla_estoque")] = "nao"
            novas.append(linha)
            print("  cria     %-26s R$ %.2f" % (nome, preco))

    for nome, (lin, preco, ativo) in sorted(atuais.items()):
        if nome not in querer and ativo.lower() == "sim":
            print("  tira do boteco: %-26s (R$ %s, fica no histórico)" % (nome, preco))
            ups.append({"range": rowcol_to_a1(lin, c_ativo + 1), "values": [["nao"]]})

    if not ups and not novas:
        print("\nnada a fazer.")
        return 0
    if a.dry:
        print("\n(--dry: nada gravado)")
        return 0

    if ups:
        ws.batch_update(ups, value_input_option="RAW")
    if novas:
        ws.append_rows(novas, value_input_option="RAW")
    print("\ngravado: %d células, %d itens novos." % (len(ups), len(novas)))
    print("O boteco pega na próxima geração do painel (até 15 min) ou rode:")
    print("  /opt/racha/app/deploy/gerar-panel.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
