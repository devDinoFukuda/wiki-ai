from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from wiki_ai.ingestion.adapters import registry
from wiki_ai.ingestion.adapters.documents import version_hash
from wiki_ai.ingestion.outcome import Gap, StructuralFault
from wiki_ai.ingestion.source import (
    Block,
    Diagnostic,
    DiagnosticLevel,
    Source,
    SourceDocument,
    SourceKind,
    locator_diagnostics,
    sanitize_metadata,
)

__all__ = [
    "IngestStatus",
    "IngestedSource",
    "EPOCH",
    "version_hash",
    "ingest",
    "reingest",
    "ingest_many",
]

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_FAULTS: Mapping[str, StructuralFault] = {item.value: item for item in StructuralFault}
_GAPS: Mapping[str, Gap] = {item.value: item for item in Gap}


class IngestStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class IngestedSource:
    document: SourceDocument
    status: IngestStatus
    uri: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def source(self) -> Source:
        return self.document.source

    @property
    def source_id(self) -> str:
        return self.document.source.id

    @property
    def kind(self) -> SourceKind:
        return self.document.source.kind

    @property
    def version_hash(self) -> str:
        return self.document.source.version_hash

    @property
    def blocks(self) -> tuple[Block, ...]:
        return self.document.blocks

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return self.document.diagnostics

    @property
    def gaps(self) -> tuple[Diagnostic, ...]:
        return tuple(
            d for d in self.document.diagnostics if d.level is DiagnosticLevel.WARNING
        )

    @property
    def faults(self) -> tuple[StructuralFault, ...]:
        found: list[StructuralFault] = []
        for item in self.document.diagnostics:
            if item.level is not DiagnosticLevel.ERROR:
                continue
            fault = _FAULTS.get(item.code)
            if fault is not None and fault not in found:
                found.append(fault)
        return tuple(found)

    @property
    def open_gaps(self) -> tuple[Gap, ...]:
        found: list[Gap] = []
        for item in self.document.diagnostics:
            if item.level is DiagnosticLevel.INFO:
                continue
            gap = _GAPS.get(item.code)
            if gap is not None and gap not in found:
                found.append(gap)
        return tuple(found)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "uri": self.uri,
            "metadata": dict(self.metadata),
            **self.document.to_dict(),
        }


def _status(document: SourceDocument) -> IngestStatus:
    if document.errors:
        return IngestStatus.FAILED
    if any(d.level is DiagnosticLevel.WARNING for d in document.diagnostics):
        return IngestStatus.PARTIAL
    return IngestStatus.COMPLETE


def ingest(
    path: str | Path,
    metadata: Mapping[str, Any] | None = None,
    captured_at: datetime | None = None,
) -> IngestedSource:
    target = Path(path)
    document = registry.adapt(target, captured_at or EPOCH)
    clean, extra, found = sanitize_metadata(metadata or {})
    diagnostics = tuple(document.diagnostics) + tuple(found)
    diagnostics += locator_diagnostics(document.source.kind, document.blocks)
    document = SourceDocument(
        source=document.source, blocks=document.blocks, diagnostics=diagnostics
    )
    return IngestedSource(
        document=document,
        status=_status(document),
        uri=document.source.uri,
        metadata={**clean, "unmapped": extra} if extra else dict(clean),
    )


def reingest(
    path: str | Path,
    metadata: Mapping[str, Any] | None = None,
    captured_at: datetime | None = None,
) -> IngestedSource:
    return ingest(path, metadata, captured_at)


def ingest_many(
    root: str | Path,
    metadata: Mapping[str, Any] | None = None,
    captured_at: datetime | None = None,
    suffixes: Iterable[str] | None = None,
) -> tuple[IngestedSource, ...]:
    base = Path(root)
    if base.is_file():
        return (ingest(base, metadata, captured_at),)
    allowed = {s.lower() for s in suffixes} if suffixes else None
    found: list[IngestedSource] = []
    for entry in sorted(p for p in base.rglob("*") if p.is_file()):
        if allowed is not None and entry.suffix.lower() not in allowed:
            continue
        found.append(ingest(entry, metadata, captured_at))
    return tuple(found)
