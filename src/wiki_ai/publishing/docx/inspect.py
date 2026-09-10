from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .errors import PackageValidationError
from .parts import (
    CONTENT_TYPE_DOCUMENT,
    CONTENT_TYPES_NAMESPACE,
    PACKAGE_RELATIONSHIPS,
    RELATIONSHIP_BASE,
)
from .properties import CoreProperties, parse_core_xml
from .xmltools import local_name, parse_xml

WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REQUIRED_PARTS = (
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
    "word/_rels/document.xml.rels",
    "docProps/core.xml",
)
CONTENT_TYPES_PART = "[Content_Types].xml"
DOCUMENT_PART = "word/document.xml"
DOCUMENT_RELS_PART = "word/_rels/document.xml.rels"
CORE_PART = "docProps/core.xml"
MEDIA_PREFIX = "word/media/"
HEADING_STYLE_PREFIX = "Heading"

_W = f"{{{WORD_NAMESPACE}}}"


@dataclass(frozen=True, slots=True)
class PackageSummary:
    parts: tuple[str, ...]
    paragraph_count: int
    heading_count: int
    table_count: int
    list_item_count: int
    image_count: int
    hyperlink_targets: tuple[str, ...]
    media_parts: tuple[str, ...]
    text: str
    properties: CoreProperties


def _open_archive(path: Path) -> zipfile.ZipFile:
    if not path.exists():
        raise PackageValidationError(f"pacote inexistente: {path}")
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise PackageValidationError(f"{path}: nao e um zip valido ({exc})") from exc


def _check_required_parts(names: tuple[str, ...]) -> None:
    missing = [part for part in REQUIRED_PARTS if part not in names]
    if missing:
        raise PackageValidationError(f"partes obrigatorias ausentes: {missing}")


def _content_type_maps(root: ElementTree.Element) -> tuple[dict[str, str], dict[str, str]]:
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for child in root:
        tag = local_name(child.tag)
        if tag == "Default":
            defaults[child.get("Extension", "").lower()] = child.get("ContentType", "")
        elif tag == "Override":
            overrides[child.get("PartName", "")] = child.get("ContentType", "")
    return defaults, overrides


def _check_content_types(root: ElementTree.Element, names: tuple[str, ...]) -> None:
    if root.tag != f"{{{CONTENT_TYPES_NAMESPACE}}}Types":
        raise PackageValidationError("[Content_Types].xml com raiz inesperada")
    defaults, overrides = _content_type_maps(root)
    for part_name in overrides:
        if part_name.lstrip("/") not in names:
            raise PackageValidationError(f"Override sem parte correspondente: {part_name}")
    for name in names:
        if name == CONTENT_TYPES_PART:
            continue
        if f"/{name}" in overrides:
            continue
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if extension not in defaults:
            raise PackageValidationError(f"parte sem content type declarado: {name}")
    if overrides.get(f"/{DOCUMENT_PART}") != CONTENT_TYPE_DOCUMENT:
        raise PackageValidationError("word/document.xml sem content type de documento principal")


def _check_document_root(root: ElementTree.Element) -> ElementTree.Element:
    if root.tag != f"{_W}document":
        raise PackageValidationError("word/document.xml com raiz inesperada")
    body = root.find(f"{_W}body")
    if body is None:
        raise PackageValidationError("word/document.xml sem w:body")
    return body


def _paragraph_style(paragraph: ElementTree.Element) -> str:
    properties = paragraph.find(f"{_W}pPr")
    if properties is None:
        return ""
    style = properties.find(f"{_W}pStyle")
    if style is None:
        return ""
    return style.get(f"{_W}val", "")


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    fragments = []
    for element in paragraph.iter():
        tag = element.tag
        if tag == f"{_W}t":
            fragments.append(element.text or "")
        elif tag == f"{_W}tab":
            fragments.append("\t")
    return "".join(fragments)


def _has_numbering(paragraph: ElementTree.Element) -> bool:
    properties = paragraph.find(f"{_W}pPr")
    return properties is not None and properties.find(f"{_W}numPr") is not None


def _summarize_body(body: ElementTree.Element) -> tuple[int, int, int, int, int, str]:
    paragraphs = 0
    headings = 0
    list_items = 0
    lines = []
    for paragraph in body.iter(f"{_W}p"):
        paragraphs += 1
        style = _paragraph_style(paragraph)
        if style.startswith(HEADING_STYLE_PREFIX):
            headings += 1
        if _has_numbering(paragraph):
            list_items += 1
        lines.append(_paragraph_text(paragraph))
    tables = sum(1 for _ in body.iter(f"{_W}tbl"))
    images = sum(1 for _ in body.iter(f"{_W}drawing"))
    return paragraphs, headings, tables, list_items, images, "\n".join(lines)


def _hyperlink_targets(root: ElementTree.Element) -> tuple[str, ...]:
    if root.tag != f"{{{PACKAGE_RELATIONSHIPS}}}Relationships":
        raise PackageValidationError("word/_rels/document.xml.rels com raiz inesperada")
    hyperlink_type = f"{RELATIONSHIP_BASE}/hyperlink"
    return tuple(
        child.get("Target", "")
        for child in root
        if child.get("Type") == hyperlink_type
    )


def read_package(path: str | Path) -> PackageSummary:
    target = Path(path)
    with _open_archive(target) as archive:
        names = tuple(archive.namelist())
        _check_required_parts(names)
        _check_content_types(parse_xml(CONTENT_TYPES_PART, archive.read(CONTENT_TYPES_PART)), names)
        for name in names:
            if name.endswith(".xml") or name.endswith(".rels"):
                parse_xml(name, archive.read(name))
        body = _check_document_root(parse_xml(DOCUMENT_PART, archive.read(DOCUMENT_PART)))
        paragraphs, headings, tables, list_items, images, text = _summarize_body(body)
        hyperlinks = _hyperlink_targets(
            parse_xml(DOCUMENT_RELS_PART, archive.read(DOCUMENT_RELS_PART))
        )
        properties = parse_core_xml(archive.read(CORE_PART))
    return PackageSummary(
        parts=tuple(sorted(names)),
        paragraph_count=paragraphs,
        heading_count=headings,
        table_count=tables,
        list_item_count=list_items,
        image_count=images,
        hyperlink_targets=hyperlinks,
        media_parts=tuple(sorted(n for n in names if n.startswith(MEDIA_PREFIX))),
        text=text,
        properties=properties,
    )
