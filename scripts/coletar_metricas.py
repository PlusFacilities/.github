#!/usr/bin/env python3
"""Atualiza o painel de produtividade no README do perfil da organização.

Soma a atividade de TODOS os repositórios da organização (inclusive privados),
mas publica apenas números agregados: nenhum nome de repositório, pessoa,
pull request ou issue aparece no resultado.

Variáveis de ambiente:
  GH_TOKEN  token com leitura de Contents, Pull requests, Issues e Metadata (obrigatório)
  GH_ORG    organização a consultar (padrão: PlusFacilities)
  TZ        fuso para agrupar os dias (padrão: America/Sao_Paulo)
  DIAGNOSTICO  "true" imprime no log contagens por repositório (sem nomes)
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ORG = os.environ.get("GH_ORG", "PlusFacilities")
TOKEN = os.environ.get("GH_TOKEN", "")
DIAGNOSTICO = os.environ.get("DIAGNOSTICO", "false").lower() == "true"
TZ = ZoneInfo(os.environ.get("TZ", "America/Sao_Paulo"))
DIAS_UTEIS, SEMANAS = 10, 12
RAIZ = Path(os.environ.get("RAIZ", Path(__file__).resolve().parent.parent))
README = RAIZ / "profile" / "README.md"
ASSETS = RAIZ / "profile" / "assets"
CAMPOS = ("commits", "prs_abertos", "prs_mesclados", "revisoes", "issues_abertas", "issues_fechadas")
ROTULOS = {
    "commits": "Commits",
    "prs_abertos": "PRs abertos",
    "prs_mesclados": "PRs mesclados",
    "revisoes": "Revisões",
    "issues_abertas": "Issues abertas",
    "issues_fechadas": "Issues fechadas",
}
DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

Q_REPOS = """
query($org: String!, $cursor: String) {
  organization(login: $org) {
    repositories(first: 50, after: $cursor, orderBy: {field: PUSHED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { id pushedAt isArchived isPrivate primaryLanguage { name } }
    }
  }
}
"""

Q_COMMITS = """
query($id: ID!, $since: GitTimestamp!, $cursor: String) {
  node(id: $id) {
    ... on Repository {
      defaultBranchRef {
        target {
          ... on Commit {
            history(first: 100, since: $since, after: $cursor) {
              pageInfo { hasNextPage endCursor }
              nodes { committedDate }
            }
          }
        }
      }
    }
  }
}
"""

Q_BUSCA = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest { createdAt mergedAt reviews(first: 50) { nodes { submittedAt } } }
      ... on Issue { createdAt closedAt }
    }
  }
}
"""


def graphql(query, variaveis):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variaveis}).encode(),
        headers={
            "Authorization": f"bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "org-profile-stats",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resposta:
            dados = json.load(resposta)
    except urllib.error.HTTPError as erro:
        sys.exit(f"A API do GitHub respondeu {erro.code}: {erro.read().decode()[:300]}\n"
                 "Confira se o token é válido, não expirou e tem acesso à organização.")
    if dados.get("errors"):
        sys.exit(f"Erro na consulta GraphQL: {dados['errors']}")
    return dados["data"]


def paginar(query, variaveis, caminho):
    """Percorre todas as páginas; 'caminho' leva até o objeto com pageInfo e nodes."""
    cursor = None
    while True:
        dados = graphql(query, {**variaveis, "cursor": cursor})
        for chave in caminho:
            if dados is None:
                return  # repositório vazio, sem branch padrão
            dados = dados[chave]
        if dados is None:
            return
        yield from dados["nodes"]
        if not dados["pageInfo"]["hasNextPage"]:
            return
        cursor = dados["pageInfo"]["endCursor"]


def dia_local(texto):
    momento = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    return momento.astimezone(TZ).date()


def coletar(primeiro_dia):
    """Devolve (contagem por dia, commits por repositório e dia, linguagem por repositório)."""
    desde_utc = datetime.combine(primeiro_dia, time.min, tzinfo=TZ).astimezone(timezone.utc)
    desde_txt = desde_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    por_dia = defaultdict(lambda: dict.fromkeys(CAMPOS, 0))
    commits_repo = defaultdict(lambda: defaultdict(int))  # id interno -> dia -> commits
    linguagem = {}

    repos = []
    for repo in paginar(Q_REPOS, {"org": ORG}, ("organization", "repositories")):
        if repo["pushedAt"] and repo["pushedAt"] < desde_txt:
            break  # lista vem ordenada por push mais recente
        if not repo["isArchived"]:
            repos.append(repo)

    for repo in repos:
        linguagem[repo["id"]] = (repo["primaryLanguage"] or {}).get("name", "Outras")
        for no in paginar(Q_COMMITS, {"id": repo["id"], "since": desde_txt},
                          ("node", "defaultBranchRef", "target", "history")):
            d = dia_local(no["committedDate"])
            por_dia[d]["commits"] += 1
            commits_repo[repo["id"]][d] += 1

    def conta(texto, campo):
        if texto:
            d = dia_local(texto)
            if d >= primeiro_dia:
                por_dia[d][campo] += 1

    data_busca = primeiro_dia.isoformat()
    for no in paginar(Q_BUSCA, {"q": f"org:{ORG} is:pr updated:>={data_busca}"}, ("search",)):
        conta(no.get("createdAt"), "prs_abertos")
        conta(no.get("mergedAt"), "prs_mesclados")
        for rev in (no.get("reviews") or {}).get("nodes", []):
            conta(rev.get("submittedAt"), "revisoes")
    for no in paginar(Q_BUSCA, {"q": f"org:{ORG} is:issue updated:>={data_busca}"}, ("search",)):
        conta(no.get("createdAt"), "issues_abertas")
        conta(no.get("closedAt"), "issues_fechadas")
    privados = sum(1 for r in repos if r["isPrivate"])
    print(f"Repositórios analisados: {len(repos)} ({privados} privados).")
    if repos and privados == 0:
        print("AVISO: nenhum repositório privado visível; o token pode estar limitado a repositórios públicos.")
    if DIAGNOSTICO:
        for i, repo in enumerate(repos, 1):
            dias_repo = commits_repo.get(repo["id"], {})
            ultimo = max(dias_repo) if dias_repo else "nenhum"
            print(f"  repositório {i}: {sum(dias_repo.values())} commits no período; último em {ultimo}")
    return por_dia, commits_repo, linguagem


def total(registro):
    return sum(registro[c] for c in CAMPOS)


def somar(dias, por_dia):
    soma = dict.fromkeys(CAMPOS, 0)
    for d in dias:
        for c in CAMPOS:
            soma[c] += por_dia[d][c] if d in por_dia else 0
    return soma


def variacao(atual, anterior):
    if anterior == 0:
        return "—" if atual == 0 else "novo"
    return f"{(atual - anterior) / anterior * 100:+.0f}%"


def tabela(cabecalho, linhas):
    saida = ["| " + " | ".join(cabecalho) + " |", "|" + "|".join(["---"] * len(cabecalho)) + "|"]
    saida += ["| " + " | ".join(str(c) for c in linha) + " |" for linha in linhas]
    return "\n".join(saida)


def svg_barras(rotulos, valores, titulo):
    largura, altura, margem, topo, base = 760, 240, 40, 40, 204
    maximo = max(valores) or 1
    passo = (largura - 2 * margem) / len(valores)
    partes = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{largura}" height="{altura}" '
        f'viewBox="0 0 {largura} {altura}" role="img" aria-label="{titulo}">',
        "<style>.t{font:600 14px sans-serif;fill:#24292f}.r{font:11px sans-serif;fill:#57606a}"
        ".b{fill:#1f6feb}.l{stroke:#d0d7de}"
        "@media(prefers-color-scheme:dark){.t{fill:#e6edf3}.r{fill:#8b949e}.b{fill:#58a6ff}"
        ".l{stroke:#30363d}}</style>",
        f'<text class="t" x="{margem}" y="24">{titulo}</text>',
        f'<line class="l" x1="{margem}" y1="{base}" x2="{largura - margem}" y2="{base}"/>',
    ]
    for i, (rotulo, valor) in enumerate(zip(rotulos, valores)):
        h = (base - topo) * valor / maximo
        w = passo * 0.7
        x = margem + i * passo + passo * 0.15
        meio = x + w / 2
        partes.append(f'<rect class="b" x="{x:.1f}" y="{base - h:.1f}" width="{w:.1f}" height="{h:.1f}" rx="3"/>')
        partes.append(f'<text class="r" x="{meio:.1f}" y="{base - h - 5:.1f}" text-anchor="middle">{valor}</text>')
        partes.append(f'<text class="r" x="{meio:.1f}" y="{base + 18}" text-anchor="middle">{rotulo}</text>')
    partes.append("</svg>")
    return "\n".join(partes)


def trocar(texto, nome, conteudo, em_linha=False):
    padrao = re.compile(rf"(<!--{nome}:INICIO-->)(.*?)(<!--{nome}:FIM-->)", re.S)
    if not padrao.search(texto):
        sys.exit(f"Marcador {nome} não encontrado em profile/README.md. "
                 "Cole no README o bloco do painel, com todos os marcadores.")
    modelo = (lambda m: m.group(1) + conteudo + m.group(3)) if em_linha else (
        lambda m: f"{m.group(1)}\n{conteudo}\n{m.group(3)}")
    return padrao.sub(modelo, texto)


def main():
    if not TOKEN:
        sys.exit("Defina GH_TOKEN.")
    agora = datetime.now(TZ)
    hoje = agora.date()
    segunda = hoje - timedelta(days=hoje.weekday())
    semanas = [segunda - timedelta(weeks=i) for i in range(SEMANAS - 1, -1, -1)]
    dias, d = [], hoje  # últimos dias úteis (segunda a sexta), do mais antigo ao mais recente
    while len(dias) < DIAS_UTEIS:
        if d.weekday() < 5:
            dias.append(d)
        d -= timedelta(days=1)
    dias.reverse()

    por_dia, commits_repo, linguagem = coletar(semanas[0])
    if DIAGNOSTICO:
        print("Commits por dia: " + ", ".join(
            f"{d:%d/%m}={por_dia[d]['commits'] if d in por_dia else 0}" for d in dias))

    def dias_da_semana(seg):
        return [seg + timedelta(days=i) for i in range(7)]

    def repos_ativos(lista_dias):
        return sum(1 for r in commits_repo.values() if any(d in r for d in lista_dias))

    # Resumo: esta semana (segunda até hoje) contra a semana anterior
    atual = somar(dias_da_semana(semanas[-1]), por_dia)
    anterior = somar(dias_da_semana(semanas[-2]), por_dia)
    linhas = [[ROTULOS[c], atual[c], anterior[c], variacao(atual[c], anterior[c])] for c in CAMPOS]
    ta, tp = sum(atual.values()), sum(anterior.values())
    linhas.append(["**Total de atividades**", ta, tp, variacao(ta, tp)])
    ra, rp = repos_ativos(dias_da_semana(semanas[-1])), repos_ativos(dias_da_semana(semanas[-2]))
    linhas.append(["Repositórios com commits", ra, rp, variacao(ra, rp)])
    resumo = tabela(["Métrica", "Esta semana", "Semana passada", "Variação"], linhas)

    # Diário (mais recente primeiro na tabela)
    vazio = dict.fromkeys(CAMPOS, 0)
    cab = ["Dia", *[ROTULOS[c] for c in CAMPOS], "Total"]
    linhas_dia = []
    for d in reversed(dias):
        r = por_dia.get(d, vazio)
        linhas_dia.append([f"{DIAS_SEMANA[d.weekday()]} {d:%d/%m}", *[r[c] for c in CAMPOS], total(r)])
    svg_dia = svg_barras([f"{d:%d/%m}" for d in dias],
                         [total(por_dia.get(d, vazio)) for d in dias],
                         f"Atividades por dia (últimos {DIAS_UTEIS} dias úteis)")

    # Semanal
    soma_semana = [somar(dias_da_semana(s), por_dia) for s in semanas]
    linhas_sem = [[f"Semana de {s:%d/%m}", *[r[c] for c in CAMPOS], sum(r.values())]
                  for s, r in reversed(list(zip(semanas, soma_semana)))]
    svg_sem = svg_barras([f"{s:%d/%m}" for s in semanas],
                         [sum(r.values()) for r in soma_semana],
                         f"Atividades por semana (últimas {SEMANAS} semanas)")

    # Linguagens por participação nos commits (só nomes de linguagem, nunca de repositório)
    por_ling = defaultdict(int)
    for repo_id, dias_repo in commits_repo.items():
        por_ling[linguagem[repo_id]] += sum(dias_repo.values())
    soma_ling = sum(por_ling.values())
    if soma_ling:
        top = sorted(por_ling.items(), key=lambda kv: kv[1], reverse=True)[:5]
        tabela_ling = tabela(["Linguagem", f"Participação nos commits ({SEMANAS} semanas)"],
                             [[nome, f"{n / soma_ling * 100:.0f}%"] for nome, n in top])
    else:
        tabela_ling = "_Sem commits no período._"

    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "diario.svg").write_text(svg_dia, encoding="utf-8")
    (ASSETS / "semanal.svg").write_text(svg_sem, encoding="utf-8")

    texto = README.read_text(encoding="utf-8")
    texto = trocar(texto, "ATUALIZADO", f"{agora:%d/%m/%Y}", em_linha=True)
    texto = trocar(texto, "SEMANA", resumo)
    texto = trocar(texto, "DIARIO", tabela(cab, linhas_dia))
    texto = trocar(texto, "SEMANAL", tabela(["Semana", *[ROTULOS[c] for c in CAMPOS], "Total"], linhas_sem))
    texto = trocar(texto, "LINGUAGENS", tabela_ling)
    README.write_text(texto, encoding="utf-8")
    print("README e gráficos atualizados.")


if __name__ == "__main__":
    main()
