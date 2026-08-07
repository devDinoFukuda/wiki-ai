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


def _mermaid_fallback(content, tipo, motivo):
    """Degradacao generica: legenda italica + uma linha de codigo por paragrafo CodeBlock."""
    legenda = {
        "kind": "paragraph",
        "runs": [_mk_run(f"Diagrama {tipo} (Mermaid — não renderizado)", italic=True)],
        "style": "Normal",
    }
    corpo = [{"kind": "paragraph", "runs": [_mk_run(ln)], "style": "CodeBlock"} for ln in content]
    return [legenda] + corpo, [f"mermaid: {motivo}"]


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

    if tipo in ("flowchart", "graph"):
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

    if tipo == "erDiagram":
        itens, warns, ok = _mermaid_er(content)
        if ok:
            legenda = {"kind": "paragraph", "runs": [_mk_run("Entidades e relacionamentos:")], "style": "Normal"}
            return [legenda] + itens, warns
        return _mermaid_fallback(content, tipo, "nenhuma relacao erDiagram reconhecida")

    # stateDiagram-v2, quadrantChart, qualquer outro tipo ou bloco vazio
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
