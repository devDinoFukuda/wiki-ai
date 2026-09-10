from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from xml.etree.ElementTree import Element

from wiki_ai.ingestion.adapters import documents
from wiki_ai.ingestion.adapters.blocks import BlockBuilder
from wiki_ai.ingestion.adapters.ooxml import XML_REJECTED, collapse
from wiki_ai.ingestion.adapters.xmlsafe import XmlRejected, local_name, parse_defused
from wiki_ai.ingestion.source import BlockKind, SourceDocument, SourceKind

__all__ = ["adapt", "leaves"]


def _child_index(parent: Element, child: Element, name: str) -> int:
    position = 0
    for node in parent:
        if local_name(node.tag) == name:
            position += 1
            if node is child:
                return position
    return position or 1


def leaves(root: Element) -> Iterator[tuple[str, Element]]:
    stack: list[tuple[str, Element]] = [(f"/{local_name(root.tag)}", root)]
    while stack:
        xpath, node = stack.pop()
        found = list(node)
        if not found:
            yield xpath, node
            continue
        for child in reversed(found):
            name = local_name(child.tag)
            index = _child_index(node, child, name)
            stack.append((f"{xpath}/{name}[{index}]", child))


def adapt(
    path: str | Path, payload: bytes, captured_at: datetime | None = None
) -> SourceDocument:
    target = Path(path)
    source, metadata_diagnostics = documents.build_source(
        target,
        SourceKind.XML,
        payload,
        {"source_type": SourceKind.XML.value, "title": target.name},
        captured_at,
    )
    builder = BlockBuilder(source.id)
    builder.extend_diagnostics(metadata_diagnostics)

    try:
        root = parse_defused(payload)
    except XmlRejected as exc:
        builder.fail(XML_REJECTED, f"{target.name}: {exc}", {"xpath": "/"})
        return SourceDocument(source=source, blocks=(), diagnostics=builder.diagnostics)

    for xpath, node in leaves(root):
        attributes: dict[str, Any] = {
            local_name(key): value for key, value in node.attrib.items()
        }
        text = collapse(node.text or "")
        if not text and not attributes:
            continue
        builder.add(
            BlockKind.OTHER,
            text,
            {"xpath": xpath, "tag": local_name(node.tag)},
            attributes={"attributes": attributes},
        )

    return SourceDocument(
        source=source, blocks=builder.blocks, diagnostics=builder.diagnostics
    )
