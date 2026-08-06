# Wiki AI

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

1.2. Defina as variáveis de execução:

```bash
export WKPY='/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe'
export WK='C:/Users/User/Downloads/files/second-brain/wk.pyz'
```

Esperado: nenhuma saída (só `export`). Se falhar: `WKPY`/`WK` apontam para
caminho errado — confira com `ls "$WKPY"` e `ls "$WK"`.

1.3. Materialize a skill e grave as permissões da engine sobre store/repo:

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

```bash
$WKPY "$WK" store init "$WK_STORE"
```

Esperado: JSON com `"criados"`/`"ja_existiam"` listando `inbox/`, `raw/`,
`wiki/`, `log.md`, `quarantine.md`. Idempotente — rodar de novo não apaga
nada. Se falhar: caminho de `$WK_STORE` inválido (ex.: unidade não montada).

1.5. Verificação final — rode `wk doctor` antes de qualquer outra coisa:

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

Pré-requisito: passo 1 completo. Defina as variáveis do repositório antes de
começar:

```bash
export WK_STORE='C:/Users/User/OneDrive/Documentos/projetos/wiki/store'
export WK_REPO='C:/Users/User/projetos/desafio-tech-lead/insurance-quote-service'
export WK_TOPIC='codebases/insurance-quote-service'
```

Sequência completa, sem pular etapas: `surface` → `export` → `config` →
`plan` → `run-stage modules` → fan-out → `merge-agent-output modules`
(um por batch) → `done modules` → `run-stage rules` → merge → `done rules`
→ `run-stage architecture` → merge → `done architecture` → `pending specs`
→ `run-stage specs` → merge → `done specs` → `evidence` → `done evidence`
→ `run-stage synth` → merge → `done synth` → `verify` → `done verify` →
`audit --strict` → `publish` → `promote` → `compile` → `lint`.

### 2.1. `surface` — estágio 1: mapa determinístico

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" surface --topic "$WK_TOPIC"
```

Esperado: `"ok":true`, `"cmd":"surface"`, com `arquivos`, `loc`, `modulos`,
`entry_points` e `artifact` (caminho de `surface.json`). Sem `error`.

Se falhar: `--repo` não existe → corrija o caminho. `store não informado` →
falta `--store` explícito ou `WK_STORE` no ambiente.

### 2.2. `export` — artefatos SDD determinísticos (sem LLM)

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" export --topic "$WK_TOPIC"
```

Esperado: `"ok":true`, `"cmd":"export"`; grava `sdd/inventory.md`,
`sdd/dependencies.md`, `sdd/coupling.md` no workdir. Passo obrigatório do
estágio 1, não opcional.

Se falhar: erro `rode surface primeiro` → repita 2.1. Nunca use `--output`
apontando para `raw/`/`wiki/` do store (bloqueado — use o fluxo de
`publish` na seção 2.27).

### 2.3. `config` — decisões do SDD (obrigatório antes de `plan`/`run-stage modules`)

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" config --doc-level detalhado --granularity hybrid
```

Esperado: `"ok":true`, `"cmd":"config"`, ecoa `doc_level`/`granularity`
gravados no estado.

Se falhar: `--doc-level`/`--granularity` fora das opções válidas
(`essencial|completo|detalhado` / `module|endpoint|use-case|hybrid|feature|custom`)
→ argparse recusa antes de chamar o comando; corrija o valor.

### 2.4. `plan` — lista de módulos a cavar (LOC, main primeiro)

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" plan
```

Esperado: `"ok":true`, `"cmd":"plan"`, lista de módulos condensada em
`*_count`. Não fecha nenhum estágio — só planeja.

Se falhar: erro `config SDD obrigatória antes de plan/run-stage modules`
com campo `"acao"` trazendo o comando `config` exato → rode 2.3 antes.

### 2.5. `run-stage modules` — manifesto de fan-out

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

Não é comando `wk`. Dispare **exatamente `fanout_required` subagentes**, um
por batch, com listas de itens disjuntas (cada módulo cai em um único
batch). Para cada batch `N`:

- o caminho de saída é o campo `output` do batch (corpo completo do passo
  2.5, padrão, ou o manifesto em disco): `.../agent-outputs/modules-batch-NN.txt`;
- o subagente **grava o próprio arquivo** nesse caminho, no formato
  `=== MODULE: <path> === ... === END ===` (contrato completo em
  `sdd-brief modules`, seção 8);
- o subagente devolve só o recibo de 3 linhas — `ARQUIVO:`/`BLOCOS:`/`BYTES:`
  — nunca o conteúdo do artefato, log, diff ou eco de comando na mensagem.

### 2.7. `merge-agent-output modules` — um por batch

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

### 2.8. `done modules`

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

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage rules
```

Esperado: igual a 2.5, mas para `rules` — normalmente 1 batch (`agent_slot`
tipo `rules-b01`). Sem fan-out múltiplo obrigatório quando `fanout_required
<= 1`.

Se falhar: mesmos casos de 2.5, adaptados ao estágio `rules`.

### 2.10. Subagente + `merge-agent-output rules`

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

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done rules
```

Esperado/Se falhar: mesmos casos de 2.8, estágio `rules`. Em `doc_level`
completo/detalhado, `done rules` também exige ao menos um ADR retroativo em
`sdd/adrs/`.

### 2.12. `run-stage architecture`

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage architecture
```

Esperado/Se falhar: mesmos casos de 2.9, estágio `architecture`.

### 2.13. Subagente + `merge-agent-output architecture`

Blocos aceitos: `=== ARCHITECTURE: architecture ===` / `c4-context` /
`c4-containers` / `c4-components` / `erd-complete` /
`traceability/spec-impact-matrix` / `sequences/<slug>` (os últimos por
`doc_level` — ver seção 3; `sequences/` só em `detalhado`).

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output architecture --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/architecture-batch-01.txt" --agent architecture-b01
```

Esperado/Se falhar: mesmos casos de 2.7, estágio `architecture`.

### 2.14. `done architecture`

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done architecture
```

Esperado/Se falhar: mesmos casos de 2.8, estágio `architecture`. Em
`doc_level` detalhado, `done architecture` também exige ao menos um diagrama
de sequência em `sdd/sequences/`.

### 2.15. `pending specs` — registra as units a especificar

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" pending specs --items "criar-cotacao,emitir-apolice,invalidar-cache"
```

Esperado: `"ok":true`, `"cmd":"pending"`, estágio `specs` com `pending_count`
igual ao número de itens informados.

Se falhar: `stage` inválido → só os estágios de `STAGES` são aceitos; confira
grafia (`specs`, não `spec`).

### 2.16. `run-stage specs`

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage specs
```

Esperado/Se falhar: mesmos casos de 2.9, estágio `specs`, batch com os itens
registrados em 2.15.

### 2.17. Subagente + `merge-agent-output specs`

Bloco por unit: `=== SPEC: <unit> ===`, com três sub-blocos obrigatórios
dentro — `--- requirements.md ---`, `--- design.md ---`, `--- tasks.md ---` —
e os opcionais `--- contracts.md ---` / `--- edge-cases.md ---` (obrigatórios
em `doc_level` detalhado). O mesmo cabeçalho `=== SPEC: ... ===` também
aceita documentos nomeados de revisão final: `confidence-report`, `gaps`,
`traceability/code-spec-matrix`, `user-stories/<slug>` (grava `.md`) e
`openapi/<slug>` (grava `.yaml`) — corpo livre, sem sub-blocos (ver seção 3).

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output specs --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/specs-batch-01.txt" --agent specs-b01
```

Esperado/Se falhar: mesmos casos de 2.7, estágio `specs`. Artefatos vão para
`sdd/specs/<slug-da-unit>/{requirements,design,tasks}.md`.

### 2.18. `done specs`

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done specs
```

Esperado/Se falhar: mesmos casos de 2.8; exige ao menos uma unit concluída,
`sdd/confidence-report.md` não vazio e, em `doc_level` >= completo,
`sdd/gaps.md`, `sdd/traceability/code-spec-matrix.md` e ao menos uma user
story em `sdd/user-stories/`.

### 2.19. `evidence` — evidence-pack por tópico

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" evidence --topic "$WK_TOPIC"
```

Esperado: `"ok":true`, `"cmd":"evidence"`, `artifact` apontando para
`evidence-<topic>.json` no workdir.

Se falhar: sem candidatos para o tópico → confira `--topic`/`--top`; nenhum
match não é erro fatal, mas o artefato sai vazio (revise antes de prosseguir).

### 2.20. `done evidence`

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done evidence
```

Esperado/Se falhar: mesmos casos de 2.8, estágio `evidence`.

### 2.21. `run-stage synth`

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage synth
```

Esperado/Se falhar: mesmos casos de 2.9, estágio `synth`. `run-stage synth`
recusa se `modules`/`rules`/`architecture`/`specs` não estiverem `done`.

### 2.22. Subagente + `merge-agent-output synth`

Blocos: `=== SYNTH: confirmed ===` / `=== SYNTH: inferred ===`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output synth --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/synth-batch-01.txt" --agent synth-b01
```

Esperado/Se falhar: mesmos casos de 2.7. Grava `sdd/confirmed.md` e/ou
`sdd/inferred.md` — local canônico; nunca uma cópia na raiz do workdir. O
manifesto `agent-runs/synth.json` exigido pelo `audit` só é gravado por este
merge.

### 2.23. `done synth`

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done synth
```

Esperado/Se falhar: mesmos casos de 2.8, estágio `synth`.

### 2.24. `verify` — valida Markdown confirmado contra arquivo:linha

Comando:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" verify --artifact "$WK_STORE/.codescan/<repo>-<hash>/sdd/confirmed.md"
```

Esperado: `"ok":true`, `"cmd":"verify"`, relatório com citações
`arquivo:linha` validadas contra o repo.

Se falhar: citação não bate com o repo → corrija `confirmed.md` (via novo
merge de `synth`) e repita.

### 2.25. `done verify`

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done verify
```

Esperado/Se falhar: mesmos casos de 2.8, estágio `verify`.

### 2.26. `audit --strict`

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
refaça o estágio citado com o merge atual (schema `wiki-ai.agent-runs.v2`).
`P0: artefato alterado após o merge` → não editar artefato SDD à mão; refaça
`merge-agent-output` do estágio.

### 2.27. `publish` — leva a árvore SDD do workdir para `inbox/`

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

Comando:

```bash
$WKPY "$WK" compile "$WK_TOPIC" --store "$WK_STORE"
```

Esperado: `"paginas"` (uma por fonte promovida, mais `wiki/index.md` —
catálogo agrupado por `topic`, com contagem total e por tópico),
`"reindexed":true`. Roda `reindex` sozinho ao final.

Se falhar: `"fontes":0` → nada foi promovido para esse tópico; volte a 2.28.

### 2.30. `lint` — audita o índice e fecha o runbook

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

| Estágio | Cabeçalho aceito | Artefato gerado |
|---|---|---|
| modules | `=== MODULE: <path> === ... === END ===` | `modules/<slug-do-path>.md` |
| rules | `=== RULES: domain \| state-machines \| permissions \| adrs/NNN-<slug> ===` (state-machines/permissions/adrs só em `doc_level` completo/detalhado) | `sdd/<nome>.md` / `sdd/adrs/NNN-<slug>.md` |
| architecture | `=== ARCHITECTURE: architecture \| c4-context \| c4-containers \| c4-components \| erd-complete \| traceability/spec-impact-matrix \| sequences/<slug> ===` (por `doc_level`; `sequences/` só em detalhado) | `sdd/<nome>.md` |
| specs (unit) | `=== SPEC: <unit> ===` com sub-blocos `--- requirements.md ---` / `--- design.md ---` / `--- tasks.md ---` (+ opcionais `--- contracts.md ---` / `--- edge-cases.md ---`) | `sdd/specs/<slug-da-unit>/*.md` |
| specs (doc nomeado) | `=== SPEC: confidence-report \| gaps \| traceability/code-spec-matrix \| user-stories/<slug> \| openapi/<slug> ===` (corpo livre) | `sdd/<nome>.md` (`openapi/` grava `.yaml`) |
| synth | `=== SYNTH: confirmed \| inferred ===` | `sdd/confirmed.md` / `sdd/inferred.md` |

Contrato compacto e exato de cada estágio (seções obrigatórias, limites de
linha, regras de "sem prosa"): `wk code --repo "$WK_REPO" --store
"$WK_STORE" sdd-brief <stage>` (`stage` em `evidence, modules, rules,
architecture, specs, synth`) — rode antes de escrever o subagente.

## 4. Retomada

Perdeu o fio? Nesta ordem, sem adivinhar:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" state
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" next
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" read <arquivo-do-repo> --from <linha> --count <n>
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" audit --strict
```

- `state`: estado completo do workdir por padrão; `--quiet` condensa a uma
  linha (`estagio_atual` sobrevive à condensação por ser escalar).
- `next`: o que fazer agora, sem reconstruir o histórico à mão.
- `read`: leitura confinada ao repo declarado, numerada — nunca leia o repo
  por fora do `wk` (sem `cat`, sem editor, sem IDE apontando pra fora do
  contrato).
- `audit --strict`: mede o que já está bloqueando antes de continuar; se
  nada foi checado ainda, reporta `"status":"sem_evidencia"` em vez de
  `pass` (seção 2.26) — nunca trate ausência de evidência como aprovação.

Nunca use `python -c`, heredoc ou script ad hoc para ler JSON de
surface/state — use os comandos acima.

## 5. Erros comuns

| Sintoma | Causa | Correção |
|---|---|---|
| `wk ingest codebase ...` recusado | `ingest codebase` não é subcomando do CLI — é a operação da skill | rode o fluxo da seção 2, a partir de `code ... surface` |
| saída truncada/estranha, algo como `No linha:N caractere:` ou cheia de `~~` | comando rodou em PowerShell, não Git Bash | reexecute em Git Bash; se preso em PowerShell, embrulhe: `bash -c '...'` |
| `store não informado: sem --store e sem WK_STORE` | faltou `--store` e `WK_STORE` não está no ambiente | passe `--store "$WK_STORE"` ou `export WK_STORE=...` (seção 2) |
| `flag(s) --topic pertence(m) ao subcomando 'surface', não ao nível superior` | flag do subcomando escrita antes do subcomando | mova a flag para depois do subcomando — a própria mensagem traz o comando corrigido em `"acao"` |
| `merge-agent-output exige --agent <identificador do subagente>` | `--agent` ausente | repita com `--agent <agent_slot-do-batch>` |
| `--agent '...' é genérico demais` | usou `main`/`orquestrador`/`orchestrator`/`self`/`principal` | use o `agent_slot` real do batch (ex.: `modules-b01`) |
| `input ... tem mtime anterior ao plano de fan-out` | `--input` é arquivo velho (sobra de tentativa anterior) | grave o output de novo, depois de rodar `run-stage` |
| `fan-out obrigatório não cumprido em <stage>` | `done` bloqueado: todos os batches vieram do mesmo `--agent` | refaça `merge-agent-output` do(s) batch(es) faltante(s) com `--agent` realmente distinto |
| `P0: agent-runs schema legado (wiki-ai.agent-runs.v1) sem prova criptográfica` | manifesto `agent-runs` antigo (v1), sem hash | refaça o estágio citado com o `merge-agent-output` atual (schema v2) |
| `P0: artefato alterado após o merge` | artefato SDD editado à mão depois do merge (hash não bate) | não edite artefato manualmente; refaça `merge-agent-output` do estágio |

## 6. Gates

| Gate | Bloqueia |
|---|---|
| `audit --strict` | falso positivo |
| `agent-pack v2` | leitura ampla do repo |
| `merge-agent-output` | escrita manual de SDD; `--agent` ausente/genérico; input com `mtime` anterior ao plano do stage |
| `agent-runs` (schema `wiki-ai.agent-runs.v2`) | stage `done` sem subagente; manifesto v1 (legado) ou artefato/input alterado após o merge (hash `sha256` recomputado pelo audit) |
| `done --artifact` | fechar `done` com artefato que nunca passou por `merge-agent-output`; ou todos os batches registrados pelo mesmo `--agent` |
| `export --output` | gravar direto em `raw/`/`wiki/` do store (use `publish`) |
| `code` sem store | execução sem `--store` explícito e sem `WK_STORE` no ambiente |
| `ingest codebase` (CLI) | subcomando inexistente — erro aponta `code ... surface` |
| workdir hygiene | `*.py`, `*.txt`, `src/`, specs vazias |
| Mermaid strict | diagrama vazio, denso, sem aresta, label inválido |
| noise | log, diff, `Ran command`, `Edited`, `Wrote` |

## 7. Proibido

- PowerShell.
- misturar `/wiki-ai ingest` com comando `python`.
- script auxiliar no workdir.
- copiar repo para `store/.codescan`.
- pasta vazia em `sdd/specs`.
- pasta vazia em `modules`.
- output fora dos blocos parseáveis.
- log colado no chat.
- diff colado no chat.
- artefato recém-escrito colado no chat.
- `python -c`, heredoc ou script ad hoc para ler JSON de surface/state; use `state`, `next`, `read` e `audit`.

## 8. Referência rápida de comandos

| Comando | Para quê |
|---|---|
| `wk doctor` | diagnóstico único do ambiente (shell, python, store, repo, engine) |
| `wk init --engine <e> [--store --repo]` | materializa a skill + grava permissões |
| `wk check --engine <e> [--store --repo]` | compara disco vs. embutido; valida permissões |
| `wk store init [caminho]` | cria `inbox/`/`raw/`/`wiki/` do store |
| `wk code ... surface --topic <slug>` | estágio 1: mapa determinístico do repo |
| `wk code ... export --topic <slug>` | inventory/dependencies/coupling (sem LLM) |
| `wk code ... config --doc-level --granularity` | grava decisões do SDD (obrigatório antes de plan) |
| `wk code ... plan` | lista módulos a cavar, por LOC |
| `wk code ... pending <stage> --items <lista>` | registra pendências de um estágio |
| `wk code ... run-stage <stage>` | manifesto de fan-out para subagentes |
| `wk code ... sdd-brief <stage>` | contrato compacto do estágio para o subagente |
| `wk code ... agent-pack <stage> --batch N` | pacote determinístico de um batch (todos os estágios; `modules` lê o repo, os demais leem o material do workdir) |
| `wk code ... merge-agent-output <stage> --input --agent` | integra a saída de um subagente |
| `wk code ... evidence --topic <slug>` | evidence-pack por tópico |
| `wk code ... verify --artifact <md>` | valida citações `arquivo:linha` |
| `wk code ... done <stage> [--item --artifact]` | marca estágio/item concluído |
| `wk code ... blocked\|failed\|degraded <stage>` | registra item com problema |
| `wk code ... state` | estado completo do workdir |
| `wk code ... next` | o que fazer agora |
| `wk code ... read <path> --from --count` | lê arquivo do repo, numerado |
| `wk code ... audit [--strict]` | mede qualidade SDD sem alterar estado |
| `wk publish --workdir --topic` | leva a árvore SDD do workdir para `inbox/` |
| `wk promote [--approve \| --approve-all --source-type]` | move fontes seguras de `inbox/` para `raw/` |
| `wk compile [topic]` | gera `wiki/` a partir de `raw/` promovido |
| `wk lint [path]` | audita o índice, escreve `wiki/_lint-report.md` |
| `wk ingest <arquivo> --source-type --origin --topic` | registra fonte em `inbox/` com proveniência: md/txt passthrough; vtt/srt transcrição; html; xml → análise estrutural (draw.io/XMI/outline); json → bloco de código; docx/xlsx/csv/pdf → original imutável em `raw/assets/` + página de fonte para análise pelo LLM |
| `wk index reindex\|status` | manutenção do índice |
| `wk search "<query>"` / `wk get <ref>` | busca híbrida / recupera documento |
| `wk audit` | regras determinísticas L1/L2/L5 do índice |
| `wk docs [nome] [--list]` | documentação embutida no executável |

## 9. Modo skill (slash commands)

Alternativa ao runbook manual: em vez de digitar a sequência da seção 2, você
invoca a operação e o agente executa o pipeline. As sete operações existentes:

| Slash command | O que faz | Equivale, na CLI, a |
|---|---|---|
| `/wiki-ai ingest <arquivo>` | registra fonte solta (transcrição, doc, planilha, XML, clip) em `inbox/`; docx/xlsx/csv/pdf viram asset imutável em `raw/assets/` + página de fonte, e o agente escreve a análise (portão agent-output) | `wk ingest <arquivo> --source-type <t> --origin "<origem>" --topic <slug>` |
| `/wiki-ai ingest codebase <caminho> --topic <slug>` | pipeline de repositório inteiro (seção 2, do `surface` ao `lint`) | `wk code --repo <r> --store <s> surface --topic <slug>` e o restante da seção 2 |
| `/wiki-ai promote` | portão de promoção `inbox/` → `raw/` | `wk promote --store <s>` |
| `/wiki-ai compile` | (re)gera `wiki/` a partir de `raw/` promovido | `wk compile <topic> --store <s>` |
| `/wiki-ai lint` | audita integridade (somente leitura) | `wk lint --store <s>` |
| `/wiki-ai query <pergunta>` | consulta a wiki | `wk search "<pergunta>" --store <s>` |
| `/wiki-ai reindex` | atualiza o índice de busca | `wk index reindex --store <s>` |

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
| Auditar sem alterar nada | `/wiki-ai lint` ou `wk audit --store <s>` |

### 9.3. Pré-requisito

O slash command só existe depois de `wk init --engine <e>` (passo 1.3), que
materializa a skill no diretório da engine (`.claude/skills/wiki-ai` no Claude
Code, `.agents/skills/wiki-ai` nas demais). Verifique com:

```bash
$WKPY "$WK" check --engine devin --store "$WK_STORE" --repo "$WK_REPO"
```
