# ingest codebase

Uso: `ingest codebase <caminho-ou-URL> --topic <slug>`. Pipeline de 8 estágios.
`--repo` aceita caminho local OU URL (`https://`, `git@`, `.git`) — URL é clonada
fora do `store/.codescan` e reusada. Repo é READ-ONLY. Workdir:
`store/.codescan/<repo>-<hash>/`; árvore SDD em `<workdir>/sdd/` (contrato:
`sdd-contract`).

## Quem executa o quê neste fluxo
Só duas personas: 👤 humano (digita comando no terminal) e 🤖 modelo (LLM,
subagente ou orquestrador). `wk.pyz`/`code` é ferramenta — nunca sujeito da
frase. Contagem de passos nomeados neste doc:

| Tipo | Passos | Quem | Precisa de LLM? |
|---|---|---|---|
| **D** | 16 — `surface`, `export`, `plan`, `agent-pack`, `run-stage` (as 5 chamadas, que só preparam manifesto), `merge-agent-output` (as 5 chamadas, mecânico), `evidence`, `verify`, `audit --strict`, `publish`, `state`/`next`/`read`, `cleanup` | 👤 ou 🤖, indiferente | **Não** |
| **M** | 5 — os 5 pontos de fan-out: subagente escreve `modules`, `rules`, `architecture`, `specs`, `synth` | 🤖 obrigatório | **Sim** — sem modelo o estágio não produz conteúdo, fica pendente para sempre |
| **H** | 4 — `config --doc-level/--granularity`; `pending specs --items`; fechar `done <stage>` (portão de qualidade, decide se aceita/rebaixa); decidir `--allow-unverified` em `promote`/`compile`/`docx` a jusante | 👤 obrigatório | Não, mas exige julgamento e responsabilidade |

**Dá para completar sem modelo?** NÃO — os 5 pontos de fan-out (tabela
abaixo) são obrigatoriamente 🤖; sem eles `modules/*.md`, `sdd/domain.md`,
`sdd/architecture.md`, `sdd/specs/**` e `sdd/confirmed.md`/`inferred.md`
simplesmente não existem, e o pipeline não tem como fechar `done` em nenhum
desses 5 estágios (prova de que nada gera conteúdo fora deles:
`cmd_run_stage`, docstring `"""Prepara execução por subagentes. Não gera
conteúdo SDD."""`, `scripts/codescan/cli.py` (docstring de `cmd_run_stage`)). Todo o resto —
`surface`, `export`, `config`, `plan`, `run-stage` (a chamada que só monta o
manifesto), `merge-agent-output`, `done`, `evidence`, `verify`, `audit`,
`publish` — roda sem LLM: são leitura de repositório, regex, SQL/state JSON
e cópia de arquivo. A prova, comando a comando, está na tabela "Estágio ×
tipo" logo abaixo e repetida em cada seção de estágio.

## Estágio × tipo (visão geral antes do detalhe)
| Estágio/comando | Tipo | Por quê |
|---|---|---|
| `surface` | **D** | varredura de arquivos + regex (`scan`, `scripts/codescan/surface.py`, chamado em `cmd_surface`, `scripts/codescan/cli.py` (função `cmd_surface`)) |
| `export` | **D** | motor de acoplamento Java/genérico, cálculo Ce/Ca/I determinístico |
| `config --doc-level/--granularity` | **H** | valor não é inferido — alguém escolhe (`cmd_config`, `scripts/codescan/cli.py` (função `cmd_config`), grava exatamente o que foi passado) |
| `plan` | **D** | partição por fórmula fixa `min(8, nº módulos, ceil(LOC/2000))` (`scripts/codescan/cli.py` (dimensionamento de fan-out `_auto_batches`)) — humano NÃO escolhe N |
| `run-stage <stage>` | **D** | só monta manifesto/packs; nunca gera conteúdo SDD (`scripts/codescan/cli.py` (docstring de `cmd_run_stage`)) |
| **fan-out `modules`/`rules`/`architecture`/`specs`/`synth`** | **M** | só um LLM lê o pack e escreve o bloco de resposta |
| `pending specs --items` | **H** | humano decide/confirma quais units entram na fila (`cmd_pending`, `scripts/codescan/cli.py` (função `cmd_pending`)) |
| `agent-pack` | **D** | serialização determinística do pack para o subagente |
| `merge-agent-output` | **D** | parser de blocos fixos, sem LLM (`agentmerge.py`); o conteúdo dentro veio de M, o parse em si não |
| `done <stage>` (fechar o estágio) | **H** | validações estruturais são D, mas aceitar/rebaixar (`portão de qualidade` abaixo) é decisão |
| `evidence` | **D** | busca lexical no repo, sem `run-stage`, sem subagente (`cmd_evidence`, `scripts/codescan/cli.py` (função `cmd_evidence`)) |
| `verify` | **D** | checador de citação `arquivo:linha`, não semântico (`cmd_verify`, `scripts/codescan/cli.py` (função `cmd_verify`)) |
| `audit --strict` | **D** | checagens estruturais (arquivos, Mermaid, `agent-runs`) |
| `publish` | **D** | classificação fixa por caminho + cópia de arquivo (`cmd_publish`, `scripts/wk/cli.py` (função `cmd_publish`) em diante) |
| `state`/`next`/`read`/`cleanup` | **D** | introspecção/leitura pontual, sem geração |
| `promote`/`compile`/`docx` a jusante | **D**, exceto `--allow-unverified` que é **H** | ver `operations/promote.md`, `compile.md`, `docx.md` |

## Onde chamar o modelo — os 5 pontos de fan-out

Só 5 pontos deste pipeline exigem que um LLM escreva conteúdo — todo o resto
é `wk` determinístico:

| # | Comando que ABRE | Modelo LÊ | Modelo ESCREVE | Formato do bloco | Comando que FECHA |
|---|---|---|---|---|---|
| 1 | `run-stage modules` | `agent-packs/modules-batch-NN.json` | `agent-outputs/modules-batch-NN.txt` | `=== MODULE: <path> === … === END ===` | `merge-agent-output modules` → `done modules` |
| 2 | `run-stage rules` | `agent-packs/rules-batch-NN.json` | `agent-outputs/rules-batch-NN.txt` | `=== RULES: domain\|state-machines\|permissions\|adrs/NNN-<slug> ===` | `merge-agent-output rules` → `done rules` |
| 3 | `run-stage architecture` | `agent-packs/architecture-batch-NN.json` | `agent-outputs/architecture-batch-NN.txt` | `=== ARCHITECTURE: architecture\|c4-*\|erd-complete\|traceability/spec-impact-matrix\|sequences/<slug> ===` | `merge-agent-output architecture` → `done architecture` |
| 4 | `run-stage specs` | `agent-packs/specs-batch-NN.json` | `agent-outputs/specs-batch-NN.txt` | `=== SPEC: <unit> ===` **SINGULAR** + trio `--- requirements.md/design.md/tasks.md ---` | `merge-agent-output specs` → `done specs` |
| 5 | `run-stage synth` | `sdd/*.md` de 1º nível | `agent-outputs/synth-batch-01.txt` | `=== SYNTH: confirmed\|inferred ===` | `merge-agent-output synth` → `done synth` |

`run-stage` NUNCA gera conteúdo — só prepara o manifesto
(`"generates_sdd_content": false` no próprio retorno). Todo output com
`fanout_required` ≥ 1 é chamada de modelo obrigatória. `evidence` **não**
entra nessa lista — roda sem `run-stage`, sem fan-out (ver Estágio 6
abaixo). Mecanismo de disparo por engine: Claude Code = `Task` · Devin =
subagentes · Antigravity = `start_subagent` · Copilot = sessões de
subagente.

## Regra do campo `acao`

Erro de `merge-agent-output` (e o guard de estágio inválido) traz `"acao"`
com o comando corretivo exato. **Execute-o literalmente antes de qualquer
outra investigação.** `done` é exceção: a dica vem em `blockers[].action`
(inglês), só quando há mais de um blocker; com um único blocker, o `"error"`
já nomeia o que corrigir. Nunca abra `wk.pyz` com `zipfile`/decompilação —
o contrato de cada estágio é `{{WK}} code sdd-brief <stage>`.

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
- `coupling.md` sem grafo quando houver módulos/dependências internas (vale
  para os dois motores — Java e genérico);
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

## Escape hatch: `blocked`/`failed`/`degraded <stage> --item`
Mecanismo para quando um item **não pode** ser concluído — sem ele, um item
travado fica "pendente" para sempre e nada sinaliza o motivo. Rodar o
comando é 👤/🤖 **D** (mutação de estado, sem LLM — função
`cmd_problem_status` em `scripts/codescan/cli.py`); **decidir qual dos três
usar é 👤 H**: é julgamento sobre a causa da falha, não algo que o CLI
infere.

| Comando | Quando usar |
|---|---|
| `blocked <stage> --item <item>` | Item não pode nem começar (dependência externa faltando, escopo ambíguo, aguardando resposta humana) |
| `failed <stage> --item <item>` | Subagente tentou e não produziu contrato mínimo (morreu, saiu do formato, `FAILED <TIPO>` — ver "Brief compacto") |
| `degraded <stage> --item <item>` | O pai fez fallback manual incompleto para não travar o pipeline; artefato existe mas abaixo do padrão — nunca vira `done` |

Exemplo concreto — execução real, capturada rodando os três comandos em
sequência contra um workdir de teste (`item` = caminho de um módulo real
do repo, ex. `src/main.py`):
```bash
{{WK}} code --repo <repo> blocked modules --item "src/main.py"
```
Saída real:
```json
{
  "status": "blocked",
  "done": [],
  "pending": [],
  "artifact": null,
  "blocked": ["src/main.py"],
  "failed": [],
  "degraded": [],
  "items_complete": false
}
```
`failed`/`degraded` produzem o mesmo formato, só troca a lista populada
(`"failed": ["src/main.py"]` ou `"degraded": ["src/main.py"]`) e o campo
`status` de topo. Como saber que deu certo: o item aparece na lista
correspondente ao comando rodado e nas outras duas listas de problema ele
some (um item só pode estar em um estado por vez).

Efeito em `done`/`audit`: `items_complete` fica `false` enquanto qualquer
item estiver em `blocked`/`failed`/`degraded` — `done <stage>` (sem
`--item`) recusa com `exit 2` e um blocker por item pendente/degradado,
cada um já com o campo `action` (inglês) apontando a correção, ex. (saída
real, um blocker do total, o formato é sempre `{"error": "...", "action":
"..."}`):
```json
{"error": "estágio modules ainda tem itens degraded: src/main.py", "action": "corrigir blocker e rodar done modules"}
```
Um item `degraded` nunca vira `done` sozinho — é preciso corrigir o
conteúdo (rodar `merge-agent-output` de novo com um input melhor, ou
`redo`) e só então marcar `done modules --item <item>`; enquanto isso,
`audit --strict` também reprova o stage por causa da mesma lista de itens
problemáticos.

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

> ⚠️ **`SPEC` é SINGULAR.** `SPECS` (plural) é o erro mais comum de fan-out
> neste estágio — o parser (`SPEC_RE` em `scripts/codescan/agentmerge.py`)
> só reconhece `=== SPEC: ...===`; `=== SPECS: ... ===` vira "prosa fora de
> arquivo SPEC" e derruba a rodada de fan-out inteira.
>
> | Errado ❌ | Certo ✅ |
> |---|---|
> | `=== SPECS: functional ===` | `=== SPEC: functional ===` |
> | `=== SPECS: <unit> ===` | `=== SPEC: <unit> ===` |

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
👤 ou 🤖 **D** para `surface`/`export` — varredura de arquivos e cálculo de
acoplamento, sem LLM. 👤 **H** para a decisão de `doc_level`/`granularity`
logo abaixo — o CLI grava exatamente o valor passado, não infere nada.

Exemplo concreto (substitua `<repo>`, `<slug>` pelos valores reais):
```bash
{{WK}} code --repo C:/projetos/insurance-quote-service --store <store> surface --topic codebases/insurance-quote-service
{{WK}} code --repo C:/projetos/insurance-quote-service --store <store> export --topic codebases/insurance-quote-service
```
Saída esperada de `surface` (trecho): `{"workdir": "...", "artifact":
".../surface.json", "arquivos": 812, "loc": 54211, "modulos": 37, ...}`.
Como saber que deu certo: `artifact` aponta para um `surface.json` que
existe em disco e `modulos > 0`. Se `modulos: 0` ou `arquivos: 0`: repo
vazio/caminho errado — confira `--repo` antes de prosseguir.

`export` gera 3 artefatos `code-repo`: `sdd/inventory.md`, `sdd/dependencies.md`
e `sdd/coupling.md` (zonas de design — Ce/Ca/I 🟢, abstração/zona 🟡). Motor
Java (classe e pacote, ciclos, classe-deus, hotspot de Ca, abstração
especulativa; limiares por outlier IQR) quando o repo tem Java; motor
genérico multi-linguagem caso contrário. Com Java, `export` também grava
`sdd/coupling.html` — mapa interativo. Fica local no `export`, mas **é
publicado** por `wk publish` como asset `code-repo` (bytes em
`raw/assets/<doc_id>.html` + stub `.md`; ver `publish`, adiante no
Estágio 7 — Synth). Default de saída é `<workdir>/sdd`;
`--output <dir>` recusa qualquer destino dentro de `raw/` ou `wiki/` do
store (esses só mudam via `publish`/`promote`).
Ler os `warnings`. Decidir com o usuário e registrar — 👤 **H**: o CLI não
tem como inferir o nível de detalhe certo para este repositório; errar aqui
(ex.: escolher `essencial` para um repo crítico) produz uma árvore SDD rasa
demais para reimplementação, só percebido tarde, na revisão final.

```bash
{{WK}} code --repo C:/projetos/insurance-quote-service --store <store> config --doc-level completo --granularity module
```
Saída esperada: `{"doc_level": "completo", "granularity": "module"}` — eco
exato do que foi passado, prova de que não há inferência. Como saber que
deu certo: os dois campos aparecem preenchidos. Se `next` continuar
pedindo `config` depois disso, o valor de `--doc-level`/`--granularity`
passado não é um dos válidos (tabelas em `sdd-contract` §2/§3) e foi
ignorado silenciosamente na gravação — confira a grafia exata.

Tabelas em `sdd-contract` §2 (doc_level) e §3 (granularity). Retomada: `state`
mostra as decisões em `sdd` — não pergunte de novo.
`config` é obrigatório antes de `plan` e `run-stage modules`; se faltar
`sdd.doc_level` ou `sdd.granularity`, use `next` e rode a ação indicada.

## Estágio 2 — Modules (por subagentes)
SEMPRE paralelize por subagentes. Não cave em série. Passos 1 e a abertura
do passo 2 (`agent-pack`/`run-stage`) são 👤/🤖 **D**; a escrita do conteúdo
pelo subagente é 🤖 **M** obrigatória; `merge-agent-output` volta a ser
**D** (mecânico); fechar `done modules` no fim do passo 3 é 👤 **H**.

1. Particionar — **D**:
```bash
{{WK}} code --repo C:/projetos/insurance-quote-service --store <store> plan
```
`plan` decide o nº de subagentes (`min(8, nº módulos, ceil(LOC/2000))`), por
fórmula fixa (`scripts/codescan/cli.py` (dimensionamento de fan-out `_auto_batches`)) — humano não escolhe N.
Teste fora do alvo (`--include-tests` inclui). Cada `batches[].modulos` → um
subagente. Saída esperada (trecho): `{"batches": [{"batch": 1, "modulos":
["src/quote/"]}, ...]}`. Como saber que deu certo: nº de `batches` > 0 e
cada módulo do repo aparece em exatamente um batch.

2. Fan-out — 🤖 **M**, obrigatório: um subagente por grupo, listas
disjuntas. Sem subagente aqui, os `modules/*.md` simplesmente não existem —
nada mais no pipeline gera esse conteúdo. Spawn: Claude Code = Task ·
Devin = subagentes · Antigravity = `start_subagent` · Copilot = sessões de subagente.

Gerar pacote determinístico por batch — 👤/🤖 **D**:
```bash
{{WK}} code --repo C:/projetos/insurance-quote-service --store <store> agent-pack modules --batch 1
```

Fluxo fechado — abertura é 👤/🤖 **D** (só monta manifesto e packs; não
gera conteúdo SDD, `scripts/codescan/cli.py` (docstring de `cmd_run_stage`)):
```bash
{{WK}} code --repo C:/projetos/insurance-quote-service --store <store> run-stage modules
```
Saída esperada (trecho): `{"stage": "modules", "fanout_required": 3,
"generates_sdd_content": false, "batches": [{"batch": 1, "agent_slot":
"Archaeologist-1", "output": ".../agent-outputs/modules-batch-01.txt",
"merge_command": "wk code ... merge-agent-output modules --input ... --agent Archaeologist-1"}, ...]}`.
Como saber que deu certo: `fanout_required >= 1` — **esse número de
subagentes 🤖 é obrigatório**; `generates_sdd_content: false` confirma que
o próprio comando não escreveu nenhum `.md` de conteúdo, só o manifesto.

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

Pai: depois do recibo, roda (👤/🤖 **D** — parser de blocos fixos, sem LLM;
o conteúdo que ele grava já foi escrito pelo subagente no passo M acima)
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

Só então rode — 👤 **H**: fechar o estágio é decisão, não checagem
mecânica pura. Antes de rodar, confira o "Portão de qualidade" (abaixo):
aceitar um artefato genérico/raso como `done` propaga baixa qualidade para
toda a árvore SDD e para a wiki final; o risco de decidir errado aqui é
publicar documentação que não serve para reimplementar o sistema.
```bash
{{WK}} code --repo <repo> done modules
```
O estágio `modules` não está concluído apenas porque todos os `modules/*.md`
existem; esses arquivos são insumo intermediário, não wiki final.

## Estágio 3 — Rules
🤖 **M** obrigatório para a escrita do conteúdo (papel `Detective`); as três
chamadas de comando abaixo são 👤/🤖 **D** (abrem/fecham manifesto e
mesclam blocos), exceto `done rules` que é 👤 **H** (mesmo julgamento do
"Portão de qualidade").

Papel: `Detective`. Lê `modules/*.md`, não o repo (só `read` pontual para citar). Extrair: regras de
negócio implícitas, máquinas de estado, permissões, ADRs retroativos (git log:
fix/hotfix, reverts), lógica circular/morta, contradições.
Artefatos (§5): `sdd/domain.md`; se `doc_level` ≥ completo, também
`sdd/state-machines.md`, `sdd/permissions.md` e `sdd/adrs/`.
Subagente entrega um bloco por artefato, nome fixo (`=== RULES: domain ===`,
`=== RULES: state-machines ===`, `=== RULES: permissions ===` — ver "Brief
compacto" acima); nome fora dessa lista faz o merge rejeitar o bloco. Depois
rode (D, D, H nesta ordem):
```bash
{{WK}} code --repo <repo> run-stage rules
{{WK}} code --repo <repo> merge-agent-output rules --input <output-do-batch> --agent <id-do-subagente>
{{WK}} code --repo <repo> done rules
```
`run-stage rules` é obrigatório quando houver geração por subagente; grava
`agent-runs/rules.json` (schema `wiki-ai.agent-runs.v2`).

## Estágio 4 — Architecture
🤖 **M** obrigatório para a escrita do conteúdo (papel `Architect`); as
chamadas de comando são 👤/🤖 **D**, exceto `done architecture` (👤 **H**).

Papel: `Architect`. Lê `modules/*.md` + artefatos do estágio 3, não o repo. Produz por `doc_level`:
- `sdd/architecture.md`
- `sdd/c4-context.md` (+ `c4-containers`, `c4-components` se ≥ completo)
- `sdd/erd-complete.md` (≥ completo)
- `sdd/traceability/spec-impact-matrix.md` (≥ completo)
- `sdd/sequences/` (detalhado)
Subagente entrega um bloco por artefato, nome fixo (`=== ARCHITECTURE:
architecture ===`, `c4-context`, `c4-containers`, `c4-components`,
`erd-complete` — ver "Brief compacto" acima).

**Diagrama Mermaid é obrigatório** nesses 5 artefatos — `audit` reprova sem
ele:

| Artefato | Tipo de bloco ```mermaid``` exigido |
|---|---|
| `sdd/architecture.md` | `flowchart` ou `graph` |
| `sdd/c4-context.md` / `c4-containers.md` / `c4-components.md` | `flowchart` \| `graph` \| `C4Context` \| `C4Container` \| `C4Component` |
| `sdd/erd-complete.md` | `erDiagram` |

Diagrama ausente sem escape = **blocker** (`score -25`). Escape auditável —
`<!-- no-diagram: <motivo> -->` no corpo, motivo obrigatório e não-vazio —
vira **warning** (`score -5`), nunca bypass silencioso. Erro de sintaxe
Mermaid sempre traz linha + trecho + nome do padrão violado na mensagem;
corrija o trecho apontado, não redesenhe o diagrama do zero.

D, D, H nesta ordem:
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
👤 **H** para decidir a lista de units abaixo (`pending specs --items`); 🤖
**M** obrigatório para a escrita das specs e da revisão final; comandos de
abertura/fechamento de manifesto voltam a ser 👤/🤖 **D**.

Substitua `<u1>,<u2>` pelos nomes reais das units (ex.: nomes de endpoint/
feature, conforme a `granularity` escolhida no Estágio 1):
```bash
{{WK}} code --repo <repo> pending specs --items "quote-creation,quote-approval"
```
Como saber que deu certo: a saída ecoa `{"pending": ["quote-creation",
"quote-approval"], ...}` — exatamente a lista informada, prova de que o
comando não infere nada (`cmd_pending`, `scripts/codescan/cli.py` (função `cmd_pending`)).
Escolher mal as units aqui (granularidade errada, unit faltando) só aparece
como lacuna na revisão final — decida com cuidado.

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
Para fechar (D, depois H):
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
👤 ou 🤖 **D**, integralmente — sem `run-stage`, sem fan-out, sem
subagente — `evidence` não está na constante `CRITICAL_STAGES` de
`scripts/codescan/sdd.py` e a função `cmd_evidence` em
`scripts/codescan/cli.py` só faz busca lexical no repo, sem LLM.
`run-stage`/`merge-agent-output`/`agent-pack`/`redo` com
`stage=evidence` falham com JSON em stderr — `{"error": "'evidence' não é
um estágio válido para '<comando>' ...", "acao": "... rode \`wk code
evidence --topic <topico>\` diretamente"}` — e **exit 2**, não o `argparse:
invalid choice` cru.
```bash
{{WK}} code --repo <repo> evidence --topic codebases/insurance-quote-service
```
Saída esperada: `{"artifact": ".../evidence-....json", "items": 48,
"warnings": []}`. Como saber que deu certo: `items > 0`. Pacote em
`<workdir>/evidence-<slug>.json`. Vazio → não sintetize; refine o tópico ou
cave à mão (👤, ainda sem modelo — é ajuste de parâmetro, não geração de
conteúdo).

## Estágio 7 — Synth
🤖 **M** obrigatório para a escrita de `confirmed.md`/`inferred.md` (papel
`Synthesis Writer`); `run-stage`/`merge-agent-output` são 👤/🤖 **D**;
`done synth` e a decisão de publicar são 👤 **H**; `verify` (checador
mecânico) é **D**.

Papel: `Synthesis Writer`. Lê `sdd/*.md` de primeiro nível (não o repo).
Local **canônico**: `sdd/confirmed.md` e `sdd/inferred.md` (nunca uma cópia na
raiz do workdir — `audit` reprova como P0 se as duas versões divergirem; se
existir cópia na raiz, apague-a e mantenha só `sdd/`). Não há `questions.md`
no contrato do código.

`sdd/confirmed.md` → só 🟢 com `arquivo:linha`; ao publicar/promover, mesmo
sendo o artefato mais verificável da síntese, entra como `agent-output` (só
`inventory.md`/`dependencies.md`/`coupling.md`/`coupling.html` do `export`
são `code-repo` — `coupling.html` entra como asset, ver `publish` abaixo).
`sdd/inferred.md` → 🟡, `agent-output`, nunca auto-promove.

O CLI aceita `synth` em `run-stage`/`merge-agent-output`
(`choices=(modules, rules, architecture, specs, synth)`). Subagente entrega
um bloco por artefato, nome fixo (`=== SYNTH: confirmed ===`, `=== SYNTH:
inferred ===` — ver "Brief compacto" acima); nome fora dessa lista faz o
merge rejeitar o bloco. Fluxo fechado, igual aos demais estágios (D, D, H):
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

Validar antes de publicar/promover — 👤/🤖 **D**, checador mecânico de
citação `arquivo:linha` (`cmd_verify`, `scripts/codescan/cli.py` (função `cmd_verify`),
não tenta provar semântica):
```bash
{{WK}} code --repo <repo> verify --artifact <workdir>/sdd/confirmed.md
```
Saída esperada: `{"ok": true, "checados": 112, "falhas": []}` (exit 0) ou
`{"ok": false, "falhas": [{"linha": "...", "motivo": "..."}]}` (exit 1).
Como saber que deu certo: `ok: true`. Falha → rebaixa para `inferred.md` ou
vira pergunta (👤 decide qual — **H**).

Depois, leve a árvore inteira para `inbox/` com `publish` (§6 do
`sdd-contract`) — não copie arquivo por arquivo com `ingest`. 👤/🤖 **D**:
classificação fixa por caminho + cópia de arquivo, sem LLM (`cmd_publish`,
`scripts/wk/cli.py` (função `cmd_publish`) em diante):
```bash
{{WK}} publish --workdir <workdir> --topic codebases/insurance-quote-service --store <store>
```
`publish` classifica sozinho: `sdd/inventory.md`, `sdd/dependencies.md`,
`sdd/coupling.md` e `sdd/coupling.html` (quando o motor Java rodou) como
`code-repo` em `inbox/code-notes/`; todo o resto de `sdd/**/*.md` e
`modules/*.md` (síntese incluída) como `agent-output` em
`inbox/agent-output/`. Scripts auxiliares (`*.py`), `state.json` e
`surface.json` nunca entram — o comando já os exclui. `sdd/coupling.html`
entra como **asset**, não como Markdown: bytes originais copiados para
`raw/assets/<doc_id>.html`, com um stub `.md` de proveniência ao lado
(`doc_id` com sufixo `-html`, para não colidir com o `doc_id` de
`coupling.md`). `wk compile` linka o asset a partir da página da fonte e
do overview do tópico; `wk docx` nunca converte esse `.html`.

`publish` nunca bloqueia por `verify` falho — é staging em `inbox/`, nada
vira canônico ali; só anota `aviso_verify` no JSON de saída quando o
workdir de origem tem `verify` falho. O gate real é em `promote` (por item,
escopado por `topic`, cobrindo também os artefatos `code-repo`) e em
`compile`/`docx` (escopados pelo `topic` do comando) — override consciente
via `--allow-unverified` (ver `operations/promote.md`). Depois: `promote`
(👤 **H** para tudo que não for `code-repo` — ver `operations/promote.md`),
`compile` (👤/🤖 **D** — ver `operations/compile.md`). Repo remoto:
`cleanup` para apagar o clone — 👤/🤖 **D**, só remove diretório local.

## Retomada — 👤/🤖 D
```bash
{{WK}} code --repo <repo> state    # next para continuar
```
Saída esperada: JSON com `stages` e o status de cada um (`pending`/
`in_progress`/`done`/`failed`). Como saber que deu certo: o estágio em que
você parou aparece como `in_progress` ou o próximo como `pending` — não
refaça estágios já `done`. Após 3+ módulos/units na sessão, ofereça pausa
(`/clear` + retomar).

## Não faça
- Não escreva no repositório, nem estado.
- Não marque 🟢 sem linha.
- Não pule `plan`.
- Não copie o codebase para o workdir/wiki.
- Não use scripts temporários para gerar SDD; use comandos `wk.pyz`.
- Não leia o repo nos estágios 3–5.
- Não misture 🟡 e 🟢 em `confirmed.md`/`inferred.md`.
- Não gere todas as specs numa resposta só.
