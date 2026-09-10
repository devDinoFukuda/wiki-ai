from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from wiki_ai.knowledge.model import SourceVersion
from wiki_ai.knowledge.repository import KnowledgeRepository

from wiki_ai.ingestion import pipeline
from wiki_ai.ingestion.outcome import (
    BLOCKING_FAULTS,
    Gap,
    IngestionStatus,
    PARTIAL_GAPS,
    SemanticFault,
    StructuralFault,
)
from wiki_ai.ingestion.pipeline import IngestedSource, IngestStatus
from wiki_ai.ingestion.semantic import (
    AUTHOR,
    SemanticInvestigator,
    SemanticOutcome,
    SessionProvider,
)
from wiki_ai.ingestion.source import DiagnosticLevel

__all__ = [
    "PROVIDER_UNAVAILABLE",
    "VERSION_MISMATCH",
    "IngestionStatus",
    "IngestionResult",
    "IngestionEngine",
    "derive_status",
]

PROVIDER_UNAVAILABLE = SemanticFault.PROVIDER_UNAVAILABLE.value
VERSION_MISMATCH = SemanticFault.VERSION_HASH_MISMATCH.value

_NO_REASON = ""


@dataclass(frozen=True)
class IngestionResult:
    source_id: str
    version_hash: str
    blocks: int
    entities_written: int
    status: IngestionStatus = IngestionStatus.COMPLETE
    reason: str = _NO_REASON
    diagnostics: tuple[str, ...] = ()
    provider_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "version_hash": self.version_hash,
            "blocks": self.blocks,
            "entities_written": self.entities_written,
            "status": self.status.value,
            "reason": self.reason,
            "diagnostics": list(self.diagnostics),
            "provider_used": self.provider_used,
        }


def derive_status(
    faults: Sequence[StructuralFault],
    gaps: Sequence[Gap],
    provider_present: bool,
    semantic_faults: Sequence[SemanticFault] = (),
) -> tuple[IngestionStatus, str]:
    for fault in faults:
        if fault in BLOCKING_FAULTS:
            return IngestionStatus.BLOCKED, fault.value
    if faults:
        return IngestionStatus.FAILED, faults[0].value
    if not provider_present:
        return (
            IngestionStatus.STRUCTURAL_ONLY,
            SemanticFault.PROVIDER_UNAVAILABLE.value,
        )
    for fault in semantic_faults:
        if fault is SemanticFault.PROVIDER_RUN_ABORTED:
            return IngestionStatus.FAILED, fault.value
        return IngestionStatus.PARTIAL, fault.value
    for gap in gaps:
        if gap in PARTIAL_GAPS:
            return IngestionStatus.PARTIAL, gap.value
    return IngestionStatus.COMPLETE, _NO_REASON


def _structural_diagnostics(ingested: IngestedSource) -> tuple[str, ...]:
    found: list[str] = []
    for item in ingested.diagnostics:
        if item.level is DiagnosticLevel.INFO:
            continue
        found.append(f"{item.level.value}:{item.code}")
    if ingested.status is IngestStatus.PARTIAL:
        found.append("source_partially_interpreted")
    if ingested.status is IngestStatus.FAILED:
        found.append("source_not_interpreted")
    return tuple(dict.fromkeys(found))


class IngestionEngine:
    def __init__(
        self,
        investigator: SemanticInvestigator | None = None,
        ingest: Callable[..., IngestedSource] | None = None,
    ) -> None:
        self._investigator = investigator
        self._ingest = ingest or pipeline.ingest

    def run(
        self,
        source_path: Path,
        version_hash: str,
        knowledge: KnowledgeRepository,
        namespace: str,
        metadata: Mapping[str, Any] | None = None,
        *,
        provider: SessionProvider | None = None,
    ) -> IngestionResult:
        try:
            ingested = self._ingest(source_path, metadata or {})
        except OSError:
            return IngestionResult(
                source_id=str(Path(source_path)),
                version_hash=str(version_hash or ""),
                blocks=0,
                entities_written=0,
                status=IngestionStatus.FAILED,
                reason=StructuralFault.UNREADABLE_SOURCE.value,
                diagnostics=(f"error:{StructuralFault.UNREADABLE_SOURCE.value}",),
                provider_used=False,
            )
        diagnostics = list(_structural_diagnostics(ingested))
        semantic_faults: list[SemanticFault] = []
        declared = str(version_hash or "").strip()
        if declared and declared != ingested.version_hash:
            diagnostics.append(VERSION_MISMATCH)
        faults = ingested.faults
        self._register(ingested, knowledge, namespace)
        outcome = SemanticOutcome()
        investigated = False
        if provider is not None and not faults:
            investigated = True
            outcome = self._investigate(ingested, knowledge, provider, namespace)
            diagnostics.extend(outcome.diagnostics)
            diagnostics.extend(f"gap:{item}" for item in outcome.gaps_opened)
            semantic_faults.extend(outcome.faults)
        if provider is None:
            diagnostics.append(PROVIDER_UNAVAILABLE)
        status, reason = derive_status(
            faults=faults,
            gaps=ingested.open_gaps,
            provider_present=provider is not None,
            semantic_faults=tuple(semantic_faults),
        )
        return IngestionResult(
            source_id=ingested.source_id,
            version_hash=ingested.version_hash,
            blocks=len(ingested.blocks),
            entities_written=outcome.entities_written,
            status=status,
            reason=reason,
            diagnostics=tuple(dict.fromkeys(diagnostics)),
            provider_used=investigated,
        )

    def _investigate(
        self,
        ingested: IngestedSource,
        knowledge: KnowledgeRepository,
        provider: SessionProvider,
        namespace: str,
    ) -> SemanticOutcome:
        investigator = self._investigator or SemanticInvestigator()
        return investigator.run(ingested, knowledge, provider, namespace)

    def _register(
        self,
        ingested: IngestedSource,
        knowledge: KnowledgeRepository,
        namespace: str,
    ) -> SourceVersion:
        version = SourceVersion(
            source_id=ingested.source_id,
            version_hash=ingested.version_hash,
            locator_root=ingested.uri,
            captured_at=ingested.source.captured_at.isoformat(),
        )
        known = {item.key for item in knowledge.source_versions(ingested.source_id)}
        if version.key in known:
            return version
        with knowledge.begin_revision(
            author=AUTHOR, summary=f"{namespace}:{ingested.source_id}"
        ) as transaction:
            transaction.put_source_version(version)
        return version
