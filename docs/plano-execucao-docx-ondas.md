# Plano de execução por ondas — `wk docx`

Status: **proposto** — não executado.
Especificação funcional: [plano-wiki-docx.md](plano-wiki-docx.md). Este documento é **só execução**: quem faz o quê, em que ordem, com que contrato, e como se prova que ficou certo.

## Princípio de isolamento

Isolamento é garantido por **propriedade exclusiva de arquivo**. Nenhum arquivo é escrito por duas ondas. Ondas A, B, C e D não têm dependência de código entre si — só dependem do **contrato IR** congelado na §0 deste documento. Por isso rodam **100% concorrentes**.

| Onda | Arquivos que ESCREVE (exclusivos) | Lê | Depende de |
|---|---|---|---|
| **A** | `scripts/wk/docx_ooxml.py`, `scripts/wk/tests/test_docx_ooxml.py` | §0 IR, §1 do plano funcional | nada |
| **B** | `scripts/wk/docx_md.py`, `scripts/wk/tests/test_docx_md.py` | §0 IR, §2 do plano funcional | nada |
| **C** | `scripts/wk/docx_meta.py`, `scripts/wk/tests/test_docx_meta.py` | §0 IR, §3/§4 do plano funcional | nada |
| **D** | `operations/docx.md`, `SKILL.md`, `scripts/build_pyz.py`, `README.md`, `INSTALL.md`, `schema.md` | §5 do plano funcional | nada |
| **E** | `scripts/wk/docxgen.py`, `scripts/wk/cli.py`, `scripts/wk/tests/test_docx_cli.py` | tudo | **A + B + C** |

```
tempo →
┌──────────┐
│  Onda A  │──┐
├──────────┤  │
│  Onda B  │──┤
├──────────┤  ├──→ ┌──────────┐
│  Onda C  │──┘    │  Onda E  │
├──────────┤       └──────────┘
│  Onda D  │────────────(independente, pode fechar antes)
└──────────┘
```

**Prova de não-interferência:** a interseção dos conjuntos "arquivos escritos" de A, B, C, D e E é vazia. Verificação obrigatória ao fim de cada onda: `git status --porcelain` deve listar **apenas** os arquivos da coluna "ESCREVE" daquela onda.

Regra para todos os executores: **nenhuma onda edita `scripts/wk/cli.py` exceto a Onda E.** Se um executor de A/B/C/D achar que precisa tocar `cli.py`, ele para e reporta — não edita.

---

## §0. Contrato IR — CONGELADO

Este é o único acoplamento entre as ondas. Nenhum executor pode alterá-lo. Se um executor concluir que o contrato é insuficiente, **para e reporta**; não improvisa.

### 0.1 Tipo `Run` (fragmento inline)

```python
{
  "text": str,            # obrigatório; texto literal já des-escapado de markdown
  "bold": bool,           # default False
  "italic": bool,         # default False
  "code": bool,           # default False — mapeia para rStyle CodeChar
  "href": str | None,     # default None; se não-None, é hyperlink externo (http/https/mailto)
}
```

Invariante: `text` nunca é `None`; string vazia é permitida mas deve ser evitada.

### 0.2 Tipo `Block`

```python
# heading
{"kind": "heading", "level": int, "runs": list[Run]}          # level ∈ 1..4

# parágrafo
{"kind": "paragraph", "runs": list[Run], "style": str}
#   style ∈ {"Normal", "CodeBlock", "Quote"}

# item de lista
{"kind": "list_item", "ordered": bool, "level": int, "runs": list[Run]}
#   level ∈ 0..8 (mapeia direto para w:ilvl)

# tabela
{"kind": "table",
 "header": list[list[Run]],          # uma lista de Runs por célula do cabeçalho
 "rows": list[list[list[Run]]]}      # linhas × células × Runs
```

Nenhum outro `kind` existe na v1. Um `kind` desconhecido é erro de contrato.

### 0.3 Assinaturas públicas por onda

Congeladas. Executor que precisar mudar assinatura **para e reporta**.

```python
# Onda A — scripts/wk/docx_ooxml.py
def build_package(blocks: list[dict], core: dict) -> bytes: ...
#   blocks: lista de Block (§0.2)
#   core:   dict com chaves opcionais title, subject, creator, category,
#           keywords, description, created, modified, identifier
#   retorna: bytes do .docx completo, determinístico

# Onda B — scripts/wk/docx_md.py
def parse(markdown: str) -> tuple[list[dict], list[str]]: ...
#   retorna: (blocks, warnings)

# Onda C — scripts/wk/docx_meta.py
def docx_filename(source: dict) -> str: ...
def derive_title(source: dict) -> str: ...
def derive_summary(source: dict) -> tuple[str, bool]: ...   # (texto, is_placeholder)
def core_props(source: dict, title: str, summary: str) -> dict: ...
#   source: o dict devolvido por _promoted_raw_sources (cli.py:952-976)
```

### 0.4 Fixture compartilhada

Cada onda que precisar de um `source` de teste usa **este** dict, copiado literalmente. Não inventar variações fora dos casos de teste próprios.

```python
SOURCE_FIXTURE = {
    "id": "sb-codescan-wiki-ai-coupling",
    "source_type": "code-repo",
    "topic": "arquitetura",
    "origin": "codescan wiki-ai @abc1234",
    "confidence": "reviewed",
    "captured_at": "2026-08-06T12:00:00Z",
    "path": "/tmp/store/raw/code-notes/coupling.md",
    "rel": "raw/code-notes/coupling.md",
    "body": "# Zonas de design — wiki-ai\n\nGrafo: 4 arestas, 3 módulos.\n",
}
```

---

## ONDA A — Núcleo OOXML

**Escreve:** `scripts/wk/docx_ooxml.py`, `scripts/wk/tests/test_docx_ooxml.py`
**Não lê nem importa** nada de `wk`, `codescan` ou `sbindex`. Só stdlib (`zipfile`, `io`, `hashlib`, `re`, `xml.etree.ElementTree` nos testes).
**Referência normativa:** §1 de [plano-wiki-docx.md](plano-wiki-docx.md) — blocos XML literais estão lá; copiar deles, não reinventar.

### A.1 — Partes fixas do pacote

Constantes de módulo com o XML literal de: `[Content_Types].xml`, `_rels/.rels`, `docProps/app.xml`, `word/styles.xml`, `word/numbering.xml`, `word/_rels/document.xml.rels`.

Detalhes obrigatórios:
- Content types **exatos** do §1 "A-extra" (atenção a `...wordprocessingml.document.main+xml` — o `.main` é literal).
- `[Content_Types].xml` só inclui o `Override` de `/word/numbering.xml` **quando** o documento tem lista. Duas variantes da constante, ou geração condicional.
- `document.xml.rels` sempre com `Relationship` para `styles.xml`; para `numbering.xml` **só quando** a parte existe; hyperlinks acrescentam entradas com `TargetMode="External"`.
- `styles.xml`: `Normal` (`w:default="1"`), `Heading1`–`Heading4`, `ListParagraph`, `CodeBlock` (`w:type="paragraph"`), `CodeChar` (`w:type="character"` — **explícito**). Cada Heading com `w:styleId` sem espaço, `w:name` com espaço (`heading 1`), `w:qFormat`, `w:outlineLvl`. Ordem de filhos de `<w:style>` segue `CT_Style` (`name`→`basedOn`→…→`qFormat`→…→`pPr`→`rPr`) — sequência estrita.
- `numbering.xml`: **2 `abstractNum`** (bullet `abstractNumId=0`, decimal `abstractNumId=1`), cada um com os **9 níveis** `<w:lvl w:ilvl="0">`..`"8"` e indentação crescente (`w:ind w:left = 720*(ilvl+1)`, `w:hanging="360"`); **2 `num`** (`numId=1`→abstract 0, `numId=2`→abstract 1). Não existe `num` por nível.

**Feito quando:** cada constante é XML bem-formado por `ET.fromstring`.

### A.2 — Builders de `document.xml`

Funções privadas que recebem `Run`/`Block` e devolvem string XML:

| Função | Recebe | Emite |
|---|---|---|
| `_run_xml(run, rel_id=None)` | `Run` | `<w:r>` com `<w:rPr>` (`b`/`i`/`rStyle=CodeChar`) + `<w:t xml:space="preserve">`; se `href`, envolve em `<w:hyperlink r:id="...">` |
| `_para_xml(runs, style, numpr=None, ind=None)` | lista de `Run` | `<w:p>` com `<w:pPr>` respeitando a ordem de `CT_PPrBase`: `pStyle` → `numPr` → `ind` → `jc` → `outlineLvl` |
| `_heading_xml(block)` | Block heading | `_para_xml` com `pStyle=Heading{level}` |
| `_list_item_xml(block)` | Block list_item | `_para_xml` com `pStyle=ListParagraph` e `<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{1|2}"/></w:numPr>` |
| `_table_xml(block)` | Block table | `<w:tbl>` na ordem `tblPr` → `tblGrid` → `tr`* |

Regras não-negociáveis:
- `xml:space="preserve"` em **todo** `<w:t>`, sem exceção.
- `<w:tblGrid>` com **um `<w:gridCol>` por coluna** — número de colunas = `len(header)`. Largura: `9026 // n_cols` (twips, largura útil A4 com as margens do `sectPr`).
- **Nenhuma `<w:tc>` sem `<w:p>` filho.** Célula sem conteúdo emite `<w:tc><w:tcPr/><w:p/></w:tc>`.
- Linha com menos células que o cabeçalho é preenchida com células vazias até `len(header)`; linha com mais é truncada e gera aviso.
- Escape XML por funções próprias `_esc_text` (`&`, `<`, `>`) e `_esc_attr` (idem + `"`). **Não usar `html.escape`.**

### A.3 — Montagem de `document.xml` + gestão de relações

`_document_xml(blocks) -> tuple[str, list[tuple[str, str]]]` devolve o XML e a lista de `(rel_id, url)` de hyperlinks descobertos, para A.1 montar `document.xml.rels`.

- Raiz com **exatamente** `xmlns:w` e `xmlns:r` (§1 A3 do plano funcional).
- IDs de hyperlink alocados sequencialmente a partir de `rId10` (deixa `rId1`–`rId9` para as partes fixas), na ordem de aparição — **determinístico**.
- `<w:sectPr>` sempre presente, último filho de `<w:body>`, com `w:pgSz` A4 (`11906×16838`) e `w:pgMar` do §1.
- Detecta se há algum `list_item` nos blocos; esse booleano decide se `numbering.xml` entra no pacote, no Content_Types e nas relações.

### A.4 — `docProps/core.xml`

`_core_xml(core: dict) -> str`.

- Namespaces: `cp`, `dc`, `dcterms`, `xsi` (todos os quatro, sempre).
- `dcterms:created` / `dcterms:modified` com `xsi:type="dcterms:W3CDTF"`. Se a data for ausente ou não parseável, **omitir o par inteiro** — nunca emitir data inventada.
- `dc:identifier` recebe o marcador `wk-docx-gerado` (guarda de reingestão, M7 do plano funcional).
- Campo ausente em `core` → elemento omitido, não elemento vazio.

### A.5 — Empacotador ZIP determinístico

`build_package(blocks, core) -> bytes`.

- `io.BytesIO` + `zipfile.ZipFile(..., "w", zipfile.ZIP_DEFLATED)`.
- Ordem de escrita **fixa**: `[Content_Types].xml`, `_rels/.rels`, `docProps/core.xml`, `docProps/app.xml`, `word/document.xml`, `word/_rels/document.xml.rels`, `word/styles.xml`, `word/numbering.xml` (última só se houver lista).
- Cada entrada via `ZipInfo` explícito: `date_time=(1980,1,1,0,0,0)`, `create_system=0`, `external_attr=0o600 << 16`, `compress_type=ZIP_DEFLATED`.
- Toda parte começa com `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>`.

### A.6 — Testes (`test_docx_ooxml.py`)

| Teste | Asserção |
|---|---|
| `test_zip_valido` | `ZipFile(BytesIO(data)).testzip() is None` |
| `test_partes_presentes` | `namelist()` contém as 7 partes obrigatórias; `numbering.xml` presente **só** com lista |
| `test_todas_partes_xml_bem_formado` | `ET.fromstring` em cada `.xml` do zip, sem exceção |
| `test_namespaces_document` | raiz de `document.xml` declara `xmlns:w` **e** `xmlns:r` |
| `test_rels_styles_e_numbering` | `document.xml.rels` tem `Relationship` de `Type` terminando em `/styles`; e em `/numbering` quando há lista |
| `test_content_type_document_main` | `[Content_Types].xml` usa `...wordprocessingml.document.main+xml` |
| `test_sectpr_presente` | último filho de `<w:body>` é `<w:sectPr>` |
| `test_tblgrid_conta_colunas` | tabela de 3 colunas → 3 `<w:gridCol>` |
| `test_tc_nunca_vazia` | nenhuma `<w:tc>` sem `<w:p>` descendente (varre todas) |
| `test_linha_curta_preenchida` | linha com 2 células e header de 3 → 3 `<w:tc>` na saída |
| `test_codechar_type_character` | `<w:style w:styleId="CodeChar">` tem `w:type="character"` |
| `test_numbering_9_niveis` | cada `abstractNum` tem 9 `<w:lvl>`, `w:ilvl` de 0 a 8; existem exatamente 2 `<w:num>` |
| `test_ilvl_no_paragrafo` | `list_item` com `level=2` → `<w:ilvl w:val="2"/>` com `numId` igual ao do nível 0 |
| `test_xsi_type_datas` | `dcterms:created` tem `xsi:type="dcterms:W3CDTF"` |
| `test_data_invalida_omite_par` | `created` inválido → nenhum `dcterms:created` no XML |
| `test_identifier_marcador` | `dc:identifier` == `wk-docx-gerado` |
| `test_xml_space_preserve` | todo `<w:t>` tem `xml:space="preserve"` |
| `test_hyperlink_external` | `Run` com `href` → `<w:hyperlink r:id>` + relação `TargetMode="External"` |
| `test_ordem_ppr` | em `<w:pPr>`, `pStyle` aparece antes de `numPr`, que aparece antes de `ind` |
| `test_determinismo_bytes` | dois `build_package` com mesma entrada → `sha256` idêntico |
| `test_datas_zip_fixas` | todo `ZipInfo.date_time == (1980,1,1,0,0,0)` |
| `test_escape_xml` | `Run` com texto `a & b < c > "d"` não quebra o parse e preserva os caracteres |

**Comando:** `cd scripts && python -m unittest wk.tests.test_docx_ooxml -v`

### A.7 — Entrega
Reportar: linhas do módulo, nº de testes, saída do unittest, saída de `git status --porcelain` (deve ter só os 2 arquivos da onda).

---

## ONDA B — Parser Markdown → IR

**Escreve:** `scripts/wk/docx_md.py`, `scripts/wk/tests/test_docx_md.py`
**Não importa** `docx_ooxml`, `cli`, nem nada do repo. Só stdlib. **Não sabe o que é OOXML.**
**Referência normativa:** §2 de [plano-wiki-docx.md](plano-wiki-docx.md).

### B.1 — Tokenizador de blocos

`_split_blocks(markdown: str) -> list[tuple[str, list[str]]]` — agrupa linhas em blocos brutos rotulados antes de qualquer conversão.

Reconhece, nesta precedência:
1. **Fence** ` ``` ` — captura até o fence de fechamento, **inclusive linhas em branco**; guarda a linguagem anotada. Fence não fechado até o EOF → fecha no EOF + aviso.
2. **Heading** `^#{1,6}\s+` — `#` além de 4 rebaixa para nível 4.
3. **Tabela** — linha começando com `|` **seguida** de linha separadora `^\s*\|[\s:|-]+\|\s*$`. Sem a separadora, não é tabela: trata como parágrafo.
4. **Item de lista** `^(\s*)([-*+]|\d+[.)])\s+` — `level = len(indent) // 2`, teto em 8. Aceita indentação de 2 ou 4 espaços; documenta a escolha (`// 2`) e trata tab como 4 espaços.
5. **Citação** `^>\s?`.
6. **Regra horizontal** `^(---|___|\*\*\*)\s*$` — mas só se **não** for a separadora de uma tabela nem o delimitador de front matter.
7. Resto: **parágrafo**, terminado por linha em branco.

Front matter: se o texto começa com `---`, o bloco até o próximo `---` é **descartado** (o `body` de `_promoted_raw_sources` já vem sem ele, mas defesa em profundidade).

### B.2 — Parser inline

`_parse_inline(text: str) -> list[Run]`.

Ordem de reconhecimento (importa — código primeiro para não interpretar markdown dentro dele):
1. `` `code` `` → `Run(code=True)`. Conteúdo **não** sofre parsing adicional.
2. `[texto](url)` → `Run(href=url)` se `url` casa `^(https?://|mailto:)`; senão `Run` de texto `"texto (url)"` sem `href`.
3. `![alt](src)` → `Run` de texto `"[Imagem: alt] (src)"`, `href` só se `src` for http(s). Emite aviso.
4. `[[wikilink]]` → `Run(bold=True)` com o miolo, colchetes removidos.
5. `**bold**` e `__bold__` → `bold=True`.
6. `*italic*` e `_italic_` → `italic=True`.
7. Resto → `Run` simples.

Aninhamento suportado: `**negrito com `código` dentro**` deve produzir dois Runs, ambos `bold=True`, o segundo também `code=True`. Casos não suportados (ex. link dentro de negrito) degradam para texto plano + aviso — nunca perdem caractere.

Runs adjacentes com atributos idênticos são **fundidos** (reduz ruído no XML e torna os testes estáveis).

### B.3 — Tabelas: classificação em dois eixos

Implementa a regra corrigida (A5 do plano funcional):

| Colunas | Linhas de dados | Saída |
|---|---|---|
| ≤ 2 | qualquer | `list_item` por linha: Runs = `[Run(col1, bold=True), Run(": "), *inline(col2)]` |
| ≥ 3 | ≤ 10 | Block `table` |
| ≥ 3 | > 10 | `list_item` por registro: `"<col1>: <hdr2>=<v2>, <hdr3>=<v3>, ..."`, com `col1` em negrito |

Constante de módulo `MAX_TABLE_ROWS = 10`, documentada como ajustável. Célula vazia no formato de registro é **omitida** do texto (não emite `hdr=`). Toda conversão de tabela ≥3 col e >10 linhas emite aviso informando quantos registros foram achatados — nunca silencioso.

Escape de `\|` dentro de célula deve ser respeitado no split.

### B.4 — Mermaid

`_convert_mermaid(lines: list[str]) -> tuple[list[dict], list[str]]`.

Detecção de tipo pela primeira linha não vazia do bloco:

| Tipo | Tratamento |
|---|---|
| `flowchart` / `graph` **com** `subgraph` | **agrupamento**. Monta mapa `id → rótulo` a partir de `ID["label"]`. Para cada aresta `<hub> --> <nó>` dentro de `subgraph X["Título"]`, emite `list_item`: `"<rótulo do nó> está em <Título>"`. **Nunca** emite "depende de". |
| `flowchart` / `graph` **sem** `subgraph` | **dependência**. `A --> B` → `"<rótulo A> depende de <rótulo B>"`. `A -->\|"texto"\| B` → `"<rótulo A> <texto> <rótulo B>"`. |
| `erDiagram` | parseia `ENT1 \|\|--o{ ENT2 : "rótulo"` → `"<ENT1> <rótulo> <ENT2> (cardinalidade <op>)"`. |
| `stateDiagram-v2`, `quadrantChart`, qualquer outro | Bloco `paragraph` com `style="CodeBlock"`, uma linha por parágrafo, precedido de `paragraph` itálico `"Diagrama <tipo> (Mermaid — não renderizado)"` + aviso. |
| flowchart com `subgraph` **e** arestas fora dele, ou sintaxe irreconhecível | **CodeBlock + aviso.** Nunca inventa o verbo. |

Todo caminho de sucesso é precedido de um `paragraph` de legenda (`"Dependências entre módulos:"` / `"Agrupamento por zona:"` / `"Entidades e relacionamentos:"`).

**Caso de teste obrigatório** — extraído literalmente de [coupling_generic.py:406-412](../scripts/codescan/coupling_generic.py#L406):
```
flowchart LR
  subgraph zone_dor["Dor"]
    ZDOR["Dor"]
    P001["modulo x | I=0.10 A=0.20"]
    ZDOR --> P001
  end
```
Saída exigida: `"modulo x | I=0.10 A=0.20 está em Dor"`. Saída **proibida**: qualquer string contendo `"Dor depende"`.

### B.5 — Degradação e avisos

- Cada bloco converte dentro de `try/except Exception`. Falha → `paragraph` `Normal` com as linhas brutas do bloco + aviso `"bloco {i} ({kind}): {tipo de erro}"`.
- Construtos não suportados (`~~strike~~`, `- [ ]`, HTML inline, `$math$`) → texto bruto preservado + aviso.
- `parse()` devolve `(blocks, warnings)`; `warnings` é lista de strings, ordenada por posição do bloco. Nunca `None`.

### B.6 — Testes (`test_docx_md.py`)

Grupos: blocos (heading 1-6, parágrafo multi-linha, lista aninhada 3 níveis, fence com linha em branco dentro, fence não fechado, citação, hr, hr vs separadora de tabela); inline (cada construto, fusão de runs adjacentes, código não sofre parsing interno, link relativo vs externo, imagem, wikilink); tabelas (2 col → lista; 3 col × 5 linhas → table; 3 col × 30 linhas → registros + aviso de achatamento; 11 col × 200 linhas → registros; linha curta; `\|` escapado); Mermaid (os 5 casos da tabela B.4 + o caso obrigatório de B.4 com a asserção negativa `assertNotIn("depende", out)`); degradação (bloco malformado preserva texto e popula warnings; nenhum caractere do input some — teste de conservação comparando conjunto de palavras entrada/saída).

**Comando:** `cd scripts && python -m unittest wk.tests.test_docx_md -v`

### B.7 — Entrega
Reportar: linhas, nº de testes, saída do unittest, `git status --porcelain` (só os 2 arquivos).

---

## ONDA C — Heurísticas e nomenclatura

**Escreve:** `scripts/wk/docx_meta.py`, `scripts/wk/tests/test_docx_meta.py`
**Importa** apenas `hashlib`, `re`, `os` (stdlib). **Não importa** `cli.py` — reimplementa `_slug` localmente? **Não**: copia a regra e documenta a duplicação, OU a Onda E injeta a função. **Decisão: a Onda C define `_slug` privada idêntica a [cli.py:945-949](../scripts/wk/cli.py#L945), com comentário apontando a origem e o motivo (evitar import circular e manter o módulo puro).**
**Referência normativa:** §3 e §4 de [plano-wiki-docx.md](plano-wiki-docx.md).

### C.1 — `docx_filename(source) -> str`

Passos, nesta ordem:
1. `domain = _slug(source["topic"] or "geral")`
2. Extrai `subject`/`scope` do `id`, testando os padrões em ordem:
   - `^sb-codescan-(?P<repo>.+)-(?P<artifact>inventory|dependencies|coupling)$` → `subject=artifact`, `scope=repo`
   - `^sb-publish-(?P<repo>.+?)-(?P<rest>(modules|sdd)-.+)$` → `subject=rest`, `scope=repo`
   - fallback: remove `^sb-[a-z]+-`, resto vira `subject`, `scope=None`
3. `stem = f"{domain}-{subject}-{scope}"` ou `f"{domain}-{subject}"`
4. Sanitiza: minúsculas; remove `" * : < > ? / \ |`; troca espaço por `-`; colapsa `-` repetido; `strip("- ")`; se o resultado (case-insensitive, sem extensão) ∈ `{con, prn, aux, nul, com0..com9, lpt0..lpt9, _vti_}` → sufixa `-doc`; remove prefixo `~$`; se vazio → `"documento"`
5. Trunca em 120 chars **preservando o final** (corta do meio: `stem[:60] + "-" + stem[-59:]`)
6. Devolve `f"{stem}.docx"`

`docx_filename_unique(source, taken: set[str]) -> str` — se `stem` já está em `taken`, sufixa `-{sha1(id).hexdigest()[:8]}`. **Determinístico**: não consulta o disco, não depende de ordem de iteração. Se ainda colidir (praticamente impossível), sufixa com o sha1 completo e emite aviso.

### C.2 — `derive_title(source) -> str`

Cascata:
1. Primeira linha do `body` que casa `^#{1,2}\s+(.+?)\s*#*\s*$` → grupo 1, com markdown inline removido (`**`, `` ` ``, `*`).
2. Do `id`, se casar um dos padrões de C.1: `f"{artifact.capitalize()} — {repo}"`.
3. Humaniza o `id`: remove `^sb-[a-z]+-`, troca `-`/`_` por espaço, capitaliza a primeira letra.
4. Se tudo falhar: `"Documento sem título"` — nunca string vazia.

Truncar em 250 chars (limite prático de `dc:title`).

### C.3 — `derive_summary(source) -> tuple[str, bool]`

Devolve `(texto, is_placeholder)`.

1. Varre os blocos de texto do `body`, pegando o **primeiro parágrafo de texto corrido** — não heading, não item de lista, não linha de tabela, não dentro de fence, não citação.
2. **Rejeita se for boilerplate conhecido.** Constante de módulo:
   ```python
   BOILERPLATE_PREFIXES = (
       "gerado deterministicamente pelo `codescan export`",
       "dependências **declaradas** nos manifests do repositório",
   )
   ```
   Comparação: normaliza (minúsculas, colapsa espaços) e testa `startswith` contra cada prefixo. Origem verificada: [export.py:154-157](../scripts/codescan/export.py#L154) e [export.py:173-177](../scripts/codescan/export.py#L173) — textos fixos, sem interpolação, idênticos entre repositórios.
3. **Segunda defesa** (boilerplate futuro não cadastrado): rejeita se o parágrafo, após remover stopwords pt-BR, **não contiver** nenhum termo específico do documento — nome do repo/módulo extraído do `id`, ou o `topic`. Lista de stopwords é constante do módulo.
4. Parágrafo aprovado → verbatim, truncado em 600 chars na fronteira de frase (último `.`, `!` ou `?` antes de 600; se não houver, última fronteira de palavra) + `"…"`. Devolve `(texto, False)`.
5. Nenhum aprovado → `(f"Artefato {source_type} do tópico {topic}, gerado a partir de {origin}.", True)`.

O flag `is_placeholder` existe para a Onda E poder registrá-lo em `avisos` no JSON.

### C.4 — `core_props(source, title, summary) -> dict`

| Chave | Valor |
|---|---|
| `title` | `title` |
| `subject` | `source["topic"]` (texto, não slug) |
| `creator` | `source["origin"]` truncado em 250 chars |
| `category` | `source["source_type"]` |
| `keywords` | `f"{topic}, {source_type}, {subject_extraído_em_C1}"` |
| `description` | `summary` |
| `created` / `modified` | `source["captured_at"]` normalizado para W3CDTF (`YYYY-MM-DDThh:mm:ssZ`); **ausente da dict** se não parseável |
| `identifier` | `"wk-docx-gerado"` (constante) |

Parsing de data: aceita `YYYY-MM-DDTHH:MM:SSZ` e `YYYY-MM-DD`. Qualquer outra coisa → omite `created`/`modified`. **Nunca inventa data** (proibido `time.time()`).

### C.5 — Testes (`test_docx_meta.py`)

| Grupo | Casos |
|---|---|
| Nomenclatura | os 4 exemplos da tabela §4 do plano funcional, byte a byte · caractere proibido em `topic` · `topic` = `"CON"` → `con-doc-...` · id com 300 chars → nome ≤ 124 chars · colisão → sufixo `sha1[:8]` estável entre duas chamadas · colisão **não** depende da ordem de iteração (testar com `taken` construído em ordens diferentes) |
| Título | H1 no topo vence o id · H2 no topo é promovido · sem heading → derivado do id · id irreconhecível → humanizado · nunca devolve `""` · markdown inline removido do título |
| Resumo | **boilerplate de `render_inventory` é rejeitado** → `is_placeholder=True` · **boilerplate de `render_dependencies` é rejeitado** · parágrafo com o nome do repo é aceito · corpo que abre com tabela → placeholder · truncamento em fronteira de frase · dois `inventory.md` de repos diferentes **não** produzem o mesmo resumo |
| core_props | data válida normalizada · data inválida omite `created` **e** `modified` · `identifier` sempre presente · `origin` longo truncado · nenhuma chave com valor `None` |

**Comando:** `cd scripts && python -m unittest wk.tests.test_docx_meta -v`

### C.6 — Entrega
Reportar: linhas, nº de testes, saída do unittest, `git status --porcelain` (só os 2 arquivos).

---

## ONDA D — Documentação e empacotamento

**Escreve:** `operations/docx.md`, `SKILL.md`, `scripts/build_pyz.py`, `README.md`, `INSTALL.md`, `schema.md`
**Não escreve nenhum `.py` de `scripts/wk/`.** Não depende de A/B/C — documenta a interface contratada, que já está congelada em §0.3 e §5 do plano funcional.

### D.1 — `operations/docx.md`

Novo arquivo, no estilo e tamanho de [operations/compile.md](../operations/compile.md). Ler esse arquivo primeiro e espelhar a estrutura. Deve conter:
- O que o comando faz e o que **não** faz (não toca `wiki/`, não publica, não sobe para o SharePoint).
- Sintaxe: `wk docx [<topico>] [--store <path>] [--out-dir wiki-docx] [--no-prune]`.
- Pré-condição: fontes promovidas em `raw/`. Não exige `wk compile` prévio.
- Saída: árvore `wiki-docx/<topic>/<source_type>/<arquivo>.docx` + JSON com `documentos`, `fontes`, `removidos`, `pulados`.
- Idempotência: rodar duas vezes produz a mesma árvore; nunca gera `-2.docx`.
- Poda: `.docx` sem fonte correspondente é removido; `--no-prune` desliga.
- Aviso explícito: o transporte para o SharePoint **não** é feito por este comando (fora de escopo v1) — alternativa operacional é sync de pasta ou upload manual.
- Limitação: `.md` não é aceito pelo Copilot Studio via SharePoint; é por isso que este comando existe. Linkar [references/copilot-studio-sharepoint-kb.md](../references/copilot-studio-sharepoint-kb.md).

### D.2 — `SKILL.md`

Acrescentar **uma linha** na tabela de operações, no mesmo formato das linhas vizinhas (`ingest`, `promote`, `compile`, `lint`). Ler a tabela e copiar o padrão exato de colunas. Não reordenar nem reformatar as linhas existentes.

### D.3 — `scripts/build_pyz.py`

Acrescentar entrada em `DOCS` mapeando `"docx"` → `operations/docx.md`, no mesmo formato das entradas existentes de `operations/*`. Não alterar `PACKAGES` (os módulos novos entram automaticamente por serem parte do pacote `wk`). Não alterar a lógica de `ignore_patterns` — `tests/` já é excluído.

Após a edição, rodar `python scripts/build_pyz.py` e confirmar que o build passa e que `operations/docx.md` está embutido.

### D.4 — `README.md`, `INSTALL.md`, `schema.md`

Edições cirúrgicas, não reescrita:
- `schema.md` — acrescentar `wiki-docx/` à árvore do store (§1), marcado como **gerado por `wk docx`, derivado, nunca fonte-verdade, não publicável**. Deixar claro que não recebe front matter (é binário) e não entra no `index.db`.
- `README.md` — acrescentar `wk docx` ao runbook, após o passo de `compile`, e à tabela de comandos.
- `INSTALL.md` — mencionar a segunda árvore onde a estrutura do store é descrita.

Registrar no relatório o fato verificado: `README.md` **não** está em `DOCS` do `build_pyz.py`, logo editá-lo não afeta `wk docs`/`wk check`; `schema.md` e `INSTALL.md` **estão**, e exigem rebuild do `.pyz` + `wk init --force` para propagar.

### D.5 — Verificação

1. `cd scripts && python build_pyz.py` — build sem erro.
2. `python wk.pyz docs --list` (ou equivalente) — `docx` aparece na listagem.
3. `python wk.pyz check ...` — sem divergência disco vs embutido para os docs alterados.
4. `cd scripts && python -m unittest discover -s wk/tests -v` — os 45 testes existentes continuam passando (Onda D não deveria afetá-los; se afetar, é bug da onda).
5. `git status --porcelain` — apenas os 6 arquivos da onda.

### D.6 — Entrega
Reportar as 5 saídas + confirmação de que nenhum `.py` de `scripts/wk/` foi tocado.

---

## ONDA E — Integração e CLI

**Escreve:** `scripts/wk/docxgen.py`, `scripts/wk/cli.py`, `scripts/wk/tests/test_docx_cli.py`
**Depende de:** A, B e C concluídas e verdes. **Não inicia antes.**
**Referência normativa:** §5 de [plano-wiki-docx.md](plano-wiki-docx.md).

### E.1 — `docxgen.py` — fachada

```python
def build_document(source: dict) -> tuple[bytes, list[str]]:
    """Monta o .docx de uma fonte promovida. Devolve (bytes, avisos)."""
```

Sequência:
1. `title = docx_meta.derive_title(source)`
2. `summary, is_placeholder = docx_meta.derive_summary(source)`
3. `body_blocks, warnings = docx_md.parse(source["body"])`
4. Monta a lista final de blocos na ordem do §3 do plano funcional:
   - `heading` nível 1 com `title`
   - `paragraph` `Normal` com `summary`
   - `*body_blocks`
   - `heading` nível 2 `"Proveniência"`
   - `list_item` por campo: `topic`, `source_type`, `confidence`, `origin`, `captured_at`, `fonte` (= `source["rel"]`)
5. `core = docx_meta.core_props(source, title, summary)`
6. `data = docx_ooxml.build_package(blocks, core)`
7. Se `is_placeholder`, acrescenta aviso `"resumo: placeholder de metadados (nenhum parágrafo próprio aprovado)"`.
8. Devolve `(data, warnings)`

**Não duplicar lógica** de A/B/C aqui — esta função só orquestra.

### E.2 — `cmd_docx` em `cli.py`

Registro do subparser imediatamente após o de `compile` ([cli.py:2001-2004](../scripts/wk/cli.py#L2001)), mesmo estilo:
```python
dx = sub.add_parser("docx", help="gera wiki-docx/ (DOCX) a partir de raw/ promovido")
dx.add_argument("topic", nargs="?", help="filtro opcional por topic")
dx.add_argument("--store", default=None, help="raiz do store (default: WK_STORE ou ./store)")
dx.add_argument("--out-dir", default="wiki-docx", help="pasta de saída dentro do store")
dx.add_argument("--no-prune", action="store_true", help="não remove .docx órfão")
dx.set_defaults(fn=cmd_docx)
```

`cmd_docx(a) -> int`:
1. `store_root = _store_root(a)`; `sources = _promoted_raw_sources(store_root, a.topic)`
2. `out_root = os.path.join(store_root, a.out_dir)`; `os.makedirs(..., exist_ok=True)` (mesmo padrão de [cli.py:996](../scripts/wk/cli.py#L996) — `wiki-docx` **não** entra em `STORE_TREE`, decisão M4)
3. Para cada fonte, em ordem determinística (ordenar por `id`): calcula path com `docx_meta.docx_filename_unique`, monta o documento, **escreve sobrescrevendo** (`open(path,"wb")`), acumula em `gerados`
4. **Poda** (se não `--no-prune`): varre `out_root` por `*.docx`, remove todo path que não esteja em `gerados`, acumula em `removidos`. Remove diretórios que ficarem vazios.
5. `_append_log(store_root, f"## [{_utc_now()}] docx | {len(gerados)} documentos | {len(sources)} fontes | {len(removidos)} removidos")`
6. Imprime o JSON do §5 do plano funcional; `return 1` se houver `pulados`, senão `0`
7. Import tardio: `from wk import docxgen` dentro da função (padrão de [cli.py:953](../scripts/wk/cli.py#L953))

**Proibido:** usar `_unique_dest`. **Proibido:** alterar `cmd_compile`, `_publish_candidates`, `STORE_TREE` ou qualquer função existente além do necessário para E.3.

### E.3 — Guarda de reingestão

`.docx` está em `_INGEST_ASSET_EXTS` ([cli.py:1227](../scripts/wk/cli.py#L1227)), então `wk ingest` aceitaria um `.docx` gerado como fonte original, criando proveniência falsa.

Em `_ingest_asset` (ou no ponto onde o asset é validado): se a extensão for `.docx`, abrir com `zipfile`, ler `docProps/core.xml` e recusar se contiver `dc:identifier` igual a `wk-docx-gerado`. Mensagem: apontar que o arquivo é gerado por `wk docx` e que a fonte é o `.md` correspondente em `raw/`. Falha ao ler o zip **não** bloqueia (arquivo pode ser um `.docx` legítimo qualquer) — só a marcação bloqueia.

Alteração mínima e localizada. Registrar no relatório o `arquivo:linha` exato do ponto tocado.

### E.4 — Testes (`test_docx_cli.py`)

Padrão de [test_corpus.py](../scripts/wk/tests/test_corpus.py): `unittest`, `tempfile.mkdtemp` + `addCleanup(shutil.rmtree)`, `cli.main(argv)` com stdout capturado.

| Teste | Asserção |
|---|---|
| `test_gera_arvore` | store com 3 fontes promovidas → 3 `.docx` em `wiki-docx/<topic>/<source_type>/` |
| `test_idempotencia` | rodar `wk docx` 2× → mesma lista de arquivos, mesmos `sha256`, **nenhum** arquivo casando `-2.docx`/`-3.docx` |
| `test_nao_toca_wiki` | snapshot de `wiki/` (paths + sha256) antes e depois → idêntico |
| `test_roda_sem_compile` | store sem `wiki/` → comando sai `0` e gera a árvore |
| `test_poda_orfao` | remove uma fonte de `raw/`, roda de novo → `.docx` some, aparece em `"removidos"` |
| `test_no_prune_preserva` | mesmo cenário com `--no-prune` → arquivo permanece, `"removidos"` vazio |
| `test_poda_remove_dir_vazio` | último `.docx` de um topic removido → diretório do topic some |
| `test_json_saida` | chaves `documentos`, `fontes`, `removidos`, `pulados` presentes; `documentos[*]` tem `path`, `id`, `source_type`, `avisos` |
| `test_log_escrito` | `log.md` ganha linha começando com `## [` e contendo `docx \|` |
| `test_filtro_topic` | `wk docx <topic>` gera só as fontes daquele topic |
| `test_out_dir` | `--out-dir alt` escreve em `store/alt/` e não em `wiki-docx/` |
| `test_ingest_recusa_docx_gerado` | `wk ingest` apontado para um `.docx` de `wiki-docx/` → exit ≠ 0, mensagem menciona `wk docx` |
| `test_ingest_aceita_docx_externo` | `.docx` sem a marcação (gerar um pelo `build_package` com `identifier` diferente) → ingest funciona normalmente |
| `test_placeholder_vira_aviso` | fonte cujo corpo é só boilerplate → `avisos` contém a menção a placeholder |

### E.5 — Regressão global

Obrigatório antes de fechar a onda:
```bash
cd scripts && python -m unittest discover -s wk/tests -v
cd scripts && python -m unittest discover -s codescan/tests -v
cd scripts && python -m unittest discover -s sbindex/tests -v
```
Os 45 testes de `wk` + 232 de `codescan` + os de `sbindex` que existiam antes **devem continuar passando**. Qualquer quebra é bug desta onda, não "teste desatualizado" — corrigir o código, não o teste.

### E.6 — E2E manual (reportar, não automatizar)
1. `wk store init <tmp>` → `wk publish` de um workdir do codescan → `wk promote --approve`
2. `wk compile` → conferir `wiki/**/*.md`
3. `wk docx` → conferir `wiki-docx/**/*.docx`
4. `wk docx` **de novo** → `find wiki-docx -name '*-2.docx'` deve retornar vazio; `sha256sum` de todos os arquivos idêntico à rodada anterior
5. Abrir um `.docx` no Word — **fora deste ambiente**, por um humano: confirmar que abre sem diálogo de reparo e que o painel de navegação mostra a hierarquia de headings

### E.7 — Entrega
Reportar: linhas de `docxgen.py`, diff de `cli.py` (só as adições), nº de testes novos, saída das 3 suítes de regressão, `git status --porcelain` (só os 3 arquivos da onda).

---

## Matriz de rastreabilidade — defeito corrigido → onda

| Defeito (do plano funcional) | Onda | Subtarefa |
|---|---|---|
| C1 `_unique_dest` | C + E | C.1 (`docx_filename_unique` determinístico) · E.2 (escrita sobrescrevendo) |
| C2 modelo `numbering.xml` | A | A.1, A.6 (`test_numbering_9_niveis`) |
| C3 Mermaid `subgraph` | B | B.4 (caso obrigatório + asserção negativa) |
| C4 premissa de índice | — | já corrigido no plano funcional; nenhuma onda gera `index.docx` |
| A1 `tblGrid` / `<w:tc>` vazia | A | A.2, A.6 |
| A2 relações `styles`/`numbering` | A | A.1, A.3, A.6 |
| A3 namespaces | A | A.3, A.6 |
| A4 resumo boilerplate | C | C.3, C.5 |
| A5 tabelas em dois eixos | B | B.3, B.6 |
| A6 caminho de ingestão A | D | D.1 (documentar consequências) |
| A7 tripla de visibilidade | D | D.1, D.2, D.3 |
| A8 poda de órfãos | E | E.2 passo 4, E.4 |
| A9 rotas alternativas | D | D.1 (registrar Lista SharePoint como trabalho futuro) |
| M1 `w:type="character"` | A | A.1, A.6 |
| M2 `xsi:type` W3CDTF | A | A.4, A.6 |
| M3 ordem de filhos | A | A.2, A.6 (`test_ordem_ppr`) |
| M4 `STORE_TREE` | E | E.2 passo 2 (decisão: não adicionar) |
| M5 chunk de tabela órfã | B | B.3 (regra de registros) |
| M6 cobertura Mermaid | B | B.4 (`erDiagram`) |
| M7 reingestão de `.docx` | A + E | A.4 (`dc:identifier`) · E.3 (guarda no ingest) |
| M8 divergência entre árvores | — | aceito na v1, documentado no plano funcional |
| M9 transporte ao SharePoint | D | D.1 (declarar fora de escopo + alternativa operacional) |
| M10 linha de log | E | E.2 passo 5 |

---

## Regras válidas para todos os executores

1. **Zero dependências externas.** Só stdlib. Nenhum `pip install`, nenhum import de terceiro. Se parecer impossível sem lib, **para e reporta**.
2. **Não editar arquivo fora da coluna "ESCREVE" da sua onda.** Nem para "corrigir um errinho". Reporta e segue.
3. **Não alterar o contrato §0.** Se ele não fecha, para e reporta.
4. **Determinismo é requisito, não otimização.** Nada de `time.time()`, `datetime.now()`, `random`, iteração sobre `set` sem `sorted`, `dict` sem ordem garantida de construção.
5. **Nunca perder conteúdo em silêncio.** Toda degradação emite aviso.
6. **Teste que falha é bug do código, não do teste.** Corrigir o código.
7. **`git status --porcelain` ao fim** — a saída faz parte do relatório e prova o isolamento.
8. Encoding UTF-8, newline `\n`, sem BOM.

## Critério de pronto — global

| # | Verificação |
|---|---|
| 1 | As 5 ondas entregues; união dos `git status` = exatamente 12 arquivos (6 novos `.py`/testes + 3 de teste + `cli.py` + 6 de doc, conforme as tabelas) sem interseção |
| 2 | `cd scripts && python -m unittest discover -s wk/tests -v` — verde, incluindo os 45 pré-existentes |
| 3 | `cd scripts && python -m unittest discover -s codescan/tests -v` — 232 verdes |
| 4 | `python scripts/build_pyz.py` — build passa, `operations/docx.md` embutido |
| 5 | E2E manual (E.6) executado, incluindo a segunda rodada de `wk docx` sem duplicação |
| 6 | **Pendência declarada:** abertura no Word real por um humano, fora deste ambiente — único item não verificável aqui (§7 do plano funcional) |
