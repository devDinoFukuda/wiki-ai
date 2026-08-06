# sdd-contract

Árvore de artefatos e templates do `ingest codebase`. Replica o Discovery do
Reversa (github.com/sandeco/reversa, MIT © sandeco). Vive em `<workdir>/sdd/`.

## 1. Árvore de saída

```
<workdir>/sdd/
├── inventory.md              # export (determinístico)      🟢 code-repo
├── dependencies.md           # export (determinístico)      🟢 code-repo
├── coupling.md               # coupling (determinístico)    🟢 code-repo (zona 🟡)
├── code-analysis.md          # estágio modules              agent-output
├── data-dictionary.md        # estágio modules              agent-output
├── domain.md                 # estágio rules                agent-output
├── state-machines.md         # estágio rules                agent-output
├── permissions.md            # estágio rules                agent-output
├── adrs/NNN-<titulo>.md      # estágio rules                agent-output
├── architecture.md           # estágio architecture         agent-output
├── c4-context.md             # estágio architecture         agent-output
├── c4-containers.md          # estágio architecture         agent-output
├── c4-components.md          # estágio architecture         agent-output
├── erd-complete.md           # estágio architecture         agent-output
├── flowcharts/<modulo>.md    # estágio modules              agent-output
├── sequences/<fluxo>.md      # estágio architecture         agent-output
├── openapi/<api>.yaml        # estágio specs (se API)       agent-output
├── user-stories/<fluxo>.md   # estágio specs                agent-output
├── specs/<unit>/             # estágio specs — por unit
│   ├── requirements.md       #                              agent-output
│   ├── design.md             #                              agent-output
│   └── tasks.md              #                              agent-output
├── traceability/
│   ├── spec-impact-matrix.md # estágio architecture         agent-output
│   └── code-spec-matrix.md   # estágio specs                agent-output
├── confidence-report.md      # revisão final                agent-output
├── gaps.md                   # revisão final                agent-output
├── confirmed.md              # estágio synth (canônico)     agent-output
└── inferred.md               # estágio synth (canônico)     agent-output
```

`<workdir>/modules/<slug>.md` fica **fora** de `sdd/` por design: é o insumo
intermediário do estágio `modules` (um arquivo por módulo, evidência bruta
gravada por `merge-agent-output modules`), não um artefato final do wiki. A
consolidação final mora em `sdd/code-analysis.md` (e, se `doc_level` ≥
completo, `sdd/data-dictionary.md` + `sdd/flowcharts/<modulo>.md`). A
divergência entre `modules/` e `sdd/` é intencional — não a "corrija" movendo
`modules/*.md` para dentro de `sdd/`.

Não existe `questions.md` no código: nenhuma `ArtifactRule`, scaffold ou bloco
de `merge-agent-output` o produz. As 🔴 (perguntas ao humano) ficam registradas
dentro de `confidence-report.md`/`gaps.md` e do próprio `confirmed.md`/
`inferred.md` — não crie um arquivo separado para elas.

`confirmed.md`/`inferred.md` são **canônicos em `sdd/`** — nunca uma cópia
solta na raiz do `<workdir>`. O `audit` compara as duas se ambas existirem e
reprova como P0 se divergirem (mantenha só `sdd/`).

Proveniência (coluna direita): todo artefato carrega o frontmatter de
`templates/source-frontmatter.yaml`, aplicado no momento do `wk publish`
(não pelo `codescan` em si — ver §6). `inventory.md`/`dependencies.md`/`coupling.md`
saem determinísticos como `code-repo` (auto-promovem; em `coupling.md`, Ce/Ca/I são
🟢 e abstração/zona são 🟡 inline). Todo o resto — `confirmed.md`/`inferred.md`
inclusos — é `agent-output` e passa pelo portão com aprovação humana, mesmo
citando `arquivo:linha`.

`confirmed.md`/`inferred.md` são síntese para ingestão, não substituem a árvore
SDD. Um pipeline que produz apenas `modules/*.md` + `confirmed.md` não cumpriu o
contrato de wiki: falta a consolidação em `sdd/code-analysis.md`, domínio,
arquitetura, specs e rastreabilidade conforme o `doc_level`.

## 1.1 Portão de qualidade obrigatório

Todo artefato manual/agent-output deve passar por contrato mínimo antes de ser
marcado como `done`:

- idioma integral em PT-BR técnico;
- escala de confiança 🟢🟡🔴 em afirmações relevantes;
- 🟢 sempre com `arquivo:linha` relativo completo;
- seções canônicas do template presentes;
- conteúdo operacional suficiente para reimplementação, não resumo executivo;
- rastreabilidade explícita entre código, regra, requisito, design e tarefa;
- lacunas preservadas como 🟡/🔴, nunca promovidas para confirmado;
- rejeição de boilerplate, placeholders e cabeçalhos em inglês.

Sinais de reprovação imediata:
- qualquer `*.py` ou script temporário em `store/.codescan/<repo>/`;
- arquivo operacional solto como `todo_matches.txt`, `check_todo.py`, `clean_todo.py` ou `parse_modules.py`;
- diretório `src/` dentro de `store/.codescan/<repo>/` contendo cópia do codebase;
- diretório vazio em `modules/**/` ou `sdd/specs/*/`;
- spec vazia, órfã, `_unit` ou não registrada no estado;
- stage `done` sem `agent-runs/<stage>.json` obrigatório;
- diagrama Mermaid vazio, ilegível, denso em uma linha ou com erro estrutural;
- `coupling.md` sem grafo Mermaid quando há dois ou mais módulos;
- `coupling.md` sem aresta quando a análise encontrou dependência interna;
- eco de log, diff, `Ran command`, `Edited`, `Write` ou `Wrote` em saída de subagente;
- `Overview`, `Responsibility`, `Requirements`, `Technical Design`,
  `Implementation Tasks`, `Dependencies` como estrutura principal;
- `TODO`, `TBD`, `<arquivo:linha>`, `<descrição>`, conteúdo genérico;
- afirmações 🟢 curtas como "classe existe", "usa serviço", "gerencia fluxo"
  sem comportamento, entrada, saída, erro ou efeito observável;
- artefato sem matriz ou seção de rastreabilidade;
- mensagens de blocker sempre citam arquivo, valor medido e valor esperado
  (ex.: "citações insuficientes em X: medido 2, esperado >= 3"), nunca só
  "insuficiente".

Lacuna conhecida (não é gate): não existe hoje verificação automática que
impeça uma pergunta 🔴 de `modules/*.md` virar afirmação 🟡 (ou 🟢) errada nos
estágios posteriores, mesmo reciclando o mesmo texto sem citação nova. Um
gate anterior tentava pegar esse caso por sobreposição lexical entre a 🟡 e a
🔴 de origem e foi removido — não detectava o caso real que o motivou (uma
🟡 com citação nova, porém factualmente errada, reciclando uma pergunta 🔴).
A responsabilidade é do subagente do estágio (não reafirmar uma 🔴 como
🟡/🟢 sem evidência nova de verdade) e do `verify` (que só garante citação
`arquivo:linha` válida, não a ausência de reciclagem semântica incorreta).

`modules/*.md` são auditados **individualmente** (não só o agregado
`code-analysis.md`): cada arquivo do glob tem sua própria checagem de bytes,
citações, seções, PT-BR e boilerplate.

## 1.2 Contrato operacional Reversa >=90%

Meta mínima: **aderência >=90% ao padrão Reversa** antes de aceitar `done` em
`modules`, `rules`, `architecture` ou `specs`. O contrato mede qualidade
operacional, não volume textual.

| Eixo | Peso | Gate |
|---|---:|---|
| Rastreabilidade código -> regra -> requisito -> design -> tarefa | 20 | todo 🟢 com `arquivo:linha`; matriz por unit |
| Profundidade operacional | 20 | entradas, saídas, erros, estado, dependências, efeitos |
| Cobertura do legado | 15 | arquivos relevantes cobertos ou listados como lacuna |
| Consistência cruzada | 15 | sem conflito entre modules/rules/architecture/specs |
| PT-BR técnico | 10 | sem headings/template em inglês |
| Lacunas classificadas | 10 | 🟡 justificado; 🔴 vira pergunta objetiva |
| Reimplementabilidade | 10 | agente sem repo consegue implementar a unit |

`done` só é válido se score final >=90, nenhum eixo crítico abaixo de 80 e nenhum
artefato obrigatório estiver `blocked`, `failed` ou `degraded`. Score 85-89 fica
`degraded`; score <85 fica `failed`.

## 1.3 Papéis especializados obrigatórios

Cada execução SDD usa papéis distintos. A engine pode executar em subagentes
reais ou sessões separadas, mas a responsabilidade e a saída de cada papel são
obrigatórias.

| Papel | Entrada | Saída obrigatória | Gate |
|---|---|---|---|
| Scout | `surface.json`, inventário, dependências | mapa de módulos, entrypoints, stack, riscos iniciais | não inventar módulo; registrar lacunas |
| Archaeologist | arquivos do batch | `modules/<slug>.md` operacional por módulo | PT-BR, 3+ citações, seções canônicas |
| Detective | modules + leitura pontual | `domain.md`, estados, permissões, regras implícitas | regra sem evidência vira 🟡/🔴 |
| Architect | modules + rules + dependências | C4, ERD, integrações, dívida técnica | integração/entidade sem origem vira gap |
| Writer | facts consolidados por unit | `requirements.md`, `design.md`, `tasks.md` | spec reimplementável e rastreável |
| Reviewer | árvore SDD completa | `confidence-report.md`, `gaps.md` | score >=90, rebaixa 🟢 frágil |

Status por papel:
- `blocked`: sem permissão, sem arquivo, decisão humana necessária.
- `failed`: saída inválida, rasa, em inglês, sem rastreabilidade ou fora do escopo.
- `degraded`: saída parcial útil, mas abaixo do gate de 90%.
- `done`: saída validada pelo pai e pelo gate aplicável.

Subagentes recebem `agent-pack` determinístico por batch, para **todo** stage
de `RUN_STAGE_ROLES` (`modules`, `rules`, `architecture`, `specs`, `synth`),
não só `modules`. Para `modules` o pack varre o repo (evidência ranqueada por
módulo, via `build_agent_pack`); para os demais estágios o pack lê o material
de entrada já gravado no workdir — nunca o repo — via `build_stage_pack`:
`rules` → `modules/*.md`; `architecture` → `modules/*.md` +
`sdd/domain.md`/`sdd/state-machines.md`/`sdd/permissions.md`; `specs` →
`modules/*.md` + `sdd/architecture.md` + `sdd/domain.md`; `synth` →
`sdd/*.md` de primeiro nível. O pacote contém trechos limitados e numerados;
não é permitido copiar a árvore do repositório para `store/.codescan`.

Nomes de bloco/documento aceitos por `merge-agent-output` (além do trio
canônico de `specs/<unit>/`):
- `rules`: `=== RULES: domain|state-machines|permissions ===` e, por prefixo,
  `=== RULES: adrs/NNN-<slug> ===` (ADR retroativo; grava
  `sdd/adrs/NNN-<slug>.md`);
- `architecture`: `=== ARCHITECTURE: architecture|c4-context|c4-containers|
  c4-components|erd-complete|traceability/spec-impact-matrix ===` e, por
  prefixo, `=== ARCHITECTURE: sequences/<slug> ===`;
- `specs`: unit no trio `--- requirements.md ---`/`--- design.md ---`/
  `--- tasks.md ---` (opcionais `--- contracts.md ---`/`--- edge-cases.md
  ---` no mesmo bloco `=== SPEC: <unit> ===`) **ou** documento nomeado —
  `=== SPEC: confidence-report|gaps|traceability/code-spec-matrix ===` e,
  por prefixo, `user-stories/<slug>` (grava `.md`) e `openapi/<slug>` (grava
  `.yaml`);
- `synth`: `=== SYNTH: confirmed|inferred ===`.

Nome fora dessa lista faz o merge rejeitar o bloco/documento inteiro.

Manifesto obrigatório (schema `wiki-ai.agent-runs.v2`):
- `agent-runs/modules.json`;
- `agent-runs/rules.json`;
- `agent-runs/architecture.json`;
- `agent-runs/specs.json`;
- `agent-runs/synth.json`.

Cada manifesto registra stage, `input` (caminho + `input_sha256`) e, por item,
os artefatos gravados com `sha256` individual. `audit` **recomputa** os hashes
a cada rodada: manifesto no schema legado `wiki-ai.agent-runs.v1`, ou input/
artefato alterado depois do merge, é blocker P0 — o manifesto não é mais
forjável escrevendo JSON à mão, porque qualquer edição posterior ao arquivo
registrado quebra o hash. `done` sem manifesto obrigatório é P0; `done
<stage> --artifact <path>` também recusa se `<path>` nunca passou por
`merge-agent-output` (não está nos artefatos do manifesto).

`agent-runs/synth.json` é exigido pelo `audit` para o stage `synth` e é
gerado pelo mesmo fluxo fechado dos demais estágios (§1.7): `run-stage synth`
monta o manifesto e o `agent-pack` do material de entrada (`sdd/*.md` de
primeiro nível); um subagente Synthesis Writer grava blocos `=== SYNTH:
confirmed|inferred ===` no `output` do batch; `merge-agent-output synth
--input <output> --agent <id>` integra em `sdd/confirmed.md`/
`sdd/inferred.md` e grava o manifesto.

## 1.4 Economia de tokens e redução de prosa

Qualidade não depende de texto longo. Todo papel deve responder em blocos
estruturados, densos e auditáveis:

- usar `agent-pack v2` com limite de 45 KB por batch;
- incluir no máximo 40 linhas por arquivo;
- incluir no máximo 4 arquivos por módulo;
- retornar no máximo 120 linhas por módulo;
- retornar no máximo 220 linhas por stage;
- status do orquestrador com no máximo 3 linhas;
- erro sumarizado com no máximo 8 linhas;
- compactação por excesso de contexto com alvo 0;
- usar `merge-agent-output`; não escrever artefato diretamente;
- rejeitar output com eco de log, diff, comando, grep completo ou artefato;
- resumir execução por contador/status/gate, não por log;
- sem resumo executivo;
- sem repetir contexto já fornecido;
- uma seção por responsabilidade real, não por decoração;
- bullets curtos com verbo + objeto + evidência;
- tabelas/matrizes para relações N:N;
- limite recomendado por módulo/unit: 900-1400 palavras, salvo módulo crítico;
- limite por seção comum: 3-8 bullets ou 1 matriz;
- cada bullet operacional deve conter comportamento, condição ou efeito;
- não explicar metodologia dentro do artefato;
- não colar trechos longos de código;
- preservar lacunas em vez de preencher com prosa especulativa.

O Reviewer rejeita saída verbosa que consuma tokens sem aumentar cobertura,
rastreabilidade, consistência ou reimplementabilidade.

Se ocorrer compactação por excesso de contexto, o stage deve ser tratado como
incidente operacional: reduzir batch por bytes, reduzir pack, descartar contexto
do batch após merge e manter somente contadores/status no chat.

## 1.5 Enforcement de ruído

Toda saída de subagente passa por gate `noise` antes do merge.

Bloqueios:
- `Ran command`;
- `Running command`;
- `Edited`;
- `Write`;
- `Wrote`;
- `diff --git`;
- `@@`;
- logs multiline;
- grep/rg completo;
- stacktrace completo;
- conteúdo de artefato recém-escrito.

Permitido:
- contador;
- status;
- arquivo impactado;
- gate aprovado/reprovado;
- erro essencial com arquivo/linha quando houver.

## 1.6 Gates Mermaid e coupling

Mermaid obrigatório:
- labels quoted;
- IDs ASCII;
- mínimo 2 nós e 1 aresta em flowchart;
- uma declaração por linha;
- sem `A[@...]`;
- sem linha densa;
- sem bloco vazio;
- sem node id duplicado.
- validação estrutural interna; parser/renderizador externo não é obrigatório
  para fechar o fluxo.
- rótulo de aresta é aceito (`A -->|"texto"| B`), desde que quoted.

Coupling obrigatório:
- gerar grafo Mermaid quando houver 2+ módulos;
- resolver dependência interna por package/class quando disponível;
- falhar se `edges=0` com imports internos detectados;
- falhar se o grafo não for render-safe.

## 1.7 Run-stage determinístico

`run-stage` é o fluxo fechado para estágios com saída de subagente:

```bash
{{WK}} code --repo <repo> run-stage modules
{{WK}} code --repo <repo> run-stage rules
{{WK}} code --repo <repo> run-stage architecture
{{WK}} code --repo <repo> run-stage specs
{{WK}} code --repo <repo> run-stage synth
```

Garantias:
- roda um probe de permissão (escreve+apaga sentinela em
  `<workdir>/agent-outputs/`) antes de emitir o manifesto; falha aí sinaliza
  que `wk init --store/--repo` não configurou a permissão da engine;
- gera `agent-pack v2`;
- exige subagentes por papel;
- aceita somente blocos parseáveis;
- contrato de recibo: o subagente GRAVA sua resposta no caminho `output` do
  batch e devolve só 3 linhas (`ARQUIVO:`/`BLOCOS:`/`BYTES:`) — nunca o
  conteúdo do artefato na mensagem;
- integra via `merge-agent-output --input <output> [--agent <id>]`;
- grava `agent-runs/<stage>.json` (schema v2, com hash por artefato — §1.3);
- roda `audit --strict`;
- bloqueia geração manual pelo orquestrador.

`synth` usa o mesmo fluxo fechado dos demais estágios: o CLI aceita `synth`
em `run-stage`/`merge-agent-output`
(`choices=(modules, rules, architecture, specs, synth)`). Fluxo: `run-stage
synth` monta manifesto + `agent-pack` do material de entrada (`sdd/*.md` de
primeiro nível) → subagente Synthesis Writer grava blocos `=== SYNTH:
confirmed|inferred ===` no `output` do batch → `merge-agent-output synth
--input <output> --agent <id>` integra em `sdd/confirmed.md`/
`sdd/inferred.md` e grava `agent-runs/synth.json`.

## 1.8 Guardrails operacionais do CLI (vigentes)

| Comando | Comportamento |
|---|---|
| `{{WK}} ingest codebase ...` | não existe como subcomando; falha com erro acionável apontando `{{WK}} code --repo <r> --store <s> surface --topic <slug>` |
| `{{WK}} doctor [--store] [--repo] [--engine]` | chamada única: shell detectado (aviso se PowerShell), python, versão do `wk`, estado do store/índice/repo, permissões da engine, `proximo_passo` — substitui varredura por `--help`; invariante: exit `0` se e somente se `bloqueios` estiver vazio; engine inválida entra em `bloqueios` e `proximo_passo` nunca a ecoa de volta, só lista as engines válidas |
| `{{WK}} code ...` | imprime o corpo completo por padrão; `--quiet` (opt-in) condensa a uma linha JSON de resumo; `--verbose` é aceito só como no-op de compatibilidade |
| `{{WK}} code ... audit [--strict]` | sem estágio elegível (nenhum artefato verificado), o resultado é `"status":"sem_evidencia"`, `"score":null`, com `stages_evaluated`/`artifacts_checked` e `"message"` acionável — nunca `"pass"`/score por ausência de evidência; `--strict` sai com código != 0 nesse caso |
| `run-stage` | emite `next_action`, `fanout_required` (nº de batches) e `agent_slot` por batch |
| `merge-agent-output --agent <id>` | obrigatório; valores genéricos (`main`, `orquestrador`, `self`, `principal`) são recusados; input com `mtime` anterior ao plano do stage é recusado; `done` bloqueia se todos os batches foram registrados pelo mesmo `--agent` |
| `{{WK}} code ...` (store) | recusa execução sem `--store` e sem `WK_STORE`; não cai mais em `./store` silenciosamente |
| `{{WK}} init`/`{{WK}} check` (permissões) | cada arquivo de settings traz `"formato"`: `verificado` só para `claude-code` (schema real, comprovadamente consumido pela engine); `best-effort` para antigravity/devin/copilot (arquivo gravado, consumo pela engine não garantido) |

E2E determinístico:

```bash
{{WK}} code --repo <repo> surface --topic <slug>
{{WK}} code --repo <repo> export --topic <slug>
{{WK}} code --repo <repo> plan
{{WK}} code --repo <repo> run-stage modules
{{WK}} code --repo <repo> run-stage rules
{{WK}} code --repo <repo> run-stage architecture
{{WK}} code --repo <repo> pending specs --items "<u1>,<u2>"
{{WK}} code --repo <repo> run-stage specs
{{WK}} code --repo <repo> run-stage synth
{{WK}} code --repo <repo> audit --strict
```

## 2. Níveis de documentação (`doc_level`)

Pergunte ao usuário ao fim do estágio `surface`, como o Reversa faz. Default:
`essencial`.

| Artefato | essencial | completo | detalhado |
|---|---|---|---|
| inventory, dependencies | sim | sim | sim |
| code-analysis.md | sim (dicionário embutido) | sim | sim |
| data-dictionary.md | não | sim | sim |
| flowcharts/ | não (fluxo em texto) | por módulo | por módulo + por função não-trivial |
| domain.md | sim (glossário + regras principais) | sim | sim |
| state-machines.md | só se entidade central tiver múltiplos status | sim | sim |
| permissions.md | só se RBAC for central | sim | sim |
| adrs/ | não | sim | sim, com "Alternativas" e "Consequências" |
| architecture.md | sim (C4 contexto embutido; ERD embutido se < 5 entidades) | sim | sim |
| c4-context.md | sim | sim | sim |
| c4-containers.md / c4-components.md | não | sim | sim |
| erd-complete.md | não | sim | sim |
| sequences/ | não | não | sim |
| specs/<unit>/ (3 canônicos) | sim | sim | sim |
| contracts.md / edge-cases.md por unit | não | contracts se a unit expõe contrato externo | ambos |
| openapi/ | só se a API for o produto principal | sim, se houver API | sim, se houver API |
| user-stories/ | não | sim | sim |
| traceability/ (2 matrizes) | não | sim | sim |
| confidence-report.md | sim (simplificado) | sim | sim |
| gaps.md | não (incorpora no confidence-report) | sim | sim, categorizado por severidade |
| confirmed.md / inferred.md (`sdd/`, canônicos) | sim | sim | sim |

Não há `questions.md`: as 🔴 ficam dentro de `confidence-report.md`/`gaps.md`
e do `confirmed.md`/`inferred.md` — nenhum artefato separado as recebe.

---

## 3. Granularidade das units (`specs/<unit>/`)

`gaps.md` em `completo`/`detalhado` deve conter `Lacunas críticas`,
`Lacunas moderadas`, `Perguntas abertas` e `Impacto`; arquivo curto ou genérico
reprova o gate.

Decida após o `surface`, com o usuário. Heurística (a primeira que dominar):

| Sinal no repo | granularidade |
|---|---|
| Pastas top-level com nomes de domínio (`auth/`, `orders/`) | `module` |
| Roteamento centralizado (`routes.*`, `urls.py`, `*Controller.*`) | `endpoint` |
| Specs Gherkin/E2E comportamentais (`features/*.feature`) | `use-case` |
| Dois ou mais sinais com peso parecido | `hybrid` (módulo no topo, casos de uso aninhados) |
| Nenhum sinal claro | `feature` (enumere features lendo o código) |

Fonte para enumerar units: `modules` do `surface.json` (para `module`/`hybrid`)
ou o que o agente identificar (demais). Registre a lista no estado:

```bash
{{WK}} code --repo <repo> pending specs --items "auth,orders,payments"
```

Nomes de pasta: minúsculos, espaços viram `-`, sem caracteres proibidos.

---

## 4. Templates por unit

Cada unit recebe os 3 canônicos. **Toda afirmação carrega marca de confiança;
🟢 exige `arquivo:linha` — o `verify` rejeita 🟢 sem citação válida.**

### 4.1 `requirements.md` — O QUE a unit faz

```markdown
# <Unit>

## Visão geral
<o que é, que problema resolve, 2–3 linhas>

## Responsabilidades
- <responsabilidade> 🟢 `arquivo:linha`

## Regras de negócio
- <regra> 🟢 `arquivo:linha`
- <regra deduzida de padrão> 🟡 (justificativa)
- <comportamento não determinável> 🔴 (pergunta objetiva em `gaps.md`/`confidence-report.md`)

## Requisitos funcionais
| ID | Requisito | Prioridade | Critério de aceite | Confiança |
|----|-----------|-----------|--------------------|-----------|
| RF-01 | ... | Must | ... | 🟢 `arquivo:linha` |

## Requisitos não funcionais
| Tipo | Requisito inferido | Evidência | Confiança |
|------|--------------------|-----------|-----------|
| Performance | ex.: timeout 30s em chamada externa | `arquivo:linha` | 🟢 |

## Critérios de aceitação (Gherkin)
Dado <pré-condição> / Quando <ação> / Então <resultado>
(inclua ao menos um cenário de erro)

## Prioridade (MoSCoW)
| Requisito | MoSCoW | Justificativa |
(inferida por frequência de chamada e posição na cadeia de dependências)

## Rastreabilidade de código
| Arquivo | Função / Classe | Cobertura |
```

### 4.2 `design.md` — COMO a unit é construída

```markdown
# <Unit> — Design técnico

## Interface
Endpoints: | Método | Caminho | Entrada | Saída | Status codes |
Símbolos:  | Símbolo | Assinatura | Retorno | Observação |

## Fluxo principal
1. <passo com referência ao arquivo legado>

## Fluxos alternativos
- **<condição/erro>:** <comportamento>

## Dependências
- <componente/serviço> — <como usa>

## Decisões de design identificadas
| Decisão | Evidência | Confiança |

## Estado interno
<campos, onde armazenados, como evoluem — se houver>

## Observabilidade
<logs, métricas, traces emitidos, com referência ao código>

## Riscos e lacunas
- 🔴 <não inferível do código>
- 🟡 <suposição que pode estar errada>
```

### 4.3 `tasks.md` — reimplementação rastreável

```markdown
# <Unit> — Tarefas de implementação

## Pré-requisitos
- [ ] dependências do design.md disponíveis
- [ ] schema/migrations compatíveis (se aplicável)

## Tarefas
- [ ] T-01 <descrição>
  - Origem no legado: `arquivo:linha`
  - Critério de pronto: <como validar>
  - Confiança: 🟢/🟡/🔴

## Tarefas de teste
- [ ] TT-01 happy path do fluxo principal
- [ ] TT-02 caso de erro principal

## Ordem sugerida
<bloqueios e o que vem primeiro>

## Lacunas pendentes (🔴)
<decisões que dependem de validação humana antes de implementar>
```

Teste: um agente sem o código original reimplementaria a unit só com estes 3
arquivos? Se não, a spec está incompleta.

Critério mínimo de aceite da spec:
- `requirements.md`: visão geral, responsabilidades, regras de negócio,
  requisitos funcionais, critérios de aceitação e rastreabilidade de código.
- `design.md`: interface, fluxo principal, fluxos alternativos, dependências,
  decisões de design, riscos/lacunas e rastreabilidade.
- `tasks.md`: pré-requisitos, tarefas implementáveis, tarefas de teste, ordem
  sugerida e lacunas pendentes.
- Cada arquivo deve explicar caminho feliz, caminho de erro, entradas, saídas,
  dependências e evidências. Se faltar, marque `degraded` ou `failed`, não `done`.

## 5. Artefatos transversais

### `architecture.md` + C4 (Mermaid)
- **c4-context**: sistema no centro, personas, sistemas externos, protocolos.
- **c4-containers**: apps, serviços, bancos, filas, caches + tecnologia de cada.
- **c4-components**: componentes internos dos containers relevantes.
- `architecture.md` inclui: mapa de integrações (APIs consumidas/produzidas,
  webhooks, eventos) e **dívida técnica** (duplicação, padrões inconsistentes,
  dependências desatualizadas críticas, módulos críticos sem teste).

### `erd-complete.md`
Todas as entidades com atributos principais, cardinalidades (1:1, 1:N, N:M),
PKs e FKs, em Mermaid `erDiagram`. Entidades vêm do estágio modules; marque a
confiança de cada relacionamento.

### `domain.md`, `state-machines.md`, `permissions.md`
Produtos do estágio rules: glossário + regras de domínio; máquinas de estado
em Mermaid `stateDiagram-v2` (valores, transições, gatilhos) para cada entidade
com campo de status; matriz papel × permissão se houver RBAC/ACL.

### `adrs/NNN-<titulo>.md`
ADRs **retroativos**: decisões já tomadas, reconstruídas do código e do
histórico git (`git log` — mensagens de fix/hotfix, refatorações, reverts
revelam decisões). Formato: Contexto / Decisão / Evidência (`arquivo:linha`
ou commit) / Status. Em `detalhado`: + Alternativas / Consequências.

### `traceability/`
- `spec-impact-matrix.md`: unit × unit — qual spec impacta qual.
- `code-spec-matrix.md`: arquivo do legado → spec que o cobre. Arquivo sem
  spec é lacuna; liste no fim.

### Revisão final: `confidence-report.md`, `gaps.md`
Passo de Reviewer, obrigatório antes de encerrar o estágio specs. Não existe
`questions.md`: as perguntas objetivas ao humano (🔴) ficam registradas dentro
destes dois artefatos e do `confirmed.md`/`inferred.md` do estágio synth.
1. Releia cada unit procurando: inconsistências internas, contradições entre
   units, lacunas críticas, e **🟢 frágil** (afirmação marcada 🟢 que é
   inferência — rebaixe para 🟡).
2. Rode `verify` em cada artefato; 🟢 sem citação válida rebaixa ou vira
   pergunta.
3. `confidence-report.md`: contagem 🟢/🟡/🔴 por artefato + lista dos rebaixados.
4. `gaps.md`: lacunas com severidade (crítico / moderado / cosmético), incluindo
   as perguntas objetivas ao humano.

---

## 6. Frontmatter e destino no corpus

`inventory.md`/`dependencies.md`/`coupling.md` já saem do `export`/`coupling`
com frontmatter próprio (`source_type: code-repo`, `confidence: reviewed`).
Os demais artefatos escritos por `merge-agent-output` (`modules/*.md`,
`sdd/domain.md`, `sdd/architecture.md`, `sdd/confirmed.md`, etc.) **não têm
frontmatter nenhum no workdir** — são conteúdo puro. Proveniência real
(`id`, `source_type`, `origin`, `captured_at`, `promoted`, `topic`) só existe
depois de `wk publish`, que gera a sua própria (substitui qualquer
frontmatter que o artefato já tivesse) — não confie em campos de frontmatter
lidos direto de dentro de `<workdir>/sdd/` ou `<workdir>/modules/`.

Ingestão é feita por `wk publish --workdir <workdir> --topic <slug>`, nunca
por `ingest` arquivo a arquivo nem por cópia manual (a permissão da engine
nega `Write`/`Edit` direto no store):

```bash
{{WK}} publish --workdir <workdir> --topic <slug> --store <store>
```

`publish` classifica sozinho: `sdd/inventory.md`, `sdd/dependencies.md` e
`sdd/coupling.md` → `code-repo` em `inbox/code-notes/`; todo o resto de
`sdd/**/*.md` e `modules/*.md` → `agent-output` em `inbox/agent-output/`.
`origin` grava `"codescan <repo> — <caminho-relativo-ao-workdir>"`;
`confidence` só é definido depois, pelo `promote`. Scripts auxiliares
temporários (`*.py`) não são artefatos SDD e não podem ir para `inbox/`;
`publish` já os exclui automaticamente, junto com `state.json` e
`surface.json`, então não há necessidade de filtrá-los manualmente.
