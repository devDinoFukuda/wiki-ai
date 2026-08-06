# ingest codebase

Uso: `ingest codebase <caminho-ou-URL> --topic <slug>`. Pipeline de 8 estágios.
`--repo` aceita caminho local OU URL (`https://`, `git@`, `.git`) — URL é clonada
fora do `store/.codescan` e reusada. Repo é READ-ONLY. Workdir:
`store/.codescan/<repo>-<hash>/`; árvore SDD em `<workdir>/sdd/` (contrato:
`sdd-contract`).

## Entrada determinística
Slash command não executa em shell e não expande variáveis exportadas no Git
Bash. Trate `$WK_REPO`, `$WK_TOPIC` e similares como texto literal.

Válido:
```text
/wiki-ai ingest codebase C:/Users/User/projetos/desafio-tech-lead/insurance-quote-service --topic codebases/insurance-quote-service
```

Inválido por design:
```text
/wiki-ai ingest codebase $WK_REPO --topic $WK_TOPIC
```

Se receber o formato inválido, não tente resolver por ambiente. Peça caminho e
topic literais, ou instrua o usuário a rodar o modo CLI no Git Bash, onde
`export WK_REPO=...` e `export WK_TOPIC=...` são válidos.

Ao fim do estágio 7, apague o clone (só remoto; mantém a análise):
```bash
{{WK}} code --repo <url> cleanup
```

## Invioláveis
- PowerShell é proibido; execute comandos somente no Git Bash. Sintoma de
  diagnóstico: saída com `No linha:` seguido de `caractere:`, ou o marcador
  `~~`, ou sugestão de `Get-ChildItem`/`Select-Object` = shell é PowerShell.
  Remediação: reexecute embrulhando em `bash -c '<comando>'`. Primeiro
  comando de qualquer sessão: `{{WK}} doctor --store <s> --repo <r> --engine <e>`.
- Use `audit --strict` antes de qualquer fechamento de estágio.
- Estágios `modules`, `rules`, `architecture`, `specs` e `synth` exigem
  subagente; o pai/orquestrador nunca gera conteúdo SDD diretamente. Use
  `run-stage` para esses estágios: o JSON de retorno traz `next_action` e
  `fanout_required` (nº de batches) por estágio, além de `agent_slot` por
  batch.
- `merge-agent-output --agent <id>` é obrigatório; valores genéricos (`main`,
  `orquestrador`, `self`, `principal`) são recusados. Input com `mtime`
  anterior ao plano do stage (`run-stage`) é recusado. `done` bloqueia se
  todos os batches do stage foram registrados pelo mesmo `--agent`.
- Nada é escrito no repo.
- Nunca copie o repo para `store/.codescan/<repo>/src`; `audit` reprova.
- Subagente lê `agent-pack` ou `read`, não a árvore completa do repo.
- Subagente GRAVA sua resposta no caminho `output` do manifesto de `run-stage`
  e devolve só o recibo de 3 linhas (`ARQUIVO:`/`BLOCOS:`/`BYTES:`); o pai
  integra o arquivo gravado via `merge-agent-output`, nunca o conteúdo do chat.
- `run-stage` roda um probe de permissão antes de emitir o manifesto; falha =
  `wk init --store/--repo` não configurou a permissão do store para esta engine.
- Cada stage com saída de subagente exige `agent-runs/<stage>.json` (schema
  `wiki-ai.agent-runs.v2`; `audit` recomputa `sha256` de input e artefatos —
  manifesto v1 ou artefato alterado após o merge é blocker P0).
- `done` sem `agent-runs` obrigatório é falha P0.
- `done <stage> --artifact <path>` recusa se `<path>` nunca passou por
  `merge-agent-output` deste stage.
- `agent-pack v2`: máximo 45 KB por batch, 40 linhas por arquivo e 4 arquivos por módulo.
  Gerado para **todo** estágio de `run-stage`/`agent-pack` (`modules`, `rules`,
  `architecture`, `specs`, `synth`), não só `modules`: para `modules` varre o
  repo; para os demais lê o material de entrada já gravado no workdir
  (`modules/*.md` e/ou `sdd/*.md`, conforme o estágio — `sdd-contract` §1.3).
- Compactação por excesso de contexto é incidente: limite esperado `0`.
- Proibido ecoar log, diff, comando, grep completo ou artefato recém-escrito.
- Proibido `python -c`, heredoc ou script ad hoc para ler JSON de surface/state.
  Use `state`, `next`, `read` e `audit`.
- 🟢 exige `arquivo:linha`; sem citação, rebaixe para 🟡.
- Checkpoint a cada item.
- Artefatos e respostas de subagente são sempre em PT-BR. Não aceite saída em inglês.
- Artefato curto, genérico, sem seções obrigatórias ou sem rastreabilidade não pode virar `done`.
- O padrão mínimo é operacional: um agente deve conseguir reimplementar a unit sem reler o código original.

| Marca | Exige |
|---|---|
| 🟢 CONFIRMADO | `arquivo:linha` |
| 🟡 INFERIDO | justificativa |
| 🔴 GAP | pergunta ao humano |

## Portão de qualidade
Antes de qualquer `done`, o pai deve rejeitar e marcar como `failed`/`degraded` quando houver:
- `*.py`, `.txt` operacional solto, `src/` ou script temporário no workdir;
- diretório vazio em `modules/**/` ou `sdd/specs/*/`;
- spec vazia, órfã ou não registrada no estado;
- Mermaid sem grafo válido, sem arestas/nós, denso ou com label inválido;
- Mermaid strict é gate estrutural interno; parser/renderizador externo não é obrigatório para fechar o fluxo;
- `coupling.md` sem grafo quando houver módulos/dependências internas;
- saída com eco de log, diff, `Ran command`, `Edited`, `Write` ou `Wrote`;
- texto majoritariamente em inglês;
- seções canônicas ausentes;
- menos de 3 citações em artefato de módulo/spec principal;
- ausência da escala 🟢🟡🔴;
- placeholder/boilerplate (`TODO`, `TBD`, `<arquivo:linha>`, `<descrição>`, conteúdo genérico);
- afirmação 🟢 que apenas diz que algo "existe", "usa" ou "gerencia" sem explicar comportamento operacional.

Meta de aderência: **>=90% ao padrão Reversa**. O pai só aceita `done` se o
artefato for rastreável, operacional, PT-BR, consistente com os demais artefatos
e suficiente para reimplementação. Entre 85-89 marque `degraded`; abaixo de 85
marque `failed`.

Papéis obrigatórios:
- `Scout`: inventaria stack, módulos, entrypoints, dependências e riscos.
- `Archaeologist`: analisa batches de módulos e produz leitura operacional.
- `Detective`: extrai regras implícitas, estados, permissões e contradições.
- `Architect`: consolida C4, ERD, integrações, dívida técnica e impactos.
- `Writer`: gera specs por unit com requirements/design/tasks.
- `Reviewer`: valida semântica, score >=90, gaps, perguntas e rebaixa 🟢 frágil.

Regras de token/prosa:
- `agent-pack v2`: máximo 45 KB por batch, 40 linhas por arquivo e 4 arquivos por módulo;
- status do orquestrador: máximo 3 linhas; erro sumarizado: máximo 8 linhas;
- compactação por excesso de contexto: alvo 0; se ocorrer, registrar incidente e reduzir pack/batch;
- saída de subagente rejeitada se ecoar log, diff, comando, grep ou artefato;
- resultado de comando deve virar contador/status, nunca log no chat;
- sem resumo executivo;
- não repetir contexto de entrada;
- usar blocos estruturados, bullets densos e matrizes;
- limitar cada seção comum a 3-8 bullets ou 1 tabela;
- preferir evidência e decisão operacional a explicação metodológica;
- reduzir output sem reduzir rastreabilidade, cobertura ou reimplementabilidade.

Inspeção operacional:
```bash
{{WK}} code --repo <repo> state
{{WK}} code --repo <repo> next
{{WK}} code --repo <repo> read <arquivo> --from <linha> --count <n>
{{WK}} code --repo <repo> audit --strict
```

Qualidade esperada:
- **Modules:** responsabilidade, estruturas de dados/entidades, fluxos, dependências, funções, riscos, lacunas e rastreabilidade por arquivo.
- **Rules:** regras de negócio, máquinas de estado, permissões, contradições, lacunas e perguntas humanas.
- **Architecture:** contexto, containers, integrações, decisões, riscos, dívida técnica e rastreabilidade.
- **Specs:** requisitos, design e tarefas com critérios de aceite, caminhos de erro, ordem de implementação e testes.

## Brief compacto para subagentes
Use este contrato em qualquer fan-out. Ele reduz tokens sem reduzir evidência.
`merge-agent-output` (`scripts/codescan/agentmerge.py`) só aceita EXATAMENTE
estes formatos de bloco — qualquer prosa fora de um bloco, bloco sem `=== END
===`, ou nome de artefato não previsto faz o merge inteiro falhar.

Regras:
- Responda só em blocos parseáveis: `=== <TIPO>: <id-ou-nome> ===` ... `=== END ===`.
- Não ecoe código, diff, log, prompt, lista global de arquivos ou contexto já dado.
- Não repita contexto.
- Cada bloco cobre somente o item atribuído.
- Sem resumo, preâmbulo, conclusão, explicação de método ou nota final.
- Use PT-BR técnico, bullets rastreáveis e tabelas densas quando comparar.
- Limite por item: 8 seções, 8 bullets por seção, 0 linhas de quote de código.
- 🟢 exige `arquivo:linha`; 🟡 exige justificativa; 🔴 exige pergunta objetiva.
- Se não cumprir, retorne `FAILED <TIPO>` com motivo objetivo; não sintetize fallback.

`<id-ou-nome>` depende do estágio:
- `MODULE` (stage `modules`): `<id>` é o caminho do módulo (`path` do `surface.json`).
- `SPEC` (stage `specs`): `<id>` é o nome da unit (`sdd/specs/<unit>/`).
- `RULES` (stage `rules`): `<id>` é um destes nomes fixos — `domain`,
  `state-machines`, `permissions` — ou, por prefixo, `adrs/NNN-<slug>` (ADR
  retroativo; grava `sdd/adrs/NNN-<slug>.md`). Nome fora dessa lista faz o
  merge rejeitar o bloco inteiro.
- `ARCHITECTURE` (stage `architecture`): `<id>` fixo — `architecture`,
  `c4-context`, `c4-containers`, `c4-components`, `erd-complete` ou
  `traceability/spec-impact-matrix` — ou, por prefixo, `sequences/<slug>`.
- `SYNTH` (stage `synth`): `<id>` fixo — `confirmed` ou `inferred`.
- `SPEC` (stage `specs`) também aceita documento nomeado no lugar do trio de
  unit: `confidence-report`, `gaps`, `traceability/code-spec-matrix` e, por
  prefixo, `user-stories/<slug>` (grava `.md`) ou `openapi/<slug>` (grava
  `.yaml`).

Formato mínimo (MODULE/SPEC — bloco por item, uma seção Markdown por
responsabilidade):
```text
=== MODULE: <path> ===
## <Seção>
- 🟢 fato operacional. `path/file.ext:linha`
- 🟡 inferência curta; base: <evidência ou lacuna>
- 🔴 pergunta objetiva para humano
=== END ===
```

Formato SPEC (uma unit, os 3 arquivos marcados por `--- <arquivo> ---`;
qualquer um dos três ausente ou vazio faz o merge rejeitar a unit inteira;
`contracts.md`/`edge-cases.md` são opcionais no mesmo bloco):
```text
=== SPEC: <unit> ===
--- requirements.md ---
<conteúdo de requirements.md, templates §4.1>
--- design.md ---
<conteúdo de design.md, templates §4.2>
--- tasks.md ---
<conteúdo de tasks.md, templates §4.3>
=== END ===
```

Formato SPEC para documento nomeado (revisão final/artefatos globais, um
bloco por documento, sem trio de arquivos):
```text
=== SPEC: confidence-report ===
<conteúdo integral de sdd/confidence-report.md>
=== END ===
=== SPEC: openapi/<api> ===
<conteúdo integral de sdd/openapi/<api>.yaml>
=== END ===
```

Formato RULES/ARCHITECTURE (um bloco por artefato, nome exatamente igual ao da
lista acima, um bloco por artefato entregue no batch; `adrs/`/`sequences/`
usam o mesmo formato por prefixo):
```text
=== RULES: domain ===
<conteúdo integral de sdd/domain.md>
=== END ===
=== RULES: adrs/001-<slug> ===
<conteúdo integral de sdd/adrs/001-<slug>.md>
=== END ===
```

Formato SYNTH (um bloco por artefato canônico do estágio synth):
```text
=== SYNTH: confirmed ===
<conteúdo integral de sdd/confirmed.md>
=== END ===
=== SYNTH: inferred ===
<conteúdo integral de sdd/inferred.md>
=== END ===
```

## Estágio 1 — Surface
```bash
{{WK}} code --repo <repo> surface --topic <slug>
{{WK}} code --repo <repo> export --topic <slug>
```
`export` gera 3 artefatos `code-repo`: `sdd/inventory.md`, `sdd/dependencies.md`
e `sdd/coupling.md` (zonas de design — Ce/Ca/I 🟢, abstração/zona 🟡). Default
de saída é `<workdir>/sdd`; `--output <dir>` recusa qualquer destino dentro de
`raw/` ou `wiki/` do store (esses só mudam via `publish`/`promote`).
Ler os `warnings`. Decidir com o usuário e registrar:
```bash
{{WK}} code --repo <repo> config --doc-level <essencial|completo|detalhado> --granularity <module|endpoint|use-case|hybrid|feature>
```
Tabelas em `sdd-contract` §2 (doc_level) e §3 (granularity). Retomada: `state`
mostra as decisões em `sdd` — não pergunte de novo.
`config` é obrigatório antes de `plan` e `run-stage modules`; se faltar
`sdd.doc_level` ou `sdd.granularity`, use `next` e rode a ação indicada.

## Estágio 2 — Modules (por subagentes)
SEMPRE paralelize por subagentes. Não cave em série.

1. Particionar:
```bash
{{WK}} code --repo <repo> plan
```
`plan` decide o nº de subagentes (`min(8, nº módulos, ceil(LOC/2000))`). Teste
fora do alvo (`--include-tests` inclui). Cada `batches[].modulos` → um subagente.

2. Fan-out — um subagente por grupo, listas disjuntas. Spawn: Claude Code = Task ·
Devin = subagentes · Antigravity = `start_subagent` · Copilot = sessões de subagente.

Gerar pacote determinístico por batch:
```bash
{{WK}} code --repo <repo> agent-pack modules --batch <n>
```

Fluxo fechado:
```bash
{{WK}} code --repo <repo> run-stage modules
```

`run-stage modules` primeiro roda um probe de permissão (escreve+apaga um
sentinela em `<workdir>/agent-outputs/`; falha aí = `wk init --store/--repo`
não foi rodado — resolva a permissão antes de continuar), depois gera packs,
exige subagentes, aceita somente blocos parseáveis, integra por
`merge-agent-output`, grava `agent-runs/modules.json` (schema
`wiki-ai.agent-runs.v2`, com `sha256` por artefato e `input_sha256` do input
mesclado) e roda `audit --strict`. O JSON de `run-stage` traz, por batch,
`output` (caminho onde o subagente GRAVA a resposta) e `merge_command` pronto.

> Subagente é SOMENTE LEITURA quanto ao repositório em todas as engines. Não
> roda `wk.pyz`. Ele GRAVA a própria resposta no caminho `output` do batch
> (`<workdir>/agent-outputs/modules-batch-NN.txt`) e devolve ao pai só o
> recibo de 3 linhas — `ARQUIVO:` / `BLOCOS:` / `BYTES:` — nunca o conteúdo do
> bloco, log, diff ou eco de comando na mensagem de chat.
> Entrada do subagente: `agent-packs/modules-batch-NN.json`; não use `Copy-Item` do repo.
> Papel do subagente neste estágio: `Archaeologist`. O pai atua como orquestrador
> e valida com gate >=90 antes de gravar/fechar item.

Instrução ao subagente:
> Analise SOMENTE os módulos do `agent-pack`. Responda integralmente em PT-BR.
> GRAVE a resposta em `<output do batch>` (não a devolva na mensagem); não
> rode `wk.pyz`, não copie arquivos do repo.
> Se não conseguir ler os arquivos de um módulo, não sintetize fallback: use
> `=== FAILED MODULE: <path> ===` com o motivo objetivo. Caso contrário, escreva
> no arquivo `output` um bloco por módulo:
> ```
> === MODULE: <path> ===
> <modules/<slug>.md: responsabilidade, estruturas de dados, fluxos, dependências,
> funções, entidades — cada afirmação marcada, 🟢 exige arquivo:linha>
> === END ===
> ```
> Nada fora da sua lista. Não use cabeçalhos em inglês como Responsibility,
> Data Structures/Entities ou Dependencies. Não entregue resumo executivo:
> entregue leitura operacional com fluxos, entradas, saídas, erros, dependências,
> estado, entidades, invariantes, riscos e lacunas. Cada módulo precisa ter
> seções `Responsabilidade`, `Estruturas de dados`, `Fluxos`, `Dependências` e
> `Rastreabilidade`. Ao terminar de gravar, responda só com o recibo de 3 linhas.

Pai: depois do recibo, roda
`{{WK}} code --repo <repo> merge-agent-output modules --input <output-do-batch> --agent <id-do-subagente>`
(`--agent` é obrigatório; identifica quem gerou o input no manifesto — valor
genérico como `main`/`orquestrador`/`self`/`principal` é recusado). O merge lê
o arquivo, grava `<workdir>/modules/<slug>.md` por bloco e marca os itens
`done` sozinho — não precisa rodar `done modules --item <path>` à parte.
Subagente morto → módulos seguem pendentes (`next`/`state`). Subagente que
falha sem contrato mínimo bloqueia o item: use `failed modules --item <path>` ou
`blocked modules --item <path>`. Se o pai fizer fallback manual incompleto, marque
`degraded modules --item <path>`; fallback não é `done`. `done modules
--artifact <path>` recusa se `<path>` nunca passou por `merge-agent-output`
deste stage.

3. Consolidar (após todos) antes de fechar o estágio:
- sempre: `sdd/code-analysis.md`
- se `doc_level` ≥ completo: `sdd/data-dictionary.md` e `sdd/flowcharts/<modulo>.md`

Só então rode:
```bash
{{WK}} code --repo <repo> done modules
```
O estágio `modules` não está concluído apenas porque todos os `modules/*.md`
existem; esses arquivos são insumo intermediário, não wiki final.

## Estágio 3 — Rules
Papel: `Detective`. Lê `modules/*.md`, não o repo (só `read` pontual para citar). Extrair: regras de
negócio implícitas, máquinas de estado, permissões, ADRs retroativos (git log:
fix/hotfix, reverts), lógica circular/morta, contradições.
Artefatos (§5): `sdd/domain.md`; se `doc_level` ≥ completo, também
`sdd/state-machines.md`, `sdd/permissions.md` e `sdd/adrs/`.
Subagente entrega um bloco por artefato, nome fixo (`=== RULES: domain ===`,
`=== RULES: state-machines ===`, `=== RULES: permissions ===` — ver "Brief
compacto" acima); nome fora dessa lista faz o merge rejeitar o bloco. Depois
rode:
```bash
{{WK}} code --repo <repo> run-stage rules
{{WK}} code --repo <repo> merge-agent-output rules --input <output-do-batch> --agent <id-do-subagente>
{{WK}} code --repo <repo> done rules
```
`run-stage rules` é obrigatório quando houver geração por subagente; grava
`agent-runs/rules.json` (schema `wiki-ai.agent-runs.v2`).

## Estágio 4 — Architecture
Papel: `Architect`. Lê `modules/*.md` + artefatos do estágio 3, não o repo. Produz por `doc_level`:
- `sdd/architecture.md`
- `sdd/c4-context.md` (+ `c4-containers`, `c4-components` se ≥ completo)
- `sdd/erd-complete.md` (≥ completo)
- `sdd/traceability/spec-impact-matrix.md` (≥ completo)
- `sdd/sequences/` (detalhado)
Subagente entrega um bloco por artefato, nome fixo (`=== ARCHITECTURE:
architecture ===`, `c4-context`, `c4-containers`, `c4-components`,
`erd-complete` — ver "Brief compacto" acima).
```bash
{{WK}} code --repo <repo> run-stage architecture
{{WK}} code --repo <repo> merge-agent-output architecture --input <output-do-batch> --agent <id-do-subagente>
{{WK}} code --repo <repo> done architecture
```
`run-stage architecture` é obrigatório quando houver geração por subagente; grava
`agent-runs/architecture.json` (schema `wiki-ai.agent-runs.v2`).
`done architecture` exige `sdd/architecture.md` e `sdd/c4-context.md`; se
`doc_level` ≥ completo, exige também `sdd/c4-containers.md`,
`sdd/c4-components.md`, `sdd/erd-complete.md` e
`sdd/traceability/spec-impact-matrix.md`.

## Estágio 5 — Specs (por subagentes)
```bash
{{WK}} code --repo <repo> pending specs --items "<u1>,<u2>,..."
```
Papel de geração: `Writer`. Fan-out, mesmo modelo read-only do estágio 2:
subagente responde em PT-BR, monta `requirements.md`, `design.md`, `tasks.md`
(templates §4) e RETORNA em um bloco `=== SPEC: <unit> ===` com os três
arquivos marcados por `--- requirements.md ---` / `--- design.md ---` /
`--- tasks.md ---` dentro do mesmo bloco (ver "Brief compacto" acima) — falta
de qualquer um dos três faz o merge rejeitar a unit inteira. Pai grava em
`sdd/specs/<unit>/` via
`{{WK}} code --repo <repo> merge-agent-output specs --input <output-do-batch> --agent <id-do-subagente>`,
que já marca a unit `done` sozinho — não precisa rodar `done specs --item
<unit>` à parte.
Specs genéricas, em inglês, sem critérios de aceite, sem caminho de erro ou sem
matriz arquivo → requisito devem ser rejeitadas. A unit só vira `done` quando os
3 arquivos canônicos têm profundidade suficiente e rastreabilidade.
Falha de subagente bloqueia a unit (`failed specs --item <unit>` ou
`blocked specs --item <unit>`). Fallback parcial deve virar
`degraded specs --item <unit>`, não `done`.
Globais (≥ completo): `sdd/traceability/code-spec-matrix.md`, `sdd/openapi/` (se API), `sdd/user-stories/`.
Papel de revisão: `Reviewer`. Revisão final (§5): reclassificar 🟢 frágil, rodar
`verify`, gerar `sdd/confidence-report.md` e, se `doc_level` ≥ completo,
`sdd/gaps.md`. Não há `questions.md` no contrato do código — as 🔴 ficam
registradas dentro de `confidence-report.md`/`gaps.md` e do `sdd/confirmed.md`/
`sdd/inferred.md` do estágio synth.
Para fechar:
```bash
{{WK}} code --repo <repo> run-stage specs
{{WK}} code --repo <repo> done specs
```
`run-stage specs` é obrigatório para geração das units; grava
`agent-runs/specs.json` (schema `wiki-ai.agent-runs.v2`).
`done specs` exige todas as units concluídas, os 3 arquivos por unit e
`sdd/confidence-report.md`; se `doc_level` ≥ completo, exige também
`sdd/gaps.md` e `sdd/traceability/code-spec-matrix.md`.

## Estágio 6 — Evidence
```bash
{{WK}} code --repo <repo> evidence --topic <slug>
```
Pacote em `<workdir>/evidence-<slug>.json`. Vazio → não sintetize; refine o tópico ou cave à mão.

## Estágio 7 — Synth
Papel: `Synthesis Writer`. Lê `sdd/*.md` de primeiro nível (não o repo).
Local **canônico**: `sdd/confirmed.md` e `sdd/inferred.md` (nunca uma cópia na
raiz do workdir — `audit` reprova como P0 se as duas versões divergirem; se
existir cópia na raiz, apague-a e mantenha só `sdd/`). Não há `questions.md`
no contrato do código.

`sdd/confirmed.md` → só 🟢 com `arquivo:linha`; ao publicar/promover, mesmo
sendo o artefato mais verificável da síntese, entra como `agent-output` (só
`inventory.md`/`dependencies.md`/`coupling.md` do `export` são `code-repo`).
`sdd/inferred.md` → 🟡, `agent-output`, nunca auto-promove.

O CLI aceita `synth` em `run-stage`/`merge-agent-output`
(`choices=(modules, rules, architecture, specs, synth)`). Subagente entrega
um bloco por artefato, nome fixo (`=== SYNTH: confirmed ===`, `=== SYNTH:
inferred ===` — ver "Brief compacto" acima); nome fora dessa lista faz o
merge rejeitar o bloco. Fluxo fechado, igual aos demais estágios:
```bash
{{WK}} code --repo <repo> run-stage synth
{{WK}} code --repo <repo> merge-agent-output synth --input <output-do-batch> --agent <id-do-subagente>
{{WK}} code --repo <repo> done synth
```
`run-stage synth` grava `agent-runs/synth.json` (schema
`wiki-ai.agent-runs.v2`), exigido pelo `audit` para este estágio.

`synth` só pode fechar depois de `modules`, `rules`, `architecture` e `specs`
estarem concluídos com seus artefatos SDD. Não substitua a árvore SDD por um
`confirmed.md` grande; isso perde estrutura de wiki.

Validar antes de publicar/promover:
```bash
{{WK}} code --repo <repo> verify --artifact <workdir>/sdd/confirmed.md
```
Falha → rebaixa para `inferred.md` ou vira pergunta.

Depois, leve a árvore inteira para `inbox/` com `publish` (§6 do
`sdd-contract`) — não copie arquivo por arquivo com `ingest`:
```bash
{{WK}} publish --workdir <workdir> --topic <slug> --store <store>
```
`publish` classifica sozinho: `sdd/inventory.md`, `sdd/dependencies.md` e
`sdd/coupling.md` como `code-repo` em `inbox/code-notes/`; todo o resto de
`sdd/**/*.md` e `modules/*.md` (síntese incluída) como `agent-output` em
`inbox/agent-output/`. Scripts auxiliares (`*.py`), `state.json` e
`surface.json` nunca entram — o comando já os exclui.
Depois: `promote`, `compile`. Repo remoto: `cleanup` para apagar o clone.

## Retomada
```bash
{{WK}} code --repo <repo> state    # next para continuar
```
Após 3+ módulos/units na sessão, ofereça pausa (`/clear` + retomar).

## Não faça
- Não escreva no repositório, nem estado.
- Não marque 🟢 sem linha.
- Não pule `plan`.
- Não copie o codebase para o workdir/wiki.
- Não use scripts temporários para gerar SDD; use comandos `wk.pyz`.
- Não leia o repo nos estágios 3–5.
- Não misture 🟡 e 🟢 em `confirmed.md`/`inferred.md`.
- Não gere todas as specs numa resposta só.
