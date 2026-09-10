from __future__ import annotations

import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from .blocks import (
    Block,
    BulletList,
    CodeBlock,
    Heading,
    Image,
    ListItem,
    PageBreak,
    Paragraph,
    Run,
    Table,
    TableOfContents,
)
from .errors import PackageValidationError
from .parts import (
    BULLET_NUM_ID,
    NUMBERING_XML,
    ORDERED_NUM_ID,
    RELATIONSHIP_BASE,
    ROOT_RELS_XML,
    SECTION_PROPERTIES,
    STYLES_XML,
    TABLE_WIDTH_TWIPS,
    WORD_NAMESPACES,
    content_types_xml,
    settings_xml,
)
from .properties import CoreProperties, app_xml, core_xml
from .xmltools import XML_DECLARATION, escape_attr, escape_text

STYLES_REL_ID = "rId1"
NUMBERING_REL_ID = "rId2"
SETTINGS_REL_ID = "rId3"
FIXED_REL_IDS = frozenset({STYLES_REL_ID, NUMBERING_REL_ID, SETTINGS_REL_ID})
HYPERLINK_REL_PREFIX = "rIdH"
MEDIA_DIRECTORY = "word/media"
TOC_FIELD = ' TOC \\o "1-6" \\h \\z \\u '
TOC_PLACEHOLDER = "Sumario"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def image_extension(payload: bytes) -> str:
    for signature, extension in _IMAGE_SIGNATURES:
        if payload.startswith(signature):
            return extension
    raise PackageValidationError("formato de imagem nao reconhecido (PNG, JPEG ou GIF)")


def _run_properties(run: Run, hyperlinked: bool) -> str:
    properties = []
    if run.code:
        properties.append('<w:rStyle w:val="CodeChar"/>')
    elif hyperlinked:
        properties.append('<w:rStyle w:val="Hyperlink"/>')
    if run.bold:
        properties.append("<w:b/>")
    if run.italic:
        properties.append("<w:i/>")
    if not properties:
        return ""
    return f'<w:rPr>{"".join(properties)}</w:rPr>'


def _run_xml(run: Run, rel_id: str | None) -> str:
    properties = _run_properties(run, rel_id is not None)
    body = (
        f"<w:r>{properties}"
        f'<w:t xml:space="preserve">{escape_text(run.text)}</w:t>'
        "</w:r>"
    )
    if rel_id is None:
        return body
    return f'<w:hyperlink r:id="{escape_attr(rel_id)}">{body}</w:hyperlink>'


class _RelationshipTable:
    def __init__(self) -> None:
        self._hyperlinks: dict[str, str] = {}
        self._images: list[tuple[str, str]] = []

    def hyperlink(self, url: str) -> str:
        existing = self._hyperlinks.get(url)
        if existing is not None:
            return existing
        rel_id = f"{HYPERLINK_REL_PREFIX}{len(self._hyperlinks) + 1}"
        self._hyperlinks[url] = rel_id
        return rel_id

    def image(self, rel_id: str, target: str) -> None:
        if rel_id in FIXED_REL_IDS:
            raise PackageValidationError(f"Image.rel_id colide com parte fixa: {rel_id}")
        self._images.append((rel_id, target))

    @property
    def hyperlink_targets(self) -> tuple[tuple[str, str], ...]:
        return tuple((rel_id, url) for url, rel_id in self._hyperlinks.items())

    def document_rels_xml(self) -> str:
        entries = [
            f'<Relationship Id="{STYLES_REL_ID}" Type="{RELATIONSHIP_BASE}/styles"'
            ' Target="styles.xml"/>',
            f'<Relationship Id="{NUMBERING_REL_ID}" Type="{RELATIONSHIP_BASE}/numbering"'
            ' Target="numbering.xml"/>',
            f'<Relationship Id="{SETTINGS_REL_ID}" Type="{RELATIONSHIP_BASE}/settings"'
            ' Target="settings.xml"/>',
        ]
        for rel_id, url in self.hyperlink_targets:
            entries.append(
                f'<Relationship Id="{escape_attr(rel_id)}"'
                f' Type="{RELATIONSHIP_BASE}/hyperlink"'
                f' Target="{escape_attr(url)}" TargetMode="External"/>'
            )
        for rel_id, target in self._images:
            entries.append(
                f'<Relationship Id="{escape_attr(rel_id)}"'
                f' Type="{RELATIONSHIP_BASE}/image"'
                f' Target="{escape_attr(target)}"/>'
            )
        return (
            XML_DECLARATION
            + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(entries)
            + "</Relationships>"
        )


def _runs_xml(runs: Sequence[Run], relationships: _RelationshipTable) -> str:
    parts = []
    for run in runs:
        rel_id = relationships.hyperlink(run.hyperlink) if run.hyperlink else None
        parts.append(_run_xml(run, rel_id))
    return "".join(parts)


def _paragraph_xml(
    runs: Sequence[Run],
    relationships: _RelationshipTable,
    style: str = "Normal",
    numbering: str = "",
) -> str:
    properties = f'<w:pStyle w:val="{escape_attr(style)}"/>{numbering}'
    return f"<w:p><w:pPr>{properties}</w:pPr>{_runs_xml(runs, relationships)}</w:p>"


def _heading_xml(block: Heading, relationships: _RelationshipTable) -> str:
    return _paragraph_xml((Run(text=block.text),), relationships, f"Heading{block.level}")


def _list_item_xml(item: ListItem, ordered: bool, relationships: _RelationshipTable) -> str:
    num_id = ORDERED_NUM_ID if ordered else BULLET_NUM_ID
    numbering = f'<w:numPr><w:ilvl w:val="{item.level}"/><w:numId w:val="{num_id}"/></w:numPr>'
    style = "ListNumber" if ordered else "ListBullet"
    return _paragraph_xml(item.runs, relationships, style, numbering)


def _bullet_list_xml(block: BulletList, relationships: _RelationshipTable) -> str:
    return "".join(_list_item_xml(item, block.ordered, relationships) for item in block.items)


def _cell_xml(cell: Paragraph, width: int, bold: bool, relationships: _RelationshipTable) -> str:
    runs = tuple(replace(run, bold=True) for run in cell.runs) if bold else cell.runs
    content = _paragraph_xml(runs, relationships)
    return (
        f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>{content}</w:tc>'
    )


def _table_xml(block: Table, relationships: _RelationshipTable) -> str:
    width = TABLE_WIDTH_TWIPS // block.column_count
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(block.column_count))
    table_properties = (
        "<w:tblPr>"
        '<w:tblStyle w:val="TableGrid"/>'
        '<w:tblW w:w="0" w:type="auto"/>'
        "</w:tblPr>"
    )
    rows = []
    for index, row in enumerate(block.rows):
        is_header = block.header and index == 0
        row_properties = "<w:trPr><w:tblHeader/></w:trPr>" if is_header else ""
        cells = "".join(_cell_xml(cell, width, is_header, relationships) for cell in row)
        rows.append(f"<w:tr>{row_properties}{cells}</w:tr>")
    return f"<w:tbl>{table_properties}<w:tblGrid>{grid}</w:tblGrid>{''.join(rows)}</w:tbl>"


def _code_block_xml(block: CodeBlock, relationships: _RelationshipTable) -> str:
    paragraphs = []
    if block.language:
        paragraphs.append(
            _paragraph_xml((Run(text=block.language, italic=True),), relationships, "Code")
        )
    for line in block.text.split("\n"):
        paragraphs.append(_paragraph_xml((Run(text=line, code=True),), relationships, "Code"))
    return "".join(paragraphs)


def _image_xml(block: Image, index: int) -> str:
    name = escape_attr(f"image{index}")
    description = escape_attr(block.alt)
    return (
        "<w:p><w:r><w:drawing>"
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{block.width_emu}" cy="{block.height_emu}"/>'
        f'<wp:docPr id="{index}" name="{name}" descr="{description}"/>'
        "<a:graphic>"
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        "<pic:pic>"
        "<pic:nvPicPr>"
        f'<pic:cNvPr id="{index}" name="{name}" descr="{description}"/>'
        "<pic:cNvPicPr/>"
        "</pic:nvPicPr>"
        "<pic:blipFill>"
        f'<a:blip r:embed="{escape_attr(block.rel_id)}"/>'
        "<a:stretch><a:fillRect/></a:stretch>"
        "</pic:blipFill>"
        "<pic:spPr>"
        "<a:xfrm>"
        '<a:off x="0" y="0"/>'
        f'<a:ext cx="{block.width_emu}" cy="{block.height_emu}"/>'
        "</a:xfrm>"
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        "</pic:spPr>"
        "</pic:pic>"
        "</a:graphicData>"
        "</a:graphic>"
        "</wp:inline>"
        "</w:drawing></w:r></w:p>"
    )


def _page_break_xml() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def _table_of_contents_xml() -> str:
    return (
        "<w:p>"
        '<w:pPr><w:pStyle w:val="Normal"/></w:pPr>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve">{escape_text(TOC_FIELD)}</w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f'<w:r><w:t xml:space="preserve">{escape_text(TOC_PLACEHOLDER)}</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "</w:p>"
    )


def _block_xml(
    block: Block,
    relationships: _RelationshipTable,
    image_index: int,
) -> str:
    if isinstance(block, Heading):
        return _heading_xml(block, relationships)
    if isinstance(block, Paragraph):
        return _paragraph_xml(block.runs, relationships)
    if isinstance(block, BulletList):
        return _bullet_list_xml(block, relationships)
    if isinstance(block, Table):
        return _table_xml(block, relationships)
    if isinstance(block, CodeBlock):
        return _code_block_xml(block, relationships)
    if isinstance(block, Image):
        return _image_xml(block, image_index)
    if isinstance(block, PageBreak):
        return _page_break_xml()
    if isinstance(block, TableOfContents):
        return _table_of_contents_xml()
    raise PackageValidationError(f"bloco nao suportado: {type(block).__name__}")


def _media_entries(
    blocks: Sequence[Block], media: Mapping[str, bytes]
) -> tuple[tuple[str, str, bytes], ...]:
    referenced = [block.rel_id for block in blocks if isinstance(block, Image)]
    duplicated = {rel_id for rel_id in referenced if referenced.count(rel_id) > 1}
    if duplicated:
        raise PackageValidationError(f"Image.rel_id repetido: {sorted(duplicated)}")
    missing = [rel_id for rel_id in referenced if rel_id not in media]
    if missing:
        raise PackageValidationError(f"midia ausente para rel_id: {sorted(missing)}")
    unused = sorted(set(media) - set(referenced))
    if unused:
        raise PackageValidationError(f"midia sem bloco Image correspondente: {unused}")
    entries = []
    for rel_id in referenced:
        payload = media[rel_id]
        extension = image_extension(payload)
        entries.append((rel_id, f"{rel_id}.{extension}", payload))
    return tuple(entries)


def _document_xml(
    blocks: Sequence[Block], relationships: _RelationshipTable
) -> str:
    rendered = []
    image_index = 0
    for block in blocks:
        if isinstance(block, Image):
            image_index += 1
        rendered.append(_block_xml(block, relationships, image_index))
    body = "".join(rendered) + SECTION_PROPERTIES
    return (
        XML_DECLARATION
        + f"<w:document{WORD_NAMESPACES}>"
        + f"<w:body>{body}</w:body>"
        + "</w:document>"
    )


def build_package(
    blocks: Iterable[Block],
    properties: CoreProperties,
    out_path: str | Path,
    media: Mapping[str, bytes] | None = None,
) -> Path:
    materialized = tuple(blocks)
    entries = _media_entries(materialized, dict(media or {}))
    relationships = _RelationshipTable()
    for rel_id, filename, _payload in entries:
        relationships.image(rel_id, f"media/{filename}")
    document = _document_xml(materialized, relationships)
    extensions = sorted({filename.rsplit(".", 1)[1] for _r, filename, _p in entries})
    has_toc = any(isinstance(block, TableOfContents) for block in materialized)

    text_parts: list[tuple[str, str]] = [
        ("[Content_Types].xml", content_types_xml(extensions)),
        ("_rels/.rels", ROOT_RELS_XML),
        ("docProps/core.xml", core_xml(properties)),
        ("docProps/app.xml", app_xml(properties)),
        ("word/document.xml", document),
        ("word/_rels/document.xml.rels", relationships.document_rels_xml()),
        ("word/styles.xml", STYLES_XML),
        ("word/numbering.xml", NUMBERING_XML),
        ("word/settings.xml", settings_xml(has_toc)),
    ]
    binary_parts = [
        (f"{MEDIA_DIRECTORY}/{filename}", payload) for _r, filename, payload in entries
    ]

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in text_parts:
            _write_entry(archive, name, content.encode("utf-8"))
        for name, payload in binary_parts:
            _write_entry(archive, name, payload)
    return target


def _write_entry(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.create_system = 0
    info.external_attr = 0o600 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, payload)
