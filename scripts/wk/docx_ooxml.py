"""Núcleo OOXML — geração determinística de pacotes .docx (Onda A).

Sem dependências externas: só stdlib. Não importa nada de `wk`, `codescan`
ou `sbindex`. Contrato IR (Run/Block) congelado em
docs/plano-execucao-docx-ondas.md §0.

Público: build_package(blocks, core) -> bytes
"""

import io
import re
import zipfile


# ----------------------------------------------------------------------
# A.1 — Partes fixas do pacote
# ----------------------------------------------------------------------

XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _rel_esc_attr(value: str) -> str:
    # Escape reservado a este módulo (Target de Relationship), evita colisão
    # com _esc_attr/_esc_text que a Onda A define para document.xml (A.2).
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def content_types_xml(has_numbering: bool) -> str:
    numbering_override = (
        '\n  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
        if has_numbering
        else ""
    )
    return (
        XML_DECL
        + "\n"
        + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        + '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        + '  <Default Extension="xml" ContentType="application/xml"/>\n'
        + '  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        + '  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        + numbering_override
        + "\n"
        + '  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>\n'
        + '  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>\n'
        + "</Types>\n"
    )


RELS_ROOT_XML = (
    XML_DECL
    + "\n"
    + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    + '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>\n'
    + '  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>\n'
    + '  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>\n'
    + "</Relationships>\n"
)


APP_XML = (
    XML_DECL
    + "\n"
    + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">\n'
    + "  <Application>wk docx</Application>\n"
    + "  <DocSecurity>0</DocSecurity>\n"
    + "  <ScaleCrop>false</ScaleCrop>\n"
    + "  <LinksUpToDate>false</LinksUpToDate>\n"
    + "  <SharedDoc>false</SharedDoc>\n"
    + "  <HyperlinksChanged>false</HyperlinksChanged>\n"
    + "  <AppVersion>1.0000</AppVersion>\n"
    + "</Properties>\n"
)


STYLES_XML = (
    XML_DECL
    + "\n"
    + '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
    + '  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">\n'
    + '    <w:name w:val="Normal"/>\n'
    + "    <w:qFormat/>\n"
    + "  </w:style>\n"
    + '  <w:style w:type="paragraph" w:styleId="Heading1">\n'
    + '    <w:name w:val="heading 1"/>\n'
    + '    <w:basedOn w:val="Normal"/>\n'
    + '    <w:next w:val="Normal"/>\n'
    + "    <w:qFormat/>\n"
    + "    <w:pPr>\n"
    + '      <w:outlineLvl w:val="0"/>\n'
    + "    </w:pPr>\n"
    + "  </w:style>\n"
    + '  <w:style w:type="paragraph" w:styleId="Heading2">\n'
    + '    <w:name w:val="heading 2"/>\n'
    + '    <w:basedOn w:val="Normal"/>\n'
    + '    <w:next w:val="Normal"/>\n'
    + "    <w:qFormat/>\n"
    + "    <w:pPr>\n"
    + '      <w:outlineLvl w:val="1"/>\n'
    + "    </w:pPr>\n"
    + "  </w:style>\n"
    + '  <w:style w:type="paragraph" w:styleId="Heading3">\n'
    + '    <w:name w:val="heading 3"/>\n'
    + '    <w:basedOn w:val="Normal"/>\n'
    + '    <w:next w:val="Normal"/>\n'
    + "    <w:qFormat/>\n"
    + "    <w:pPr>\n"
    + '      <w:outlineLvl w:val="2"/>\n'
    + "    </w:pPr>\n"
    + "  </w:style>\n"
    + '  <w:style w:type="paragraph" w:styleId="Heading4">\n'
    + '    <w:name w:val="heading 4"/>\n'
    + '    <w:basedOn w:val="Normal"/>\n'
    + '    <w:next w:val="Normal"/>\n'
    + "    <w:qFormat/>\n"
    + "    <w:pPr>\n"
    + '      <w:outlineLvl w:val="3"/>\n'
    + "    </w:pPr>\n"
    + "  </w:style>\n"
    + '  <w:style w:type="paragraph" w:styleId="ListParagraph">\n'
    + '    <w:name w:val="List Paragraph"/>\n'
    + '    <w:basedOn w:val="Normal"/>\n'
    + "    <w:qFormat/>\n"
    + "  </w:style>\n"
    + '  <w:style w:type="paragraph" w:styleId="CodeBlock">\n'
    + '    <w:name w:val="Code Block"/>\n'
    + '    <w:basedOn w:val="Normal"/>\n'
    + "    <w:qFormat/>\n"
    + "    <w:rPr>\n"
    + '      <w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>\n'
    + "    </w:rPr>\n"
    + "  </w:style>\n"
    + '  <w:style w:type="character" w:styleId="CodeChar">\n'
    + '    <w:name w:val="Code Char"/>\n'
    + '    <w:basedOn w:val="DefaultParagraphFont"/>\n'
    + "    <w:qFormat/>\n"
    + "    <w:rPr>\n"
    + '      <w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>\n'
    + "    </w:rPr>\n"
    + "  </w:style>\n"
    + "</w:styles>\n"
)


def _bullet_lvl_text(ilvl: int) -> str:
    # Alterna entre os dois caracteres exigidos pelo contrato: • e ▪.
    return "•" if ilvl % 2 == 0 else "▪"


def _numbering_lvl_xml(ilvl: int, is_bullet: bool) -> str:
    left = 720 * (ilvl + 1)
    if is_bullet:
        num_fmt = "bullet"
        lvl_text = _bullet_lvl_text(ilvl)
        rpr = (
            "      <w:rPr>\n"
            '        <w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/>\n'
            "      </w:rPr>\n"
        )
    else:
        num_fmt = "decimal"
        lvl_text = "%{0}.".format(ilvl + 1)
        rpr = ""
    return (
        '    <w:lvl w:ilvl="{ilvl}">\n'.format(ilvl=ilvl)
        + '      <w:start w:val="1"/>\n'
        + '      <w:numFmt w:val="{fmt}"/>\n'.format(fmt=num_fmt)
        + '      <w:lvlText w:val="{text}"/>\n'.format(text=lvl_text)
        + '      <w:lvlJc w:val="left"/>\n'
        + "      <w:pPr>\n"
        + '        <w:ind w:left="{left}" w:hanging="360"/>\n'.format(left=left)
        + "      </w:pPr>\n"
        + rpr
        + "    </w:lvl>\n"
    )


def _abstract_num_xml(abstract_num_id: int, is_bullet: bool) -> str:
    levels = "".join(_numbering_lvl_xml(ilvl, is_bullet) for ilvl in range(9))
    return (
        '  <w:abstractNum w:abstractNumId="{aid}">\n'.format(aid=abstract_num_id)
        + levels
        + "  </w:abstractNum>\n"
    )


NUMBERING_XML = (
    XML_DECL
    + "\n"
    + '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
    + _abstract_num_xml(0, True)
    + _abstract_num_xml(1, False)
    + '  <w:num w:numId="1">\n'
    + '    <w:abstractNumId w:val="0"/>\n'
    + "  </w:num>\n"
    + '  <w:num w:numId="2">\n'
    + '    <w:abstractNumId w:val="1"/>\n'
    + "  </w:num>\n"
    + "</w:numbering>\n"
)


def document_rels_xml(hyperlinks: list, has_numbering: bool) -> str:
    body = [
        '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>\n'
    ]
    if has_numbering:
        body.append(
            '  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>\n'
        )
    for rel_id, url in hyperlinks:
        body.append(
            '  <Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{target}" TargetMode="External"/>\n'.format(
                rid=_rel_esc_attr(rel_id), target=_rel_esc_attr(url)
            )
        )
    return (
        XML_DECL
        + "\n"
        + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "".join(body)
        + "</Relationships>\n"
    )

# ----------------------------------------------------------------------
# A.2/A.3/A.4/A.5 — Builders, document.xml, core.xml, empacotador
# ----------------------------------------------------------------------

# --- Onda A.2/A.3/A.4/A.5 — builders de document.xml, core.xml e empacotador ---
# Este fragmento é concatenado APÓS wave_a_parts.py; usa XML_DECL, content_types_xml,
# RELS_ROOT_XML, APP_XML, STYLES_XML, NUMBERING_XML, document_rels_xml definidos lá.

# Regex de data W3CDTF estrita (YYYY-MM-DDThh:mm:ssZ) — única forma aceita em core.xml.
_OOXML_A_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _esc_text(s: str) -> str:
    """Escapa &, < e > (nessa ordem — & primeiro evita escapar em duplicidade). Não usa html.escape."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(s: str) -> str:
    """Escapa &, <, > e " para uso dentro de valores de atributo."""
    return _esc_text(s).replace('"', "&quot;")


def _run_xml(run: dict, rel_id=None) -> str:
    """Renderiza um Run em <w:r>. rPr na ordem rStyle(code) -> b -> i; omite rPr se vazio.
    Se rel_id não None, envolve em <w:hyperlink r:id="...">."""
    rpr_parts = []
    if run.get("code"):
        rpr_parts.append('<w:rStyle w:val="CodeChar"/>')
    if run.get("bold"):
        rpr_parts.append("<w:b/>")
    if run.get("italic"):
        rpr_parts.append("<w:i/>")
    rpr = f'<w:rPr>{"".join(rpr_parts)}</w:rPr>' if rpr_parts else ""
    text = run.get("text")
    if text is None:
        text = ""
    xml = f'<w:r>{rpr}<w:t xml:space="preserve">{_esc_text(text)}</w:t></w:r>'
    if rel_id is not None:
        xml = f'<w:hyperlink r:id="{_esc_attr(rel_id)}">{xml}</w:hyperlink>'
    return xml


def _para_xml(runs: list, style: str, numpr: str = None, ind: str = None) -> str:
    """<w:p><w:pPr>pStyle->numPr->ind</w:pPr>{runs}</w:p>. pStyle sempre emitido (mesmo "Normal").
    Cada item de `runs` é um Run (dict); rel_id de hyperlink é lido de run["_relid"] (anotado
    previamente por _document_xml — ver comentário lá)."""
    ppr_parts = [f'<w:pStyle w:val="{_esc_attr(style)}"/>']
    if numpr:
        ppr_parts.append(numpr)
    if ind:
        ppr_parts.append(ind)
    ppr = f'<w:pPr>{"".join(ppr_parts)}</w:pPr>'
    runs_xml = "".join(_run_xml(r, r.get("_relid")) for r in (runs or []))
    return f"<w:p>{ppr}{runs_xml}</w:p>"


def _heading_xml(block: dict) -> str:
    """pStyle=Heading{level}, level clamped 1..4."""
    level = block.get("level", 1)
    level = 1 if level < 1 else (4 if level > 4 else level)
    return _para_xml(block.get("runs") or [], f"Heading{level}")


def _list_item_xml(block: dict) -> str:
    """pStyle=ListParagraph + numPr(ilvl, numId). numId=1 se não ordenada, 2 se ordenada. level clamp 0..8."""
    level = block.get("level", 0)
    level = 0 if level < 0 else (8 if level > 8 else level)
    num_id = 2 if block.get("ordered") else 1
    numpr = f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/></w:numPr>'
    return _para_xml(block.get("runs") or [], "ListParagraph", numpr=numpr)


def _ooxml_a_table_cell_xml(cell_runs: list, col_w: int) -> str:
    """Uma <w:tc>: tcPr(tcW) + <w:p> (sem pStyle — tabela não herda Normal). Célula vazia -> <w:p/> literal."""
    runs_xml = "".join(_run_xml(r, r.get("_relid")) for r in (cell_runs or []))
    p = f"<w:p>{runs_xml}</w:p>" if runs_xml else "<w:p/>"
    return f'<w:tc><w:tcPr><w:tcW w:w="{col_w}" w:type="dxa"/></w:tcPr>{p}</w:tc>'


def _table_xml(block: dict) -> tuple:
    """<w:tbl> na ordem tblPr -> tblGrid -> tr*. Retorna (xml, warnings).
    Sem w:tblStyle (TableGrid não existe em styles.xml) — usa tblW auto + tblBorders single.
    A primeira <w:tr> (cabeçalho) traz <w:trPr><w:tblHeader/></w:trPr> como primeiro filho
    (ordem de schema trPr -> tc*), fazendo o Word repetir o cabeçalho em toda página."""
    header = block.get("header") or []
    n_cols = len(header)
    if n_cols == 0:
        return "", ["tabela sem cabeçalho ignorada"]

    warnings = []
    col_w = 9026 // n_cols
    grid = "".join(f'<w:gridCol w:w="{col_w}"/>' for _ in range(n_cols))
    tbl_pr = (
        '<w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>'
        '<w:top w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:left w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:right w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        "</w:tblBorders></w:tblPr>"
    )

    # header em negrito forçado — copia cada run (não muta o dict de entrada).
    header_bold = [[{**r, "bold": True} for r in cell] for cell in header]
    header_row = "".join(_ooxml_a_table_cell_xml(c, col_w) for c in header_bold)

    rows_xml = []
    for row in block.get("rows") or []:
        cells = list(row)
        if len(cells) < n_cols:
            cells = cells + [[] for _ in range(n_cols - len(cells))]
        elif len(cells) > n_cols:
            warnings.append(f"linha com {len(cells)} células truncada para {n_cols}")
            cells = cells[:n_cols]
        rows_xml.append("".join(_ooxml_a_table_cell_xml(c, col_w) for c in cells))

    tbl = (
        f"<w:tbl>{tbl_pr}<w:tblGrid>{grid}</w:tblGrid>"
        f"<w:tr><w:trPr><w:tblHeader/></w:trPr>{header_row}</w:tr>"
        + "".join(f"<w:tr>{r}</w:tr>" for r in rows_xml)
        + "</w:tbl>"
    )
    return tbl, warnings


def _has_list(blocks: list) -> bool:
    """Detecta presença de list_item — decide se numbering.xml entra no pacote."""
    return any(b.get("kind") == "list_item" for b in blocks)


def _ooxml_a_collect_hrefs(blocks: list) -> list:
    """Varre blocos (incl. células de tabela) coletando URLs de href, em ordem de 1ª aparição, sem repetir."""
    order = []
    seen = set()
    for b in blocks:
        kind = b.get("kind")
        if kind in ("heading", "paragraph", "list_item"):
            cells_iter = [b.get("runs") or []]
        elif kind == "table":
            cells_iter = list(b.get("header") or []) + [
                c for row in (b.get("rows") or []) for c in row
            ]
        else:
            continue
        for runs in cells_iter:
            for r in runs:
                href = r.get("href")
                if href and href not in seen:
                    seen.add(href)
                    order.append(href)
    return order


def _ooxml_a_annotate_run(r: dict, relid_map: dict) -> dict:
    """Anexa r["_relid"] (rId alocado) sem mutar o dict original; run sem href/URL desconhecida passa direto."""
    href = r.get("href")
    if href and href in relid_map:
        nr = dict(r)
        nr["_relid"] = relid_map[href]
        return nr
    return r


def _ooxml_a_annotate_block(b: dict, relid_map: dict) -> dict:
    """Cópia rasa do bloco com runs anotados com _relid — nunca muta o bloco de entrada."""
    kind = b.get("kind")
    if kind in ("heading", "paragraph", "list_item"):
        nb = dict(b)
        nb["runs"] = [_ooxml_a_annotate_run(r, relid_map) for r in (b.get("runs") or [])]
        return nb
    if kind == "table":
        nb = dict(b)
        nb["header"] = [
            [_ooxml_a_annotate_run(r, relid_map) for r in cell] for cell in (b.get("header") or [])
        ]
        nb["rows"] = [
            [[_ooxml_a_annotate_run(r, relid_map) for r in cell] for cell in row]
            for row in (b.get("rows") or [])
        ]
        return nb
    return b


def _document_xml(blocks: list) -> tuple:
    """Monta word/document.xml. Retorna (xml_com_XML_DECL, hyperlinks: list[(rId, url)]).
    rIds sequenciais a partir de rId10, ordem de aparição, URL repetida reusa o mesmo rId."""
    hrefs = _ooxml_a_collect_hrefs(blocks)
    relid_map = {}
    hyperlinks = []
    next_id = 10
    for href in hrefs:
        rid = f"rId{next_id}"
        next_id += 1
        relid_map[href] = rid
        hyperlinks.append((rid, href))

    body_parts = []
    for b in blocks:
        kind = b.get("kind")
        nb = _ooxml_a_annotate_block(b, relid_map)
        if kind == "heading":
            body_parts.append(_heading_xml(nb))
        elif kind == "paragraph":
            body_parts.append(_para_xml(nb.get("runs") or [], nb.get("style", "Normal")))
        elif kind == "list_item":
            body_parts.append(_list_item_xml(nb))
        elif kind == "table":
            tbl_xml, _warn = _table_xml(nb)
            body_parts.append(tbl_xml)
        else:
            # erro de contrato (§0.2) — kind fora dos 4 previstos nunca é ignorado silenciosamente.
            raise ValueError(f"kind desconhecido: {kind}")

    sect_pr = (
        "<w:sectPr>"
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1417" w:right="1440" w:bottom="1417" w:left="1440" '
        'w:header="708" w:footer="708" w:gutter="0"/>'
        "</w:sectPr>"
    )
    body = "".join(body_parts) + sect_pr
    xml = (
        XML_DECL
        + '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{body}</w:body></w:document>"
    )
    return xml, hyperlinks


def _core_xml(core: dict) -> str:
    """docProps/core.xml — cp/dc/dcterms/xsi sempre declarados. Chave ausente/None/"" -> elemento omitido.
    created/modified: só emite (com xsi:type W3CDTF) se casar o regex estrito; nunca inventa data."""
    core = core or {}
    parts = []
    for key, tag in (
        ("title", "dc:title"),
        ("subject", "dc:subject"),
        ("creator", "dc:creator"),
        ("category", "cp:category"),
        ("keywords", "cp:keywords"),
        ("description", "dc:description"),
        ("identifier", "dc:identifier"),
    ):
        val = core.get(key)
        if val:
            parts.append(f"<{tag}>{_esc_text(str(val))}</{tag}>")

    created = core.get("created")
    if created and _OOXML_A_DATE_RE.match(created):
        parts.append(f'<dcterms:created xsi:type="dcterms:W3CDTF">{_esc_text(created)}</dcterms:created>')
    modified = core.get("modified")
    if modified and _OOXML_A_DATE_RE.match(modified):
        parts.append(f'<dcterms:modified xsi:type="dcterms:W3CDTF">{_esc_text(modified)}</dcterms:modified>')

    inner = "".join(parts)
    return (
        XML_DECL
        + "<cp:coreProperties "
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"{inner}</cp:coreProperties>"
    )


def build_package(blocks: list, core: dict) -> bytes:
    """Empacota o .docx completo em memória, determinístico (mesma entrada -> mesmos bytes)."""
    has_numbering = _has_list(blocks)
    document_xml, hyperlinks = _document_xml(blocks)
    core_xml = _core_xml(core)
    content_types = content_types_xml(has_numbering)
    rels_xml = document_rels_xml(hyperlinks, has_numbering)

    entries = [
        ("[Content_Types].xml", content_types),
        ("_rels/.rels", RELS_ROOT_XML),
        ("docProps/core.xml", core_xml),
        ("docProps/app.xml", APP_XML),
        ("word/document.xml", document_xml),
        ("word/_rels/document.xml.rels", rels_xml),
        ("word/styles.xml", STYLES_XML),
    ]
    if has_numbering:
        entries.append(("word/numbering.xml", NUMBERING_XML))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 0
            info.external_attr = 0o600 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content.encode("utf-8"))
    return buf.getvalue()
