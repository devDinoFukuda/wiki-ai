from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.agent.session import AgentProvider
from wiki_ai.app.ports import (
    AnswerOutcome,
    IngestionOutcome,
    IngestionRunner,
    InvestigationOutcome,
    InvestigationRunner,
    OutcomeStatus,
    PublicationOutcome,
    PublicationRunner,
    QueryRunner,
    UpdateOutcome,
    UpdateRunner,
)
from wiki_ai.ingestion.integration import IngestionEngine
from wiki_ai.investigation.orchestrator import InvestigationOutcomeData, Investigator
from wiki_ai.investigation.update import UpdateEngine
from wiki_ai.investigation.update import UpdateOutcome as UpdateOutcomeData
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.publishing.answer import Answerer
from wiki_ai.publishing.pipeline import Publisher
from wiki_ai.repository.snapshot import RepositorySnapshot

__all__ = [
    "DETAIL_KEYS",
    "status_of",
    "reason_of",
    "IngestionAdapter",
    "InvestigationAdapter",
    "PublicationAdapter",
    "QueryAdapter",
    "UpdateAdapter",
    "Wiring",
    "default_wiring",
]

DETAIL_KEYS = (
    "rounds",
    "tool_calls",
    "coverage",
    "focus_paths",
    "outside_focus_reads",
    "files_read",
)


def _summary(details: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in DETAIL_KEYS:
        if key in details:
            summary[key] = details[key]
    return summary


def status_of(data: Any) -> OutcomeStatus:
    produced = getattr(data, "status", None)
    if produced is None:
        return OutcomeStatus.COMPLETE
    name = getattr(produced, "name", str(produced))
    try:
        return OutcomeStatus[str(name)]
    except KeyError:
        return OutcomeStatus.FAILED


def reason_of(data: Any) -> str:
    return str(getattr(data, "reason", "") or "")


def _status_value(data: Any) -> str:
    produced = getattr(data, "status", "")
    return str(getattr(produced, "value", produced))


def _mode_of(data: Any) -> str:
    produced = getattr(data, "mode", "")
    if not produced:
        return ""
    return str(getattr(produced, "value", produced))


def _investigation_outcome(data: InvestigationOutcomeData) -> InvestigationOutcome:
    return InvestigationOutcome(
        objective=data.objective,
        entities_written=data.entities_written,
        relations_written=data.relations_written,
        evidence_written=data.evidence_written,
        status=status_of(data),
        reason=reason_of(data),
        unresolved=tuple(data.unresolved),
        details=_summary(data.details),
    )


def _update_outcome(data: UpdateOutcomeData) -> UpdateOutcome:
    reinvestigated: dict[str, Any] | None = None
    if data.reinvestigation is not None:
        reinvestigated = _investigation_outcome(data.reinvestigation).to_dict()
        reinvestigated["details"] = _summary(data.reinvestigation.details)
    return UpdateOutcome(
        diff=dict(data.diff_summary),
        invalidated=data.invalidated.total,
        status=status_of(data),
        reason=reason_of(data),
        reinvestigated=reinvestigated,
        skipped_reason=data.skipped_reason,
        gaps=tuple(data.invalidated.gaps),
    )


class InvestigationAdapter:
    def __init__(self, investigator: Investigator | None = None) -> None:
        self._investigator = investigator if investigator is not None else Investigator()

    def run(
        self,
        objective: str,
        snapshot: RepositorySnapshot,
        knowledge: KnowledgeRepository,
        provider: AgentProvider,
        namespace: str,
    ) -> InvestigationOutcome:
        return _investigation_outcome(
            self._investigator.run(objective, snapshot, knowledge, provider, namespace)
        )


class IngestionAdapter:
    def __init__(self, engine: IngestionEngine | None = None) -> None:
        self._engine = engine if engine is not None else IngestionEngine()

    def run(
        self,
        source_path: Path,
        version_hash: str,
        knowledge: KnowledgeRepository,
        namespace: str,
        *,
        provider: AgentProvider | None = None,
    ) -> IngestionOutcome:
        produced = self._engine.run(
            source_path, version_hash, knowledge, namespace, provider=provider
        )
        return IngestionOutcome(
            source_id=produced.source_id,
            version_hash=produced.version_hash,
            blocks=produced.blocks,
            entities_written=produced.entities_written,
            status=status_of(produced),
            reason=reason_of(produced),
            diagnostics=tuple(produced.diagnostics),
            provider_used=bool(produced.provider_used),
        )


class UpdateAdapter:
    def __init__(self, engine: UpdateEngine | None = None) -> None:
        self._engine = engine if engine is not None else UpdateEngine()

    def run(
        self,
        previous_snapshot: RepositorySnapshot,
        current_snapshot: RepositorySnapshot,
        knowledge: KnowledgeRepository,
        provider: AgentProvider | None,
        namespace: str,
    ) -> UpdateOutcome:
        produced = self._engine.run(
            previous_snapshot, current_snapshot, knowledge, provider, namespace
        )
        return _update_outcome(produced)


class PublicationAdapter:
    def __init__(self, publisher: Publisher | None = None) -> None:
        self._publisher = publisher if publisher is not None else Publisher(
            query_factory=KnowledgeQuery
        )

    def run(
        self,
        knowledge: KnowledgeRepository,
        publications_dir: Path,
        namespace: str,
    ) -> PublicationOutcome:
        produced = self._publisher.run(knowledge, publications_dir, namespace)
        return PublicationOutcome(
            publication_id=produced.publication_id,
            artifacts=tuple(produced.artifacts),
            manifest_hash=produced.manifest_hash,
        )


class QueryAdapter:
    def __init__(self, answerer: Answerer | None = None) -> None:
        self._answerer = answerer if answerer is not None else Answerer(
            query_factory=KnowledgeQuery
        )

    def run(
        self,
        question: str,
        knowledge: KnowledgeRepository,
        provider: AgentProvider | None,
        namespace: str,
    ) -> AnswerOutcome:
        produced = self._answerer.run(question, knowledge, provider, namespace)
        return AnswerOutcome(
            question=produced.question,
            answer=produced.answer,
            mode=_mode_of(produced),
            reason=reason_of(produced),
            status=_status_value(produced),
            evidence_ids=tuple(produced.evidence_ids),
            entity_ids=tuple(produced.entity_ids),
            unresolved=tuple(produced.unresolved),
        )


class Wiring:
    def __init__(
        self,
        registry: ProviderRegistry | None = None,
        provider: str | None = None,
    ) -> None:
        self._registry = registry if registry is not None else ProviderRegistry(adapters=True)
        wanted = provider.strip() if provider is not None else ""
        self._provider = wanted or None

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    @property
    def provider(self) -> str | None:
        return self._provider

    def investigation_runner(self) -> InvestigationRunner:
        return InvestigationAdapter()

    def ingestion_runner(self) -> IngestionRunner:
        return IngestionAdapter()

    def update_runner(self) -> UpdateRunner:
        return UpdateAdapter()

    def query_runner(self) -> QueryRunner:
        return QueryAdapter()

    def publication_runner(self) -> PublicationRunner:
        return PublicationAdapter()


def default_wiring() -> Wiring:
    return Wiring()
