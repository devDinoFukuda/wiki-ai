# Implementação do pacote de conhecimento de codebase

Status: desenho em elaboração. Este documento registra decisões confirmadas e organiza a implementação; itens marcados como **pendentes** ainda dependem do grilling.

## 1. Objetivo

Reestruturar a engenharia reversa de codebases para produzir um pacote canônico de conhecimento sobre o estado atual do sistema, rastreável até o código analisado e adequado à recuperação por agentes de IA.

O resultado não deve conter planejamento de implementação, priorização de produto, arquitetura-alvo ou propostas de evolução. Esses conteúdos pertencem a fluxos prospectivos separados.

## 2. Decisões confirmadas

1. O produto canônico da análise de codebase descreve exclusivamente o estado atual verificável.
2. Reimplementação e evolução não fazem parte do fluxo canônico de codebase.
3. A raiz publicável passa de `sdd/` para `knowledge/`.
4. Registros operacionais da análise não fazem parte do pacote canônico.

## 3. Não objetivos

- Gerar PRD.
- Priorizar requisitos.
- Prescrever refatorações.
- Definir arquitetura futura.
- Produzir plano ou tarefas de implementação.
- Alterar o codebase analisado.
- Substituir os agentes especialistas de Ask, Refinamento, PRD ou Evoluir.
- Implementar neste trabalho um conector de publicação para Microsoft 365.

## 4. Problemas do desenho atual

### 4.1 SDD mistura estado atual e estado futuro

O contrato atual exige `requirements.md`, `design.md` e `tasks.md` por unit. O primeiro pode sugerir intenção de produto não comprovada e o último é explicitamente um plano prospectivo de reimplementação.

### 4.2 Evidência é reconstruída tarde demais

O estágio `evidence` executa busca lexical depois da geração das specs. Quando o tópico é uma classificação como `codebases/tr/app`, seus segmentos viram termos de busca, embora não representem o domínio da aplicação.

### 4.3 Síntese duplica conhecimento

`confirmed.md` e `inferred.md` repetem afirmações que já deveriam carregar confiança e evidência no artefato original. A duplicação cria múltiplos candidatos a fonte canônica e pode introduzir divergência.

### 4.4 Profundidade por módulo é limitada por amostragem

O `agent-pack` usa por padrão 45 KB, quatro arquivos por módulo e quarenta linhas por arquivo. Esse limite controla contexto, mas não prova cobertura suficiente de comportamento, contratos, persistência, configuração e erros.

### 4.5 Publicação usa descoberta ampla de arquivos

`publish` coleta Markdown de `sdd/**/*.md` e `modules/*.md`. O mecanismo é orientado a localização, não a um manifesto de artefatos aprovados, e pode publicar intermediários que apenas coincidem com o padrão de caminho.

### 4.6 Proveniência e confiança estão acopladas

`source_type: agent-output` determina `confidence: unverified`, mesmo quando a afirmação possui citação válida e passou por verificação mecânica. Origem, forma de derivação, estado de revisão e confiança da afirmação são dimensões diferentes.

### 4.7 Tópicos hierárquicos têm filtro exato

`codebases/tr/app` é armazenável como tópico, mas uma busca filtrada por `codebases/tr` não inclui automaticamente seus descendentes. A hierarquia existe no nome, não na recuperação.

## 5. Separação arquitetural

### 5.1 Workspace operacional

Continua fora do repositório analisado e contém apenas coordenação e evidências intermediárias:

```text
.codescan/<repo-id>/
├── state.json
├── config.json
├── surface.json
├── plan.json
├── agent-packs/
├── agent-outputs/
├── agent-runs/
├── reports/
│   ├── audit.json
│   ├── coverage.json
│   └── verification.json
└── knowledge/
```

`knowledge/` é a única subárvore elegível para publicação. Os demais caminhos nunca entram no corpus.

### 5.2 Pacote canônico

A estrutura recomendada abaixo ainda depende da confirmação individual dos nomes e artefatos:

```text
knowledge/
├── index.md
├── system/
│   ├── overview.md
│   ├── inventory.md
│   └── dependencies.md
├── units/
│   └── <unit-id>/
│       ├── overview.md
│       ├── behavior.md
│       ├── implementation.md
│       ├── contracts.md
│       └── flows.md
├── domain/
│   ├── glossary.md
│   ├── rules.md
│   ├── state-machines.md
│   └── permissions.md
├── architecture/
│   ├── overview.md
│   ├── c4-context.md
│   ├── c4-containers.md
│   ├── c4-components.md
│   ├── integrations.md
│   └── deployment.md
├── data/
│   ├── dictionary.md
│   └── erd.md
├── contracts/
│   ├── apis/
│   ├── events.md
│   ├── messages.md
│   └── scheduled-jobs.md
├── analysis/
│   ├── coupling.md
│   ├── coverage.md
│   └── observed-risks.md
└── traceability/
    ├── code-knowledge-matrix.md
    ├── confidence-report.md
    └── gaps.md
```

Artefatos condicionais só existem quando o codebase contém evidência aplicável. Diretórios e documentos vazios são inválidos.

## 6. Pipeline-alvo recomendado

```text
surface
  -> units
  -> domain
  -> architecture
  -> review
  -> publish
  -> promote
  -> compile
```

### 6.1 `surface`

Responsabilidade determinística:

- identificar revisão Git analisada;
- inventariar linguagens e frameworks;
- extrair dependências de manifests;
- identificar entry points, build, testes e deployment;
- propor units candidatas;
- calcular acoplamento com cobertura declarada;
- iniciar o manifesto de análise.

### 6.2 `units`

Responsabilidade de escavação:

- analisar cada unit com cobertura mensurável;
- registrar responsabilidade, fronteiras e superfície pública;
- extrair comportamentos, regras, erros e invariantes;
- documentar implementação atual;
- coletar evidência no momento da análise;
- registrar arquivos analisados, ignorados e não acessíveis.

### 6.3 `domain`

Responsabilidade transversal:

- consolidar glossário de domínio;
- consolidar regras observadas;
- mapear estados e transições;
- mapear papéis e permissões;
- registrar lacunas sem transformá-las em suposições.

### 6.4 `architecture`

Responsabilidade sistêmica:

- consolidar contexto, containers e componentes;
- documentar integrações e protocolos;
- consolidar modelo de dados;
- registrar deployment observado;
- registrar riscos técnicos observados;
- produzir rastreabilidade entre código e conhecimento.

Não pode produzir arquitetura-alvo ou prescrever transformação.

### 6.5 `review`

Responsabilidade de qualidade:

- verificar citações contra a revisão analisada;
- verificar cobertura das units;
- procurar contradições entre artefatos;
- rebaixar afirmações não sustentadas;
- classificar lacunas;
- validar diagramas;
- aprovar o manifesto de publicação.

`verify` e `audit` tornam-se mecanismos internos do review, não fases de conhecimento independentes.

## 7. Contrato de evidência e confiança

### 7.1 Afirmações

Cada afirmação relevante deve ser classificada:

- 🟢 **Confirmada**: sustentada diretamente por uma ou mais referências `arquivo:linha` válidas na revisão analisada.
- 🟡 **Inferida**: conclusão plausível baseada em padrões explicitados; não pode ser apresentada como fato.
- 🔴 **Lacuna**: não determinável pelo codebase; exige fonte externa ou validação humana.

### 7.2 Artefatos

O estado do artefato não substitui a confiança das afirmações. Metadados mínimos recomendados:

```yaml
---
id: codebase-tr-app-orders-behavior
topic: codebases/tr/app
artifact_kind: behavior
system_id: app
unit_id: orders
unit_kind: module
repository: <origem>
revision: <git-sha>
derivation: agent-extracted
review_status: machine-verified
captured_at: <timestamp>
---
```

Valores recomendados:

- `derivation`: `deterministic`, `agent-extracted`, `human-authored`;
- `review_status`: `unreviewed`, `machine-verified`, `human-reviewed`;
- `artifact_kind`: vocabulário fechado por contrato.

O schema definitivo desses campos permanece pendente.

## 8. Manifesto de publicação

Criar `knowledge-manifest.json` no workspace operacional. Ele não é publicado como conteúdo de consulta; controla o que pode ser publicado.

Estrutura mínima recomendada:

```json
{
  "schema": "wiki-ai.codebase-knowledge.v1",
  "topic": "codebases/tr/app",
  "repository": "<origem>",
  "revision": "<git-sha>",
  "artifacts": [
    {
      "path": "knowledge/units/orders/behavior.md",
      "kind": "behavior",
      "review_status": "machine-verified",
      "sha256": "<hash>"
    }
  ]
}
```

Invariantes:

- somente caminhos presentes no manifesto podem ser publicados;
- todo caminho deve permanecer dentro de `knowledge/`;
- todo artefato deve existir e ter o hash esperado;
- artefato reprovado no review não entra no manifesto;
- reexecução com o mesmo repositório, revisão, tópico e path deve ser idempotente;
- mudança de revisão deve produzir supersessão explícita, não duplicação silenciosa.

## 9. Mudanças por componente

### 9.1 `scripts/codescan/state.py`

- Introduzir versão do schema de estado.
- Substituir a sequência antiga por stages do conhecimento.
- Remover dependências de `evidence` e `synth`.
- Renomear tracking de `specs` para `units` ou estágio equivalente.
- Recusar retomada automática de estado incompatível.

### 9.2 `scripts/codescan/sdd.py`

Recomendação: substituir por `scripts/codescan/knowledge.py`, mantendo um adaptador temporário apenas se necessário para migração.

- Definir o contrato dos artefatos de conhecimento.
- Remover rules de `requirements.md`, `design.md`, `tasks.md`.
- Remover `confirmed.md` e `inferred.md`.
- Adicionar validação por `artifact_kind`.
- Adicionar validação de cobertura.
- Validar ausência de linguagem prospectiva nos artefatos canônicos.
- Manter gates de citações, Mermaid, conteúdo genérico e proveniência.

### 9.3 `scripts/codescan/agentmerge.py`

- Substituir blocos `SPEC` por blocos `UNIT`.
- Definir arquivos obrigatórios e condicionais por unit.
- Registrar `artifact_kind`, unit e hash no manifesto da execução.
- Impedir que arquivos prospectivos sejam integrados em `knowledge/`.

Contrato recomendado, pendente de confirmação:

```text
=== UNIT: <unit-id> ===
--- overview.md ---
...
--- behavior.md ---
...
--- implementation.md ---
...
=== END ===
```

### 9.4 `scripts/codescan/agentpack.py`

- Trocar limite fixo como critério de suficiência por orçamento adaptativo.
- Selecionar arquivos por papel: entrada, domínio, persistência, contrato, configuração, erro e teste.
- Permitir solicitações incrementais de evidência.
- Calcular cobertura por unit.
- Bloquear conclusão quando arquivos críticos conhecidos não foram examinados.

### 9.5 `scripts/codescan/evidence.py`

- Retirar do pipeline canônico a geração lexical baseada no tópico.
- Reaproveitar apenas primitivas seguras de leitura e verificação, se úteis.
- Não usar taxonomia corporativa como consulta de código.

### 9.6 `scripts/codescan/coupling.py`

- Gravar em `knowledge/analysis/coupling.md`.
- Renomear “Plano Abstração × Instabilidade” para “Mapa Abstração × Instabilidade”.
- Separar confiança de Ce/Ca/I da confiança de abstração e zona.
- Publicar número de imports resolvidos e não resolvidos.
- Publicar linguagens e arquivos cobertos.
- Não converter zona em recomendação de evolução.

### 9.7 `scripts/codescan/cli.py`

- Atualizar commands, choices e roteamento de stages.
- Gerar o manifesto de publicação após review aprovado.
- Expor estado e próximo passo usando nomes do domínio de conhecimento.
- Remover `sdd-scaffold` do happy path; avaliar sua remoção definitiva.
- Preservar fan-out, identidade do agente, hashes e checkpoints.

### 9.8 `scripts/wk/cli.py`

- Fazer `publish` consumir o manifesto, não varrer Markdown por glob.
- Tornar publicação idempotente.
- Registrar revisão e supersessão.
- Impedir publicação de relatórios operacionais.
- Manter promoção humana quando a derivação envolver agente.
- Não tratar origem por agente como sinônimo de afirmação não verificável.

### 9.9 `scripts/sbindex/frontmatter.py` e `store.py`

- Versionar o schema do índice.
- Adicionar campos de escopo e revisão necessários para recuperação.
- Separar derivação de review status.
- Definir migração ou reconstrução automática do índice derivado.
- Preservar erro para filtros desconhecidos.

### 9.10 `scripts/sbindex/cli.py`

- Adicionar filtro hierárquico de tópico, por exemplo `topic_prefix`.
- Permitir filtro por sistema, unit, artifact kind, revisão e review status.
- Evitar resultados duplicados de `raw/` e `wiki/` para o mesmo artefato lógico.
- Exibir revisão e confiança junto dos resultados.

### 9.11 Documentação e empacotamento

Atualizar de forma atômica:

- `README.md`;
- `INSTALL.md`;
- `SKILL.md`;
- `schema.md`;
- `operations/ingest-codebase.md`;
- `references/sdd-contract.md`, substituído por contrato de conhecimento;
- `PLANO_EXECUCAO_TECNICO.md`;
- assets incorporados em `wk.pyz`.

Remover a documentação contraditória sobre suporte a `synth`, pois o estágio deixa de existir no fluxo novo.

## 10. Estratégia de migração recomendada

### 10.1 Compatibilidade

- Não fazer dual-write em `sdd/` e `knowledge/`; isso recriaria a duplicação que a mudança pretende eliminar.
- Permitir dual-read temporário apenas para identificar workdirs antigos.
- Workdir antigo deve exigir migração explícita ou nova análise.
- Estado novo recebe schema próprio e não é silenciosamente compatível com o anterior.

### 10.2 Mapeamento inicial

| Artefato antigo | Destino recomendado | Regra |
|---|---|---|
| `inventory.md` | `system/inventory.md` | migrável |
| `dependencies.md` | `system/dependencies.md` | migrável |
| `code-analysis.md` | `system/overview.md` ou units | requer decomposição |
| `modules/*.md` | `units/*/overview.md` | requer revisão |
| `requirements.md` | `behavior.md` | pendente de confirmação |
| `design.md` | `implementation.md` | pendente de confirmação |
| `tasks.md` | nenhum | não publicar |
| `domain.md` | `domain/glossary.md` + `domain/rules.md` | requer separação |
| `architecture.md` | `architecture/overview.md` | migrável com revisão |
| `coupling.md` | `analysis/coupling.md` | regenerar preferencialmente |
| `confirmed.md` | nenhum | duplicação |
| `inferred.md` | nenhum | confiança volta à afirmação original |
| `confidence-report.md` | `traceability/confidence-report.md` | regenerar |
| `gaps.md` | `traceability/gaps.md` | regenerar |

### 10.3 Stores existentes

A política para artefatos já promovidos permanece pendente. A implementação não deve apagar fontes antigas automaticamente. Opções futuras incluem supersessão por nova revisão ou migração administrada.

## 11. Plano de entrega

### Fase 0 — Congelar o contrato

- Resolver nomes dos artefatos por unit.
- Resolver granularidade das units.
- Resolver schema mínimo de metadados.
- Resolver política de revisão e promoção.
- Criar fixtures douradas de um pacote válido e inválido.

Saída: contrato `wiki-ai.codebase-knowledge.v1` aprovado.

### Fase 1 — Modelo e validação

- Implementar schema de estado versionado.
- Implementar contrato `knowledge/`.
- Implementar manifesto de publicação.
- Implementar gates de path, hash, tipo e linguagem prospectiva.

Saída: geração manual de fixture validada sem publicar.

### Fase 2 — Geração por agentes

- Adaptar agent packs para cobertura.
- Adaptar fan-out e merge para units.
- Adaptar domain e architecture.
- Implementar review e relatórios.

Saída: pipeline completo em workdir isolado.

### Fase 3 — Corpus e recuperação

- Publicação por manifesto.
- Idempotência e supersessão.
- Novos metadados.
- Filtro hierárquico de tópicos.
- Deduplicação lógica entre raw e wiki.

Saída: pacote recuperável por `codebases/<sigla>/<app>`.

### Fase 4 — Migração e remoção do legado

- Detectar workdirs SDD antigos.
- Aplicar política de migração aprovada.
- Remover `evidence`, `synth` e scaffolds obsoletos.
- Remover documentação e testes do contrato antigo.
- Reconstruir `wk.pyz`.

Saída: somente o contrato novo é gravável.

## 12. Estratégia de testes

### Unitários

- path de artefato permanece dentro de `knowledge/`;
- `artifact_kind` pertence ao vocabulário permitido;
- afirmação confirmada exige citação válida;
- revisão divergente invalida citação;
- conteúdo prospectivo é rejeitado no pacote canônico;
- unit obrigatória incompleta não fecha;
- coupling declara cobertura e incerteza;
- topic prefix inclui descendentes sem incluir irmãos;
- manifesto adulterado falha por hash;
- publicação repetida é idempotente.

### Integração

- `surface -> units -> domain -> architecture -> review` com checkpoints;
- retomada após interrupção;
- fan-out com agentes distintos;
- merge concorrente sem perda de estado;
- review rebaixa afirmação sem evidência;
- publish inclui somente o manifesto;
- supersessão entre revisões do mesmo repositório;
- reindex não retorna duplicatas lógicas.

### Regressão

- ingestão de transcrições e documentos continua funcionando;
- promoção humana continua registrada;
- lint continua ignorando relatórios operacionais;
- comandos de store e busca preservam compatibilidade não relacionada a codebase;
- build de `wk.pyz` corresponde aos fontes.

### Teste comparativo

Executar o pipeline antigo e o novo sobre a mesma codebase de referência e medir:

- cobertura de arquivos por unit;
- quantidade de afirmações confirmadas;
- citações inválidas;
- contradições detectadas;
- duplicação de chunks;
- artefatos prospectivos presentes no corpus;
- precisão de perguntas respondidas pelos agentes consumidores.

## 13. Critérios de aceite

1. Nenhum arquivo canônico é gravado sob `sdd/`.
2. Nenhum artefato prospectivo é publicável pelo manifesto.
3. Todo artefato publicado declara repositório e revisão analisada.
4. Toda afirmação confirmada possui evidência verificável.
5. Toda unit declara sua cobertura.
6. `publish` é idempotente para a mesma revisão.
7. Nova revisão supersede a anterior sem duplicação silenciosa.
8. `topic_prefix=codebases/tr` recupera `codebases/tr/app`, mas não `codebases/x/app`.
9. Relatórios operacionais não entram no corpus.
10. Busca não retorna cópias lógicas de raw e wiki como resultados independentes.
11. O pipeline pode ser retomado sem regenerar units concluídas e verificadas.
12. A suíte de testes do corpus não sofre regressão.

## 14. Riscos

| Risco | Impacto | Mitigação |
|---|---|---|
| Renomear sem mudar semântica | novo diretório, mesmos problemas | contrato e fixtures antes do runtime |
| Cobertura adaptativa elevar custo | análise lenta em monorepos | orçamento por unit e checkpoints |
| Fragmentação excessiva | piora do retrieval | artefatos atômicos, mas não microscópicos |
| Metadados demais | contrato difícil de manter | schema mínimo e vocabulários fechados |
| Migração duplicar corpus | respostas contraditórias | supersessão e publicação idempotente |
| Review validar só formato | falsa confiança | gates semânticos e teste comparativo |
| Inferência virar fato | contaminação dos agentes | confiança por afirmação e gaps explícitos |

## 15. Fila de decisões do grilling

As decisões devem ser resolvidas nesta ordem, uma por vez:

1. Nomes e conteúdo dos artefatos obrigatórios por unit.
2. Regra de escolha da granularidade de unit.
3. Schema mínimo de metadados.
4. Condição para um artefato extraído por agente tornar-se canônico.
5. Política de supersessão entre revisões do mesmo codebase.
6. Política para stores e workdirs do contrato SDD anterior.

Nenhuma recomendação pendente desta seção deve ser tratada como decisão confirmada.
