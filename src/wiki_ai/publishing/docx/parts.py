from __future__ import annotations

from collections.abc import Sequence

from .xmltools import XML_DECLARATION, escape_attr

WORD_NAMESPACES = (
    ' xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
    ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    ' xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)
RELATIONSHIP_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"
CORE_PROPERTIES_RELATIONSHIP = (
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
)

CONTENT_TYPE_DOCUMENT = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
CONTENT_TYPE_STYLES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
)
CONTENT_TYPE_NUMBERING = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
)
CONTENT_TYPE_SETTINGS = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
)
CONTENT_TYPE_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
CONTENT_TYPE_APP = "application/vnd.openxmlformats-officedocument.extended-properties+xml"
CONTENT_TYPE_RELATIONSHIPS = "application/vnd.openxmlformats-package.relationships+xml"

BULLET_NUM_ID = 1
ORDERED_NUM_ID = 2
TABLE_WIDTH_TWIPS = 9026
LEVEL_INDENT_TWIPS = 720

_BULLET_CHARS = ("•", "▪", "○")
_HEADING_SIZES = ("32", "28", "26", "24", "22", "20")
_MONOSPACE = '<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/></w:rPr>'


def image_content_type(extension: str) -> str:
    return "image/jpeg" if extension == "jpg" else f"image/{extension}"


def content_types_xml(media_extensions: Sequence[str]) -> str:
    defaults = [
        f'<Default Extension="rels" ContentType="{CONTENT_TYPE_RELATIONSHIPS}"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
    ]
    for extension in media_extensions:
        defaults.append(
            f'<Default Extension="{escape_attr(extension)}"'
            f' ContentType="{escape_attr(image_content_type(extension))}"/>'
        )
    overrides = [
        f'<Override PartName="/word/document.xml" ContentType="{CONTENT_TYPE_DOCUMENT}"/>',
        f'<Override PartName="/word/styles.xml" ContentType="{CONTENT_TYPE_STYLES}"/>',
        f'<Override PartName="/word/numbering.xml" ContentType="{CONTENT_TYPE_NUMBERING}"/>',
        f'<Override PartName="/word/settings.xml" ContentType="{CONTENT_TYPE_SETTINGS}"/>',
        f'<Override PartName="/docProps/core.xml" ContentType="{CONTENT_TYPE_CORE}"/>',
        f'<Override PartName="/docProps/app.xml" ContentType="{CONTENT_TYPE_APP}"/>',
    ]
    return (
        XML_DECLARATION
        + f'<Types xmlns="{CONTENT_TYPES_NAMESPACE}">'
        + "".join(defaults)
        + "".join(overrides)
        + "</Types>"
    )


ROOT_RELS_XML = (
    XML_DECLARATION
    + f'<Relationships xmlns="{PACKAGE_RELATIONSHIPS}">'
    + f'<Relationship Id="rId1" Type="{RELATIONSHIP_BASE}/officeDocument"'
    ' Target="word/document.xml"/>'
    + f'<Relationship Id="rId2" Type="{CORE_PROPERTIES_RELATIONSHIP}"'
    ' Target="docProps/core.xml"/>'
    + f'<Relationship Id="rId3" Type="{RELATIONSHIP_BASE}/extended-properties"'
    ' Target="docProps/app.xml"/>'
    + "</Relationships>"
)


def settings_xml(update_fields: bool) -> str:
    update = '<w:updateFields w:val="true"/>' if update_fields else ""
    return (
        XML_DECLARATION
        + '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        + '<w:zoom w:percent="100"/>'
        + '<w:defaultTabStop w:val="708"/>'
        + update
        + "</w:settings>"
    )


def _paragraph_style(style_id: str, name: str, outline: int | None, run_properties: str) -> str:
    blocks = []
    if outline is not None:
        blocks.append('<w:spacing w:before="240" w:after="120"/>')
        blocks.append(f'<w:outlineLvl w:val="{outline}"/>')
    paragraph_properties = f'<w:pPr>{"".join(blocks)}</w:pPr>' if blocks else ""
    return (
        f'<w:style w:type="paragraph" w:styleId="{style_id}">'
        f'<w:name w:val="{name}"/>'
        '<w:basedOn w:val="Normal"/>'
        '<w:next w:val="Normal"/>'
        "<w:qFormat/>"
        f"{paragraph_properties}{run_properties}"
        "</w:style>"
    )


def _heading_styles() -> str:
    styles = []
    for index, size in enumerate(_HEADING_SIZES):
        run_properties = f'<w:rPr><w:b/><w:sz w:val="{size}"/></w:rPr>'
        styles.append(
            _paragraph_style(f"Heading{index + 1}", f"heading {index + 1}", index, run_properties)
        )
    return "".join(styles)


def _list_style(style_id: str, name: str, num_id: int) -> str:
    return (
        f'<w:style w:type="paragraph" w:styleId="{style_id}">'
        f'<w:name w:val="{name}"/>'
        '<w:basedOn w:val="Normal"/>'
        "<w:qFormat/>"
        "<w:pPr>"
        f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
        f'<w:ind w:left="{LEVEL_INDENT_TWIPS}" w:hanging="360"/>'
        "</w:pPr>"
        "</w:style>"
    )


TABLE_BORDERS = (
    "<w:tblBorders>"
    '<w:top w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
    '<w:left w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
    '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
    '<w:right w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
    '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
    '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
    "</w:tblBorders>"
)

STYLES_XML = (
    XML_DECLARATION
    + '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    + "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/>'
    "</w:rPr></w:rPrDefault></w:docDefaults>"
    + '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/><w:qFormat/></w:style>'
    + '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">'
    '<w:name w:val="Default Paragraph Font"/></w:style>'
    + _paragraph_style("Title", "Title", None, '<w:rPr><w:b/><w:sz w:val="52"/></w:rPr>')
    + _heading_styles()
    + _list_style("ListBullet", "List Bullet", BULLET_NUM_ID)
    + _list_style("ListNumber", "List Number", ORDERED_NUM_ID)
    + _paragraph_style("Code", "Code", None, _MONOSPACE)
    + '<w:style w:type="character" w:styleId="CodeChar">'
    '<w:name w:val="Code Char"/><w:basedOn w:val="DefaultParagraphFont"/>'
    f"{_MONOSPACE}</w:style>"
    + '<w:style w:type="character" w:styleId="Hyperlink">'
    '<w:name w:val="Hyperlink"/><w:basedOn w:val="DefaultParagraphFont"/>'
    '<w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr></w:style>'
    + '<w:style w:type="table" w:styleId="TableGrid">'
    '<w:name w:val="Table Grid"/>'
    f"<w:tblPr>{TABLE_BORDERS}</w:tblPr>"
    "</w:style>"
    + "</w:styles>"
)


def _numbering_level(level: int, bullet: bool) -> str:
    indent = LEVEL_INDENT_TWIPS * (level + 1)
    if bullet:
        text = _BULLET_CHARS[level % len(_BULLET_CHARS)]
        number_format = "bullet"
        run_properties = (
            '<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr>'
        )
    else:
        text = f"%{level + 1}."
        number_format = "decimal"
        run_properties = ""
    return (
        f'<w:lvl w:ilvl="{level}">'
        '<w:start w:val="1"/>'
        f'<w:numFmt w:val="{number_format}"/>'
        f'<w:lvlText w:val="{escape_attr(text)}"/>'
        '<w:lvlJc w:val="left"/>'
        f'<w:pPr><w:ind w:left="{indent}" w:hanging="360"/></w:pPr>'
        f"{run_properties}"
        "</w:lvl>"
    )


def _abstract_numbering(abstract_id: int, bullet: bool) -> str:
    levels = "".join(_numbering_level(level, bullet) for level in range(9))
    return f'<w:abstractNum w:abstractNumId="{abstract_id}">{levels}</w:abstractNum>'


NUMBERING_XML = (
    XML_DECLARATION
    + '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    + _abstract_numbering(0, True)
    + _abstract_numbering(1, False)
    + f'<w:num w:numId="{BULLET_NUM_ID}"><w:abstractNumId w:val="0"/></w:num>'
    + f'<w:num w:numId="{ORDERED_NUM_ID}"><w:abstractNumId w:val="1"/></w:num>'
    + "</w:numbering>"
)

SECTION_PROPERTIES = (
    "<w:sectPr>"
    '<w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1417" w:right="1440" w:bottom="1417" w:left="1440"'
    ' w:header="708" w:footer="708" w:gutter="0"/>'
    "</w:sectPr>"
)
