from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from wiki_ai import __version__
from wiki_ai.app.ports import CapabilityUnavailable
from wiki_ai.app.session import Session, detect_outdated_store
from wiki_ai.app.wiring import Wiring, default_wiring
from wiki_ai.ingestion.source import SourceKind
from wiki_ai.knowledge.model import SourceVersion
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.publishing.release import current as current_publication
from wiki_ai.repository.inventory import Inventory, build_inventory
from wiki_ai.repository.snapshot import (
    RepositorySnapshot,
    SnapshotSpec,
    take_snapshot,
)

__all__ = [
    "ApiError",
    "RepositoryNotFound",
    "SourceNotFound",
    "OutdatedStore",
    "InspectReport",
    "AnalyzeReport",
    "IngestReport",
    "AskReport",
    "PublishReport",
    "StatusReport",
    "DEFAULT_EXCLUDES",
    "DEFAULT_OBJECTIVE",
    "SOURCE_KIND_BY_EXTENSION",
    "source_kind_for",
    "version",
    "inspect",
    "analyze",
    "ingest",
    "ask",
    "publish",
    "status",
]

DEFAULT_EXCLUDES = (".wiki-ai",)
DEFAULT_OBJECTIVE = "describe how this system works"
_HASH_CHUNK = 65536

SOURCE_KIND_BY_EXTENSION: Mapping[str, SourceKind] = {
    ".docx": SourceKind.DOCX,
    ".xlsx": SourceKind.XLSX,
    ".xlsm": SourceKind.XLSX,
    ".drawio": SourceKind.DRAWIO,
    ".pdf": SourceKind.PDF,
    ".vtt": SourceKind.TRANSCRIPT,
    ".srt": SourceKind.TRANSCRIPT,
    ".md": SourceKind.MARKDOWN,
    ".markdown": SourceKind.MARKDOWN,
    ".html": SourceKind.HTML,
    ".htm": SourceKind.HTML,
    ".json": SourceKind.JSON,
    ".xml": SourceKind.XML,
}


class ApiError(Exception):
    pass


class RepositoryNotFound(ApiError):
    pass


class SourceNotFound(ApiError):
    pass


class OutdatedStore(ApiError):
    def __init__(self, markers: tuple[str, ...]) -> None:
        super().__init__(", ".join(markers))
        self.markers = markers


@dataclass(frozen=True)
class InspectReport:
    root: str
    git_head: str | None
    snapshot_digest: str
    total_files: int
    analyzable_files: int
    by_classification: tuple[tuple[str, int], ...]
    by_language: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "git_head": self.git_head,
            "snapshot_digest": self.snapshot_digest,
            "total_files": self.total_files,
            "analyzable_files": self.analyzable_files,
            "by_classification": dict(self.by_classification),
            "by_language": dict(self.by_language),
        }


@dataclass(frozen=True)
class AnalyzeReport:
    status: str
    snapshot_digest: str
    analyzable_files: int
    reason: str = ""
    action: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "snapshot_digest": self.snapshot_digest,
            "analyzable_files": self.analyzable_files,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.action:
            payload["action"] = self.action
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class IngestReport:
    status: str
    source: str
    kind: str
    version_hash: str
    registered: bool
    reason: str = ""
    action: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "source": self.source,
            "kind": self.kind,
            "version_hash": self.version_hash,
            "registered": self.registered,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.action:
            payload["action"] = self.action
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class AskReport:
    status: str
    question: str
    answer: str = ""
    evidence_ids: tuple[str, ...] = ()
    reason: str = ""
    action: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status, "question": self.question}
        if self.answer:
            payload["answer"] = self.answer
        if self.evidence_ids:
            payload["evidence_ids"] = list(self.evidence_ids)
        if self.reason:
            payload["reason"] = self.reason
        if self.action:
            payload["action"] = self.action
        return payload


@dataclass(frozen=True)
class PublishReport:
    status: str
    publication_id: str = ""
    artifacts: tuple[str, ...] = ()
    reason: str = ""
    action: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status}
        if self.publication_id:
            payload["publication_id"] = self.publication_id
        if self.artifacts:
            payload["artifacts"] = list(self.artifacts)
        if self.reason:
            payload["reason"] = self.reason
        if self.action:
            payload["action"] = self.action
        return payload


@dataclass(frozen=True)
class StatusReport:
    status: str
    root: str
    snapshot_digest: str | None
    entities: int
    relations: int
    evidence: int
    sources: int
    last_publication: str | None
    provider_available: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "root": self.root,
            "snapshot_digest": self.snapshot_digest,
            "entities": self.entities,
            "relations": self.relations,
            "evidence": self.evidence,
            "sources": self.sources,
            "last_publication": self.last_publication,
            "provider_available": self.provider_available,
        }


def version() -> str:
    return __version__


def _pairs(counts: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(counts.items()))


def _report(snapshot: RepositorySnapshot, inventory: Inventory) -> InspectReport:
    return InspectReport(
        root=snapshot.root,
        git_head=snapshot.git_head,
        snapshot_digest=snapshot.digest,
        total_files=len(inventory.entries),
        analyzable_files=len(inventory.analyzable()),
        by_classification=_pairs(inventory.by_classification()),
        by_language=_pairs(inventory.by_language()),
    )


def _checked_root(repo: Path) -> Path:
    root = Path(repo)
    if not root.is_dir():
        raise RepositoryNotFound(f"repository root is not a directory: {root}")
    markers = detect_outdated_store(root)
    if markers:
        raise OutdatedStore(markers)
    return root


def inspect(repo: Path) -> InspectReport:
    root = _checked_root(repo)
    snapshot = take_snapshot(SnapshotSpec(root=root, excludes=DEFAULT_EXCLUDES))
    return _report(snapshot, build_inventory(snapshot))


def source_kind_for(source_path: Path) -> SourceKind:
    if Path(source_path).is_dir():
        return SourceKind.CODEBASE
    return SOURCE_KIND_BY_EXTENSION.get(Path(source_path).suffix.lower(), SourceKind.JSON)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _knowledge_counts(knowledge: KnowledgeRepository) -> tuple[int, int, int, int]:
    evidence = int(knowledge.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0])
    return (
        knowledge.entity_count(),
        knowledge.relation_count(),
        evidence,
        len(knowledge.source_versions()),
    )


def analyze(
    repo: Path, objective: str | None = None, wiring: Wiring | None = None
) -> AnalyzeReport:
    root = _checked_root(repo)
    composition = wiring if wiring is not None else default_wiring()
    session = Session.open(root)
    snapshot = take_snapshot(SnapshotSpec(root=root, excludes=DEFAULT_EXCLUDES))
    session.save_snapshot(snapshot)
    inventory = build_inventory(snapshot)
    analyzable = len(inventory.analyzable())
    provider = session.resolve_provider(composition.registry)
    if provider is None:
        return AnalyzeReport(
            status="blocked",
            snapshot_digest=snapshot.digest,
            analyzable_files=analyzable,
            reason="agent_provider_unavailable",
            action="configure a supported provider",
        )
    goal = objective.strip() if objective and objective.strip() else DEFAULT_OBJECTIVE
    try:
        runner = composition.investigation_runner()
    except CapabilityUnavailable as exc:
        return AnalyzeReport(
            status="blocked",
            snapshot_digest=snapshot.digest,
            analyzable_files=analyzable,
            reason=exc.reason,
            action=exc.action,
        )
    with session.open_knowledge() as knowledge:
        outcome = runner.run(goal, snapshot, knowledge, provider, session.namespace)
    return AnalyzeReport(
        status="ok",
        snapshot_digest=snapshot.digest,
        analyzable_files=analyzable,
        details=outcome.to_dict(),
    )


def ingest(source_path: Path, repo: Path, wiring: Wiring | None = None) -> IngestReport:
    root = _checked_root(repo)
    source = Path(source_path)
    if not source.is_file():
        raise SourceNotFound(f"source is not a readable file: {source}")
    composition = wiring if wiring is not None else default_wiring()
    session = Session.open(root)
    kind = source_kind_for(source)
    version_hash = _file_hash(source)
    resolved = source.resolve()
    record = SourceVersion(
        source_id=resolved.as_posix(),
        version_hash=version_hash,
        locator_root=resolved.parent.as_posix(),
        captured_at=_utc_now(),
    )
    with session.open_knowledge() as knowledge:
        with knowledge.begin_revision(author=session.namespace, summary=kind.value) as tx:
            tx.put_source_version(record)
        try:
            runner = composition.ingestion_runner()
        except CapabilityUnavailable as exc:
            return IngestReport(
                status="blocked",
                source=resolved.as_posix(),
                kind=kind.value,
                version_hash=version_hash,
                registered=True,
                reason=exc.reason,
                action=exc.action,
            )
        outcome = runner.run(source, version_hash, knowledge, session.namespace)
    return IngestReport(
        status="ok",
        source=resolved.as_posix(),
        kind=kind.value,
        version_hash=version_hash,
        registered=True,
        details=outcome.to_dict(),
    )


def ask(question: str, repo: Path, wiring: Wiring | None = None) -> AskReport:
    root = _checked_root(repo)
    composition = wiring if wiring is not None else default_wiring()
    session = Session.open(root)
    with session.open_knowledge() as knowledge:
        entities, _, evidence, sources = _knowledge_counts(knowledge)
        if entities == 0 and evidence == 0 and sources == 0:
            return AskReport(
                status="blocked",
                question=question,
                reason="knowledge_empty",
                action="run analyze or ingest first",
            )
        try:
            runner = composition.query_runner()
        except CapabilityUnavailable as exc:
            return AskReport(
                status="blocked",
                question=question,
                reason=exc.reason,
                action=exc.action,
            )
        provider = session.resolve_provider(composition.registry)
        outcome = runner.run(question, knowledge, provider, session.namespace)
    return AskReport(
        status="ok",
        question=question,
        answer=outcome.answer,
        evidence_ids=outcome.evidence_ids,
    )


def publish(repo: Path, wiring: Wiring | None = None) -> PublishReport:
    root = _checked_root(repo)
    composition = wiring if wiring is not None else default_wiring()
    session = Session.open(root)
    with session.open_knowledge() as knowledge:
        entities, _, evidence, _ = _knowledge_counts(knowledge)
        if entities == 0 and evidence == 0:
            return PublishReport(
                status="blocked",
                reason="nothing_to_publish",
                action="run analyze or ingest first",
            )
        try:
            runner = composition.publication_runner()
        except CapabilityUnavailable as exc:
            return PublishReport(status="blocked", reason=exc.reason, action=exc.action)
        outcome = runner.run(knowledge, session.publications_dir, session.namespace)
    return PublishReport(
        status="ok",
        publication_id=outcome.publication_id,
        artifacts=outcome.artifacts,
    )


def status(repo: Path, wiring: Wiring | None = None) -> StatusReport:
    root = _checked_root(repo)
    composition = wiring if wiring is not None else default_wiring()
    session = Session.open(root)
    with session.open_knowledge() as knowledge:
        entities, relations, evidence, sources = _knowledge_counts(knowledge)
    publication = current_publication(session.publications_dir)
    return StatusReport(
        status="ok",
        root=session.repo.as_posix(),
        snapshot_digest=session.current_snapshot_id(),
        entities=entities,
        relations=relations,
        evidence=evidence,
        sources=sources,
        last_publication=publication.publication_id if publication else None,
        provider_available=bool(composition.registry.available()),
    )
