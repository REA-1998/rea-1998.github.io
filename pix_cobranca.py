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
MESES_OK_ORDEM = ["JAN","FEV","MAR","ABR","MAI","JUN","JUL","AGO","SET","OUT","NOV","DEZ"]

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


def _nf(x):
    try:
        return float(str(x).replace(",", ".") or 0)
    except (TypeError, ValueError):
        return 0.0


def _mes_anterior(mes3, ano):
    i = MESES_OK_ORDEM.index(mes3)
    return (MESES_OK_ORDEM[i - 1], str(int(ano) - 1)) if i == 0 else (MESES_OK_ORDEM[i - 1], ano)


def virar_mes(sh, mes, mes3, ano):
    """Cria as linhas do mês na aba Pagamentos (uma por atleta ativo não isento),
    trazendo o saldo do mês anterior como s_a e aplicando a multa de virada (R$30)
    para quem virou o mês devendo a mensalidade. Idempotente."""
    pg = sh.worksheet("Pagamentos")
    hdr = pg.row_values(1)
    recs = pg.get_all_records()
    ja = {str(r["atleta"]).strip().upper() for r in recs
          if str(r.get("mes", "")).strip().upper() == mes.upper()}
    m3_ant, ano_ant = _mes_anterior(mes3, ano)
    mes_ant = f"{m3_ant} {ano_ant}"
    saldo = {}
    for r in recs:
        if str(r.get("mes", "")).strip().upper() == mes_ant.upper():
            saldo[str(r["atleta"]).strip().upper()] = (
                _nf(r["s_a"]) + _nf(r["mensalidade"]) + _nf(r["multa_chu"]) - _nf(r["valor_pago"]))
    novas = []
    for a in sh.worksheet("Atletas").get_all_records():
        if str(a.get("ativo", "")).strip().lower() != "sim":
            continue
        if str(a.get("isento_mensalidade", "")).strip().lower() == "sim":
            continue
        nome = str(a["nome"]).strip().upper()
        if nome in ja:
            continue
        sa = round(saldo.get(nome, 0.0), 2)
        multa = 30 if sa >= 90 else 0
        d = {"mes": mes, "atleta": nome, "s_a": sa, "mensalidade": float(MENSALIDADE),
             "multa_chu": multa, "valor_pago": 0, "data_pgto": "",
             "obs": f"multa de virada (devia {mes_ant})" if multa else ""}
        novas.append([d.get(c, "") for c in hdr])
    if novas:
        pg.append_rows(novas, value_input_option="RAW")
    print(f"Virada de mês: {len(novas)} linhas criadas em Pagamentos ({mes}).")
    return novas


def valores_devidos(sh, mes):
    """{ATLETA: valor a cobrar} = s_a + mensalidade + multa − já pago (mín. 0)."""
    out = {}
    for r in sh.worksheet("Pagamentos").get_all_records():
        if str(r.get("mes", "")).strip().upper() != mes.upper():
            continue
        v = _nf(r["s_a"]) + _nf(r["mensalidade"]) + _nf(r["multa_chu"]) - _nf(r["valor_pago"])
        out[str(r["atleta"]).strip().upper()] = max(0.0, round(v, 2))
    return out


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

    # vira o mês na aba Pagamentos (traz saldo anterior + multa de virada) antes de cobrar
    if not dry:
        virar_mes(sh, mes, mes3, ano)
    devidos = valores_devidos(sh, mes)

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
        valor = devidos.get(atleta.upper(), float(MENSALIDADE))
        if valor <= 0:
            puladas += 1
            print(f"  = sem cobrança (crédito/quitado): {atleta}")
            continue
        if dry:
            print(f"  + (dry) {atleta:<12} R$ {valor:.2f}  txid={txid}")
            criadas += 1
            continue
        body = {
            "calendario": {"expiracao": 60 * 60 * 24 * 40},  # 40 dias
            "valor": {"original": f"{valor:.2f}"},
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
            novas_linhas.append([mes, atleta, txid, f"{valor:.2f}",
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
