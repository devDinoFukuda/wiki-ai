# Wiki AI — comandos

Legenda: **👤** humano executa (tudo que é determinístico é do humano) · **🤖** LLM — usada SOMENTE onde há análise de conteúdo (fan-out de codebase, análise de asset, síntese de resposta).

Variáveis usadas em todos os comandos (troque pelos seus):

```bash
WKPY="python"
WK="C:/Users/User/projetos/wiki-ai/wk.pyz"
WK_STORE="C:/caminho/do/store"
WK_REPO="C:/caminho/do/repo"
WK_TOPIC="codebases/nome-do-repo"
```

---

## FLUXO 0 — Setup (uma vez por máquina)

| # | Quem | Comando |
|---|---|---|
| 0.1 | 👤 | `$WKPY "$WK" doctor --store "$WK_STORE" --repo "$WK_REPO" --engine claude-code` |
| 0.2 | 👤 | `$WKPY "$WK" init --engine claude-code --store "$WK_STORE" --repo "$WK_REPO"` |
| 0.3 | 👤 | `$WKPY "$WK" store init "$WK_STORE"` |
| 0.4 | 👤 | `$WKPY "$WK" doctor --store "$WK_STORE" --repo "$WK_REPO" --engine claude-code` |

Fim quando `bloqueios: []`.

---

## FLUXO 1 — Ingerir transcrição / nota / texto

| # | Quem | Comando |
|---|---|---|
| 1.1 | 👤 | `$WKPY "$WK" ingest "C:/caminho/arquivo.md" --source-type human-transcript --origin "reunião 2026-08-07" --topic pagamentos --store "$WK_STORE"` |
| 1.2 | 👤 | `$WKPY "$WK" promote --approve-all --source-type human-transcript --approved-by "seu-nome" --store "$WK_STORE"` |
| 1.3 | 👤 | `$WKPY "$WK" compile pagamentos --store "$WK_STORE"` |
| 1.4 | 👤 | `$WKPY "$WK" index reindex --store "$WK_STORE"` |
| 1.5 | 👤 | `$WKPY "$WK" lint --store "$WK_STORE"` |

Zero passos de LLM.

---

## FLUXO 2 — Ingerir .docx / .xlsx / .csv / .pdf

| # | Quem | Comando |
|---|---|---|
| 2.1 | 👤 | `$WKPY "$WK" ingest "C:/caminho/planilha.xlsx" --source-type human-doc --origin "planilha de tarifas" --topic pagamentos --store "$WK_STORE"` |
| 2.2 | 🤖 | Lê `$WK_STORE/raw/assets/<id>.xlsx` e grava `C:/caminho/analise.md` |
| 2.3 | 👤 | `$WKPY "$WK" ingest "C:/caminho/analise.md" --source-type agent-output --origin "análise de planilha.xlsx" --topic pagamentos --store "$WK_STORE"` |
| 2.4 | 👤 | `$WKPY "$WK" promote --approve-all --source-type agent-output --approved-by "seu-nome" --store "$WK_STORE"` |
| 2.5 | 👤 | `$WKPY "$WK" compile pagamentos --store "$WK_STORE"` |
| 2.6 | 👤 | `$WKPY "$WK" index reindex --store "$WK_STORE"` |

Prompt do passo 2.2:

```
Leia C:/caminho/do/store/raw/assets/<id>.xlsx
Escreva C:/caminho/analise.md em PT-BR técnico.
Toda afirmação confirmada exige arquivo:linha ou célula de origem.
Sem prosa decorativa, sem preâmbulo, sem resumo.
Devolva só: ARQUIVO: <caminho> / BYTES: <n>
```

---

## FLUXO 3 — Ingerir codebase (começo → meio → fim)

### Divisão de responsabilidade

| Papel | Quem | Onde |
|---|---|---|
| Determinístico — scripts geram as estruturas/artefatos | 👤 | `surface` `export` `config` `plan` `pending` `run-stage` `sdd-brief` `merge-agent-output` `done` `evidence` `verify` `audit` `publish` `promote` `compile` `index` `lint` `next` `state` `redo` |
| Análise de conteúdo | 🤖 | SOMENTE o passo F.4: subagentes analisam o codebase e escrevem os artefatos dos estágios `modules` `rules` `architecture` `specs` `synth` |

A LLM não executa nenhum comando `wk`. Todo insumo que ela precisa (o que analisar, onde gravar, em que formato) é gerado antes, por script, pelo humano.

Perdeu o fio: `next --quiet` diz o próximo passo (seção "Retomada e erro").

Ordem da máquina de estados: `surface → modules → rules → architecture → specs → evidence → synth → verify`.

### 3A. Preparação — 👤 sozinho

| # | Quem | Comando |
|---|---|---|
| 3.1 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" surface --topic "$WK_TOPIC"` |
| 3.2 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" export --topic "$WK_TOPIC"` |
| 3.3 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" config --doc-level detalhado --granularity module` |
| 3.4 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" plan` |

Valores válidos de 3.3: `--doc-level essencial|completo|detalhado` · `--granularity module|endpoint|use-case|hybrid|feature|custom` (obrigatório, mas hoje não altera o plano — só `doc-level` muda artefatos exigidos).

### 3B. Estágios com fan-out — mesmo ciclo para `modules` · `rules` · `architecture` · `specs` · `synth`

O humano prepara tudo (F.1–F.3), entrega **um único prompt de despacho** à LLM (F.4) e integra o resultado (F.5–F.6). A LLM não precisa entender o pipeline: só ler 2 arquivos por batch e gravar 1.

| # | Quem | Comando |
|---|---|---|
| F.1 | 👤 — só p/ `specs` | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" pending specs --items "functional,api-contract,event-contract,non-functional"` |
| F.2 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage <stage> --quiet` — gera os agent-packs, o manifesto e `agent-packs/<stage>-contract.json` |
| F.3 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" handoff <stage>` — imprime o prompt de despacho pronto (batches, caminhos e slots preenchidos) |
| F.4 | 🤖 | Humano cola a saída do F.3 na LLM. Única participação da LLM no fluxo inteiro. |
| F.5 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output <stage> --input "$WK_STORE/.codescan/<workdir>/agent-outputs/<stage>-batch-NN.txt" --agent <stage>-bNN` — repita por batch (comando exato já vem no `next_action` do F.2) |
| F.6 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done <stage>` |

Regras do ciclo:

- F.1 é o que pluraliza o fan-out: sem `pending`, `rules`/`architecture`/`synth` viram **1 batch único** (`fanout_required: 1`) — aceitável. Em `specs` isso geraria uma unit genérica `"specs"` — por isso F.1 é obrigatório lá. Em `modules` quem popula o pending é o `plan`.
- `run-stage` só prepara (packs + contrato + manifesto); nunca gera conteúdo. `merge-agent-output` recusa `--agent` genérico (`main`, `self`, …) — use o slot do batch (`modules-b01`).
- O prompt do F.3 instrui a LLM como despachante: cada subagente lê 2 arquivos (`<stage>-batch-NN.json` = itens+evidência, `<stage>-contract.json` = blocos/seções/limites), grava 1 arquivo e devolve o recibo `ARQUIVO/BLOCOS/BYTES`. A LLM não executa comando, não estuda o pipeline, não faz merge.
- Gates do F.6: fan-out real (N agentes distintos quando N>1), artefatos obrigatórios do `doc-level`, score ≥ 90, mermaid válido, proveniência sha256. Falhou → o erro traz `blockers[].action`; corrija (👤 mecânico, ou novo F.4 se for conteúdo) e repita.
- Não escreva prompt à mão: seções/limites variam por estágio e já estão no `<stage>-contract.json`; o `handoff` monta tudo.

Referência rápida do contrato (fonte de verdade: `sdd-brief <stage>`):

| Estágio | Bloco | Ids aceitos | Máx linhas/bloco |
|---|---|---|---|
| modules | `=== MODULE: <path> ===` | paths do batch | 56 |
| rules | `=== RULES: <id> ===` | `domain` · `state-machines` · `permissions` · `adrs/NNN-<slug>` | 80 |
| architecture | `=== ARCHITECTURE: <id> ===` | `architecture` · `c4-context` · `c4-containers` · `c4-components` · `erd-complete` · `traceability/spec-impact-matrix` · `sequences/<slug>` | 90 |
| specs | `=== SPEC: <unit> ===` (**SPEC singular**) | units do F.1 + `confidence-report` · `gaps` · `traceability/code-spec-matrix` · `user-stories/<slug>` · `openapi/<slug>` — com `--- requirements.md ---`, `--- design.md ---`, `--- tasks.md ---` | 70 (por arquivo) |
| synth | `=== SYNTH: <id> ===` | `confirmed` · `inferred` (não use `=== CONFIRMED: ===`) | 80 |

Globais: 8 seções por artefato · 8 bullets por seção · 0 linhas de código. Mermaid obrigatório em `architecture` (flowchart/graph), `c4-*` (flowchart/graph/C4Context/C4Container/C4Component) e `erd-complete` (erDiagram); sem diagrama, `<!-- no-diagram: <motivo> -->`.

### 3C. Estágio evidence — 👤 sozinho, sem LLM (antes de synth)

| # | Quem | Comando |
|---|---|---|
| 3.5 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" evidence --topic "$WK_TOPIC"` |
| 3.6 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done evidence` |

`evidence` não aceita `run-stage` nem fan-out.

### 3D. Fechamento — 👤 sozinho

| # | Quem | Comando |
|---|---|---|
| 3.7 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" verify --artifact sdd/confirmed.md` |
| 3.8 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" audit` |
| 3.9 | 👤 | `$WKPY "$WK" publish --workdir "$WK_STORE/.codescan/<workdir>" --topic "$WK_TOPIC" --store "$WK_STORE"` |
| 3.10 | 👤 | `$WKPY "$WK" promote --approve-all --source-type agent-output --approved-by "seu-nome" --store "$WK_STORE"` |
| 3.11 | 👤 | `$WKPY "$WK" compile "$WK_TOPIC" --store "$WK_STORE"` |
| 3.12 | 👤 | `$WKPY "$WK" index reindex --store "$WK_STORE"` |
| 3.13 | 👤 | `$WKPY "$WK" lint --store "$WK_STORE"` |
| opcional | 👤 | `$WKPY "$WK" docx "$WK_TOPIC" --store "$WK_STORE"` |

- `verify` grava sozinho o estado do estágio (`done`/`failed`) — não existe passo `done verify`.
- `audit --strict` é aceito, mas `--strict` é no-op: os gates P0 já são padrão.
- `publish` nunca bloqueia por `verify` reprovado — só anota `aviso_verify`. O bloqueio real é em `promote`/`compile`/`docx`. Para seguir mesmo assim: `--allow-unverified` (decisão 👤).

---

## FLUXO 4 — Consultar a wiki

| # | Quem | Comando |
|---|---|---|
| 4.1 | 👤 | `$WKPY "$WK" index status --store "$WK_STORE"` |
| 4.2 | 👤 | `$WKPY "$WK" index reindex --store "$WK_STORE"` |
| 4.3 | 👤 | `printf 'intent: como funciona o retry\nlex: retry dead-letter\n' \| $WKPY "$WK" index search -c wiki -n 5 --format json --store "$WK_STORE"` |
| 4.4 | 👤 | `$WKPY "$WK" index get "<docid>" --store "$WK_STORE"` |
| 4.5 | 🤖 | Transforma os `results` em resposta de prosa |
| 4.6 | 🤖 | Grava a resposta em `C:/caminho/resposta.md` |
| 4.7 | 👤 | `$WKPY "$WK" ingest "C:/caminho/resposta.md" --source-type agent-output --origin "resposta a: <pergunta>" --topic pagamentos --store "$WK_STORE"` |
| 4.8 | 👤 | `$WKPY "$WK" promote --approve --source-type agent-output --approved-by "seu-nome" --store "$WK_STORE"` |
| 4.9 | 👤 | `$WKPY "$WK" compile pagamentos --store "$WK_STORE"` |

Sem embeddings configurados: `vec:` e `hyde:` falham com `exit 2`. Só `lex:` funciona. Para forçar modo léxico: `index reindex --lex-only`.

---

## FLUXO 5 — Auditoria e integridade

| # | Quem | Comando |
|---|---|---|
| 5.1 | 👤 | `$WKPY "$WK" index status --store "$WK_STORE"` |
| 5.2 | 👤 | `$WKPY "$WK" index audit --store "$WK_STORE"` |
| 5.3 | 👤 | `$WKPY "$WK" lint --store "$WK_STORE"` |
| 5.4 | 🤖 | Corrige achados L3/L4 (julgamento de conteúdo) |
| 5.5 | 👤 | Corrige achados L1/L2/L5/W1/W2/W3 (mecânicos) |

---

## Retomada e erro

| Situação | Quem | Comando |
|---|---|---|
| Perdi o fio | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" state --quiet` |
| Qual o próximo passo | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" next --quiet` |
| Ver arquivo do repo | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" read <arquivo> --from 1 --count 80` |
| Refazer estágio/item | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" redo modules --item <item>` |
| Item impossível | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" blocked modules --item <item>` (ou `failed`, `degraded`) |
| Erro traz campo `acao` | 👤/🤖 | Execute o comando de `acao` literalmente |
| Erro de `done` | 👤/🤖 | Dica em `blockers[].action`, só quando há mais de um blocker |
| Não sei o contrato do estágio | 👤/🤖 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" sdd-brief <stage>` |
| Ler doc embutido | 👤/🤖 | `$WKPY "$WK" docs --list` · `$WKPY "$WK" docs <slug>` |

Proibido abrir `wk.pyz` com zipfile/decompilação para entender um erro.

---

## Erros comuns

| Erro | Comando de correção |
|---|---|
| `P0: artefato ausente após o merge` | use `--store` com caminho absoluto |
| `ruído rejeitado: ...` | o erro traz regra + linha + trecho; corrija só o trecho apontado |
| `Mermaid ... label nao quoted` | o erro traz linha + trecho + padrão; não remova o diagrama |
| `argument stage: invalid choice: 'evidence'` | `code evidence --topic "$WK_TOPIC"` |
| `--agent` genérico recusado | use o `agent_slot` do batch: `modules-b01` |
| `prosa fora de arquivo SPEC` / `prosa fora de bloco SPEC` | cabeçalho é `SPEC` singular, não `SPECS`; todo conteúdo dentro de bloco `=== SPEC: ... === … === END ===` |
| `prosa fora de bloco SYNTH` | blocos são `=== SYNTH: confirmed ===` e `=== SYNTH: inferred ===` — não `=== CONFIRMED: ===`/`=== INFERRED: ===` |
| `vec/hyde exigem embeddings` | use `lex:` ou defina `AZURE_OPENAI_*` |
| índice sujo bloqueia compile/lint | `index reindex --store "$WK_STORE"` |

---

## Referência — todos os comandos

**`wk <cmd>`**: `docs` · `init` · `check` · `engines` · `doctor` · `store` · `promote` · `compile` · `docx` · `lint` · `ingest` · `publish` · `code` · `index` · `search` · `get` · `audit`

**`wk code <cmd>`**: `cleanup` · `surface` · `export` · `plan` · `pending` · `config` · `next` · `done` · `blocked` · `failed` · `degraded` · `state` · `read` · `evidence` · `agent-pack` · `merge-agent-output` · `redo` · `run-stage` · `handoff` · `audit` · `sdd-brief` · `sdd-scaffold` · `verify`

**`wk index <cmd>`**: `reindex` · `search` · `get` · `audit` · `status`

Estágios com fan-out (🤖 obrigatório): `modules` · `rules` · `architecture` · `specs` · `synth`

Estágios sem fan-out (👤 sozinho): `surface` · `export` · `evidence` · `verify`
