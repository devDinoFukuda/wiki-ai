# Wiki AI — Guia de operação

Seis fluxos operacionais. Cada um é uma tabela sequencial: você executa a linha, confere o sinal de "deu certo quando", passa para a próxima. Referência técnica, guardrails e catálogo de erros ficam nas seções 7–9 — fora do caminho.

---

## 0. Antes de começar

### Legenda

| Símbolo | Quem | O que significa |
|---|---|---|
| 👤 | humano | executa o comando no terminal. Tudo que é determinístico é do humano |
| 🤖 | LLM | **colar o prompt impresso pelo `wk` numa sessão de agente COM ACESSO AO DISCO desta máquina** (ex.: Claude Code, ou a engine configurada no FLUXO 0), aberta em qualquer pasta. Nunca um chat web sem acesso a arquivos — os subagentes precisam LER packs e GRAVAR outputs em caminhos locais. Usada SOMENTE onde há análise de conteúdo: fan-out de codebase, análise de asset binário, síntese de resposta |

A LLM não executa nenhum comando `wk`. Todo insumo que ela precisa (o que analisar, onde gravar, em que formato) é gerado antes, por script, pelo humano.

### Variáveis usadas em todos os comandos

```bash
WKPY="python"
WK="C:/Users/User/projetos/wiki-ai/wk.pyz"
WK_STORE="C:/caminho/do/store"
WK_REPO="C:/caminho/do/repo"
WK_TOPIC="codebases/nome-do-repo"
```

---

## 1. FLUXO 0 — Setup (uma vez por máquina)

| P# | Quem | O que fazer | O que acontece | Deu certo quando |
|---|---|---|---|---|
| P1 | 👤 | `$WKPY "$WK" doctor --store "$WK_STORE" --repo "$WK_REPO" --engine claude-code` | diagnostica shell, python, `wk`, store, repo, engine | lista de `bloqueios` (pode vir cheia — é o retrato inicial) |
| P2 | 👤 | `$WKPY "$WK" init --engine claude-code --store "$WK_STORE" --repo "$WK_REPO"` | materializa a skill em disco e grava as permissões da engine | `.claude/settings.json` escrito com `permissions` |
| P3 | 👤 | `$WKPY "$WK" store init "$WK_STORE"` | cria `inbox/`, `raw/`, `wiki/` | estrutura do store criada |
| P4 | 👤 | `$WKPY "$WK" doctor --store "$WK_STORE" --repo "$WK_REPO" --engine claude-code` | rediagnostica | **`bloqueios: []`** |

Engines aceitas em `--engine`: `claude-code` · `antigravity` · `devin` · `copilot` · `all` (vírgula para vários). Detalhes do que o `init` escreve: §7.5.

---

## 2. FLUXO 1 — Ingerir texto (transcrição / nota)

Formatos: `.md` · `.txt` · `.vtt` · `.srt` · `.html` · `.xml` · `.xmi` · `.json` (convertidos para Markdown). **Zero passos de LLM.**

| P# | Quem | O que fazer | O que acontece | Deu certo quando |
|---|---|---|---|---|
| P1 | 👤 | `$WKPY "$WK" ingest "C:/caminho/arquivo.md" --source-type human-transcript --origin "reunião 2026-08-07" --topic pagamentos --store "$WK_STORE"` | converte para Markdown com frontmatter de proveniência em `inbox/transcripts/` | JSON com `id`, `path`, `source_type` |
| P2 | 👤 | `$WKPY "$WK" promote --approve-all --source-type human-transcript --topic pagamentos --approved-by "seu-nome" --store "$WK_STORE"` | move de `inbox/` para `raw/` com `promoted_by` | `promovidos[]` preenchido, sem `duplicados[]` |
| P3 | 👤 | `$WKPY "$WK" compile pagamentos --store "$WK_STORE"` | gera as páginas de `wiki/<topic>/` e regrava `wiki/index.md` | `paginas[]` preenchido, `recusados` ausente |
| P4 | 👤 | `$WKPY "$WK" index reindex --store "$WK_STORE"` | reindexa o store | `documents`/`changed`/`pruned`/`embedded` no JSON |
| P5 | 👤 | `$WKPY "$WK" lint --store "$WK_STORE"` | audita e escreve `wiki/_lint-report.md` | `achados: 0` (ou achados conhecidos e aceitos) |

---

## 3. FLUXO 2 — Ingerir documento binário (`.docx` / `.xlsx` / `.csv` / `.pdf`)

O binário não é convertido: é guardado imutável em `raw/assets/<id>.<ext>` e a análise vira uma fonte `agent-output` separada.

| P# | Quem | O que fazer | O que acontece | Deu certo quando |
|---|---|---|---|---|
| P1 | 👤 | `$WKPY "$WK" ingest "C:/caminho/planilha.xlsx" --source-type human-doc --origin "planilha de tarifas" --topic pagamentos --store "$WK_STORE"` | guarda o original em `raw/assets/<id>.xlsx` e cria a página de fonte no `inbox/` | JSON com `id` **e** `asset` |
| P2 | 🤖 | cole o prompt abaixo numa sessão de agente com acesso ao disco | a LLM lê o asset e grava `C:/caminho/analise.md` | recibo `ARQUIVO: <caminho> / BYTES: <n>` e o `.md` existe no disco |
| P3 | 👤 | `$WKPY "$WK" ingest "C:/caminho/analise.md" --source-type agent-output --origin "análise de planilha.xlsx" --topic pagamentos --derived-from "<id-do-asset>" --store "$WK_STORE"` | ingere a análise com linhagem de proveniência | JSON com `id` e `derived_from` |
| P4 | 👤 | `$WKPY "$WK" promote --approve-all --source-type agent-output --topic pagamentos --approved-by "seu-nome" --store "$WK_STORE"` | promove para `raw/` | `promovidos[]` preenchido, sem `bloqueio: realimentacao` |
| P5 | 👤 | `$WKPY "$WK" compile pagamentos --store "$WK_STORE"` | compila a wiki do tópico | `paginas[]` preenchido |
| P6 | 👤 | `$WKPY "$WK" index reindex --store "$WK_STORE"` | reindexa | contagens no JSON |

Prompt do P2:

```
Leia C:/caminho/do/store/raw/assets/<id>.xlsx
Escreva C:/caminho/analise.md em PT-BR técnico.
Toda afirmação confirmada exige arquivo:linha ou célula de origem.
Sem prosa decorativa, sem preâmbulo, sem resumo.
Devolva só: ARQUIVO: <caminho> / BYTES: <n>
```

`--derived-from` no P3 é opcional na CLI, mas é o que alimenta o `L4_realimentacao` do `lint` e o gate `realimentacao` do `promote` — ver §7.6.

---

## 4. FLUXO 3 — Ingerir codebase

### 4.1 Como funciona

`wk code auto` é o caminho principal. Ele encadeia sozinho TODAS as ações determinísticas — `surface` → `export` → `config` → `plan`/`pending` → prepara o fan-out (`run <stage>`) → integra (`integrate <stage>`) → `evidence` + `done evidence` — e só devolve o controle numa parada real. A única coisa que ele não pode fazer é escrever conteúdo: cinco vezes (`modules`, `rules`, `architecture`, `specs`, `synth`) ele imprime um prompt de despacho e para; você cola esse prompt numa sessão de agente com acesso ao disco, os subagentes gravam os `.txt`, e você roda `auto` de novo. As decisões humanas (tópico, nível de documentação, granularidade, unidades de `specs`, aprovar o `promote`) entram por flag ou por um comando explícito.

```mermaid
flowchart TD
    subgraph F1["FASE 1 — Preparar (👤, tudo dentro de auto)"]
        S["surface<br/>etapa 1/8"] --> E["export"] --> C["config<br/>DECISÃO: doc-level + granularity"] --> P["plan<br/>batches de modules"]
    end
    subgraph F2["FASE 2 — Fan-out ×5 (👤 prepara/integra · 🤖 escreve)"]
        M["modules 2/8"] --> R["rules 3/8"] --> A["architecture 4/8"] --> SP["specs 5/8<br/>DECISÃO: --specs-items"] --> EV["evidence 6/8<br/>sem fan-out"] --> SY["synth 7/8"]
    end
    subgraph F3["FASE 3 — Fechar (👤)"]
        FIN["finish sem --approve<br/>DECISÃO: revisar pendentes[]"] --> FA["finish --approve<br/>promote+compile+index+lint"]
    end
    P --> M
    SY --> STOP(["auto para em pipeline_completo"]) --> FIN
    LLM{{"🤖 colar prompt<br/>5x: modules · rules · architecture · specs · synth"}}
    M -.-> LLM
    R -.-> LLM
    A -.-> LLM
    SP -.-> LLM
    SY -.-> LLM

    style C fill:#ffe0b2
    style SP fill:#ffe0b2
    style FIN fill:#ffe0b2
    style LLM fill:#fff3e0
    style EV fill:#f3e5f5
```

Ordem da máquina de estados (8 etapas): `surface → modules → rules → architecture → specs → evidence → synth → verify`. `evidence` roda **entre `specs` e `synth`**, sozinho, sem fan-out. `verify` não roda dentro de `auto` — roda dentro de `wk finish`, com o artefato certo.

Paradas possíveis de `auto` (campo `parado_em` da linha JSON):

| `parado_em` | Exit | Significa |
|---|---|---|
| `decisao_humana` | 0 | falta uma decisão-chave (topic / doc-level+granularity / specs-items); a `acao` traz a flag que resolve |
| `fanout:<stage>` | 0 | o próximo passo é colar o prompt impresso na LLM despachante — único ponto 🤖 |
| `pipeline_completo` | 0 | todos os estágios SDD fechados; a `acao` já traz o `wk finish ... --approve` pronto |
| `limite_de_acoes` | 0 | teto de 30 ações numa invocação (proteção contra laço); rode `auto` de novo |
| `erro` | 2 | uma ação falhou pela 1ª vez com esta assinatura; corrija e rode `auto` de novo |
| `intervencao` | 2 | a MESMA falha se repetiu 2x seguidas na MESMA etapa — §4.4 |
| `sem_progresso` | 2 | a mesma ação saiu 0 três vezes seguidas sem mover a máquina de estados; rode `state`/`next` e investigue |

Toda saída de `auto` carrega também `executados[]` (o que ESTA invocação rodou) e `progresso` (`"etapa <i> de 8 — fase <...>"`).

Por baixo é a mesma máquina de estados e os mesmos gates de sempre: `auto` chama as MESMAS funções do CLI que `run`/`integrate`/`done` chamariam. Os compostos e os atômicos continuam disponíveis para controle fino/retomada (§7.4).

### 4.2 Passo a passo completo

Substitua `<workdir>` pelo caminho que `auto` devolve na `acao` do `pipeline_completo` (`$WK_STORE/.codescan/<repo>-<hash>`). Prefixo comum de todos os passos 👤 de `code`: `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE"`.

| P# | Quem | O que fazer (comando exato ou ação) | O que acontece | Deu certo quando |
|---|---|---|---|---|
| P1 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" auto --topic "$WK_TOPIC" --doc-level detalhado --granularity module` | roda `surface` → `export` → grava `config` → `plan` → prepara o fan-out de `modules` e imprime o prompt | `parado_em: "fanout:modules"`, `progresso: "etapa 2 de 8 ..."`, prompt impresso abaixo da linha JSON |
| P2 | 🤖 | cole o prompt de `modules` (§4.3) | N subagentes gravam `agent-outputs/modules-batch-NN.txt` | N recibos `ARQUIVO / BLOCOS / BYTES` e os N `.txt` existem |
| P3 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" auto` | integra os batches de `modules`, fecha o estágio, prepara `rules` | `executados: ["integrate modules","run rules"]`, `parado_em: "fanout:rules"` |
| P4 | 🤖 | cole o prompt de `rules` | subagentes gravam `agent-outputs/rules-batch-NN.txt` | N recibos `ARQUIVO / BLOCOS / BYTES` |
| P5 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" auto` | integra `rules`, fecha, prepara `architecture` | `parado_em: "fanout:architecture"`, `progresso: "etapa 4 de 8 ..."` |
| P6 | 🤖 | cole o prompt de `architecture` | subagentes gravam `agent-outputs/architecture-batch-NN.txt` | N recibos `ARQUIVO / BLOCOS / BYTES` |
| P7 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" auto --specs-items "a,b"` | integra `architecture`, fecha, **registra as pendências de `specs`** (a flag é exigida AQUI) e prepara o fan-out de `specs` | `executados` contém `pending specs`; `parado_em: "fanout:specs"` |
| P8 | 🤖 | cole o prompt de `specs` | subagentes gravam `agent-outputs/specs-batch-NN.txt` | N recibos `ARQUIVO / BLOCOS / BYTES` |
| P9 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" auto` | integra `specs`, fecha, roda `evidence` + `done evidence` sozinho e prepara `synth` | `executados: ["integrate specs","evidence","done evidence","run synth"]`, `parado_em: "fanout:synth"` |
| P10 | 🤖 | cole o prompt de `synth` | subagentes gravam `agent-outputs/synth-batch-NN.txt` | N recibos `ARQUIVO / BLOCOS / BYTES` |
| P11 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" auto` | integra `synth` e fecha o pipeline do `wk code` | `parado_em: "pipeline_completo"`, exit 0, `acao` = o `wk finish ... --approve` pronto |
| P12 | 👤 | `$WKPY "$WK" finish --workdir "$WK_STORE/.codescan/<workdir>" --topic "$WK_TOPIC" --repo "$WK_REPO" --store "$WK_STORE" --approved-by "seu-nome"` | `evidence` (se preciso) → `verify` → `audit` → `publish` e **PARA antes do `promote`** — nada é promovido | exit **3**, `parado_em: "promote"`, `pendentes[]` listando o que seria aprovado, `acao: "reexecute com --approve ..."` |
| P13 | 👤 | o MESMO comando do P12 **+ `--approve`** | `promote --approve-all` → `compile` → `index reindex` → `lint` → `docx` | exit **0**, `passos[]` completo com todos `status: "ok"` (`lint`/`docx` podem sair `"aviso"` sem abortar) |

**Totais: você digita ~8 comandos, cola 5 prompts, decide 3 vezes** (doc-level+granularity no P1 · unidades de specs no P7 · aprovar o promote no P12→P13).

### 4.3 O passo 🤖 — checklist de colagem

| Pergunta | Resposta |
|---|---|
| QUANDO | Toda vez que `auto` parar com `parado_em: "fanout:<stage>"` — acontece 5 vezes (`modules`, `rules`, `architecture`, `specs`, `synth`). O prompt vem impresso ABAIXO da linha JSON da saída |
| O QUE copiar | TUDO a partir da linha `Fan-out do estágio <stage>. Você é despachante:` até o fim (a linha JSON acima NÃO faz parte) |
| ONDE colar | Numa sessão de agente COM ACESSO AO DISCO desta máquina — ex.: Claude Code (ou a engine configurada no FLUXO 0), aberta em qualquer pasta. NUNCA num chat web sem acesso a arquivos: os subagentes precisam LER os packs e GRAVAR os outputs em caminhos locais |
| O QUE a LLM faz | Atua como despachante: dispara N subagentes, um por batch, com listas de itens disjuntas; cada um lê 2 arquivos (`<stage>-batch-NN.json` = itens+evidência, `<stage>-contract.json` = blocos/seções/limites), analisa SÓ os itens do seu batch e grava 1 arquivo (`agent-outputs/<stage>-batch-NN.txt`). Ela NÃO executa comandos `wk`, NÃO faz merge |
| COMO saber que terminou | A LLM devolve N recibos `ARQUIVO / BLOCOS / BYTES`; confira que os N arquivos `.txt` existem em `agent-outputs/` |
| DEPOIS | Volte ao terminal e rode `wk code auto` de novo — ele integra os batches, fecha o estágio e imprime o próximo prompt |
| SE DER ERRADO | Output rejeitado no integrate → o erro aponta regra+linha; cole um novo prompt (ou peça correção pontual à LLM) e rode `auto`; 2 falhas idênticas → `intervencao` (§4.4) |

Não escreva o prompt à mão: seções e limites variam por estágio e já estão no `<stage>-contract.json`; `auto`/`run`/`handoff` montam tudo.

### 4.4 Se algo falhar

| Situação | Sinal | O que fazer |
|---|---|---|
| Falha na 1ª vez | `parado_em: "erro"`, exit 2, `tentativas: 1` | corrija o que a `acao` do erro original manda e rode `auto` de novo — ele reexecuta sozinho a ação que falhou |
| Mesma falha 2x seguidas na mesma etapa | `parado_em: "intervencao"`, exit 2, `erro` original completo + `comandos_redo` quando existe | o laço parou de insistir. Corrija pelos `comandos_redo` do payload, ou reescrevendo o que `erro` aponta; depois rode `auto --retry` (zera o contador de tentativas e retoma) |
| Laço rodando sem andar | `parado_em: "sem_progresso"`, exit 2 | rode `state --quiet` e `next --quiet` para ver por que a ação não avança (artefato esperado não foi escrito?); corrija e rode `auto` de novo |
| Teto de ações batido | `parado_em: "limite_de_acoes"`, exit 0 | rode `auto` de novo; se o teto voltar a bater sem progresso, investigue com `state` |
| Perdi o fio / onde parei | — | `code state --quiet` · `code next --quiet` (o próximo passo) · `code next --run` (executa esse passo, se for determinístico: `run-stage`/`handoff`/`run`/`integrate`/`done`/`evidence`/`export`/`plan`) |
| Commit do repo mudou desde o `surface` | `drift_detectado: true` no `verify` | `code drift` compara o commit pinado em `surface.json` com o HEAD atual, lista `arquivos_alterados`, `artefatos_afetados` e o `redo` exato por item; refaça os afetados e rode `surface` de novo para repinar |
| Refazer 1 item | — | `code redo <stage> --item <item>` — arquiva os artefatos anteriores em `agent-runs/superseded/<run-id>/` antes de sobrescrever |
| Item impossível | — | `code blocked <stage> --item <item>` (ou `failed`, `degraded`) |
| Ver arquivo do repo | — | `code read <arquivo> --from 1 --count 80` |
| Não sei o contrato do estágio | — | `code sdd-brief <stage>` (fonte de verdade do contrato) |
| Ler doc embutido | — | `wk docs --list` · `wk docs <slug>` |
| Erro traz campo `acao` | — | execute o comando de `acao` literalmente |
| Erro de `done` | — | dica em `blockers[].action` (só quando há mais de um blocker) |

**Proibido abrir `wk.pyz` com zipfile/decompilação para entender um erro.**

---

## 5. FLUXO 4 — Consultar a wiki

| P# | Quem | O que fazer | O que acontece | Deu certo quando |
|---|---|---|---|---|
| P1 | 👤 | `$WKPY "$WK" index status --store "$WK_STORE"` | mostra o estado do índice | JSON com as contagens do índice |
| P2 | 👤 | `$WKPY "$WK" index reindex --store "$WK_STORE"` | reindexa antes de buscar | `documents`/`changed`/`pruned`/`embedded` |
| P3 | 👤 | `printf 'intent: como funciona o retry\nlex: retry dead-letter\n' \| $WKPY "$WK" index search -c wiki -n 5 --format json --store "$WK_STORE"` | busca híbrida | `results[]` com os docids |
| P4 | 👤 | `$WKPY "$WK" index get "<docid>" --store "$WK_STORE"` | recupera o documento/trecho | conteúdo do doc no stdout |
| P5 | 🤖 | transforma os `results` em resposta de prosa | — | resposta redigida |
| P6 | 🤖 | grava a resposta em `C:/caminho/resposta.md` | — | o arquivo existe no disco |
| P7 | 👤 | `$WKPY "$WK" ingest "C:/caminho/resposta.md" --source-type agent-output --origin "resposta a: <pergunta>" --topic pagamentos --derived-from "<ids-das-fontes-citadas>" --store "$WK_STORE"` | ingere a resposta com linhagem | JSON com `id` e `derived_from` |
| P8 | 👤 | `$WKPY "$WK" promote --approve "<id-da-resposta>" --approved-by "seu-nome" --store "$WK_STORE"` | aprova só esse item | `promovidos[]` com 1 item |
| P9 | 👤 | `$WKPY "$WK" compile pagamentos --store "$WK_STORE"` | recompila a wiki do tópico | `paginas[]` preenchido |

`--approve` exige o alvo (id ou caminho em `inbox/`) como VALOR da flag; a aprovação em massa é `--approve-all --source-type <t> --topic <x> --approved-by <p>` (§7.6).

Sem embeddings configurados, `vec:` e `hyde:` falham com `exit 2` (`vec/hyde exigem embeddings; índice em modo léxico`); só `lex:` funciona. Para forçar modo léxico ao reindexar: `index reindex --lex-only`.

---

## 6. FLUXO 5 — Auditoria e integridade

| P# | Quem | O que fazer | O que acontece | Deu certo quando |
|---|---|---|---|---|
| P1 | 👤 | `$WKPY "$WK" index status --store "$WK_STORE"` | estado do índice | JSON com as contagens |
| P2 | 👤 | `$WKPY "$WK" index audit --store "$WK_STORE"` | regras determinísticas `L1_canonico_so_de_agente`, `L2_proveniencia_ausente`, `L5_supersedida_ainda_citada` (+ `L4_realimentacao`) | lista de achados por regra |
| P3 | 👤 | `$WKPY "$WK" lint --store "$WK_STORE"` | audita o índice e escreve `wiki/_lint-report.md`; inclui `W1_link_quebrado`, `W2_wikilink_sem_pagina`, `W3_pagina_orfa` | `achados: 0` ou lista de achados |
| P4 | 🤖 | revisa contradição entre fontes (o que seria "L3") | julgamento de conteúdo | contradições resolvidas nas fontes |
| P5 | 👤 | corrige achados L1/L2/L4/L5/W1/W2/W3 | correções mecânicas | `lint` volta a `achados: 0` |

"L3" (contradição) **não é emitido por nenhum comando** — está deliberadamente fora do `index audit` porque exige julgamento semântico. É o único ponto 🤖 deste fluxo.

`L4_realimentacao` (dentro do `lint` e do `index audit`) é 100% mecânico: percorre a cadeia `derived_from` do frontmatter (gravada por `ingest --derived-from`) e acusa quando ela só alcança `agent-output`/página da wiki, nunca uma fonte humana ou `code-repo` — sem leitura de prosa. É o mesmo cálculo que bloqueia `promote` (§8, `bloqueio: realimentacao`); no lint ele reaparece para pegar casos que já passaram pelo `promote` antes da regra existir.

---

## 7. Referência técnica

### 7.1 Contrato entregue à LLM

`compact_contract` — fonte de verdade em runtime: `code sdd-brief <stage>`. São os limites **informados no prompt**, não os gates que decidem sucesso/falha (§7.3).

| Estágio | Bloco | Ids aceitos | Máx linhas/bloco | Seções exigidas |
|---|---|---|---|---|
| modules | `=== MODULE: <path> ===` (e `=== FAILED MODULE: ... ===`) | paths do batch | 56 | Responsabilidade · Estruturas de dados · Fluxos · Dependencias · Rastreabilidade · Lacunas |
| rules | `=== RULES: <id> ===` | `domain` · `state-machines` · `permissions` · `adrs/NNN-<slug>` | 80 | Regras · Estados · Permissoes · Contradicoes · Lacunas |
| architecture | `=== ARCHITECTURE: <id> ===` | `architecture` · `c4-context` · `c4-containers` · `c4-components` · `erd-complete` · `traceability/spec-impact-matrix` · `sequences/<slug>` | 90 | Containers · Integracoes · Decisoes · Riscos · Rastreabilidade |
| specs | `=== SPEC: <unit> ===` (**SPEC singular**) | units do `pending` + `confidence-report` · `gaps` · `traceability/code-spec-matrix` · `user-stories/<slug>` · `openapi/<slug>` | 70 (por arquivo) | Requisitos · Criterios · Design · Tarefas · Testes · Rastreabilidade · Lacunas |
| synth | `=== SYNTH: <id> ===` | `confirmed` · `inferred` (não use `=== CONFIRMED: ===`) | 80 | Confirmados · Inferidos · Perguntas |

Todo bloco fecha com `=== END ===`. Em `specs`, o corpo da unit é dividido por `--- requirements.md ---`, `--- design.md ---`, `--- tasks.md ---` (e, em `doc-level detalhado`, os opcionais `--- contracts.md ---` e `--- edge-cases.md ---`).

Globais do `compact_contract`: 8 seções por artefato · 8 bullets por seção · 0 linhas de código · sem preâmbulo, sem resumo, sem diff. Marcadores obrigatórios: 🟢 confirmado · 🟡 inferido (com justificativa) · 🔴 desconhecido (com pergunta objetiva). Mermaid obrigatório em `architecture` (`flowchart`/`graph`), `c4-*` (`flowchart`/`graph`/`C4Context`/`C4Container`/`C4Component`) e `erd-complete` (`erDiagram`); sem diagrama, `<!-- no-diagram: <motivo> -->`.

### 7.2 Regras de citação, id de bloco e entidades

**Citação (literal no prompt de despacho):** todo bullet 🟢 confirmado exige citação com caminho relativo COMPLETO a partir da raiz do repo, com `/`, exatamente como em `evidence[].path` do pack, seguido de `:linha`. Válido: `quote-service/src/main/java/br/com/acme/insurance/quote/domain/event/DomainEvent.java:5`. Inválido: `DomainEvent.java:5` (basename — reprovado no gate de `verify`/`audit`; vira `caminho_parcial` se o basename casar com exatamente 1 arquivo do repo, `arquivo_inexistente` caso contrário).

**Id de bloco `MODULE`:** o `<id>` tem que ser o path do item do batch (o mesmo que está em `pending` / no `modules-batch-NN.json`). Encurtamento por SUFIXO ÚNICO é mapeado automaticamente (ex.: `domain/event` casa com `quote-service/.../domain/event` se for o único candidato do batch); sufixo ambíguo (casa com mais de um item) ou id que não corresponde a nenhum item do batch vira erro no merge — `id de bloco fora do batch do plano`, com a lista dos ids válidos. `FAILED MODULE` segue a mesma regra.

**Estruturas de dados (bloco `modules`):** nomes de entidade/tipo vão entre crases (ex.: `` `Quote` ``) — não conta como eco de código (a proibição cobre trecho/linha, não identificador entre crases). Alimenta `sdd/data-dictionary.md`, extraído automaticamente no merge de `modules`; sem crases, a entidade pode não ser reconhecida.

### 7.3 Gates determinísticos

O que de fato aprova ou reprova. Aplicados em `integrate`/`merge-agent-output` (`done`) e em `audit`/`verify` — mais amplos que o contrato de §7.1.

| Gate | Onde | Critério |
|---|---|---|
| Fan-out real | merge | N agentes distintos quando o estágio tem mais de 1 batch (evita 1 agente fingindo ser 2) |
| Artefatos obrigatórios | merge | o `doc-level` define o mínimo (`essencial` < `completo` < `detalhado`) — travado antes do fan-out |
| Score ≥ 90 | merge / audit | aderência agregada (citações/seções/tamanho); 89 = falha |
| Mermaid válido | merge / audit | diagrama obrigatório em `architecture`/`c4-*`/`erd-complete`; sem ele, `<!-- no-diagram: <motivo> -->` |
| Proveniência sha256 | merge | sha256 do run registrado em `agent-runs/` |
| Amostra de citações contra o disco | audit | os mesmos gates do merge, mais uma amostra de `arquivo:linha` validada contra o disco real — não confia no texto sozinho |
| Cobertura por artefato | verify | `stages.verify.artifacts`; `sdd/confirmed.md` sempre exigido, `sdd/inferred.md` exigido só se existir no workdir |
| Drift | verify / drift | o commit pinado em `surface.json` (`git.head`) mudou desde a análise → a citação `arquivo:linha` não prova mais nada. Citação para arquivo alterado vira ERRO `drift_detectado`; drift que não toca arquivo citado é só warning |
| Compactação de output | merge | máx. 220 linhas por batch, ajustável por `WK_AGENT_OUTPUT_MAX_LINES` — rejeita eco de instrução / repetição de contexto |

Falhou algum gate → o erro traz `blockers[].action`/`acao`; corrija (👤 mecânico, ou cole novo prompt na LLM se for conteúdo) e rode `auto` (ou `integrate`) de novo.

### 7.4 Comandos atômicos de controle fino

Use quando precisar de granularidade que os compostos não dão (retomar 1 batch específico, inspecionar antes de integrar, repetir só 1 item).

| Comando | Substitui, dentro de | Para quê |
|---|---|---|
| `code run <stage>` | `auto` (só a preparação de 1 fan-out) | prepara packs + `<stage>-contract.json` + manifesto e imprime o prompt de despacho, sem deixar `auto` decidir sozinho quando repetir — útil para retomar depois de fechar o terminal |
| `code integrate <stage> [--partial]` | `auto` (só a integração de 1 fan-out) | integra **todos** os batches do manifesto (`merge-agent-output` de cada um, pelo `agent_slot`) + `done <stage>` de UM estágio, sem deixar `auto` avançar para o próximo; `--partial` integra só os batches cujo output já existe (sem ele, output faltando aborta antes do 1º merge) |
| `code run-stage <stage>` | `run <stage>` (só a 1ª metade) | gera packs + manifesto + `<stage>-contract.json`, sem imprimir o prompt |
| `code handoff <stage>` | `run <stage>` (só a 2ª metade) | reimprime o prompt de despacho de um `run-stage` já feito |
| `code merge-agent-output <stage> --input <arquivo> --agent <slot>` | `integrate <stage>` (1 batch por vez) | integra um batch específico, sem rodar `done` |
| `code done <stage>` | `integrate <stage>` (só o fechamento) | fecha o estágio depois de já ter mergeado todos os batches à mão |
| `code evidence --topic <t>` + `code done evidence` | `auto`/`finish` (passo evidence) | gera o evidence-pack e fecha o estágio manualmente |
| `code auto --retry` | — (dentro do próprio `auto`) | zera o contador de tentativas do loop de erro depois de corrigir uma `intervencao` |
| `code verify --artifact <path>` | `finish` (passo verify) | roda só a verificação, isolada, contra um artefato específico |
| `code audit` | `finish` (passo audit) | roda só a auditoria P0 do workdir |
| `code drift` | — | compara commit pinado × HEAD atual; lista artefatos afetados + `redo` por item |
| `code redo <stage> --item <item>` | — | refaz 1 item; arquiva os artefatos anteriores em `agent-runs/superseded/<run-id>/` antes de sobrescrever. Sem `--item`, reabre todos os itens `current` do stage |
| `code agent-pack <stage> --batch N` | `run-stage` (1 pack por vez) | gera o pacote determinístico de 1 batch, sem copiar o repo |
| `code sdd-scaffold <stage>` | — | cria o esqueleto dos artefatos SDD do estágio |
| `code cleanup` | — | apaga o clone de um repo remoto, depois do término |
| `publish --workdir ... --topic ...` | `finish` (passo publish) | só publica em `inbox/`, sem promover/compilar |
| `promote --approve-all --source-type ... --topic ... --approved-by ...` | `finish` (passo promote) | só promove, sem compilar/reindexar/lint |
| `compile "$WK_TOPIC" --store "$WK_STORE"` | `finish` (passo compile) | só recompila a wiki do tópico |
| `code next --quiet` / `code state --quiet` | — | diagnóstico: qual o próximo passo / onde parei |

- `verify` grava sozinho o estado do estágio (`done`/`failed`) — não existe passo `done verify` separado.
- `audit --strict` é aceito, mas `--strict` é no-op: os gates P0 já são padrão.
- `publish` nunca bloqueia por `verify` reprovado — só anota `aviso_verify`. O bloqueio real é em `promote`/`compile`/`docx`. Para seguir mesmo assim: `--allow-unverified` (decisão 👤).
- `merge-agent-output`/`integrate` recusam `--agent` genérico (`main`, `self`, `principal`, `orquestrador`, `orchestrator`) — use sempre o `agent_slot` do batch (`modules-b01`).
- `merge-agent-output --input` recusa arquivo com `mtime` anterior ao `agent-runs/<stage>-plan.json`: sobra de tentativa antiga não passa por resposta fresca de subagente.
- Sem `pending`, `rules`/`architecture`/`synth` viram **1 batch único** (`fanout_required: 1`) — aceitável. Em `specs` isso geraria uma unit genérica `"specs"` — por isso `pending`/`--specs-items` é obrigatório lá. Em `modules`, quem popula o pending é o `plan` (sem flag nenhuma); `plan` exclui módulos de teste por padrão (`--include-tests` os traz de volta).
- A preparação do fan-out (`auto`, ou `run <stage>`) só prepara (packs + contrato + manifesto) e imprime o prompt; **nunca gera conteúdo**.

### 7.5 Omissões — guardrails que não têm passo próprio

- **Permissão da engine é escrita, não só documentada.** `wk init --engine <e> --store <s> --repo <r>` grava, no `settings.json` da engine (`.claude/settings.json` para `claude-code`; `.agents/settings.json` para as demais), um merge idempotente em `permissions`: `additionalDirectories` com store e repo, `allow: ["Read(<store>/**)", "Read(<repo>/**)", "Bash(wk *)"]` e `deny: ["Write(<store>/**)", "Edit(<store>/**)"]` (caminhos absolutos). A sessão principal nunca escreve dentro do store à mão; quem escreve é sempre `wk` (`ingest`/`publish`/`promote`/`compile`). O enforcement real desse `deny` só é **verificado** para `claude-code`; para `antigravity`/`devin`/`copilot` é **best-effort** (`wk check`/`doctor` reportam `"formato": "best-effort"` — o schema é gravado certo, mas nada garante que a engine de fato o consuma).
- **`wk doctor` audita o próprio `.pyz`.** Compara o hash do código-fonte embutido no `.pyz` (`_build_manifest.json`) com um hash recalculado do diretório `scripts/` ao lado do arquivo, quando existe; se divergir, reporta `pyz_desatualizado: true` e `acao: "rode python scripts/build_pyz.py"`. É diagnóstico — nunca vira `bloqueio` nem afeta o exit code.
- **`publish` é idempotente por `doc_id`.** O id é determinístico (`sb-publish-<repo>-<artefato>`, não hash de conteúdo); reexecutar `publish` no mesmo workdir localiza o arquivo já em `inbox/` com esse `doc_id` e regrava (`atualizado: true`) em vez de duplicar. Vale só para o estágio `inbox/`; duplicidade em `raw/` (pós-`promote`) é outro mecanismo (`duplicados[]`, §8).
- **`export --output` não escreve em `<store>/raw` nem `<store>/wiki`.** Esses diretórios só mudam via `wk publish`/`promote`/`compile`; grave em `inbox/`.
- **`.docx` gerado por `wk docx` não pode ser reingerido** — seria proveniência falsa. A fonte é o `.md` correspondente em `raw/`.

### 7.6 Notas de comportamento

- **`promote --approve-all`** exige `--source-type` + `--topic` + `--approved-by` juntos (escopo fechado — sem os três, aprovaria o inbox inteiro). `--approve <alvo>` aprova UM item explicitamente (id ou caminho em `inbox/`) e também exige `--approved-by`. Não há aprovador default.
- **`compile`** poda página órfã de `wiki/<topic>/` por padrão (`--no-prune` desliga), sempre regrava `wiki/index.md` **global** (todas as fontes promovidas, não só as do `--topic`), e reporta `recusados[]` (id/topic com componente de caminho inválido — cada um pula sem travar o resto) e `podados[]` (as páginas removidas).
- **`ingest --derived-from <ids>`** grava a linhagem de proveniência no frontmatter (`derived_from`, separado por vírgula). Alimenta o `L4_realimentacao` do `lint` e o gate `realimentacao` do `promote` — sem ele, uma cadeia de `agent-output` que nunca cita fonte humana/`code-repo` passa despercebida até alguém tentar promovê-la.
- **`wk finish` absorve `evidence`+`done evidence`**: se `stages.evidence.status` do workdir ainda não é `done` (ex.: veio do caminho atômico, sem `auto`), ele gera o evidence-pack e fecha o estágio antes de seguir; se já é `done` (veio de `auto`), pula direto para `verify`. Idempotente entre reexecuções.
- **Sequência do `wk finish`**: `evidence`(+`done evidence`, se preciso) → `verify` (`sdd/confirmed.md`, e `sdd/inferred.md` se existir) → `audit` → `publish` — falha em qualquer um para com exit 2 e `parado_em: "<passo>"`. Depois vem o **ponto de decisão humana**: sem `--approve`, para com exit **3**, `parado_em: "promote"` e `pendentes[]`; nada é promovido. Com `--approve`: `promote --approve-all --source-type agent-output --topic <t>` → `compile` → `index reindex` → `lint` → `docx` (opcional). Falha de `lint`/`docx` não aborta — vira `"status": "aviso"` no passo. Flags: `--allow-unverified` propaga a decisão de seguir mesmo com `verify` reprovado a `promote`/`compile`/`docx` (decisão 👤, registrada no log); `--no-docx` pula o passo opcional de `wiki-docx/`.
- **`--doc-level` / `--granularity`** (`auto` e `config`): `essencial|completo|detalhado` · `module|endpoint|use-case|hybrid|feature|custom`. Ambas obrigatórias, mas hoje só `doc-level` muda os artefatos exigidos. As flags só importam na invocação em que a decisão ainda está pendente; nas seguintes, `auto` ignora quem for repassada (já gravou) e segue. O mesmo vale para `--specs-items` e `--topic`.
- **`WK_AGENT_OUTPUT_MAX_LINES`** (env var) ajusta o limite de linhas do output de subagente; padrão 220. Valor ausente, não-inteiro ou abaixo de 50 é ignorado e cai no padrão.
- **`reindex_modo`** — o reindex automático dentro de `ingest`/`promote`/`compile`/`finish` detecta sozinho as três `AZURE_OPENAI_*` (`_ENDPOINT`/`_API_KEY`/`_EMBED_DEPLOY`): todas presentes → `reindex_modo: "completo"` (com embeddings); qualquer uma ausente → `"lex-only (embeddings não configurados)"`. Não precisa passar `--lex-only` manualmente nesse caminho — só ao chamar `index reindex` direto.
- **`wk search`** aceita `lex:` / `vec:` / `hyde:` por linha; texto livre de UMA linha sem prefixo recebe `lex:` automaticamente.

---

## 8. Erros comuns

| Erro | Comando de correção |
|---|---|
| `P0: artefato ausente após o merge` | use `--store` com caminho absoluto |
| `ruído rejeitado: ...` | o erro traz regra + linha + trecho; corrija só o trecho apontado |
| `Mermaid ... label nao quoted` | o erro traz linha + trecho + padrão; não remova o diagrama |
| `argument stage: invalid choice: 'evidence'` | `code evidence --topic "$WK_TOPIC"` (não é um `CRITICAL_STAGE`) |
| `--agent` genérico recusado | use o `agent_slot` do batch: `modules-b01` |
| `id de bloco fora do batch do plano` | use o path exato do item do batch (ou um sufixo ÚNICO dele) — §7.2 |
| `prosa fora de arquivo SPEC` / `prosa fora de bloco SPEC` | cabeçalho é `SPEC` singular, não `SPECS`; todo conteúdo dentro de `=== SPEC: ... === … === END ===` |
| `prosa fora de bloco SYNTH` | blocos são `=== SYNTH: confirmed ===` e `=== SYNTH: inferred ===` — não `=== CONFIRMED: ===`/`=== INFERRED: ===` |
| `artefato já pertence a outro batch` (`integrate`/`merge-agent-output`) | outro batch já é dono desse artefato; rode `code redo <stage> --item <item-do-dono>` antes de re-mergear, ou corrija o output deste batch para não reivindicar esse artefato |
| `vec/hyde exigem embeddings; índice em modo léxico` | use `lex:` ou defina as três `AZURE_OPENAI_*` |
| `índice não encontrado: ...` (`lint`, exit 2) | store sem `index.db` — `index reindex --store "$WK_STORE"` primeiro |
| `--approve-all exige --source-type` / `--approve-all exige --topic` | passe os dois filtros: `promote --approve-all --source-type <tipo> --topic <topic> --approved-by <pessoa>` |
| `--approve/--approve-all exigem --approved-by` | informe quem assume a aprovação: `--approved-by "<pessoa>"` (vai para `promoted_by` e para o `log.md`) |
| `publish exige --topic` | `publish --workdir ... --topic "$WK_TOPIC" --store "$WK_STORE"` |
| `drift_detectado: true` (`verify`) | commit do repo mudou desde o `surface`; rode `code drift` e refaça os itens afetados |
| `bloqueio: realimentacao` (`promote`) | fonte deriva de saída de agente sem lastro humano; corrija a proveniência (`--derived-from`) ou use `--allow-feedback-loop` (decisão 👤, fica no log) |
| `duplicados: [...]` (`promote`) | id já existe em `raw/`; `promote` não sobrescreve — resolva o conflito de id antes |
| `caminho_parcial` / `arquivo_inexistente` (`verify`/`evidence`) | use o caminho relativo COMPLETO a partir da raiz do repo (o erro sugere o caminho certo quando o basename é único) |
| `parado_em: "intervencao"` (`code auto`) | a MESMA falha se repetiu 2x seguidas na mesma etapa; rode o(s) `comandos_redo` do payload ou corrija o que `erro` aponta, depois `code auto --retry` |
| `parado_em: "sem_progresso"` (`code auto`) | a ação sai 0 sem escrever o artefato esperado; rode `state`/`next`, corrija e rode `auto` de novo |
| `manifesto ausente para <stage>` (`handoff`) | rode `run-stage <stage>` antes de `handoff <stage>` |
| `input ... tem mtime anterior ao plano de fan-out` | o `.txt` é sobra de tentativa antiga; regrave o output DEPOIS do `run-stage` |
| `'<arquivo>' é um .docx gerado por wk docx` | reingira o `.md` de `raw/`, não o `.docx` derivado |

---

## 9. Todos os comandos

**`wk <cmd>`** (18): `docs` · `init` · `check` · `engines` · `doctor` · `store` · `promote` · `compile` · `docx` · `lint` · `ingest` · `publish` · `finish` · `code` · `index` · `search` · `get` · `audit`

**`wk code <cmd>`** (27): `cleanup` · `surface` · `export` · `plan` · `pending` · `config` · `next` · `done` · `blocked` · `failed` · `degraded` · `state` · `read` · `evidence` · `agent-pack` · `merge-agent-output` · `redo` · `run-stage` · `handoff` · `run` · `integrate` · `auto` · `audit` · `sdd-brief` · `sdd-scaffold` · `verify` · `drift`

**`wk index <cmd>`** (5): `reindex` · `search` · `get` · `audit` · `status`

Estágios da máquina de estados (8, nesta ordem): `surface` · `modules` · `rules` · `architecture` · `specs` · `evidence` · `synth` · `verify`

- Com fan-out (🤖 obrigatório): `modules` · `rules` · `architecture` · `specs` · `synth`
- Sem fan-out (👤 sozinho): `surface` · `evidence` · `verify` (mais `export`, `config` e `plan`, que são passos determinísticos e não estágios)
