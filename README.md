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
| 1.2 | 👤 | `$WKPY "$WK" promote --approve-all --source-type human-transcript --topic pagamentos --approved-by "seu-nome" --store "$WK_STORE"` |
| 1.3 | 👤 | `$WKPY "$WK" compile pagamentos --store "$WK_STORE"` |
| 1.4 | 👤 | `$WKPY "$WK" index reindex --store "$WK_STORE"` |
| 1.5 | 👤 | `$WKPY "$WK" lint --store "$WK_STORE"` |

Zero passos de LLM.

`promote --approve-all` sempre exige `--source-type` + `--topic` + `--approved-by` juntos (escopo fechado — sem os três, aprovaria o inbox inteiro). `compile` poda página órfã de `wiki/<topic>/` por padrão (`--no-prune` desliga), sempre regrava `wiki/index.md` **global** (todas as fontes promovidas, não só as do `--topic`), e reporta `recusados[]` (id/topic com componente de caminho inválido — cada um pula sem travar o resto) e `podados[]` (as páginas removidas).

---

## FLUXO 2 — Ingerir .docx / .xlsx / .csv / .pdf

| # | Quem | Comando |
|---|---|---|
| 2.1 | 👤 | `$WKPY "$WK" ingest "C:/caminho/planilha.xlsx" --source-type human-doc --origin "planilha de tarifas" --topic pagamentos --store "$WK_STORE"` |
| 2.2 | 🤖 | Lê `$WK_STORE/raw/assets/<id>.xlsx` e grava `C:/caminho/analise.md` |
| 2.3 | 👤 | `$WKPY "$WK" ingest "C:/caminho/analise.md" --source-type agent-output --origin "análise de planilha.xlsx" --topic pagamentos --store "$WK_STORE"` |
| 2.4 | 👤 | `$WKPY "$WK" promote --approve-all --source-type agent-output --topic pagamentos --approved-by "seu-nome" --store "$WK_STORE"` |
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

Opcional no passo 2.3: `--derived-from <id-do-asset>` grava a linhagem de proveniência no frontmatter (`derived_from`). Alimenta o `L4_realimentacao` do `lint` e o gate `realimentacao` do `promote` (FLUXO 5) — sem ele, uma cadeia de `agent-output` que nunca cita fonte humana/`code-repo` passa despercebida até alguém tentar promovê-la.

---

## FLUXO 3 — Ingerir codebase (Preparar → Fan-out → Fechar)

### Divisão de responsabilidade

| Papel | Quem | Onde |
|---|---|---|
| Determinístico — scripts geram as estruturas/artefatos | 👤 | `surface` `export` `config` `plan` `pending` `auto` `run-stage` `handoff` `run` `sdd-brief` `merge-agent-output` `integrate` `done` `evidence` `verify` `drift` `audit` `publish` `promote` `compile` `index` `lint` `next` `state` `redo` `finish` |
| Análise de conteúdo | 🤖 | SOMENTE o passo de fan-out: subagentes analisam o codebase e escrevem os artefatos dos estágios `modules` `rules` `architecture` `specs` `synth` |

A LLM não executa nenhum comando `wk`. Todo insumo que ela precisa (o que analisar, onde gravar, em que formato) é gerado antes, por script, pelo humano.

Perdeu o fio: `next --quiet` diz o próximo passo (seção "Retomada e erro"); `next --run` chega a **executar** esse próximo passo quando ele é determinístico (mesmos passos que `auto` roda sozinho, ver abaixo).

Ordem da máquina de estados: `surface → modules → rules → architecture → specs → evidence → synth → verify`.

### Caminho feliz novo — `wk code auto` (loop até a próxima parada real)

`wk code auto` é o caminho principal: encadeia sozinho `surface`→`export`→`config`→`plan`→`pending`→ prepara o fan-out (`run <stage>`) → integra (`integrate <stage>`) → `evidence`+`done evidence`, invocação após invocação, até bater numa parada real:
- `decisao_humana` — falta uma decisão-chave (topic/doc-level/granularity/specs-items); a `acao` traz a flag que resolve.
- `fanout:<stage>` — o próximo passo é colar o prompt impresso na LLM despachante; único ponto 🤖 do fluxo inteiro.
- `pipeline_completo` — todos os estágios SDD fechados; a `acao` já traz o `wk finish ...` pronto.
- `limite_de_acoes` — teto de 30 ações numa invocação (proteção contra laço); rode `auto` de novo.
- `erro` — uma ação falhou pela 1ª vez; corrija e rode `auto` de novo.
- `intervencao` — a MESMA falha se repetiu 2x seguidas na MESMA etapa; ver "Loop de erro" abaixo.

Por baixo é a mesma máquina de estados e os mesmos gates de sempre — `auto` só decide sozinho os pontos que antes exigiam `run`/`integrate` em sequência, chamando as MESMAS funções do CLI. Os compostos (`run`, `integrate`, `finish`) e os atômicos (`run-stage`, `handoff`, `merge-agent-output`, `done`) continuam disponíveis para controle fino/retomada (subseção abaixo).

#### 1ª invocação — decisões-chave nas flags

| # | Quem | Comando |
|---|---|---|
| 1 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" auto --topic "$WK_TOPIC" --doc-level detalhado --granularity module` |
| 2 | 🤖 | Cole o prompt impresso pela invocação acima na LLM despachante |
| 3 | 👤 | `$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" auto` |

Sem estado algum, a invocação 1 roda sozinha `surface`→`export`→grava `config` (as duas flags evitam a parada `decisao_humana`)→`plan`→prepara o fan-out de `modules` e já imprime o prompt — tudo isso numa única chamada de `auto`. `--doc-level essencial|completo|detalhado` · `--granularity module|endpoint|use-case|hybrid|feature|custom` (obrigatório, mas hoje só `doc-level` muda artefatos exigidos). As duas flags só importam na invocação em que a decisão ainda está pendente; nas seguintes, `auto` ignora quem for repassada (já gravou) e segue.

A invocação 3 integra os batches do estágio, fecha (`done`), prepara o fan-out do PRÓXIMO estágio e imprime o próximo prompt.

#### Repita 2↔3 — Fan-out ×5 — `modules` · `rules` · `architecture` · `specs` · `synth`

Porquê: é o único trecho do pipeline em que conteúdo novo é escrito — por isso é o único ponto 🤖 do fluxo inteiro.

Repita "cole o prompt" → `auto` para os 5 estágios, nesta ordem. Em `specs`, a 1ª invocação de `auto` que chegar lá também precisa de `--specs-items "a,b"` (escopo de negócio, 👤 H) — sem a flag, `auto` para em `decisao_humana` pedindo exatamente ela; em `modules` quem popula o pending é o `plan` da 1ª invocação, sem flag nenhuma.

Depois de `synth` fechado, `auto` roda `evidence`+`done evidence` sozinho (sem parar) e só então para em `pipeline_completo`, com a `acao` = `wk finish ...` pronta para colar.

#### Loop de erro

Falhou 1x → `parado_em: "erro"`; corrija (a `acao` do erro original) e rode `auto` de novo — ele reexecuta sozinho a ação que falhou. A MESMA falha 2x seguidas na MESMA etapa → `parado_em: "intervencao"`: o laço para de insistir e devolve o erro original completo (com `comandos_redo` quando existe). Corrija — pelos `comandos_redo` do payload, ou reescrevendo o que `erro` aponta — e rode `wk code auto --retry` (zera o contador de tentativas) para retomar.

Regras do ciclo (ainda valem, chamadas por baixo de `auto`):

- Sem `pending`, `rules`/`architecture`/`synth` viram **1 batch único** (`fanout_required: 1`) — aceitável. Em `specs` isso geraria uma unit genérica `"specs"` — por isso `pending`/`--specs-items` é obrigatório lá.
- A preparação do fan-out (por baixo de `auto`, ou `run <stage>` no atômico) só prepara (packs + contrato + manifesto) e imprime o prompt; nunca gera conteúdo. A integração (por baixo de `auto`, ou `integrate`/`merge-agent-output` no atômico) recusa `--agent` genérico (`main`, `self`, `orquestrador`, `principal`) — usa sempre o `agent_slot` do batch (`modules-b01`).
- O prompt instrui a LLM como despachante: cada subagente lê 2 arquivos (`<stage>-batch-NN.json` = itens+evidência, `<stage>-contract.json` = blocos/seções/limites), grava 1 arquivo e devolve o recibo `ARQUIVO/BLOCOS/BYTES`. A LLM não executa comando, não estuda o pipeline, não faz merge.
- Não escreva prompt à mão: seções/limites variam por estágio e já estão no `<stage>-contract.json`; `auto`/`run`/`handoff` montam tudo.
- Saída de subagente longa demais (eco de instrução, repetição) é ruído rejeitado no merge; limite padrão 220 linhas, ajustável via `WK_AGENT_OUTPUT_MAX_LINES` (env var; valor abaixo de 50 é ignorado, cai no padrão).

Referência rápida do **contrato entregue à LLM** (`compact_contract` — fonte de verdade: `sdd-brief <stage>`; são limites informados no prompt, não os gates que decidem sucesso/falha):

| Estágio | Bloco | Ids aceitos | Máx linhas/bloco |
|---|---|---|---|
| modules | `=== MODULE: <path> ===` | paths do batch | 56 |
| rules | `=== RULES: <id> ===` | `domain` · `state-machines` · `permissions` · `adrs/NNN-<slug>` | 80 |
| architecture | `=== ARCHITECTURE: <id> ===` | `architecture` · `c4-context` · `c4-containers` · `c4-components` · `erd-complete` · `traceability/spec-impact-matrix` · `sequences/<slug>` | 90 |
| specs | `=== SPEC: <unit> ===` (**SPEC singular**) | units do `pending` + `confidence-report` · `gaps` · `traceability/code-spec-matrix` · `user-stories/<slug>` · `openapi/<slug>` — com `--- requirements.md ---`, `--- design.md ---`, `--- tasks.md ---` | 70 (por arquivo) |
| synth | `=== SYNTH: <id> ===` | `confirmed` · `inferred` (não use `=== CONFIRMED: ===`) | 80 |

Globais do `compact_contract`: 8 seções por artefato · 8 bullets por seção · 0 linhas de código. Mermaid obrigatório em `architecture` (flowchart/graph), `c4-*` (flowchart/graph/C4Context/C4Container/C4Component) e `erd-complete` (erDiagram); sem diagrama, `<!-- no-diagram: <motivo> -->`.

**Citação (regra do contrato, literal no prompt de despacho):** todo bullet 🟢 confirmado exige citação com caminho relativo COMPLETO a partir da raiz do repo, com `/`, exatamente como em `evidence[].path` do pack, seguido de `:linha`. Válido: `quote-service/src/main/java/br/com/acme/insurance/quote/domain/event/DomainEvent.java:5`. Inválido: `DomainEvent.java:5` (basename — reprovado no gate de `verify`/`audit`, vira erro `caminho_parcial` se o basename casar com exatamente 1 arquivo do repo, `arquivo_inexistente` caso contrário).

**Id de bloco `MODULE`:** `=== MODULE: <id> ===` — o `<id>` tem que ser o path do item do batch (o mesmo que está em `pending`/no `modules-batch-NN.json`). Encurtamento por SUFIXO ÚNICO é mapeado automaticamente (ex.: `domain/event` casa com `quote-service/.../domain/event` se for o único candidato do batch); sufixo ambíguo (casa com mais de um item) ou id que não corresponde a nenhum item do batch vira erro no merge — `id de bloco fora do batch do plano`, com a lista dos ids válidos do batch. `FAILED MODULE` segue a mesma regra.

**Estruturas de dados (bloco `modules`):** nomes de entidade/tipo vão entre crases (ex.: `` `Quote` ``) — não conta como eco de código (a proibição cobre trecho/linha, não identificador entre crases). Alimenta `sdd/data-dictionary.md` — extração automática no merge de `modules`; sem crases, a entidade pode não ser reconhecida.

Os **gates determinísticos** — o que de fato aprova ou reprova, aplicados em `integrate`/`merge-agent-output` (`done`) e em `audit`/`verify` — são outra coisa, e mais ampla que o contrato acima:
- No merge (`integrate`/`done`): fan-out real (N agentes distintos quando N>1), artefatos obrigatórios do `doc-level`, score ≥ 90, mermaid válido, proveniência sha256.
- No `audit`: os mesmos, mais amostra de citações `arquivo:linha` validada contra o disco real (não confia no texto sozinho).
- No `verify`: cobertura por artefato (`stages.verify.artifacts`; `sdd/confirmed.md` sempre exigido, `sdd/inferred.md` exigido só se existir no workdir) e `drift_detectado` — o commit pinado em `surface.json` (`git.head`) mudou desde a análise, então a citação `arquivo:linha` não prova mais nada. `wk code drift` compara o pinado com o HEAD atual e lista os artefatos afetados, com o comando `redo` exato por item.

Falhou algum gate → o erro traz `blockers[].action`/`acao`; corrija (👤 mecânico, ou cole novo prompt na LLM se for conteúdo) e rode `auto` (ou `integrate`) de novo.

#### Fechar (👤 sozinho)

Porquê: fecha a síntese, publica no corpus e decide promover num só comando de decisão.

`auto` já fechou `evidence`+`done evidence` sozinho, logo depois de `synth` (ver acima) — não rode os dois à mão. Só falta:

| # | Quem | Comando |
|---|---|---|
| 1 | 👤 | `$WKPY "$WK" finish --workdir "$WK_STORE/.codescan/<workdir>" --topic "$WK_TOPIC" --repo "$WK_REPO" --store "$WK_STORE" --approved-by "seu-nome"` |

`wk finish` também roda `evidence`+`done evidence` sozinho — se `stages.evidence.status` do workdir ainda não é `done` (ex.: veio do caminho atômico, sem `auto`), ele gera o evidence-pack e fecha o estágio antes de seguir; se já é `done` (veio de `auto`), pula direto para `verify`. Idempotente entre reexecuções — não precisa rodar `evidence`/`done evidence` à mão antes de `finish`.

`wk finish` substitui evidence→verify→audit→publish→promote→compile→index reindex→lint(+docx opcional) por um único comando composto:

1. `evidence`+`done evidence` (só se ainda não `done`) → `verify` (`sdd/confirmed.md`, e `sdd/inferred.md` se existir) → `audit` → `publish` — falha em qualquer um para com exit 2 e `parado_em: "<passo>"` (`"evidence"` inclusive).
2. **Ponto de decisão humana**: sem `--approve`, `finish` PARA antes do `promote` — exit **3**, `parado_em: "promote"`, lista `pendentes[]` (o que seria aprovado) e `acao: "reexecute com --approve para aprovar como <approved-by>"`. Nada é promovido nesse ponto.
3. Com `--approve` (repita o mesmo comando): `promote --approve-all` → `compile` → `index reindex` → `lint` → `docx` (opcional; `--no-docx` pula). Falha de `lint`/`docx` não aborta — vira `"status": "aviso"` no passo.

Flags: `--allow-unverified` propaga a decisão de seguir mesmo com `verify` reprovado a `promote`/`compile`/`docx` (decisão 👤, registrada no log); `--no-docx` pula o passo opcional de `wiki-docx/`.

---

### Controle fino / retomada — os atômicos por trás dos compostos

Use estes quando precisar de granularidade que os compostos não dão (retomar 1 batch específico, inspecionar antes de integrar, repetir só 1 item):

| Comando | Substitui, dentro de | Para quê |
|---|---|---|
| `code run <stage>` | `auto` (só a preparação de 1 fan-out) | prepara packs + `<stage>-contract.json` + manifesto e imprime o prompt de despacho, sem deixar `auto` decidir sozinho quando repetir — útil para retomar depois de fechar o terminal |
| `code integrate <stage> [--partial]` | `auto` (só a integração de 1 fan-out) | integra **todos** os batches do manifesto (`merge-agent-output` de cada um, pelo `agent_slot`) + `done <stage>` de UM estágio manualmente, sem deixar `auto` avançar sozinho para o próximo; `--partial` integra só os batches cujo output já existe |
| `code run-stage <stage>` | `run <stage>` (só a 1ª metade) | gera packs + manifesto + `<stage>-contract.json`, sem imprimir o prompt |
| `code handoff <stage>` | `run <stage>` (só a 2ª metade) | reimprime o prompt de despacho de um `run-stage` já feito |
| `code merge-agent-output <stage> --input <arquivo> --agent <slot>` | `integrate <stage>` (1 batch por vez) | integra um batch específico, sem rodar `done` |
| `code done <stage>` | `integrate <stage>` (só o fechamento) | fecha o estágio depois de já ter mergeado todos os batches à mão |
| `code evidence --topic <t>` + `code done evidence` | `auto`/`finish` (passo evidence) | gera o evidence-pack e fecha o estágio manualmente, sem esperar `auto` chegar lá nem rodar `finish` |
| `code auto --retry` | — (dentro do próprio `auto`) | zera o contador de tentativas do loop de erro depois de corrigir uma `intervencao` (ver "Loop de erro" acima) |
| `code verify --artifact <path>` | `finish` (passo 1) | roda só a verificação, isolada, contra um artefato específico |
| `code audit` | `finish` (passo 1) | roda só a auditoria P0 do workdir |
| `code drift` | — (novo, sem equivalente antigo) | compara commit pinado × HEAD atual; lista artefatos afetados + `redo` por item |
| `code redo <stage> --item <item>` | — | refaz 1 item; arquiva os artefatos anteriores em `agent-runs/superseded/` antes de sobrescrever |
| `publish --workdir ... --topic ...` | `finish` (passo 1) | só publica em `inbox/`, sem promover/compilar |
| `promote --approve-all --topic ... --approved-by ...` | `finish` (passo 3) | só promove, sem compilar/reindexar/lint |
| `compile "$WK_TOPIC" --store "$WK_STORE"` | `finish` (passo 3) | só recompila a wiki do tópico |
| `code next --quiet` / `code state --quiet` | — | diagnóstico: qual o próximo passo / onde parei |

- `verify` grava sozinho o estado do estágio (`done`/`failed`) — não existe passo `done verify` separado.
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

Reindex automático (dentro de `ingest`/`promote`/`compile`/`finish`, campo `reindex_modo` na saída) detecta sozinho as três `AZURE_OPENAI_*` (`_ENDPOINT`/`_API_KEY`/`_EMBED_DEPLOY`): todas presentes → `reindex_modo: "completo"` (embeddings); qualquer uma ausente → `"lex-only (embeddings não configurados)"`. Não precisa passar `--lex-only` manualmente nesse caminho — só ao chamar `index reindex` direto.

---

## FLUXO 5 — Auditoria e integridade

| # | Quem | Comando |
|---|---|---|
| 5.1 | 👤 | `$WKPY "$WK" index status --store "$WK_STORE"` |
| 5.2 | 👤 | `$WKPY "$WK" index audit --store "$WK_STORE"` |
| 5.3 | 👤 | `$WKPY "$WK" lint --store "$WK_STORE"` |
| 5.4 | 🤖 | Corrige achados L3 — contradição entre fontes (julgamento de conteúdo) |
| 5.5 | 👤 | Corrige achados L1/L2/L4/L5/W1/W2/W3 (mecânicos) |

`L4_realimentacao` (dentro de `lint`, não de `index audit`) hoje é 100% mecânico: percorre a cadeia `derived_from` do frontmatter (gravada por `ingest --derived-from`) e acusa quando ela só alcança `agent-output`/página da wiki, nunca uma fonte humana ou `code-repo` — sem leitura de prosa. É o mesmo cálculo que bloqueia `promote` (ver "Erros comuns" — `bloqueio: realimentacao`); no lint ele reaparece para pegar casos que já passaram pelo `promote` antes da regra existir.

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
| `índice não encontrado: ...` (`lint`, exit 2) | store sem `index.db` — `index reindex --store "$WK_STORE"` primeiro |
| `--approve-all exige --topic e --approved-by` | passe os dois juntos: `promote --approve-all --topic ... --approved-by ...` |
| `publish exige --topic` | `publish --workdir ... --topic "$WK_TOPIC" --store "$WK_STORE"` |
| `drift_detectado: true` (`verify`) | commit do repo mudou desde o `surface`; rode `code drift` e refaça os itens afetados |
| `bloqueio: realimentacao` (`promote`) | fonte deriva de saída de agente sem lastro humano; corrija a proveniência ou use `--allow-feedback-loop` (decisão 👤, fica no log) |
| `duplicados: [...]` (`promote`) | id já existe em `raw/`; promote não sobrescreve — resolva o conflito de id antes |
| `parado_em: "intervencao"` (`code auto`) | a MESMA falha se repetiu 2x seguidas na mesma etapa; rode o(s) `comandos_redo` do payload ou corrija o que `erro` aponta, depois `code auto --retry` |
| `caminho_parcial` (`verify`/`evidence`) | use o caminho relativo COMPLETO a partir da raiz do repo (o erro sugere o caminho certo quando o basename é único) |
| `artefato já pertence a outro batch` (`integrate`/`merge-agent-output`) | outro batch já é dono desse artefato; rode `code redo <stage> --item <item-do-dono>` antes de re-mergear, ou corrija o output deste batch para não reivindicar esse artefato |

---

## Omissões — guardrails e comportamentos que não têm passo próprio

- **Permissão da engine é escrita, não só documentada.** `wk init --engine <e> --store <s> --repo <r>` grava, no `settings.json` da engine (`.claude/settings.json` para `claude-code`; `.agents/settings.json` para as demais), `deny: ["Write(<store>/**)", "Edit(<store>/**)"]` — a sessão principal nunca escreve dentro do store à mão; quem escreve é sempre `wk` (`ingest`/`publish`/`promote`/`compile`). O enforcement real desse `deny` só é **verificado** para `claude-code`; para `antigravity`/`devin`/`copilot` é **best-effort** (`wk check`/`doctor` reportam `"formato": "best-effort"` — o schema é gravado certo, mas nada garante que a engine de fato o consuma).
- **`wk doctor` audita o próprio `.pyz`.** Ele compara o hash do código-fonte embutido no `.pyz` (`_build_manifest.json`) com um hash recalculado do diretório `scripts/` ao lado do arquivo, quando existe; se divergir, reporta `pyz_desatualizado: true` e `acao: "rode python scripts/build_pyz.py"`. É diagnóstico — nunca vira `bloqueio` nem afeta o exit code.
- **`publish` é idempotente por `doc_id`.** O id é determinístico (`sb-publish-<repo>-<artefato>`, não hash de conteúdo); reexecutar `publish` no mesmo workdir localiza o arquivo já em `inbox/` com esse `doc_id` e regrava (`refreshed: true`) em vez de duplicar. Vale só para o estágio `inbox/`; duplicidade em `raw/` (pós-`promote`) é outro mecanismo (`duplicados[]`, ver "Erros comuns").

---

## Referência — todos os comandos

**`wk <cmd>`**: `docs` · `init` · `check` · `engines` · `doctor` · `store` · `promote` · `compile` · `docx` · `lint` · `ingest` · `publish` · `code` · `index` · `search` · `get` · `audit` · `finish`

**`wk code <cmd>`**: `cleanup` · `surface` · `export` · `plan` · `pending` · `config` · `next` · `done` · `blocked` · `failed` · `degraded` · `state` · `read` · `evidence` · `agent-pack` · `merge-agent-output` · `redo` · `run-stage` · `handoff` · `run` · `integrate` · `auto` · `drift` · `audit` · `sdd-brief` · `sdd-scaffold` · `verify`

**`wk index <cmd>`**: `reindex` · `search` · `get` · `audit` · `status`

Estágios com fan-out (🤖 obrigatório): `modules` · `rules` · `architecture` · `specs` · `synth`

Estágios sem fan-out (👤 sozinho): `surface` · `export` · `evidence` · `verify`
