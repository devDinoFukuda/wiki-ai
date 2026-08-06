# Validação de requisitos — wiki-ai (second-brain)

Benchmark: **aderência ao llm-wiki** (gist Karpathy 442a6bf5 — raw/→wiki/, index, log append-only, ingest→query→lint, LLM escreve/humano cura) **recebendo docs, planilhas, txt, transcrições e XML de arquitetura como fontes analisadas** (análise pelo LLM, não conversão mecânica), **mais análise de codebases replicando o reversa da sandeco** (github.com/sandeco/reversa, sem usar o pacote — contrato em `references/sdd-contract.md`). Execução do plano `docs/evolucoes.md` concluída em 2026-08-06; suíte com 293 testes verde; `wk.pyz` rebuildo.

## Veredito por requisito

| # | Requisito | Veredito |
|---|-----------|----------|
| 1 | Docs, planilhas, txt como fontes analisadas | ✅ **ATENDE** |
| 2 | Transcrições | ✅ **ATENDE** |
| 3 | Arquitetura de software em XML | ✅ **ATENDE** |
| 4 | Codebases estilo reversa | ✅ **ATENDE** |
| 5 | Mecanismos llm-wiki (index, wikilinks, lint, query→arquivo) | ✅ **ATENDE** |

## 1. Docs, planilhas, txt — ATENDE

- `.txt`/`.md`: passthrough → markdown com frontmatter de proveniência (`scripts/wk/cli.py`, `_convert_to_markdown`).
- `.docx`/`.xlsx`/`.csv`/`.pdf`: `wk ingest` grava o **original imutável** em `raw/assets/<id><ext>` (padrão llm-wiki) e cria página de fonte (stub) em `inbox/` com link para o asset e seção "Análise pendente" (`_ingest_asset`, `scripts/wk/cli.py:1628`). A análise é do LLM: lê o asset, escreve a página de análise e a ingere via `--source-type agent-output` → portão `promote` (nunca auto-promove) → `compile`. Fluxo documentado em `operations/ingest.md` e `SKILL.md`.
- Sem conversão mecânica e sem dependência externa (política zero-dependências preservada).

## 2. Transcrições — ATENDE

- `.vtt`/`.srt` → markdown com timestamps (`_convert_subtitles`), taxonomia `human-transcript`, roteamento `inbox/transcripts/`, promoção só com `--approve`, chunking recursivo para texto sem headings (`scripts/sbindex/chunker.py`). STT de áudio é externo por design.

## 3. Arquitetura em XML — ATENDE

`_convert_xml_architecture` (`scripts/wk/cli.py:1536`, stdlib `xml.etree`):
- **draw.io** (`mxfile`/`mxGraphModel`): componentes e relações em tabelas + Mermaid `flowchart` (IDs ASCII, labels quoted);
- **XMI/UML** (`packagedElement`): classes/atributos/associações + Mermaid `classDiagram`;
- **fallback**: outline estrutural (profundidade ≤4) + XML original preservado em bloco de código;
- erro de parse → comportamento antigo (bloco literal), nada se perde.

## 4. Codebase estilo reversa — ATENDE

- Zero `import reversa` (só atribuição textual MIT © sandeco) — réplica sem o pacote. ✅
- Gaps fechados: **G1** (`sequences/`, `openapi/`, `user-stories/`, `contracts.md`, `edge-cases.md` com merge + auditoria), **G2** (caminho de escrita legítimo p/ `adrs/NNN-*`, `traceability/*`, `confidence-report`, `gaps` via `merge-agent-output` — `scripts/codescan/agentmerge.py`), **G3** (docs corrigidos: `synth` é aceito em `run-stage`/`merge-agent-output`), **G4** (`agent-pack` determinístico para todos os estágios — `build_stage_pack`, `scripts/codescan/agentpack.py:269`), **G5** (`modules/` fora de `sdd/` documentado no contrato §1).
- Nomes de bloco aceitos pelo merge documentados em `references/sdd-contract.md:172-188`.

## 5. Mecanismos llm-wiki — ATENDE

- **index.md por categorias**: `compile` agrupa por `topic` com contagens e data.
- **Wikilinks `[[...]]`**: convenção documentada; `wk lint` ganhou W1 (link relativo quebrado), W2 (wikilink sem página), W3 (página órfã) por travessia determinística de `wiki/` (`_audit_wiki_links`, `scripts/wk/cli.py:1094`), no mesmo `_lint-report.md`, exit 1.
- **log.md** append-only e **query→arquivo** (arquivar resposta como página via portão agent-output) documentados em `SKILL.md`.

## Pendências conhecidas (fora de escopo desta entrega)

- `.doc` legado continua fora de escopo (formato binário proprietário; converter para `.docx` antes).
- Achados laterais anteriores não tratados: frontmatter duplicado ao ingerir `.md` com frontmatter próprio; encoding fixo `utf-8-sig`; `_convert_html` baseado em regex; `.git` do repositório vazio (sem histórico).
