# wiki-ai

## 1. O que é

| # | Descrição |
|---|---|
| 1 | CLI que investiga um repositório com um agente LLM (Claude ou Codex) e constrói uma base de conhecimento com proveniência (entidades, relações, evidências ligadas a arquivo/linha). |
| 2 | Publica essa base como documentos DOCX estruturados (`wiki-ai publish`), prontos para indexação em SharePoint/Copilot Studio. |
| 3 | Princípio: **o LLM investiga, o harness governa** — o agente só age através de ferramentas MCP controladas pelo produto; toda afirmação publicável exige evidência rastreável (`src/wiki_ai/knowledge/`), sem bypass de permissões do CLI do agente. |

## 2. Instalação

| Quando | Onde | O quê | Como saber que terminou |
|---|---|---|---|
| 🤖 Antes de qualquer uso | Raiz do repo | `pip install -e .` (requer Python ≥3.12; `dependencies = []` em `pyproject.toml` — stdlib apenas) | comando retorna sem erro |
| 🤖 Verificação | Terminal | `wiki-ai version` | saída JSON `{"status":"ok","command":"version","version":"2.0.0a0"}` |

## 3. Providers de agente

| Aspecto | Evidência | Detalhe |
|---|---|---|
| Descoberta | `agent/registry.py::ProviderRegistry` | `Wiring()` cria `ProviderRegistry(adapters=True)`, que instala `wiki_ai.agent.providers.install_all` — registra `claude` e `codex` |
| Binário | `agent/providers/claude.py` / `codex.py` | `CliRunner.locate(BINARY)` procura `claude` / `codex` no `PATH`; `connect()` roda `<binário> --version` |
| Sem provider disponível | `app/api.py::analyze/ask/publish` | `provider is None` → relatório `status: "blocked"`, `reason: "agent_provider_unavailable"`, `action: "configure a supported provider"` (exit code 2, ver §4) |
| Flags — Claude | `agent/providers/claude.py::_argv` | `-p <prompt> --output-format json --mcp-config <config> --allowedTools <tools MCP qualificados> --disallowedTools Bash Edit Write MultiEdit NotebookEdit WebFetch WebSearch --max-turns <n>` (sem `--bypassPermissions`) |
| Flags — Codex | `agent/providers/codex.py::_argv` | `exec --sandbox read-only -C <workspace> --skip-git-repo-check --json` + config TOML com `enabled_tools` (allowlist) e `shell_environment_policy.inherit = "none"` |
| Allowlist MCP | `agent/providers/bridge.py` (`SERVER_NAME = "wiki"`) | Cada provider só habilita as ferramentas MCP do namespace `wiki.*` construídas pela sessão (`session.tool_names()` + `wiki.finish`) |
| Variável de ambiente | `app/session.py::HOME_VARIABLE = "WIKI_AI_HOME"` | Se definida, o estado do repositório fica em `<WIKI_AI_HOME>/<identity>` em vez de `<repo>/.wiki-ai/` |
| Timeout padrão | `claude.py`/`codex.py` | `_DEFAULT_TIMEOUT = 900.0` segundos por execução do agente |

## 4. Fluxo de uso

| Comando | Quando usar | O que faz (1 linha) | Campos principais da saída JSON | Exit codes | Como saber que terminou |
|---|---|---|---|---|---|
| `wiki-ai analyze <repo> [--objective TXT]` | Primeira análise ou após mudanças no repo | Tira snapshot, compara com o anterior e roda investigação (nova ou incremental) | `status`, `snapshot_digest`, `analyzable_files`, `analysis_status`, `reason` (`up_to_date` se nada mudou), `details` | 0 ok / 1 erro / 2 bloqueado | JSON impresso em stdout com `status` |
| `wiki-ai ingest <source> --repo <repo>` | Adicionar uma fonte externa (doc, planilha, transcrição, diagrama) | Detecta o formato, extrai blocos e grava `SourceVersion` no knowledge store | `status` (`ok`/`partial`/`blocked`), `source`, `kind`, `version_hash`, `registered`, `ingestion_status` | 0/1/2 | `registered: true` e `status` no JSON |
| `wiki-ai ask "<pergunta>" --repo <repo>` | Consultar a base já investigada | Roda `Answerer` sobre o `KnowledgeRepository`; bloqueia se a base estiver vazia | `status`, `answer`, `mode`, `evidence_ids`, `entity_ids`, `unresolved` | 0/1/2 | `status: "ok"` com `answer` preenchido |
| `wiki-ai publish --repo <repo>` | Gerar os documentos DOCX para publicação | Planeja documentos, monta narrativa, renderiza DOCX, valida pacote e promove release | `status`, `publication_id`, `artifacts`, `manifest_hash` | 0/1/2 | `status: "ok"` e `publication_id` presente |
| `wiki-ai status --repo <repo>` | Ver estado atual sem alterar nada | Lê contadores do knowledge store e da última publicação | `entities`, `relations`, `evidence`, `sources`, `pending_update`, `last_publication`, `provider_available` | 0 (sempre `ok`) | Sempre retorna; `pending_update: true` indica que `analyze` deve rodar de novo |
| `wiki-ai inspect [<repo>]` | Ver inventário de arquivos sem tocar no knowledge store | Faz snapshot + inventário (classificação/linguagem), sem investigar | `total_files`, `analyzable_files`, `by_classification`, `by_language` | 0/1/2 | JSON com `total_files` |
| `wiki-ai version` | Checar versão instalada | Retorna `__version__` do pacote | `status`, `version` | 0 | `status: "ok"` |

Exit codes (`app/commands.py`): `EXIT_OK=0`, `EXIT_ERROR=1` (payload `status:"error"`), `EXIT_BLOCKED=2` (payload `status:"blocked"`, sempre com `reason`/`action`).

### Ciclo de atualização

| Etapa | Evidência | Comportamento |
|---|---|---|
| 1ª execução de `analyze` | `app/api.py::analyze` | Não há snapshot anterior → investigação completa com o `objective` informado (ou `DEFAULT_OBJECTIVE = "describe how this system works"`) |
| Execução repetida sem mudanças | `analyze`, condição `previous.digest == snapshot.digest` | Retorna `status:"ok"`, `reason:"up_to_date"`, sem rodar o agente |
| Execução após mudanças no repo | `analyze`, `diff(previous, snapshot).is_empty()` falso | Roda `_update_report` → `UpdateEngine` (investigação incremental sobre o diff), invalida conhecimento afetado |

### Estados tipados de saída

Evidência: `app/ports.py::OutcomeStatus`, `app/session.py::AnalysisStatus`, `investigation/orchestrator.py::InvestigationStatus`, `ingestion/outcome.py::IngestionStatus`, `publishing/answer.py::AnswerMode`.

| Comando | Campo | Valores | Evidência |
|---|---|---|---|
| `analyze` | `status` | `ok`, `partial`, `blocked`, `error` | `app/api.py::REPORT_STATUS_BY_OUTCOME` (mapeia `OutcomeStatus` do runner) |
| `analyze` | `analysis_status` | `never`, `complete`, `partial`, `blocked`, `failed` | `app/api.py::ANALYSIS_STATUS_BY_OUTCOME`, `app/session.py::AnalysisStatus` |
| `ingest` | `status` | `ok`, `partial`, `blocked` | `app/api.py::REPORT_STATUS_BY_OUTCOME` |
| `ingest` | `ingestion_status` | `complete`, `structural_only`, `partial`, `blocked`, `failed` | `app/ports.py::OutcomeStatus`; `structural_only` quando não há provider de agente (`ingest`, `STRUCTURAL_ONLY`) |
| `ask` | `mode` | `agentic`, `deterministic_fallback` | `publishing/answer.py::AnswerMode` |
| `ask` | `reason` | texto livre (ex.: `knowledge_empty`) | `app/api.py::AskReport` |

Como interpretar cada estado / o que fazer:

| Estado | Significa | O que fazer |
|---|---|---|
| `status: ok` | Execução completa sem lacunas | Nenhuma ação — resultado utilizável |
| `status: partial` | Execução concluiu mas com lacunas (gaps) reportadas em `details`/`diagnostics` | Ler `details`/`reason`; decidir se as lacunas são aceitáveis ou exigem nova fonte/execução |
| `status: blocked` | Pré-condição ausente (provider, biblioteca, base vazia) | Ler `reason`/`action` no JSON e resolver a causa (ex.: instalar dependência, configurar provider) antes de repetir |
| `status: error` | Falha de execução (`OutcomeStatus.FAILED`) | Investigar a causa raiz (log/exceção); execução não produziu resultado confiável |
| `analysis_status: never` | Repositório nunca foi analisado com sucesso | Rodar `wiki-ai analyze` |
| `analysis_status: complete` | Última análise concluiu sem bloqueios/lacunas para o `analyzed_digest` atual | `reason: up_to_date` em execuções repetidas sem mudanças |
| `analysis_status: partial`/`blocked`/`failed` | Análise não concluiu integralmente; `analyzed_digest` não avança para o novo snapshot | Resolver a causa (`reason`/`action`) e rodar `analyze` novamente — só `complete` avança `analyzed_digest` |
| `ingestion_status: structural_only` | Extração estrutural ocorreu, mas sem investigação semântica (sem provider de agente) | Configurar um provider para enriquecimento semântico, se necessário |
| `mode: agentic` | `ask` usou o agente para responder | — |
| `mode: deterministic_fallback` | `ask` respondeu sem agente, só com dados determinísticos do knowledge store | Configurar provider para respostas mais completas, se necessário |

`up_to_date` (`reason` de `analyze`) só é retornado quando o `analysis_status` da última execução é `complete` para o mesmo `snapshot_digest` (`app/session.py::AnalysisState.is_current_for`) — uma análise `partial`/`blocked`/`failed` não marca o repositório como atualizado, mesmo sem mudanças no digest.

## 5. Formatos de fonte suportados

Evidência: `ingestion/adapters/registry.py::EXTENSION_KINDS`, `ingestion/adapters/pdf.py`, `ingestion/adapters/blocks.py::IMAGE_GAP_CODE`, `ingestion/outcome.py`.

| Formato | Extensões | O que é extraído | Locator | Limitações |
|---|---|---|---|---|
| DOCX | `.docx`, `.docm` | Títulos, parágrafos, listas, tabelas (via `adapters/docx.py`) | posição no documento | — |
| XLSX | `.xlsx`, `.xlsm` | Células e folhas (via `adapters/xlsx.py`) | referência de célula (`cellref.py`) | — |
| Draw.io | `.drawio`, e PNG/SVG embutidos (`*.drawio.png/svg`) | Nós e relações do diagrama | id do nó/aresta | — |
| PDF | `.pdf` | Texto extraído por página via `pypdf` (`LibraryTextExtractor`) | página/posição | Requer `pip install "wiki-ai[pdf]"`; sem a biblioteca → `blocked` `pdf_library_unavailable` (`StructuralFault.PDF_LIBRARY_UNAVAILABLE`, em `BLOCKING_FAULTS`); páginas sem texto → `partial` `image_content_not_interpreted` (`Gap.IMAGE_CONTENT_NOT_INTERPRETED`, em `PARTIAL_GAPS`; OCR não incluído); documentos malformados → `failed` `malformed_document` (`StructuralFault.MALFORMED_DOCUMENT`, fora de `BLOCKING_FAULTS` — `ingestion/integration.py::derive_status`) |
| Transcrição | `.vtt`, `.srt`, `.txt` (se detectado como fala) | Falas por locutor/timestamp (heurística `_SPEAKER_LINE`, `_looks_srt`, `_looks_transcript`) | timestamp/locutor | Detecção heurística — texto livre sem marcação de locutor pode não ser reconhecido como transcrição |
| Markdown | `.md`, `.markdown` | Seções e blocos de texto | posição no documento | — |
| HTML | `.html`, `.htm` | Estrutura textual | posição no DOM | — |
| JSON | `.json`, `.jsonl`, `.ndjson` | Estrutura de dados | caminho JSON | — |
| XML | `.xml` | Estrutura de dados | caminho XML | — |
| Codebase | (diretório) | Snapshot + investigação do agente (`analyze`) | arquivo/linha | — |
| Imagens puras (PNG/SVG não-drawio) | `.png`, `.svg` | Nenhuma | — | Gap declarado — formato não suportado (`UNSUPPORTED_FORMAT`) |

## 6. O que o usuário não precisa conhecer

Store, leases e bindings internos (`knowledge/repository.py`, `repository/snapshot.py`) são detalhes de implementação. O que importa saber é onde os dados ficam:

| Caminho | Conteúdo | Evidência |
|---|---|---|
| `<repo>/.wiki-ai/state.db` | Banco de conhecimento (entidades, relações, evidências, fontes) | `app/session.py::DATABASE_FILE` |
| `<repo>/.wiki-ai/snapshots/` | Snapshots do repositório por digest + `latest.json` | `app/session.py::SUBDIRECTORIES`, `LATEST_FILE` |
| `<repo>/.wiki-ai/publications/releases/<publication_id>/` | Release publicada: arquivos `.docx` + `manifest.json` | `publishing/release.py` (`RELEASES_DIRNAME`), `publishing/pipeline.py::Publisher.run` |
| `<repo>/.wiki-ai/publications/current` | Ponteiro para a release atual | `publishing/release.py::current` |

Se `WIKI_AI_HOME` estiver definida, tudo acima fica em `<WIKI_AI_HOME>/<identity>/` em vez de `<repo>/.wiki-ai/` (`app/session.py::state_dir_for`, `HOME_VARIABLE = "WIKI_AI_HOME"`).

### Snapshot imutável

Evidência: `repository/store.py::SnapshotStore`.

| Aspecto | Evidência | Detalhe |
|---|---|---|
| Blobs content-addressed | `store.py::SnapshotStore.put`, `_blob_path` | Cada arquivo do snapshot é gravado em `.wiki-ai/snapshots/blobs/<sha256[:2]>/<sha256>` (`BLOBS_DIRECTORY = "blobs"`); escrita atômica via arquivo `.pending` + `os.replace` |
| Manifesto por digest | `store.py::_manifest_path`, `_write_manifest` | `.wiki-ai/snapshots/<digest>/manifest.json` (`MANIFEST_NAME = "manifest.json"`) lista os blobs que compõem aquele snapshot |
| Leitura sempre pelo snapshot | `store.py::SnapshotStore.open`, `.materialize` | O agente e os runners leem o conteúdo dos arquivos a partir do blob armazenado (`open(sha256)`), nunca do working tree diretamente — o snapshot já tirado é a fonte imutável para toda a investigação |
| Criação automática do `.gitignore` | `app/session.py::Session.prepare`, `write_ignore` | Ao abrir uma sessão, se `<state_dir>/.gitignore` não existir, é criado com `IGNORE_CONTENT = "*\n"` (ignora todo `.wiki-ai/` do controle de versão do repositório) |
| Localização alternativa do estado | `app/session.py::HOME_VARIABLE = "WIKI_AI_HOME"`, `state_dir_for` | Se a variável de ambiente `WIKI_AI_HOME` estiver definida (e não vazia), todo o estado (`state.db`, `snapshots/`, `publications/`) fica em `<WIKI_AI_HOME>/<identity>/` em vez de `<repo>/.wiki-ai/` |

### Detecção de store antigo

Evidência: `app/session.py::detect_outdated_store`, `_codescan_signature`, `_docx_signature`; `app/api.py::_checked_root` (usado por `inspect`, `analyze`, `ingest`, `ask`, `publish`).

| Assinatura | Condição inequívoca | Evidência |
|---|---|---|
| `.codescan` | Diretório `<repo>/.codescan/` existe **e** contém `state.db` ou `manifest.json` (`CODESCAN_FILES`) | `_codescan_signature` |
| `wiki-docx` | Diretório `<repo>/wiki-docx/` existe **e** os diretórios companheiros `raw/` e `wiki/` (`COMPANION_DIRECTORIES`) também existem em `<repo>` | `_docx_signature` |

Quando qualquer assinatura é encontrada, `_checked_root` levanta `OutdatedStore(markers)` e o comando não prossegue — só a presença de arquivo/diretório isolado (sem a combinação exigida) não é suficiente para classificar o repositório como store antigo.

## 7. Publicação SharePoint/Copilot Studio

| Aspecto | Evidência | Detalhe |
|---|---|---|
| Estrutura do DOCX (capability) | `publishing/model.py::CAPABILITY_SECTIONS` | 15 seções: Objetivo, Como funciona, Fluxo principal, Inputs, Outputs, Regras de negócio, Invariantes, Edge cases, Integrações, Persistência, Falhas e recuperação, Diagramas, Testes e evidências, Lacunas conhecidas, Rastreabilidade técnica |
| Outros tipos de documento | `publishing/model.py::SYSTEM_SECTIONS/CATALOG_SECTIONS/GAPS_SECTIONS/CHANGE_IMPACT_SECTIONS` | Visão de sistema, catálogo de integrações/regras, relatório de lacunas e impacto de mudança — cada um com seu próprio conjunto de seções, todos terminando em "Lacunas conhecidas" e "Rastreabilidade técnica" |
| O que é pesquisável | `publishing/render_docx.py`, `docx_structure_problems` (`publishing/pipeline.py`) | Texto, títulos, parágrafos e tabelas nativos do DOCX (`heading_count`, `paragraph_count` > 0 exigidos); diagramas exigem equivalente textual (`DIAGRAM_WITHOUT_TEXT` bloqueia publicação sem ele) |
| Gate de confirmação | `publishing/gate.py::check` | Verifica manifest legível, DOCX presente/válido, manifest batendo com o pacote (`validate_package`), artefatos obrigatórios presentes, diagramas com equivalente textual, e lacunas bloqueantes não escondidas por métricas de completude |

## 8. Qualidade / gates para quem desenvolve

| Comando | Quando rodar | O que verifica |
|---|---|---|
| `python -m pytest tests` | Antes de qualquer commit/PR | Suíte de testes completa (`pyproject.toml::[tool.pytest.ini_options]`, `testpaths = ["tests"]`) |
| Gate de higiene (`quality/source_hygiene.py::scan`) | CI/local | Zero comentários (`#`, `//`, `/* */`, `<!-- -->`), zero docstrings, zero marcadores (`TODO`, `FIXME`, `HACK`, `NOTE`, `XXX`) e diretivas de supressão (`type: ignore`, `noqa`, `pragma`, `pylint:`, `fmt:`) em código Python/JS/TS/Java/Go/C/C#/HTML/XML |
| Gate de arquitetura (`quality/architecture.py::check`) | CI/local | Ver regras abaixo |
| Gate de código morto (`quality/dead_code.py::check`) | CI/local | Símbolo (função/método/classe/constante/membro de enum) definido em `src/wiki_ai/` sem uso fora da sua própria definição (`_external_uses`); exceções: métodos que sobrescrevem base (`_overridden_methods`), membros de enum construídos por valor (`_enum_classes_built_by_value`), nomes referenciados por teste que casam com `public_api` de `vocabulary.json`, e símbolos listados em `dead_code_allow` (`symbol` + `reason` obrigatórios) |
| Benchmark semântico (`tests/benchmark/runner.py`) | Validar qualidade de investigação por provider/repositório | `python -m tests.benchmark.runner --provider all --repo all --out <json>` — roda `analyze` sobre corpora de verdade conhecida e mede com `metrics.evaluate` (`BenchmarkReport`/`MetricScore`); exit `3` (`EXIT_SKIPPED`) quando o binário do provider está ausente (`SKIP_REASON = "provider_binary_unavailable"`), exit `1` em erro, exit `0` ao concluir |
| Conformidade entre providers (`tests/benchmark/conformance.py`) | Validar que providers produzem conhecimento equivalente | `python -m tests.benchmark.conformance --repo all --out <json>` — roda `analyze` com `claude` e `codex` (`PAIR`) sobre o mesmo repositório e compara `KnowledgeShape` (`compare_shapes`/`compare_metrics`); mesmos exit codes de `runner.py` (`3` sem binário, `1` erro, `0` ok) |

### Regras do gate de arquitetura

| Regra | Enum | Evidência |
|---|---|---|
| Arquivo de produção ≤ 1000 linhas | `FILE_TOO_LONG` | `vocabulary.json::max_production_file_lines = 1000` |
| Sem ciclos de import internos | `IMPORT_CYCLE` | SCC (Tarjan) sobre o grafo de imports do pacote |
| Matriz de imports por camada | `FORBIDDEN_DIRECTION` | `vocabulary.json::layers`: `app` importa tudo; `investigation` → `repository, knowledge, agent`; `publishing` → `knowledge`; `ingestion` → `knowledge, agent`; `knowledge`, `repository`, `agent`, `quality` não importam outras áreas do domínio |
| Provider concreto isolado | `CONCRETE_PROVIDER_IN_CORE` | Só `wiki_ai.agent.providers.*` pode importar de si mesmo; nenhum outro módulo pode |
| Termos banidos no código | `BANNED_TERM` | `vocabulary.json::banned_terms`: `legacy`, `legado`, `compat`, `deprecated` |
| Nome de provider fora do adapter | `PROVIDER_NAME_OUTSIDE_ADAPTER` | `claude`, `codex`, `devin`, `cursor`, `windsurf`, `antigravity`, `anthropic`, `openai` só podem aparecer dentro de `agent/providers/` |
| Pacotes removidos ausentes | `REMOVED_PACKAGE` | `scripts`, `scripts/codescan`, `scripts/sbindex`, `scripts/wk` não podem existir |
| Comandos removidos ausentes | `REMOVED_COMMAND` | `promote`, `compile`, `docx`, `finish`, `ingest-legacy`, `migrate`, `code`, `index`, `search`, `get`, `audit`, `lint`, `check`, `engines`, `store` não podem ser subcomandos nem console scripts |
| Único entrypoint | `MULTIPLE_ENTRYPOINTS` | Apenas o console script `wiki-ai` é permitido em `pyproject.toml`; apenas um `__main__.py` no pacote |

Execução: `check(src_root, repo_root)` (`quality/architecture.py`), `scan(paths)` (`quality/source_hygiene.py`) e `check(src_root, tests_root)` (`quality/dead_code.py`) retornam listas vazias quando o repositório está em conformidade.
