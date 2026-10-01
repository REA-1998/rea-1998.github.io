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
            "valor": {"original": f"{saldo:.2f}", "modalidadeAlteracao": 1},
            "chave": os.environ["EFI_PIX_KEY"],
            "solicitacaoPagador": f"Saldo {mes} - Racha REA - {atleta.title()} (pode alterar o valor)"}
    resp = efi.pix_create_charge(params={"txid": txid}, body=body)
    if not isinstance(resp, dict) or "loc" not in resp:
        print("Falha ao criar cobranca de saldo:", resp)
        return None
    copia = resp.get("pixCopiaECola", "")
    if not copia:
        qr = efi.pix_generate_qrcode(params={"id": resp["loc"]["id"]})
        copia = qr.get("qrcode", "") if isinstance(qr, dict) else ""
    agora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sh.worksheet("PixCobrancas").append_row(
        [mes, atleta, txid, f"{saldo:.2f}", "ATIVA", str(resp["loc"]["id"]),
         resp.get("location", ""), copia, agora, "", ""], value_input_option="RAW")
    print(f"Nova cobranca de saldo: {atleta} {mes} R$ {saldo:.2f}")
    return txid


def registrar_pagamento(sh, txid, valor, horario, e2eid):
    """Casa o txid com o atleta/mês e grava o pagamento. Retorna um resumo (dict)."""
    pix_ws = sh.worksheet("PixCobrancas")
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
    pg = sh.worksheet("Pagamentos")
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
        devido = _nf(existente[gi("s_a")]) + _nf(existente[gi("mensalidade")]) + _nf(existente[gi("multa_chu")])
        saldo = round(devido - novo, 2)
    else:
        row = [""] * len(gh)
        row[gi("mes")] = mes; row[gi("atleta")] = atleta
        row[gi("mensalidade")] = "90"; row[gi("valor_pago")] = f"{val:.2f}"
        row[gi("data_pgto")] = data_pgto; row[gi("obs")] = "Pix auto (Efi)"
        pg.append_row(row, value_input_option="RAW")
        saldo = round(90.0 - val, 2)
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
    sh = sheets()

    @app.get("/pix-racha/health")
    def health():
        return jsonify({"ok": True})

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
