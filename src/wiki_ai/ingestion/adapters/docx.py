from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from wiki_ai.ingestion.adapters import documents, ooxml
from wiki_ai.ingestion.adapters.blocks import BlockBuilder, image_gap
from wiki_ai.ingestion.adapters.ooxml import (
    CORRUPT_ARCHIVE,
    OoxmlPackage,
    PackageUnreadable,
    XML_REJECTED,
    attribute,
    children,
    collapse,
    descendants,
    find_child,
    relationships,
)
from wiki_ai.ingestion.adapters.xmlsafe import XmlRejected, local_name
from wiki_ai.ingestion.source import BlockKind, SourceDocument, SourceKind

__all__ = ["adapt", "DOCUMENT_PART", "HEADING_STYLE"]

DOCUMENT_PART = "word/document.xml"
_CORE_PART = "docProps/core.xml"
_APP_PART = "docProps/app.xml"
_COMMENTS_PART = "word/comments.xml"
_FOOTNOTES_PART = "word/footnotes.xml"
_ENDNOTES_PART = "word/endnotes.xml"
_MEDIA_PREFIX = "word/media/"

HEADING_STYLE = re.compile(r"^heading\s*(\d+)$")
_TITLE_STYLES = {"title": 0, "subtitle": 1}
_SEPARATOR_NOTES = frozenset({"separator", "continuationSeparator"})
_CORE_FIELDS = ("creator", "subject", "description", "lastModifiedBy", "category")


def _style(paragraph: Any) -> str:
    props = find_child(paragraph, "pPr")
    if props is None:
        return ""
    style = find_child(props, "pStyle")
    return attribute(style, "val") if style is not None else ""


def _numbering(paragraph: Any) -> tuple[str, str] | None:
    props = find_child(paragraph, "pPr")
    if props is None:
        return None
    num = find_child(props, "numPr")
    if num is None:
        return None
    ilvl = find_child(num, "ilvl")
    num_id = find_child(num, "numId")
    return (
        attribute(num_id, "val") if num_id is not None else "",
        attribute(ilvl, "val") if ilvl is not None else "0",
    )


def _paragraph_text(paragraph: Any) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        name = local_name(node.tag)
        if name == "t":
            parts.append(node.text or "")
        elif name == "tab":
            parts.append("\t")
    return collapse("".join(parts))


def _heading_level(style: str) -> int | None:
    if not style:
        return None
    lowered = style.strip().lower()
    if lowered in _TITLE_STYLES:
        return _TITLE_STYLES[lowered]
    match = HEADING_STYLE.match(lowered)
    return int(match.group(1)) if match else None


def _hyperlinks(paragraph: Any, rels: Mapping[str, tuple[str, str]]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for node in descendants(paragraph, "hyperlink"):
        rel_id = attribute(node, "id")
        anchor = attribute(node, "anchor")
        target = rels.get(rel_id, ("", ""))[1] if rel_id else ""
        found.append(
            {"text": _paragraph_text(node), "target": target or anchor, "rel_id": rel_id}
        )
    return found


def _images(
    paragraph: Any, rels: Mapping[str, tuple[str, str]], package: OoxmlPackage
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for blip in descendants(paragraph, "blip"):
        rel_id = attribute(blip, "embed") or attribute(blip, "link")
        target = rels.get(rel_id, ("", ""))[1]
        name = target.rsplit("/", 1)[-1]
        part = _MEDIA_PREFIX + name if name else ""
        found.append(
            {
                "rel_id": rel_id,
                "name": name,
                "part": part,
                "bytes": package.size(part) if part else 0,
            }
        )
    return found


def _heading_path(stack: list[tuple[int, str]]) -> list[str]:
    return [text for _, text in stack]


def _push_heading(stack: list[tuple[int, str]], level: int, text: str) -> None:
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, text))


def _cell_text(cell: Any) -> str:
    return collapse(
        " ".join(
            _paragraph_text(p) for p in children(cell, "p") if _paragraph_text(p)
        )
    )


def _walk_body(
    body: Any,
    part: str,
    builder: BlockBuilder,
    package: OoxmlPackage,
    rels: Mapping[str, tuple[str, str]],
) -> None:
    stack: list[tuple[int, str]] = []
    counter = {"paragraph": 0, "table": 0}
    for node in body:
        name = local_name(node.tag)
        if name == "p":
            _emit_paragraph(node, part, builder, package, rels, stack, counter)
        elif name == "tbl":
            _emit_table(node, part, builder, stack, counter)


def _emit_paragraph(
    paragraph: Any,
    part: str,
    builder: BlockBuilder,
    package: OoxmlPackage,
    rels: Mapping[str, tuple[str, str]],
    stack: list[tuple[int, str]],
    counter: dict[str, int],
) -> None:
    counter["paragraph"] += 1
    index = counter["paragraph"]
    text = _paragraph_text(paragraph)
    style = _style(paragraph)
    level = _heading_level(style)
    numbering = _numbering(paragraph)

    if level is not None and text:
        _push_heading(stack, level, text)
        builder.add(
            BlockKind.HEADING,
            text,
            {
                "part": part,
                "paragraph": index,
                "heading_path": _heading_path(stack),
                "section": " / ".join(_heading_path(stack)),
                "block": f"paragraph:{index}",
            },
            attributes={"style": style, "level": level},
        )
        return

    path = _heading_path(stack)
    locator: dict[str, Any] = {
        "part": part,
        "paragraph": index,
        "heading_path": path,
        "section": " / ".join(path) or part,
        "block": f"paragraph:{index}",
    }

    if text:
        attributes: dict[str, Any] = {"style": style}
        links = _hyperlinks(paragraph, rels)
        if links:
            attributes["hyperlinks"] = links
        kind = BlockKind.PARAGRAPH
        if numbering is not None:
            kind = BlockKind.LIST_ITEM
            attributes["num_id"] = numbering[0]
            attributes["ilvl"] = int(numbering[1] or 0)
        builder.add(kind, text, locator, attributes=attributes)

    for image in _images(paragraph, rels, package):
        rel_id = image["rel_id"]
        image_locator = dict(locator)
        image_locator["block"] = f"paragraph:{index}:image:{rel_id}"
        image_locator["image"] = rel_id
        builder.add(
            BlockKind.IMAGE,
            image["name"] or rel_id,
            image_locator,
            attributes={
                "gap": True,
                "rel_id": rel_id,
                "name": image["name"],
                "bytes": image["bytes"],
            },
        )
        image_gap(builder, f"{part} paragraph {index}", image_locator)


def _emit_table(
    table: Any,
    part: str,
    builder: BlockBuilder,
    stack: list[tuple[int, str]],
    counter: dict[str, int],
) -> None:
    counter["table"] += 1
    table_index = counter["table"]
    rows = children(table, "tr")
    grid = [[_cell_text(cell) for cell in children(row, "tc")] for row in rows]
    path = _heading_path(stack)
    section = " / ".join(path) or part
    table_block = builder.add(
        BlockKind.TABLE,
        "\n".join("\t".join(row) for row in grid),
        {
            "part": part,
            "table": table_index,
            "heading_path": path,
            "section": section,
            "block": f"table:{table_index}",
        },
        attributes={
            "rows": len(grid),
            "columns": max((len(row) for row in grid), default=0),
            "header_row": grid[0] if grid else [],
        },
    )
    for row_index, row in enumerate(rows, start=1):
        for col_index, cell in enumerate(children(row, "tc"), start=1):
            builder.add(
                BlockKind.CELL,
                _cell_text(cell),
                {
                    "part": part,
                    "table": table_index,
                    "row": row_index,
                    "col": col_index,
                    "heading_path": path,
                    "section": section,
                    "block": f"table:{table_index}:r{row_index}c{col_index}",
                },
                parent_id=table_block.id,
                attributes={"header": row_index == 1},
            )


def _note_parts(package: OoxmlPackage) -> Iterable[tuple[str, str]]:
    for part, label in ((_FOOTNOTES_PART, "footnote"), (_ENDNOTES_PART, "endnote")):
        if package.has(part):
            yield part, label


def _emit_notes(package: OoxmlPackage, builder: BlockBuilder) -> None:
    for part, label in _note_parts(package):
        try:
            root = package.xml(part)
        except XmlRejected as exc:
            builder.fail(XML_REJECTED, f"{part}: {exc}", {"part": label})
            continue
        if root is None:
            continue
        for index, entry in enumerate(children(root, label), start=1):
            if attribute(entry, "type") in _SEPARATOR_NOTES:
                continue
            text = collapse(" ".join(_paragraph_text(p) for p in children(entry, "p")))
            if not text:
                continue
            note_id = attribute(entry, "id") or str(index)
            builder.add(
                BlockKind.PARAGRAPH,
                text,
                {
                    "part": label,
                    "paragraph": index,
                    "section": label,
                    "block": f"{label}:{note_id}",
                    "note_id": note_id,
                },
                attributes={"annotation": label},
            )


def _emit_comments(package: OoxmlPackage, builder: BlockBuilder) -> None:
    if not package.has(_COMMENTS_PART):
        return
    try:
        root = package.xml(_COMMENTS_PART)
    except XmlRejected as exc:
        builder.fail(XML_REJECTED, f"{_COMMENTS_PART}: {exc}", {"part": "comments"})
        return
    if root is None:
        return
    for index, comment in enumerate(children(root, "comment"), start=1):
        text = collapse(" ".join(_paragraph_text(p) for p in children(comment, "p")))
        if not text:
            continue
        comment_id = attribute(comment, "id") or str(index)
        builder.add(
            BlockKind.OTHER,
            text,
            {
                "part": "comments",
                "paragraph": index,
                "section": "comments",
                "block": f"comment:{comment_id}",
                "comment_id": comment_id,
            },
            attributes={
                "author": attribute(comment, "author"),
                "date": attribute(comment, "date"),
                "initials": attribute(comment, "initials"),
            },
        )


def _header_footer_parts(package: OoxmlPackage) -> list[str]:
    return sorted(
        name
        for name in package.names()
        if re.fullmatch(r"word/(header|footer)\d+\.xml", name)
    )


def _properties(package: OoxmlPackage) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in (_CORE_PART, _APP_PART):
        if not package.has(part):
            continue
        try:
            root = package.xml(part)
        except XmlRejected:
            continue
        if root is None:
            continue
        for node in root.iter():
            if node is root or not (node.text or "").strip():
                continue
            found.setdefault(local_name(node.tag), collapse(node.text or ""))
    return found


def _metadata(properties: Mapping[str, str]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"source_type": SourceKind.DOCX.value}
    if properties.get("title"):
        metadata["title"] = properties["title"]
    for field in _CORE_FIELDS:
        if properties.get(field):
            metadata[field] = properties[field]
    return metadata


def adapt(
    path: str | Path, payload: bytes, captured_at: datetime | None = None
) -> SourceDocument:
    target = Path(path)
    try:
        package = ooxml.open_package(payload)
    except PackageUnreadable as exc:
        return documents.empty_document(
            target,
            SourceKind.DOCX,
            payload,
            CORRUPT_ARCHIVE,
            f"{target.name} is not a readable OOXML package: {exc.reason}",
            captured_at=captured_at,
        )
    try:
        return _adapt_package(target, payload, package, captured_at)
    finally:
        package.close()


def _adapt_package(
    target: Path, payload: bytes, package: OoxmlPackage, captured_at: datetime | None
) -> SourceDocument:
    properties = _properties(package)
    source, metadata_diagnostics = documents.build_source(
        target, SourceKind.DOCX, payload, _metadata(properties), captured_at
    )
    builder = BlockBuilder(source.id)
    builder.extend_diagnostics(metadata_diagnostics)
    if properties:
        builder.add(
            BlockKind.OTHER,
            "\n".join(f"{key}: {value}" for key, value in sorted(properties.items())),
            {
                "part": "docProps",
                "paragraph": 0,
                "section": "document_properties",
                "block": "document_properties",
            },
            attributes={"properties": dict(sorted(properties.items()))},
        )

    try:
        document = package.xml(DOCUMENT_PART)
    except XmlRejected as exc:
        builder.fail(XML_REJECTED, f"{DOCUMENT_PART}: {exc}", {"part": "document"})
        document = None
    if document is None and not builder.diagnostics:
        builder.fail(
            CORRUPT_ARCHIVE, f"{target.name} has no {DOCUMENT_PART}", {"part": "document"}
        )
    if document is not None:
        body = find_child(document, "body")
        if body is not None:
            _walk_body(
                body, "document", builder, package, relationships(package, DOCUMENT_PART)
            )

    for part in _header_footer_parts(package):
        label = part.rsplit("/", 1)[-1].removesuffix(".xml")
        try:
            root = package.xml(part)
        except XmlRejected as exc:
            builder.fail(XML_REJECTED, f"{part}: {exc}", {"part": label})
            continue
        if root is not None:
            _walk_body(root, label, builder, package, relationships(package, part))

    _emit_notes(package, builder)
    _emit_comments(package, builder)

    return SourceDocument(
        source=source, blocks=builder.blocks, diagnostics=builder.diagnostics
    )
