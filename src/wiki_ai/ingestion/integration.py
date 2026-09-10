from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from wiki_ai.knowledge.model import SourceVersion
from wiki_ai.knowledge.repository import KnowledgeRepository

from wiki_ai.ingestion import pipeline
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
    "IngestionResult",
    "ProviderResolver",
    "IngestionEngine",
]

PROVIDER_UNAVAILABLE = "semantic_investigation_skipped:agent_provider_unavailable"
VERSION_MISMATCH = "version_hash_mismatch"


@runtime_checkable
class ProviderResolver(Protocol):
    def __call__(self) -> SessionProvider | None: ...


@dataclass(frozen=True)
class IngestionResult:
    source_id: str
    version_hash: str
    blocks: int
    entities_written: int
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "version_hash": self.version_hash,
            "blocks": self.blocks,
            "entities_written": self.entities_written,
            "diagnostics": list(self.diagnostics),
        }


def _structural_diagnostics(ingested: IngestedSource) -> tuple[str, ...]:
    found: list[str] = []
    for item in ingested.diagnostics:
        if item.level is DiagnosticLevel.INFO:
            continue
        found.append(f"{item.level.value}:{item.code}")
    if ingested.status == IngestStatus.PARTIAL:
        found.append("source_partially_interpreted")
    if ingested.status == IngestStatus.FAILED:
        found.append("source_not_interpreted")
    return tuple(dict.fromkeys(found))


class IngestionEngine:
    def __init__(
        self,
        provider_resolver: ProviderResolver | None = None,
        investigator: SemanticInvestigator | None = None,
        ingest: Callable[..., IngestedSource] | None = None,
    ) -> None:
        self._resolve = provider_resolver
        self._investigator = investigator
        self._ingest = ingest or pipeline.ingest

    def run(
        self,
        source_path: Path,
        version_hash: str,
        knowledge: KnowledgeRepository,
        namespace: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> IngestionResult:
        ingested = self._ingest(source_path, metadata or {})
        diagnostics = list(_structural_diagnostics(ingested))
        declared = str(version_hash or "").strip()
        if declared and declared != ingested.version_hash:
            diagnostics.append(VERSION_MISMATCH)
        self._register(ingested, knowledge, namespace)
        provider = self._resolve() if self._resolve is not None else None
        if provider is None:
            diagnostics.append(PROVIDER_UNAVAILABLE)
            return IngestionResult(
                source_id=ingested.source_id,
                version_hash=ingested.version_hash,
                blocks=len(ingested.blocks),
                entities_written=0,
                diagnostics=tuple(dict.fromkeys(diagnostics)),
            )
        if ingested.status == IngestStatus.FAILED:
            return IngestionResult(
                source_id=ingested.source_id,
                version_hash=ingested.version_hash,
                blocks=len(ingested.blocks),
                entities_written=0,
                diagnostics=tuple(dict.fromkeys(diagnostics)),
            )
        outcome = self._investigate(ingested, knowledge, provider, namespace)
        diagnostics.extend(outcome.diagnostics)
        diagnostics.extend(f"gap:{item}" for item in outcome.gaps_opened)
        return IngestionResult(
            source_id=ingested.source_id,
            version_hash=ingested.version_hash,
            blocks=len(ingested.blocks),
            entities_written=outcome.entities_written,
            diagnostics=tuple(dict.fromkeys(diagnostics)),
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
