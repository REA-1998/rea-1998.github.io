# -*- coding: utf-8 -*-
"""
Recebedor do webhook Pix da Efí (Racha REA).

Quando um atleta paga a cobrança, a Efí faz um POST aqui com o txid. Este serviço:
  1. acha o txid na aba PixCobrancas -> descobre atleta e mês
  2. marca a cobrança como CONCLUIDA (pago_em, e2eid)
  3. lança o pagamento na aba Pagamentos (valor_pago + data)
  4. (opcional) dispara a regeneração do painel

Rodar o serviço:
    python pix_webhook.py                 # sobe o Flask (porta PIX_WEBHOOK_PORT, padrão 8090)

Registrar a URL do webhook na Efí (uma vez, quando o servidor estiver no ar):
    python pix_webhook.py registrar https://racharea.com.br/pix-racha/webhook

Segurança: o endpoint fica atrás de um token secreto no caminho (PIX_WEBHOOK_TOKEN) e o
registro usa skip-mTLS (a Efí valida pela posse do token/URL). Rode sempre via HTTPS (Caddy).
"""
import os, sys, json, subprocess, datetime
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1
import conta as C  # conta-corrente do atleta (fonte unica da matematica)

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_SA_JSON = os.environ["GOOGLE_SA_JSON"]
TOKEN = os.environ.get("PIX_WEBHOOK_TOKEN", "racha")     # segredo no caminho da URL
PORT = int(os.environ.get("PIX_WEBHOOK_PORT", "8090"))
PANEL_REFRESH_CMD = os.environ.get("PANEL_REFRESH_CMD", "")  # ex.: no servidor, o gerar-panel.sh


def sheets():
    creds = Credentials.from_service_account_file(
        GOOGLE_SA_JSON, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def _ci(header, name):
    return header.index(name)


def _nf(x):
    try:
        return float(str(x).replace(",", ".") or 0)
    except (TypeError, ValueError):
        return 0.0


def _efi():
    from efipay import EfiPay
    return EfiPay({
        "client_id": os.environ["EFI_CLIENT_ID"],
        "client_secret": os.environ["EFI_CLIENT_SECRET"],
        "certificate": os.environ["EFI_CERT_PATH"],
        "sandbox": os.environ.get("EFI_SANDBOX", "false").lower() == "true",
    })


TETO_CONSUMO = float(os.environ.get("BAR_TETO", "100"))


_CACHE = {}          # aba -> (momento, linhas) — segura a cota do Google no sábado
_WS = {}             # aba -> objeto gspread


def _ws(sh, nome):
    """O gspread busca os metadados da planilha a cada _ws(sh, ), e isso conta na cota
    de leitura. No sábado, com todo mundo lançando junto, era o que estourava o limite."""
    w = _WS.get(nome)
    if w is None:
        w = sh.worksheet(nome)
        _WS[nome] = w
    return w


def _aba(sh, nome, seg=45):
    """Lê a aba com cache curto: no sábado dezenas de atletas confirmam presença ao mesmo
    tempo e cada checagem lia a planilha inteira três vezes."""
    agora = datetime.datetime.now()
    hit = _CACHE.get(nome)
    if hit and (agora - hit[0]).total_seconds() < seg:
        return hit[1]
    linhas = _ws(sh, nome).get_all_records()
    _CACHE[nome] = (agora, linhas)
    return linhas


def _controla_estoque(p):
    """Produto de serviço (resenha) não tem prateleira: marcado com controla_estoque=nao."""
    return str(p.get("controla_estoque", "sim")).strip().lower() not in ("nao", "não", "n")


def _consumo_aberto(sh, atleta, mes):
    """Quanto o atleta tem de consumo ainda não pago no mês.

    Conta-corrente (conta.py): o pagamento abate PRIMEIRO o boteco, mas só o consumo que
    JÁ EXISTIA quando o dinheiro entrou. Quem pagou de manhã e bebeu à noite continua
    devendo a bebida — e por isso trava a presença."""
    alvo = atleta.strip().upper()
    reg = next((r for r in _aba(sh, "Pagamentos")
                if str(r.get("mes", "")).strip().upper() == mes.upper()
                and str(r.get("atleta", "")).strip().upper() == alvo), None)
    if reg is None:
        return 0.0
    ini = datetime.date.today().replace(day=1)
    cons = C.consumos_por_atleta(_aba(sh, "BarConsumo"), ini=ini).get(alvo, [])
    pgs = C.pagamentos_por_atleta(_aba(sh, "PixCobrancas"), ini=ini).get(alvo, [])
    return C.extrato(reg, cons, pgs, ini)["boteco_aberto"]


def _somar_consumo_na_conta(sh, atleta, mes, valor):
    """Soma o consumo na linha do mês (cria a linha se não existir)."""
    pg = _ws(sh, "Pagamentos")
    gv = pg.get_all_values(); gh = gv[0]
    def gi(n): return _ci(gh, n)
    for i, r in enumerate(gv[1:], start=2):
        if (r[gi("mes")].strip().upper() == mes.upper()
                and r[gi("atleta")].strip().upper() == atleta.upper()):
            novo = _nf(r[gi("consumo")]) + valor
            pg.update(values=[[f"{novo:.2f}"]], range_name=rowcol_to_a1(i, gi("consumo")+1),
                      value_input_option="RAW")
            return novo
    isento = any(str(a.get("nome", "")).strip().upper() == atleta.upper()
                 and str(a.get("isento_mensalidade", "")).strip().lower() == "sim"
                 for a in _ws(sh, "Atletas").get_all_records())
    row = [""] * len(gh)
    row[gi("mes")] = mes; row[gi("atleta")] = atleta
    row[gi("mensalidade")] = "0" if isento else "90"
    row[gi("consumo")] = f"{valor:.2f}"
    row[gi("valor_pago")] = "0"
    row[gi("obs")] = "linha criada pelo boteco" + (" (isento de mensalidade)" if isento else "")
    pg.append_row(row, value_input_option="RAW")
    return valor


def _mes_atual():
    M = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
    h = datetime.date.today()
    return f"{M[h.month - 1]} {h.year}"


def nova_cobranca_saldo(sh, atleta, mes, saldo):
    """Depois de um pagamento parcial, emite um novo QR com o que ainda falta."""
    import random
    import string
    import unicodedata
    efi = _efi()
    slug = "".join(c for c in unicodedata.normalize("NFKD", atleta).encode("ascii", "ignore").decode()
                   if c.isalnum()).upper()
    suf = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    txid = (f"RACHA{mes.replace(' ', '')}{slug}{suf}")[:35]
    if len(txid) < 26:
        txid += "X" * (26 - len(txid))
    body = {"calendario": {"expiracao": 60 * 60 * 24 * 40},
            "valor": {"original": f"{saldo:.2f}"},
            "chave": os.environ["EFI_PIX_KEY"],
            "solicitacaoPagador": f"Saldo {mes} - Racha REA - {atleta.title()}"}
    resp = efi.pix_create_charge(params={"txid": txid}, body=body)
    if not isinstance(resp, dict) or "loc" not in resp:
        print("Falha ao criar cobranca de saldo:", resp)
        return None
    copia = resp.get("pixCopiaECola", "")
    if not copia:
        qr = efi.pix_generate_qrcode(params={"id": resp["loc"]["id"]})
        copia = qr.get("qrcode", "") if isinstance(qr, dict) else ""
    agora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _ws(sh, "PixCobrancas").append_row(
        [mes, atleta, txid, f"{saldo:.2f}", "ATIVA", str(resp["loc"]["id"]),
         resp.get("location", ""), copia, agora, "", ""], value_input_option="RAW")
    print(f"Nova cobranca de saldo: {atleta} {mes} R$ {saldo:.2f}")
    return txid


def registrar_pagamento(sh, txid, valor, horario, e2eid):
    """Casa o txid com o atleta/mês e grava o pagamento. Retorna um resumo (dict)."""
    pix_ws = _ws(sh, "PixCobrancas")
    pv = pix_ws.get_all_values(); ph = pv[0]
    linha = atleta = mes = None
    for i, r in enumerate(pv[1:], start=2):
        if r[_ci(ph, "txid")].strip() == txid.strip():
            linha, atleta, mes = i, r[_ci(ph, "atleta")].strip(), r[_ci(ph, "mes")].strip()
            break
    if not linha:
        return {"ok": False, "motivo": f"txid nao encontrado: {txid}"}

    data_pgto = (horario or "")[:10]  # AAAA-MM-DD
    # 1) marca a cobranca como concluida
    pix_ws.update(values=[["CONCLUIDA"]], range_name=rowcol_to_a1(linha, _ci(ph, "status")+1), value_input_option="RAW")
    pix_ws.update(values=[[horario or ""]], range_name=rowcol_to_a1(linha, _ci(ph, "pago_em")+1), value_input_option="RAW")
    pix_ws.update(values=[[e2eid or ""]], range_name=rowcol_to_a1(linha, _ci(ph, "e2eid")+1), value_input_option="RAW")

    # 2) lanca em Pagamentos (upsert por mes+atleta)
    pg = _ws(sh, "Pagamentos")
    gv = pg.get_all_values(); gh = gv[0]
    def gi(n): return _ci(gh, n)
    alvo = None
    for i, r in enumerate(gv[1:], start=2):
        if r[gi("mes")].strip().upper() == mes.upper() and r[gi("atleta")].strip().upper() == atleta.upper():
            alvo = i; existente = r; break
    val = float(str(valor).replace(",", "."))
    saldo = 0.0
    if alvo:
        atual = _nf(existente[gi("valor_pago")])
        novo = atual + val
        pg.update(values=[[f"{novo:.2f}"]], range_name=rowcol_to_a1(alvo, gi("valor_pago")+1), value_input_option="RAW")
        pg.update(values=[[data_pgto]], range_name=rowcol_to_a1(alvo, gi("data_pgto")+1), value_input_option="RAW")
        obs = (existente[gi("obs")] + " | Pix auto (Efi)").strip(" |")
        pg.update(values=[[obs]], range_name=rowcol_to_a1(alvo, gi("obs")+1), value_input_option="RAW")
        devido = (_nf(existente[gi("s_a")]) + _nf(existente[gi("mensalidade")])
                  + _nf(existente[gi("multa_chu")]) + _nf(existente[gi("consumo")]))
        saldo = round(devido - novo, 2)
    else:
        row = [""] * len(gh)
        row[gi("mes")] = mes; row[gi("atleta")] = atleta
        row[gi("mensalidade")] = "90"; row[gi("valor_pago")] = f"{val:.2f}"
        row[gi("data_pgto")] = data_pgto; row[gi("obs")] = "Pix auto (Efi)"
        pg.append_row(row, value_input_option="RAW")
        saldo = round(90.0 - val, 2)
    _CACHE.clear()   # pagamento conciliado muda a conta
    # pagamento parcial -> emite automaticamente um novo QR com o que falta
    if saldo > 0.009:
        try:
            nova_cobranca_saldo(sh, atleta, mes, saldo)
        except Exception as e:
            print("Nao consegui emitir a cobranca de saldo:", e)
    return {"ok": True, "atleta": atleta, "mes": mes, "valor": val, "saldo": saldo}


def processar_payload(sh, data):
    resultados = []
    for pix in (data or {}).get("pix", []):
        resultados.append(registrar_pagamento(
            sh, pix.get("txid", ""), pix.get("valor", "0"),
            pix.get("horario", ""), pix.get("endToEndId", "")))
    if resultados and PANEL_REFRESH_CMD:
        try:
            subprocess.Popen(PANEL_REFRESH_CMD, shell=True)
        except Exception as e:
            print("Falha ao atualizar painel:", e)
    return resultados


# ---------------- servidor Flask ----------------
def make_app():
    from flask import Flask, request, jsonify
    app = Flask(__name__)

    @app.errorhandler(gspread.exceptions.APIError)
    def _sheets_fora(e):
        """Cota do Google estourada (muita gente lançando junto) não pode virar erro 500
        na cara do atleta — ele precisa saber que é só tentar de novo."""
        txt = str(e)
        if "429" in txt or "Quota" in txt:
            return jsonify({"ok": False, "erro": "muita gente lançando ao mesmo tempo. "
                                                 "Espere uns 30 segundos e tente de novo."}), 503
        print("Erro do Sheets:", txt[:300])
        return jsonify({"ok": False, "erro": "a planilha não respondeu agora. "
                                             "Tente de novo em instantes."}), 503
    sh = sheets()

    @app.get("/pix-racha/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/pix-racha/bar/produtos")
    def bar_produtos():
        """Catálogo com estoque, para a tela do boteco."""
        itens = [{"produto": str(p.get("produto", "")), "categoria": str(p.get("categoria", "")),
                  "preco": _nf(p.get("preco")), "estoque": int(_nf(p.get("estoque"))),
                  "controla": _controla_estoque(p)}
                 for p in _ws(sh, "BarProdutos").get_all_records()
                 if str(p.get("ativo", "")).strip().lower() == "sim"]
        return jsonify({"ok": True, "itens": itens})

    @app.post("/pix-racha/bar/consumo")
    def bar_consumo():
        """Atleta marca o que consumiu. Entra na conta dele (coluna 'consumo' do mês).
        Body: {"atleta":"WALTER","itens":[{"produto":"Skol 300","qtd":2}, ...]}"""
        d = request.get_json(silent=True) or {}
        atleta = str(d.get("atleta", "")).strip().upper()
        pedidos = d.get("itens") or []
        if not atleta or not pedidos:
            return jsonify({"ok": False, "erro": "informe atleta e itens"}), 400
        atletas = {str(a.get("nome", "")).strip().upper(): a
                   for a in _aba(sh, "Atletas", 300)
                   if str(a.get("ativo", "")).strip().lower() == "sim"}
        if atleta not in atletas:
            return jsonify({"ok": False, "erro": "atleta não encontrado"}), 404
        pw_ = _ws(sh, "BarProdutos")
        prods = pw_.get_all_values()
        phdr = prods[0]
        pi = {n: phdr.index(n) for n in ("produto", "preco", "estoque")}
        ic = phdr.index("controla_estoque") if "controla_estoque" in phdr else None
        linha_de = {r[pi["produto"]].strip().lower(): (i, r) for i, r in enumerate(prods[1:], start=2)}
        mes = _mes_atual()
        hoje = datetime.date.today().isoformat()
        agora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        novos, total, ups_estoque = [], 0.0, []
        for it in pedidos:
            nome = str(it.get("produto", "")).strip().lower()
            qtd = int(_nf(it.get("qtd")))
            if nome not in linha_de or qtd < 1:
                continue
            i, r = linha_de[nome]
            preco = _nf(r[pi["preco"]])
            est = int(_nf(r[pi["estoque"]]))
            # itens de serviço (resenha) não têm estoque: não trancam nem baixam prateleira
            controla = not (ic is not None and str(r[ic]).strip().lower() in ("nao", "não", "n"))
            if controla and qtd > est:
                return jsonify({"ok": False, "erro": f"só restam {est} de {r[pi['produto']]}"}), 409
            sub = round(preco * qtd, 2)
            total += sub
            novos.append([hoje, atleta, r[pi["produto"]], qtd, f"{preco:.2f}", f"{sub:.2f}", agora])
            if controla:
                ups_estoque.append({"range": rowcol_to_a1(i, pi["estoque"] + 1),
                                    "values": [[est - qtd]]})
        if not novos:
            return jsonify({"ok": False, "erro": "nenhum item válido"}), 400
        # trava: teto de consumo em aberto
        aberto = _consumo_aberto(sh, atleta, mes)
        if aberto + total > TETO_CONSUMO:
            return jsonify({"ok": False,
                            "erro": f"limite de R$ {TETO_CONSUMO:.0f} atingido "
                                    f"(você já tem R$ {aberto:.2f} em aberto). Acerte antes de consumir mais."}), 409
        _ws(sh, "BarConsumo").append_rows(novos, value_input_option="RAW")
        for aba in ("BarConsumo", "Pagamentos"):   # só o que o lançamento mexeu;
            _CACHE.pop(aba, None)                  # recarregar tudo estourava a cota
        if ups_estoque:
            pw_.batch_update(ups_estoque, value_input_option="RAW")
        _somar_consumo_na_conta(sh, atleta, mes, total)
        return jsonify({"ok": True, "total": f"{total:.2f}",
                        "em_aberto": f"{aberto + total:.2f}", "itens": len(novos)})

    @app.post("/pix-racha/cobranca")
    def cobranca_sob_medida():
        """Gera um QR com o valor que o atleta escolheu (pagar parte ou adiantar).
        Body: {"atleta": "WALTER", "valor": 50.00}"""
        d = request.get_json(silent=True) or {}
        atleta = str(d.get("atleta", "")).strip()
        valor = _nf(d.get("valor"))
        if not atleta or valor < 1 or valor > 5000:
            return jsonify({"ok": False, "erro": "informe atleta e um valor entre R$1 e R$5.000"}), 400
        # só aceita atleta ativo do cadastro
        nomes = {str(a.get("nome", "")).strip().upper() for a in _ws(sh, "Atletas").get_all_records()
                 if str(a.get("ativo", "")).strip().lower() == "sim"}
        if atleta.upper() not in nomes:
            return jsonify({"ok": False, "erro": "atleta não encontrado"}), 404
        mes = _mes_atual()
        try:
            txid = nova_cobranca_saldo(sh, atleta.upper(), mes, round(valor, 2))
        except Exception as e:
            print("Erro na cobranca sob medida:", e)
            return jsonify({"ok": False, "erro": "falha ao gerar"}), 500
        if not txid:
            return jsonify({"ok": False, "erro": "falha ao gerar"}), 500
        xv = _ws(sh, "PixCobrancas").get_all_values(); xh = xv[0]
        for r in reversed(xv[1:]):
            if r[_ci(xh, "txid")].strip() == txid:
                return jsonify({"ok": True, "copia": r[_ci(xh, "pix_copia_cola")],
                                "valor": f"{valor:.2f}", "atleta": atleta.title()})
        return jsonify({"ok": False, "erro": "gerado, mas não encontrei o QR"}), 500

    # aceita a URL com token e tambem o /pix que a Efi acrescenta
    @app.post(f"/pix-racha/webhook/{TOKEN}")
    @app.post(f"/pix-racha/webhook/{TOKEN}/pix")
    @app.post("/pix-racha/webhook")
    @app.post("/pix-racha/webhook/pix")
    def webhook():
        data = request.get_json(silent=True) or {}
        print("WEBHOOK recebido:", json.dumps(data)[:500])
        res = processar_payload(sh, data)
        print("Processado:", res)
        return jsonify({"ok": True, "processados": res}), 200

    return app


def registrar_webhook(url):
    """Registra a URL do webhook na Efi (skip-mTLS)."""
    from efipay import EfiPay
    efi = EfiPay({
        "client_id": os.environ["EFI_CLIENT_ID"],
        "client_secret": os.environ["EFI_CLIENT_SECRET"],
        "certificate": os.environ["EFI_CERT_PATH"],
        "sandbox": os.environ.get("EFI_SANDBOX", "false").lower() == "true",
    })
    resp = efi.pix_config_webhook(
        params={"chave": os.environ["EFI_PIX_KEY"]},
        body={"webhookUrl": url},
        headers={"x-skip-mtls-checking": "true"})
    print("Registro do webhook:", json.dumps(resp, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "registrar":
        registrar_webhook(sys.argv[2])
    else:
        make_app().run(host="0.0.0.0", port=PORT)
