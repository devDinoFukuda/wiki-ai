from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from wiki_ai.ingestion.source import (
    Diagnostic,
    DiagnosticLevel,
    Source,
    SourceDocument,
    SourceKind,
    content_digest,
    make_source,
    sanitize_metadata,
)

__all__ = ["version_hash", "build_source", "empty_document"]


def version_hash(payload: bytes) -> str:
    return content_digest(payload.hex())


def build_source(
    path: str | Path,
    kind: SourceKind,
    payload: bytes,
    metadata: Mapping[str, Any] | None = None,
    captured_at: datetime | None = None,
) -> tuple[Source, tuple[Diagnostic, ...]]:
    clean, _, found = sanitize_metadata(metadata or {})
    return (
        make_source(
            kind=kind,
            uri=Path(path).resolve().as_uri(),
            version_hash=version_hash(payload),
            captured_at=captured_at or datetime.now(timezone.utc),
            metadata=clean,
        ),
        found,
    )


def empty_document(
    path: str | Path,
    kind: SourceKind,
    payload: bytes,
    code: str,
    message: str,
    level: DiagnosticLevel = DiagnosticLevel.ERROR,
    captured_at: datetime | None = None,
) -> SourceDocument:
    source, found = build_source(path, kind, payload, {}, captured_at)
    return SourceDocument(
        source=source,
        blocks=(),
        diagnostics=found
        + (
            Diagnostic(
                level=level, code=code, message=message, locator={"uri": source.uri}
            ),
        ),
    )
