# -*- coding: utf-8 -*-
"""
Montagem de times do Racha REA — determinística, pelas regras do Mateus.

Regras implementadas:
 a) time = 6 de linha + 1 goleiro + reservas
 b) mínimo 2 times, máximo 3 (4 só em torneio, via parâmetro)
 c) 3 times só se cada time ficar com pelo menos 1 reserva (>= 24 confirmados)
 d) Egnaldo vira linha (ZAGUEIRO, nível 4) quando sobra goleiro (mais goleiros que times)
 e) soma dos níveis dos TITULARES o mais próxima possível entre os times (prioridade),
    soma total em segundo lugar
 f) distribuição por posição de trás pra frente (ZAG -> VOL -> MEI -> ATA): quem tem menos
    defensores recebe o próximo defensor; faltando zagueiro pega volante, e assim por diante
 g) (ver d) Egnaldo na linha = zagueiro nível 4
 i) determinístico: mesma lista => mesmos times

Titulares de cada time = goleiro + os 6 de linha com MAIS PONTOS DE ATLETA FIEL
(regra do REA: quem tem mais pontos tem preferência para começar jogando);
empate -> maior nível -> ordem alfabética.

Uso pelo bot:  montar(confirmados, T, n_times=None)  -> dict
"""
import unicodedata
import itertools

CORES = ["AZUL", "AMARELO", "ROSA", "VERDE"]
POS_ORDEM = ["GOLEIRO", "ZAGUEIRO", "VOLANTE", "MEIA", "ATACANTE"]
POS_ABREV = {"GOLEIRO": "GOL", "ZAGUEIRO": "ZAG", "VOLANTE": "VOL", "MEIA": "MEI", "ATACANTE": "ATA"}
TITULARES_LINHA = 6
EGNALDO_LINHA = ("ZAGUEIRO", 4)


def _chave(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return "".join(c for c in s.upper() if c.isalnum())


def _cadastro(T):
    """{chave: {nome, pos, nivel, goleiro}} a partir da aba Atletas (só ativos que jogam)."""
    out = {}
    for r in T.get("Atletas", []):
        nome = str(r.get("nome", "")).strip()
        if not nome:
            continue
        if str(r.get("ativo", "")).strip().lower() != "sim":
            continue
        if str(r.get("joga", "")).strip().lower() == "não":
            continue
        pos = str(r.get("posicao", "")).strip().upper() or "MEIA"
        try:
            nivel = int(float(str(r.get("nivel", "")).replace(",", ".") or 0))
        except ValueError:
            nivel = 0
        out[_chave(nome)] = {"nome": nome.upper(), "pos": pos, "nivel": nivel,
                             "goleiro": str(r.get("goleiro", "")).strip().lower() == "sim"}
    return out


def _fiel_pontos(T):
    """{NOME: (pontos_ultimos_8, eh_fiel)} reaproveitando a regra do painel."""
    try:
        import gerar_painel_sheets as S
        rk = S.dados_ranking(T)
        return {str(x["nome"]).upper(): (int(x["pontos"]), bool(x["fiel"])) for x in rk["fiel_tab"]}
    except Exception:
        return {}


def decidir_n_times(n_conf, forcar=None):
    if forcar:
        return max(2, min(4, int(forcar)))
    # 3 times exigem 21 titulares + 1 reserva por time (regra c)
    return 3 if n_conf >= 24 else 2


def _titulares(time):
    """Marca titulares: goleiro + 6 de linha com mais pontos de fiel (desempate nível, nome)."""
    linha = [a for a in time["atletas"] if not a["em_gol"]]
    linha.sort(key=lambda a: (-a["fiel_pts"], -a["nivel"], a["nome"]))
    for i, a in enumerate(linha):
        a["titular"] = i < TITULARES_LINHA
    for a in time["atletas"]:
        if a["em_gol"]:
            a["titular"] = True
    time["soma_titular"] = sum(a["nivel"] for a in time["atletas"] if a["titular"])
    time["soma_total"] = sum(a["nivel"] for a in time["atletas"])
    time["n_linha"] = len(linha)


def _objetivo(times):
    st = [t["soma_titular"] for t in times]
    tt = [t["soma_total"] for t in times]
    return (max(st) - min(st)) * 10 + (max(tt) - min(tt))


def montar(confirmados, T, n_times=None):
    cad = _cadastro(T)
    fiel = _fiel_pontos(T)
    avisos, nao_reconhecidos, jogadores = [], [], []
    vistos = set()
    for nome in confirmados:
        k = _chave(nome)
        if k in vistos:
            continue
        vistos.add(k)
        c = cad.get(k)
        if not c:
            nao_reconhecidos.append(str(nome))
            continue
        fp, ef = fiel.get(c["nome"], (0, False))
        jogadores.append({"nome": c["nome"], "pos": c["pos"], "nivel": c["nivel"],
                          "goleiro": c["goleiro"], "fiel_pts": fp, "fiel": ef,
                          "em_gol": False, "titular": False})

    n = len(jogadores)
    k = decidir_n_times(n, n_times)
    if n_times and n_times == 3 and n < 24:
        avisos.append(f"Com {n} confirmados, 3 times ficam sem reserva em algum time (regra c).")

    # ---- goleiros ----
    gks = sorted([j for j in jogadores if j["goleiro"]], key=lambda j: (-j["nivel"], j["nome"]))
    if len(gks) > k:
        # sobra goleiro: Egnaldo (ou o de menor nível) vai pra linha como zagueiro nível 4
        sobra = gks[k:]
        for j in sobra:
            j["pos"], j["nivel"] = EGNALDO_LINHA
            avisos.append(f"{j['nome'].title()} joga na linha (zagueiro) — sobrou goleiro.")
        gks = gks[:k]
    for j in gks:
        j["em_gol"] = True
    linha = [j for j in jogadores if not j["em_gol"]]

    times = [{"cor": CORES[i], "atletas": [], "goleiro": None} for i in range(k)]
    # goleiros: o melhor goleiro vai pro time que (ainda) não tem — alterna cores; empatar depois via swaps
    for i, j in enumerate(gks):
        times[i]["atletas"].append(j)
        times[i]["goleiro"] = j["nome"]
    if len(gks) < k:
        for t in times[len(gks):]:
            avisos.append(f"Time {t['cor'].title()} sem goleiro fixo — linha reveza no gol.")

    # ---- linha: de trás pra frente (regra f), o time mais "carente" recebe primeiro ----
    def cont(t, poss):
        return sum(1 for a in t["atletas"] if not a["em_gol"] and a["pos"] in poss)
    def soma(t):
        return sum(a["nivel"] for a in t["atletas"])
    acumulado = []
    for pos in ["ZAGUEIRO", "VOLANTE", "MEIA", "ATACANTE"]:
        acumulado.append(pos)
        grupo = sorted([j for j in linha if j["pos"] == pos], key=lambda j: (-j["nivel"], j["nome"]))
        outros = [j for j in linha if j["pos"] not in POS_ORDEM]  # posição desconhecida -> trata como meia
        if pos == "MEIA":
            grupo += sorted(outros, key=lambda j: (-j["nivel"], j["nome"]))
        for j in grupo:
            # chave: menos jogadores "de trás até esta posição", depois menor soma, depois índice
            alvo = min(range(k), key=lambda i: (cont(times[i], acumulado), len(times[i]["atletas"]),
                                               soma(times[i]), i))
            times[alvo]["atletas"].append(j)

    # tamanhos: garante diferença máxima de 1 entre times (move o de menor nível se preciso)
    def tamanhos_ok():
        tam = [len(t["atletas"]) for t in times]
        return max(tam) - min(tam) <= 1
    guard = 0
    while not tamanhos_ok() and guard < 50:
        guard += 1
        maior = max(range(k), key=lambda i: (len(times[i]["atletas"]), i))
        menor = min(range(k), key=lambda i: (len(times[i]["atletas"]), i))
        cand = sorted([a for a in times[maior]["atletas"] if not a["em_gol"]], key=lambda a: (a["nivel"], a["nome"]))
        if not cand:
            break
        times[maior]["atletas"].remove(cand[0])
        times[menor]["atletas"].append(cand[0])

    for t in times:
        _titulares(t)

    # ---- refinamento: trocas 1x1 de mesma posição que reduzem o desequilíbrio (determinístico) ----
    melhor = _objetivo(times)
    melhorou = True
    passos = 0
    while melhorou and passos < 200:
        melhorou = False
        passos += 1
        for i, j in itertools.combinations(range(k), 2):
            for a in sorted([x for x in times[i]["atletas"] if not x["em_gol"]], key=lambda x: x["nome"]):
                for b in sorted([x for x in times[j]["atletas"] if not x["em_gol"]], key=lambda x: x["nome"]):
                    if a["pos"] != b["pos"]:
                        continue
                    times[i]["atletas"].remove(a); times[j]["atletas"].remove(b)
                    times[i]["atletas"].append(b); times[j]["atletas"].append(a)
                    _titulares(times[i]); _titulares(times[j])
                    obj = _objetivo(times)
                    if obj < melhor:
                        melhor = obj
                        melhorou = True
                        break
                    # desfaz
                    times[i]["atletas"].remove(b); times[j]["atletas"].remove(a)
                    times[i]["atletas"].append(a); times[j]["atletas"].append(b)
                    _titulares(times[i]); _titulares(times[j])
                if melhorou:
                    break
            if melhorou:
                break

    # ordem de exibição dentro do time: goleiro, titulares por posição, depois reservas
    for t in times:
        t["atletas"].sort(key=lambda a: (0 if a["em_gol"] else 1, 0 if a["titular"] else 1,
                                         POS_ORDEM.index(a["pos"]) if a["pos"] in POS_ORDEM else 9,
                                         -a["fiel_pts"], a["nome"]))

    return {"n_confirmados": n, "n_times": k, "times": times,
            "nao_reconhecidos": nao_reconhecidos, "avisos": avisos,
            "objetivo": melhor}


EMOJI = {"AZUL": "🔵", "AMARELO": "🟡", "ROSA": "🩷", "VERDE": "🟢"}


def formatar(res, sabado=""):
    """Texto COMPARTILHÁVEL (Telegram/WhatsApp): SEM níveis, SEM somas.
    Mostra nome, posição, pontos de atleta fiel e ⭐ se for fiel."""
    L = [f"👥 Times — sábado {sabado}".strip() if sabado else "👥 Times",
         f"{res['n_confirmados']} confirmados → {res['n_times']} times"]
    for t in res["times"]:
        L.append("")
        L.append(f"{EMOJI.get(t['cor'],'')} {t['cor']}")
        tit = [a for a in t["atletas"] if a["titular"]]
        res_ = [a for a in t["atletas"] if not a["titular"]]
        for a in tit:
            tag = "🧤" if a["em_gol"] else POS_ABREV.get(a["pos"], a["pos"][:3])
            star = " ⭐" if a["fiel"] else ""
            L.append(f"  {a['fiel_pts']:>2}{star} {tag} {a['nome'].title()}")
        if res_:
            L.append("  — reservas: " + ", ".join(
                f"{a['nome'].title()} ({POS_ABREV.get(a['pos'], a['pos'][:3])}, fiel {a['fiel_pts']}{' ⭐' if a['fiel'] else ''})"
                for a in res_))
    if res["nao_reconhecidos"]:
        L.append("\n⚠️ Não reconheci no cadastro: " + ", ".join(res["nao_reconhecidos"]))
    for av in res["avisos"]:
        L.append(f"ℹ️ {av}")
    L.append("\n(número na frente = pontos de atleta fiel; ⭐ = atleta fiel — quem tem mais pontos começa jogando)")
    return "\n".join(L)


def formatar_privado(res):
    """Linha de equilíbrio SÓ PRA VOCÊ (níveis): não encaminhar."""
    partes = [f"{EMOJI.get(t['cor'],'')} {t['cor'].title()}: titulares {t['soma_titular']} · total {t['soma_total']}"
              for t in res["times"]]
    return "🔒 Só pra você (não encaminhar) — equilíbrio por nível:\n" + "\n".join(partes)


def para_linhas(res, sabado):
    """Linhas para a aba 'Times' do Sheets (sem nível)."""
    rows = []
    for t in res["times"]:
        for i, a in enumerate(t["atletas"], start=1):
            rows.append([sabado, t["cor"], a["nome"], POS_ABREV.get(a["pos"], a["pos"]),
                         "sim" if a["em_gol"] else "não", "sim" if a["titular"] else "não",
                         a["fiel_pts"], "sim" if a["fiel"] else "não", i])
    return rows


def montar_manual(por_cor, T):
    """Recalcula titulares/somas a partir de uma atribuição manual {COR: [nomes]} (ajuste por chat)."""
    cad = _cadastro(T)
    fiel = _fiel_pontos(T)
    times, nao = [], []
    for cor in CORES:
        nomes = por_cor.get(cor) or por_cor.get(cor.title()) or por_cor.get(cor.lower())
        if not nomes:
            continue
        t = {"cor": cor, "atletas": [], "goleiro": None}
        for nome in nomes:
            c = cad.get(_chave(nome))
            if not c:
                nao.append(str(nome)); continue
            fp, ef = fiel.get(c["nome"], (0, False))
            t["atletas"].append({"nome": c["nome"], "pos": c["pos"], "nivel": c["nivel"],
                                 "goleiro": c["goleiro"], "fiel_pts": fp, "fiel": ef,
                                 "em_gol": False, "titular": False})
        times.append(t)
    # goleiro do time = melhor goleiro presente; goleiro sobrando vira zagueiro nível 4
    for t in times:
        gks = sorted([a for a in t["atletas"] if a["goleiro"]], key=lambda a: (-a["nivel"], a["nome"]))
        if gks:
            gks[0]["em_gol"] = True; t["goleiro"] = gks[0]["nome"]
            for g in gks[1:]:
                g["pos"], g["nivel"] = EGNALDO_LINHA
        _titulares(t)
        t["atletas"].sort(key=lambda a: (0 if a["em_gol"] else 1, 0 if a["titular"] else 1,
                                         POS_ORDEM.index(a["pos"]) if a["pos"] in POS_ORDEM else 9,
                                         -a["fiel_pts"], a["nome"]))
    n = sum(len(t["atletas"]) for t in times)
    return {"n_confirmados": n, "n_times": len(times), "times": times,
            "nao_reconhecidos": nao, "avisos": [], "objetivo": _objetivo(times) if times else 0}


if __name__ == "__main__":
    import sys, json
    sys.path.insert(0, ".")
    import gerar_painel_sheets as S
    T = S.carregar()
    nomes = sys.argv[1:] or ["Alexandre", "Egnaldo", "Francês", "Marlon", "Mateus", "Pagode", "Wesley", "Zé"]
    r = montar(nomes, T)
    print(formatar(r))
