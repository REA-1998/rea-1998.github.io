# -*- coding: utf-8 -*-
"""
Conta-corrente do atleta no mês — fonte ÚNICA da matemática do racha.

Regra do Mateus: o pagamento abate PRIMEIRO o boteco, depois a dívida antiga (s_a),
depois mensalidade/multa.

Porém só pode abater o que JÁ EXISTIA quando o dinheiro entrou. Consumo lançado DEPOIS
do Pix não é coberto por aquele Pix — senão quem pagou a mensalidade em dia e bebeu
depois aparecia como "boteco pago" e "mensalidade em aberto", que é o contrário da
verdade (foi o caso do Vinícius em 17/10/2026).

Este módulo é importado por gerar_painel_sheets.py, pix_webhook.py, pix_cobranca.py e
resumo_boteco.py justamente para não existirem quatro contas diferentes.
"""
import datetime

TZ = datetime.timezone(datetime.timedelta(hours=-3))  # Brasil não tem mais horário de verão
_FMTS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d/%m")


def nf(x):
    try:
        return float(str(x).replace(",", ".") or 0)
    except (TypeError, ValueError):
        return 0.0


def ts(v, default=None):
    """Texto -> datetime ingênuo no fuso de Brasília. Aceita o ISO em UTC da Efí
    ('2026-10-17T13:23:19.000Z'), o 'YYYY-MM-DD HH:MM:SS' do bot e datas soltas."""
    s = str(v or "").strip()
    if not s:
        return default
    try:
        d = datetime.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
        return d.astimezone(TZ).replace(tzinfo=None) if d.tzinfo else d
    except ValueError:
        pass
    for f in _FMTS:
        try:
            d = datetime.datetime.strptime(s, f)
            return d.replace(year=datetime.date.today().year) if f == "%d/%m" else d
        except ValueError:
            continue
    return default


def _quando_consumo(c):
    """Momento do consumo. Sem 'registrado_em', assume FIM do dia: um Pix feito de manhã
    não paga a cerveja que o cara pegou à noite."""
    d = ts(c.get("registrado_em"))
    if d is not None:
        return d
    d = ts(c.get("data"))
    return d.replace(hour=23, minute=59, second=59) if d is not None else None


def consumos_por_atleta(rows, ini=None, fim=None):
    """BarConsumo -> {ATLETA: [(datetime, valor)]} ordenado."""
    out = {}
    for c in rows:
        d = _quando_consumo(c)
        if d is None or (ini and d.date() < ini) or (fim and d.date() > fim):
            continue
        out.setdefault(str(c.get("atleta", "")).strip().upper(), []).append((d, nf(c.get("total")), c))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def pagamentos_por_atleta(rows, ini=None, fim=None):
    """PixCobrancas -> {ATLETA: [(datetime, valor)]} só das cobranças CONCLUIDA."""
    out = {}
    for p in rows:
        if str(p.get("status", "")).strip().upper() != "CONCLUIDA":
            continue
        d = ts(p.get("pago_em"))
        if d is None or (ini and d.date() < ini) or (fim and d.date() > fim):
            continue
        out.setdefault(str(p.get("atleta", "")).strip().upper(), []).append((d, nf(p.get("valor"))))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


MESES3 = ("JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ")


def ordem_mes(mes):
    """'OUT 2026' -> (2026, 10). Serve para percorrer os meses na ordem certa."""
    p = str(mes or "").strip().upper().split()
    if len(p) != 2 or p[0] not in MESES3:
        return (9999, 99)
    try:
        return (int(p[1]), MESES3.index(p[0]) + 1)
    except ValueError:
        return (9999, 99)


def extrato_corrido(regs, consumos=(), pagamentos=(), desde=None):
    """Conta-corrente ATRAVESSANDO os meses — devolve o que falta pagar do boteco sem prazo.

    regs       — as linhas do atleta na aba Pagamentos, de qualquer mês (ordena aqui)
    consumos   — [(datetime, valor, linha)] do BarConsumo, do período inteiro
    pagamentos — [(datetime, valor)] dos Pix CONCLUIDA, do período inteiro
    desde      — (ano, mes) do primeiro mês a considerar; antes disso o saldo entra como
                 dívida fixa, porque o boteco ainda não existia

    Mesma regra do extrato(): o pagamento abate primeiro o boteco que JÁ EXISTIA quando o
    dinheiro entrou, depois a dívida antiga, depois mensalidade e multa. A diferença é que
    a fila do boteco não é zerada na virada do mês.

    Devolve boteco_aberto, boteco_itens, fixo_aberto e credito.
    """
    ordenados = sorted(regs, key=lambda r: ordem_mes(r.get("mes")))
    if desde:
        ordenados = [r for r in ordenados if ordem_mes(r.get("mes")) >= desde]
    if not ordenados:
        return {"boteco_aberto": 0.0, "boteco_itens": [], "fixo_aberto": 0.0, "credito": 0.0}

    if desde:
        # Pix de cobranca anterior a janela nao entra: ele quitou um mes que nao estamos
        # recalculando, e aqui viraria credito sobrando abatendo boteco que nao e dele.
        pagamentos = [p for p in pagamentos
                      if len(p) < 3 or not p[2] or ordem_mes(p[2]) >= desde]
    eventos = []
    for i, reg in enumerate(ordenados):
        ano, m = ordem_mes(reg.get("mes"))
        if ano == 9999:
            continue
        d1 = datetime.datetime(ano, m, 1)
        fixo = nf(reg.get("mensalidade")) + nf(reg.get("multa_chu"))
        if i == 0:
            # o s_a do primeiro mês é dívida de antes do boteco: entra como fixa.
            # negativo é crédito trazido, e entra como pagamento no dia 1º.
            sa = nf(reg.get("s_a"))
            if sa < 0:
                eventos.append((d1, 1, -sa))
            else:
                fixo += sa
        # nos meses seguintes o s_a é justamente o que esta função recalcula: ignorar
        if fixo > 0.009:
            eventos.append((d1, 0.5, fixo))     # 0.5: entra depois do consumo, antes do Pix

        # parte do valor_pago que não veio de Pix conciliado (dinheiro, acerto, desconto)
        fim = datetime.datetime(ano + (m == 12), (m % 12) + 1, 1)
        pix_mes = 0.0
        for p in pagamentos:
            mes_pg = p[2] if len(p) > 2 else ""
            # com o mes da cobranca, casa por ele; sem ele, cai no intervalo de datas
            dentro = (ordem_mes(mes_pg) == (ano, m)) if mes_pg else (d1 <= p[0] < fim)
            if dentro:
                pix_mes += p[1]
        resid = round(nf(reg.get("valor_pago")) - pix_mes, 2)
        if resid > 0.009:
            eventos.append((ts(reg.get("data_pgto")) or d1, 1, resid))

    eventos += [(c[0], 0, c[1], c[2] if len(c) > 2 else None) for c in consumos]
    eventos += [(p[0], 1, p[1]) for p in pagamentos]
    eventos = [(e[0], e[1], e[2], e[3] if len(e) > 3 else None) for e in eventos]
    eventos.sort(key=lambda e: (e[0], e[1]))

    fila, fixo_ab, credito = [], 0.0, 0.0
    for _, tipo, v, item in eventos:
        if tipo == 0:                              # consumo
            rest = v
            if credito > 0:
                ab = min(credito, rest)
                credito -= ab
                rest -= ab
            if rest > 0.009:
                fila.append([rest, item, v])
        elif tipo == 0.5:                          # mensalidade + multa do mês
            fixo_ab += v
        else:                                      # pagamento
            sobra = v
            for pend in fila:                      # 1º boteco
                if sobra <= 0:
                    break
                ab = min(sobra, pend[0])
                pend[0] -= ab
                sobra -= ab
            fila = [p for p in fila if p[0] > 0.009]
            ab = min(sobra, fixo_ab)               # 2º mensalidade/multa/dívida antiga
            fixo_ab -= ab
            credito += sobra - ab

    return {"boteco_aberto": round(sum(p[0] for p in fila), 2),
            "boteco_itens": _itens_em_aberto(fila),
            "fixo_aberto": round(fixo_ab, 2), "credito": round(credito, 2)}


def pagamentos_com_mes(rows):
    """Como pagamentos_por_atleta, mas guarda o mes DA COBRANCA junto: {ATLETA:
    [(datetime, valor, mes)]}. O extrato_corrido precisa dele porque a cobranca de um mes
    costuma ser paga no mes seguinte, e esse Pix nao pode entrar na janela errada."""
    out = {}
    for p in rows:
        if str(p.get("status", "")).strip().upper() != "CONCLUIDA":
            continue
        d = ts(p.get("pago_em"))
        if d is None:
            continue
        out.setdefault(str(p.get("atleta", "")).strip().upper(), []).append(
            (d, nf(p.get("valor")), str(p.get("mes", "")).strip()))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def extrato(reg, consumos=(), pagamentos=(), ini_mes=None):
    """Roda a conta-corrente do mês e devolve o que está realmente em aberto.

    reg        — a linha do atleta na aba Pagamentos (s_a, mensalidade, multa_chu,
                 valor_pago, consumo, data_pgto)
    consumos   — [(datetime, valor)] do BarConsumo do atleta no mês
    pagamentos — [(datetime, valor)] dos Pix CONCLUIDA do atleta no mês

    Devolve: sa_aberto, fixo_aberto (s_a + mensalidade + multa que falta),
    boteco_aberto, credito, total, pago, saldo.
    """
    sa = nf(reg.get("s_a"))
    outros = nf(reg.get("mensalidade")) + nf(reg.get("multa_chu"))
    # s_a negativo e credito trazido do mes anterior (quem pagou adiantado). Entra como
    # pagamento feito no dia 1o, senao ele abatia o total mas o boteco continuava "em
    # aberto" - e boteco em aberto trava presenca e o teto de consumo de quem, na verdade,
    # nao deve nada. Caso do Bruno em 27/10/2026: R$223,43 de credito combinado com ele.
    credito_anterior = 0.0
    if sa < 0:
        credito_anterior, sa = -sa, 0.0
    pago = nf(reg.get("valor_pago"))
    cons = sorted(consumos, key=lambda x: x[0])
    consumo_total = nf(reg.get("consumo")) or round(sum(c[1] for c in cons), 2)

    pgs = sorted(pagamentos, key=lambda x: x[0])
    resid = round(pago - sum(v for _, v in pgs), 2)
    if resid > 0.009:
        # entrada manual (dinheiro, acerto, desconto): usa a data_pgto; sem data legível,
        # entra no começo do mês — conservador, não cobre consumo posterior
        d = ts(reg.get("data_pgto")) or (datetime.datetime.combine(ini_mes, datetime.time())
                                         if ini_mes else datetime.datetime(1900, 1, 1))
        pgs.append((d, resid))
        pgs.sort(key=lambda x: x[0])
    elif resid < -0.009:
        # valor_pago (ajustado à mão) é menor que os Pix conciliados: manda o valor_pago
        falta = -resid
        for i in range(len(pgs) - 1, -1, -1):
            corte = min(falta, pgs[i][1])
            pgs[i] = (pgs[i][0], pgs[i][1] - corte)
            falta -= corte
            if falta <= 0.009:
                break
        pgs = [p for p in pgs if p[1] > 0.009]

    # linha do tempo: no empate, o consumo entra antes (o pagamento daquele instante o cobre)
    eventos = ([(c[0], 0, c[1], c[2] if len(c) > 2 else None) for c in cons]
               + [(p[0], 1, p[1], None) for p in pgs])
    if credito_anterior > 0.009:
        d0 = (datetime.datetime.combine(ini_mes, datetime.time())
              if ini_mes else datetime.datetime(1900, 1, 1))
        eventos.append((d0, 1, credito_anterior, None))
    eventos.sort(key=lambda e: (e[0], e[1]))

    fila, credito = [], 0.0   # fila = [restante, item, valor cheio], mais antigo primeiro
    for _, tipo, v, item in eventos:
        if tipo == 0:
            rest = v
            if credito > 0:                       # sobra de pagamento anterior cobre o novo consumo
                ab = min(credito, rest)
                credito -= ab
                rest -= ab
            if rest > 0.009:
                fila.append([rest, item, v])
        else:
            sobra = v
            for pend in fila:                     # 1º boteco
                if sobra <= 0:
                    break
                ab = min(sobra, pend[0])
                pend[0] -= ab
                sobra -= ab
            fila = [p for p in fila if p[0] > 0.009]
            ab = min(sobra, sa)                   # 2º dívida antiga
            sa -= ab
            sobra -= ab
            ab = min(sobra, outros)               # 3º mensalidade + multa
            outros -= ab
            credito += sobra - ab

    sa_ab, fixo_ab = round(sa, 2), round(sa + outros, 2)
    bot_ab = round(sum(p[0] for p in fila), 2)
    total = round(nf(reg.get("s_a")) + nf(reg.get("mensalidade")) + nf(reg.get("multa_chu"))
                  + consumo_total, 2)
    return {"sa_aberto": sa_ab, "fixo_aberto": fixo_ab, "boteco_aberto": bot_ab,
            "credito": round(credito, 2), "total": total, "pago": round(pago, 2),
            "consumo": round(consumo_total, 2), "saldo": round(fixo_ab + bot_ab, 2),
            "boteco_itens": _itens_em_aberto(fila)}


def _itens_em_aberto(fila):
    """Agrupa por produto o consumo que AINDA não foi pago - e não o mês inteiro, senão a
    lista do site mostra também o que a pessoa já acertou.

    Se um lançamento foi pago pela metade, a quantidade entra proporcional ao que falta;
    o valor em aberto continua sendo a fonte da verdade."""
    por = {}
    for rest, item, cheio in fila:
        if not item:
            continue
        nome = str(item.get("produto", "")).strip()
        frac = (rest / cheio) if cheio else 1.0
        q, v = por.get(nome, (0.0, 0.0))
        por[nome] = (q + nf(item.get("qtd")) * frac, v + rest)
    itens = []
    for nome, (q, v) in por.items():
        qtd = int(round(q))
        itens.append({"produto": nome, "qtd": max(1, qtd) if v > 0.009 else qtd,
                      "valor": round(v, 2)})
    itens.sort(key=lambda x: -x["valor"])
    return itens



def situacao(reg, ext):
    """Texto da situação no painel, a partir do extrato."""
    if ext["total"] == 0 and ext["saldo"] == 0:
        return "Isento"
    if ext["saldo"] <= 0:
        return "Em dia"
    # Atrasado = virou o mês devendo mensalidade (s_a >= 90) e ainda não quitou essa dívida.
    # Multa pequena antiga (s_a < 90) não rebaixa ninguém.
    if nf(reg.get("s_a")) >= 90 and ext["sa_aberto"] > 0:
        return "Atrasado"
    return "Mês em aberto"
