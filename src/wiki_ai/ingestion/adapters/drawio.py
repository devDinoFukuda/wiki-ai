from __future__ import annotations

import base64
import binascii
import re
import urllib.parse
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any

from wiki_ai.ingestion.adapters import documents
from wiki_ai.ingestion.adapters.blocks import BlockBuilder
from wiki_ai.ingestion.adapters.ooxml import XML_REJECTED, attribute, collapse, descendants
from wiki_ai.ingestion.adapters.xmlsafe import XmlRejected, local_name, parse_defused
from wiki_ai.ingestion.source import BlockKind, SourceDocument, SourceKind

__all__ = [
    "adapt",
    "EMBEDDED_PAYLOAD",
    "MALFORMED_DIAGRAM",
    "parse_style",
    "decode_diagram",
]

EMBEDDED_PAYLOAD = "embedded_payload_not_supported"
MALFORMED_DIAGRAM = "malformed_diagram"

_HTML_TAG = re.compile(r"<[^>]+>")
_ENTITY = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
}
_BREAK = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)


def _plain(label: str) -> str:
    text = _BREAK.sub(" ", label or "")
    text = _HTML_TAG.sub("", text)
    for entity, replacement in _ENTITY.items():
        text = text.replace(entity, replacement)
    return collapse(text)


def parse_style(style: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in (style or "").split(";"):
        chunk = part.strip()
        if not chunk:
            continue
        key, separator, value = chunk.partition("=")
        if separator:
            found[key.strip()] = value.strip()
        else:
            found.setdefault("shape", chunk)
    return found


def decode_diagram(payload: str) -> str | None:
    compact = re.sub(r"\s+", "", payload or "")
    if not compact:
        return None
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        inflated = zlib.decompress(raw, -zlib.MAX_WBITS)
    except zlib.error:
        try:
            inflated = zlib.decompress(raw)
        except zlib.error:
            return None
    try:
        return urllib.parse.unquote(inflated.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _geometry(cell: Any) -> dict[str, float]:
    for node in cell:
        if local_name(node.tag) != "mxGeometry":
            continue
        found: dict[str, float] = {}
        for key, target in (("x", "x"), ("y", "y"), ("width", "width"), ("height", "height")):
            raw = attribute(node, key)
            if raw:
                try:
                    found[target] = float(raw)
                except ValueError:
                    continue
        return found
    return {}


def _model_root(diagram: Any) -> Any | None:
    text = (diagram.text or "").strip()
    for child in diagram:
        if local_name(child.tag) == "mxGraphModel":
            return child
    if not text:
        return None
    decoded = decode_diagram(text)
    if decoded is None:
        return None
    try:
        return parse_defused(decoded)
    except XmlRejected:
        return None


def _cells(model: Any) -> list[Any]:
    return [node for node in model.iter() if local_name(node.tag) == "mxCell"]


def _layers(cells: list[Any]) -> set[str]:
    found: set[str] = set()
    for cell in cells:
        if attribute(cell, "parent") == "0" or not attribute(cell, "parent"):
            found.add(attribute(cell, "id"))
    return found


def _label(cell: Any, objects: dict[str, Any]) -> str:
    holder = objects.get(attribute(cell, "id"))
    if holder is not None:
        return _plain(attribute(holder, "label") or attribute(holder, "value"))
    return _plain(attribute(cell, "value"))


def _objects(model: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for node in model.iter():
        if local_name(node.tag) not in {"object", "UserObject"}:
            continue
        for child in node:
            if local_name(child.tag) == "mxCell":
                found[attribute(child, "id") or attribute(node, "id")] = node
                if not attribute(child, "id"):
                    child.set("id", attribute(node, "id"))
    return found


def _emit_model(
    model: Any, builder: BlockBuilder, diagram_name: str, page: int
) -> None:
    cells = _cells(model)
    objects = _objects(model)
    layers = _layers(cells)
    vertices = {
        attribute(cell, "id") for cell in cells if attribute(cell, "vertex") == "1"
    }
    parents = {attribute(cell, "parent") for cell in cells}
    for cell in cells:
        cell_id = attribute(cell, "id")
        if not cell_id or cell_id in layers:
            continue
        style = attribute(cell, "style")
        parsed = parse_style(style)
        parent = attribute(cell, "parent")
        if attribute(cell, "vertex") == "1":
            attributes: dict[str, Any] = {
                "style": style,
                "style_parsed": parsed,
                "geometry": _geometry(cell),
                "parent": parent,
                "container": cell_id in parents,
                "in_container": parent not in layers and parent in vertices,
            }
            builder.add(
                BlockKind.NODE,
                _label(cell, objects),
                {
                    "diagram": diagram_name,
                    "page": page,
                    "node": cell_id,
                },
                attributes=attributes,
            )
        elif attribute(cell, "edge") == "1":
            builder.add(
                BlockKind.EDGE,
                _label(cell, objects),
                {
                    "diagram": diagram_name,
                    "page": page,
                    "node": cell_id,
                    "edge": cell_id,
                },
                attributes={
                    "style": style,
                    "style_parsed": parsed,
                    "source": attribute(cell, "source"),
                    "target": attribute(cell, "target"),
                    "parent": parent,
                },
            )


def _embedded(target: Path, payload: bytes) -> bool:
    name = target.name.lower()
    if name.endswith(".drawio.png") or name.endswith(".drawio.svg"):
        return True
    return payload.startswith(b"\x89PNG\r\n\x1a\n")


def adapt(
    path: str | Path, payload: bytes, captured_at: datetime | None = None
) -> SourceDocument:
    target = Path(path)
    source, metadata_diagnostics = documents.build_source(
        target,
        SourceKind.DRAWIO,
        payload,
        {"source_type": SourceKind.DRAWIO.value, "title": target.name},
        captured_at,
    )
    builder = BlockBuilder(source.id)
    builder.extend_diagnostics(metadata_diagnostics)

    if _embedded(target, payload):
        builder.warn(
            EMBEDDED_PAYLOAD,
            f"{target.name} carries the diagram inside an image container; "
            "the structural adapter reads only mxfile XML",
            {"diagram": target.name, "page": 1, "node": "*"},
        )
        return SourceDocument(
            source=source, blocks=(), diagnostics=builder.diagnostics
        )

    try:
        root = parse_defused(payload)
    except XmlRejected as exc:
        builder.fail(
            XML_REJECTED,
            f"{target.name}: {exc}",
            {"diagram": target.name, "page": 1, "node": "*"},
        )
        return SourceDocument(
            source=source, blocks=(), diagnostics=builder.diagnostics
        )

    tag = local_name(root.tag)
    if tag == "mxGraphModel":
        _emit_model(root, builder, target.stem, 1)
        return SourceDocument(
            source=source, blocks=builder.blocks, diagnostics=builder.diagnostics
        )

    diagrams = [node for node in descendants(root, "diagram")]
    if not diagrams:
        builder.fail(
            MALFORMED_DIAGRAM,
            f"{target.name} declares no mxfile diagram page",
            {"diagram": target.name, "page": 1, "node": "*"},
        )
        return SourceDocument(
            source=source, blocks=(), diagnostics=builder.diagnostics
        )

    for page, diagram in enumerate(diagrams, start=1):
        name = attribute(diagram, "name") or f"page-{page}"
        model = _model_root(diagram)
        if model is None:
            builder.fail(
                MALFORMED_DIAGRAM,
                f"page {page} of {target.name} carries no readable mxGraphModel",
                {"diagram": name, "page": page, "node": "*"},
            )
            continue
        _emit_model(model, builder, name, page)

    return SourceDocument(
        source=source, blocks=builder.blocks, diagnostics=builder.diagnostics
    )
