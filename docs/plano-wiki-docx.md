# Wiki dupla: Markdown (as-is) + DOCX (Copilot Studio / SharePoint)

Status: **proposto** — não implementado. Documento para análise.

## Histórico de revisão

Revisão adversarial em 3 lentes (OOXML, integração com o resto do repo,
adequação ao propósito de retrieval) encontrou **20 defeitos confirmados**,
dos quais **4 críticos**. Esta versão corrige os 20. Nenhuma seção anterior
foi mantida sem reverificação contra o código-fonte e contra
[references/copilot-studio-sharepoint-kb.md](../references/copilot-studio-sharepoint-kb.md).

## Contexto

O wiki-ai gera exclusivamente Markdown — `_publish_candidates` filtra por
`.md` ([scripts/wk/cli.py:1746](../scripts/wk/cli.py#L1746)) e `wk compile`
escreve `wiki/<topic>/<source_type>/<id>.md`
([scripts/wk/cli.py:1006](../scripts/wk/cli.py#L1006)).

Pesquisa em
[references/copilot-studio-sharepoint-kb.md](../references/copilot-studio-sharepoint-kb.md)
estabeleceu (§2.1, `FATO`): **`.md` não é formato suportado como knowledge
source do Copilot Studio via SharePoint** — nem pelo conector (caminho A),
nem por `Upload files > SharePoint` (caminho B). Só `DOC/DOCX`, `PPT/PPTX`,
`PDF` nesses dois caminhos. `.md` só é aceito no upload direto de arquivo ao
agente (caminho C, Dataverse) — fora do escopo deste plano, que assume
conteúdo residente no SharePoint (ver Decisão/A6 abaixo). A saída atual não é
ingerível pelos caminhos A/B.

Resultado pretendido: duas árvores de saída paralelas a partir da mesma
fonte-verdade (`raw/`) — `wiki/` em Markdown, inalterada, e `wiki-docx/` em
DOCX reestruturado conforme a guidance oficial de authoring (KB §4).

**Restrição dura:** zero dependências externas. Distribuição é um `wk.pyz`
único — `"Sem unzip, sem PYTHONPATH, sem pip. Requer só python3 no PATH."`
([INSTALL.md:21-23](../INSTALL.md#L21)). `python-docx` e `pandoc` estão fora.
DOCX é ZIP + XML: `zipfile` + string-building, mesmo estilo de
[coupling_java_html.py](../scripts/codescan/coupling_java_html.py).

## Decisões

| # | Decisão | Razão |
|---|---|---|
| 1 | Árvore paralela `store/wiki-docx/<topic>/<source_type>/<arquivo>.docx` | Sincroniza para o SharePoint sem arrastar `.md`, que os caminhos A/B não aceitam (KB §2.1) |
| 2 | Novo subcomando `wk docx`; **não** alterar `cmd_compile` nem `wiki/` | Desacopla; `wk docx` roda independente de `wk compile` |
| 3 | Lê de `raw/`, não de `wiki/` | `raw/` tem corpo limpo; `wiki/` já traz o wrapper `# {id}` + bloco de metadados injetado em [cli.py:1007-1022](../scripts/wk/cli.py#L1007) que teria de ser re-parseado e descartado |
| 4 | Conteúdo reestruturado para retrieval | H1 = título humano, resumo no início, proveniência no rodapé, front matter → `docProps/core.xml` — alinhado à guidance literal da KB §4.1 (#2–#4) |
| 5 | Nomenclatura SharePoint preservando a taxonomia do artefato | `<topic>-<artefato>-<escopo>.docx` |
| 6 | Mermaid: distinguir diagrama de **dependência** de diagrama de **agrupamento** antes de traduzir; senão bloco de código com legenda | Ver §2 "Mermaid" — traduzir toda aresta como "depende de" produz afirmação falsa quando a aresta é de pertencimento (subgraph) |
| 7 | **Não** gerar `index.docx` nesta v1 — decisão revisável | Ver "Decisão 7" abaixo; a premissa da versão anterior sobre o agente não seguir links não tem base na KB |

### Decisão 7 — detalhada (substitui a versão anterior, que citava uma premissa inexistente)

A versão anterior deste plano justificava não gerar `index.docx` afirmando
que o agente do Copilot Studio não segue/navega hyperlinks. **Essa afirmação
não existe na KB e foi removida.** A única menção da KB a hyperlinks
([copilot-studio-sharepoint-kb.md:144](../references/copilot-studio-sharepoint-kb.md#L144))
diz o contrário: `"Hyperlinks allow both users and LLMs to quickly access
referenced resources... Ensure links are descriptive"` — ou seja, a KB
recomenda links, não os desqualifica.

Razão honesta, revisada:

| Pergunta | Resposta |
|---|---|
| Um índice ajuda o retrieval do Copilot Studio? | `DADO NÃO ENCONTRADO` — a KB não trata de páginas-índice como estratégia de retrieval |
| Por que não gerar mesmo assim? | Consumo de cota (cada `.docx` é 1 documento contra os limites da biblioteca, KB §6) e ausência de conteúdo próprio — um índice é só uma lista de links, o que a KB recomenda evitar como *conteúdo principal* (`"Use tables sparingly"`, `"Write scannable, topic-focused pages"`, §4.1 #1) |
| É definitivo? | **Não.** Marcado explicitamente como decisão revisável — se o teste de verificação (§6, E2E) mostrar que um índice melhora a navegação humana no SharePoint (não o retrieval do agente), reavaliar |

---

## Arquivos

| Arquivo | Ação |
|---|---|
| `scripts/wk/docxgen.py` | **Novo.** Funções puras: escritor OOXML, conversor MD→OOXML, heurísticas, `_docx_filename`. Sem argparse |
| `scripts/wk/tests/test_docx.py` | **Novo.** `unittest`, padrão de [test_corpus.py](../scripts/wk/tests/test_corpus.py) |
| `scripts/wk/cli.py` | `cmd_docx(a)` + registro do subparser após `compile` ([cli.py:2001-2004](../scripts/wk/cli.py#L2001)) |
| `operations/docx.md` | **Novo.** Doc da operação, padrão de [operations/compile.md](../operations/compile.md) |
| `SKILL.md` | Nova linha na tabela de operações (`docx` → gera `wiki-docx/` a partir de `raw/`) |
| `README.md`, `INSTALL.md`, `schema.md` | Documentar a segunda árvore e o novo comando |

`scripts/build_pyz.py` empacota `wk/` inteiro via `PACKAGES`
([build_pyz.py:20](../scripts/build_pyz.py#L20)) — o módulo `docxgen.py`
entra no `.pyz` sem registro extra. **Mas o comando só fica visível a
`wk docs`/`wk check` se `operations/docx.md` for adicionado a `DOCS`**
([build_pyz.py:25-46](../scripts/build_pyz.py#L25)) — ver A7. Note que
`README.md` **não** está em `DOCS`: editá-lo não afeta o executável
empacotado. Editar `schema.md`/`INSTALL.md` está em `DOCS` (`"schema"`,
`"install"`) e exige rebuild do `.pyz` + `wk init --force` para propagar.

**Reutilizar** (não reimplementar): `_slug`
([cli.py:945](../scripts/wk/cli.py#L945)) · `_promoted_raw_sources`
([cli.py:952](../scripts/wk/cli.py#L952)) · `_append_log`
([cli.py:754](../scripts/wk/cli.py#L754)) · `sbindex.frontmatter.split`.

**Rejeitado:** reusar `_unique_dest` ([cli.py:742](../scripts/wk/cli.py#L742))
para resolver colisão de nome — ver C1 abaixo.

---

## 1. Escritor OOXML

Partes do pacote, escritas nesta ordem fixa:

| Caminho no zip | Obrigatória | Fixa/Gerada |
|---|---|---|
| `[Content_Types].xml` | sim | gerada (overrides variam com numbering) |
| `_rels/.rels` | sim | fixa |
| `docProps/core.xml` | requisito funcional (decisão 4) | gerada |
| `docProps/app.xml` | opcional | fixa |
| `word/document.xml` | sim | gerada |
| `word/_rels/document.xml.rels` | sim | gerada — relações para `styles.xml` e `numbering.xml` (obrigatórias, ver A2) + uma por hyperlink externo |
| `word/styles.xml` | de facto | fixa |
| `word/numbering.xml` | **sim se houver lista** | fixa (2 `abstractNum` × 9 níveis + 2 `num`, ver C2) |

Omitir `theme1.xml`, `fontTable.xml`, `settings.xml`, `webSettings.xml` — o
Word sintetiza defaults.

`styles.xml` mínimo: `Normal` (`w:default="1"`), `Heading1`–`Heading4`,
`ListParagraph`, `CodeBlock` (`w:type="paragraph"`), `CodeChar`
(`w:type="character"` — **explícito**, nunca omitido; ver M1). Cada Heading
com `w:styleId` sem espaço, `w:name` com espaço (`heading 1`), `w:qFormat` e
`w:outlineLvl` — os três consistentes, já que não está confirmado qual campo
o parser da Microsoft usa (ver §7 Riscos).

### A1 — `<w:tbl>`: ordem de filhos e `<w:tc>` nunca vazia

`CT_Tbl` exige, nesta ordem: `<w:tblPr>` (obrigatório) → `<w:tblGrid>`
(obrigatório, um `<w:gridCol w:w="...">` por coluna) → sequência de `<w:tr>`.
O plano anterior omitia `tblGrid` inteiramente — corrigido. Toda `<w:tc>`
precisa de ao menos um `<w:p/>` (`CT_Tc`, `EG_BlockLevelElts minOccurs="1"`);
célula vazia (ex. coluna opcional sem valor) escreve `<w:p/>` vazio, nunca
omite o parágrafo.

```xml
<w:tbl>
  <w:tblPr>
    <w:tblStyle w:val="TableGrid"/>
    <w:tblW w:w="0" w:type="auto"/>
  </w:tblPr>
  <w:tblGrid>
    <w:gridCol w:w="2400"/>
    <w:gridCol w:w="2400"/>
    <w:gridCol w:w="2400"/>
  </w:tblGrid>
  <w:tr>
    <w:tc><w:p><w:r><w:t>Coluna A</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>Coluna B</w:t></w:r></w:p></w:tc>
    <w:tc><w:p/></w:tc>
  </w:tr>
</w:tbl>
```

### A2 — relações obrigatórias em `document.xml.rels`

`word/_rels/document.xml.rels` precisa de `Relationship` para `styles.xml`
(`Type=".../relationships/styles"`) e para `numbering.xml`
(`.../relationships/numbering"`) sempre que essas partes existirem — não só
para hyperlinks externos. OPC (Open Packaging Conventions) resolve partes
**por relação**, não por presença física no zip; uma parte presente no zip e
listada em `[Content_Types].xml` mas sem `Relationship` de `document.xml`
para ela não é carregada pelo Word.

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>
```

### A3 — namespaces obrigatórios em `<w:document>`

```xml
<w:document
  xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
```

`xmlns:w` é o namespace de todo o markup do corpo. `xmlns:r` é exigido pelo
`r:id` de hyperlink (`<w:hyperlink r:id="rId3">`). Para o escopo deste plano
(sem imagens/DrawingML v1) são os únicos dois necessários.

### A-extra — blocos XML mínimos

`[Content_Types].xml` — content types exatos (`.main` no tipo do
`document.xml` é literal do schema, não erro de digitação):

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
```

`_rels/.rels`:

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
```

`word/document.xml` — esqueleto:

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
  xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <!-- parágrafos, tabelas, listas -->
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1417" w:right="1133" w:bottom="1417" w:left="1133"/>
    </w:sectPr>
  </w:body>
</w:document>
```

`<w:sectPr>` é `minOccurs="0"` no schema (`CT_Body`) — opcional. Decisão
explícita: **incluir sempre**, no fim do `<w:body>`. Custo baixo, elimina uma
variável não testável neste ambiente (comportamento do Word sem `sectPr` não
pode ser verificado sem Word instalado — ver §7).

### C2 — modelo de `numbering.xml` (correção do plano anterior)

O plano anterior descrevia "`num` por nível de aninhamento" — **modelo
incorreto**. O modelo real do OOXML (`wml.xsd`, `CT_AbstractNum`,
`CT_Num`):

- Um `<w:abstractNum w:abstractNumId="N">` define **todos os 9 níveis**
  possíveis de uma lista, cada um como `<w:lvl w:ilvl="0">` a
  `<w:lvl w:ilvl="8">` (`CT_AbstractNum`, `lvl maxOccurs="9"`).
- `<w:num w:numId="N">` é uma **instância de lista**: aponta para um
  `abstractNumId` via `<w:abstractNumId w:val="N"/>`. Não existe um `num` por
  nível.
- O nível de aninhamento no documento é resolvido **em runtime**, dentro do
  parágrafo: `<w:numPr><w:ilvl w:val="K"/><w:numId w:val="N"/></w:numPr>`.
  Trocar `w:ilvl` muda a indentação visual sem trocar `numId`.

Para este plano: **2 `abstractNum`** (bullet, ordenada) × 9 níveis cada +
**2 `num`** (um por `abstractNum`), reaproveitados por todos os documentos
gerados.

```xml
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>
    <!-- w:ilvl="1" .. "8", mesmo padrão, indent crescente -->
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="1">
    <w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>
    <!-- w:ilvl="1" .. "8" -->
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>
```

### Determinismo e encoding

| Armadilha | Mitigação |
|---|---|
| `ZipInfo.date_time` = hora do sistema | fixar `(1980,1,1,0,0,0)` |
| ordem de entradas | ordem explícita acima |
| `create_system`/`external_attr` variam por SO | fixar `create_system=0`, `external_attr` explícito |
| escape XML | `_esc_text`/`_esc_attr` dedicados; não usar `html.escape` |
| espaço perdido ao quebrar runs | `xml:space="preserve"` em **todo** `<w:t>` |
| declaração XML | `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` em cada parte |
| M3 — ordem de filhos `xsd:sequence` estrita | `CT_PPrBase`: `pStyle` → … → `numPr` → `spacing`/`ind` → `jc` → `outlineLvl`. `CT_Style`: `name` → `basedOn` → … → `qFormat` → … → `pPr` → `rPr`. `CT_TblPrBase`: ordem própria de `w:tblStyle`/`w:tblW`/etc. `w:rPr` é `xsd:choice` (ordem livre por schema), mas por convenção `rStyle` vem primeiro |
| M2 — `xsi:type` nas datas de `core.xml` | `dcterms:created`/`dcterms:modified` exigem `xsi:type="dcterms:W3CDTF"`; declarar `xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"` em `core.xml` |

---

## 2. Conversor Markdown → OOXML

| Construto | Mapeamento |
|---|---|
| `#`–`####` | `w:pStyle` = `Heading1`–`Heading4`; H5+ → `Heading4` |
| parágrafo | `w:pStyle="Normal"` |
| `- ` / `1. ` | `ListParagraph` + `w:numPr`, `numId=1` bullet / `numId=2` ordenada, aninhamento por `w:ilvl` (mesmo `numId`, ver C2) |
| `**bold**` / `*italic*` | `<w:b/>` / `<w:i/>` |
| `` `code` `` | `w:rStyle="CodeChar"` |
| ` ```bloco``` ` | 1 `<w:p>` por linha, `CodeBlock`, sem parsing interno |
| `> citação` | `Normal` + `w:ind w:left="720"` + itálico |
| `[texto](url)` | hyperlink real só se `http(s)://` ou `mailto:`; caminho relativo → texto `"texto (url)"` |
| `[[wikilink]]` | texto em negrito, colchetes removidos (não há resolvedor no projeto) |
| `---` | parágrafo vazio |
| `![alt](src)` | `"[Imagem: alt] (src)"`, hyperlink se `http(s)` — DrawingML fora do escopo v1 |
| outros (`~~strike~~`, task list, HTML inline, math) | markdown bruto preservado + entrada em `warnings` |

### A5 — Tabelas: regra em dois eixos (substitui a regra anterior)

Medição real de densidade no corpus, citada em vez de estimada:
[export.py:119-134](../scripts/codescan/export.py#L119) (tabela de módulos)
= **7 colunas**; [coupling_java_md.py:339-360](../scripts/codescan/coupling_java_md.py#L339)
(tabela de métricas por classe) = **11 colunas × até 200 linhas**
(`max_class_rows = 200`, [coupling_java_md.py:350](../scripts/codescan/coupling_java_md.py#L350)).

A regra anterior ("2 colunas → lista, ≥3 → tabela") convertia só as tabelas
pequenas e inofensivas; uma tabela de 11×200 vira `<w:tbl>` de 200 linhas —
exatamente o padrão contra o qual a KB adverte (`"Use tables sparingly:
LLMs prefer well-structured, contextualized text over tables"`,
[copilot-studio-sharepoint-kb.md:143](../references/copilot-studio-sharepoint-kb.md#L143))
e o formato mais exposto a M5 (chunk de linha órfã sem cabeçalho).

Regra corrigida, dois eixos — nº de colunas **e** nº de linhas:

| Colunas | Linhas | Regra |
|---|---|---|
| ≤ 2 | qualquer | lista `- **chave**: valor` |
| ≥ 3 | ≤ 10 (corte definido; ajustável) | `<w:tbl>` simples: sem merge, sem aninhamento, header em negrito |
| ≥ 3 | > 10 | **um parágrafo/item de lista por registro**, formato `"<coluna1>: <col2>=<v2>, <col3>=<v3>..."` — cada registro carrega o próprio cabeçalho, sobrevive ao chunking sem depender de uma linha de `<w:tbl>` anterior |

Exemplo da regra (c) aplicada à tabela de métricas por classe:

```
- AuthService (com.x.auth): Ca=3, Ce=7, WMC soma=42, WMC max=12, Classe-deus=não
```

### C3 / Decisão 6 — Mermaid (correção do plano anterior)

`coupling_generic.py:406-412` e o equivalente em `coupling_java_md.py` (linhas
~273-279) emitem, dentro de `subgraph zone_<zona>[...]`, arestas
`Z<ZONA> --> P001`. O significado dessa aresta é **"P001 pertence à zona
Z<ZONA>"** (pertencimento/agrupamento), não "Z<ZONA> depende de P001". A regra
anterior deste plano ("traduzir toda aresta `A --> B` como `A depende de B`")
corromperia o sentido em qualquer flowchart com `subgraph`, produzindo texto
como `"Dor depende de módulo X"` a partir de um diagrama que só agrupa `X` na
zona `Dor`.

Regra corrigida — detectar o tipo de diagrama **antes** de escolher o
template:

1. Parsear o corpo do bloco `flowchart`/`graph`. Se contiver `subgraph` →
   diagrama de **agrupamento**: cada aresta `<hub> --> <nó>` dentro de um
   `subgraph X[...]` vira `"<nó> está em <X>"`. Nunca usar o verbo "depende
   de" nesse caso.
2. Se **não** contiver `subgraph` → diagrama de **dependência**: aplica-se o
   template anterior — parsear declarações `ID["label"]` para montar o mapa
   de rótulos, traduzir cada aresta `A --> B` como `"<label A> depende de
   <label B>"`. Aresta com rótulo (`A -->|"texto"| B`) → `"<label A> <texto>
   <label B>"`.
3. Se o tipo de relação não for determinável por essas duas regras (ex.
   mistura de subgraph e arestas fora dele, ou sintaxe não reconhecida) →
   **degradar para bloco de código** com legenda, nunca inventar o verbo.
4. Qualquer tipo que não seja `flowchart`/`graph` → bloco `CodeBlock` com
   legenda `"Diagrama <tipo> (Mermaid — não renderizado)"` + `warnings`.

```
flowchart LR
  M001["api"] --> M002["domain"]
```
→ (sem subgraph, regra 2)
```
Dependências entre módulos:
• api depende de domain
```

```
flowchart LR
  subgraph zone_dor["Dor"]
    ZDOR["Dor"]
    ZDOR --> P001["módulo x"]
  end
```
→ (com subgraph, regra 1)
```
Agrupamento por zona:
• módulo x está em Dor
```

### M6 — cobertura de tipos Mermaid: fração real

O contrato SDD torna **obrigatórios** `erDiagram`
([sdd-contract.md:576](../references/sdd-contract.md#L576)) e
`stateDiagram-v2` ([sdd-contract.md:581](../references/sdd-contract.md#L581));
`sdd.py` também emite `quadrantChart`
([codescan/sdd.py:1037](../scripts/codescan/sdd.py#L1037)). Não são casos de
borda — são tipos de primeira classe do corpus SDD. A regra anterior
("qualquer tipo que não seja flowchart/graph → bloco de código") os trata
todos como degradação total, o que é aceitável para `quadrantChart`
(estrutura pouco tabular) mas descarta estrutura recuperável de `erDiagram`.

Tradução mínima adicional para `erDiagram`: parsear linhas
`ENTIDADE1 ||--o{ ENTIDADE2 : "rótulo"` e emitir lista
`"<ENTIDADE1> <rótulo> <ENTIDADE2> (cardinalidade <op>)"`. `stateDiagram-v2` e
`quadrantChart` permanecem em `CodeBlock` + `warnings` nesta v1 —
`NÃO VERIFICADO` se a tradução de `erDiagram` cobre 100% dos padrões emitidos
por `sdd.py`; validar contra os fixtures de `test_coupling.py` antes de
fechar o parser.

**Degradação:** cada bloco converte em `try/except` isolado. Falha →
parágrafo `Normal` com o markdown bruto + `warnings`. Nunca perder conteúdo em
silêncio.

---

## 3. Estrutura do documento

Corpo, nesta ordem:

1. **H1** — título humano
2. **Parágrafo de resumo** — sem heading próprio
3. **Corpo** — conversão integral de `source["body"]`
4. **H2 `Proveniência`** — lista com `topic`, `source_type`, `confidence`,
   `origin`, `captured_at`, `fonte` (mesmos campos hoje no topo do wrapper em
   [cli.py:1010-1014](../scripts/wk/cli.py#L1010), reposicionados)

`docProps/core.xml`:

| Campo | Origem |
|---|---|
| `dc:title` | H1 derivado |
| `dc:subject` | `topic` (texto, não slug) |
| `dc:creator` | `origin` |
| `cp:category` | `source_type` |
| `cp:keywords` | `topic, source_type, artefato` |
| `dc:description` | resumo |
| `dcterms:created`/`dcterms:modified` | `captured_at` → W3CDTF, com `xsi:type="dcterms:W3CDTF"` (M2); **omitir o par** se ausente ou inválido |

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties
  xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:dcterms="http://purl.org/dc/terms/"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Coupling — wiki-ai</dc:title>
  <dcterms:created xsi:type="dcterms:W3CDTF">2026-08-06T00:00:00Z</dcterms:created>
</cp:coreProperties>
```

**Título (H1)** — cascata:
1. primeiro `^#{1,2}\s+(.+)$` do corpo (H1 preferido; H2 no topo é promovido)
2. do id: `sb-codescan-wiki-ai-coupling` → `"Coupling — wiki-ai"`
3. humaniza o id: remove prefixo `sb-<gerador>-`, `-`/`_` → espaço,
   title-case

Nunca fabrica texto além de formatar o que já existe.

### A4 — Resumo: rejeitar boilerplate (correção do plano anterior)

O plano anterior citava `inventory.md` como exemplo de fonte "sem parágrafo,
abre com tabela". **Falso**:
[export.py:154-157](../scripts/codescan/export.py#L154) sempre emite um
parágrafo antes de qualquer tabela. O problema real é pior que "sem
parágrafo": esse parágrafo —

> `"Gerado deterministicamente pelo codescan export a partir do
> surface.json. Toda afirmação aqui é 🟢 por construção: contagem e
> metadado, não interpretação."`

— é **idêntico byte-a-byte em todo `inventory.md` de qualquer repositório**
processado pelo codescan (é texto fixo em `render_inventory`, sem
interpolação de dados do repo). O mesmo vale para `dependencies.md`
([export.py:173-177](../scripts/codescan/export.py#L173)), com outro texto
igualmente fixo. Uma cascata de resumo que aceita "primeiro parágrafo de
texto corrido" sem checar o conteúdo captura esse boilerplate como se fosse
resumo — sucesso aparente, dano real: N documentos de repos distintos com
resumo idêntico, competindo entre si na busca semântica do Copilot Studio
sem nenhum poder discriminante.

**Resumo — cascata corrigida:**
1. primeiro parágrafo de texto corrido (não heading/lista/tabela/fence)
2. **rejeitar** se o parágrafo bate com a lista de aberturas boilerplate
   conhecidas dos geradores determinísticos do codescan (`render_inventory`,
   `render_dependencies`, e equivalentes — lista mantida em `docxgen.py`,
   comparação normalizada por prefixo) — critério documentado no código como
   "boilerplate conhecido", não heurística de estilo
3. **rejeitar** também se o parágrafo, após remover stopwords, não contiver
   nenhum termo específico do documento (nome do repo/módulo/id) — segunda
   defesa para boilerplate futuro não cadastrado na lista
4. parágrafo aprovado em (1)–(3) → usar verbatim, truncado em ~600 chars na
   fronteira de frase
5. nenhum parágrafo aprovado (ex. corpo abre direto em tabela, ou só
   boilerplate disponível) → placeholder factual de metadados:
   `"Artefato {source_type} do tópico {topic}, gerado a partir de {origin}."`
   — documentado no código como placeholder, **não** resumo de conteúdo

---

## 4. Nomenclatura

`_docx_filename(source) -> str`:

1. `domain = _slug(topic)`
2. extrai `subject`/`scope` do id, na ordem:
   - `sb-codescan-(?P<repo>.+)-(?P<artifact>inventory|dependencies|coupling)`
     → `subject=artifact`, `scope=repo`
   - `sb-publish-(?P<repo>.+?)-(?P<rest>(modules|sdd)-.+)` →
     `subject=rest`, `scope=repo`
   - nenhum casa → remove `^sb-[a-z]+-`, resto vira `subject`, sem `scope`
3. `stem` = `{domain}-{subject}-{scope}` ou `{domain}-{subject}`
4. sanitiza: minúsculas; remove `" * : < > ? / \ |`; colapsa `-`;
   `strip("- ")`; nome reservado (`CON`, `PRN`, `AUX`, `NUL`, `COM0-9`,
   `LPT0-9`, `_vti_`) → sufixo `-doc`; remove prefixo `~$`
5. trunca em ~120 chars preservando o final (artefato/escopo discrimina mais
   que o meio)
6. colisão de `stem` (dois ids distintos mapeando para o mesmo nome) → ver
   C1

### C1 — resolução de colisão e escrita (correção do plano anterior)

O plano anterior mandava reusar `_unique_dest`
([cli.py:742](../scripts/wk/cli.py#L742)) para resolver tanto "regenerar o
mesmo documento" quanto "colisão real de nome". **`_unique_dest` é a função
errada para este caso**: ela só verifica `os.path.exists` e, se o caminho já
existir, anexa `-2`, `-3`, ... sem nunca sobrescrever
([cli.py:742-751](../scripts/wk/cli.py#L742)). Como `wiki-docx/` é uma árvore
regenerada a cada execução de `wk docx`, a segunda execução encontraria o
`.docx` da primeira e produziria `<stem>-2.docx`; a terceira, `<stem>-3.docx`;
sem limite, sem nunca convergir. `_unique_dest` é adequada para
`ingest`/`promote` (onde cada chamada é um item novo e distinto que não deve
sobrescrever o anterior — cli.py:898,1666,1720,1849), não para uma
regeração idempotente.

**Rejeitado**: `_unique_dest` para nomear `.docx`.

**Adotado**: escrita idempotente em path determinístico, **sobrescrevendo** —
mesmo comportamento de `cmd_compile`, que escreve
`wiki/<topic>/<source_type>/<id>.md` sempre no mesmo caminho e sobrescreve a
cada `wk compile` ([cli.py:1023](../scripts/wk/cli.py#L1023), via
`_write_md`, sem verificação de existência prévia). `wk docx` segue o mesmo
padrão: o path é função pura de `(topic, source_type, id)`, e cada execução
escreve por cima do arquivo anterior.

Colisão real de `stem` (dois `id` **distintos** mapeando para o mesmo
`stem` sanitizado) não é resolvida por contador dependente do estado do
disco (não determinístico entre execuções, ordem de iteração importa) — é
resolvida por **sufixo determinístico derivado do id**: 8 primeiros
caracteres de `sha1(id)` (hex), anexados ao stem antes da extensão. O mesmo
`id` sempre produz o mesmo sufixo; a colisão nunca depende de quantos
arquivos já existem no disco nem da ordem de processamento.

| id | topic | Resultado |
|---|---|---|
| `sb-codescan-wiki-ai-coupling` | `arquitetura` | `arquitetura-coupling-wiki-ai.docx` |
| `sb-codescan-wiki-ai-inventory` | `arquitetura` | `arquitetura-inventory-wiki-ai.docx` |
| `sb-publish-wiki-ai-modules-auth` | `arquitetura` | `arquitetura-modules-auth-wiki-ai.docx` |
| `sb-ingest-reuniao-kickoff-3f9a1c02` | `onboarding` | `onboarding-ingest-reuniao-kickoff-3f9a1c02.docx` |
| dois ids distintos → mesmo `stem` sanitizado | — | `<stem>-<8 chars de sha1(id)>.docx` para o segundo (e qualquer subsequente) |

O limite de 400 chars é do caminho publicado no SharePoint, fora do controle
do comando — garantir nome curto deixa folga.

---

## 5. CLI

```
wk docx [<topico>] [--store <path>] [--out-dir wiki-docx] [--no-prune]
```

Mesmo padrão de `wk compile` ([cli.py:2001-2004](../scripts/wk/cli.py#L2001)):
posicional opcional de topic, `set_defaults(fn=cmd_docx)`, import tardio
`from wk import docxgen`.

Saída JSON:
```json
{
  "documentos": [{"path": "wiki-docx/...", "id": "...", "source_type": "...", "avisos": []}],
  "fontes": 12,
  "removidos": [{"path": "wiki-docx/...", "motivo": "orfao"}],
  "pulados": [{"id": "...", "motivo": "..."}]
}
```
Exit `1` só se algum `.docx` falhar ao ser escrito (listado em `pulados`).

### A6 — caminho de ingestão

Este plano assume explicitamente o **caminho A** da KB (§1) — conector
SharePoint sobre biblioteca de documentos, `Add knowledge → SharePoint`.
Consequências decorrentes, citando a KB:

| Consequência | Fonte |
|---|---|
| Citação só em nível de **documento**, não de página | KB §1, tabela "Diferença crítica": citação nível de página é exclusiva do caminho B |
| Limite de tamanho **7 MB** por arquivo se o tenant **não** tiver licença M365 Copilot | KB §6, `sharepoint-no-response` |
| Limite de tamanho **200 MB** (ou 512 MB p/ DOCX/PPTX/PDF) com semantic search habilitado | KB §6 — `EVIDÊNCIA CONTRADITÓRIA` entre duas seções da mesma página, marcado como tal na própria KB (§7.1 #3) |
| Máx. **25 URLs de site** por agente (orquestração generativa) | KB §6 |
| Bibliotecas de documentos **suportadas** no caminho A | KB §1 — diferente do caminho B, onde `"Currently, document libraries are not supported"` **invalidaria** o caminho B para esta árvore, caso fosse escolhido no lugar de A |

### A9 — rotas alternativas avaliadas

A KB recomenda literalmente Lista SharePoint para dados tabulares (§4.3:
`"Dados tabulares / matriz | Não usar documento | Lista SharePoint"`).
Comparação das rotas disponíveis para este corpus:

| Rota | Recomendação | Justificativa do descarte (quando aplicável) |
|---|---|---|
| **DOCX** (adotada) | usar para conteúdo narrativo (specs, transcritos, análises, inventário/dependências/coupling em prosa) | — |
| **Lista SharePoint** | **trabalho futuro** para a fatia tabular do codescan (ex. tabela de métricas por classe, A5) | Fora do escopo v1: exige API de criação de lista + colunas tipadas, não é gerável como arquivo estático pelo `wk.pyz` offline |
| **PDF** | descartado | Geração from-scratch (sem lib) tem custo desproporcional ao ganho — KB não mostra vantagem de citação sobre DOCX no caminho A (citação nível-página do PDF é exclusiva do caminho B, não usado aqui); DOCX já é editável in-place no SharePoint, PDF exigiria regeração completa a cada revisão |
| **Página moderna SharePoint (`.aspx`)** | descartado | Exige Graph API ou SharePoint REST API **ao vivo** para criar/editar página — incompatível com o modelo offline do `wk.pyz` (zero dependências externas, sem chamada de rede) |

### A8 — poda de órfãos

Fonte removida, renomeada ou retopicada em `raw/` deixa um `.docx` morto em
`wiki-docx/` — nada no plano anterior tratava disso. Comportamento definido:
ao fim de `wk docx`, varrer a árvore de saída e remover todo `.docx` cujo
path não esteja no conjunto gerado **nesta execução**; `--no-prune` desliga a
varredura; os removidos entram em `"removidos"` no JSON de saída.
`cmd_compile` tem a mesma lacuna hoje para `wiki/` (não poda páginas
órfãs) — mas aqui o custo do silêncio é maior porque um sistema externo
(Copilot Studio) consome a árvore e pode continuar citando um documento cuja
fonte já não existe em `raw/`.

---

## 6. Verificação

Testes em `scripts/wk/tests/test_docx.py` — `zipfile` e
`xml.etree.ElementTree`, ambos stdlib:

| Grupo | Casos |
|---|---|
| Pacote | zip abre (`testzip() is None`) · partes obrigatórias presentes · `document.xml` bem-formado |
| OOXML estrutural | `<w:document>` declara `xmlns:w` e `xmlns:r` (A3) · `document.xml.rels` tem `Relationship` para `styles.xml` e para `numbering.xml` (A2) · `<w:tbl>` tem `<w:tblGrid>` com nº de `<w:gridCol>` = nº de colunas da tabela de origem (A1) · nenhuma `<w:tc>` sem `<w:p>` filho (A1) · `CodeChar` declara `w:type="character"` (M1) · `dcterms:created`/`modified` têm `xsi:type="dcterms:W3CDTF"` quando presentes (M2) |
| Estilos | `styles.xml` define `Heading1`–`Heading4` · primeiro `<w:p>` referencia `Heading1` · `numbering.xml` existe quando há lista, 2 `abstractNum` × 9 níveis, `numId` referenciado casa com algum `num` (C2) |
| Idempotência | `wk docx` rodado 2× seguidas sobre o mesmo `raw/` produz **a mesma árvore** de `wiki-docx/` — nenhum arquivo `-2.docx`/`-3.docx` aparece (C1) |
| Poda | fonte removida de `raw/` entre duas execuções → `.docx` correspondente desaparece de `wiki-docx/` e aparece em `"removidos"`; `--no-prune` preserva o arquivo órfão (A8) |
| Determinismo | dois builds no mesmo processo → `sha256` idêntico · todo `ZipInfo.date_time == (1980,1,1,0,0,0)` |
| Nomes | sanitização de caractere proibido e nome reservado · colisão real de `stem` gera sufixo `sha1(id)[:8]` determinístico, não contador de disco (C1) |
| Heurísticas | título vem do H1 do corpo, não do id · fallback sem H1 não devolve string vazia · resumo boilerplate de `render_inventory`/`render_dependencies` é **rejeitado**, cai no placeholder (A4) |
| Conversão | tabela ≤2 col → lista (sem `<w:tbl>`) · tabela ≥3 col e ≤10 linhas → `<w:tbl>` · tabela ≥3 col e >10 linhas → um item de lista por registro, sem `<w:tbl>` (A5) · flowchart Mermaid **sem** `subgraph` → lista "depende de" com rótulos resolvidos · flowchart Mermaid **com** `subgraph` → lista "está em", **nunca** "depende de" (C3) · `erDiagram` → lista entidade/relacionamento (M6) · `stateDiagram-v2`/`quadrantChart` → `CodeBlock` + aviso · construto não suportado preserva texto e popula `warnings` · hyperlink externo cria relação `TargetMode="External"` · link relativo não cria relação |
| CLI | roda sem `wiki/` existir · **`wiki/` inalterado após rodar** (compara conteúdo antes/depois) · JSON tem as chaves esperadas, incluindo `"removidos"` |

Comando: `cd scripts && python -m unittest discover -s wk/tests -v` (precisa
de cwd em `scripts/`; a partir da raiz falha com `ModuleNotFoundError: No
module named 'wk'`).

End-to-end manual:
1. `wk store init <tmp>` → `wk publish` de um workdir do codescan →
   `wk promote --approve`
2. `wk compile` → confere `wiki/**/*.md` intacto
3. `wk docx` → confere `wiki-docx/**/*.docx`; rodar **de novo** e confirmar
   que a árvore não muda (nenhum arquivo novo, mesmos `sha256`)
4. Abrir um `.docx` no Word: confirmar que abre **sem diálogo de reparo** e
   que o painel de navegação mostra a hierarquia de headings

---

## 7. Riscos

| Risco | Severidade | Nota |
|---|---|---|
| Sem Word/LibreOffice neste ambiente | **alta — lacuna de verificação** | Testes cobrem só conformidade estrutural. "Abre sem diálogo de reparo" exige teste manual fora daqui |
| `numbering.xml` mal formado é a causa mais comum de "conteúdo ilegível" em geradores OOXML manuais | alta | XML bem-formado ≠ Word aceita |
| Chunk de tabela órfã: linha de `<w:tbl>` isolada sem o cabeçalho no mesmo chunk é ininteligível para o retrieval | média-alta | Mitigado pela regra (c) de A5 para tabelas grandes; tabelas pequenas (≥3 col, ≤10 linhas) continuam expostas ao risco — não eliminado, só reduzido ao caso de menor volume |
| Resumo boilerplate captura texto fixo dos geradores determinísticos do codescan como se fosse resumo de conteúdo | média-alta | Mitigado por A4 (lista de rejeição + checagem de termo específico); lista de boilerplate conhecido pode ficar desatualizada se novos geradores forem adicionados ao codescan |
| Cobertura parcial de tipos Mermaid: `erDiagram` ganha tradução mínima, `stateDiagram-v2`/`quadrantChart` seguem em bloco de código apesar de obrigatórios no contrato SDD | média | M6; `NÃO VERIFICADO` se a fração de diagramas SDD cobertos por tradução (só `erDiagram` + flowchart) é suficiente para o objetivo de retrieval — medir sobre um workdir real antes de fechar v1 |
| Ausência de transporte ao SharePoint | média | M9 — ver nota abaixo, fora de escopo v1 |
| Divergência entre `wiki/` e `wiki-docx/` não detectada | média | M8 — ver nota abaixo |
| Qual campo o parser da Microsoft usa p/ heading (`styleId`, `w:name`, `outlineLvl`) — não confirmado | média | Mitigado emitindo os três consistentes |
| Determinismo do zip não é garantido entre versões de Python/zlib | média | Teste roda os dois builds no mesmo processo; sem golden file no repo |
| Heurística de título/resumo sobre corpus heterogêneo (módulos de LLM, specs, transcritos) | média | Fallbacks evitam vazio; qualidade varia por `source_type` |
| `wk ingest` apontado para `wiki-docx/` cria fonte nova desconectada da proveniência | média | M7 — `.docx` está em `_INGEST_ASSET_EXTS` ([cli.py:1227](../scripts/wk/cli.py#L1227)), `wk ingest` aceitaria qualquer `.docx` de `wiki-docx/` como asset original, sem saber que é gerado. Guarda: `docxgen.py` grava um campo de marcação (ex. `dc:identifier` = `"wk-docx-gerado"`) em `core.xml`; `wk ingest` recusa asset com essa marcação, com mensagem apontando para regenerar via `wk docx` em vez de reingerir |
| Imagens fora do escopo v1 | baixa | Corpus atual não tem; degradam para texto + link |

### M9 — transporte ao SharePoint: fora de escopo v1

Não existe no projeto nenhum código de upload/sincronização para SharePoint
(nenhuma chamada Graph/REST, nenhuma dependência de biblioteca de upload).
`wk docx` produz `wiki-docx/**/*.docx` em disco local — a transferência para
a biblioteca do SharePoint é **explicitamente fora de escopo desta v1**.
Alternativa operacional até que exista automação: sincronização de pasta via
OneDrive/SharePoint sync client apontado para `wiki-docx/`, ou upload manual
periódico pela interface web do SharePoint. `NÃO VERIFICADO`: se o sync
client preserva o conteúdo de `docProps/core.xml` gravado localmente ou o
reescreve ao subir — validar antes de depender dos metadados para a coluna
`Title`/filtros (KB §3.1).

### M8 — divergência entre `wiki/` e `wiki-docx/`

Nada no plano detecta ou impede que as duas árvores fiquem fora de sincronia
(ex. `wk docx` rodado com `--out-dir` diferente, ou rodado contra um `raw/`
mais antigo que o usado pelo último `wk compile`). Declarado como **aceito**
nesta v1 — ambas as árvores são regeradas determinísticamente a partir do
mesmo `raw/`, então divergência persistente só ocorre se as execuções forem
feitas contra estados diferentes de `raw/`, o que já é um erro operacional
fora do escopo deste comando. Se isso se mostrar insuficiente na prática:
`wk doctor` poderia comparar o conjunto de `id`s cobertos por `wiki/` contra
`wiki-docx/` e reportar divergência — não implementado nesta v1,
`NÃO VERIFICADO` se vale o custo.

### M4 — `STORE_TREE`

`wiki-docx` não está em `STORE_TREE`
([cli.py:584-589](../scripts/wk/cli.py#L584)), a lista de diretórios que
`wk store init` cria antecipadamente. Decisão: **não adicionar** —
`wk docx` cria a própria pasta de saída sob demanda (mesmo padrão de
`os.makedirs(wiki_root, exist_ok=True)` que `cmd_compile` já usa para
`wiki/`, [cli.py:996](../scripts/wk/cli.py#L996)), então `wiki-docx/`
não precisa existir previamente. Declarado explicitamente para não ser
reaberto como dúvida depois.

### A7 — comando visível à skill

Todo comando real do projeto tem a tripla: `operations/<nome>.md`, linha na
tabela de operações de [SKILL.md](../SKILL.md), e entrada em
`build_pyz.py:DOCS`
([build_pyz.py:25-46](../scripts/build_pyz.py#L25)). O plano anterior listava
só a criação de `operations/docx.md` como arquivo solto, sem as duas outras
pernas da tripla — corrigido em "Arquivos" acima: `SKILL.md` recebe linha na
tabela de operações e `build_pyz.py:DOCS` recebe entrada `"docx"` apontando
para `operations/docx.md`. Sem isso, `wk docx` existiria como comando mas
seria invisível a `wk docs --list` e ao roteador da skill.

### M10 — linha de log

`cmd_docx` grava em `log.md` no mesmo formato de
[cli.py:1055-1058](../scripts/wk/cli.py#L1055) (`_append_log`), trocando o
verbo e as contagens:

```
## [{timestamp}] docx | {n_documentos} documentos | {n_fontes} fontes | {n_removidos} removidos
```

---

## Notas de disciplina de evidência

Toda afirmação sobre comportamento do Copilot Studio/SharePoint neste
documento cita `references/copilot-studio-sharepoint-kb.md` por seção ou
linha. Onde a KB não tem base (ex. utilidade de índice para retrieval,
Decisão 7), este documento usa `DADO NÃO ENCONTRADO`. Onde este documento
faz uma dedução própria não coberta literalmente pela KB, usa `INFERÊNCIA`
com a premissa nomeada (ex. A9, escolha de DOCX sobre PDF). Nenhuma
afirmação sobre o produto é feita sem uma dessas três marcas ou uma citação
direta.
