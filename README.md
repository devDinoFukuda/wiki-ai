# Wiki AI

## O que é

Wiki AI é a ferramenta que transforma fontes soltas — transcrições,
documentos, planilhas, XML de arquitetura e repositórios de código legado —
numa wiki corporativa curada, com proveniência rastreável por fonte. Serve
times que precisam manter conhecimento de negócio e de sistemas legados
navegável e citável, sem deixar saída de LLM virar fato sem revisão. Entrega
três coisas: páginas Markdown em `wiki/` (fonte-verdade legível), a mesma
wiki em `.docx` (`wiki-docx/`) para bibliotecas SharePoint consumidas pelo
Copilot Studio, e, quando a fonte é um repositório, uma árvore de artefatos
SDD (inventário, C4, ERD, specs por unit) sob `.codescan/`.

## Arquitetura e fluxo

```
fonte externa ──ingest/publish──> inbox/ (não-verificado)
                                     │
                                  promote
                                     ▼
                                   raw/ (fonte-verdade; confidence/promoted_by)
                                     │
                         ┌───────────┴───────────┐
                      compile                   docx
                         ▼                         ▼
                      wiki/                   wiki-docx/
                (gerado, .md, reproduzível)  (gerado, .docx, p/ SharePoint)
```

- **Store**: raiz de dados do Wiki AI, separada da skill (`INSTALL.md` §2).
  Contém `inbox/`, `raw/`, `wiki/`, `wiki-docx/`, `log.md`, `quarantine.md`.
  Estrutura normativa completa em [schema.md](schema.md) §1.
- **Proveniência**: todo arquivo em `inbox/`/`raw/` carrega `source_type`,
  `confidence` e `origin` no frontmatter ([schema.md](schema.md) §2).
  `source_type: agent-output` nunca vira `confidence: reviewed`, nem
  aprovado manualmente — vira `unverified` para sempre, e `origin` preserva
  a origem de agente ([schema.md](schema.md):51,69-71). É o que impede
  saída de LLM de virar fato canônico sem revisão humana.
- **Promoção**: `inbox/` é entrada não-verificada; `promote` é o único
  portão para `raw/`. `code-repo` auto-promove; os demais tipos exigem
  `--approve`/`--approve-all` explícito ([schema.md](schema.md) §3).
- **Gerado vs. fonte-verdade**: `wiki/` e `wiki-docx/` são derivados de
  `raw/` — nunca edite à mão (`INSTALL.md`: "Página some após compile:
  correto: a wiki é derivada"). `compile` reescreve cada página a partir de
  `raw/` a cada rodada; `wk docx` além disso poda `.docx` órfão por padrão.
- **`wiki-docx/`**: existe porque o conector SharePoint do Copilot Studio
  não aceita `.md` como formato de biblioteca de documentos — só
  DOC/DOCX, PPT/PPTX e PDF ([operations/docx.md](operations/docx.md):88-96).
  `wk store init` não cria essa árvore; ela só existe sob demanda, na
  primeira execução de `wk docx` (`INSTALL.md` §2, linhas 63-66).
- **Índice (`index.db`)**: derivado de `raw/` + `wiki/`, reconstruível e
  deletável ([schema.md](schema.md) §7). `status` sujo bloqueia `compile` e
  `lint`; `reindex` destrava.
- **Fan-out de subagentes**: os estágios `modules`, `rules`, `architecture`,
  `specs` e `synth` do pipeline de código exigem subagente — o orquestrador
  nunca gera conteúdo SDD diretamente. Existe por dois motivos: limite de
  contexto (cada subagente lê só o material do próprio batch, não o
  workdir inteiro) e isolamento da leitura do repo (cada batch grava o
  próprio arquivo e devolve só um recibo de 3 linhas, nunca o conteúdo).
  `run-stage` informa `fanout_required`; `merge-agent-output` recusa
  `--agent` ausente ou genérico ([SKILL.md](SKILL.md):77-83).
- **codescan/SDD**: ao rodar o pipeline sobre um repositório, o Wiki AI cria
  um workdir isolado em `store/.codescan/<repo>-<hash>` (hash derivado do
  caminho ou URL do repo). Os estágios seguem a ordem
  `surface → modules → rules → architecture → specs → evidence → synth →
  verify`; ao final, `publish` leva a árvore `sdd/**` e `modules/*.md` para
  `inbox/`.

Visão geral em 5 linhas: uma fonte entra por `ingest` (arquivo solto) ou
`publish` (saída do pipeline de código) em `inbox/` (seção 2.1/2.27);
`promote` decide o que vira fonte-verdade em `raw/` (seção 2.28); `compile`
gera `wiki/` (seção 2.29) e `docx` gera `wiki-docx/` (seção 2.30), os dois a
partir de `raw/`; `lint` fecha o runbook auditando o índice (seção 2.31).

## Como usar este documento

Runbook de execução. Leia de cima para baixo e execute na ordem — cada passo
diz o comando, o que esperar e o que fazer se falhar. Não pule passos.

**Dois modos de uso.** Seções 1–8 são o modo CLI (você digita cada comando).
A **seção 9** é o modo skill (`/wiki-ai ingest codebase ...`), em que o agente
executa o pipeline por você. Se seu caminho é o slash command, vá direto para
a seção 9 — ela mapeia cada operação da skill ao comando CLI equivalente.

Regra de contexto: `wk code` imprime o corpo completo por padrão. `--quiet` é
opt-in: condensa a saída a uma única linha JSON de resumo
(`{"ok":true,"cmd":"<subcomando>", ...campos condensados...}`) — listas viram
`<chave>_count`, dicts viram `<chave>_keys`. `--verbose` é aceito só por
compatibilidade (no-op explícito; o corpo completo já é o padrão). Ambas as
flags valem antes ou depois do subcomando. O manifesto completo sempre é
gravado em disco antes da impressão. A economia de contexto real é outra: o
subagente grava o próprio arquivo de saída e devolve só o recibo de 3 linhas
(`ARQUIVO:`/`BLOCOS:`/`BYTES:`).

## 0. Terminal

Use Git Bash. Não use PowerShell — a sintaxe deste runbook (`export`, aspas,
`&&`) quebra em PowerShell e o erro aparece truncado/deformado. Se você
suspeita estar em PowerShell, embrulhe o comando: `bash -c '...'`.

## 1. Setup (uma vez por máquina)

1.1. Abra um terminal Git Bash.

Responsável: 👤 humano — abre o terminal Git Bash.

1.2. Defina as variáveis de execução:

Responsável: 👤 humano — define as variáveis de ambiente da sessão.

```bash
export WKPY='/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe'
export WK='C:/Users/User/Downloads/files/second-brain/wk.pyz'
export WK_STORE='C:/Users/User/OneDrive/Documentos/projetos/wiki/store'
export WK_REPO='C:/Users/User/projetos/desafio-tech-lead/insurance-quote-service'
```

`WK_STORE`/`WK_REPO` já vão aqui porque os passos 1.3 e 1.5, logo abaixo,
dependem deles — defini-los só na seção 2 deixa `--store`/`--repo` vazios
neste ponto do runbook.

Esperado: nenhuma saída (só `export`). Se falhar: `WKPY`/`WK`/`WK_STORE`/
`WK_REPO` apontam para caminho errado — confira com `ls "$WKPY"`,
`ls "$WK"`, `ls "$WK_STORE"` e `ls "$WK_REPO"`.

1.3. Materialize a skill e grave as permissões da engine sobre store/repo:

Responsável: 🔧 script — materializa a skill e grava as permissões.

```bash
$WKPY "$WK" init --engine devin --store "$WK_STORE" --repo "$WK_REPO"
```

Esperado: JSON com `"engines"`, `"alvos"` (diretórios escritos) e
`"permissoes"` (settings.json atualizado com `additionalDirectories`/`deny`),
um item por arquivo, com `"formato"`: `verificado` só para `claude-code`
(schema real de `.claude/settings.json`, comprovadamente consumido pela
engine); `best-effort` para antigravity/devin/copilot (`.agents/settings.json`
é gravado, mas nada garante que a engine o consuma).
Se falhar: erro `existente e diferente; use --force` — repita com `--force`
só se a divergência for esperada (ex.: versão nova do `wk`).

1.4. Crie a estrutura do store:

Responsável: 🔧 script — cria a estrutura de diretórios do store.

```bash
$WKPY "$WK" store init "$WK_STORE"
```

Esperado: JSON com `"criados"`/`"ja_existiam"` listando `inbox/`, `raw/`,
`wiki/`, `log.md`, `quarantine.md`. Idempotente — rodar de novo não apaga
nada. Não cria `wiki-docx/` — essa árvore só existe depois do primeiro
`wk docx` (passo 2.30), sob demanda. Se falhar: caminho de `$WK_STORE`
inválido (ex.: unidade não montada).

1.5. Verificação final — rode `wk doctor` antes de qualquer outra coisa:

Responsável: 🔧 script — verifica o ambiente e indica o próximo passo.

```bash
$WKPY "$WK" doctor --store "$WK_STORE" --repo "$WK_REPO" --engine devin
```

Esperado: `"bloqueios": []` e `"proximo_passo": "ambiente ok — nenhuma ação
necessária"` (ou já apontando `wk code --repo ... --store ... surface
--topic ...`, o que também é sucesso) — exit `0`. Invariante: exit `0` se e
somente se `"bloqueios"` estiver vazio. Engine desconhecida entra em
`"bloqueios"` (`"engine"`) e força exit != 0; `proximo_passo` nunca ecoa a
engine inválida de volta — só lista as engines válidas e o `init` corrigido.
Com `--store`/`--repo`, `engine.permissao_garantida` só é `true` para
`claude-code` (formato `verificado`); para antigravity/devin/copilot fica
`false` mesmo com o arquivo gravado corretamente (formato `best-effort` —
`aviso_permissao` explica). Se falhar: `"bloqueios"` não vazio —
`proximo_passo` traz o comando exato de correção (reexecutar `init`, corrigir
`--repo`, ou `wk store init`). Não use `--help` para descobrir o ambiente;
`doctor` substitui essa fase.

## 2. Runbook de codebase

**Quem faz cada passo.** Cada subseção abre com a linha `Responsável:`, com um
destes marcadores:

| Marcador | Significado |
|---|---|
| 🔧 **script** | o `wk` executa sozinho, sem LLM. Mesma entrada → mesma saída (determinístico). Auditável e repetível. |
| 🤖 **LLM** | conteúdo escrito por um subagente. O `wk` só prepara o pacote de entrada e valida a saída — nunca gera o conteúdo. |
| 👤 **humano** | decisão ou ação que exige pessoa: escolher valores, aprovar promoção, abrir um terminal. |

Passos mistos trazem os dois marcadores, na ordem em que acontecem.

**Resumo desta seção** (2.1–2.31, com o 2.7-bis, 32 subseções ao todo —
mistos contam em mais de uma linha):

| Marcador | Aparece em |
|---|---|
| 🔧 script | 31 dos 32 passos |
| 🤖 LLM | 5 dos 32 passos |
| 👤 humano | 4 dos 32 passos |

Até `agent-pack`/`sdd-brief` (contratos na seção 3) tudo é determinístico;
o conteúdo do SDD em si (módulos, regras, arquitetura, specs, synth) só
existe via 🤖; `promote` de fonte que não é `code-repo` só avança com 👤.

Pré-requisito: passo 1 completo. `WK_STORE`/`WK_REPO` já foram exportados no
passo 1.2 — o bloco abaixo repete os dois (idempotente) para quem retoma o
runbook direto nesta seção, e acrescenta `WK_TOPIC`, usado a partir daqui:

```bash
export WK_STORE='C:/Users/User/OneDrive/Documentos/projetos/wiki/store'
export WK_REPO='C:/Users/User/projetos/desafio-tech-lead/insurance-quote-service'
export WK_TOPIC='codebases/insurance-quote-service'
```

Sequência completa, sem pular etapas: `surface` 🔧 → `export` 🔧 →
`config` 👤🔧 → `plan` 🔧 → `run-stage modules` 🔧 → fan-out 🤖 →
`merge-agent-output modules` 🔧 (um por batch) → `done modules` 🔧 →
`run-stage rules` 🔧 → merge 🤖🔧 → `done rules` 🔧 →
`run-stage architecture` 🔧 → merge 🤖🔧 → `done architecture` 🔧 →
`pending specs` 👤🔧 → `run-stage specs` 🔧 → merge 🤖🔧 →
`done specs` 🔧 → `evidence` 🔧 → `done evidence` 🔧 →
`run-stage synth` 🔧 → merge 🤖🔧 → `done synth` 🔧 → `verify` 🔧 →
`done verify` 🔧 → `audit --strict` 🔧 → `publish` 🔧 →
`promote` 🔧👤 → `compile` 🔧 → `docx` 🔧 → `lint` 🔧.

`redo` não entra nessa cadeia — é fora-de-banda, só quando um estágio já
`done` precisa ser refeito (👤🔧, ver 2.7-bis).

### 2.1. `surface` — estágio 1: mapa determinístico

Responsável: 🔧 script — varre o repo e grava `surface.json`.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" surface --topic "$WK_TOPIC"
```

Esperado: `"ok":true`, `"cmd":"surface"`, com `arquivos`, `loc`, `modulos`,
`entry_points` e `artifact` (caminho de `surface.json`). Sem `error`.

Se falhar: `--repo` não existe → corrija o caminho. `store não informado` →
falta `--store` explícito ou `WK_STORE` no ambiente.

### 2.2. `export` — artefatos SDD determinísticos (sem LLM)

Responsável: 🔧 script — gera os artefatos SDD determinísticos.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" export --topic "$WK_TOPIC"
```

Esperado: `"ok":true`, `"cmd":"export"`; grava `sdd/inventory.md`,
`sdd/dependencies.md`, `sdd/coupling.md` no workdir. Passo obrigatório do
estágio 1, não opcional. `coupling.md` usa o motor Java (classe e pacote,
ciclos, classe-deus, hotspot de Ca, abstração especulativa; limiares por
outlier IQR) quando o repo tem Java, e o motor genérico multi-linguagem
(Ce/Ca/I por módulo) caso contrário — quando o motor Java roda, o export
também grava `sdd/coupling.html` (mapa interativo, artefato LOCAL de
inspeção; não entra em `inbox/`, não publica).

Se falhar: erro `rode surface primeiro` → repita 2.1. Nunca use `--output`
apontando para `raw/`/`wiki/` do store (bloqueado — use o fluxo de
`publish` na seção 2.27).

### 2.3. `config` — decisões do SDD

Obrigatório antes de `plan` e de `run-stage modules`.

Responsável: 👤 humano decide `--doc-level`/`--granularity` · 🔧 script grava.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" config --doc-level detalhado --granularity hybrid
```

Esperado: `"ok":true`, `"cmd":"config"`, ecoa `doc_level`/`granularity`
gravados no estado.

Se falhar: `--doc-level`/`--granularity` fora das opções válidas
→ argparse recusa antes de chamar o comando; corrija o valor. Válidos:

- `--doc-level`: `essencial` · `completo` · `detalhado`
- `--granularity`: `module` · `endpoint` · `use-case` · `hybrid` ·
  `feature` · `custom`

### 2.4. `plan` — lista de módulos a cavar (LOC, main primeiro)

Responsável: 🔧 script — monta a lista de módulos a cavar, por LOC.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" plan
```

Esperado: `"ok":true`, `"cmd":"plan"`, lista de módulos condensada em
`*_count`. Não fecha nenhum estágio — só planeja.

Se falhar: erro `config SDD obrigatória antes de plan/run-stage modules`
com campo `"acao"` trazendo o comando `config` exato → rode 2.3 antes.

### 2.5. `run-stage modules` — manifesto de fan-out

Responsável: 🔧 script — só prepara o manifesto; não gera conteúdo.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage modules
```

Esperado: `fanout_required` (inteiro, N subagentes exigidos), `next_action` e,
no corpo completo (padrão), `batches[].output`, `batches[].agent_slot` e
`batches[].merge_command` de cada batch. Use `--quiet` só para condensar a
uma linha (`fanout_required`/`next_action` sobrevivem por serem escalares;
`batches` vira `batches_count`). O manifesto completo é sempre gravado em
`$WK_STORE/.codescan/<repo>-<hash>/agent-runs/modules-plan.json`.

Se falhar: erro `sem módulos pendentes; rode plan` → repita 2.4. Erro de
permissão em `agent-outputs/` → refaça 1.3 (`wk init --store ... --repo ...`).

### 2.6. Fan-out — dispare os subagentes

Responsável: 🤖 LLM — cada subagente escreve o próprio arquivo de saída.

Não é comando `wk`. Dispare **exatamente `fanout_required` subagentes**, um
por batch, com listas de itens disjuntas (cada módulo cai em um único
batch). Para cada batch `N`:

- o caminho de saída é o campo `output` do batch (corpo completo do passo
  2.5, padrão, ou o manifesto em disco):
  `.../agent-outputs/modules-batch-NN.txt`;
- o subagente **grava o próprio arquivo** nesse caminho, no formato
  `=== MODULE: <path> === ... === END ===` (contrato completo em
  `sdd-brief modules`, seção 8);
- o subagente devolve só o recibo de 3 linhas — `ARQUIVO:`/`BLOCOS:`/`BYTES:`
  — nunca o conteúdo do artefato, log, diff ou eco de comando na mensagem.

### 2.7. `merge-agent-output modules` — um por batch

Responsável: 🔧 script — valida e integra a saída do 🤖 (passo 2.6).

Comando (repita para cada batch, trocando `NN` e `--agent`):

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output modules --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/modules-batch-01.txt" --agent modules-b01
```

Esperado: `"ok":true`, `"cmd":"merge-agent-output"`, artefatos gravados em
`modules/<slug>.md` (um por módulo do batch).

Se falhar: `--agent` ausente → obrigatório em todo `merge-agent-output`.
`--agent` genérico (`main`/`orquestrador`/`orchestrator`/`self`/`principal`)
→ use o `agent_slot` real do batch (ex.: `modules-b01`). `input` com `mtime`
anterior ao plano → o arquivo é anterior ao `run-stage`; grave o output de
novo, depois do passo 2.5.

### 2.7-bis. `redo` — refaz um estágio já mergeado

Responsável: 👤 humano decide refazer · 🔧 script executa.

Não é um passo obrigatório da sequência linear (ver nota no fim da lista
acima). Use quando um estágio já `done` — ou um item específico dele —
precisa ser reaberto e reescrito, sem apagar o histórico de runs
anteriores. `redo` marca os runs `current` cobertos como `superseded`
(nunca apaga conteúdo) e devolve o(s) item(ns) a `pending` em
`state.json`. Vale para `modules`, `rules`, `architecture`, `specs` e
`synth`.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" redo modules --item nome-do-modulo
```

Sem `--item`, reabre todos os itens `current` do estágio.

Esperado: `"itens_reabertos"`, `"runs_superseded"` (quantidade + ids) e
`"proximo_passo"` apontando `run-stage <stage>` — volte ao `run-stage`
do estágio (2.5/2.9/2.12/2.16/2.21, conforme o caso) para gerar um novo
manifesto de fan-out dos itens reabertos. A partir daqui o manifesto do
estágio passa a usar o schema `wiki-ai.agent-runs.v3` (carrega
`status`/`supersedes` por run; `audit` aceita `v2` e `v3` — ver 2.26).

Se falhar: `agent-runs ausente ou ilegível` → rode `run-stage`/
`merge-agent-output` do estágio antes de `redo`. `item nunca coberto por
nenhum run` → confira o nome do item (o mesmo usado no
`merge-agent-output` original).

### 2.8. `done modules`

Responsável: 🔧 script — fecha o gate do estágio `modules`.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done modules
```

Esperado: `"ok":true`, estado do estágio `modules` com `"status":"done"`.

Se falhar: `fan-out obrigatório não cumprido` → todos os batches foram
registrados com o mesmo `--agent`; refaça o `merge-agent-output` dos batches
que faltam com um `--agent` realmente distinto. Itens `pending`/`blocked`/
`failed`/`degraded` → resolva-os (repita 2.6–2.7 para o batch faltante, ou
use `blocked`/`failed`/`degraded <stage> --item <item>` para registrar o
problema).

### 2.9. `run-stage rules`

Responsável: 🔧 script — prepara o manifesto de fan-out de `rules`.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage rules
```

Esperado: igual a 2.5, mas para `rules` — normalmente 1 batch (`agent_slot`
tipo `rules-b01`). Sem fan-out múltiplo obrigatório quando `fanout_required
<= 1`.

Se falhar: mesmos casos de 2.5, adaptados ao estágio `rules`.

### 2.10. Subagente + `merge-agent-output rules`

Responsável: 🤖 LLM escreve as regras · 🔧 script integra o merge.

O subagente grava o recibo em `output` do batch (2.9), com blocos
`=== RULES: domain ===` (e `=== RULES: state-machines ===` /
`=== RULES: permissions ===` / `=== RULES: adrs/NNN-<slug> ===` para ADRs
retroativos em `doc_level` completo/detalhado — ver tabela da seção 3).
Depois:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output rules --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/rules-batch-01.txt" --agent rules-b01
```

Esperado/Se falhar: mesmos casos de 2.7, estágio `rules`.

### 2.11. `done rules`

Responsável: 🔧 script — fecha o gate do estágio `rules`.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done rules
```

Esperado/Se falhar: mesmos casos de 2.8, estágio `rules`. Em `doc_level`
completo/detalhado, `done rules` também exige ao menos um ADR retroativo em
`sdd/adrs/`.

### 2.12. `run-stage architecture`

Responsável: 🔧 script — prepara o manifesto de fan-out do estágio
`architecture`.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage architecture
```

Esperado/Se falhar: mesmos casos de 2.9, estágio `architecture`.

### 2.13. Subagente + `merge-agent-output architecture`

Responsável: 🤖 LLM escreve os diagramas · 🔧 script integra o merge.

Blocos aceitos: `=== ARCHITECTURE: architecture ===` / `c4-context` /
`c4-containers` / `c4-components` / `erd-complete` /
`traceability/spec-impact-matrix` / `sequences/<slug>` (os últimos por
`doc_level` — ver seção 3; `sequences/` só em `detalhado`).

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output architecture --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/architecture-batch-01.txt" --agent architecture-b01
```

Esperado/Se falhar: mesmos casos de 2.7, estágio `architecture`.

### 2.14. `done architecture`

Responsável: 🔧 script — fecha o gate do estágio `architecture`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done architecture
```

Esperado/Se falhar: mesmos casos de 2.8, estágio `architecture`. Em
`doc_level` detalhado, `done architecture` também exige ao menos um diagrama
de sequência em `sdd/sequences/`.

### 2.15. `pending specs` — registra as units a especificar

Responsável: 👤 humano decide a lista de units · 🔧 script registra.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" pending specs --items "criar-cotacao,emitir-apolice,invalidar-cache"
```

Esperado: `"ok":true`, `"cmd":"pending"`, estágio `specs` com `pending_count`
igual ao número de itens informados.

Se falhar: `stage` inválido → só os estágios de `STAGES` são aceitos; confira
grafia (`specs`, não `spec`).

### 2.16. `run-stage specs`

Responsável: 🔧 script — prepara o manifesto de fan-out de `specs`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage specs
```

Esperado/Se falhar: mesmos casos de 2.9, estágio `specs`, batch com os itens
registrados em 2.15.

### 2.17. Subagente + `merge-agent-output specs`

Responsável: 🤖 LLM escreve as specs · 🔧 script integra o merge.

Bloco por unit: `=== SPEC: <unit> ===`, com três sub-blocos obrigatórios
dentro — `--- requirements.md ---`, `--- design.md ---`, `--- tasks.md ---` —
e os opcionais `--- contracts.md ---` / `--- edge-cases.md ---`. Em
`doc_level` detalhado o `sdd-brief` pede os dois, mas `done specs <unit>`
não os exige; a auditoria de `done specs` (sem `--item`) só bloqueia se
nenhum contracts.md/edge-cases.md existir em todo o estágio. O mesmo
cabeçalho `=== SPEC: ... ===` também aceita documentos nomeados de revisão
final: `confidence-report`, `gaps`, `traceability/code-spec-matrix`,
`user-stories/<slug>` (grava `.md`) e `openapi/<slug>` (grava `.yaml`) —
corpo livre, sem sub-blocos (ver seção 3).

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output specs --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/specs-batch-01.txt" --agent specs-b01
```

Esperado/Se falhar: mesmos casos de 2.7, estágio `specs`. Artefatos vão para
`sdd/specs/<slug-da-unit>/{requirements,design,tasks}.md`.

### 2.18. `done specs`

Responsável: 🔧 script — fecha o gate do estágio `specs`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done specs
```

Esperado/Se falhar: mesmos casos de 2.8; exige ao menos uma unit concluída,
`sdd/confidence-report.md` não vazio e, em `doc_level` >= completo,
`sdd/gaps.md`, `sdd/traceability/code-spec-matrix.md` e ao menos uma user
story em `sdd/user-stories/`.

### 2.19. `evidence` — evidence-pack por tópico

Responsável: 🔧 script — monta o evidence-pack do tópico.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" evidence --topic "$WK_TOPIC"
```

Esperado: `"ok":true`, `"cmd":"evidence"`, `artifact` apontando para
`evidence-<topic>.json` no workdir.

Se falhar: sem candidatos para o tópico → confira `--topic`/`--top`; nenhum
match não é erro fatal, mas o artefato sai vazio (revise antes de prosseguir).

### 2.20. `done evidence`

Responsável: 🔧 script — fecha o gate do estágio `evidence`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done evidence
```

Esperado/Se falhar: mesmos casos de 2.8, estágio `evidence`.

### 2.21. `run-stage synth`

Responsável: 🔧 script — prepara o manifesto de fan-out de `synth`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage synth
```

Esperado/Se falhar: mesmos casos de 2.9, estágio `synth`. `run-stage synth`
recusa se `modules`/`rules`/`architecture`/`specs` não estiverem `done`.

### 2.22. Subagente + `merge-agent-output synth`

Responsável: 🤖 LLM escreve confirmed/inferred · 🔧 script integra.

Blocos: `=== SYNTH: confirmed ===` / `=== SYNTH: inferred ===`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output synth --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/synth-batch-01.txt" --agent synth-b01
```

Esperado/Se falhar: mesmos casos de 2.7. Grava `sdd/confirmed.md` e/ou
`sdd/inferred.md` — local canônico; nunca uma cópia na raiz do workdir. O
manifesto `agent-runs/synth.json` exigido pelo `audit` só é gravado por este
merge.

### 2.23. `done synth`

Responsável: 🔧 script — fecha o gate do estágio `synth`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done synth
```

Esperado/Se falhar: mesmos casos de 2.8, estágio `synth`.

### 2.24. `verify` — valida Markdown confirmado contra arquivo:linha

Responsável: 🔧 script — valida citações `arquivo:linha` contra o repo.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" verify --artifact "$WK_STORE/.codescan/<repo>-<hash>/sdd/confirmed.md"
```

Esperado: `"ok":true`, `"cmd":"verify"`, relatório com citações
`arquivo:linha` validadas contra o repo.

Se falhar: citação não bate com o repo → corrija `confirmed.md` (via novo
merge de `synth`) e repita.

### 2.25. `done verify`

Responsável: 🔧 script — fecha o gate do estágio `verify`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done verify
```

Esperado/Se falhar: mesmos casos de 2.8, estágio `verify`.

### 2.26. `audit --strict`

Responsável: 🔧 script — audita os artefatos de todos os estágios.

Nota: `--strict` é mantido por compatibilidade — os gates P0 já são padrão
em `audit` mesmo sem a flag; ela não liga nada adicional. O comando continua
válido no runbook.

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" audit --strict
```

Esperado (corpo completo, padrão): `"status":"pass"`, `score >= threshold`,
`stages_evaluated` igual ao nº de estágios do escopo estrito e
`artifacts_checked > 0`. Sem estágio elegível (nenhum artefato verificado), o
resultado **não** é `pass`: é `"status":"sem_evidencia"`, `"score":null`, com
`stages_evaluated`/`artifacts_checked` (podendo ser `0`) e `"message"`
apontando os estágios pendentes e o `sdd-brief` a rodar — `--strict` sai com
código != 0 nesse caso. `score: 100` só significa aprovação real quando
`stages_evaluated > 0`; nenhum score aparece por ausência de evidência. Use
`--quiet` para condensar a uma linha (`blockers_count` soma os blockers de
todos os estágios/artefatos).

Se falhar: blockers em `stages[].blockers`/`stages[].artifacts[].blockers`
(ou `blockers_count` > 0 sob `--quiet`) → cada blocker cita estágio +
artefato + causa; corrija na origem (reabra o estágio com
`merge-agent-output`) e repita o `audit`. `"status":"sem_evidencia"` →
nenhum estágio do escopo tinha artefato para checar; siga o `message` do
próprio relatório. `P0: agent-runs schema legado (wiki-ai.agent-runs.v1)` →
refaça o estágio citado com o merge atual (schema `wiki-ai.agent-runs.v2`;
`redo` grava `v3`, também aceito — ver 2.7-bis).
`P0: artefato alterado após o merge` → não editar artefato SDD à mão; refaça
`merge-agent-output` do estágio.

### 2.27. `publish` — leva a árvore SDD do workdir para `inbox/`

Responsável: 🔧 script — copia a árvore SDD do workdir para `inbox/`.

Comando:

```bash
$WKPY "$WK" publish --workdir "$WK_STORE/.codescan/<repo>-<hash>" --topic "$WK_TOPIC" --store "$WK_STORE"
```

Esperado: `"publicados"` com um item por arquivo elegível (`sdd/**/*.md`,
`modules/*.md`, `confirmed.md`/`inferred.md`). Único caminho válido de
workdir do codescan para `inbox/` — nunca escreva ali manualmente.

Se falhar: `workdir não encontrado` → confira `<repo>-<hash>`; rode `wk code
--repo "$WK_REPO" --store "$WK_STORE" state` para achar o workdir certo.
`aviso: nenhum artefato elegível` → estágios anteriores não geraram `.md` em
`sdd/`/`modules/` — não é erro fatal, mas revise antes de prosseguir.

### 2.28. `promote` — move fontes seguras de `inbox/` para `raw/`

Responsável: 🔧 script auto-promove `code-repo` · 👤 humano aprova o
resto com `--approve`.

Comando:

```bash
$WKPY "$WK" promote --store "$WK_STORE"
```

Esperado: `"promovidos"` (auto-promovidos, ex. `source_type: code-repo`),
`"decisao_humana"` (exige `--approve`/`--approve-all`), `"quarentena"`
(proveniência incompleta), `"reindexed":true`.

Se falhar: `reindex_error` no corpo → índice não atualizou; rode `wk index
reindex --store "$WK_STORE"` manualmente e investigue o erro. Itens presos
em `decisao_humana` não são erro — exigem aprovação explícita:

```bash
$WKPY "$WK" promote --approve-all --source-type human-transcript --approved-by "Maria" --store "$WK_STORE"
```

### 2.29. `compile` — gera `wiki/` a partir de `raw/` promovido

Responsável: 🔧 script — gera `wiki/` a partir do que foi promovido.

Comando:

```bash
$WKPY "$WK" compile "$WK_TOPIC" --store "$WK_STORE"
```

Esperado: `"paginas"` (uma por fonte promovida, mais `wiki/index.md` —
catálogo agrupado por `topic`, com contagem total e por tópico),
`"reindexed":true`. Roda `reindex` sozinho ao final.

Se falhar: `"fontes":0` → nada foi promovido para esse tópico; volte a 2.28.

### 2.30. `docx` — gera `wiki-docx/` (DOCX) a partir de `raw/` promovido

Responsável: 🔧 script — gera `wiki-docx/` a partir do que foi
promovido.

Comando:

```bash
$WKPY "$WK" docx "$WK_TOPIC" --store "$WK_STORE"
```

Esperado: JSON com `documentos` (um por fonte promovida, com `path`, `id`,
`source_type`, `avisos`), `fontes`, `removidos` e `pulados`. Gera
`wiki-docx/<topic>/<source_type>/<arquivo>.docx` — árvore paralela a `wiki/`,
para bibliotecas SharePoint consumidas pelo Copilot Studio (`.md` não é
formato aceito lá). Não lê `wiki/`, não altera `wiki/`, não sobe nada para o
SharePoint. Idempotente: rodar de novo sobrescreve no mesmo caminho, nunca
gera `-2.docx`. Sem filtro de topic, poda toda a árvore `wiki-docx/`
(remove `.docx` órfão). Com `<topic>`, a poda fica restrita à subárvore
do topic — `.docx` de outros topics não é removido; `--no-prune` desliga
tudo.

Se falhar: `pulados` não vazio → exit `1`; cada item lista `id` e motivo.

### 2.31. `lint` — audita o índice e fecha o runbook

Responsável: 🔧 script — audita o índice e fecha o runbook.

Comando:

```bash
$WKPY "$WK" lint --store "$WK_STORE"
```

Esperado: `"achados":0` e `"report"` apontando para
`wiki/_lint-report.md`. Além das regras L do índice (L1/L2/L5), o lint
percorre `wiki/**/*.md` e checa **W1** (link markdown relativo quebrado),
**W2** (wikilink `[[nome]]` sem página alvo — match por stem do arquivo ou
`id` do frontmatter) e **W3** (página órfã, sem link de entrada; `index.md` e
`_lint-report.md` isentos). Runbook concluído quando `achados` chega a `0`.

Se falhar: `"achados" > 0` → cada regra em `"regras"` lista os documentos
problemáticos; corrija na fonte (`raw/`/`wiki/` conforme a regra) e repita.

## 3. Contratos de bloco por estágio

| Estágio | Cabeçalho aceito | Artefato gerado | Quem escreve |
|---|---|---|---|
| modules | `=== MODULE: <path> === ... === END ===` | `modules/<slug-do-path>.md` | 🤖 cabeçalho · 🔧 grava |
| rules | `=== RULES: domain \| state-machines \| permissions \| adrs/NNN-<slug> ===` (state-machines/permissions/adrs só em `doc_level` completo/detalhado) | `sdd/<nome>.md` / `sdd/adrs/NNN-<slug>.md` | 🤖 cabeçalho · 🔧 grava |
| architecture | `=== ARCHITECTURE: architecture \| c4-context \| c4-containers \| c4-components \| erd-complete \| traceability/spec-impact-matrix \| sequences/<slug> ===` (por `doc_level`; `sequences/` só em detalhado) | `sdd/<nome>.md` | 🤖 cabeçalho · 🔧 grava |
| specs (unit) | `=== SPEC: <unit> ===` com sub-blocos `--- requirements.md ---` / `--- design.md ---` / `--- tasks.md ---` (+ opcionais `--- contracts.md ---` / `--- edge-cases.md ---`; pedidos pelo `sdd-brief` em `doc_level` detalhado, não exigidos por `done specs <unit>`) | `sdd/specs/<slug-da-unit>/*.md` | 🤖 cabeçalho · 🔧 grava |
| specs (doc nomeado) | `=== SPEC: confidence-report \| gaps \| traceability/code-spec-matrix \| user-stories/<slug> \| openapi/<slug> ===` (corpo livre) | `sdd/<nome>.md` (`openapi/` grava `.yaml`) | 🤖 cabeçalho · 🔧 grava |
| synth | `=== SYNTH: confirmed \| inferred ===` | `sdd/confirmed.md` / `sdd/inferred.md` | 🤖 cabeçalho · 🔧 grava |

Contrato compacto e exato de cada estágio (seções obrigatórias, limites de
linha, regras de "sem prosa"): `wk code --repo "$WK_REPO" --store
"$WK_STORE" sdd-brief <stage>` (`stage` em `evidence, modules, rules,
architecture, specs, synth`) — rode antes de escrever o subagente.

`sdd-brief` é 🔧: gera o contrato, não o cumpre — é puramente
determinístico, sem chamar LLM. Quem cumpre o contrato — escreve os
blocos `=== ... ===` no formato exigido — é sempre 🤖 (subagente); o
`wk` nunca gera esse conteúdo, só valida a forma e grava o artefato
via `merge-agent-output` (🔧).

## 4. Retomada

Perdeu o fio? Nesta ordem, sem adivinhar:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" state
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" next
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" read <arquivo-do-repo> --from <linha> --count <n>
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" audit --strict
```

- `state` (🔧): estado completo do workdir por padrão; `--quiet` condensa a
  uma linha (`estagio_atual` sobrevive à condensação por ser escalar).
- `next` (🔧): o que fazer agora, sem reconstruir o histórico à mão.
- `read` (🔧): leitura confinada ao repo declarado, numerada — nunca leia o
  repo por fora do `wk` (sem `cat`, sem editor, sem IDE apontando pra fora
  do contrato).
- `audit --strict` (🔧): mede o que já está bloqueando antes de continuar;
  se nada foi checado ainda, reporta `"status":"sem_evidencia"` em vez de
  `pass` (seção 2.26) — nunca trate ausência de evidência como aprovação.

Achou o estágio/item a corrigir? `redo <stage> [--item <item>]` reabre:
marca os runs `current` cobertos como `superseded` (preserva a trilha) e
devolve o(s) item(ns) de `done` para `pending`. Não apaga nem reescreve o
run anterior — só destrava para um novo `merge-agent-output` do mesmo
item. `redo` é 👤 decide · 🔧 executa: a máquina nunca reabre um estágio
sozinha; só o operador (ou o orquestrador da skill), lendo `state`/`next`/
`audit`, tem esse critério.

Nunca use `python -c`, heredoc ou script ad hoc para ler JSON de
surface/state — use os comandos acima.

## 5. Erros comuns

| Sintoma | Causa | Correção | Quem corrige |
|---|---|---|---|
| `wk ingest codebase ...` recusado | `ingest codebase` não é subcomando do CLI — é a operação da skill | rode o fluxo da seção 2, a partir de `code ... surface` | 👤 |
| saída truncada/estranha, algo como `No linha:N caractere:` ou cheia de `~~` | comando rodou em PowerShell, não Git Bash | reexecute em Git Bash; se preso em PowerShell, embrulhe: `bash -c '...'` | 👤 |
| `store não informado: sem --store e sem WK_STORE, nada seria gravado no lugar certo` | faltou `--store` e `WK_STORE` não está no ambiente | passe `--store "$WK_STORE"` ou `export WK_STORE=...` (seção 2) | 👤 |
| `flag(s) --topic pertence(m) ao subcomando 'surface', não ao nível superior` | flag do subcomando escrita antes do subcomando | mova a flag para depois do subcomando — a própria mensagem traz o comando corrigido em `"acao"` | 👤 |
| `merge-agent-output exige --agent <identificador do subagente>` | `--agent` ausente | repita com `--agent <agent_slot-do-batch>` | 👤 |
| `--agent '...' é genérico demais` | usou `main`/`orquestrador`/`orchestrator`/`self`/`principal` | use o `agent_slot` real do batch (ex.: `modules-b01`) | 👤 |
| `input ... tem mtime anterior ao plano de fan-out` | `--input` é arquivo velho (sobra de tentativa anterior) | grave o output de novo, depois de rodar `run-stage` | 🤖 |
| `fan-out obrigatório não cumprido em <stage>` | `done` bloqueado: todos os batches vieram do mesmo `--agent` | refaça `merge-agent-output` do(s) batch(es) faltante(s) com `--agent` realmente distinto | 🤖 |
| `item já mergeado; rode wk code ... redo <stage> --item <item> antes de refazer` | reenvio de `merge-agent-output` sobre item que já tem run `current` registrado (BQ4) | rode `redo <stage> --item <item>` primeiro (reabre o item), depois refaça o `merge-agent-output` | 👤 decide (`redo`) · 🤖 refaz |
| `P0: agent-runs schema legado (wiki-ai.agent-runs.v1) sem prova criptográfica` | manifesto `agent-runs` antigo (v1), sem hash | refaça o estágio citado com o merge atual (schema v2) | 🤖 |
| `P0: artefato alterado após o merge` | artefato SDD editado à mão depois do merge (hash não bate) | não edite artefato manualmente; refaça `merge-agent-output` do estágio | 👤 não edita · 🔧 reroda |

Versionamento do manifesto: `merge-agent-output` continua gravando
`wiki-ai.agent-runs.v2`, como sempre. `redo` é o único comando que
reescreve o manifesto para `wiki-ai.agent-runs.v3` (mesmo schema, agora
com `status`/`supersedes` explícitos por run) — `audit` aceita v2 e v3
como válidos; só v1 (legado, sem hash) continua bloqueado
(`scripts/codescan/sdd.py`:690-698).

## 6. Gates

| Gate | Bloqueia | Quem é bloqueado |
|---|---|---|
| `audit` (gates P0; `--strict` é só compatibilidade) | falso positivo | 🤖 |
| `agent-pack v2` | leitura ampla do repo | 🤖 |
| `merge-agent-output` | escrita manual de SDD; `--agent` ausente/genérico; input com `mtime` anterior ao plano do stage; item já coberto por run `current` sem `redo` prévio | 👤 / 🤖 |
| `agent-runs` (schema `wiki-ai.agent-runs.v2`; vira `v3` depois de `redo`) | stage `done` sem subagente; manifesto v1 (legado) ou artefato/input alterado após o merge (hash `sha256` recomputado pelo audit) | 🤖 |
| `done --artifact` | fechar `done` com artefato que nunca passou por `merge-agent-output`; ou todos os batches registrados pelo mesmo `--agent` | 🤖 |
| `export --output` | gravar direto em `raw/`/`wiki/` do store (use `publish`) | 👤 |
| `code` sem store | execução sem `--store` explícito e sem `WK_STORE` no ambiente | 👤 |
| `ingest codebase` (CLI) | subcomando inexistente — erro aponta `code ... surface` | 👤 |
| workdir hygiene | `*.py`, `*.txt`, `src/`, specs vazias | 🤖 |
| Mermaid strict | diagrama vazio, denso, sem aresta, label inválido | 🤖 |
| noise | log, diff, `Ran command`, `Edited`, `Wrote` | 🤖 |

## 7. Proibido

### 7.1. Bloqueado por código

- script auxiliar no workdir — vale para 🤖.
- copiar repo para `store/.codescan` — vale para 👤 (o subagente não tem
  como copiar o repo; é erro de quem monta o ambiente).
- pasta vazia em `sdd/specs` — vale para 🤖.
- pasta vazia em `modules` — vale para 🤖.
- output fora dos blocos parseáveis — vale para 🤖.

### 7.2. Convenção (não bloqueado — disciplina do operador)

- PowerShell — vale para 👤.
- misturar `/wiki-ai ingest` com comando `python` — vale para 👤.
- log colado no chat — vale para 🤖.
- diff colado no chat — vale para 🤖.
- artefato recém-escrito colado no chat — vale para 🤖.
- `python -c`, heredoc ou script ad hoc para ler JSON de surface/state; use
  `state`, `next`, `read` e `audit` — vale para 👤 e 🤖 (CLI manual e
  orquestrador da skill).

## 8. Referência rápida de comandos

| Comando | Para quê | Quem |
|---|---|---|
| `wk doctor` | diagnóstico único do ambiente (shell, python, store, repo, engine) | 🔧 |
| `wk init --engine <e> [--store --repo]` | materializa a skill + grava permissões | 🔧 |
| `wk check --engine <e> [--store --repo]` | compara disco vs. embutido; valida permissões | 🔧 |
| `wk engines [--base]` | lista engines suportadas e o que já está instalado | 🔧 |
| `wk store init [caminho]` | cria `inbox/`/`raw/`/`wiki/` do store | 🔧 |
| `wk code ... surface --topic <slug>` | estágio 1: mapa determinístico do repo | 🔧 |
| `wk code ... export --topic <slug>` | inventory/dependencies/coupling — sem LLM; coupling usa motor Java ou genérico conforme o repo, e grava `coupling.html` local quando Java | 🔧 |
| `wk code ... config --doc-level --granularity` | grava decisões do SDD (obrigatório antes de plan) | 🔧 |
| `wk code ... plan` | lista módulos a cavar, por LOC | 🔧 |
| `wk code ... pending <stage> --items <lista>` | registra pendências de um estágio | 🔧 |
| `wk code ... run-stage <stage>` | manifesto de fan-out para subagentes — prepara, não gera conteúdo | 🔧 |
| `wk code ... sdd-brief <stage>` | contrato compacto do estágio para o subagente — prepara, não gera conteúdo | 🔧 |
| `wk code ... sdd-scaffold <stage>` | cria esqueleto dos artefatos SDD do estágio — prepara, não gera conteúdo | 🔧 |
| `wk code ... agent-pack <stage> --batch N` | pacote determinístico de um batch (todos os estágios; `modules` lê o repo, os demais leem o material do workdir) — prepara, não gera conteúdo | 🔧 |
| `wk code ... merge-agent-output <stage> --input --agent` | integra a saída de um subagente | 🔧 |
| `wk code ... redo <stage> [--item]` | reabre stage/item: runs `current` viram `superseded`, `done` volta a `pending` | 👤 decide · 🔧 executa |
| `wk code ... evidence --topic <slug>` | evidence-pack por tópico | 🔧 |
| `wk code ... verify --artifact <md>` | valida citações `arquivo:linha` | 🔧 |
| `wk code ... done <stage> [--item --artifact]` | marca estágio/item concluído | 🔧 |
| `wk code ... blocked\|failed\|degraded <stage>` | registra item com problema | 🔧 |
| `wk code ... state` | estado completo do workdir | 🔧 |
| `wk code ... next` | o que fazer agora | 🔧 |
| `wk code ... read <path> --from --count` | lê arquivo do repo, numerado | 🔧 |
| `wk code ... audit [--strict]` | mede qualidade SDD sem alterar estado | 🔧 |
| `wk code ... cleanup` | apaga o clone de um repo remoto (após o término) | 🔧 |
| `wk publish --workdir --topic` | leva a árvore SDD do workdir para `inbox/` | 🔧 |
| `wk promote [--approve \| --approve-all --source-type]` | move fontes seguras de `inbox/` para `raw/` | 👤 decide (não-`code-repo`) · 🔧 executa |
| `wk compile [topic]` | gera `wiki/` a partir de `raw/` promovido | 🔧 |
| `wk docx [topic] [--out-dir --no-prune]` | gera `wiki-docx/` (DOCX) a partir de `raw/` promovido | 🔧 |
| `wk lint [path]` | audita o índice, escreve `wiki/_lint-report.md` | 🔧 |
| `wk ingest <arquivo> --source-type --origin --topic` | registra fonte em `inbox/` com proveniência: md/txt passthrough; vtt/srt transcrição; html; xml → análise estrutural (draw.io/XMI/outline); json → bloco de código; docx/xlsx/csv/pdf → original imutável em `raw/assets/` + página de fonte para análise pelo LLM | 🔧 |
| `wk index reindex\|status` | manutenção do índice | 🔧 |
| `wk search "<query>"` / `wk get <ref>` | busca híbrida / recupera documento — query de 1 linha sem prefixo vira `lex:` automaticamente | 🔧 |
| `wk audit` | regras determinísticas L1/L2/L5 do índice | 🔧 |
| `wk docs [nome] [--list]` | documentação embutida no executável | 🔧 |

## 9. Modo skill (slash commands)

Alternativa ao runbook manual: em vez de digitar a sequência da seção 2, você
invoca a operação e o agente executa o pipeline. As oito operações existentes:

| Slash command | O que faz | Equivale, na CLI, a | Quem executa |
|---|---|---|---|
| `/wiki-ai ingest <arquivo>` | registra fonte solta (transcrição, doc, planilha, XML, clip) em `inbox/`; docx/xlsx/csv/pdf viram asset imutável em `raw/assets/` + página de fonte, e o agente escreve a análise (portão agent-output) | `wk ingest <arquivo> --source-type <t> --origin "<origem>" --topic <slug>` | 🔧 + 🤖 |
| `/wiki-ai ingest codebase <caminho> --topic <slug>` | pipeline de repositório inteiro (seção 2, do `surface` ao `lint`) | `wk code --repo <r> --store <s> surface --topic <slug>` e o restante da seção 2 | 🔧 + 🤖 |
| `/wiki-ai promote` | portão de promoção `inbox/` → `raw/` | `wk promote --store <s>` | 🔧 (+ 👤 se a fonte não for `code-repo`) |
| `/wiki-ai compile` | (re)gera `wiki/` a partir de `raw/` promovido | `wk compile <topic> --store <s>` | 🔧 |
| `/wiki-ai docx` | (re)gera `wiki-docx/` (DOCX) a partir de `raw/` promovido, para bibliotecas SharePoint | `wk docx <topic> --store <s>` | 🔧 |
| `/wiki-ai lint` | audita integridade (somente leitura) | `wk lint --store <s>` | 🔧 |
| `/wiki-ai query <pergunta>` | consulta a wiki | `wk search "<pergunta>" --store <s>` | 🔧 |
| `/wiki-ai reindex` | atualiza o índice de busca | `wk index reindex --store <s>` | 🔧 |

No modo skill, o agente dispara os comandos `wk` (🔧) e também escreve o
conteúdo (🤖 — ex.: a análise de um `ingest`, ou os blocos SDD do
fan-out). Mas isso não elimina o portão humano: `promote` de uma fonte
com `source_type` diferente de `code-repo` continua exigindo
`--approve`/`--approve-all` explícito, decisão de 👤, mesmo disparado de
dentro da skill (`scripts/wk/cli.py`:880). O modo skill acelera o
disparo, não substitui a aprovação humana onde ela é exigida.

### 9.1. Regra de entrada do slash command

Slash command **não é shell**: recebe texto literal e não expande variáveis do
Git Bash. Use caminho e topic literais.

Válido:

```text
/wiki-ai ingest codebase C:/Users/User/projetos/desafio-tech-lead/insurance-quote-service --topic codebases/insurance-quote-service
```

Inválido por design (chega como texto, não como valor):

```text
/wiki-ai ingest codebase $WK_REPO --topic $WK_TOPIC
```

Inválido (mistura operação da skill com invocação do executável):

```text
/wiki-ai ingest python 'C:/.../wk.pyz' index status codebase ...
```

### 9.2. Quando usar cada modo

| Situação | Modo |
|---|---|
| Repositório novo, ponta a ponta, com fan-out de subagentes | `/wiki-ai ingest codebase ...` |
| Retomar pipeline parado no meio | CLI, seção 4 (`state` → `next`) |
| Depurar um estágio específico | CLI, o passo correspondente da seção 2 |
| Arquivo solto (transcrição, doc, planilha, PDF, XML, VTT) | `/wiki-ai ingest <arquivo>` ou `wk ingest` |
| Publicar para bibliotecas SharePoint/Copilot Studio | `/wiki-ai docx` ou `wk docx <topic> --store <s>` |
| Auditar sem alterar nada | `/wiki-ai lint` ou `wk audit --store <s>` |

### 9.3. Pré-requisito

O slash command só existe depois de `wk init --engine <e>` (passo 1.3), que
materializa a skill no diretório da engine (`.claude/skills/wiki-ai` no Claude
Code, `.agents/skills/wiki-ai` nas demais). Verifique com:

```bash
$WKPY "$WK" check --engine devin --store "$WK_STORE" --repo "$WK_REPO"
```

## 10. Glossário

| Termo | Significado |
|---|---|
| store | raiz de dados do Wiki AI (`inbox/`, `raw/`, `wiki/`, `wiki-docx/`); separada da skill |
| workdir | diretório de trabalho do pipeline de código, `store/.codescan/<repo>-<hash>` |
| topic | slug que agrupa fontes/páginas por assunto (ex. `codebases/insurance-quote-service`) |
| estágio (stage) | fase do pipeline de código (`surface`, `modules`, `rules`, `architecture`, `specs`, `evidence`, `synth`, `verify`) |
| script determinístico | comando `wk` que roda sozinho, sem LLM — a mesma entrada sempre produz a mesma saída (ex. `surface`, `export`, `run-stage`, `merge-agent-output`) |
| subagente | processo de LLM disparado pelo fan-out para escrever o conteúdo de um batch; nunca é o orquestrador principal, nunca lê o repo por fora do pacote que recebeu |
| fan-out | disparo de N subagentes, um por batch, exigido nos estágios `modules`/`rules`/`architecture`/`specs`/`synth` |
| batch | fatia disjunta de um estágio, com `output` e `agent_slot` próprios, gerada por `run-stage` |
| agent_slot | identificador único do subagente de um batch (ex. `modules-b01`); exigido em `--agent`, recusa valor genérico |
| SDD | árvore de artefatos determinísticos/sintetizados do pipeline de código (`inventory`, C4, ERD, specs por unit) sob `sdd/` no workdir |
| proveniência | metadados obrigatórios de origem de um documento: `source_type`, `confidence`, `origin` |
| promoção | passagem de um arquivo de `inbox/` para `raw/` via `promote`, único portão para fonte-verdade |
| asset imutável | original de `.docx`/`.xlsx`/`.csv`/`.pdf` gravado direto em `raw/assets/`, fora do fluxo de promoção |
| gate | verificação que bloqueia uma ação até uma condição ser satisfeita (ex. índice sujo bloqueia `compile`/`lint`) |
| evidence-pack | pacote de evidências `arquivo:linha` por tópico, gerado por `evidence`, usado para escrever `confirmed.md`/`inferred.md` |
| doc_level | granularidade de conteúdo do SDD (`essencial`/`completo`/`detalhado`), decisão obrigatória via `config` |
| granularity | unidade de particionamento do plano do SDD (`module`/`endpoint`/`use-case`/`hybrid`/`feature`/`custom`) |
| agent-runs | manifesto de proveniência de um estágio (schema `wiki-ai.agent-runs.v2`; vira `v3` depois de `redo`), exigido por `done` e checado por `audit` |
| redo | comando que reabre stage/item: marca runs `current` como `superseded` e devolve `done` para `pending`, sem apagar a trilha anterior |
| superseded | status de um run de `agent-runs` que foi substituído por `redo`; continua no manifesto, só deixa de contar como cobertura vigente do item |

## 11. Limitações conhecidas

- `wk docx` não transporta nada para o SharePoint — não há chamada de
  rede, Graph API/REST ou biblioteca de upload no projeto; é decisão
  deliberada, fora de escopo desta v1. Alternativa até existir automação
  própria: sincronização de pasta via cliente OneDrive/SharePoint apontado
  para `wiki-docx/`, ou upload manual periódico pela interface web do
  SharePoint ([operations/docx.md](operations/docx.md):78-86).
- `wiki/` e `wiki-docx/` podem divergir entre si: não há gate de
  consistência entre as duas árvores, e elas nem se comportam igual —
  `compile` reescreve páginas a partir de `raw/` mas não poda página órfã,
  enquanto `wk docx` poda `.docx` órfão por padrão (`scripts/wk/cli.py`,
  `cmd_compile` vs. `cmd_docx`).
- Permissões de engine: só `claude-code` tem `"formato": "verificado"`
  (schema real de `.claude/settings.json`, comprovadamente consumido pela
  engine); `antigravity`/`devin`/`copilot` são `"formato": "best-effort"` —
  o arquivo é gravado, mas nada garante que a engine o consuma
  (`README.md` §1.3).
- `README.md` não é embutido no `.pyz`: não consta em `DOCS` de
  `scripts/build_pyz.py`. `schema.md`, `INSTALL.md` e `operations/*.md`
  constam e são lidos via `wk docs <slug>` em runtime — uma mudança neles
  exige rebuild do `.pyz` e `wk init --force` para propagar aos diretórios
  de skill das engines.
- `store init` não cria `wiki-docx/`: essa árvore só existe sob demanda,
  criada na primeira execução de `wk docx` (`INSTALL.md`:63-66;
  `STORE_TREE` em `scripts/wk/cli.py` não lista `wiki-docx`).
- Azure OpenAI (embeddings) não validado: `vec:`/`hyde:` dão erro de
  propósito em vez de retornar vazio; só o modo léxico (`lex:`) é testado
  (`INSTALL.md`:391-416,440). Mitigação nova desta sessão: uma query de
  texto livre em uma linha, sem prefixo, agora vira `lex:`
  automaticamente — reduz o risco de cair sem querer em `vec:`/`hyde:`
  sem embeddings configurados (`scripts/wk/cli.py`,
  `_SEARCH_MODE_PREFIXES`, linha 2240).
- Rescan de codebase divide artefatos por confiança, não por tipo: uma
  reanálise (`surface`/`export` de novo) produz reescrita completa da
  árvore SDD, não um diff (`INSTALL.md`:441-442). Mitigação parcial: para
  corrigir só um item, sem reescrever o estágio inteiro, use
  `redo <stage> --item <item>` (seção 4) — reabre aquele item, preserva
  o resto da árvore.
- Resumo (`dc:description`) de alguns `.docx` sai como placeholder
  genérico ("Artefato \<source_type\> do tópico ..."), não como texto
  real: `derive_summary` só extrai resumo de um parágrafo corrido sob
  heading em `SUMMARY_SECTIONS`, ou do primeiro parágrafo não-boilerplate
  do corpo. Fontes compostas só por tabela/lista — como `inventory.md`
  ou `coupling.md`, gerados por `codescan export` — não têm parágrafo
  corrido e caem no placeholder (`scripts/wk/docx_meta.py`,
  `derive_summary`:398-418, `BOILERPLATE_PREFIXES`:26-29,
  `SUMMARY_SECTIONS`:37-39).
