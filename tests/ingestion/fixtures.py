from __future__ import annotations

import base64
import zipfile
import zlib
from pathlib import Path

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
SR = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
RELS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'

XXE = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE root [<!ENTITY payload SYSTEM "file:///etc/passwd">]>'
    "<root>&payload;</root>"
)


def _paragraph(text: str, style: str = "", numbering: tuple[str, str] | None = None) -> str:
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if numbering is not None:
        props += (
            f'<w:numPr><w:ilvl w:val="{numbering[1]}"/>'
            f'<w:numId w:val="{numbering[0]}"/></w:numPr>'
        )
    holder = f"<w:pPr>{props}</w:pPr>" if props else ""
    return f"<w:p>{holder}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _table(rows: list[list[str]]) -> str:
    body = ""
    for row in rows:
        cells = "".join(f"<w:tc>{_paragraph(cell)}</w:tc>" for cell in row)
        body += f"<w:tr>{cells}</w:tr>"
    return f"<w:tbl>{body}</w:tbl>"


DOCX_DOCUMENT = (
    f'<?xml version="1.0"?><w:document {W} {R} {A}><w:body>'
    + _paragraph("Regras de Faturamento", "Title")
    + _paragraph("Escopo", "Heading1")
    + _paragraph("O faturamento roda no dia 5.")
    + _paragraph("Primeiro item", numbering=("1", "0"))
    + _paragraph("Subitem", numbering=("1", "1"))
    + _paragraph("Excecoes", "Heading2")
    + _table([["Regra", "Valor"], ["Desconto", "10%"]])
    + '<w:p><w:hyperlink r:id="rId9"><w:r><w:t>portal</w:t></w:r></w:hyperlink></w:p>'
    + '<w:p><w:r><w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p>'
    + "</w:body></w:document>"
)

DOCX_RELS = (
    f'<?xml version="1.0"?><Relationships {RELS}>'
    '<Relationship Id="rId7" Type="http://x/image" Target="media/diagrama.png"/>'
    '<Relationship Id="rId9" Type="http://x/hyperlink" Target="https://portal.example"/>'
    "</Relationships>"
)

DOCX_HEADER = f'<?xml version="1.0"?><w:hdr {W}>{_paragraph("Cabecalho corporativo")}</w:hdr>'
DOCX_FOOTER = f'<?xml version="1.0"?><w:ftr {W}>{_paragraph("Pagina confidencial")}</w:ftr>'
DOCX_FOOTNOTES = (
    f'<?xml version="1.0"?><w:footnotes {W}>'
    f'<w:footnote w:id="-1" w:type="separator">{_paragraph("")}</w:footnote>'
    f'<w:footnote w:id="2">{_paragraph("Nota de rodape sobre imposto")}</w:footnote>'
    "</w:footnotes>"
)
DOCX_ENDNOTES = (
    f'<?xml version="1.0"?><w:endnotes {W}>'
    f'<w:endnote w:id="3">{_paragraph("Nota de fim sobre auditoria")}</w:endnote>'
    "</w:endnotes>"
)
DOCX_COMMENTS = (
    f'<?xml version="1.0"?><w:comments {W}>'
    f'<w:comment w:id="1" w:author="Ana" w:date="2026-01-02T10:00:00Z">'
    f'{_paragraph("Confirmar com o juridico")}</w:comment>'
    "</w:comments>"
)
DOCX_CORE = (
    '<?xml version="1.0"?><cp:coreProperties '
    'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<dc:title>Regras de Faturamento</dc:title>"
    "<dc:creator>Equipe Financeira</dc:creator>"
    "</cp:coreProperties>"
)


def write_docx(path: Path, document: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("word/document.xml", document or DOCX_DOCUMENT)
        archive.writestr("word/_rels/document.xml.rels", DOCX_RELS)
        archive.writestr("word/header1.xml", DOCX_HEADER)
        archive.writestr("word/footer1.xml", DOCX_FOOTER)
        archive.writestr("word/footnotes.xml", DOCX_FOOTNOTES)
        archive.writestr("word/endnotes.xml", DOCX_ENDNOTES)
        archive.writestr("word/comments.xml", DOCX_COMMENTS)
        archive.writestr("word/media/diagrama.png", b"\x89PNG\r\n\x1a\n" + b"0" * 120)
        archive.writestr("docProps/core.xml", DOCX_CORE)
    return path


SHARED_STRINGS = [
    "Regra",
    "Condicao",
    "Limite",
    "Acao",
    "Owner",
    "Desconto A",
    "valor > 100",
    "aplicar",
    "Financeiro",
    "Desconto B",
    "valor > 500",
    "escalar",
    "Diretoria",
    "Catalogo",
]


def _shared_strings_xml() -> str:
    items = "".join(f"<si><t>{text}</t></si>" for text in SHARED_STRINGS)
    return f'<?xml version="1.0"?><sst {S} count="{len(SHARED_STRINGS)}">{items}</sst>'


def _cell(reference: str, index: int | None = None, number: str = "", formula: str = "") -> str:
    if formula:
        return f'<c r="{reference}"><f>{formula}</f><v>0</v></c>'
    if index is not None:
        return f'<c r="{reference}" t="s"><v>{index}</v></c>'
    return f'<c r="{reference}"><v>{number}</v></c>'


def _rules_sheet() -> str:
    rows = [
        (12, [_cell("B12", 0), _cell("C12", 1), _cell("D12", 2), _cell("E12", 3), _cell("F12", 4)]),
        (
            13,
            [
                _cell("B13", 5),
                _cell("C13", 6),
                _cell("D13", number="100"),
                _cell("E13", 7),
                _cell("F13", 8),
            ],
        ),
        (
            27,
            [
                _cell("B27", 9),
                _cell("C27", 10),
                _cell("D27", number="500"),
                _cell("E27", 11),
                _cell("F27", 12),
            ],
        ),
    ]
    filled = [
        (row, [_cell(f"{col}{row}", number=str(row)) for col in "BCDEF"])
        for row in range(14, 27)
    ]
    everything = sorted(rows + filled)
    body = "".join(
        f'<row r="{index}">{"".join(cells)}</row>' for index, cells in everything
    )
    return (
        f'<?xml version="1.0"?><worksheet {S} {SR}><sheetData>{body}</sheetData>'
        '<mergeCells count="1"><mergeCell ref="B12:C12"/></mergeCells>'
        '<dataValidations count="1"><dataValidation type="list" sqref="E13:E27" '
        'operator="equal"><formula1>"aplicar,escalar"</formula1></dataValidation>'
        "</dataValidations></worksheet>"
    )


def _resumo_sheet() -> str:
    return (
        f'<?xml version="1.0"?><worksheet {S} {SR}><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>13</v></c>'
        '<c r="B1"><f>Regras!D13+Regras!D27</f><v>600</v></c></row>'
        "</sheetData></worksheet>"
    )


XLSX_WORKBOOK = (
    f'<?xml version="1.0"?><workbook {S} {SR}><sheets>'
    '<sheet name="Regras" sheetId="1" r:id="rId1"/>'
    '<sheet name="Resumo" sheetId="2" r:id="rId2"/>'
    '<sheet name="Rascunho" sheetId="3" state="hidden" r:id="rId3"/>'
    "</sheets>"
    '<definedNames><definedName name="FaixaRegras">Regras!$B$12:$F$27</definedName>'
    "</definedNames></workbook>"
)

XLSX_WORKBOOK_RELS = (
    f'<?xml version="1.0"?><Relationships {RELS}>'
    '<Relationship Id="rId1" Type="http://x/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://x/worksheet" Target="worksheets/sheet2.xml"/>'
    '<Relationship Id="rId3" Type="http://x/worksheet" Target="worksheets/sheet3.xml"/>'
    "</Relationships>"
)

XLSX_SHEET1_RELS = (
    f'<?xml version="1.0"?><Relationships {RELS}>'
    '<Relationship Id="rId1" Type="http://x/table" Target="../tables/table1.xml"/>'
    '<Relationship Id="rId2" Type="http://x/comments" Target="../comments1.xml"/>'
    "</Relationships>"
)

XLSX_TABLE = (
    f'<?xml version="1.0"?><table {S} id="1" name="TabelaRegras" '
    'displayName="TabelaRegras" ref="B12:F27" headerRowCount="1">'
    '<tableColumns count="5">'
    '<tableColumn id="1" name="Regra"/><tableColumn id="2" name="Condicao"/>'
    '<tableColumn id="3" name="Limite"/><tableColumn id="4" name="Acao"/>'
    '<tableColumn id="5" name="Owner"/></tableColumns></table>'
)

XLSX_COMMENTS = (
    f'<?xml version="1.0"?><comments {S}><authors><author>Bruno</author></authors>'
    '<commentList><comment ref="D13" authorId="0"><text><r><t>limite revisado</t></r>'
    "</text></comment></commentList></comments>"
)

XLSX_STYLES = (
    f'<?xml version="1.0"?><styleSheet {S}>'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/></numFmts>'
    '<cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="164"/><xf numFmtId="49"/>'
    "</cellXfs></styleSheet>"
)


def write_xlsx(path: Path, workbook: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("xl/workbook.xml", workbook or XLSX_WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", XLSX_WORKBOOK_RELS)
        archive.writestr("xl/sharedStrings.xml", _shared_strings_xml())
        archive.writestr("xl/styles.xml", XLSX_STYLES)
        archive.writestr("xl/worksheets/sheet1.xml", _rules_sheet())
        archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", XLSX_SHEET1_RELS)
        archive.writestr("xl/worksheets/sheet2.xml", _resumo_sheet())
        archive.writestr(
            "xl/worksheets/sheet3.xml",
            f'<?xml version="1.0"?><worksheet {S}><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>rascunho</t></is></c></row>'
            "</sheetData></worksheet>",
        )
        archive.writestr("xl/tables/table1.xml", XLSX_TABLE)
        archive.writestr("xl/comments1.xml", XLSX_COMMENTS)
    return path


MX_MODEL = (
    "<mxGraphModel><root>"
    '<mxCell id="0"/><mxCell id="1" parent="0"/>'
    '<mxCell id="grp1" value="Dominio Faturamento" style="group;fillColor=#eeeeee" '
    'vertex="1" parent="1"><mxGeometry x="40" y="40" width="400" height="220"/></mxCell>'
    '<mxCell id="n1" value="&lt;b&gt;API Cobranca&lt;/b&gt;" '
    'style="rounded=1;fillColor=#dae8fc" vertex="1" parent="grp1">'
    '<mxGeometry x="20" y="30" width="160" height="60"/></mxCell>'
    '<mxCell id="n2" value="Banco" style="shape=cylinder;fillColor=#d5e8d4" '
    'vertex="1" parent="grp1"><mxGeometry x="220" y="30" width="120" height="80"/></mxCell>'
    '<mxCell id="e1" value="grava" style="edgeStyle=orthogonal" edge="1" parent="1" '
    'source="n1" target="n2"><mxGeometry relative="1"/></mxCell>'
    "</root></mxGraphModel>"
)

MX_PAGE2 = (
    "<mxGraphModel><root>"
    '<mxCell id="0"/><mxCell id="1" parent="0"/>'
    '<mxCell id="p2n1" value="Fila" style="shape=queue" vertex="1" parent="1">'
    '<mxGeometry x="10" y="10" width="80" height="40"/></mxCell>'
    "</root></mxGraphModel>"
)


def compress_diagram(model: str) -> str:
    import urllib.parse

    quoted = urllib.parse.quote(model, safe="")
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    raw = compressor.compress(quoted.encode("utf-8")) + compressor.flush()
    return base64.b64encode(raw).decode("ascii")


def drawio_document() -> str:
    return (
        '<mxfile host="app.diagrams.net">'
        f'<diagram id="d1" name="Arquitetura">{compress_diagram(MX_MODEL)}</diagram>'
        f'<diagram id="d2" name="Integracoes">{MX_PAGE2}</diagram>'
        "</mxfile>"
    )


def minimal_pdf(text: str = "Politica de credito aprovada") -> bytes:
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    stream = zlib.compress(content)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" /Filter /FlateDecode >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Title (Politica de Credito) /Author (Comite) /Producer (fixture) >>",
    ]
    payload = b"%PDF-1.4\n"
    for number, body in enumerate(objects, start=1):
        payload += str(number).encode("ascii") + b" 0 obj\n" + body + b"\nendobj\n"
    payload += b"trailer\n<< /Root 1 0 R /Info 6 0 R >>\n%%EOF\n"
    return payload


def image_only_pdf() -> bytes:
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /XObject << /Im0 4 0 R >> >> >>",
        b"<< /Type /XObject /Subtype /Image /Width 10 /Height 10 /Length 4 >>\nstream\n"
        b"\x00\x01\x02\x03\nendstream",
    ]
    payload = b"%PDF-1.4\n"
    for number, body in enumerate(objects, start=1):
        payload += str(number).encode("ascii") + b" 0 obj\n" + body + b"\nendobj\n"
    payload += b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    return payload


VTT = """WEBVTT

1
00:00:12.000 --> 00:00:19.500
<v Ana>Decidimos manter o corte no dia 5.</v>

2
00:00:19.500 --> 00:00:24.000
<v Ana>O time financeiro confirma amanha.</v>

3
00:00:24.000 --> 00:00:31.000
<v Bruno>Preciso do limite de 500 documentado.</v>
"""

SRT = """1
00:00:03,000 --> 00:00:07,250
Carla: A integracao roda em lote.

2
00:00:07,250 --> 00:00:11,000
Diego: Qual a janela de retry?
"""

TXT_TRANSCRIPT = """[00:12:03] Ana: O contrato vence em marco.
continuidade da mesma fala.
Bruno (00:13): Vou revisar a clausula 7.
[00:14:00] Ana: Fechado.
"""

MARKDOWN = """# Politica

Texto introdutorio da politica.

## Regras

- primeiro
- segundo

| Regra | Limite |
| --- | --- |
| A | 100 |
| B | 500 |

```python
def cobrar(valor):
    return valor
```
"""

HTML = """<html><head><title>Manual</title></head><body>
<h1>Manual Operacional</h1>
<p>Fluxo <a href="https://wiki.example/fluxo">documentado</a>.</p>
<h2>Passos</h2>
<ul><li>abrir chamado</li><li>validar</li></ul>
<table><tr><th>Etapa</th><th>Prazo</th></tr><tr><td>Triagem</td><td>2h</td></tr></table>
<pre>curl /api/v1</pre>
<script>var ignorado = 1;</script>
</body></html>
"""

JSON_DOCUMENT = '{"regra_a": {"limite": 100}, "regra_b": {"limite": 500}}'
JSONL_DOCUMENT = '{"id": 1, "nome": "alpha"}\n{"id": 2, "nome": "beta"}\n'
XML_DOCUMENT = (
    '<?xml version="1.0"?><catalogo><item id="1"><nome>Alpha</nome>'
    "<preco>100</preco></item><item id=\"2\"><nome>Beta</nome></item></catalogo>"
)
