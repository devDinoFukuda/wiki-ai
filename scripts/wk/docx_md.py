"""Parser Markdown → IR (Onda B).

Sem dependências externas: só stdlib. Não importa `docx_ooxml`, `cli`, nem
nada do repo — este módulo não sabe o que é OOXML. Contrato IR (Run/Block)
congelado em docs/plano-execucao-docx-ondas.md §0.

Público: parse(markdown) -> (blocks, warnings)
"""

import re


# ----------------------------------------------------------------------
# B.1/B.2 — Tokenizador de blocos e parser inline
# ----------------------------------------------------------------------


# ============================================================
# ONDA B.1/B.2 — tokenizador de blocos + parser inline (IR)
# Fragmento puro; nenhum import aqui — assume `re` já disponível
# no módulo final. Sem stdlib além de `re`. Não sabe o que é OOXML.
# ============================================================

# ---------- Run (contrato §0.1) ----------

def _mk_run(text, bold=False, italic=False, code=False, href=None):
    # monta um Run completo com as 5 chaves do contrato IR
    return {"text": text, "bold": bold, "italic": italic, "code": code, "href": href}


def _merge_runs(runs):
    # funde runs adjacentes com atributos idênticos; descarta runs com text==""
    out = []
    for r in runs:
        if r["text"] == "":
            continue
        if out and out[-1]["bold"] == r["bold"] and out[-1]["italic"] == r["italic"] \
                and out[-1]["code"] == r["code"] and out[-1]["href"] == r["href"]:
            prev = out[-1]
            out[-1] = _mk_run(prev["text"] + r["text"], prev["bold"], prev["italic"], prev["code"], prev["href"])
        else:
            out.append(dict(r))
    return out


# ---------- front matter (defesa em profundidade) ----------

def _strip_front_matter(markdown):
    # se o texto começa com "---", descarta até o próximo "---" (inclusive)
    if not markdown.startswith("---"):
        return markdown
    lines = markdown.split("\n")
    if lines[0].strip() != "---":
        return markdown
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:])
    # delimitador de fechamento não encontrado: não descarta nada (defensivo,
    # nunca perde conteúdo por engano)
    return markdown


# ---------- B.1 — tokenizador de blocos ----------

_RE_FENCE_OPEN = re.compile(r'^(```+|~~~+)')
_RE_HEADING = re.compile(r'^#{1,6}\s+')
_RE_TABLE_SEP = re.compile(r'^\s*\|[\s:|-]+\|\s*$')
_RE_LIST_ITEM = re.compile(r'^(\s*)([-*+]|\d+[.)])\s+')
_RE_LIST_CONT = re.compile(r'^\s+\S')
_RE_QUOTE = re.compile(r'^>\s?')
_RE_HR = re.compile(r'^(---|___|\*\*\*)\s*$')


def _starts_new_block(line, next_line):
    # detecta se `line` abre um bloco de precedência mais alta que parágrafo;
    # usado só para decidir onde um parágrafo termina (não consome nada)
    if _RE_FENCE_OPEN.match(line):
        return True
    if _RE_HEADING.match(line):
        return True
    if line.lstrip().startswith('|') and next_line is not None and _RE_TABLE_SEP.match(next_line):
        return True
    if _RE_LIST_ITEM.match(line):
        return True
    if _RE_QUOTE.match(line):
        return True
    if _RE_HR.match(line):
        return True
    return False


def _split_blocks(markdown):
    # agrupa linhas brutas em blocos rotulados, na precedência do plano B.1
    lines = markdown.split("\n")
    n = len(lines)
    i = 0
    blocks = []
    while i < n:
        line = lines[i]
        if line.strip() == "":
            i += 1
            continue

        # 1. fence ``` ou ~~~ — inclui linhas em branco internas; fecha no EOF se preciso
        m_fence = _RE_FENCE_OPEN.match(line)
        if m_fence:
            fence = m_fence.group(1)
            fence_char = fence[0]
            close_re = re.compile(r'^' + re.escape(fence_char) + '{' + str(len(fence)) + ',}\\s*$')
            block_lines = [line]
            i += 1
            while i < n:
                block_lines.append(lines[i])
                if close_re.match(lines[i]):
                    i += 1
                    break
                i += 1
            blocks.append(("fence", block_lines))
            continue

        # 2. heading — bloco de 1 linha (nível/clamp é responsabilidade de quem consome)
        if _RE_HEADING.match(line):
            blocks.append(("heading", [line]))
            i += 1
            continue

        # 3. tabela — linha "|" seguida de separadora; sem separadora vira parágrafo
        if line.lstrip().startswith('|'):
            nxt = lines[i + 1] if i + 1 < n else None
            if nxt is not None and _RE_TABLE_SEP.match(nxt):
                table_lines = []
                while i < n and lines[i].lstrip().startswith('|'):
                    table_lines.append(lines[i])
                    i += 1
                blocks.append(("table", table_lines))
                continue

        # 4. lista — agrupa linhas de lista + continuações indentadas
        if _RE_LIST_ITEM.match(line):
            list_lines = [line]
            i += 1
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "":
                    break
                if _RE_LIST_ITEM.match(nxt) or _RE_LIST_CONT.match(nxt):
                    list_lines.append(nxt)
                    i += 1
                    continue
                break
            blocks.append(("list", list_lines))
            continue

        # 5. citação
        if _RE_QUOTE.match(line):
            quote_lines = [line]
            i += 1
            while i < n and _RE_QUOTE.match(lines[i]):
                quote_lines.append(lines[i])
                i += 1
            blocks.append(("quote", quote_lines))
            continue

        # 6. regra horizontal (só chega aqui se não foi tabela, por causa da precedência acima)
        if _RE_HR.match(line):
            blocks.append(("hr", [line]))
            i += 1
            continue

        # 7. parágrafo — termina em linha em branco OU no início de bloco de precedência maior
        para_lines = [line]
        i += 1
        while i < n:
            nxt = lines[i]
            if nxt.strip() == "":
                break
            nxt2 = lines[i + 1] if i + 1 < n else None
            if _starts_new_block(nxt, nxt2):
                break
            para_lines.append(nxt)
            i += 1
        blocks.append(("paragraph", para_lines))

    return blocks


def _list_item_meta(line):
    # -> (ordered, level, texto). Tab vira 4 espaços antes do cálculo de nível;
    # level = indent_expandido // 2, teto 8.
    expanded = line.expandtabs(4)
    m = re.match(r'^(\s*)([-*+]|\d+[.)])\s+(.*)$', expanded)
    if not m:
        return (False, 0, expanded.strip())
    indent, marker, texto = m.group(1), m.group(2), m.group(3)
    ordered = re.match(r'^\d+[.)]$', marker) is not None
    level = min(len(indent) // 2, 8)
    return (ordered, level, texto)


# ---------- B.2 — parser inline ----------

_RE_CODE = re.compile(r'`([^`]+)`')
_RE_IMAGE = re.compile(r'!\[([^\]]*)\]\(([^)]*)\)')
_RE_LINK = re.compile(r'\[([^\]]*)\]\(([^)]*)\)')
_RE_WIKILINK = re.compile(r'\[\[([^\]]+)\]\]')
_RE_BOLD_STAR = re.compile(r'\*\*(.+?)\*\*', re.S)
_RE_BOLD_UNDER = re.compile(r'__(.+?)__', re.S)
_RE_ITALIC_STAR = re.compile(r'\*(.+?)\*', re.S)
_RE_ITALIC_UNDER = re.compile(r'_(.+?)_', re.S)
_RE_URL_OK = re.compile(r'^(https?://|mailto:)', re.I)
_RE_IMG_URL_OK = re.compile(r'^https?://', re.I)

# construtos reconhecidos mas não suportados: preservam texto bruto + aviso
_RE_STRIKE = re.compile(r'~~(.+?)~~', re.S)
_RE_MATH = re.compile(r'\$(.+?)\$', re.S)
_RE_HTML_INLINE = re.compile(r'</?[a-zA-Z][a-zA-Z0-9]*[^>]*>')
_RE_TASK = re.compile(r'\[([ xX])\]')


def _flush_text(buf, runs, bold, italic):
    # despeja o buffer de texto literal acumulado como um Run simples
    if buf:
        runs.append(_mk_run("".join(buf), bold, italic, False, None))
        del buf[:]


def _parse_inline_core(text, bold, italic, warnings, top):
    # varredura recursiva de `text`; `top` habilita link/imagem/wikilink
    # (não reconhecidos dentro de bold/itálico aninhado — degradam com aviso)
    runs = []
    buf = []
    i = 0
    n = len(text)
    while i < n:
        # 1. código — sem parsing adicional do conteúdo, em qualquer profundidade
        m = _RE_CODE.match(text, i)
        if m:
            _flush_text(buf, runs, bold, italic)
            runs.append(_mk_run(m.group(1), bold, italic, True, None))
            i = m.end()
            continue

        # construtos conhecidos mas não suportados — texto bruto + aviso, em qualquer profundidade
        m = _RE_STRIKE.match(text, i)
        if m:
            _flush_text(buf, runs, bold, italic)
            runs.append(_mk_run(m.group(0), bold, italic, False, None))
            warnings.append("construto não suportado (~~riscado~~): texto bruto preservado")
            i = m.end()
            continue
        m = _RE_MATH.match(text, i)
        if m:
            _flush_text(buf, runs, bold, italic)
            runs.append(_mk_run(m.group(0), bold, italic, False, None))
            warnings.append("construto não suportado ($math$): texto bruto preservado")
            i = m.end()
            continue
        m = _RE_HTML_INLINE.match(text, i)
        if m:
            _flush_text(buf, runs, bold, italic)
            runs.append(_mk_run(m.group(0), bold, italic, False, None))
            warnings.append("construto não suportado (HTML inline): texto bruto preservado")
            i = m.end()
            continue
        m = _RE_TASK.match(text, i)
        if m:
            _flush_text(buf, runs, bold, italic)
            runs.append(_mk_run(m.group(0), bold, italic, False, None))
            warnings.append("construto não suportado (task list): texto bruto preservado")
            i = m.end()
            continue

        if top:
            # 2. imagem — testada ANTES de link, pois "![" contém "["
            m = _RE_IMAGE.match(text, i)
            if m:
                _flush_text(buf, runs, bold, italic)
                alt, src = m.group(1), m.group(2)
                href = src if _RE_IMG_URL_OK.match(src) else None
                runs.append(_mk_run("[Imagem: %s] (%s)" % (alt, src), bold, italic, False, href))
                warnings.append("imagem convertida para texto: %r" % (alt,))
                i = m.end()
                continue
            # 3. link
            m = _RE_LINK.match(text, i)
            if m:
                _flush_text(buf, runs, bold, italic)
                txt, url = m.group(1), m.group(2)
                if _RE_URL_OK.match(url):
                    runs.append(_mk_run(txt, bold, italic, False, url))
                else:
                    runs.append(_mk_run("%s (%s)" % (txt, url), bold, italic, False, None))
                i = m.end()
                continue
            # 4. wikilink
            m = _RE_WIKILINK.match(text, i)
            if m:
                _flush_text(buf, runs, bold, italic)
                runs.append(_mk_run(m.group(1), True, italic, False, None))
                i = m.end()
                continue
        else:
            # aninhado em bold/itálico: link/imagem/wikilink não suportados aqui —
            # degrada para texto plano + aviso, nunca perde caractere
            m = _RE_IMAGE.match(text, i) or _RE_LINK.match(text, i) or _RE_WIKILINK.match(text, i)
            if m:
                _flush_text(buf, runs, bold, italic)
                runs.append(_mk_run(m.group(0), bold, italic, False, None))
                warnings.append("link/imagem/wikilink dentro de negrito/itálico não suportado: texto preservado")
                i = m.end()
                continue

        # 5. bold (** ou __), só se ainda não estamos dentro de bold
        if not bold:
            m = _RE_BOLD_STAR.match(text, i) or _RE_BOLD_UNDER.match(text, i)
            if m:
                _flush_text(buf, runs, bold, italic)
                runs.extend(_parse_inline_core(m.group(1), True, italic, warnings, False))
                i = m.end()
                continue

        # 6. italic (* ou _), só se ainda não estamos dentro de itálico
        if not italic:
            m = _RE_ITALIC_STAR.match(text, i) or _RE_ITALIC_UNDER.match(text, i)
            if m:
                _flush_text(buf, runs, bold, italic)
                runs.extend(_parse_inline_core(m.group(1), bold, True, warnings, False))
                i = m.end()
                continue

        # 7. resto — acumula como texto literal
        buf.append(text[i])
        i += 1

    _flush_text(buf, runs, bold, italic)
    return runs


def _parse_inline(text):
    # -> (runs, warnings); ponto de entrada público
    warnings = []
    runs = _parse_inline_core(text, False, False, warnings, True)
    runs = _merge_runs(runs)
    return (runs, warnings)

# ----------------------------------------------------------------------
# B.3/B.4/B.5 — Tabelas, Mermaid, parse() e degradação
# ----------------------------------------------------------------------

# ============================================================
# Fragmento ONDA B — parte 2: tabelas, mermaid, parse()/degradacao
# Concatenado APOS wave_b_blocks.py para formar scripts/wk/docx_md.py.
# Depende de (definidos no outro fragmento, mesmo modulo):
#   _mk_run, _merge_runs, _strip_front_matter, _split_blocks,
#   _parse_inline, _list_item_meta
# ============================================================

# Toda tabela com >=3 colunas vira Block table, qualquer que seja o numero de
# linhas (decisao do usuario, corrige M5 via cabecalho repetido por pagina em
# docx_ooxml.py, nao mais via achatamento). As duas constantes abaixo so
# controlam avisos/paragrafo de contexto, nunca a forma do Block emitido.

# A partir de quantas linhas de dados a tabela ganha um paragrafo de contexto
# ("Tabela: N registros. Colunas: ...") imediatamente antes do Block table,
# para dar contexto a um chunk de RAG que caia no meio dela. Ajustavel.
TABLE_CONTEXT_MIN_ROWS = 10

# A partir de quantas linhas de dados a tabela emite o aviso informativo de
# tabela grande (cabecalho repetido por pagina). Ajustavel.
TABLE_LARGE_HINT = 40

# Linha separadora de tabela markdown: mesma forma usada por _split_blocks (B.1)
# para reconhecer a tabela, so pipes, tracos, dois-pontos e espacos.
_TABLE_SEP_RE = re.compile(r'^\s*\|[\s:|-]+\|\s*$')

# Declaracao de no mermaid: ID["label"] | ID[label] | ID(label)
_MERMAID_NODE_RE = re.compile(r'(\w+)\s*(?:\["([^"]*)"\]|\[([^\]]*)\]|\(([^)]*)\))')

# Linha de abertura de subgraph: subgraph ID["Titulo"]  (colchete opcional)
_MERMAID_SUBGRAPH_RE = re.compile(r'^subgraph\s+(\w+)(?:\s*\["([^"]*)"\])?')

# Aresta: A --> B  ou  A -->|"texto"| B
# id pode vir com o rotulo colado (ex.: M001["api"] --> M002["domain"]); o
# rotulo em si eh ignorado aqui, ja foi capturado por _MERMAID_NODE_RE.
_MERMAID_EDGE_RE = re.compile(
    r'(\w+)(?:\[[^\]]*\]|\([^)]*\))?\s*-->\s*(?:\|"?([^|"]*)"?\|\s*)?'
    r'(\w+)(?:\[[^\]]*\]|\([^)]*\))?'
)

# Relacao erDiagram: ENT1 ||--o{ ENT2 : "rotulo"
_MERMAID_ER_RE = re.compile(r'^(\w+)\s+([|o{}\-]+)\s+(\w+)\s*:\s*"?([^"\n]*?)"?\s*$')

# ---- sequenceDiagram ----

# participant/actor com alias opcional: participant C as Cliente
_MERMAID_SEQ_PART_RE = re.compile(r'^(?:participant|actor)\s+([\w.\-]+)(?:\s+as\s+(.+))?$')

# palavras de agrupamento (mantem o texto apos a palavra-chave como condicao/rotulo)
_MERMAID_SEQ_BLOCK_RE = re.compile(r'^(alt|else|opt|loop|par|and|critical|option)\b\s*(.*)$')

# activate/deactivate explicitos (fora do atalho +/- na seta)
_MERMAID_SEQ_ACT_RE = re.compile(r'^(activate|deactivate)\s+([\w.\-]+)$')

# Note over A,B: texto | Note left of A: texto | Note right of A: texto
_MERMAID_SEQ_NOTE_RE = re.compile(r'^[Nn]ote\s+(over|left of|right of)\s+([^:]+?)\s*:\s*(.*)$')

# mensagem: A->>B: texto | A-->>+B: texto | A-)B: texto (setas mermaid, mais longas primeiro
# para nao casar um prefixo mais curto por engano)
_MERMAID_SEQ_MSG_RE = re.compile(
    r'^(\w+)\s*(-->>|->>|-->|--x|--\)|-x|-\)|->)\s*([+-]?)\s*(\w+)\s*:\s*(.*)$'
)

# ---- stateDiagram / stateDiagram-v2 ----

# renomeio de estado: state "Rotulo" as Alias  (chave aberta opcional no final, ignorada)
_MERMAID_STATE_DECL_RE = re.compile(r'^state\s+"([^"]+)"\s+as\s+(\w+)')

# transicao: A --> B  ou  A --> B : evento/condicao  ([*] representa inicio/fim)
_MERMAID_STATE_TRANS_RE = re.compile(r'^(\[\*\]|\w+)\s*-->\s*(\[\*\]|\w+)\s*(?::\s*(.*))?$')

# ---- classDiagram ----

_MERMAID_CLASS_OPEN_RE = re.compile(r'^class\s+([\w.\-]+)\s*\{$')
_MERMAID_CLASS_DECL_RE = re.compile(r'^class\s+([\w.\-]+)\s*$')
_MERMAID_CLASS_MEMBER_LINE_RE = re.compile(r'^([\w.\-]+)\s*:\s*(.+)$')
_MERMAID_CLASS_REL_ARROW = r'(<\|--|--\|>|\*--|--\*|o--|--o|\.\.\|>|<\|\.\.|\.\.>|<\.\.|-->|<--|--)'
_MERMAID_CLASS_REL_RE = re.compile(
    r'^([\w.\-]+)\s*(?:"[^"]*"\s*)?' + _MERMAID_CLASS_REL_ARROW + r'\s*(?:"[^"]*"\s*)?([\w.\-]+)\s*(?::\s*(.*))?$'
)
_MERMAID_CLASS_ARROW_LABELS = {
    "<|--": "herança", "--|>": "herança",
    "*--": "composição", "--*": "composição",
    "o--": "agregação", "--o": "agregação",
    "..|>": "realização", "<|..": "realização",
    "..>": "dependência", "<..": "dependência",
    "-->": "associação", "<--": "associação",
    "--": "link",
}

# ---- C4 (C4Context / C4Container / C4Component) ----

# chamada de macro C4: Macro(arg1, "arg2", "arg3", ...) — chave "{" final opcional (boundary)
_MERMAID_C4_CALL_RE = re.compile(r'^(\w+)\(([^)]*)\)\s*\{?\s*$')
_MERMAID_C4_ELEMENT_PREFIXES = ("Person", "System", "Container", "Component", "Deployment_Node", "Node")
_MERMAID_C4_REL_PREFIXES = ("Rel", "BiRel")


def _split_table_cells(line):
    """Divide uma linha de tabela em celulas, respeitando \\| escapado (vira '|' literal)."""
    s = line.strip()
    cells = []
    buf = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n and s[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    # pipe inicial/final de sintaxe markdown gera celula vazia extra nas pontas: remove
    if cells and cells[0] == "" and s.startswith("|"):
        cells.pop(0)
    if cells and cells[-1] == "" and s.endswith("|"):
        cells.pop()
    return cells


def _convert_table(lines):
    """Converte bloco de tabela bruto em Block(s), regra de dois eixos (B.3/A5)."""
    warnings = []
    if not lines:
        return [], warnings

    header_cells = _split_table_cells(lines[0])
    data_lines = [ln for ln in lines[1:] if not _TABLE_SEP_RE.match(ln)]
    n_cols = len(header_cells)
    n_rows = len(data_lines)

    def _row_cells(ln):
        cells = _split_table_cells(ln)
        if len(cells) < n_cols:
            cells = cells + [""] * (n_cols - len(cells))
        elif len(cells) > n_cols:
            cells = cells[:n_cols]
            warnings.append("tabela: linha com mais celulas que o cabecalho foi truncada")
        return cells

    # <= 2 colunas: sempre lista, independente do numero de linhas
    if n_cols <= 2:
        blocks = []
        for ln in data_lines:
            cells = _row_cells(ln)
            col1 = cells[0] if cells else ""
            col2 = cells[1] if len(cells) > 1 else ""
            inline_runs, w = _parse_inline(col2)
            warnings.extend(w)
            runs = [_mk_run(col1, bold=True), _mk_run(": ")] + inline_runs
            blocks.append({"kind": "list_item", "ordered": False, "level": 0, "runs": _merge_runs(runs)})
        return blocks, warnings

    # >= 3 colunas: SEMPRE Block table (decisao do usuario, opcao B). O cabecalho
    # repete a cada pagina (docx_ooxml.py:_table_xml/tblHeader); nunca achata.
    header_runs = []
    for h in header_cells:
        r, w = _parse_inline(h)
        warnings.extend(w)
        header_runs.append(_merge_runs(r))
    rows_runs = []
    for ln in data_lines:
        cells = _row_cells(ln)
        row = []
        for c in cells:
            r, w = _parse_inline(c)
            warnings.extend(w)
            row.append(_merge_runs(r))
        rows_runs.append(row)

    blocks = []
    if n_rows > TABLE_CONTEXT_MIN_ROWS:
        # paragrafo de contexto imediatamente antes da tabela: da contexto a um
        # chunk de RAG mesmo que ele caia no meio dela (item C aprovado com B).
        colunas = ", ".join(header_cells)
        contexto_texto = f"Tabela: {n_rows} registros. Colunas: {colunas}"
        blocks.append({"kind": "paragraph", "runs": [_mk_run(contexto_texto)], "style": "Normal"})
    if n_rows > TABLE_LARGE_HINT:
        warnings.append(f"tabela: {n_rows} linhas — cabeçalho repetido por página")
    blocks.append({"kind": "table", "header": header_runs, "rows": rows_runs})
    return blocks, warnings


def _mermaid_node_labels(content):
    """Mapa id -> rotulo a partir de declaracoes ID["label"]/ID[label]/ID(label)."""
    labels = {}
    for ln in content:
        for m in _MERMAID_NODE_RE.finditer(ln):
            node_id = m.group(1)
            label = m.group(2)
            if label is None:
                label = m.group(3)
            if label is None:
                label = m.group(4)
            if label is not None:
                labels[node_id] = label.strip()
    return labels


def _mermaid_grouping(content):
    """flowchart/graph COM subgraph: cada aresta dentro do subgraph -> '<no> esta em <Titulo>'."""
    labels = _mermaid_node_labels(content)
    pilha = []
    itens = []
    arestas_fora = False
    for ln in content:
        t = ln.strip()
        if not t or t.startswith("flowchart") or t.startswith("graph"):
            continue
        m_sub = _MERMAID_SUBGRAPH_RE.match(t)
        if m_sub:
            sub_id = m_sub.group(1)
            titulo = m_sub.group(2) if m_sub.group(2) is not None else labels.get(sub_id, sub_id)
            pilha.append(titulo)
            continue
        if t == "end":
            if pilha:
                pilha.pop()
            continue
        if t.startswith("classDef") or t.startswith("class "):
            continue
        m_edge = _MERMAID_EDGE_RE.search(t)
        if m_edge:
            _src, _rotulo, dst = m_edge.groups()
            if pilha:
                dst_label = labels.get(dst, dst)
                texto = f"{dst_label} está em {pilha[-1]}"
                itens.append({"kind": "list_item", "ordered": False, "level": 0, "runs": [_mk_run(texto)]})
            else:
                arestas_fora = True
            continue
        # linha de declaracao de no isolada: ja capturada em labels, ignora
    if arestas_fora or not itens:
        return [], [], False
    return itens, [], True


def _mermaid_dependency(content):
    """flowchart/graph SEM subgraph: cada aresta -> '<A> depende de <B>' (ou texto do rotulo)."""
    labels = _mermaid_node_labels(content)
    itens = []
    achou = False
    for ln in content:
        t = ln.strip()
        if not t or t.startswith("flowchart") or t.startswith("graph"):
            continue
        m_edge = _MERMAID_EDGE_RE.search(t)
        if m_edge:
            src, rotulo, dst = m_edge.groups()
            src_label = labels.get(src, src)
            dst_label = labels.get(dst, dst)
            if rotulo:
                texto = f"{src_label} {rotulo.strip()} {dst_label}"
            else:
                texto = f"{src_label} depende de {dst_label}"
            itens.append({"kind": "list_item", "ordered": False, "level": 0, "runs": [_mk_run(texto)]})
            achou = True
    if not achou:
        return [], [], False
    return itens, [], True


def _mermaid_node_list(content):
    """flowchart/graph SEM subgraph E SEM aresta: cada no declarado -> list_item com o rotulo
    (nunca o id). Nao inventa verbo de relacao — so lista os nos encontrados, em ordem de
    1a declaracao. Sem nos declarados: ok=False (cai no fallback de CodeBlock)."""
    labels = _mermaid_node_labels(content)
    if not labels:
        return [], [], False
    itens = [
        {"kind": "list_item", "ordered": False, "level": 0, "runs": [_mk_run(lbl)]}
        for lbl in labels.values()
    ]
    warns = ["mermaid: flowchart sem arestas — convertido em lista de nós"]
    return itens, warns, True


def _mermaid_er(content):
    """erDiagram: ENT1 ||--o{ ENT2 : "rotulo" -> '<ENT1> <rotulo> <ENT2> (cardinalidade <op>)'."""
    itens = []
    warns = []
    achou = False
    for ln in content:
        t = ln.strip()
        if not t or t == "erDiagram":
            continue
        m = _MERMAID_ER_RE.match(t)
        if m:
            ent1, op, ent2, rotulo = m.groups()
            texto = f"{ent1} {rotulo} {ent2} (cardinalidade {op})"
            itens.append({"kind": "list_item", "ordered": False, "level": 0, "runs": [_mk_run(texto)]})
            achou = True
        else:
            # linha nao reconhecida dentro de erDiagram: preserva bruta, nunca some em silencio
            warns.append(f"mermaid erDiagram: linha nao reconhecida: {t!r}")
            itens.append({"kind": "paragraph", "runs": [_mk_run(t)], "style": "CodeBlock"})
    if not achou:
        return [], [], False
    return itens, warns, True


def _mermaid_sequence(content):
    """sequenceDiagram: mensagens -> lista ordenada 'Ator A → Ator B: mensagem', com
    alt/else/opt/loop/par/and/critical/option como agrupamento (indentacao por nivel),
    Note over/left of/right of como observacao, e activate/deactivate (explicito ou via
    atalho +/- na seta) preservados como itens proprios."""
    aliases = {}
    for ln in content:
        m = _MERMAID_SEQ_PART_RE.match(ln.strip())
        if m:
            alias, display = m.group(1), m.group(2)
            aliases[alias] = display.strip() if display else alias

    rotulos = {"alt": "Alternativa", "else": "Senão", "opt": "Opcional", "loop": "Repetição",
               "par": "Paralelo", "and": "E", "critical": "Crítico", "option": "Opção"}
    posicoes = {"over": "sobre", "left of": "à esquerda de", "right of": "à direita de"}

    itens = []
    achou = False
    level = 0
    for ln in content:
        t = ln.strip()
        if not t or t == "sequenceDiagram" or t.startswith("%%"):
            continue
        if _MERMAID_SEQ_PART_RE.match(t):
            continue

        m_block = _MERMAID_SEQ_BLOCK_RE.match(t)
        if m_block:
            kw, cond = m_block.group(1), m_block.group(2).strip()
            rotulo = rotulos.get(kw, kw.capitalize())
            texto = f"{rotulo}: {cond}" if cond else rotulo
            item_level = max(level - 1, 0) if kw == "else" else level
            itens.append({"kind": "list_item", "ordered": True, "level": item_level,
                           "runs": [_mk_run(texto, bold=True)]})
            if kw != "else":
                level += 1
            achou = True
            continue

        if t == "end":
            level = max(level - 1, 0)
            continue

        m_act = _MERMAID_SEQ_ACT_RE.match(t)
        if m_act:
            verbo = "Ativa" if m_act.group(1) == "activate" else "Desativa"
            ator = aliases.get(m_act.group(2), m_act.group(2))
            itens.append({"kind": "list_item", "ordered": True, "level": level,
                           "runs": [_mk_run(f"{verbo} {ator}", italic=True)]})
            achou = True
            continue

        m_note = _MERMAID_SEQ_NOTE_RE.match(t)
        if m_note:
            pos, alvo, texto_nota = m_note.groups()
            alvos = ", ".join(aliases.get(a.strip(), a.strip()) for a in alvo.split(","))
            posicao = posicoes.get(pos, pos)
            itens.append({"kind": "list_item", "ordered": True, "level": level,
                           "runs": [_mk_run(f"Nota ({posicao} {alvos}): {texto_nota}", italic=True)]})
            achou = True
            continue

        m_msg = _MERMAID_SEQ_MSG_RE.match(t)
        if m_msg:
            src, _arrow, ativ, dst, msg = m_msg.groups()
            src_label = aliases.get(src, src)
            dst_label = aliases.get(dst, dst)
            msg = msg.strip()
            texto = f"{src_label} → {dst_label}: {msg}" if msg else f"{src_label} → {dst_label}"
            if ativ == "+":
                texto += " (ativa)"
            elif ativ == "-":
                texto += " (desativa)"
            itens.append({"kind": "list_item", "ordered": True, "level": level, "runs": [_mk_run(texto)]})
            achou = True
            continue
        # linha nao reconhecida (box/comentario/sintaxe rara): ignorada, nunca gera excecao

    if not achou:
        return [], [], False
    return itens, [], True


def _convert_sequence(content, tipo):
    itens, warns, ok = _mermaid_sequence(content)
    if ok:
        legenda = {"kind": "paragraph", "runs": [_mk_run("Sequência de mensagens:")], "style": "Normal"}
        return [legenda] + itens, warns
    return _mermaid_fallback(content, tipo, "nenhuma mensagem sequenceDiagram reconhecida")


def _mermaid_state_label(node, aliases, as_source):
    if node == "[*]":
        return "[*] (estado inicial)" if as_source else "[*] (estado final)"
    return aliases.get(node, node)


def _mermaid_state(content):
    """stateDiagram-v2/stateDiagram: transicoes -> linhas (origem, evento/condicao, destino).
    [*] mapeado para estado inicial (como origem) ou final (como destino)."""
    aliases = {}
    for ln in content:
        m = _MERMAID_STATE_DECL_RE.match(ln.strip())
        if m:
            aliases[m.group(2)] = m.group(1)

    linhas = []
    achou = False
    for ln in content:
        t = ln.strip()
        if not t or t in ("stateDiagram-v2", "stateDiagram") or t in ("}",):
            continue
        if t.startswith("state ") or t.startswith("%%") or t.lower().startswith("note "):
            continue
        m = _MERMAID_STATE_TRANS_RE.match(t)
        if m:
            src, dst, evento = m.groups()
            origem = _mermaid_state_label(src, aliases, True)
            destino = _mermaid_state_label(dst, aliases, False)
            linhas.append((origem, (evento or "").strip(), destino))
            achou = True
        # linha nao reconhecida (declaracao de estado composto, comentario, etc.): ignorada
    if not achou:
        return [], False
    return linhas, True


def _convert_state(content, tipo):
    linhas, ok = _mermaid_state(content)
    if not ok:
        return _mermaid_fallback(content, tipo, "nenhuma transicao stateDiagram reconhecida")
    legenda = {"kind": "paragraph", "runs": [_mk_run("Transições de estado:")], "style": "Normal"}
    header = [[_mk_run("Origem")], [_mk_run("Evento/Condição")], [_mk_run("Destino")]]
    rows = [[[_mk_run(o)], [_mk_run(e)], [_mk_run(d)]] for (o, e, d) in linhas]
    tabela = {"kind": "table", "header": header, "rows": rows}
    return [legenda, tabela], []


def _mermaid_class(content):
    """classDiagram: classes (com membros de `class X { ... }` ou `X : membro`) e relacoes
    (heranca/composicao/agregacao/associacao/dependencia/realizacao/link) -> duas colecoes."""
    classes = {}
    relacoes = []
    achou = False
    dentro = None
    for ln in content:
        t = ln.strip()
        if not t or t == "classDiagram" or t.startswith("%%"):
            continue

        if dentro is not None:
            if t == "}":
                dentro = None
                continue
            classes.setdefault(dentro, []).append(t.lstrip("+-#~ ").strip())
            achou = True
            continue

        m_open = _MERMAID_CLASS_OPEN_RE.match(t)
        if m_open:
            dentro = m_open.group(1)
            classes.setdefault(dentro, [])
            achou = True
            continue

        m_rel = _MERMAID_CLASS_REL_RE.match(t)
        if m_rel:
            origem, arrow, destino, rotulo = m_rel.groups()
            tipo_rel = _MERMAID_CLASS_ARROW_LABELS.get(arrow, arrow)
            relacoes.append((origem, tipo_rel, destino, (rotulo or "").strip()))
            classes.setdefault(origem, classes.get(origem, []))
            classes.setdefault(destino, classes.get(destino, []))
            achou = True
            continue

        m_decl = _MERMAID_CLASS_DECL_RE.match(t)
        if m_decl:
            classes.setdefault(m_decl.group(1), [])
            achou = True
            continue

        m_member = _MERMAID_CLASS_MEMBER_LINE_RE.match(t)
        if m_member:
            nome, membro = m_member.groups()
            classes.setdefault(nome, []).append(membro.lstrip("+-#~ ").strip())
            achou = True
            continue
        # linha nao reconhecida: ignorada, nunca gera excecao

    if not achou:
        return {}, [], False
    return classes, relacoes, True


def _convert_class(content, tipo):
    classes, relacoes, ok = _mermaid_class(content)
    if not ok:
        return _mermaid_fallback(content, tipo, "nenhuma classe classDiagram reconhecida")
    blocks = [{"kind": "paragraph", "runs": [_mk_run("Classes:")], "style": "Normal"}]
    header1 = [[_mk_run("Classe")], [_mk_run("Membros")]]
    rows1 = [[[_mk_run(nome)], [_mk_run("; ".join(membros) if membros else "—")]]
             for nome, membros in classes.items()]
    blocks.append({"kind": "table", "header": header1, "rows": rows1})
    if relacoes:
        blocks.append({"kind": "paragraph", "runs": [_mk_run("Relações entre classes:")], "style": "Normal"})
        header2 = [[_mk_run("Origem")], [_mk_run("Tipo")], [_mk_run("Destino")], [_mk_run("Rótulo")]]
        rows2 = [[[_mk_run(o)], [_mk_run(tp)], [_mk_run(d)], [_mk_run(r)]] for (o, tp, d, r) in relacoes]
        blocks.append({"kind": "table", "header": header2, "rows": rows2})
    return blocks, []


def _split_c4_args(raw_args):
    """Divide os argumentos de uma chamada de macro C4 respeitando virgulas dentro de aspas;
    remove as aspas de cada argumento resultante."""
    args = []
    buf = []
    in_quotes = False
    for ch in raw_args:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            continue
        if ch == ',' and not in_quotes:
            args.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    args.append("".join(buf).strip())
    return [a[1:-1] if len(a) >= 2 and a[0] == '"' and a[-1] == '"' else a for a in args]


def _mermaid_c4(content):
    """C4Context/C4Container/C4Component: chamadas de macro (Person/System/Container/Component,
    variantes _Ext/_Boundary, e Rel/BiRel) -> elementos (tipo, nome, descricao, tecnologia) e
    relacoes (origem, destino, rotulo, tecnologia)."""
    elementos = []
    relacoes = []
    ids_labels = {}
    achou = False
    for ln in content:
        t = ln.strip()
        if not t or t.startswith("C4") or t == "}" or t.lower().startswith("title") or t.startswith("%%"):
            continue
        m = _MERMAID_C4_CALL_RE.match(t)
        if not m:
            continue
        macro, raw_args = m.group(1), m.group(2)
        args = _split_c4_args(raw_args) if raw_args.strip() else []
        if not args:
            continue
        node_id = args[0]

        if macro.endswith("_Boundary"):
            nome = args[1] if len(args) > 1 else node_id
            elementos.append(("Boundary", nome, "", ""))
            ids_labels[node_id] = nome
            achou = True
            continue

        if macro.startswith(_MERMAID_C4_REL_PREFIXES):
            if len(args) < 2:
                continue
            origem_id, destino_id = args[0], args[1]
            rotulo = args[2] if len(args) > 2 else ""
            tecnologia = args[3] if len(args) > 3 else ""
            origem = ids_labels.get(origem_id, origem_id)
            destino = ids_labels.get(destino_id, destino_id)
            relacoes.append((origem, destino, rotulo, tecnologia))
            achou = True
            continue

        if macro.startswith(_MERMAID_C4_ELEMENT_PREFIXES):
            nome = args[1] if len(args) > 1 else node_id
            if macro.startswith("Container") or macro.startswith("Component"):
                tecnologia = args[2] if len(args) > 2 else ""
                descricao = args[3] if len(args) > 3 else ""
            else:
                descricao = args[2] if len(args) > 2 else ""
                tecnologia = ""
            elementos.append((macro, nome, descricao, tecnologia))
            ids_labels[node_id] = nome
            achou = True
            continue
        # macro nao reconhecida (title, UpdateElementStyle, etc.): ignorada

    if not achou:
        return [], [], False
    return elementos, relacoes, True


def _convert_c4(content, tipo):
    elementos, relacoes, ok = _mermaid_c4(content)
    if not ok:
        return _mermaid_fallback(content, tipo, "nenhum elemento C4 reconhecido")
    blocks = [{"kind": "paragraph", "runs": [_mk_run("Elementos C4:")], "style": "Normal"}]
    header1 = [[_mk_run("Tipo")], [_mk_run("Nome")], [_mk_run("Descrição")], [_mk_run("Tecnologia")]]
    rows1 = [[[_mk_run(tp)], [_mk_run(nm)], [_mk_run(ds)], [_mk_run(tc)]] for (tp, nm, ds, tc) in elementos]
    blocks.append({"kind": "table", "header": header1, "rows": rows1})
    if relacoes:
        blocks.append({"kind": "paragraph", "runs": [_mk_run("Relações C4:")], "style": "Normal"})
        header2 = [[_mk_run("Origem")], [_mk_run("Destino")], [_mk_run("Rótulo")], [_mk_run("Tecnologia")]]
        rows2 = [[[_mk_run(o)], [_mk_run(d)], [_mk_run(r)], [_mk_run(tc)]] for (o, d, r, tc) in relacoes]
        blocks.append({"kind": "table", "header": header2, "rows": rows2})
    return blocks, []


def _mermaid_fallback(content, tipo, motivo):
    """Degradacao generica: legenda italica + codigo bruto em bloco monoespacado (estilo
    CodeBlock, ja usado pelo pipeline para fences genericos) + nota de que o diagrama
    completo permanece disponivel na versao Markdown da wiki."""
    legenda = {
        "kind": "paragraph",
        "runs": [_mk_run(f"Diagrama {tipo} (Mermaid — não renderizado)", italic=True)],
        "style": "Normal",
    }
    corpo = [{"kind": "paragraph", "runs": [_mk_run(ln)], "style": "CodeBlock"} for ln in content]
    nota = {
        "kind": "paragraph",
        "runs": [_mk_run("O diagrama completo está disponível na versão Markdown da wiki.", italic=True)],
        "style": "Normal",
    }
    return [legenda] + corpo + [nota], [f"mermaid: {motivo}"]


def _convert_flowchart(content, tipo):
    tem_subgraph = any(re.search(r'\bsubgraph\b', ln) for ln in content)
    if tem_subgraph:
        itens, warns, ok = _mermaid_grouping(content)
        if ok:
            legenda = {"kind": "paragraph", "runs": [_mk_run("Agrupamento por zona:")], "style": "Normal"}
            return [legenda] + itens, warns
        return _mermaid_fallback(content, tipo, "subgraph com arestas fora dele ou sintaxe nao reconhecida")
    itens, warns, ok = _mermaid_dependency(content)
    if ok:
        legenda = {"kind": "paragraph", "runs": [_mk_run("Dependências entre módulos:")], "style": "Normal"}
        return [legenda] + itens, warns
    itens, warns, ok = _mermaid_node_list(content)
    if ok:
        legenda = {
            "kind": "paragraph",
            "runs": [_mk_run("Módulos identificados (sem dependências entre si):")],
            "style": "Normal",
        }
        return [legenda] + itens, warns
    return _mermaid_fallback(content, tipo, "sintaxe de flowchart nao reconhecida")


def _convert_er(content, tipo):
    itens, warns, ok = _mermaid_er(content)
    if ok:
        legenda = {"kind": "paragraph", "runs": [_mk_run("Entidades e relacionamentos:")], "style": "Normal"}
        return [legenda] + itens, warns
    return _mermaid_fallback(content, tipo, "nenhuma relacao erDiagram reconhecida")


# Registro extensivel tipo-mermaid -> conversor. Cada handler tem assinatura
# (content, tipo) -> (blocks, warnings) e nunca deve levantar excecao nao tratada;
# _convert_mermaid protege a chamada mesmo assim (defesa em profundidade).
_MERMAID_HANDLERS = {
    "flowchart": _convert_flowchart,
    "graph": _convert_flowchart,
    "erDiagram": _convert_er,
    "sequenceDiagram": _convert_sequence,
    "stateDiagram-v2": _convert_state,
    "stateDiagram": _convert_state,
    "classDiagram": _convert_class,
    "C4Context": _convert_c4,
    "C4Container": _convert_c4,
    "C4Component": _convert_c4,
}


def _convert_mermaid(lines):
    """Converte bloco mermaid (com linha de abertura ```` ```mermaid ```` e fechamento) em Blocks (B.4)."""
    content = list(lines)
    if content and content[0].strip().startswith("```"):
        content = content[1:]
    if content and content[-1].strip() == "```":
        content = content[:-1]

    tipo = ""
    for ln in content:
        t = ln.strip()
        if t:
            tipo = t.split()[0]
            break

    handler = _MERMAID_HANDLERS.get(tipo)
    if handler is not None:
        try:
            return handler(content, tipo)
        except Exception:
            # mermaid malformado ou sintaxe inesperada dentro de um tipo suportado:
            # nunca propaga excecao, degrada para o fallback textual
            return _mermaid_fallback(content, tipo, "erro ao converter diagrama; degradado para texto bruto")

    # tipos ainda nao suportados (quadrantChart, pie, gantt, journey, mindmap, ...) ou bloco vazio
    tipo_legenda = tipo or "desconhecido"
    return _mermaid_fallback(content, tipo_legenda, "tipo mermaid nao suportado nesta v1")


def _convert_bloco(kind, block_lines):
    """Roteador kind -> Block(s), usado por parse() dentro do try/except de degradacao (B.5)."""
    if kind == "heading":
        texto = " ".join(block_lines)
        m = re.match(r'^(#+)\s*(.*)$', texto)
        if m:
            nivel = min(len(m.group(1)), 4)
            conteudo = m.group(2)
        else:
            nivel = 1
            conteudo = texto.lstrip("#").strip()
        runs, warns = _parse_inline(conteudo)
        return [{"kind": "heading", "level": nivel, "runs": _merge_runs(runs)}], warns

    if kind == "paragraph":
        texto = " ".join(block_lines)
        runs, warns = _parse_inline(texto)
        return [{"kind": "paragraph", "runs": _merge_runs(runs), "style": "Normal"}], warns

    if kind == "quote":
        # remove o marcador '>' de cada linha antes de juntar
        conteudo_linhas = [re.sub(r'^\s*>\s?', '', ln) for ln in block_lines]
        texto = " ".join(conteudo_linhas)
        runs, warns = _parse_inline(texto)
        return [{"kind": "paragraph", "runs": _merge_runs(runs), "style": "Quote"}], warns

    if kind == "hr":
        return [{"kind": "paragraph", "runs": [_mk_run("———")], "style": "Normal"}], []

    if kind == "list":
        blocks = []
        warns = []
        for ln in block_lines:
            ordered, level, texto = _list_item_meta(ln)
            runs, w = _parse_inline(texto)
            warns.extend(w)
            blocks.append({"kind": "list_item", "ordered": ordered, "level": level, "runs": _merge_runs(runs)})
        return blocks, warns

    if kind == "table":
        return _convert_table(block_lines)

    if kind == "fence":
        abertura = block_lines[0] if block_lines else "```"
        m = re.match(r'^```\s*(\S*)', abertura.strip())
        linguagem = m.group(1).strip().lower() if m else ""
        fechado = len(block_lines) >= 2 and block_lines[-1].strip() == "```"
        warns = []
        if not fechado:
            warns.append("fence não fechado até o fim do documento")
        if linguagem == "mermaid":
            blocks, mermaid_warns = _convert_mermaid(block_lines)
            return blocks, warns + mermaid_warns
        conteudo = block_lines[1:-1] if fechado else block_lines[1:]
        # bloco de codigo generico: sem parsing inline, uma linha por paragrafo CodeBlock
        blocks = [{"kind": "paragraph", "runs": [_mk_run(ln)], "style": "CodeBlock"} for ln in conteudo]
        return blocks, warns

    # kind fora do vocabulario de _split_blocks: forca degradacao pelo chamador
    raise ValueError(f"kind desconhecido: {kind}")


def parse(markdown):
    """Onda B, assinatura congelada (§0.3): markdown -> (blocks, warnings), nunca None."""
    blocks = []
    warnings = []
    md = _strip_front_matter(markdown)
    blocos_brutos = _split_blocks(md)
    for i, item in enumerate(blocos_brutos):
        kind, block_lines = item
        try:
            novos_blocks, novos_warns = _convert_bloco(kind, block_lines)
            blocks.extend(novos_blocks)
            warnings.extend(novos_warns)
        except Exception as e:
            # degradacao: preserva as linhas brutas juntadas em um paragrafo Normal
            texto = " ".join(block_lines) if block_lines else ""
            blocks.append({"kind": "paragraph", "runs": [_mk_run(texto)], "style": "Normal"})
            warnings.append(f"bloco {i} ({kind}): {type(e).__name__}: {e}")
    return blocks, warnings
