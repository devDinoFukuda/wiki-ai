from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from wiki_ai.agent.protocol import AgentProvider
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.repository.snapshot import RepositorySnapshot

__all__ = [
    "CapabilityUnavailable",
    "InvestigationOutcome",
    "IngestionOutcome",
    "AnswerOutcome",
    "PublicationOutcome",
    "InvestigationRunner",
    "IngestionRunner",
    "QueryRunner",
    "PublicationRunner",
]


class CapabilityUnavailable(Exception):
    def __init__(self, reason: str, action: str) -> None:
        super().__init__(f"{reason}: {action}")
        self.reason = reason
        self.action = action


@dataclass(frozen=True)
class InvestigationOutcome:
    objective: str
    entities_written: int
    relations_written: int
    evidence_written: int
    unresolved: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "entities_written": self.entities_written,
            "relations_written": self.relations_written,
            "evidence_written": self.evidence_written,
            "unresolved": list(self.unresolved),
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class IngestionOutcome:
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


@dataclass(frozen=True)
class AnswerOutcome:
    question: str
    answer: str
    evidence_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "evidence_ids": list(self.evidence_ids),
            "entity_ids": list(self.entity_ids),
            "unresolved": list(self.unresolved),
        }


@dataclass(frozen=True)
class PublicationOutcome:
    publication_id: str
    artifacts: tuple[str, ...]
    manifest_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "publication_id": self.publication_id,
            "artifacts": list(self.artifacts),
            "manifest_hash": self.manifest_hash,
        }


@runtime_checkable
class InvestigationRunner(Protocol):
    def run(
        self,
        objective: str,
        snapshot: RepositorySnapshot,
        knowledge: KnowledgeRepository,
        provider: AgentProvider,
        namespace: str,
    ) -> InvestigationOutcome: ...


@runtime_checkable
class IngestionRunner(Protocol):
    def run(
        self,
        source_path: Path,
        version_hash: str,
        knowledge: KnowledgeRepository,
        namespace: str,
    ) -> IngestionOutcome: ...


@runtime_checkable
class QueryRunner(Protocol):
    def run(
        self,
        question: str,
        knowledge: KnowledgeRepository,
        provider: AgentProvider | None,
        namespace: str,
    ) -> AnswerOutcome: ...


@runtime_checkable
class PublicationRunner(Protocol):
    def run(
        self,
        knowledge: KnowledgeRepository,
        publications_dir: Path,
        namespace: str,
    ) -> PublicationOutcome: ...
