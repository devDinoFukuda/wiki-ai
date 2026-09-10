# Wiki-AI V2 — Plano de Remediação Clean-Slate e Reconstrução Arquitetural

**Status:** plano normativo para implementação  
**Base de análise:** comportamento executável da branch `fix/plano-remediacao-v1`  
**Fonte de verdade:** código executável, dependências, entrypoints, schemas, persistência, subprocessos e testes  
**Não são fonte de verdade:** README, SKILL, comentários, docstrings ou artefatos explicativos existentes  
**Estratégia:** remoção destrutiva antes de evolução  
**Compatibilidade retroativa:** não requerida  
**Objetivo:** reconstruir o Wiki-AI como harness determinístico para investigação profunda de codebases e fontes corporativas, com LLM investigadora, evidência verificável, interface humana simples e publicação DOCX adequada a SharePoint/Copilot Studio

---

# 0. Norma deste plano

Os termos abaixo são normativos:

| Termo | Significado |
|---|---|
| **DEVE** | requisito obrigatório |
| **NÃO DEVE** | comportamento proibido |
| **PODE** | opção permitida |
| **BLOQUEANTE** | impede avanço de wave |
| **REMOVER** | apagar código, testes, entrypoints, arquivos e dependências relacionadas; não esconder |
| **RECONSTRUIR** | implementar novamente a partir do requisito, sem preservar a implementação defeituosa |
| **EXTRAIR** | copiar somente comportamento comprovadamente útil para módulo novo e apagar o módulo original |
| **PRESERVAR** | manter somente após prova de aderência ao target e limpeza das dependências |

---

# 1. Objetivo do produto

O Wiki-AI DEVE funcionar como uma camada de engenharia de conhecimento sobre:

- codebases;
- documentos;
- planilhas;
- diagramas;
- transcrições;
- conhecimento levantado em inceptions;
- mudanças futuras e propostas arquiteturais.

O Wiki-AI DEVE permitir que um engenheiro faça perguntas como:

- "Analise profundamente este sistema."
- "Como funciona a renovação?"
- "Quais regras de negócio participam deste fluxo?"
- "Quais são os inputs, outputs, invariantes e edge cases?"
- "Quais integrações são críticas?"
- "O que pode quebrar se esta regra mudar?"
- "Compare o comportamento implementado com a decisão tomada na inception."
- "Quais pontos desta migração ainda não possuem evidência?"
- "Gere o material atualizado para publicação no SharePoint."

A complexidade operacional DEVE permanecer dentro do Wiki-AI.

O humano NÃO DEVE ser obrigado a conhecer ou operar:

- store interno;
- leases;
- bindings;
- objective IDs;
- reading needs;
- revisions internas;
- promote;
- compile;
- handoff;
- merge;
- resume técnico;
- códigos de pipeline;
- diretórios intermediários;
- formatos de envelope;
- detalhes do provider de LLM.

---

# 2. Decisões inegociáveis

## 2.1 Clean-slate primeiro

A **Wave 0 — Clean-Slate** é a primeira etapa e é BLOQUEANTE.

Nenhuma feature nova DEVE ser implementada enquanto a Wave 0 não estiver concluída.

É proibido:

- evoluir código condenado;
- adicionar compatibilidade;
- criar novos wrappers sobre fluxos antigos;
- manter código "por segurança";
- manter command aliases;
- deixar implementação morta comentada;
- deixar módulos antigos fora do entrypoint esperando remoção posterior;
- manter testes cujo único objetivo seja preservar comportamento removido.

A regra é:

> Se não pertence ao target, não permanece no repository.

---

## 2.2 Código executável é a única fonte de verdade para decisões de implementação

Toda decisão de remoção, preservação ou reconstrução DEVE ser derivada de:

- import graph;
- call graph;
- parser/entrypoint real;
- funções realmente chamadas;
- adapters registrados;
- schemas efetivamente validados;
- arquivos efetivamente lidos/escritos;
- subprocessos executados;
- bancos efetivamente acessados;
- testes que executam o comportamento.

É proibido justificar preservação com:

- README;
- comentário;
- docstring;
- seção "legado";
- plano antigo;
- intenção declarada;
- nome de arquivo;
- "pode ser útil depois".

---

## 2.3 Zero comentários no código

O código de implementação do Wiki-AI DEVE terminar a Wave 0 com:

- **zero comentários narrativos**;
- **zero comentários inline**;
- **zero blocos de comentário**;
- **zero código comentado**;
- **zero TODO/FIXME/HACK/NOTE**;
- **zero docstrings narrativas de módulo, classe ou função**.

A intenção arquitetural DEVE viver em:

- nomes;
- tipos;
- schemas;
- interfaces;
- testes;
- erros estruturados;
- documentos Markdown versionados separadamente.

Não haverá exceção para comentários de conveniência.

Diretivas que hoje dependam de comentários, por exemplo:

- `# type: ignore`;
- `# pragma: no cover`;
- `# noqa`;
- `# fmt: off`;
- `# pylint: disable`;

DEVEM ser eliminadas por correção do código/configuração, não preservadas.

O repositório DEVE possuir um gate determinístico que falhe se comentários ou docstrings forem adicionados novamente.

---

## 2.4 Um único control plane

O Wiki-AI DEVE possuir exatamente um fluxo executável para:

- análise de codebase;
- ingestão;
- correlação;
- atualização;
- publicação;
- consulta.

Não pode existir:

- pipeline antigo;
- pipeline novo;
- modo de compatibilidade;
- modo "legacy";
- fluxo manual alternativo;
- fluxo Claude;
- fluxo de outro provider;
- publicação antiga;
- publicação nova.

---

## 2.5 Agent-agnostic no core

Claude Code, Codex, Devin, Cursor, Windsurf, Antigravity ou outro host são transportes/providers.

Nenhum deles DEVE definir a arquitetura do Wiki-AI.

O core DEVE depender de um contrato único de agente.

Qualquer comportamento provider-specific DEVE existir somente no adapter do provider.

---

## 2.6 LLM investiga; harness governa

A LLM DEVE:

- formular hipóteses;
- navegar;
- aprofundar;
- relacionar evidências;
- identificar regras;
- reconhecer invariantes;
- reconstruir fluxos;
- descobrir dependências;
- comparar fontes;
- identificar contradições;
- decidir que leitura adicional precisa realizar.

O harness DEVE:

- limitar ferramentas;
- controlar escopo;
- controlar orçamento;
- capturar evidências;
- versionar fontes;
- validar schemas;
- impedir escrita arbitrária;
- detectar stale results;
- verificar referências;
- controlar retries;
- persistir estado;
- invalidar conhecimento;
- publicar atomicamente.

O harness NÃO DEVE transformar a LLM em preenchimento de formulário sobre snippets estáticos.

---

## 2.7 Evidência antes de conhecimento confirmado

Nenhuma afirmação sobre comportamento implementado DEVE ser marcada como confirmada sem evidência verificável.

Evidência DEVE possuir, conforme a fonte:

- source ID;
- version/hash;
- caminho;
- linhas/faixa;
- símbolo;
- bloco;
- worksheet/cell/range;
- diagram/page/node;
- speaker/time range;
- origem.

---

## 2.8 DOCX é output de primeira classe

DOCX não é etapa opcional.

Se o destino operacional é SharePoint/Copilot Studio, a publicação NÃO DEVE ser marcada como pronta se o DOCX exigido não foi:

- gerado;
- validado;
- associado ao manifesto;
- incluído no pacote de entrega.

---

# 3. Diagnóstico executável que motiva a reconstrução

Esta seção registra somente achados do código executável atual.

## 3.1 Existem dois pipelines de análise

O codebase possui simultaneamente:

```text
scripts/codescan/*
```

e:

```text
scripts/analysis/*
scripts/runtime/*
scripts/knowledge/*
scripts/publishing/*
```

O fluxo novo de análise executa `analysis -> runtime -> knowledge -> publishing`.

O `codescan` permanece como outro sistema completo, exposto por comandos próprios e por funções do `wk`.

Consequências:

- dois modelos de estado;
- dois conceitos de evidência;
- dois mecanismos de análise;
- dois grupos de testes;
- múltiplas rotas de publicação;
- mais pontos de falha;
- maior carga cognitiva;
- maior risco de uma implementação nova reutilizar acidentalmente conceitos condenados.

---

## 3.2 `wk/cli.py` é um god module

O arquivo atual possui aproximadamente:

- 12 mil linhas;
- ~576 KB.

Ele concentra responsabilidades de:

- init;
- provider setup;
- doctor;
- analyze;
- ingest;
- update;
- resume;
- status;
- delivery;
- promote;
- compile;
- docx;
- publish;
- finish;
- integração com codescan;
- compatibilidade;
- publicação;
- store;
- migração.

Este arquivo NÃO DEVE ser refatorado incrementalmente.

Ele DEVE ser substituído por uma composição mínima construída do zero.

---

## 3.3 O modo deep não permite investigação real do repository

O executor Claude atual:

- constrói prompt com JSON de tarefa;
- recebe pacote previamente montado;
- executa `claude -p`;
- utiliza diretório temporário;
- não oferece ferramentas de repository;
- declara `tools: []`.

Resultado:

```text
repository
  -> extração determinística
  -> planner
  -> pacote de snippets
  -> LLM
```

A LLM não possui autonomia controlada para:

- buscar símbolo;
- procurar referência;
- abrir arquivo adicional;
- seguir cadeia de chamadas;
- correlacionar configuração;
- procurar teste;
- investigar hipótese emergente.

O executor atual DEVE ser removido e reconstruído.

---

## 3.4 A investigação é dirigida por formulário fixo

O resultado do worker atual é condicionado por um schema fechado e por campos pré-determinados.

Schema fechado é desejável como fronteira de persistência.

Não é desejável como mecanismo de investigação.

O target DEVE inverter a relação:

```text
investigação dinâmica
    -> findings estruturados
    -> verificação
    -> normalização
    -> persistência
```

e NÃO:

```text
formulário fixo
    -> snippets
    -> preenchimento
```

---

## 3.5 Cobertura por arquivo não prova cobertura comportamental

O pipeline atual possui invariantes para garantir que arquivos de código não desapareçam do plano.

Isso é necessário, mas insuficiente.

O target precisa rastrear também:

- capabilities descobertas;
- entrypoints;
- regras;
- branches relevantes;
- efeitos;
- integrações;
- fluxos;
- falhas;
- invariantes;
- estados;
- contratos;
- pontos ainda não investigados.

---

## 3.6 Ingestão atual não atende às fontes necessárias

O registry atual não possui adapter XLSX.

XML/XMI é lido genericamente por tags/atributos; não existe reconstrução semântica de draw.io.

Transcrições preservam speaker/time, mas a classificação semântica posterior é majoritariamente baseada em regex.

PDF utiliza parsing próprio e possui limitações importantes de compressão, encoding e OCR.

Esses componentes não devem ser expandidos por remendos.

Devem ser reconstruídos conforme os requisitos da Wave de ingestão.

---

## 3.7 Existem dois caminhos de DOCX/publicação

Há uma família antiga:

```text
promote -> compile -> docx -> wiki-docx
```

e uma família nova:

```text
knowledge -> publishing planner -> release -> markdown + word
```

O target DEVE possuir apenas uma.

---

# 4. Arquitetura alvo

## 4.1 Estrutura física proposta

A implementação reconstruída DEVE convergir para um package único:

```text
src/
  wiki_ai/
    app/
      api.py
      commands.py
      session.py

    repository/
      snapshot.py
      inventory.py
      search.py
      reader.py
      symbols.py
      references.py
      history.py

    agent/
      protocol.py
      registry.py
      session.py
      providers/
        claude.py
        codex.py
        devin.py

    investigation/
      orchestrator.py
      objective.py
      finding.py
      evidence.py
      coverage.py
      verifier.py
      compaction.py

    knowledge/
      model.py
      repository.py
      schema.py
      identity.py
      relations.py
      invalidation.py
      query.py

    ingestion/
      source.py
      pipeline.py
      semantic.py
      adapters/
        docx.py
        xlsx.py
        drawio.py
        pdf.py
        transcript.py
        markdown.py
        html.py
        json.py
        xml.py

    publishing/
      model.py
      planner.py
      narrative.py
      diagrams.py
      docx.py
      manifest.py
      release.py
      delivery.py

    quality/
      source_hygiene.py
      architecture.py
      contracts.py
```

A árvore `scripts/` atual NÃO DEVE permanecer como arquitetura paralela.

---

## 4.2 Fluxo de análise de codebase

```text
User
  |
  v
Wiki-AI
  |
  v
Repository Snapshot
  |
  v
Inventory
  |
  v
Deterministic Repository Harness
  |
  v
Investigation Orchestrator
  |
  +----> Agent explores repository through controlled tools
  |          |
  |          +--> search
  |          +--> read
  |          +--> symbol
  |          +--> references
  |          +--> dependencies
  |          +--> tests
  |          +--> config
  |          +--> history
  |          +--> evidence.capture
  |
  v
Findings
  |
  v
Deterministic Verification
  |
  v
Knowledge Graph
  |
  v
Publication / Query
```

---

## 4.3 Fluxo de fontes corporativas

```text
Source
  |
  v
Format Adapter
  |
  v
Structural Representation
  |
  v
Semantic Investigation
  |
  v
Evidence-backed Findings
  |
  v
Cross-source Correlation
  |
  +----> codebase
  +----> prior decisions
  +----> requirements
  +----> architecture
  +----> initiatives
  |
  v
Knowledge Graph
```

---

# 5. WAVE 0 — CLEAN-SLATE OBRIGATÓRIO

## 5.1 Objetivo

Eliminar do repository qualquer implementação que:

- represente arquitetura anterior;
- duplique o target;
- seja compatibilidade;
- seja fluxo alternativo;
- tenha sido construída sobre premissa rejeitada;
- exista apenas para manter comportamento antigo;
- aumente risco de reutilização acidental;
- contenha lógica de domínio misturada com CLI;
- dependa de comentários para ser compreendida;
- possua implementação tão contaminada que reescrita seja mais segura que refactor.

A Wave 0 não busca manter o sistema funcionalmente completo.

Ela busca deixar um **kernel pequeno, coerente e não contaminado** sobre o qual as waves seguintes serão construídas.

---

## 5.2 Regra de retenção

O default da Wave 0 é:

> **DELETE.**

Um componente só permanece se cumprir TODOS os critérios:

1. pertence diretamente à arquitetura alvo;
2. possui comportamento executável necessário;
3. não depende do pipeline condenado;
4. não implementa uma premissa rejeitada;
5. possui testes de invariantes úteis para o target;
6. pode ser limpo sem carregar compatibilidade;
7. seu custo de reconstrução é maior que seu risco de preservação.

Se um componente possuir apenas uma parte útil:

- extrair a parte útil;
- escrever módulo novo;
- redirecionar teste;
- apagar módulo antigo.

Não é permitido manter o módulo antigo para reutilizar 10% dele.

---

## 5.3 Construir mapa de reachability antes das deleções

A primeira subtarefa técnica DEVE gerar mecanicamente:

```text
artifacts/w0/runtime-entrypoints.json
artifacts/w0/import-graph.json
artifacts/w0/reachability.json
artifacts/w0/deletion-report.json
```

A análise DEVE usar:

- AST Python;
- imports;
- parser registration;
- `__main__`;
- chamadas a entrypoints;
- imports tardios localizáveis por AST/string literal controlada;
- arquivos acessados;
- provider registrations;
- tests references.

Comentários e docstrings NÃO entram no cálculo.

O relatório DEVE classificar cada módulo como:

```text
KEEP
EXTRACT
REBUILD
DELETE
```

A classificação final deste plano prevalece para os componentes explicitamente condenados abaixo.

---

## 5.4 Deleções obrigatórias

### 5.4.1 Remover `scripts/codescan/` integralmente

DEVE ser removido:

```text
scripts/codescan/
```

Incluindo:

- CLI;
- pilot;
- fan-out;
- agent pack;
- agent merge;
- coupling antigo;
- exporters;
- SDD;
- state;
- evidence própria;
- verificadores;
- testes;
- artefatos de workdir.

Nenhum import de `codescan` pode existir após a Wave 0.

Se alguma função algorítmica possuir valor comprovado para o target:

1. implementar API nova no package alvo;
2. portar somente o algoritmo necessário;
3. criar teste novo baseado em comportamento;
4. apagar a implementação antiga.

---

### 5.4.2 Remover `scripts/sbindex/` integralmente

O mecanismo antigo de corpus/index não pertence ao target.

DEVE ser removido:

```text
scripts/sbindex/
```

O novo mecanismo de busca e recuperação DEVE nascer sobre o modelo de conhecimento novo.

Nenhuma dependência deve sobreviver apenas para permitir `promote`, `compile`, `query` antigo ou indexação antiga.

---

### 5.4.3 Remover o `wk/cli.py` atual

`scripts/wk/cli.py` NÃO DEVE ser refatorado.

DEVE ser removido após os entrypoints mínimos novos estarem disponíveis dentro da própria Wave 0.

Não devem ser portados:

```text
promote
compile
docx antigo
publish antigo
finish
code
ingest-legacy
migrate de corpus antigo
compat engine
doctor orientado ao fluxo antigo
slash-command pilot
codescan workdir handling
raw/wiki workflow
```

O novo CLI de bootstrap da Wave 0 DEVE possuir apenas comandos mínimos necessários para smoke test do kernel.

Exemplo:

```text
wiki-ai version
wiki-ai inspect
```

Os comandos de produto serão adicionados posteriormente.

---

### 5.4.4 Remover compatibilidade de engines

Remover:

- `--engine local`;
- `--engine claude-cli`;
- mapeamentos de engine para comportamento;
- defaults baseados em Claude;
- comandos exclusivos de Claude;
- geração de slash command de análise;
- qualquer decisão arquitetural baseada em executável encontrado no PATH.

Provider selection será reconstruído sobre contrato único na Wave de agentes.

---

### 5.4.5 Remover `LegacyExecutorAdapter`

Nenhuma classe, alias ou branch com significado de legado/compatibilidade DEVE permanecer.

Gate obrigatório:

```text
grep -R -i "legacy\|legado\|compat\|deprecated" src/
```

Resultado esperado:

```text
0 ocorrências
```

Nomes de domínio externos que genuinamente contenham essas palavras não são previstos no core; se aparecerem, devem ser tratados explicitamente.

---

### 5.4.6 Remover o executor `claude_cli.py` atual

A implementação atual não oferece investigação real do repository.

DEVE ser removida.

Não deve ser adaptada incrementalmente.

O provider Claude será reconstruído sobre o novo protocol de ferramentas.

---

### 5.4.7 Remover o planner atual de investigação

A implementação atual de investigação centrada em:

- objetivo fixo;
- reading needs pré-selecionadas;
- contrato de 13 campos como mecanismo primário;
- cobertura de arquivos como proxy de completude;

DEVE ser removida como núcleo de investigação.

Componentes condenados devem incluir a lógica responsável por essa premissa.

A Wave posterior construirá o investigation orchestrator do zero.

---

### 5.4.8 Reduzir `analysis` ao kernel reutilizável

O único comportamento atual inicialmente elegível a extração é:

```text
snapshot
inventory
```

Mesmo esses módulos DEVEM:

- ser movidos para a nova árvore;
- perder comentários/docstrings;
- perder referências a fluxo antigo;
- receber testes novos;
- possuir interface mínima.

Extractors específicos atuais não fazem parte do kernel obrigatório da Wave 0.

Se forem reutilizados futuramente, serão reintroduzidos como `hints`, não como autoridade da investigação.

---

### 5.4.9 Reduzir `runtime` ao mínimo comprovadamente útil

São candidatos a extração, não preservação automática:

```text
task state
lease
envelope identity
stale-result detection
retry/recovery primitives
```

Devem ser removidos:

- executores atuais;
- agent adapters atuais;
- provider-specific behavior;
- compat adapters;
- schemas de resultado presos ao formulário antigo;
- coordinator preso ao planner antigo.

A regra é:

> preservar invariantes; apagar orchestration contaminada.

---

### 5.4.10 Reduzir `knowledge` ao storage kernel

São candidatos a extração:

```text
identity
evidence
revision transaction
repository persistence
relation persistence
source versions
```

Não devem ser preservados automaticamente:

- taxonomias insuficientes;
- integração presa aos campos antigos de investigação;
- parsers linguísticos de consequência;
- transformação de campos fixos em regra/flow;
- mapeamentos construídos para o contrato antigo.

`scripts/knowledge/integrate.py` DEVE ser reconstruído.

---

### 5.4.11 Remover o semantic extractor regex atual de ingestão

A lógica de `ingestion/extract.py` que usa regex para decidir semanticamente:

```text
DECISION
QUESTION
HYPOTHESIS
ACTION
REQUIREMENT
```

NÃO DEVE ser o semantic engine do target.

O módulo DEVE ser removido e reconstruído.

Regex pode existir futuramente apenas como:

- detector barato;
- hint;
- pré-classificador não autoritativo.

---

### 5.4.12 Remover `ingestion/correlate.py` atual

A correlação futura dependerá do knowledge model reconstruído.

Portanto a implementação atual DEVE ser removida.

`normalize.py` pode ser extraído se o modelo de:

- source;
- block;
- hash;
- locator;
- diagnostics;

continuar aderente após revisão.

---

### 5.4.13 Remover adapters de fonte insuficientes

A Wave 0 DEVE apagar os adapters atuais cuja implementação será reconstruída:

```text
pdf
docx
xml genérico como substituto de draw.io
transcript semantic path
```

Não haverá preservação apenas porque "já funciona parcialmente".

O novo pipeline de ingestão será implementado na Wave específica.

---

### 5.4.14 Remover a publicação duplicada

A família:

```text
raw/
wiki/
wiki-docx/
promote
compile
docx
finish
```

DEVE desaparecer.

Não deve existir compatibilidade.

Os artefatos antigos do store não serão migrados.

Nova análise/reingestão é o caminho suportado.

---

### 5.4.15 Reduzir `publishing` ao mecanismo transacional

Pode ser extraído:

- staging;
- atomic promotion;
- manifest versioning;
- hash;
- rollback seguro;
- validação estrutural reutilizável.

Devem ser reconstruídos:

- planner documental;
- semantic unit orientada ao modelo atual;
- conteúdo Markdown;
- conteúdo Word;
- delivery atual;
- sharepoint abstraction atual.

O output final será desenhado para o usuário e para recuperação, não para expor o modelo interno.

---

### 5.4.16 Reimplementar geração DOCX em package próprio

Se primitivas OOXML atuais forem tecnicamente reutilizáveis, somente as funções efetivamente necessárias DEVEM ser extraídas para:

```text
src/wiki_ai/publishing/docx.py
```

ou subpackage equivalente.

Depois disso:

```text
scripts/wk/docx_*.py
scripts/wk/docxgen.py
```

DEVEM ser removidos.

Nenhum renderer novo pode depender de `wk`.

---

## 5.5 Remover storage antigo

Não deve haver suporte operacional para:

```text
.codescan/
raw/
inbox/
wiki/
wiki-docx/
agent-outputs/
old workdirs
old migration manifests
```

A nova árvore de runtime deve ser pequena e privada:

```text
.wiki-ai/
  state.db
  snapshots/
  evidence/
  publications/
```

O formato será versionado.

Store antigo:

- não é migrado;
- não é interpretado;
- não é atualizado;
- não abre caminho de compatibilidade.

Ao detectar store antigo, o sistema deve retornar erro objetivo indicando que nova análise é necessária.

---

## 5.6 Política de comentários e docstrings

### 5.6.1 Gate obrigatório

Criar:

```text
src/wiki_ai/quality/source_hygiene.py
```

O gate DEVE falhar em código de implementação quando detectar:

#### Python

- token `COMMENT`;
- string inicial em `Module`;
- string inicial em `ClassDef`;
- string inicial em `FunctionDef`;
- string inicial em `AsyncFunctionDef`.

Isso elimina comentários e docstrings.

#### Outras linguagens do próprio Wiki-AI

Se forem introduzidas:

- `//`;
- `/* ... */`;
- `<!-- ... -->`;
- blocos equivalentes;

devem ser cobertos pelo gate apropriado.

Markdown de documentação não é código e não entra neste gate.

---

### 5.6.2 O que substitui comentários

| Necessidade | Substituição |
|---|---|
| explicar contrato | type/schema |
| explicar estado | enum |
| explicar erro | exception tipada |
| explicar formato | schema |
| explicar decisão | Markdown arquitetural |
| explicar invariável | teste |
| explicar branch complexa | decompor função |
| TODO | issue externa ou implementação imediata |
| desativar linter | corrigir estrutura |
| documentar API interna | nome + tipo + teste |

---

## 5.7 Limites estruturais após limpeza

Gates iniciais:

| Regra | Gate |
|---|---:|
| `codescan` existente | FAIL |
| `sbindex` existente | FAIL |
| `wk/cli.py` antigo existente | FAIL |
| símbolo `LegacyExecutorAdapter` | FAIL |
| comandos antigos registrados | FAIL |
| comentário em source | FAIL |
| docstring em source | FAIL |
| import de módulo removido | FAIL |
| duas rotas de DOCX | FAIL |
| provider-specific branch no core | FAIL |
| arquivo production > 1.000 LOC | FAIL |
| módulo com ciclo de import | FAIL |

O limite de 1.000 LOC é um hard stop de proteção contra novo god module.

Objetivo operacional:

- módulos de domínio preferencialmente abaixo de 500 LOC;
- command handlers pequenos;
- composition root explícito.

---

## 5.8 Testes na Wave 0

Devem ser apagados:

- testes de comportamento removido;
- testes de compatibilidade;
- testes de pipeline antigo;
- testes de `codescan`;
- testes de `sbindex`;
- testes de CLI antiga;
- testes de promote/compile/finish;
- testes que só garantem antigos caminhos de store.

Devem ser reconstruídos:

```text
test_no_comments
test_no_docstrings
test_no_removed_packages
test_no_removed_commands
test_import_graph_has_no_cycle
test_only_one_application_entrypoint
test_storage_kernel_initializes
test_snapshot_is_immutable
test_evidence_locator_roundtrip
test_revision_is_atomic
test_manifest_atomic_promotion
```

---

## 5.9 Critério de aceite da Wave 0

A Wave 0 PASSA somente se TODOS forem verdadeiros:

```text
[ ] scripts/codescan removido
[ ] scripts/sbindex removido
[ ] CLI monolítica removida
[ ] old code command removido
[ ] promote removido
[ ] compile removido
[ ] old docx removido
[ ] finish removido
[ ] ingest-legacy removido
[ ] migrate-old-store removido
[ ] compat engines removidas
[ ] LegacyExecutorAdapter removido
[ ] claude_cli executor antigo removido
[ ] planner de investigação antigo removido
[ ] semantic extractor regex antigo removido
[ ] correlate antigo removido
[ ] publicação antiga removida
[ ] old storage contract removido
[ ] zero comentários
[ ] zero docstrings
[ ] zero imports quebrados
[ ] zero referências a módulos removidos
[ ] kernel executa smoke tests
[ ] nova árvore src/wiki_ai é a única arquitetura ativa
```

Nenhuma Wave posterior inicia com item pendente.

---

# 6. WAVE 1 — APPLICATION KERNEL E UX

## 6.1 Objetivo

Criar a superfície mínima do produto sem expor orchestration interna.

---

## 6.2 Interface humana

O fluxo principal será conversacional através do host/agente.

O host recebe intenção humana e chama API/CLI interna do Wiki-AI.

A CLI interna deve convergir para operações de produto:

```text
wiki-ai analyze <repo>
wiki-ai ingest <source> --repo <repo>
wiki-ai ask "<question>" --repo <repo>
wiki-ai publish --repo <repo>
wiki-ai status --repo <repo>
```

O humano não informa `store`.

Default:

```text
<repo>/.wiki-ai/
```

ou configuração global explícita.

---

## 6.3 Sem configuração operacional manual

O Wiki-AI deve resolver automaticamente:

- repository identity;
- state path;
- snapshot;
- current provider;
- provider availability;
- knowledge namespace;
- publication path.

Se o provider não estiver disponível, retornar:

```json
{
  "status": "blocked",
  "reason": "agent_provider_unavailable",
  "action": "configure a supported provider"
}
```

Não expor conceitos internos.

---

## 6.4 Critério de aceite

```text
[ ] analyze possui uma entrada
[ ] ingest possui uma entrada
[ ] ask possui uma entrada
[ ] publish possui uma entrada
[ ] nenhuma operação exige store manual
[ ] nenhuma operação exige binding manual
[ ] nenhuma operação exige engine antiga
[ ] nenhum comando depende do provider específico
```

---

# 7. WAVE 2 — DETERMINISTIC REPOSITORY HARNESS

## 7.1 Objetivo

Entregar à LLM acesso profundo e controlado ao repository.

---

## 7.2 Tools mínimas

### `repo.inventory`

Retorna:

- files;
- size;
- language hint;
- classification;
- ignored/generated flags;
- hash.

### `repo.search`

Busca:

- texto;
- regex controlada;
- nomes;
- patterns;
- extensões.

### `repo.read`

Lê:

- arquivo;
- intervalo;
- limite explícito.

### `repo.symbol`

Retorna informações sobre símbolo quando houver indexador disponível.

### `repo.references`

Procura usos/referências.

Deve funcionar com fallback textual quando semantic index não existir.

### `repo.dependencies`

Retorna dependências detectáveis:

- imports;
- build files;
- manifests;
- packages;
- external endpoints;
- messaging hints.

### `repo.tests`

Descobre testes relacionados.

### `repo.config`

Busca configuração por chave/uso.

### `repo.history`

Consulta Git quando disponível:

- commits;
- blame;
- diff;
- renames.

Não é obrigatório para análise básica.

### `evidence.capture`

Transforma leitura já realizada em evidência persistível.

---

## 7.3 Segurança

As tools são read-only.

A LLM NÃO recebe:

- shell arbitrário;
- filesystem global;
- write permission;
- network arbitrária;
- comando de publicação;
- acesso direto ao banco de conhecimento.

---

## 7.4 Linguagem agnóstica

Suporte a linguagem NÃO deve depender de possuir AST específico.

Fallback universal:

```text
inventory
  -> search
  -> read
  -> LLM reasoning
  -> evidence
```

Indexadores específicos são aceleradores.

Nunca declarar "linguagem não suportada" apenas porque não há parser.

---

## 7.5 Critério de aceite

Testar no mínimo:

- Java;
- Python;
- JavaScript/TypeScript;
- COBOL;
- JCL;
- SQL;
- repository misto.

A LLM deve conseguir solicitar leituras adicionais dinamicamente em todos.

---

# 8. WAVE 3 — INVESTIGATION ENGINE RECONSTRUÍDO

## 8.1 Objetivo

Substituir o modelo de "planner fixa snippets" por investigação iterativa.

---

## 8.2 Loop

```text
Objective
  |
  v
Initial inventory
  |
  v
Agent hypothesis
  |
  v
Tool call
  |
  v
Evidence
  |
  v
Update investigation state
  |
  +---- unresolved? ---- yes ----> next tool call
  |
  no
  v
Structured findings
  |
  v
Verifier
```

---

## 8.3 Objective

Objective define intenção, não formulário.

Exemplos:

```json
{
  "kind": "system_analysis",
  "scope": "repository",
  "goal": "reconstruct implemented behavior"
}
```

```json
{
  "kind": "impact_analysis",
  "goal": "identify behavior affected by renewal rule change"
}
```

---

## 8.4 Finding schema

O worker produz findings tipados.

Exemplo conceitual:

```json
{
  "type": "business_rule",
  "subject": "renewal eligibility",
  "statement": "...",
  "conditions": [],
  "effects": [],
  "evidence": [],
  "confidence": "supported"
}
```

Tipos não devem ser limitados aos 13 campos antigos.

---

## 8.5 Critério de completude

Completude deve considerar:

- discovered entrypoints;
- discovered capabilities;
- unresolved calls;
- unresolved effects;
- unresolved integrations;
- unresolved branches materialmente relevantes;
- missing evidence;
- explicit gaps.

`files_covered` pode continuar como métrica auxiliar.

Nunca como prova suficiente de análise profunda.

---

## 8.6 Context engineering

O orchestrator deve:

- enviar objetivo;
- enviar estado mínimo;
- recuperar evidência sob demanda;
- compactar resultados anteriores;
- não reenviar repository inteiro;
- manter evidence IDs;
- manter unresolved frontier.

---

# 9. WAVE 4 — KNOWLEDGE MODEL V2

## 9.1 Objetivo

Representar conhecimento com granularidade suficiente para responder perguntas arquiteturais e gerar documentos úteis.

---

## 9.2 Entidades mínimas

```text
System
Module
Capability
EntryPoint

BusinessRule
Invariant
Precondition
Postcondition
EdgeCase

Flow
FlowStep
Decision
State
StateTransition

Input
Output
DataContract
DataField
Validation

Integration
Operation
Endpoint
Protocol
Event
Topic
Queue

Persistence
DataEntity
Table
Query
Procedure
Transaction

FailureMode
RetryPolicy
Fallback
TimeoutPolicy
IdempotencyPolicy

Configuration
Dependency

TestScenario

Initiative
Requirement
Proposal
DecisionRecord

Source
Evidence
Gap
```

---

## 9.3 Estados epistemológicos separados

Não misturar:

```text
implemented
declared
proposed
historical
```

com:

```text
supported
inferred
unresolved
contradicted
```

São eixos diferentes.

---

## 9.4 Relações mínimas

```text
implements
calls
consumes
publishes
reads
writes
validates
depends_on
triggers
transitions_to
handles
retries
falls_back_to
persists_to
tests
declares
proposes_change_to
contradicts
supersedes
affects
belongs_to
```

---

# 10. WAVE 5 — DEEP CODEBASE ANALYSIS

## 10.1 Objetivo

Produzir conhecimento profundo independente da linguagem.

---

## 10.2 Saídas mínimas por capability

Quando aplicável:

```text
Purpose
Entrypoints
Inputs
Outputs
Preconditions
Business Rules
Invariants
Decision Logic
State Changes
Persistence
Integrations
Events
Failures
Retries
Fallbacks
Timeouts
Idempotency
Edge Cases
Tests
Dependencies
Evidence
Open Gaps
```

---

## 10.3 Fluxos

O analyzer deve construir grafo comportamental.

Exemplo:

```text
HTTP entry
  -> validation
  -> eligibility rule
  -> persistence
  -> event publication
  -> response
```

O grafo deve ser derivado de evidence-backed findings.

---

## 10.4 Diagramas gerados

Gerar, quando houver dados suficientes:

- flowchart;
- sequence diagram;
- state diagram;
- dependency diagram.

O diagrama deve possuir representação textual equivalente.

---

# 11. WAVE 6 — INGESTÃO MULTIFORMATO

## 11.1 Princípio

Adapter estrutural NÃO decide sozinho o significado de negócio.

Fluxo:

```text
parse structure
  -> semantic investigation
  -> evidence
  -> correlation
```

---

## 11.2 DOCX

Deve extrair, quando existentes:

- headings;
- paragraphs;
- lists;
- tables;
- headers;
- footers;
- footnotes;
- endnotes;
- comments;
- hyperlinks;
- images metadata;
- document properties.

Imagens sem interpretação devem gerar gap explícito.

---

## 11.3 XLSX

Criar adapter real.

Deve extrair:

- workbook;
- worksheets;
- visible/hidden state;
- cell values;
- formulas;
- merged ranges;
- tables;
- named ranges;
- headers;
- comments/notes;
- data validations;
- cross-sheet references relevantes.

Locator:

```text
workbook
worksheet
cell/range
```

Exemplo:

```text
Regras!B12:F27
```

A investigação semântica deve reconhecer:

- matriz de regra;
- tabela de decisão;
- catálogo;
- mapping;
- thresholds;
- estados;
- dependências entre abas.

---

## 11.4 Draw.io

Criar adapter semântico para `mxGraph`.

Suportar:

- páginas;
- nodes;
- edges;
- labels;
- containers;
- source/target;
- geometry;
- style;
- payload comprimido quando aplicável.

Converter para:

```text
Diagram
Node
Edge
Relationship
```

Depois correlacionar com:

- systems;
- modules;
- integrations;
- flows;
- capabilities.

---

## 11.5 PDF

Não implementar parser PDF artesanal como mecanismo principal.

Usar biblioteca madura aprovada para:

- texto;
- páginas;
- layout básico;
- tabelas quando disponível;
- metadata;
- image detection.

OCR deve ser uma capability separada e explícita.

Sem OCR disponível:

```text
status = partial
gap = image_content_not_interpreted
```

---

## 11.6 Transcrições

Preservar:

- speaker;
- timestamp;
- utterance;
- source version.

A semântica deve ser extraída por LLM:

- decision;
- hypothesis;
- requirement;
- constraint;
- action;
- architectural rationale;
- business rule;
- dependency;
- risk;
- open question.

Regex pode sugerir candidatos, mas nunca decidir o tipo final sozinha.

---

# 12. WAVE 7 — CORRELAÇÃO E INCEPTION

## 12.1 Objetivo

Fazer fontes diferentes formarem conhecimento conectado.

Exemplo:

```text
Transcrição
  "vamos migrar renovação para Salesforce"
        |
        v
Proposal
        |
        v
affects
        |
        +--> Capability: Renewal
        +--> Integration: Billing
        +--> BusinessRule: Eligibility
        +--> Topic: RenewalEvent
```

---

## 12.2 Comparação código × documento

O sistema deve responder:

```text
declared but not implemented
implemented but not documented
proposal conflicts with current behavior
decision supersedes prior proposal
source contradicts source
```

Sempre preservando a origem.

---

# 13. WAVE 8 — PUBLISHING V2 E DOCX PARA SHAREPOINT

## 13.1 Uma única publicação

Fluxo único:

```text
Knowledge Graph
  -> Publication Plan
  -> Narrative Model
  -> Diagrams
  -> DOCX
  -> Validation
  -> Manifest
  -> Delivery Package
```

Não existe outro caminho.

---

## 13.2 Estrutura do DOCX

O conteúdo principal deve ser orientado à pesquisa.

Exemplo para capability:

```text
1. Objetivo
2. Como funciona
3. Fluxo principal
4. Inputs
5. Outputs
6. Regras de negócio
7. Invariantes
8. Edge cases
9. Integrações
10. Persistência
11. Falhas e recuperação
12. Diagramas
13. Testes e evidências
14. Lacunas conhecidas
15. Rastreabilidade técnica
```

---

## 13.3 Separar content plane de audit plane

Não despejar IDs internos no meio da narrativa.

### Corpo

```text
Se o cliente possuir débito em aberto, a renovação é bloqueada.
```

### Apêndice de rastreabilidade

```text
Finding: ...
Evidence: ...
Source version: ...
Path: ...
Lines: ...
```

Isso evita contaminar busca e leitura humana com metadados operacionais.

---

## 13.4 DOCX obrigatório

`publication_status=ready` exige:

```text
[ ] semantic plan válido
[ ] DOCX gerado
[ ] DOCX estruturalmente válido
[ ] diagramas possuem equivalente textual
[ ] manifesto referencia o DOCX
[ ] nenhum documento obrigatório ausente
```

Markdown pode existir para debug/export.

Não é o artifact principal de SharePoint.

---

# 14. WAVE 9 — PROVIDERS E AGENT-AGNOSTIC

## 14.1 Contrato único

Exemplo conceitual:

```text
AgentProvider
  connect()
  capabilities()
  run(session)
  cancel()
```

A session opera com o mesmo tool protocol.

---

## 14.2 Providers

Implementar no mínimo dois providers reais antes de declarar agent-agnostic operacional.

Exemplo:

```text
Claude
Codex
```

Terceiros:

```text
Devin
Cursor
Windsurf
Antigravity
```

podem ser adicionados sem mudar o core.

---

## 14.3 Gate de independência

Core não pode importar:

```text
claude
codex
devin
cursor
windsurf
antigravity
```

Somente:

```text
agent.protocol
```

Providers importam o core; core não importa providers concretos.

---

# 15. WAVE 10 — UPDATE, INVALIDAÇÃO E RECUPERAÇÃO

## 15.1 Update

Novo snapshot:

```text
previous snapshot
  + current snapshot
  -> diff
  -> impacted evidence
  -> invalidated findings
  -> targeted reinvestigation
```

Não reanalisar todo repository por default.

---

## 15.2 Invalidar por evidência

Mudança em arquivo deve invalidar somente conhecimento dependente da versão anterior.

---

## 15.3 Reexecução

Reexecução deve ser idempotente.

Mesmo snapshot + mesmo objective + mesma evidence set:

```text
no duplicated knowledge
```

---

# 16. WAVE 11 — ACCEPTANCE REAL

## 16.1 Cenário Java

Repository com:

- controllers;
- services;
- persistence;
- Kafka;
- config;
- tests.

Aceite:

```text
[ ] identifica entrypoints
[ ] reconstrói regras
[ ] identifica inputs/outputs
[ ] identifica persistência
[ ] identifica eventos
[ ] identifica falhas
[ ] identifica edge cases
[ ] gera fluxo
[ ] apresenta evidências
```

A versão Java não deve ser limitante para leitura do source.

---

## 16.2 Cenário COBOL/JCL

Aceite:

```text
[ ] não bloqueia por falta de parser
[ ] encontra programs
[ ] segue CALL/PERFORM por investigação
[ ] associa copybooks
[ ] identifica JCL relevante
[ ] captura SQL quando presente
[ ] produz findings evidenciados
```

---

## 16.3 Cenário inception

Fontes:

```text
transcript
xlsx
drawio
docx
codebase
```

Pergunta:

```text
"Quais sistemas, regras e integrações tornam a migração complexa?"
```

Resposta deve correlacionar todas as fontes com proveniência.

---

## 16.4 Cenário SharePoint

Após análise:

```text
wiki-ai publish
```

Aceite:

```text
[ ] DOCX existe
[ ] conteúdo é pesquisável
[ ] estrutura é orientada ao domínio
[ ] não exige arquivos intermediários manuais
[ ] manifesto contém hash/revision
[ ] delivery está pronto sem promote/compile/docx manual
```

---

## 16.5 Cenário providers

Executar o mesmo objetivo com dois providers.

O workflow, schemas, tools e persistence devem ser os mesmos.

Somente transporte/provider muda.

---

# 17. Gates globais

## 17.1 Source Hygiene Gate

Falha se houver:

- comentário;
- docstring;
- TODO;
- FIXME;
- pragma;
- ignore directive.

---

## 17.2 Dead Code Gate

Falha quando:

- módulo production não é alcançável por nenhum entrypoint ou plugin registrado;
- função privada sem uso permanece;
- provider não registrado permanece;
- command handler não registrado permanece.

Exceções precisam ser explicitamente declaradas como API pública e possuir teste consumidor.

---

## 17.3 Architecture Gate

Falha se:

- core importa provider concreto;
- publishing importa CLI;
- knowledge importa publishing;
- ingestion escreve diretamente em publicação;
- agent escreve diretamente em knowledge DB;
- mais de um control plane existir;
- ciclo de imports existir;
- módulo production > 1.000 LOC.

---

## 17.4 Knowledge Gate

Falha se:

- finding `supported` não possui evidence;
- evidence não resolve;
- source hash diverge;
- knowledge depende de source version obsoleta sem invalidation.

---

## 17.5 Publishing Gate

Falha se:

- DOCX não existe;
- DOCX inválido;
- manifesto diverge;
- artifact obrigatório ausente;
- representação visual não possui equivalente textual;
- conteúdo afirma completude com gaps críticos ocultos.

---

# 18. O que NÃO deve ser feito

É proibido implementar a remediação como:

```text
adicionar outro planner
adicionar mais regex
adicionar mais campos ao contrato antigo
adicionar parser por linguagem como solução principal
adicionar wrapper no codescan
adicionar wrapper no wk/cli.py
adicionar outro comando de compatibilidade
adicionar novo diretório mantendo antigo
renomear legacy para compat
marcar deprecated
deixar código morto para remover depois
manter duas publicações
manter dois DOCX generators de produto
```

---

# 19. Estratégia de reconstrução

Quando um componente atual estiver parcialmente correto:

```text
1. escrever teste do comportamento que realmente queremos preservar
2. criar módulo novo
3. implementar a menor lógica necessária
4. fazer o teste passar
5. remover imports do módulo antigo
6. apagar módulo antigo
```

Não utilizar:

```text
1. copiar arquivo inteiro
2. comentar partes antigas
3. manter aliases
4. criar facade
```

---

# 20. Definition of Done final

A reconstrução termina somente quando:

```text
[ ] existe um único codebase analyzer
[ ] existe um único ingestion pipeline
[ ] existe um único knowledge model
[ ] existe um único publishing pipeline
[ ] existe um único DOCX path
[ ] não existe código legacy/compat
[ ] não existe comments/docstrings no código
[ ] não existe codescan
[ ] não existe sbindex
[ ] não existe CLI monolítica
[ ] LLM investiga repository dinamicamente
[ ] LLM não possui escrita arbitrária
[ ] evidência é obrigatória para behavior supported
[ ] análise funciona sem parser específico da linguagem
[ ] Java é analisável independentemente da versão
[ ] COBOL/JCL possuem fallback investigativo
[ ] XLSX possui adapter real
[ ] draw.io possui interpretação de grafo
[ ] transcript usa semantic extraction
[ ] DOCX é gerado automaticamente
[ ] DOCX é orientado à pesquisa
[ ] SharePoint delivery não exige pipeline manual
[ ] dois providers reais executam o mesmo protocol
[ ] update invalida por source version
[ ] perguntas de inception correlacionam código + fontes
```

---

# 21. Ordem obrigatória de execução

```text
W0  CLEAN-SLATE
      ↓
W1  APPLICATION KERNEL / UX
      ↓
W2  REPOSITORY HARNESS
      ↓
W3  INVESTIGATION ENGINE
      ↓
W4  KNOWLEDGE MODEL V2
      ↓
W5  DEEP CODEBASE ANALYSIS
      ↓
W6  MULTI-FORMAT INGESTION
      ↓
W7  CORRELATION / INCEPTION
      ↓
W8  PUBLISHING / DOCX
      ↓
W9  PROVIDERS / AGENT-AGNOSTIC
      ↓
W10 UPDATE / INVALIDATION
      ↓
W11 REAL ACCEPTANCE
```

A seta entre W0 e W1 é um hard gate.

Nenhuma alteração de produto pode atravessá-la enquanto houver código condenado no repository.

---

# 22. Resultado esperado após a Wave 0

O repository deve parecer menor, não maior.

O objetivo não é "preservar trabalho".

O objetivo é preservar somente valor técnico.

Estado esperado:

```text
antes:
  múltiplos pipelines
  múltiplas CLIs
  múltiplos stores
  compatibilidade
  god modules
  comentários extensos
  semantic extraction fraca
  providers misturados ao core

depois da W0:
  package único
  kernel pequeno
  storage mínimo
  evidence primitives
  snapshot/inventory
  publication transaction primitives
  zero comentários
  zero compatibilidade
  zero pipeline paralelo
  zero código morto conhecido
```

Somente depois deste estado a reconstrução deve começar.

---

# 23. Princípio final

O Wiki-AI não deve tentar prever deterministicamente todo comportamento antes de permitir que a LLM investigue.

Também não deve entregar o repository irrestritamente para uma LLM.

A arquitetura alvo é:

```text
LLM com autonomia investigativa
+
tools determinísticas
+
contexto governado
+
evidência versionada
+
verificação mecânica
+
persistência estruturada
+
publicação útil ao humano
```

O Clean-Slate é obrigatório porque qualquer reconstrução executada sobre os pipelines atuais corre o risco de herdar exatamente as premissas que precisam ser removidas.
