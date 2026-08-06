# Aderência llm-wiki + réplica reversa — plano final

> **Regra: NUNCA executar sem ordem explícita do usuário.** Aprovação deste plano não é ordem de execução; cada etapa aguarda comando.

## Context

A aplicação (`wk` + `codescan`) é inspirada no **llm-wiki do Karpathy** (gist 442a6bf5): raw/→wiki/, index.md catálogo, log.md append-only, ciclo ingest→query→lint, o LLM escreve o wiki e o humano cura. O requisito do usuário é **aderência ao llm-wiki recebendo docs, planilhas, txt, transcrições e XML de arquitetura como fontes analisadas** (não conversão mecânica de formatos — esclarecido pelo usuário) **e análise de codebases replicando o reversa da sandeco** (sem usar o pacote — contrato em `references/sdd-contract.md`).

Diagnóstico atual:
- Arquitetura llm-wiki existe (inbox/raw/wiki, log.md append-only em `wk/cli.py:752`, lint SQL, frontmatter YAML), mas: index.md só lista documentos (`wk/cli.py:1026-1036`), **zero wikilinks `[[...]]`**, lint sem links quebrados/páginas órfãs (`operations/lint.md:41-43`), nenhum fluxo de análise definido para docs/planilhas.
- `.docx/.xlsx/.csv/PDF` são rejeitados pelo ingest e o original não é guardado em lugar nenhum (llm-wiki manda guardar a fonte imutável em `raw/`).
- `.xml` vira bloco de código literal (`wk/cli.py:1183`), sem interpretação.
- Codebase/reversa: núcleo sólido; **G1+G2 já corrigidos em disco** (ver "Estado"), G3-G5 pendentes.

## Estado — já efetuado em disco (NÃO testado, NÃO commitado)

G1+G2 aplicados em `scripts/codescan/agentmerge.py` (blocos `adrs/NNN-*`, `sequences/`, `traceability/spec-impact-matrix` em architecture; docs nomeados `confidence-report`, `gaps`, `traceability/code-spec-matrix`, `user-stories/`, `openapi/` e opcionais `contracts.md`/`edge-cases.md` no estágio specs), `scripts/codescan/sdd.py` (ArtifactRules + scaffold + brief) e `scripts/codescan/cli.py` (gates de `done` p/ sequences e user-stories). Manter.

## Execução em paralelo — 3 trilhas disjuntas por arquivo + fase final

E1/E2/E3 originais colidem em `scripts/wk/cli.py` e `test_corpus.py`; a partição paralelizável é por **arquivo**, não por funcionalidade:

| Trilha | Agente | Arquivos exclusivos | Conteúdo |
|---|---|---|---|
| **A — código wk** | 1 | `scripts/wk/cli.py`, `scripts/wk/tests/test_corpus.py`, `scripts/sbindex/*` (se necessário ao lint) | E1.1 (ingest→raw/assets) + E2 (XML draw.io/XMI/fallback) + E3.1-3.2 (index por categorias, lint wikilinks/órfãs) — sequencial dentro do agente |
| **B — docs wk** | 2 | `operations/ingest.md`, `operations/lint.md`, `operations/compile.md`, `SKILL.md`, `schema.md`, `INSTALL.md` | E1.2 + E3.3 (fluxo de análise llm-wiki por tipo de fonte; convenção wikilinks; query→arquivo) — escreve contra o contrato definido neste plano |
| **C — codescan** | 3 | `scripts/codescan/*` (cli.py, agentpack.py), `scripts/codescan/tests/test_agentmerge.py`, `references/sdd-contract.md`, `operations/ingest-codebase.md` | E4 completo (G3, G4, G5 + testes dos G1/G2 já aplicados) |
| **E5 — final** | sequencial, após A+B+C | suíte completa, `wk.pyz`, `docs/*.md`, memória | integração: ajustar fixtures, rebuild, relatórios |

Sem git funcional no repo (`.git` vazio) não há worktree/branch — a garantia de não-conflito é a **disjunção de arquivos acima**; nenhuma trilha toca arquivo de outra. Trilha B documenta comportamento que a A implementa: o contrato (assinaturas, formatos, mensagens) é o descrito nas etapas abaixo, não o código em andamento.

## Etapas (conteúdo de cada trilha)

### E1 — Fontes llm-wiki: receber e analisar (sem conversão mecânica) — trilha A (código) + B (docs)
1. `wk ingest`: novo caminho para formatos binários/planilha (`.docx`, `.xlsx`, `.csv`, `.pdf`): guardar o **original imutável** em `raw/assets/<id>.<ext>` (padrão llm-wiki) e criar em `inbox/` a **página de fonte** (stub com frontmatter de proveniência + link para o asset), em vez de rejeitar. A análise em si é do LLM: o agente lê a fonte, escreve a página de resumo e ela entra pelo portão existente (`agent-output`/`human-doc` → promote). Reusar `_render_frontmatter` (`wk/cli.py:719`) e `INBOX_DIR_BY_SOURCE_TYPE` (`wk/cli.py:627`).
2. `operations/ingest.md` + `SKILL.md`: definir o fluxo de análise llm-wiki por tipo de fonte (ler → discutir pontos-chave → página resumo → atualizar index/entidades → log), cobrindo docs, planilhas, txt e transcrições. `.txt`/`.vtt`/`.srt` continuam com o caminho atual.

### E2 — XML de arquitetura: analisador draw.io + XMI + fallback — trilha A
- `scripts/wk/cli.py`: substituir o ramo `.xml` de `_convert_codeblock` por `_convert_xml_architecture()` (stdlib `xml.etree`):
  - **draw.io** (`mxfile`/`mxGraphModel`): vértices/arestas → markdown estruturado (componentes, relações) + Mermaid `flowchart`;
  - **XMI/UML** (`xmi:XMI`/`packagedElement`): classes, atributos, associações → markdown + Mermaid `classDiagram`;
  - **fallback**: outline estrutural da árvore XML (elementos/atributos, profundidade limitada) + bloco de código preservado.
- Reusar validação Mermaid existente como referência de sintaxe (`codescan/sdd.py:874-1070`). Testes em `scripts/wk/tests/test_corpus.py`.

### E3 — Mecanismos llm-wiki faltantes — trilha A (1-2) + B (3)
1. **index.md por categorias**: `compile` (`wk/cli.py:1026-1036`) gera seções (por `topic` e `source_type`, com contagem e last-updated), não lista chata.
2. **Wikilinks `[[...]]`**: convenção documentada; `lint` ganha checagens determinísticas novas (travessia de `wiki/`): link quebrado (markdown relativo e `[[...]]`), página órfã (sem link de entrada), alvo de wikilink sem página. Report no `wiki/_lint-report.md` existente (`wk/cli.py:1074-1095`).
3. **Query→arquivo**: documentar na skill o fluxo de arquivar resposta valiosa como página (via `ingest --source-type agent-output`, já loga em log.md) — sem comando novo.

### E4 — Reversa: fechar G3, G4, G5 + testes de G1/G2 — trilha C
1. **G3**: `references/sdd-contract.md` (~172-175, ~283-286) e `operations/ingest-codebase.md` (~393-398): o CLI **aceita** `synth` em `run-stage`/`merge-agent-output` (`codescan/cli.py:2039,2045`) — corrigir e documentar os novos nomes de bloco de G1/G2.
2. **G4**: `agent-pack` para `rules`/`architecture`/`specs` (hoje só `modules`, `codescan/cli.py:2029`): gerar pack determinístico com os artefatos de entrada do estágio (modules/*.md, rules) em vez de `agent_pack: null`.
3. **G5**: documentar `modules/` fora de `sdd/` na árvore do contrato §1.
4. Testes novos em `test_agentmerge.py`: adrs/NNN, sequences/, spec-impact-matrix, docs nomeados de specs (gaps, openapi→.yaml), contracts/edge-cases, rejeições (nome inválido/duplicado/vazio).

### E5 — Validação, empacotamento e correção dos relatórios — fase final sequencial (após A+B+C)
1. Suíte completa (`python -m unittest discover` sob `scripts/`); ajustar fixtures quebradas pelos novos gates (fixtures, não os gates).
2. Rebuild `python scripts/build_pyz.py` (wk.pyz em sincronia).
3. Reescrever `docs/validacao-requisitos.md` com o benchmark correto (aderência llm-wiki + réplica reversa) e substituir `docs/plano-remediacao.md` por este plano.
4. Atualizar memória `wiki-ai-replica-reversa` (inspiração llm-wiki é requisito de aderência).

## Verificação (quando autorizado)
1. Suíte verde + `wk.pyz` rebuildo (`python wk.pyz doctor`).
2. `wk ingest planilha.xlsx` guarda asset em `raw/assets/` e cria página de fonte no inbox (não rejeita).
3. `wk ingest diagrama.drawio.xml` gera markdown estruturado + Mermaid válido.
4. `wk lint` acusa wikilink quebrado e página órfã em store de teste.
5. E2E codebase `doc_level=detalhado`: todos os estágios (incl. `synth`) fecham `done` com a árvore completa do contrato §1.
