"""Modo piloto: UMA entrada única para o pipeline `wk code` de ponta a ponta.

PROBLEMA QUE ESTE MÓDULO RESOLVE
--------------------------------
`wk code auto` já é um laço *stateful* (checkpoint em `state.json`) que encadeia
todas as ações determinísticas, mas ele PARA em `fanout:<stage>` — porque o
passo seguinte não é um comando, é um fan-out de subagentes de LLM. Hoje o
humano é quem faz a ponte: lê a parada, copia o prompt de handoff, cola numa
sessão Claude Code, espera os recibos, roda `integrate`, roda `auto` de novo, e
repete a cada estágio. São ~13 passos humanos (P1–P13) para ~4 decisões humanas
REAIS (tópico, doc_level/granularity, unidades de `specs`, aprovar o `finish`).

O modo piloto move essa ponte para dentro da própria LLM despachante: ela roda
`auto`, interpreta a parada, dispara os subagentes, integra e reexecuta `auto`
em laço — e só devolve o controle ao humano em `decisao_humana` ou em falha.
O humano cola UM prompt (ou digita UM slash command) e volta a aparecer só nas
decisões-chave.

O QUE ESTE MÓDULO **NÃO** FAZ
------------------------------
Nada é executado aqui. Este módulo só *gera texto*: o prompt-mestre e o corpo
do slash command. Todo o trabalho continua sendo feito pelos subcomandos já
existentes (`auto`, `handoff`, `integrate`) e pelos gates que eles aplicam —
o piloto não tem nenhum caminho paralelo capaz de contornar um gate.

CONTRATO DE SAÍDA DO `auto` (o que o piloto parseia)
-----------------------------------------------------
`cli._auto_print` já emite saída *machine-readable* — por isso o piloto NÃO
inventa um segundo formato:

  * parada prevista (exit 0): 1ª linha do **stdout** é um JSON compacto; nos
    fan-outs, o prompt de handoff cru vem LOGO ABAIXO dessa linha.
  * parada de falha (exit 2): 1ª linha do **stderr** é o mesmo JSON.

Campos sempre presentes: `parado_em`, `motivo`, `acao`, `executados[]`,
`progresso`. O piloto lê `parado_em` e despacha a partir dele.

REGRAS DE CITAÇÃO E FALLBACK
-----------------------------
NÃO são reescritas aqui. `HANDOFF_CITACAO_REGRA` e `HANDOFF_FALLBACK_ESCRITA`
são importadas de `cli` e interpoladas literalmente: uma única fonte de verdade
para a regra que o gate de `verify`/`audit` cobra e para o fallback de escrita
negada. Divergir esse texto reintroduziria a falha que ele existe para curar.
"""

from __future__ import annotations

import os
import sys

from . import state as st_mod

# Import de `cli` em nível de módulo é seguro: `cli` importa `pilot`
# TARDIAMENTE (dentro de `cmd_pilot`), justamente para não fechar um ciclo.
# Inverter isso (cli importando pilot no topo) quebraria a importação.
from .cli import (
    AUTO_MAX_ACOES,
    HANDOFF_CITACAO_REGRA,
    HANDOFF_FALLBACK_ESCRITA,
)

# Teto de ações do LAÇO DA LLM (quantas vezes ela pode rodar `auto`/`integrate`
# antes de chamar o humano). É outro teto que o `AUTO_MAX_ACOES` (que limita as
# ações DENTRO de uma invocação do `auto`): aqui o custo de cada volta inclui um
# fan-out inteiro de subagentes, então o limite protege contra um laço de LLM
# que queime orçamento sem avançar o pipeline.
PILOT_MAX_ACOES = 40

# Quantas correções automáticas a LLM pode tentar antes de parar e chamar o
# humano. 1 = "tentou uma vez, não insiste". Alinhado com a guarda de
# `AUTO_MAX_TENTATIVAS` do `auto`, que já para em `intervencao` na repetição.
PILOT_MAX_CORRECOES = 1

_PLACEHOLDER_WORKDIR = "<store>/.codescan/<nome-do-repo>-<sha8>"


def _resolve_pyz() -> str:
    """Caminho do `wk.pyz` em execução, ou o melhor palpite determinístico.

    O prompt-mestre é colado numa OUTRA sessão (Claude Code), que não herda
    nada do processo atual: um comando relativo como `wk.pyz` só funcionaria
    por acidente de diretório corrente. Por isso resolvemos para absoluto aqui,
    na ordem do mais confiável para o menos:

      1. `sys.argv[0]` terminando em `.pyz` — o caso normal (`python wk.pyz ...`).
      2. Prefixo `.pyz` no `__file__` do módulo — o caso do zipimport, em que
         `__file__` é `C:\\...\\wk.pyz\\codescan\\pilot.py` (caminho que NÃO
         existe no disco; só o prefixo até `.pyz` existe).
      3. `wk.pyz` na raiz do repo do wiki-ai, deduzida de `scripts/codescan/`
         — o caso do checkout de desenvolvimento, rodando via `python -m`.
      4. Literal `wk.pyz`, como último recurso (o prompt segue legível e o
         humano corrige o caminho numa linha).
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0.lower().endswith(".pyz") and os.path.isfile(argv0):
        return os.path.abspath(argv0)

    here = os.path.abspath(__file__)
    parts = here.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if part.lower().endswith(".pyz"):
            candidate = os.path.normpath("/".join(parts[: i + 1]))
            if os.path.isfile(candidate):
                return candidate

    # scripts/codescan/pilot.py -> raiz do repo do wiki-ai
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    candidate = os.path.join(root, "wk.pyz")
    if os.path.isfile(candidate):
        return candidate
    return "wk.pyz"


def _resolve_workdir(store: str, repo: str) -> str:
    """Workdir do codescan, sem tocar no disco. Falha vira placeholder legível."""
    try:
        return st_mod.workdir(store, repo)
    except Exception:  # noqa: BLE001 - texto de prompt nunca deve derrubar a CLI
        return _PLACEHOLDER_WORKDIR


def _q(value: str) -> str:
    """Argumento pronto para colar num shell: sempre entre aspas duplas.

    Caminhos do Windows trazem espaços com frequência (`C:\\Users\\...`); um
    comando sem aspas no prompt seria quebrado pela LLM em dois argumentos.
    """
    return '"' + str(value).replace('"', r"\"") + '"'


def _auto_flags(topic: str | None, doc_level: str | None,
                granularity: str | None, specs_items: str | None) -> str:
    """Flags de decisão já conhecidas, anexadas ao comando AUTO do prompt.

    Cada flag presente aqui é uma parada de `decisao_humana` que o piloto NÃO
    vai bater: o `auto` grava a decisão sozinho e segue (ver `_auto_plan`).
    """
    parts = []
    if topic:
        parts.append(f"--topic {_q(topic)}")
    if doc_level:
        parts.append(f"--doc-level {_q(doc_level)}")
    if granularity:
        parts.append(f"--granularity {_q(granularity)}")
    if specs_items:
        parts.append(f"--specs-items {_q(specs_items)}")
    return (" " + " ".join(parts)) if parts else ""


def pilot_prompt(
    *,
    python: str | None = None,
    pyz: str | None = None,
    store: str,
    repo: str,
    workdir: str | None = None,
    topic: str | None = None,
    doc_level: str | None = None,
    granularity: str | None = None,
    specs_items: str | None = None,
    max_acoes: int = PILOT_MAX_ACOES,
) -> str:
    """Prompt-mestre do PROTOCOLO PILOTO, com caminhos REAIS já resolvidos.

    É o único texto que o humano entrega à LLM despachante. Estruturado
    (tabelas/listas, zero prosa) porque ele é lido por uma LLM que precisa
    despachar, não interpretar.

    `python`/`pyz`/`workdir` em `None` são resolvidos automaticamente; passe
    valores explícitos para gerar o texto com placeholders (é o que
    `WK_FLOW_COMMAND_TEMPLATE` faz).
    """
    python = python or sys.executable or "python"
    pyz = pyz if pyz is not None else _resolve_pyz()
    workdir = workdir if workdir is not None else _resolve_workdir(store, repo)

    base = f"{_q(python)} {_q(pyz)} code"
    alvo = f"--store {_q(store)} --repo {_q(repo)}"
    cmd_auto = f"{base} auto {alvo}{_auto_flags(topic, doc_level, granularity, specs_items)}"
    cmd_integrate = f"{base} integrate <STAGE> {alvo}"
    cmd_handoff = f"{base} handoff <STAGE> {alvo}"
    cmd_state = f"{base} state {alvo}"
    cmd_retry = f"{base} auto --retry {alvo}"

    return "\n".join([
        "# PROTOCOLO PILOTO — `wk code` de ponta a ponta",
        "",
        "Você é o PILOTO (LLM despachante) do pipeline de codebase do wiki-ai.",
        "Objetivo: levar o pipeline do estado atual até o fim SOZINHO, devolvendo o",
        "controle ao humano SOMENTE em decisão-chave ou falha.",
        "",
        "Você NÃO analisa o repositório. Você NÃO escreve conteúdo SDD. Você roda",
        "comandos, despacha subagentes e interpreta paradas.",
        "",
        "## 1. Caminhos resolvidos (use LITERALMENTE)",
        "",
        "| chave | valor |",
        "|---|---|",
        f"| python | `{python}` |",
        f"| wk.pyz | `{pyz}` |",
        f"| --store | `{store}` |",
        f"| --repo | `{repo}` |",
        f"| workdir (checkpoint) | `{workdir}` |",
        f"| agent-packs | `{os.path.join(workdir, 'agent-packs')}` |",
        f"| agent-outputs (ÚNICA área de escrita) | `{os.path.join(workdir, 'agent-outputs')}` |",
        "",
        "## 2. Comandos literais (copie e cole; não reescreva, não abrevie)",
        "",
        "| id | comando |",
        "|---|---|",
        f"| AUTO | `{cmd_auto}` |",
        f"| INTEGRATE | `{cmd_integrate}` |",
        f"| HANDOFF | `{cmd_handoff}` |",
        f"| STATE | `{cmd_state}` |",
        f"| RETRY | `{cmd_retry}` |",
        "",
        "`<STAGE>` = o sufixo do `parado_em` da parada corrente: em",
        "`\"parado_em\":\"fanout:modules\"` o `<STAGE>` é `modules`. Valores possíveis:",
        "`modules`, `rules`, `architecture`, `specs`, `synth`. Não existe campo `etapa`",
        "separado — o estágio SÓ vem daí.",
        "",
        "## 3. Como ler a saída do AUTO (contrato já existente, não invente outro)",
        "",
        "| exit | onde está o JSON | o que vem depois |",
        "|---|---|---|",
        "| 0 | **1ª linha do stdout** | em `fanout:*`, o prompt de handoff cru, logo abaixo |",
        "| 2 | **1ª linha do stderr** | nada |",
        "",
        "Capture stdout e stderr SEPARADAMENTE. Campos sempre presentes no JSON:",
        "`parado_em`, `motivo`, `acao`, `executados[]`, `progresso`.",
        "",
        "Exemplo de 1ª linha (fan-out):",
        '`{\"parado_em\":\"fanout:modules\",\"motivo\":\"...\",\"acao\":\"...\",'
        '\"executados\":[\"plan\",\"run modules\"],\"progresso\":\"etapa 2 de 8 — fase fanout\"}`',
        "",
        "## 4. LAÇO PRINCIPAL",
        "",
        "1. Rode **AUTO**.",
        "2. Parseie o JSON da parada (regra da seção 3).",
        "3. Despache pela tabela da seção 5.",
        "4. Volte ao passo 1 — SEMPRE. Nenhuma etapa é considerada fechada sem uma",
        "   revalidação por AUTO.",
        "",
        f"Teto: **{max_acoes} ações** (cada AUTO, cada INTEGRATE e cada rodada de fan-out",
        "conta 1). Ao bater o teto: PARE e reporte ao humano quantas voltas foram dadas",
        "e qual o `progresso` atual.",
        "",
        "## 5. Tabela de despacho por `parado_em`",
        "",
        "| `parado_em` | ação do piloto |",
        "|---|---|",
        "| `fanout:<stage>` | seção 6 — dispare os subagentes, integre, volte ao laço |",
        "| `decisao_humana` | seção 7 — PARE e pergunte ao humano |",
        "| `pipeline_completo` | seção 8 — tabela final e fim |",
        f"| `limite_de_acoes` | rode AUTO de novo (é só o teto de {AUTO_MAX_ACOES} ações de UMA invocação do `auto`, não uma falha) |",
        "| `erro` | seção 9 — no máximo "
        f"{PILOT_MAX_CORRECOES} correção automática, depois PARE |",
        "| `intervencao` | seção 9 — PARE imediatamente (o `auto` já tentou 2x) |",
        "| `sem_progresso` | seção 9 — PARE imediatamente (o laço não anda) |",
        "",
        "## 6. `fanout:<stage>` — o único passo em que VOCÊ age",
        "",
        "O prompt de handoff vem no stdout, abaixo da linha JSON. Ele contém N blocos:",
        "",
        "```",
        "[batch NN — subagente <stage>-bNN]",
        "  LEIA:    <caminho do agent-pack>",
        "  LEIA:    <caminho do contrato do estágio>",
        "  ESCREVA: <workdir>/agent-outputs/<stage>-batch-NN.txt",
        "  ITENS:   <ids separados por vírgula>",
        "```",
        "",
        "Passos, em ordem:",
        "",
        "1. **Conte** os blocos. N blocos = N subagentes. Nunca menos, nunca mais.",
        "2. **Dispare os N subagentes EM PARALELO** — todas as chamadas da Task tool",
        "   numa ÚNICA mensagem. Serializar aqui é o erro mais caro do fluxo.",
        "3. O prompt de CADA subagente é o bloco dele (LEIA/LEIA/ESCREVA/ITENS) +",
        "   as instruções por-subagente que o próprio handoff imprime abaixo dos blocos.",
        "   Repasse esse texto INTACTO; não resuma, não reescreva, não traduza.",
        "4. **Valide os recibos.** Cada subagente devolve `ARQUIVO` / `BLOCOS` / `BYTES`.",
        "   Um batch só está pronto quando TODAS estas condições valem:",
        "",
        "   | verificação | reprovado se |",
        "   |---|---|",
        "   | arquivo existe no caminho ESCREVA | ausente |",
        "   | `BYTES` > 0 e bate com o arquivo | zero, ou divergência grosseira |",
        "   | `BLOCOS` >= 1 | zero |",
        "   | conteúdo tem `=== <BLOCO>: <id> ===` … `=== END ===` | sem marcadores |",
        "",
        "5. **Recibo reprovado** → redispare SÓ aquele batch, uma vez. Reprovou de novo:",
        "   PARE e reporte ao humano (seção 9).",
        "6. Rode **INTEGRATE** com o `<STAGE>` da parada (um comando faz todos os merges",
        "   + o `done` do estágio; os gates de merge continuam ativos).",
        "7. Volte ao LAÇO (seção 4, passo 1).",
        "",
        "### 6.1 Regra de citação (repasse literal aos subagentes)",
        "",
        HANDOFF_CITACAO_REGRA,
        "",
        "### 6.2 Fallback de escrita negada",
        "",
        HANDOFF_FALLBACK_ESCRITA,
        "",
        "### 6.3 Se o INTEGRATE falhar",
        "",
        "| erro do integrate | ação |",
        "|---|---|",
        "| `output(s) de subagente ausente(s)` + `faltantes[]` | redispare SÓ os batches faltantes; depois INTEGRATE de novo |",
        "| violação de conteúdo (citação/marcador/prosa) | redispare SÓ o batch citado, com a violação no prompt; 1 tentativa |",
        "| `manifesto ausente` | rode AUTO (ele reprepara o fan-out) |",
        "| qualquer outro | PARE (seção 9) |",
        "",
        "## 7. `decisao_humana` — PARE e pergunte",
        "",
        "NÃO adivinhe a decisão. NÃO escolha um valor \"razoável\". Apresente ao humano:",
        "",
        "1. Uma tabela-resumo:",
        "",
        "   | campo | valor |",
        "   |---|---|",
        "   | progresso | `<progresso>` |",
        "   | motivo | `<motivo>` |",
        "   | pendências | `<missing[]` / `pendentes[]`, se houver> |",
        "   | já executado nesta sessão | `<executados[]>` |",
        "",
        "2. As opções válidas, quando o `motivo` as define:",
        "",
        "   | decisão | valores aceitos |",
        "   |---|---|",
        "   | tópico | slug do wiki-ai (`--topic`) |",
        "   | doc_level | `essencial` \\| `completo` \\| `detalhado` |",
        "   | granularity | `module` \\| `endpoint` \\| `use-case` \\| `hybrid` \\| `feature` |",
        "   | unidades de `specs` | lista separada por vírgula (`--specs-items`) |",
        "",
        "3. O comando literal do campo `acao` da parada.",
        "",
        "Então **aguarde a resposta do humano no chat**. Com a resposta, rode o AUTO",
        "acrescido da flag correspondente e volte ao LAÇO.",
        "",
        "## 8. `pipeline_completo` — encerre",
        "",
        "Apresente a tabela final e PARE:",
        "",
        "| campo | valor |",
        "|---|---|",
        "| progresso | `<progresso>` |",
        "| estágios fechados | `<executados[]` acumulado> |",
        "| próximo comando (humano) | `<acao>` — o `wk finish ... --approve` |",
        "",
        "**NÃO rode o `wk finish` por conta própria**: ele publica/promove/compila e",
        "exige `--approved-by`. É aprovação humana, não passo de piloto.",
        "",
        "## 9. `erro` / `intervencao` / `sem_progresso` — pare e mostre o cru",
        "",
        "| parada | tentativas de correção permitidas |",
        "|---|---|",
        f"| `erro` | {PILOT_MAX_CORRECOES} (o campo `acao` diz exatamente o quê) |",
        "| `intervencao` | 0 — o `auto` já repetiu o mesmo erro 2x |",
        "| `sem_progresso` | 0 — a ação sai 0 mas o pipeline não anda |",
        "",
        "Ao parar, entregue ao humano, SEM parafrasear:",
        "",
        "- o JSON da parada, inteiro (inclusive `erro{}`, `tentativas`, `assinatura`);",
        "- o comando exato que falhou;",
        "- stdout e stderr crus, truncados a 50 linhas cada;",
        "- os `comandos_redo[]`, se o payload trouxe algum.",
        "",
        f"Se o humano corrigir e mandar continuar, use **RETRY** (`{cmd_retry}`) para zerar",
        "o contador de tentativas e volte ao LAÇO.",
        "",
        "## 10. REGRAS INVIOLÁVEIS",
        "",
        "| # | regra |",
        "|---|---|",
        f"| R1 | A ÚNICA área de escrita no store é `{os.path.join(workdir, 'agent-outputs')}`. Nunca edite `state.json`, `agent-runs/`, `agent-packs/`, `sdd/`, `modules/` — nem para \"consertar\" um gate. |",
        "| R2 | O repositório analisado é READ-ONLY. Nada é escrito dentro dele. |",
        "| R3 | Nunca `git push`, nunca `git commit`, nunca alterar branch. |",
        "| R4 | Nunca edite um artefato SDD à mão para fazer um gate passar. Gate reprovado = subagente redisparado ou humano chamado. |",
        "| R5 | Nunca invente conteúdo de módulo/regra/spec. Quem produz conteúdo é o subagente, a partir do pack. |",
        f"| R6 | Teto de {max_acoes} ações por sessão de piloto. |",
        f"| R7 | No máximo {PILOT_MAX_CORRECOES} correção automática por falha. Depois, humano. |",
        "| R8 | Sempre revalide com AUTO depois de cada INTEGRATE. Nunca assuma que o estágio fechou. |",
        "| R9 | Nunca pule uma parada `decisao_humana` escolhendo um valor você mesmo. |",
        "| R10 | Repasse os textos de contrato (citação, fallback, instruções do handoff) LITERALMENTE aos subagentes. |",
        "",
        "## 11. Retomada",
        "",
        "O `auto` é *stateful*: o checkpoint vive em",
        f"`{os.path.join(workdir, 'state.json')}`.",
        "",
        "- Sessão morreu no meio? Rode este mesmo protocolo do início.",
        "- O AUTO reencontra o ponto exato e continua — não há passo a \"desfazer\",",
        "  não há flag de retomada, não refaça nada manualmente.",
        f"- Para inspecionar sem alterar nada: `{cmd_state}`.",
        "",
        "## 12. Comece agora",
        "",
        f"Rode **AUTO** (`{cmd_auto}`) e siga o LAÇO.",
    ])


_COMMAND_DESCRIPTION = (
    "Piloto do pipeline wk code: roda auto em laço, despacha os subagentes de "
    "fan-out, integra e só para em decisão humana ou falha."
)


def wk_flow_command(prompt: str) -> str:
    """Envelopa um prompt-mestre no formato de slash command do Claude Code."""
    return "\n".join([
        "---",
        f"description: {_COMMAND_DESCRIPTION}",
        "---",
        "",
        prompt,
        "",
    ])


# Slash command `.claude/commands/wk-flow.md` com placeholders. Gerado a partir
# de `pilot_prompt` — mesmo protocolo, uma só fonte de verdade: um ajuste no
# prompt-mestre chega ao slash command sem edição manual.
#
# ATENÇÃO — substitua os placeholders com `str.replace`, NUNCA com `str.format`:
# o corpo contém chaves literais (o exemplo de JSON da seção 3, os `{}` das
# tabelas) que fariam `format` estourar `KeyError`. Use `render_wk_flow_command`.
WK_FLOW_COMMAND_TEMPLATE = wk_flow_command(
    pilot_prompt(
        python="{python}",
        pyz="{pyz}",
        store="{store}",
        repo="{repo}",
        workdir="{store}/.codescan/<nome-do-repo>-<sha8>",
    )
)

WK_FLOW_PLACEHOLDERS = ("{python}", "{pyz}", "{store}", "{repo}")

WK_FLOW_COMMAND_FILENAME = "wk-flow.md"


def render_wk_flow_command(*, python: str, pyz: str, store: str, repo: str) -> str:
    """Resolve os placeholders de `WK_FLOW_COMMAND_TEMPLATE`.

    Usa `replace` (e não `format`) porque o corpo tem chaves literais — ver a
    nota em `WK_FLOW_COMMAND_TEMPLATE`.
    """
    values = {"{python}": python, "{pyz}": pyz, "{store}": store, "{repo}": repo}
    out = WK_FLOW_COMMAND_TEMPLATE
    for key, value in values.items():
        out = out.replace(key, str(value))
    return out


__all__ = [
    "AUTO_MAX_ACOES",
    "PILOT_MAX_ACOES",
    "PILOT_MAX_CORRECOES",
    "WK_FLOW_COMMAND_FILENAME",
    "WK_FLOW_COMMAND_TEMPLATE",
    "WK_FLOW_PLACEHOLDERS",
    "pilot_prompt",
    "render_wk_flow_command",
    "wk_flow_command",
]
