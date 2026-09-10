from __future__ import annotations

import json as stdjson
from datetime import datetime
from pathlib import Path
from typing import Any

from wiki_ai.ingestion.adapters import documents
from wiki_ai.ingestion.adapters.blocks import BlockBuilder
from wiki_ai.ingestion.source import BlockKind, SourceDocument, SourceKind

__all__ = ["adapt", "MALFORMED_JSON", "escape_pointer"]

MALFORMED_JSON = "malformed_json"


def escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return stdjson.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _documents(text: str) -> tuple[list[tuple[str, Any]], str]:
    stripped = text.strip()
    if not stripped:
        return [], "empty"
    try:
        payload = stdjson.loads(stripped)
    except stdjson.JSONDecodeError:
        found: list[tuple[str, Any]] = []
        for index, line in enumerate(text.splitlines()):
            chunk = line.strip()
            if not chunk:
                continue
            try:
                found.append((f"/{index}", stdjson.loads(chunk)))
            except stdjson.JSONDecodeError:
                return [], "malformed"
        return found, "jsonl"
    if isinstance(payload, list):
        return [(f"/{index}", item) for index, item in enumerate(payload)], "array"
    if isinstance(payload, dict):
        return [
            (f"/{escape_pointer(str(key))}", value) for key, value in payload.items()
        ], "object"
    return [("", payload)], "scalar"


def adapt(
    path: str | Path, payload: bytes, captured_at: datetime | None = None
) -> SourceDocument:
    target = Path(path)
    text = payload.decode("utf-8", errors="replace")
    source, metadata_diagnostics = documents.build_source(
        target,
        SourceKind.JSON,
        payload,
        {"source_type": SourceKind.JSON.value, "title": target.name},
        captured_at,
    )
    builder = BlockBuilder(source.id)
    builder.extend_diagnostics(metadata_diagnostics)

    entries, shape = _documents(text)
    if shape == "malformed":
        builder.fail(
            MALFORMED_JSON,
            f"{target.name} is neither a JSON document nor a JSON-lines stream",
            {"pointer": "/"},
        )
        return SourceDocument(source=source, blocks=(), diagnostics=builder.diagnostics)

    for pointer, value in entries:
        builder.add(
            BlockKind.OTHER,
            _render(value),
            {"pointer": pointer or "/", "json_pointer": pointer or "/", "shape": shape},
            attributes={"value_type": type(value).__name__, "shape": shape},
        )

    return SourceDocument(
        source=source, blocks=builder.blocks, diagnostics=builder.diagnostics
    )
