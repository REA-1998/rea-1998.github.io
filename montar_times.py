# -*- coding: utf-8 -*-
"""
Montagem de times do Racha REA — determinística, pelas regras do Mateus (rev. 21/08/2026).

REGRAS (rev. 21/08/2026 v3):
 1) Time = 6 titulares de linha + 1 goleiro + reservas. Time com 6 na linha e SEM goleiro
    é válido (linha reveza no gol).
 2) QUEM DECIDE o nº de times é o Mateus, no comando: "2times", "3times" ou "4times".
    Sem número o bot pergunta.
 3) Titulares de linha = os 6 de MAIOR NÍVEL do time (desempate: pontos de fiel).
 4) EQUILÍBRIO: soma dos níveis dos TITULARES DE LINHA o mais igual possível (prioridade
    máxima) E soma dos níveis dos RESERVAS também equilibrada (2º critério).
 5) GOLEIROS entram DEPOIS, como compensação: o melhor goleiro (Alexandre) vai pro time
    com MENOR soma de linha titular; empates são SORTEADOS.
 6) Egnaldo: o Mateus decide no comando ("egnaldo linha" ou "egnaldo gol"). Sem escolha,
    automático: ordem de gol por nível (Alexandre > Iroshi > Egnaldo); goleiro que sobrar
    joga na linha como ZAGUEIRO nível 4; goleiro que faltar, o próximo assume.
 7) POSIÇÕES por cota: cada posição repartida o mais igual possível (dif. máx. 1 por
    posição entre times), de trás pra frente (ZAG→VOL→MEI→ATA); time com menos defensores
    tem preferência pelo próximo volante etc. Total de linha: dif. máx. 1.
 8) NÃO determinístico: empates e escolhas equivalentes são SORTEADOS — cada rodada do
    comando pode variar os times (mantendo o equilíbrio).
 9) NÍVEIS: aparecem SÓ na mensagem do Telegram pro Mateus (formatar_telegram, com as
    somatórias de titulares e reservas). A versão pública NUNCA mostra nível.
"""
import random
import unicodedata
import itertools

CORES = ["AZUL", "AMARELO", "ROSA", "VASCO"]
POS_ORDEM = ["GOLEIRO", "ZAGUEIRO", "VOLANTE", "MEIA", "ATACANTE"]
POS_ABREV = {"GOLEIRO": "GOL", "ZAGUEIRO": "ZAG", "VOLANTE": "VOL", "MEIA": "MEI", "ATACANTE": "ATA"}
EMOJI = {"AZUL": "🔵", "AMARELO": "🟡", "ROSA": "🩷", "VASCO": "⚫"}
TITULARES_LINHA = 6
EGNALDO_LINHA = ("ZAGUEIRO", 4)


def _chave(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return "".join(c for c in s.upper() if c.isalnum())


def _cadastro(T):
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
    try:
        import gerar_painel_sheets as S
        rk = S.dados_ranking(T)
        return {str(x["nome"]).upper(): (int(x["pontos"]), bool(x["fiel"])) for x in rk["fiel_tab"]}
    except Exception:
        return {}


def decidir_n_times(n_conf, forcar=None):
    if forcar:
        return max(2, min(4, int(forcar)))
    return 3 if n_conf >= 24 else 2


def _titulares(t):
    """Marca titulares (6 de linha de maior nível) e calcula somas de titulares e reservas."""
    linha = [a for a in t["atletas"] if not a["em_gol"]]
    linha.sort(key=lambda a: (-a["nivel"], -a["fiel_pts"], a["nome"]))
    for i, a in enumerate(linha):
        a["titular"] = i < TITULARES_LINHA
    for a in t["atletas"]:
        if a["em_gol"]:
            a["titular"] = True
    t["soma_linha_titular"] = sum(a["nivel"] for a in linha if a["titular"])
    t["soma_reservas"] = sum(a["nivel"] for a in linha if not a["titular"])
    t["soma_linha_total"] = sum(a["nivel"] for a in linha)
    t["n_linha"] = len(linha)


def _objetivo(times):
    lt = [t["soma_linha_titular"] for t in times]
    rs = [t["soma_reservas"] for t in times]
    return (max(lt) - min(lt)) * 100 + (max(rs) - min(rs)) * 10


def _ordenar_exibicao(t):
    t["atletas"].sort(key=lambda a: (0 if a["em_gol"] else 1, 0 if a["titular"] else 1,
                                     POS_ORDEM.index(a["pos"]) if a["pos"] in POS_ORDEM else 9,
                                     -a["nivel"], a["nome"]))


def montar(confirmados, T, n_times=None, egnaldo=None):
    """egnaldo: None (automático), 'linha' ou 'gol' — decisão do Mateus no comando."""
    rnd = random.Random()  # sem semente fixa: cada rodada pode variar (regra 8)
    cad = _cadastro(T)
    fiel = _fiel_pontos(T)
    avisos, nao_reconhecidos, jogadores = [], [], []
    vistos = set()
    for nome in confirmados:
        k_ = _chave(nome)
        if k_ in vistos:
            continue
        vistos.add(k_)
        c = cad.get(k_)
        if not c:
            nao_reconhecidos.append(str(nome))
            continue
        fp, ef = fiel.get(c["nome"], (0, False))
        jogadores.append({"nome": c["nome"], "pos": c["pos"], "nivel": c["nivel"],
                          "goleiro": c["goleiro"], "fiel_pts": fp, "fiel": ef,
                          "em_gol": False, "titular": False})

    n = len(jogadores)
    k = decidir_n_times(n, n_times)

    # ---- goleiros ----
    gks_all = sorted([j for j in jogadores if j["goleiro"]], key=lambda j: (-j["nivel"], j["nome"]))
    egn = next((j for j in gks_all if _chave(j["nome"]) == "EGNALDO"), None)
    if egnaldo == "linha" and egn:
        gks_all.remove(egn)
        egn["pos"], egn["nivel"] = EGNALDO_LINHA
        avisos.append("Egnaldo na LINHA (zagueiro) — sua escolha.")
    if egnaldo == "gol" and egn and egn in gks_all:
        sel = [egn] + [g for g in gks_all if g is not egn][:max(0, k - 1)]
        avisos.append("Egnaldo no GOL — sua escolha.")
    else:
        sel = gks_all[:k]
    for j in gks_all:
        if j not in sel:
            j["pos"], j["nivel"] = EGNALDO_LINHA
            avisos.append(f"{j['nome'].title()} joga na linha (zagueiro) — sobrou goleiro.")
    gks = sel
    for j in gks:
        j["em_gol"] = True
    linha = [j for j in jogadores if not j["em_gol"]]

    times = [{"cor": CORES[i], "atletas": [], "goleiro": None} for i in range(k)]

    # ---- linha: cotas por posição (dif. máx. 1) e de trás pra frente ----
    def cont_pos(t, pos):
        return sum(1 for a in t["atletas"] if not a["em_gol"] and a["pos"] == pos)
    def cont_acum(t, poss):
        return sum(1 for a in t["atletas"] if not a["em_gol"] and a["pos"] in poss)
    def n_linha(t):
        return sum(1 for a in t["atletas"] if not a["em_gol"])
    def soma_linha(t):
        return sum(a["nivel"] for a in t["atletas"] if not a["em_gol"])

    acumulado = []
    conhecidos = {"ZAGUEIRO", "VOLANTE", "MEIA", "ATACANTE"}
    for pos in ["ZAGUEIRO", "VOLANTE", "MEIA", "ATACANTE"]:
        acumulado.append(pos)
        grupo = [j for j in linha if j["pos"] == pos]
        if pos == "MEIA":
            grupo += [j for j in linha if j["pos"] not in conhecidos]
        rnd.shuffle(grupo)                      # variação entre jogadores de mesmo nível
        grupo.sort(key=lambda j: -j["nivel"])   # sort estável mantém o embaralhado nos empates
        for j in grupo:
            chaves = [(cont_pos(times[i], pos),         # cota da posição
                       cont_acum(times[i], acumulado),  # menos defensores primeiro
                       n_linha(times[i]),               # total linha (dif. máx. 1)
                       soma_linha(times[i])) for i in range(k)]
            menor = min(chaves)
            alvo = rnd.choice([i for i in range(k) if chaves[i] == menor])  # empate: sorteia
            times[alvo]["atletas"].append(j)

    for t in times:
        _titulares(t)

    # ---- refinamento: trocas 1x1 de MESMA posição (preserva cotas e totais) ----
    melhor = _objetivo(times)
    melhorou, passos = True, 0
    while melhorou and passos < 300:
        melhorou = False
        passos += 1
        for i, j in itertools.combinations(range(k), 2):
            cand_i = [x for x in times[i]["atletas"] if not x["em_gol"]]
            cand_j = [x for x in times[j]["atletas"] if not x["em_gol"]]
            rnd.shuffle(cand_i); rnd.shuffle(cand_j)
            for a in cand_i:
                for b in cand_j:
                    if a["pos"] != b["pos"]:
                        continue
                    times[i]["atletas"].remove(a); times[j]["atletas"].remove(b)
                    times[i]["atletas"].append(b); times[j]["atletas"].append(a)
                    _titulares(times[i]); _titulares(times[j])
                    obj = _objetivo(times)
                    if obj < melhor:
                        melhor, melhorou = obj, True
                        break
                    times[i]["atletas"].remove(b); times[j]["atletas"].remove(a)
                    times[i]["atletas"].append(a); times[j]["atletas"].append(b)
                    _titulares(times[i]); _titulares(times[j])
                if melhorou:
                    break
            if melhorou:
                break

    # ---- goleiros como compensação: melhor goleiro no time de MENOR linha titular ----
    # (empate de somas: sorteia)
    ordem_fraco = sorted(range(k), key=lambda i: (times[i]["soma_linha_titular"], rnd.random()))
    gks_por_nivel = sorted(gks, key=lambda g: -g["nivel"])
    for idx, gk in enumerate(gks_por_nivel):
        t = times[ordem_fraco[idx]]
        t["atletas"].append(gk)
        t["goleiro"] = gk["nome"]
    for i in range(len(gks), k):
        avisos.append(f"Time {times[ordem_fraco[i]]['cor'].title()} sem goleiro — linha reveza no gol.")

    for t in times:
        _titulares(t)
        _ordenar_exibicao(t)

    return {"n_confirmados": n, "n_times": k, "times": times,
            "nao_reconhecidos": nao_reconhecidos, "avisos": avisos, "objetivo": melhor}


# ---------------- formatação ----------------
def formatar_telegram(res, sabado=""):
    """SÓ PRO MATEUS: mostra o NÍVEL de cada jogador e a soma da linha titular."""
    L = [f"👥 Times — sábado {sabado}".strip() if sabado else "👥 Times",
         f"{res['n_confirmados']} confirmados → {res['n_times']} times",
         "🔒 versão com níveis — NÃO encaminhar (após publicar te mando a versão limpa)"]
    for t in res["times"]:
        L.append("")
        L.append(f"{EMOJI.get(t['cor'],'')} {t['cor']} — Σ titulares: {t['soma_linha_titular']}"
                 f" · Σ reservas: {t['soma_reservas']}")
        for a in [x for x in t["atletas"] if x["titular"]]:
            tag = "🧤" if a["em_gol"] else POS_ABREV.get(a["pos"], a["pos"][:3])
            star = " ⭐" if a["fiel"] else ""
            L.append(f"  {a['nivel']} · {tag} {a['nome'].title()} (fiel {a['fiel_pts']}{star})")
        reservas = [x for x in t["atletas"] if not x["titular"]]
        if reservas:
            L.append("  — reservas: " + ", ".join(
                f"{a['nivel']} {a['nome'].title()} ({POS_ABREV.get(a['pos'], a['pos'][:3])})"
                for a in reservas))
    if res["nao_reconhecidos"]:
        L.append("\n⚠️ Não reconheci no cadastro: " + ", ".join(res["nao_reconhecidos"]))
    for av in res["avisos"]:
        L.append(f"ℹ️ {av}")
    return "\n".join(L)


def formatar_publico(res, sabado=""):
    """Versão SEM NÍVEIS (grupo/WhatsApp). Nome, posição, pontos de fiel e ⭐."""
    L = [f"👥 Times — sábado {sabado}".strip() if sabado else "👥 Times",
         f"{res['n_confirmados']} confirmados → {res['n_times']} times"]
    for t in res["times"]:
        L.append("")
        L.append(f"{EMOJI.get(t['cor'],'')} {t['cor']}")
        for a in [x for x in t["atletas"] if x["titular"]]:
            tag = "🧤" if a["em_gol"] else POS_ABREV.get(a["pos"], a["pos"][:3])
            star = " ⭐" if a["fiel"] else ""
            L.append(f"  {a['fiel_pts']:>2}{star} {tag} {a['nome'].title()}")
        reservas = [x for x in t["atletas"] if not x["titular"]]
        if reservas:
            L.append("  — reservas: " + ", ".join(
                f"{a['nome'].title()} ({POS_ABREV.get(a['pos'], a['pos'][:3])}, fiel {a['fiel_pts']}{' ⭐' if a['fiel'] else ''})"
                for a in reservas))
    if res["nao_reconhecidos"]:
        L.append("\n⚠️ Não reconheci no cadastro: " + ", ".join(res["nao_reconhecidos"]))
    for av in res["avisos"]:
        L.append(f"ℹ️ {av}")
    L.append("\n(número na frente = pontos de atleta fiel nos últimos 8 sábados; ⭐ = atleta fiel)")
    return "\n".join(L)


formatar = formatar_publico  # compatibilidade


def para_linhas(res, sabado):
    """Linhas para a aba 'Times' do Sheets (SEM nível)."""
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
    normalizado = {str(k).strip().upper(): v for k, v in (por_cor or {}).items() if v}
    ordem = [c for c in CORES if c in normalizado] + [c for c in normalizado if c not in CORES]
    # se o nº de times bate com o padrão, força as cores oficiais na ordem (evita "VERDE" indevido)
    if len(ordem) <= len(CORES):
        mapa = dict(zip(ordem, CORES))
    else:
        mapa = {c: c for c in ordem}
    for cor_orig in ordem:
        nomes = normalizado[cor_orig]
        t = {"cor": mapa[cor_orig], "atletas": [], "goleiro": None}
        for nome in nomes:
            c = cad.get(_chave(nome))
            if not c:
                nao.append(str(nome)); continue
            fp, ef = fiel.get(c["nome"], (0, False))
            t["atletas"].append({"nome": c["nome"], "pos": c["pos"], "nivel": c["nivel"],
                                 "goleiro": c["goleiro"], "fiel_pts": fp, "fiel": ef,
                                 "em_gol": False, "titular": False})
        times.append(t)
    for t in times:
        gks = sorted([a for a in t["atletas"] if a["goleiro"]], key=lambda a: (-a["nivel"], a["nome"]))
        if gks:
            gks[0]["em_gol"] = True
            t["goleiro"] = gks[0]["nome"]
            for g in gks[1:]:
                g["pos"], g["nivel"] = EGNALDO_LINHA
        _titulares(t)
        _ordenar_exibicao(t)
    n = sum(len(t["atletas"]) for t in times)
    return {"n_confirmados": n, "n_times": len(times), "times": times,
            "nao_reconhecidos": nao, "avisos": [], "objetivo": _objetivo(times) if times else 0}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    import gerar_painel_sheets as S
    T = S.carregar()
    nomes = sys.argv[1:] or ["Alexandre", "Egnaldo", "Francês", "Marlon", "Mateus", "Pagode", "Wesley", "Zé"]
    r = montar(nomes, T)
    print(formatar_telegram(r))
