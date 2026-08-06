# Plano De Execução Técnico

## 0. Regra Operacional

- Executor obrigatório: Git Bash.
- Python obrigatório:
  ```bash
  /c/Users/User/AppData/Local/Programs/Python/Python313/python.exe
  ```
- Proibido:
  - PowerShell.
  - scripts temporários no `store/.codescan`.
  - `*.py` no workdir do codescan.
  - `*.txt` operacional solto no workdir.
  - edição manual de artefatos SDD.
  - agente principal gerar conteúdo final.
  - agente principal substituir subagente.
  - `audit` retornar `pass` com lixo estrutural.
  - `audit` retornar `100` com warning.

## 1. Papéis

| Papel | Responsável | Função | Escopo De Edição |
|---|---|---|---|
| Orquestrador | Agente principal | planejar, delegar, integrar, validar | sem geração SDD |
| Subagente A | Flow Control | CLI, estado, merge, fluxo | `scripts/codescan/cli.py`, testes CLI |
| Subagente B | Quality Gates | audit strict, higiene, score | `scripts/codescan/sdd.py`, testes audit |
| Subagente C | Diagram Engine | Mermaid, coupling, C4, ERD | `scripts/codescan/coupling.py`, testes diagramas |
| Subagente D | Token Budget | `agent-pack v2`, limites, prompts | `scripts/codescan/cli.py`, docs/testes |
| Subagente E | Docs/UX | README, operações, contrato | `README.md`, `operations/`, `references/` |

## 2. Falhas P0

| Falha | Gate |
|---|---|
| `*.py` em `store/.codescan/<repo>` | fail |
| `*.txt` operacional solto em `store/.codescan/<repo>` | fail |
| `src/` em `store/.codescan/<repo>` | fail |
| `modules/**/` diretório vazio | fail |
| `sdd/specs/*/` vazio | fail |
| `sdd/specs/_unit` | fail |
| spec órfã fora do estado | fail |
| stage `done` sem `agent-runs` obrigatório | fail |
| Mermaid com label inválido | fail |
| Mermaid sem nó ou sem aresta | fail |
| `coupling.md` sem grafo quando `modules >= 2` | fail |
| imports internos sem arestas no coupling | fail |
| geração manual pós-subagente | fail |
| comando destrutivo fora de cleanup seguro | fail |

## 3. Arquitetura Alvo

```text
surface
  -> export
  -> plan
  -> agent-pack v2
  -> subagentes
  -> merge-agent-output
  -> done item
  -> consolidate stage
  -> audit --strict
  -> next stage
```

```text
Agente principal:
  - não escreve SDD
  - não cria script auxiliar
  - não edita artefato gerado
  - delega
  - integra resposta parseável
  - valida
```

## 4. Subagente A: Flow Control

### Implementado

- `code merge-agent-output`
  ```bash
  wk.pyz code --repo <repo> --store <store> merge-agent-output modules --input <file>
  wk.pyz code --repo <repo> --store <store> merge-agent-output specs --input <file>
  ```

- Contrato `modules`:
  ```text
  === MODULE: <path> ===
  ...
  === END ===
  ```

- Contrato `specs`:
  ```text
  === SPEC: <unit> ===
  --- requirements.md ---
  ...
  --- design.md ---
  ...
  --- tasks.md ---
  ...
  === END ===
  ```

- `merge-agent-output`:
  - parseia blocos.
  - rejeita prosa fora de blocos.
  - rejeita bloco duplicado.
  - normaliza `/` e `\`.
  - grava artefato canônico.
  - registra `agent-runs`.
  - executa validação do item.
  - atualiza estado.

- `code run-stage`
  ```bash
  wk.pyz code --repo <repo> --store <store> run-stage modules
  wk.pyz code --repo <repo> --store <store> run-stage rules
  wk.pyz code --repo <repo> --store <store> run-stage architecture
  wk.pyz code --repo <repo> --store <store> run-stage specs
  ```

- `run-stage`:
  - gera `agent-pack v2`.
  - exige subagentes por papel.
  - integra somente via `merge-agent-output`.
  - grava `agent-runs/<stage>.json`.
  - executa `audit --strict`.
  - bloqueia fechamento manual sem provenance.

### Testes

- bloco inválido não grava.
- bloco válido grava.
- path Windows e POSIX geram mesmo slug.
- `done modules --item` resolve artifact correto.
- stage sem `agent-runs` não fecha.
- resposta com prosa fora de bloco falha.

## 5. Subagente B: Quality Gates

### Implementado

- `audit --strict`.
- `done` usa strict por padrão.
- Higiene do workdir:
  - `*.py` bloqueia.
  - `*.txt` operacional bloqueia.
  - `src/` bloqueia.
  - diretório vazio em `modules/**` bloqueia.
  - diretório vazio em `sdd/specs/*` bloqueia.
  - `_unit` bloqueia.
  - spec órfã bloqueia.
  - divergência estado x arquivos bloqueia.
  - `done` sem `agent-runs` obrigatório bloqueia.

- Score:
  - P0 => `score=0`.
  - blocker => `score<=60`.
  - warning => `score<=95`.
  - `100` só com zero warning e zero blocker.

### Testes

- workdir com `check_todo.py` falha.
- workdir com `todo_matches.txt` falha.
- `src/` falha.
- 22 specs vazias falham.
- `_unit` falha.
- spec órfã falha.
- warning impede `100`.
- `done specs` falha com pasta vazia.

## 6. Subagente C: Diagram Engine

### Implementado

- Sanitizador Mermaid:
  - IDs ASCII.
  - labels sempre quoted.
  - máximo 140 chars por linha.
  - máximo 25 nós por bloco.
  - proibir node ID duplicado.
  - proibir múltiplas arestas por linha.
  - proibir label bracket não escapado.

- Forma obrigatória:
  ```mermaid
  flowchart TD
    A001["Scheduled publishPendingBatch"] --> A002["Recover expired locks"]
  ```

- Proibir:
  ```mermaid
  flowchart TD
    A[@Scheduled publishPendingBatch] --> B[recoverExpiredLocks]
  ```

- Validador:
  - `flowchart`: mínimo 2 nós e 1 aresta.
  - `erDiagram`: mínimo 2 entidades ou 1 relacionamento.
  - `coupling`: Mermaid obrigatório quando `modules >= 2`.
  - Mermaid strict interno; parser/renderizador externo não é obrigatório.

- Refatorar `coupling`:
  - mapear package Java para módulo.
  - resolver imports por package/class.
  - fallback por diretório.
  - emitir grafo de dependências sempre que houver arestas.
  - falhar se houver imports internos e `edges=0`.

### Testes

- `A[@Scheduled]` reprova.
- flowchart sem aresta reprova.
- flowchart com linha densa reprova.
- ERD vazio reprova.
- coupling sem Mermaid reprova.
- coupling com imports Java gera aresta.
- C4 com excesso de nós reprova.

## 7. Subagente D: Token Budget

### Implementado

- `agent-pack v2`.

### Limites

| Item | Limite |
|---|---:|
| pack por subagente | 45 KB |
| linhas por arquivo | 40 |
| arquivos por módulo | 4 |
| módulos por batch | por orçamento |
| output por módulo | 120 linhas |
| output por stage | 220 linhas |
| citações por claim verde | obrigatório |
| código colado | 0 linhas |
| status do orquestrador | 3 linhas |
| erro sumarizado | 8 linhas |
| compactação por excesso de contexto | 0 |

### Seleção De Evidência

- Não incluir arquivo inteiro.
- Selecionar trechos por score:
  - entrypoint.
  - controller.
  - usecase.
  - domain.
  - repository.
  - gateway.
  - config.
  - exception.
  - producer/consumer.
  - tests relevantes.

- Incluir apenas:
  - assinatura.
  - annotations.
  - branches.
  - exceptions.
  - chamadas externas.
  - persistência.
  - publicação/consumo.
  - estados.

- Formato compacto:
  ```json
  {
    "path": "src/X.java",
    "symbols": ["CreateQuoteUseCase.create"],
    "citations": ["src/X.java:10-35"],
    "facts": ["persistencia", "evento", "erro"]
  }
  ```

### Redução De Input

- `plan` calcula batch por bytes.
- agente principal não lê `agent-pack`.
- subagente lê somente o próprio pack.
- contexto descartado após merge.
- logs resumidos por contadores.
- compactação por excesso de contexto vira incidente operacional.

### Redução De Output

- Subagente retorna somente blocos parseáveis.
- Proibido:
  - resumo.
  - metodologia.
  - repetir pack.
  - listar arquivos não usados.
  - colar código.
  - ecoar comando.
  - ecoar log.
  - explicar Mermaid.

### Testes

- pack <= 45 KB.
- nenhum excerpt > 40 linhas.
- nenhum módulo > 4 arquivos.
- saída com prosa fora de bloco falha.
- prompt contém limites numéricos.
- batches por bytes.

## 8. Subagente E: Docs/UX

### Atualizado

- `README.md`:
  - Git Bash obrigatório.
  - Python absoluto.
  - fluxo oficial.
  - `merge-agent-output`.
  - `audit --strict`.
  - token budget.
  - proibições.

- `operations/ingest-codebase.md`:
  - agente principal = orquestrador/validador.
  - subagentes obrigatórios.
  - sem execução manual.
  - sem scripts auxiliares.
  - sem PowerShell.
  - Mermaid rules.
  - `run-stage`.
  - `agent-runs` obrigatório.
  - e2e determinístico.
  - Mermaid strict sem parser externo obrigatório.
  - limites de compactação.

- `references/sdd-contract.md`:
  - P0/P1.
  - agent-run manifest.
  - specs sem pastas vazias.
  - diagram rules.
  - score real.

### Testes

- docs citam Git Bash.
- docs não orientam PowerShell.
- docs citam `merge-agent-output`.
- docs citam `audit --strict`.
- docs citam `45 KB`.

## 9. Enforcement Determinístico De Baixo Ruído

### Objetivo

- Reduzir tokens de chat.
- Eliminar eco de logs.
- Eliminar eco de comandos.
- Eliminar eco de diffs.
- Eliminar conteúdo de artefato recém-escrito.
- Tornar resposta de orquestrador/subagente validável.

### Contrato De Atualização Do Orquestrador

```text
STATUS: <ação curta>
RESULTADO: <ok|fail|blocked>
PROXIMO: <ação curta>
```

### Contrato Final Do Orquestrador

```text
ALTERADO:
- <arquivo>

VALIDADO:
- <gate/teste>: <ok|fail>

BLOQUEADO:
- <motivo>
```

### Proibições Determinísticas

| Padrão | Ação |
|---|---|
| `Ran command` | fail |
| `Running command` | fail |
| `Edited` | fail |
| `Write` | fail |
| `Wrote` | fail |
| `diff --git` | fail |
| `@@` | fail |
| `+++` | fail |
| `---` em diff | fail |
| log multiline colado | fail |
| saída completa de `grep`/`rg` | fail |
| stacktrace completo | fail |
| conteúdo de artefato recém-escrito | fail |
| comando repetido mais de 1 vez | fail |
| mesmo path repetido mais de 3 vezes | fail |

### Módulo Novo

```text
scripts/codescan/noise.py
```

### Funções

```python
validate_chat_update(text: str) -> NoiseReport
validate_agent_output(text: str) -> NoiseReport
summarize_command_result(stdout: str, stderr: str, max_lines: int = 8) -> str
redact_tool_noise(text: str) -> str
```

### Regras De Sumarização

| Entrada | Saída Permitida |
|---|---|
| comando OK | status sem stdout |
| comando FAIL | exit code + erro principal |
| stacktrace | exceção + primeira mensagem |
| `rg`/`grep` > 8 linhas | contagem por arquivo |
| teste longo | total + falhas |
| audit longo | score + blockers |
| build longo | artifact + status |

### Integração

- `merge-agent-output` rejeita resposta com ruído.
- `sdd-brief` inclui `no_tool_echo`.
- `compact_contract` inclui:
  - `no_command_echo`.
  - `no_tool_output_echo`.
  - `no_diff_echo`.
  - `no_written_artifact_echo`.
  - `max_status_lines=3`.
  - `max_error_lines=8`.
- subagente com eco de ferramenta não entra no SDD.
- resposta final fora do contrato reprova validação local.

### Testes

- `Ran command` reprova.
- `Running command` reprova.
- `Edited` reprova.
- `Write` reprova.
- `Wrote` reprova.
- `diff --git` reprova.
- `@@` reprova.
- stacktrace longo é resumido.
- `grep` longo vira contagem.
- subagente com eco de log falha no merge.
- resposta com artefato recém-escrito falha.

## 10. Validação Final

### E2E Determinístico

```bash
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe wk.pyz code --repo <repo> --store <store> surface --topic <slug>
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe wk.pyz code --repo <repo> --store <store> export --topic <slug>
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe wk.pyz code --repo <repo> --store <store> plan
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe wk.pyz code --repo <repo> --store <store> run-stage modules
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe wk.pyz code --repo <repo> --store <store> run-stage rules
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe wk.pyz code --repo <repo> --store <store> run-stage architecture
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe wk.pyz code --repo <repo> --store <store> pending specs --items "<u1>,<u2>"
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe wk.pyz code --repo <repo> --store <store> run-stage specs
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe wk.pyz code --repo <repo> --store <store> audit --strict
```

### Unitários

```bash
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe -m unittest discover -s scripts/codescan/tests -v
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe -m unittest discover -s scripts/sbindex/tests -v
```

### Build

```bash
/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe scripts/build_pyz.py
```

### Must Fail

- `check_todo.py` no workdir.
- `clean_todo.py` no workdir.
- `parse_modules.py` no workdir.
- `todo_matches.txt` no workdir.
- `sdd/specs/foo/` vazio.
- `modules/foo/bar/` vazio.
- Mermaid `A[@Scheduled x]`.
- `coupling.md` sem Mermaid.
- `stage done` sem `agent-runs`.
- warning com score `100`.

### Must Pass

- fluxo sem scripts.
- specs só com units registradas.
- coupling com grafo.
- flowcharts render-safe.
- audit sem falso positivo.
- pack abaixo do orçamento.

## 11. Critério De Aceite

| Critério | Meta |
|---|---:|
| scripts no workdir | 0 |
| specs vazias | 0 |
| modules dirs vazios | 0 |
| PowerShell no fluxo | 0 |
| subagentes por stage agent-output | 100% |
| audit falso positivo conhecido | 0 |
| Mermaid quebrado aceito | 0 |
| coupling sem grafo aceito | 0 |
| agent-pack por batch | <=45 KB |
| excerpt por arquivo | <=40 linhas |
| arquivos por módulo no pack | <=4 |
| compactação por excesso de contexto | 0 |
| eco de log no chat | 0 |
| eco de comando no chat | 0 |
| eco de diff no chat | 0 |
| `Ran command`/`Edited`/`Wrote` no output | 0 |
| warning com score 100 | 0 |
| score mínimo real | >=90 |

## 12. Ordem Pós-Aprovação

1. Spawn Subagente A.
2. Spawn Subagente B.
3. Spawn Subagente C.
4. Spawn Subagente D.
5. Spawn Subagente E.
6. Integrar patches.
7. Resolver conflitos.
8. Rodar testes unitários.
9. Rodar testes must-fail.
10. Rodar testes must-pass.
11. Rebuild `wk.pyz`.
12. Validar contra store problemático.
13. Reportar:
    - arquivos alterados.
    - gates implementados.
    - testes executados.
    - falhas bloqueadas.
    - pendências reais.
