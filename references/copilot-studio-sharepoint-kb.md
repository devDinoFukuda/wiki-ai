# Base de conhecimento Copilot Studio no SharePoint — evidência oficial

## Contexto

| Item | Valor |
|---|---|
| Objetivo | Determinar formato, nomenclatura, estrutura interna e organização SharePoint para knowledge base de agentes Copilot Studio |
| Data de acesso de todas as fontes | 2026-08-06 |
| Modelo dos subagentes | `claude-sonnet-5` |
| Subagentes executados | `copilot_formats`, `sharepoint_structure`, `validation` (adversarial) |
| Escopo excluído | M365 Copilot declarative agents / Agent Builder (produto distinto — ver §7) |

## Legenda de classificação

| Marca | Significado |
|---|---|
| `FATO` | Afirmação literal em fonte oficial, com URL |
| `INFERÊNCIA` | Dedução minha, premissas declaradas |
| `DADO NÃO ENCONTRADO` | Doc oficial não trata do tema |
| `NÃO DIVULGADO OFICIALMENTE` | Número/limite não publicado |
| `EVIDÊNCIA CONTRADITÓRIA` | Duas fontes oficiais divergem |
| `NÃO SE APLICA` | Fonte oficial, produto errado |

---

## 1. Decisão preliminar: qual caminho de ingestão

`FATO` — Três caminhos distintos, com formatos e recursos diferentes.

| Caminho | Como se adiciona | Onde o conteúdo vive | Indexação |
|---|---|---|---|
| **A. Conector SharePoint** | `Add knowledge` → SharePoint → URL de site/pasta | Permanece no SharePoint | Microsoft Search / GraphSearch, tempo real |
| **B. Upload files > SharePoint** | `Add knowledge` → Upload files → SharePoint → browse | Cópia indexada em Dataverse | Vetores em Dataverse, sync 4–6 h |
| **C. Upload de arquivo direto** | `Add knowledge` → Documents (upload) | Dataverse | Chunking + vector embeddings |

Fontes: [knowledge-add-sharepoint](https://learn.microsoft.com/en-us/microsoft-copilot-studio/knowledge-add-sharepoint) · [knowledge-add-unstructured-data](https://learn.microsoft.com/en-us/microsoft-copilot-studio/knowledge-add-unstructured-data) · [knowledge-add-file-upload](https://learn.microsoft.com/en-us/microsoft-copilot-studio/knowledge-add-file-upload)

| Diferença crítica | A (conector) | B (upload SP) | C (upload direto) |
|---|---|---|---|
| Bibliotecas de documentos | suportadas | **"Currently, document libraries are not supported"** `FATO` | n/a |
| Sensitivity labels | "Supported" (permission trimming) | "Not supported" | não suportado |
| Citação nível de página (PDF) | não (doc-level) | **sim** (limiar ~4 KB) | não |
| Formatos | DOC/DOCX, PPT/PPTX, PDF | DOC/DOCX, PPT/PPTX, PDF, XLS/XLSX | 15 famílias, incl. `.md` |

---

## 2. FORMATOS

### 2.1 Matriz oficial

| Formato | A. Conector SP | B. Upload SP | C. Upload direto | Estrutura preservada | Tabelas | Manutenção | Melhor uso | Limitação documentada |
|---|---|---|---|---|---|---|---|---|
| `.docx` / `.doc` | ✅ | ✅ | ✅ | headings nativos | `DADO NÃO ENCONTRADO` | alta (Word/Web) | **geral, procedimentos, políticas, FAQ** | tamanho (ver §6) |
| `.pdf` | ✅ | ✅ | ✅ | depende da origem | `DADO NÃO ENCONTRADO` | baixa (regerar) | distribuição imutável; citação por página (só B) | imagens só via alt-text |
| `.pptx` / `.ppt` | ✅ | ✅ | ✅ | fraca | não | média | material de treinamento | — |
| `.xlsx` / `.xls` | ⚠️ aceita, degradado | ✅ | ✅ | n/a | célula | alta | **evitar p/ dados tabulares** | "Semantic search has limitations for cell indexing" `FATO` |
| Página moderna SharePoint (`.aspx`) | ✅ | n/a | n/a | headings nativos | — | alta | conteúdo curto e navegável | clássica `.aspx` e SPFx **não** funcionam `FATO` |
| `.md` | ❌ | ❌ | ✅ | headings nativos | pipe | alta | só upload direto | **ausente da lista SharePoint** `FATO` |
| `.txt` / `.log` | ❌ | ❌ | ✅ | nenhuma | não | alta | só upload direto | idem |
| `.csv` | ❌ | ❌ | ✅ | n/a | sim | alta | só upload direto | idem |
| `.html` / `.htm` | ❌ | ❌ | ✅ | headings | sim | média | só upload direto | idem |
| `.json` / `.yaml` / `.xml` | ❌ | ❌ | ✅ | n/a | n/a | alta | só upload direto | idem |
| `.odt` / `.ods` / `.odp`, `.epub`, `.rtf`, `.pages`/`.key`/`.numbers`, `.tex` | ❌ | ❌ | ✅ | varia | varia | varia | só upload direto | idem |
| imagem avulsa, vídeo, áudio, executável | ❌ | ❌ | ❌ | — | — | — | — | "can't be used as uploaded documents" `FATO`; MP4 explicitamente barrado em B |

Fontes: [requirements-quotas](https://learn.microsoft.com/en-us/microsoft-copilot-studio/requirements-quotas) · [knowledge-add-file-upload](https://learn.microsoft.com/en-us/microsoft-copilot-studio/knowledge-add-file-upload) · [knowledge-add-unstructured-data](https://learn.microsoft.com/en-us/microsoft-copilot-studio/knowledge-add-unstructured-data) · [nlu-boost-node](https://learn.microsoft.com/en-us/microsoft-copilot-studio/nlu-boost-node) · [sharepoint-no-response](https://learn.microsoft.com/en-us/troubleshoot/power-platform/copilot-studio/knowledge/sharepoint-no-response)

Citações literais verificadas pelo subagente `validation`:
- Conector SP: `"SharePoint sites containing the following file types can be used as knowledge sources: - Word documents (DOC/DOCX) - PowerPoint (PPT/PPTX) - PDF files"`
- Upload SP: `"Word: .doc, .docx / PowerPoint: .ppt, .pptx / PDF: .pdf / Excel: .xls, .xslx"` + `"Currently, document libraries are not supported"`
- Upload direto: `"Text (.txt, .md, .log)"` consta da lista `Supported document types`
- XLSX no conector: `"While you can add structured files, such as XLSX files from SharePoint, agents can't write and run code. Responses to analytical questions might not be optimal."`

### 2.2 Vereditos por caso de uso

| Pergunta | Resposta | Base |
|---|---|---|
| Melhor formato geral (conteúdo no SharePoint) | **DOCX** | `INFERÊNCIA`. Premissas: (a) suportado nos 3 caminhos `FATO`; (b) guidance oficial exige `"Consistent use of header styles (H1, H2, and so on)"` — estilos de heading são nativos e verificáveis em DOCX; (c) editável in-place no SharePoint sem regerar |
| Melhor p/ procedimentos | **DOCX** com listas numeradas | `FATO` (guidance de listas, §4) + `INFERÊNCIA` de formato |
| Melhor p/ políticas | **DOCX**; PDF se houver exigência de imutabilidade | `INFERÊNCIA`. Premissa: PDF perde editabilidade e obriga regeração a cada revisão |
| Melhor p/ FAQ | **DOCX** ou **página moderna SharePoint**, 1 pergunta por heading | `FATO` (headings) + `INFERÊNCIA` |
| Melhor p/ dados tabulares | **Lista SharePoint** (não arquivo) | `FATO`: XLSX tem `"limitations for cell indexing"`; listas SP são fonte de conhecimento própria e documentada |
| Formatos a evitar | `.md`, `.txt`, `.csv`, `.html`, `.json` **se o conteúdo mora no SharePoint** | `FATO` por ausência nas listas A e B |
| Formatos proibidos | imagem avulsa, vídeo, áudio, executável, `.aspx` clássica, páginas com SPFx | `FATO` |

> ⚠️ **Markdown**: `FATO` — `.md` é suportado **exclusivamente** no upload direto de arquivo ao agente. Não é suportado em nenhum dos dois caminhos SharePoint. Se o requisito é "armazenar no SharePoint", `.md` está fora.

---

## 3. NOMES, TÍTULOS E DESCRIÇÕES

### 3.1 Matriz de influência

| Elemento | Influencia consulta? | Classificação | Padrão recomendado | Exemplo | Evidência |
|---|---|---|---|---|---|
| **Nome físico do arquivo** | Indireto (SharePoint Search); **não confirmado** no retrieval do Copilot Studio. Doc afirma que **queries citando o nome do arquivo não são respondidas** | `FATO` (SP Search: `ows_BaseName` → managed property `Title`, Searchable=Yes) + `DADO NÃO ENCONTRADO` (ponte com Copilot Studio) | `<dominio>-<assunto>-<escopo>.docx`, sem caractere inválido, caminho total < 400 chars | `rh-licenca-parental-br.docx` | [crawled-and-managed-properties](https://learn.microsoft.com/en-us/sharepoint/technical-reference/crawled-and-managed-properties-overview) · [requirements-quotas](https://learn.microsoft.com/en-us/microsoft-copilot-studio/requirements-quotas) |
| **Coluna `Title` do SharePoint** | **SIM — comprovado** | `FATO` (duplo): (a) managed property `Title` Searchable=Yes/Queryable=Yes; (b) é 1 dos 4 atributos de **filtro** da fonte SharePoint no Copilot Studio | Frase completa e descritiva, não sigla | `Política de licença parental — Brasil` | [knowledge-add-sharepoint#filter-your-sharepoint-source](https://learn.microsoft.com/en-us/microsoft-copilot-studio/knowledge-add-sharepoint) |
| **Título interno do documento** (H1 / propriedade Office Title) | SharePoint Search: sim (`Office:2` → `Title`). Copilot Studio: guidance oficial exige headings estruturados | `FATO` (SP Search) + `FATO` (guidance de estrutura, §4) | H1 único = mesmo texto da coluna `Title` | `# Política de licença parental — Brasil` | [optimization-sharepoint](https://learn.microsoft.com/en-us/microsoft-365/copilot/employee-self-service/optimization-sharepoint) |
| **Coluna `Description` do SharePoint** | **NÃO** em busca full-text | `FATO`: managed property `Description` é **Searchable=No**, Queryable=Yes | Uso humano/refinador apenas | — | [crawled-and-managed-properties](https://learn.microsoft.com/en-us/sharepoint/technical-reference/crawled-and-managed-properties-overview) |
| **Descrição da _knowledge source_** (campo do maker no Copilot Studio, ≠ coluna SP) | **SIM** — mas para o orquestrador **escolher a fonte**, não para ranquear dentro dela | `FATO`: `"Add a name and a description. The description should be as detailed as possible, especially if generative AI is enabled, as the description aids generative orchestration"` | Detalhada: domínio, público, tipo de pergunta que responde | `Políticas de RH do Brasil: férias, licenças, benefícios, jornada. Use para perguntas de colaboradores sobre direitos trabalhistas.` | [nlu-generative-answers-sharepoint-onedrive](https://learn.microsoft.com/en-us/microsoft-copilot-studio/nlu-generative-answers-sharepoint-onedrive) |
| **Tags / Enterprise Keywords** | **NÃO** em busca full-text | `FATO`: managed property `Keywords` Searchable=No. Upload SP: `"There's no support for glossaries or synonyms"` | — | — | idem |
| **Colunas de metadados customizadas** | `EVIDÊNCIA CONTRADITÓRIA` — ver §3.2 | — | ver §3.2 | — | — |
| **Nome de biblioteca / pasta** | Define **escopo**, não ranking | `FATO` (escopo) + `DADO NÃO ENCONTRADO` (ranking) | Nome curto, sem acento, sem espaço, estável | `politicas-rh` | [knowledge-add-sharepoint](https://learn.microsoft.com/en-us/microsoft-copilot-studio/knowledge-add-sharepoint) |
| **Título exibido na citação** | — | `NÃO CONFIRMADO` — nenhuma página oficial equaciona literalmente "título da citação" com nome do arquivo | — | — | limite Teams: `~80 chars`, máx. 20 citações `FATO` |

### 3.2 ⚠️ Contradição sobre metadados customizados

| Fonte | Afirma |
|---|---|
| [optimization-sharepoint](https://learn.microsoft.com/en-us/microsoft-365/copilot/employee-self-service/optimization-sharepoint) (ms.date 2025-11-05, upd 2026-06-04) | `"Using SharePoint metadata is a proven best practice for improving both the accuracy and completeness of LLM responses, including those generated by agents."` · `"Always map important site columns to managed properties in the SharePoint Search Schema."` · Exemplo literal: mapear coluna `Country/Region` → `RefinableString100` para responder `"What is my parental leave policy in Germany?"` |
| [requirements-quotas](https://learn.microsoft.com/en-us/microsoft-copilot-studio/requirements-quotas) (ms.date 2026-06-18, upd 2026-08-04) | `"Metadata filtering at the folder or knowledge source level isn't supported."` · `"Queries related to column metadata filtering properties aren't supported. For example... 'List all files with a column name of X and a value of Y.'"` |

`EVIDÊNCIA CONTRADITÓRIA`. Leitura conciliadora possível (`INFERÊNCIA`, premissa: as duas frases tratam de mecanismos distintos) — metadado mapeado a managed property influencia **relevância/retrieval** via Microsoft Search, enquanto **query explícita de filtro por coluna** pelo usuário final não é suportada. A doc **não faz essa conciliação**; a página mais recente é a restritiva.

### 3.3 Restrições duras de nomenclatura

| Restrição | Valor | Fonte |
|---|---|---|
| Caracteres proibidos | `" * : < > ? / \ |` | [restrictions-and-limitations](https://support.microsoft.com/en-us/office/restrictions-and-limitations-in-onedrive-and-sharepoint-64883a5d-228e-48f5-b3d2-eb39e07630fa) |
| Espaço inicial/final | proibido | idem |
| Nomes reservados | `.lock`, `CON`, `PRN`, `AUX`, `NUL`, `COM0-9`, `LPT0-9`, `_vti_`, `desktop.ini` | idem |
| Prefixos proibidos | arquivo/pasta iniciando com `~$`; pasta com `~` | idem |
| Caminho decodificado total | **400 caracteres** (site + biblioteca + pastas + arquivo) | idem |

---

## 4. ESTRUTURA INTERNA — guidance oficial

`FATO`. Fonte primária única: **[Optimizing SharePoint content for Employee Self-Service agents](https://learn.microsoft.com/en-us/microsoft-365/copilot/employee-self-service/optimization-sharepoint)** — ms.date 2025-11-05, updated_at 2026-06-04. Abre com: `"Copilot Studio grounds responses in your organization's authoritative knowledge sources..."` e linka para `requirements-quotas` do Copilot Studio.

> Ressalva de escopo: a página vive na árvore `/microsoft-365/copilot/employee-self-service/`, não em `/microsoft-copilot-studio/`. Trata do agente empacotado Employee Self-Service, construído sobre Copilot Studio + SharePoint. É a **única** fonte oficial encontrada com guidance de authoring; nenhuma equivalente existe na árvore genérica do Copilot Studio (`DADO NÃO ENCONTRADO`).

### 4.1 Recomendações literais

| # | Recomendação (verbatim) | Elemento |
|---|---|---|
| 1 | `"Write scannable, topic-focused pages with clear headings to improve retrieval and summarization quality."` | separação por assunto |
| 2 | `"A concise summary helps large language models (LLMs) quickly understand the main topic, purpose, and intended audience of the article. This summary enables the LLM to ground its answers in the most relevant section, reducing the risk of pulling information from the wrong part of a long document."` | resumo inicial |
| 3 | `"When Copilot or other LLMs generate answers, they often cite the section that best matches the user's question. A summary makes it easier for the model to select the correct passage for citation."` | resumo → citação |
| 4 | `"Consistent use of header styles (H1, H2, and so on) reduces confusion for both AI and human readers."` → permite ao agente `"Identify key sections quickly / Understand relationships between topics / Retrieve the most relevant section for a user's question."` | headings hierárquicos |
| 5 | `"Without clear headers, LLMs may pull information from the wrong section or miss important context, leading to incomplete or off-topic answers."` | headings (risco) |
| 6 | `"Use lists for clarity, not dense paragraphs or tables."` · `"Lists break down complex tasks or exceptions into clear, discrete steps or points."` | listas |
| 7 | `"Use tables sparingly: LLMs prefer well-structured, contextualized text over tables... If a table is necessary, keep it simple and easy to consume. Use headings, bullet points, and consistent formatting to highlight key information instead of embedding it in tables."` | **tabelas — desaconselhadas** |
| 8 | `"Hyperlinks allow both users and LLMs to quickly access referenced resources... Ensure links are descriptive (for example, 'Download Company Portal app')."` | hiperlinks |
| 9 | `"Archive or delete duplicated or outdated content to ensure only one authoritative copy exists in the published library."` | duplicidade |
| 10 | `"The Employee Self-Service Agent can't find content in accordion webparts that use the list-based option. Use the on-page version instead."` | webpart accordion |
| 11 | `"Use content types consistently by defaulting to reusable content types (for example, policy, procedures, troubleshooting steps, FAQ) to standardize fields like owner, effective date, and region."` | content types |
| 12 | `"Prioritize search and findability by using clear, action-oriented labels in navigation, for page titles, and meta data."` | títulos/rótulos |

### 4.2 Itens do checklist do usuário sem cobertura oficial

| Elemento avaliado | Status |
|---|---|
| Título único | `FATO` indireto (H1 em §4.1 #4) |
| Resumo inicial | `FATO` (#2, #3) |
| Headings hierárquicos | `FATO` (#4, #5) |
| Seções curtas | `FATO` indireto (`"scannable, topic-focused pages"`, #1) |
| Perguntas e respostas (formato Q&A) | `DADO NÃO ENCONTRADO` — content type `FAQ` é citado (#11), mas sem guidance de formato Q&A |
| Listas numeradas | `FATO` (#6, exemplos com listas numeradas) |
| Tabelas simples | `FATO` — **desaconselhadas** (#7). Não há afirmação de que tabelas sejam preservadas/entendidas |
| Expansão de siglas | `DADO NÃO ENCONTRADO` |
| Sinônimos | `FATO` negativo (upload SP: `"There's no support for glossaries or synonyms"`) |
| Separação por assunto | `FATO` (#1) |
| Remoção de duplicados | `FATO` (#9) |
| Limite de caracteres por arquivo | `NÃO DIVULGADO OFICIALMENTE` para Copilot Studio (os 36.000 chars são de outro produto — §7) |

### 4.3 Estrutura por tipo de conteúdo

| Tipo de conteúdo | Estrutura recomendada | Formato recomendado |
|---|---|---|
| Procedimento / how-to | H1 título · resumo 2–3 frases · H2 por fase · H3 `Elegibilidade`, `Passos`, `Requisitos`, `Exceções`, `Links relacionados` · passos em lista numerada · links descritivos | DOCX |
| Política | H1 título · resumo com escopo e público · H2 por cláusula · exceções em lista, não tabela · `effective date` e `owner` como colunas SP | DOCX (PDF só se imutabilidade for exigida) |
| FAQ | H1 tema · resumo · **1 pergunta por H2**, resposta curta abaixo · sem tabela | DOCX ou página moderna SharePoint |
| Dados tabulares / matriz | **Não usar documento** | Lista SharePoint |
| Planilha analítica | evitar como knowledge source | — (`"limitations for cell indexing"`) |
| Troubleshooting | H1 sintoma · resumo · H2 por causa · passos numerados por causa | DOCX |

---

## 5. ORGANIZAÇÃO NO SHAREPOINT

### 5.1 Comparação de arquiteturas

> `DADO NÃO ENCONTRADO`: **nenhuma** doc oficial (Copilot Studio, SharePoint ou M365 Copilot) recomenda "uma biblioteca vs. várias" para knowledge base de agentes. A tabela abaixo é `INFERÊNCIA` construída sobre limites e comportamentos documentados, com as premissas nomeadas.

| Estrutura | Quando usar | Vantagens | Limitações | Evidência / premissa |
|---|---|---|---|---|
| **Uma biblioteca com pastas** | < 5.000 itens; domínio único | 1 URL de escopo; simples | Cresce até o List View Threshold; escopo grosseiro | `FATO`: LVT ~5.000 itens; `FATO`: subpastas incluídas recursivamente (`"the agent searches the URL and all subpaths"`) |
| **Bibliotecas separadas por domínio** ✅ | Múltiplos domínios (RH, TI, Jurídico); públicos distintos | Cada biblioteca vira 1 knowledge source com **descrição própria** → orquestrador escolhe a fonte certa; permissões por domínio | Consome cota de 25 URLs (orquestração generativa) | `FATO`: descrição da fonte `"aids generative orchestration"`; `FATO`: 25 URLs máx. |
| **Organização por metadados** | Faceta ortogonal (país, idioma, tipo) | Mapeamento a managed property recomendado oficialmente | Query de filtro por coluna **não suportada** no Copilot Studio | `EVIDÊNCIA CONTRADITÓRIA` — ver §3.2 |
| **Híbrida** ✅ **recomendada** | Caso geral | Biblioteca por domínio (escopo + descrição) + metadados por faceta + pastas rasas | Contradição de §3.2 permanece | `INFERÊNCIA`. Premissas: (a) biblioteca = unidade de escopo e de descrição do orquestrador `FATO`; (b) pasta = escopo, não ranking `FATO`; (c) metadado tem benefício declarado mas não filtrável por query `EVIDÊNCIA CONTRADITÓRIA` |

### 5.2 Regra de ouro derivada

| Camada | Função **comprovada** | Função **não comprovada** |
|---|---|---|
| Site / biblioteca / pasta | delimitar **o que entra** no escopo do agente | melhorar ranking |
| Descrição da knowledge source | orquestrador **escolher a fonte** | ranquear dentro da fonte |
| Coluna `Title` | filtro configurável + managed property pesquisável | — |
| Conteúdo do documento (resumo + headings + listas) | **retrieval e citação** | — |

`INFERÊNCIA` central, premissas acima: **o ganho de qualidade de resposta está majoritariamente no conteúdo do documento, não na árvore de pastas.** Pastas resolvem escopo e permissão; o texto resolve a resposta.

### 5.3 Árvore recomendada

```text
Site: kb-corporativa  (contoso.sharepoint.com/sites/kb-corporativa)
│
├── Biblioteca: politicas-rh          ← knowledge source #1
│   │  Colunas: Title (texto) · DocType (choice) · Regiao (managed metadata)
│   │           Owner (person) · EffectiveDate (date) · Status (choice)
│   │  Versionamento: maior/menor + aprovação obrigatória
│   ├── vigentes/
│   │   ├── rh-licenca-parental-br.docx
│   │   ├── rh-ferias-br.docx
│   │   └── rh-jornada-trabalho-br.docx
│   └── arquivo/                      ← EXCLUIR do escopo do agente
│       └── rh-licenca-parental-br-2024.docx
│
├── Biblioteca: procedimentos-ti      ← knowledge source #2
│   ├── vigentes/
│   │   ├── ti-acesso-vpn.docx
│   │   ├── ti-setup-notebook.docx
│   │   └── ti-reset-senha.docx
│   └── arquivo/
│
├── Biblioteca: faq-corporativo       ← knowledge source #3
│   └── vigentes/
│       ├── faq-beneficios.docx
│       └── faq-viagens.docx
│
└── Lista: matriz-aprovacao           ← knowledge source #4 (dados tabulares)
        Colunas: Processo · Valor · Aprovador · SLA
```

Regras da árvore:

| # | Regra | Base |
|---|---|---|
| 1 | 1 biblioteca = 1 domínio = 1 knowledge source com descrição própria | `INFERÊNCIA` (descrição orienta orquestrador `FATO`) |
| 2 | Registrar no agente a URL de `vigentes/`, **não** a da biblioteca — subpastas entram recursivamente, `arquivo/` ficaria dentro | `FATO`: `"The agent searches only the folder or site URL you register, and its subfolders. It never accesses parent folders, sibling folders, or other sites unless you register them separately."` |
| 3 | Profundidade ≤ 3 níveis; caminho total < 400 chars | `FATO` (400 chars) + `FATO` (10 níveis máx. no caminho B) |
| 4 | Manter < 5.000 itens por biblioteca | `FATO` (List View Threshold) |
| 5 | Dados tabulares → lista SharePoint, nunca XLSX | `FATO` (`"limitations for cell indexing"`) |
| 6 | Versionamento com aprovação: rascunhos ficam ocultos | `FATO`: `"Versioning keeps a history of changes and ensures only the latest approved version is visible... Drafts remain hidden until approved."` |
| 7 | Uma única cópia autoritativa; arquivar/excluir duplicatas | `FATO` (§4.1 #9) |
| 8 | Sem sensitivity label com criptografia, sem senha, sem DKE | `FATO`: `"the agent can't extract or ground on content whose protection encrypts the file"` |

---

## 6. LIMITES OFICIAIS

| Limite | Valor | Caminho | Fonte |
|---|---|---|---|
| URLs de site SharePoint por agente (orquestração generativa) | **25** | A | requirements-quotas |
| URLs por nó de generative answers (modo clássico) | **4** | A | requirements-quotas |
| Knowledge sources por agente (todos os tipos) | **500** | — | requirements-quotas |
| Tipos de fonte simultâneos | **5** | — | knowledge-unstructured-data |
| Arquivos / pastas / profundidade por fonte | **1.000 / 50 / 10 níveis** | B | requirements-quotas |
| Tamanho máx. por arquivo | **512 MB** | B, C | requirements-quotas |
| Tamanho máx. — SharePoint **sem** licença M365 Copilot no tenant | **7 MB** | A | [sharepoint-no-response](https://learn.microsoft.com/en-us/troubleshoot/power-platform/copilot-studio/knowledge/sharepoint-no-response) |
| Tamanho máx. — SharePoint **com** semantic search | **200 MB** / 512 MB p/ PDF-PPTX-DOCX | A | `EVIDÊNCIA CONTRADITÓRIA` — §7 |
| Listas SharePoint por agente | **10** ou **15** | A | `EVIDÊNCIA CONTRADITÓRIA` — §7 |
| Linhas retornadas por query de lista | **primeiras 2.048** | A | requirements-quotas |
| Limiar p/ citação em nível de página (PDF) | **~4 KB** | B | requirements-quotas |
| Citações por resposta (Teams) | **20**; título ~**80 chars**; snippet ~**480 chars** | — | knowledge-copilot-studio |
| Sincronização | **4–6 h**, sem refresh manual | B | knowledge-unstructured-data |
| Itens por biblioteca (SharePoint) | até **30 milhões**; View Threshold **~5.000** | — | [list-view-threshold](https://support.microsoft.com/en-us/office/list-view-threshold-for-large-lists-and-libraries-e2ea4d5d-ec23-4171-95c4-c7f5b5dbfd8a) |
| Caracteres/páginas máximos por documento | `NÃO DIVULGADO OFICIALMENTE` (para Copilot Studio) | — | — |

---

## 7. CONTRADIÇÕES E FALSOS POSITIVOS

### 7.1 Contradições reais entre fontes oficiais

| # | Divergência | Fonte A | Fonte B |
|---|---|---|---|
| 1 | Itens selecionáveis em Upload files > SharePoint: **5** vs **15** | knowledge-add-unstructured-data (`"up to five individual files, folders, or combinations"`) | requirements-quotas (`"up to 15 files or folders at once"`) |
| 2 | Listas SharePoint por agente: **10** vs **15** | knowledge-add-sharepoint (`"up to 10 lists at a time... only use up to 10 lists per agent"`) | requirements-quotas (`"up to 15 total lists... select up to 15 lists during each session"`) |
| 3 | Tamanho máx. SharePoint: **200 MB** vs **512 MB**, na **mesma seção** | knowledge-copilot-studio, §Tenant graph grounding | knowledge-copilot-studio, nota imediatamente abaixo |
| 4 | Metadados customizados: recomendados vs não suportados | optimization-sharepoint | requirements-quotas |

### 7.2 `NÃO SE APLICA` — fonte oficial, produto errado

⚠️ Guidance amplamente citada que **não vale para Copilot Studio**:

| Afirmação | Produto real | Veredito |
|---|---|---|
| `"keep your SharePoint files to a maximum of 36,000 characters (approximately 15-20 pages)"` | M365 Copilot **declarative agents / Agent Builder** | `NÃO SE APLICA` a Copilot Studio |
| `"limit the total page count... to no more than 300 pages"` | idem | `NÃO SE APLICA` |
| `"Copilot indexes the first 750-1,000 pages (1.8 million characters) of each embedded file"` | idem | `NÃO SE APLICA` |
| `"Copilot is currently unable to parse tables and other special formatting"` | idem | `NÃO SE APLICA` |
| `"store all the data in one sheet within a workbook"` (Excel) | idem | `NÃO SE APLICA` |
| Limites 100 arquivos / 1 lista / 20.000 itens | idem | `NÃO SE APLICA` |

Base do veredito (subagente `validation`): a página [optimize-content-retrieval](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/optimize-content-retrieval) tem breadcrumb `/microsoft-365/copilot/extensibility/`, abre com `"Declarative agents extend Microsoft 365 Copilot..."` e todos os limites referenciam o `agent manifest` / objeto `OneDriveAndSharePoint` — construtos inexistentes no Copilot Studio.

### 7.3 Evidência enfraquecida

| Item | Ressalva |
|---|---|
| Tabela de managed properties (`Title` Searchable=Yes, `Description`/`Keywords` Searchable=No) | Documentação de **SharePoint Server** on-prem, `ms.date 2017-09-08`. Não há tabela equivalente rotulada SharePoint Online. Extrapolação para SPO: `NÃO CONFIRMADO` |
| `"o nome do arquivo é o título da citação"` | `NÃO CONFIRMADO`. A doc só diz que o nome do arquivo vira o **nome da knowledge source** por padrão |
| Efeito de minor version/draft na indexação | Única fonte encontrada é blog arquivado (2013-era). `EVIDÊNCIA NÃO OFICIAL` |
| Guidance de authoring (§4 inteira) | Vive na árvore Employee Self-Service, não na genérica do Copilot Studio. Cita Copilot Studio explicitamente |

---

## 8. LACUNAS — `DADO NÃO ENCONTRADO` após busca ativa de refutação

1. Página oficial de guidance de authoring na árvore `/microsoft-copilot-studio/` (só existe a de Employee Self-Service).
2. Afirmação de que tabelas em documentos são preservadas/entendidas — só existe a orientação inversa (`"Use tables sparingly"`).
3. Confirmação de OCR — o suporte a imagem documentado é via **alt-text em PDF**, não OCR.
4. Limite de caracteres/páginas por documento para Copilot Studio.
5. Guidance sobre expansão de siglas.
6. Guidance sobre formato Q&A explícito.
7. Quais sinais o `Tenant graph grounding with semantic search` pondera (Title, filename, conteúdo).
8. Ponte oficial entre managed properties do SharePoint Search e o ranking interno do Copilot Studio.
9. Recomendação oficial de arquitetura de bibliotecas para knowledge base de agentes.
10. Página canônica única de "Known issues / Limitations" de knowledge sources.

---

## 9. VERIFICAÇÃO

| Passo | Como |
|---|---|
| 1. Formato aceito | Subir 1 DOCX na biblioteca e adicionar a URL como knowledge source; confirmar status `Ready` |
| 2. Indexação | Buscar o arquivo pela busca do próprio portal SharePoint — `FATO`: `"If the file doesn't appear, indexing is incomplete"` |
| 3. Estrutura interna | Perguntar ao agente algo respondido por **um H3 específico**; verificar se a citação aponta para a seção certa |
| 4. Escopo de pasta | Colocar documento em `arquivo/` e confirmar que o agente **não** o cita quando o escopo registrado é `vigentes/` |
| 5. Descrição da fonte | Com 2+ knowledge sources, perguntar algo de domínio A e confirmar que o orquestrador escolheu a fonte A |
| 6. Coluna Title | Configurar filtro por `Title` em Advanced settings e validar inclusão/exclusão |
| 7. Contradição §3.2 | Mapear 1 coluna a managed property, perguntar em linguagem natural (não query de filtro) e medir se a relevância melhora — **teste empírico obrigatório**, a doc não decide |
| 8. Limite de listas | Testar 10 vs 15 listas no tenant real — contradição #2 não é resolvível por doc |
