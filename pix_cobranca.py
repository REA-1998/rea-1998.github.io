# -*- coding: utf-8 -*-
"""
Gerador de cobranças Pix do Racha REA (Efí).

Cria 1 cobrança Pix por atleta ativo (não isento) para um mês, guarda tudo na
aba `PixCobrancas` do Google Sheets (txid <-> atleta <-> mês <-> QR) e é idempotente
(não duplica se rodar de novo). O recebedor do webhook (pix_webhook.py) usa essa aba
para casar o pagamento com o atleta e marcar "pago" na aba Pagamentos.

Uso:
    python pix_cobranca.py "SET 2026"            # gera para todos os ativos
    python pix_cobranca.py "SET 2026" --limit 2  # só os 2 primeiros (teste)
    python pix_cobranca.py "SET 2026" --dry      # simula, não cria nada
"""
import os, sys, re, json, unicodedata, datetime
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from efipay import EfiPay

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_SA_JSON = os.environ["GOOGLE_SA_JSON"]
MENSALIDADE = str(os.environ.get("MENSALIDADE_VALOR", "90"))
MESES_OK = {"JAN","FEV","MAR","ABR","MAI","JUN","JUL","AGO","SET","OUT","NOV","DEZ"}

PIX_HEADER = ["mes","atleta","txid","valor","status","loc_id","location",
              "pix_copia_cola","criado_em","pago_em","e2eid"]


def efi_client():
    return EfiPay({
        "client_id": os.environ["EFI_CLIENT_ID"],
        "client_secret": os.environ["EFI_CLIENT_SECRET"],
        "certificate": os.environ["EFI_CERT_PATH"],
        "sandbox": os.environ.get("EFI_SANDBOX", "false").lower() == "true",
    })


def sheets():
    creds = Credentials.from_service_account_file(
        GOOGLE_SA_JSON, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


def make_txid(ano, mes3, atleta, idx):
    """txid Efí: 26 a 35 caracteres alfanuméricos. idx garante unicidade."""
    base = f"RACHA{ano}{mes3}{slug(atleta)}"[:33] + f"{idx:02d}"
    if len(base) < 26:
        base += "X" * (26 - len(base))
    return base[:35]


def ensure_pix_tab(sh):
    try:
        ws = sh.worksheet("PixCobrancas")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="PixCobrancas", rows=200, cols=len(PIX_HEADER))
        ws.append_row(PIX_HEADER, value_input_option="RAW")
        print("Aba 'PixCobrancas' criada.")
    return ws


def col(header, name):
    return header.index(name)


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    mes = sys.argv[1].strip().upper()          # ex.: "SET 2026"
    limit = None
    dry = "--dry" in sys.argv
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    m = re.match(r"^([A-Z]{3})\s+(\d{4})$", mes)
    if not m or m.group(1) not in MESES_OK:
        print(f"Mês inválido: '{mes}'. Use algo como 'SET 2026'."); sys.exit(1)
    mes3, ano = m.group(1), m.group(2)

    sh = sheets()
    atletas_ws = sh.worksheet("Atletas")
    av = atletas_ws.get_all_values()
    ah = av[0]
    c_nome = col(ah, "nome"); c_isento = col(ah, "isento_mensalidade"); c_ativo = col(ah, "ativo")

    # atletas que devem receber cobrança: ativo=sim e isento_mensalidade!=sim
    elegiveis = []
    for r in av[1:]:
        if not r or not r[c_nome].strip():
            continue
        ativo = r[c_ativo].strip().lower() in ("sim", "s", "1", "true")
        isento = r[c_isento].strip().lower() in ("sim", "s", "1", "true")
        if ativo and not isento:
            elegiveis.append(r[c_nome].strip())

    pix_ws = ensure_pix_tab(sh)
    pv = pix_ws.get_all_values()
    ph = pv[0]
    ja_tem = {(row[col(ph,"mes")].strip().upper(), row[col(ph,"atleta")].strip().upper())
              for row in pv[1:] if row}

    if limit:
        elegiveis = elegiveis[:limit]

    print(f"Mês {mes} | elegíveis: {len(elegiveis)} | mensalidade R$ {MENSALIDADE}"
          + (" | (DRY-RUN)" if dry else ""))

    efi = None if dry else efi_client()
    criadas, puladas, novas_linhas = 0, 0, []
    agora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for idx, atleta in enumerate(elegiveis, start=1):
        if (mes, atleta.upper()) in ja_tem:
            puladas += 1
            print(f"  = já existe: {atleta}")
            continue
        txid = make_txid(ano, mes3, atleta, idx)
        if dry:
            print(f"  + (dry) {atleta:<12} txid={txid}")
            criadas += 1
            continue
        body = {
            "calendario": {"expiracao": 60 * 60 * 24 * 40},  # 40 dias
            "valor": {"original": f"{float(MENSALIDADE):.2f}"},
            "chave": os.environ["EFI_PIX_KEY"],
            "solicitacaoPagador": f"Mensalidade {mes} - Racha REA - {atleta}",
        }
        try:
            resp = efi.pix_create_charge(params={"txid": txid}, body=body)
            if not isinstance(resp, dict):
                # a Efí devolve um objeto de erro (ex.: UnauthorizedError) em vez de dict
                amb = "sandbox/homologação" if os.environ.get("EFI_SANDBOX","false").lower()=="true" else "produção"
                raise SystemExit(
                    f"\n! Efí NÃO autorizou ({type(resp).__name__}). Resposta: {resp}\n"
                    f"  Ambiente configurado: {amb} (EFI_SANDBOX).\n"
                    f"  Verifique se CLIENT_ID/SECRET, o certificado (.pem) e EFI_SANDBOX\n"
                    f"  são TODOS do MESMO ambiente. Para cobrança real: keys de PRODUÇÃO,\n"
                    f"  cert de PRODUÇÃO e EFI_SANDBOX=false. Abortando (nada foi criado).")
            loc_id = resp.get("loc", {}).get("id", "")
            location = resp.get("location", "")
            copia = resp.get("pixCopiaECola", "")
            if not copia and loc_id:
                qr = efi.pix_generate_qrcode(params={"id": loc_id})
                copia = qr.get("qrcode", "") if isinstance(qr, dict) else ""
            novas_linhas.append([mes, atleta, txid, f"{float(MENSALIDADE):.2f}",
                                 resp.get("status","ATIVA"), str(loc_id), location,
                                 copia, agora, "", ""])
            criadas += 1
            print(f"  + criada: {atleta:<12} R$ {MENSALIDADE}  txid={txid}")
        except SystemExit:
            if novas_linhas:
                pix_ws.append_rows(novas_linhas, value_input_option="RAW")
            raise
        except Exception as e:
            print(f"  ! ERRO em {atleta}: {e}")

    if novas_linhas:
        pix_ws.append_rows(novas_linhas, value_input_option="RAW")

    print(f"\nResumo: {criadas} criadas, {puladas} já existiam.")


if __name__ == "__main__":
    main()
