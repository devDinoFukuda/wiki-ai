from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from wiki_ai import __version__
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app.ports import CapabilityUnavailable, OutcomeStatus
from wiki_ai.app.session import (
    ANALYSIS_CONTRACT_VERSION,
    PREFERENCE_SOURCES,
    STATE_DIR_NAME,
    AnalysisState,
    AnalysisStatus,
    ResolvedProvider,
    Session,
    detect_outdated_store,
    objective_hash,
)
from wiki_ai.app.wiring import Wiring, default_wiring
from wiki_ai.ingestion.source import SourceKind
from wiki_ai.knowledge.correlation import correlate
from wiki_ai.knowledge.model import SourceVersion
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.publishing.pipeline import PublicationBlocked
from wiki_ai.publishing.release import current as current_publication
from wiki_ai.repository.inventory import Inventory, build_inventory
from wiki_ai.repository.snapshot import (
    RepositoryObservation,
    RepositorySnapshot,
    SnapshotSpec,
    diff,
    observe,
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
    "ProviderReport",
    "DEFAULT_EXCLUDES",
    "DEFAULT_OBJECTIVE",
    "UP_TO_DATE",
    "REPORT_STATUS_BY_OUTCOME",
    "ANALYSIS_STATUS_BY_OUTCOME",
    "ASK_STATUS_BY_ANSWER",
    "SOURCE_KIND_BY_EXTENSION",
    "CORRELATION_DETAIL",
    "source_kind_for",
    "snake_case",
    "version",
    "inspect",
    "analyze",
    "ingest",
    "ask",
    "publish",
    "status",
    "provider_show",
    "provider_set",
]

DEFAULT_EXCLUDES = (STATE_DIR_NAME,)
DEFAULT_OBJECTIVE = "describe how this system works"
PUBLICATION_ACTION = "resolve the listed publication issues"
UP_TO_DATE = "up_to_date"
PROVIDER_ACTION = "configure a supported provider"
PROVIDER_UNAVAILABLE = "agent_provider_unavailable"
UNKNOWN_PROVIDER = "unknown_provider"
UNKNOWN_PROVIDER_ACTION = "choose one of the registered providers"
STRUCTURAL_ONLY = "structural_only"
INCOMPLETE_ACTION = "review the reported gaps and run again"
ANSWER_ACTION = "review the unresolved notes and add evidence"
CORRELATION_DETAIL = "correlation"
_HASH_CHUNK = 65536
_NON_WORD = re.compile(r"[^a-z0-9]+")

REPORT_STATUS_BY_OUTCOME: Mapping[OutcomeStatus, str] = {
    OutcomeStatus.COMPLETE: "ok",
    OutcomeStatus.STRUCTURAL_ONLY: "partial",
    OutcomeStatus.PARTIAL: "partial",
    OutcomeStatus.BLOCKED: "blocked",
    OutcomeStatus.FAILED: "error",
}

ASK_STATUS_BY_ANSWER: Mapping[str, str] = {
    "answered": "ok",
    "partial": "partial",
    "blocked": "blocked",
}

ANALYSIS_STATUS_BY_OUTCOME: Mapping[OutcomeStatus, AnalysisStatus] = {
    OutcomeStatus.COMPLETE: AnalysisStatus.COMPLETE,
    OutcomeStatus.STRUCTURAL_ONLY: AnalysisStatus.PARTIAL,
    OutcomeStatus.PARTIAL: AnalysisStatus.PARTIAL,
    OutcomeStatus.BLOCKED: AnalysisStatus.BLOCKED,
    OutcomeStatus.FAILED: AnalysisStatus.FAILED,
}

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
    analysis_status: str = AnalysisStatus.NEVER.value
    analyzed_digest: str = ""
    provider: str = ""
    reason: str = ""
    action: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "snapshot_digest": self.snapshot_digest,
            "analyzable_files": self.analyzable_files,
            "analysis_status": self.analysis_status,
            "analyzed_digest": self.analyzed_digest,
            "provider": self.provider,
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
    ingestion_status: str = OutcomeStatus.COMPLETE.value
    provider: str = ""
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
            "ingestion_status": self.ingestion_status,
            "provider": self.provider,
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
    mode: str = ""
    evidence_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    provider: str = ""
    reason: str = ""
    action: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "question": self.question,
            "provider": self.provider,
        }
        if self.answer:
            payload["answer"] = self.answer
        if self.mode:
            payload["mode"] = self.mode
        if self.evidence_ids:
            payload["evidence_ids"] = list(self.evidence_ids)
        if self.entity_ids:
            payload["entity_ids"] = list(self.entity_ids)
        if self.unresolved:
            payload["unresolved"] = list(self.unresolved)
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
    manifest_hash: str = ""
    reason: str = ""
    action: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status}
        if self.publication_id:
            payload["publication_id"] = self.publication_id
        if self.artifacts:
            payload["artifacts"] = list(self.artifacts)
        if self.manifest_hash:
            payload["manifest_hash"] = self.manifest_hash
        if self.reason:
            payload["reason"] = self.reason
        if self.action:
            payload["action"] = self.action
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class StatusReport:
    status: str
    root: str
    snapshot_digest: str | None
    analysis_status: str
    analyzed_digest: str
    observed_digest: str
    entities: int
    relations: int
    evidence: int
    sources: int
    sources_by_kind: tuple[tuple[str, int], ...]
    pending_update: bool
    last_publication: str | None
    publication_artifacts: int
    provider_available: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "root": self.root,
            "snapshot_digest": self.snapshot_digest,
            "analysis_status": self.analysis_status,
            "analyzed_digest": self.analyzed_digest,
            "observed_digest": self.observed_digest,
            "entities": self.entities,
            "relations": self.relations,
            "evidence": self.evidence,
            "sources": dict(self.sources_by_kind),
            "source_versions": self.sources,
            "pending_update": self.pending_update,
            "last_publication": self.last_publication,
            "publication_artifacts": self.publication_artifacts,
            "provider_available": self.provider_available,
        }


@dataclass(frozen=True)
class ProviderReport:
    status: str
    provider: str = ""
    source: str = ""
    registered: tuple[str, ...] = ()
    reason: str = ""
    action: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "provider": self.provider,
            "registered": list(self.registered),
        }
        if self.source:
            payload["source"] = self.source
        if self.reason:
            payload["reason"] = self.reason
        if self.action:
            payload["action"] = self.action
        return payload


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


def snake_case(text: str) -> str:
    head = str(text).split(":", 1)[0]
    return _NON_WORD.sub("_", head.strip().lower()).strip("_")


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


def _analyzable_count(observation: RepositoryObservation) -> int:
    survey = RepositorySnapshot(
        root=observation.root,
        git_head=None,
        files=observation.files,
        digest=observation.digest,
        taken_at=observation.observed_at,
    )
    return len(build_inventory(survey).analyzable())


def _wrote_entities(reinvestigated: Mapping[str, Any] | None) -> bool:
    if reinvestigated is None:
        return False
    return int(reinvestigated.get("entities_written", 0)) > 0


def _correlated(
    knowledge: KnowledgeRepository, namespace: str, payload: dict[str, Any]
) -> dict[str, Any]:
    payload[CORRELATION_DETAIL] = correlate(knowledge, namespace).to_dict()
    return payload


def _composition(wiring: Wiring | None, registry: ProviderRegistry | None) -> Wiring:
    if wiring is not None:
        return wiring
    if registry is not None:
        return Wiring(registry)
    return default_wiring()


def _settled_baseline(state: AnalysisState, objective_key: str) -> bool:
    return (
        state.analysis_status is AnalysisStatus.COMPLETE
        and bool(objective_key)
        and objective_key == state.analyzed_objective_hash
        and state.contract_version == ANALYSIS_CONTRACT_VERSION
    )


def _blocked_analysis(
    session: Session,
    digest: str,
    analyzable: int,
    reason: str,
    action: str,
    provider_name: str = "",
    details: Mapping[str, Any] | None = None,
) -> AnalyzeReport:
    state = session.record_analysis(digest, AnalysisStatus.BLOCKED, reason)
    return AnalyzeReport(
        status="blocked",
        snapshot_digest=digest,
        analyzable_files=analyzable,
        analysis_status=state.analysis_status.value,
        analyzed_digest=state.analyzed_digest,
        provider=provider_name,
        reason=reason,
        action=action,
        details=dict(details) if details else {},
    )


def _settled_analysis(
    session: Session,
    digest: str,
    analyzable: int,
    outcome_status: OutcomeStatus,
    reason: str,
    payload: Mapping[str, Any],
    objective_key: str,
    provider_name: str,
) -> AnalyzeReport:
    analysis_status = ANALYSIS_STATUS_BY_OUTCOME[outcome_status]
    state = session.record_analysis(digest, analysis_status, reason, objective_key)
    report_status = REPORT_STATUS_BY_OUTCOME[outcome_status]
    action = "" if report_status == "ok" else INCOMPLETE_ACTION
    if report_status == "blocked" and reason == PROVIDER_UNAVAILABLE:
        action = PROVIDER_ACTION
    return AnalyzeReport(
        status=report_status,
        snapshot_digest=digest,
        analyzable_files=analyzable,
        analysis_status=state.analysis_status.value,
        analyzed_digest=state.analyzed_digest,
        provider=provider_name,
        reason=reason,
        action=action,
        details=dict(payload),
    )


def _update_report(
    session: Session,
    composition: Wiring,
    previous: RepositorySnapshot,
    snapshot: RepositorySnapshot,
    analyzable: int,
    resolved: ResolvedProvider,
    objective_key: str,
) -> AnalyzeReport:
    try:
        runner = composition.update_runner()
    except CapabilityUnavailable as exc:
        return _blocked_analysis(
            session, snapshot.digest, analyzable, exc.reason, exc.action, resolved.name
        )
    with session.open_knowledge() as knowledge:
        outcome = runner.run(
            previous, snapshot, knowledge, resolved.provider, session.namespace
        )
        payload = outcome.to_dict()
        if _wrote_entities(outcome.reinvestigated):
            payload = _correlated(knowledge, session.namespace, payload)
    session.save_snapshot(snapshot)
    if resolved.provider is None:
        return _blocked_analysis(
            session,
            snapshot.digest,
            analyzable,
            PROVIDER_UNAVAILABLE,
            PROVIDER_ACTION,
            resolved.name,
            payload,
        )
    return _settled_analysis(
        session,
        snapshot.digest,
        analyzable,
        outcome.status,
        outcome.reason,
        payload,
        objective_key,
        resolved.name,
    )


def analyze(
    repo: Path,
    objective: str | None = None,
    wiring: Wiring | None = None,
    registry: ProviderRegistry | None = None,
) -> AnalyzeReport:
    root = _checked_root(repo)
    composition = _composition(wiring, registry)
    session = Session.open(root)
    goal = objective.strip() if objective and objective.strip() else DEFAULT_OBJECTIVE
    objective_key = objective_hash(goal)
    spec = SnapshotSpec(root=root, excludes=DEFAULT_EXCLUDES)
    observation = observe(spec)
    state = session.record_observation(observation.digest)
    resolved = session.resolve_provider(
        composition.registry, composition.provider
    )
    if state.is_current_for(observation.digest, objective_key):
        return AnalyzeReport(
            status="ok",
            snapshot_digest=observation.digest,
            analyzable_files=_analyzable_count(observation),
            analysis_status=state.analysis_status.value,
            analyzed_digest=state.analyzed_digest,
            provider=resolved.name,
            reason=UP_TO_DATE,
        )
    snapshot = take_snapshot(spec, store=session.snapshot_store)
    analyzable = len(build_inventory(snapshot).analyzable())
    previous = session.current_snapshot()
    if (
        previous is not None
        and _settled_baseline(state, objective_key)
        and not diff(previous, snapshot).is_empty()
    ):
        return _update_report(
            session, composition, previous, snapshot, analyzable, resolved, objective_key
        )
    session.save_snapshot(snapshot)
    if resolved.provider is None:
        return _blocked_analysis(
            session,
            snapshot.digest,
            analyzable,
            PROVIDER_UNAVAILABLE,
            PROVIDER_ACTION,
            resolved.name,
        )
    try:
        runner = composition.investigation_runner()
    except CapabilityUnavailable as exc:
        return _blocked_analysis(
            session, snapshot.digest, analyzable, exc.reason, exc.action, resolved.name
        )
    with session.open_knowledge() as knowledge:
        outcome = runner.run(
            goal, snapshot, knowledge, resolved.provider, session.namespace
        )
        payload = outcome.to_dict()
        if outcome.entities_written:
            payload = _correlated(knowledge, session.namespace, payload)
    return _settled_analysis(
        session,
        snapshot.digest,
        analyzable,
        outcome.status,
        outcome.reason,
        payload,
        objective_key,
        resolved.name,
    )


def ingest(
    source_path: Path,
    repo: Path,
    wiring: Wiring | None = None,
    registry: ProviderRegistry | None = None,
) -> IngestReport:
    root = _checked_root(repo)
    source = Path(source_path)
    if not source.is_file():
        raise SourceNotFound(f"source is not a readable file: {source}")
    composition = _composition(wiring, registry)
    session = Session.open(root)
    kind = source_kind_for(source)
    version_hash = _file_hash(source)
    chosen = session.resolve_provider(composition.registry, composition.provider)
    provider_name = chosen.name
    resolved = source.resolve()
    with session.open_knowledge() as knowledge:
        try:
            runner = composition.ingestion_runner()
        except CapabilityUnavailable as exc:
            record = SourceVersion(
                source_id=resolved.as_posix(),
                version_hash=version_hash,
                locator_root=resolved.parent.as_posix(),
                captured_at=_utc_now(),
            )
            with knowledge.begin_revision(
                author=session.namespace, summary=kind.value
            ) as tx:
                tx.put_source_version(record)
            return IngestReport(
                status="blocked",
                source=resolved.as_posix(),
                kind=kind.value,
                version_hash=version_hash,
                registered=True,
                ingestion_status=OutcomeStatus.BLOCKED.value,
                provider=provider_name,
                reason=exc.reason,
                action=exc.action,
            )
        outcome = runner.run(
            source, "", knowledge, session.namespace, provider=chosen.provider
        )
        registered = bool(knowledge.source_versions(outcome.source_id))
        payload = outcome.to_dict()
        if outcome.entities_written:
            payload = _correlated(knowledge, session.namespace, payload)
    report_status = REPORT_STATUS_BY_OUTCOME[outcome.status]
    reason = outcome.reason
    if outcome.status is OutcomeStatus.STRUCTURAL_ONLY:
        reason = STRUCTURAL_ONLY
    action = ""
    if report_status == "blocked":
        action = PROVIDER_ACTION if reason == PROVIDER_UNAVAILABLE else INCOMPLETE_ACTION
    return IngestReport(
        status=report_status,
        source=resolved.as_posix(),
        kind=kind.value,
        version_hash=outcome.version_hash,
        registered=registered,
        ingestion_status=outcome.status.value,
        provider=provider_name,
        reason=reason,
        action=action,
        details=payload,
    )


def ask(
    question: str,
    repo: Path,
    wiring: Wiring | None = None,
    registry: ProviderRegistry | None = None,
) -> AskReport:
    root = _checked_root(repo)
    composition = _composition(wiring, registry)
    session = Session.open(root)
    resolved = session.resolve_provider(composition.registry, composition.provider)
    with session.open_knowledge() as knowledge:
        entities, _, evidence, sources = _knowledge_counts(knowledge)
        if entities == 0 and evidence == 0 and sources == 0:
            return AskReport(
                status="blocked",
                question=question,
                provider=resolved.name,
                reason="knowledge_empty",
                action="run analyze or ingest first",
            )
        try:
            runner = composition.query_runner()
        except CapabilityUnavailable as exc:
            return AskReport(
                status="blocked",
                question=question,
                provider=resolved.name,
                reason=exc.reason,
                action=exc.action,
            )
        outcome = runner.run(
            question, knowledge, resolved.provider, session.namespace
        )
    report_status = ASK_STATUS_BY_ANSWER.get(outcome.status, "partial")
    return AskReport(
        status=report_status,
        question=question,
        answer=outcome.answer,
        mode=outcome.mode,
        provider=resolved.name,
        reason=outcome.reason,
        action="" if report_status == "ok" else ANSWER_ACTION,
        evidence_ids=outcome.evidence_ids,
        entity_ids=outcome.entity_ids,
        unresolved=outcome.unresolved,
    )


def publish(
    repo: Path,
    wiring: Wiring | None = None,
    registry: ProviderRegistry | None = None,
) -> PublishReport:
    root = _checked_root(repo)
    composition = _composition(wiring, registry)
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
        try:
            outcome = runner.run(knowledge, session.publications_dir, session.namespace)
        except PublicationBlocked as blocked:
            reasons = list(blocked.reasons)
            return PublishReport(
                status="blocked",
                reason=snake_case(reasons[0]) if reasons else "publication_blocked",
                action=PUBLICATION_ACTION,
                details={"reasons": reasons},
            )
    return PublishReport(
        status="ok",
        publication_id=outcome.publication_id,
        artifacts=outcome.artifacts,
        manifest_hash=outcome.manifest_hash,
    )


def provider_show(
    repo: Path,
    wiring: Wiring | None = None,
    registry: ProviderRegistry | None = None,
) -> ProviderReport:
    root = _checked_root(repo)
    composition = _composition(wiring, registry)
    session = Session.open(root)
    chosen, origin = session.preference_origin(composition.provider)
    return ProviderReport(
        status="ok",
        provider=chosen or "",
        source=origin,
        registered=composition.registry.registered(),
    )


def provider_set(
    name: str,
    repo: Path,
    wiring: Wiring | None = None,
    registry: ProviderRegistry | None = None,
) -> ProviderReport:
    root = _checked_root(repo)
    composition = _composition(wiring, registry)
    session = Session.open(root)
    known = composition.registry.registered()
    chosen = str(name).strip()
    if chosen not in known:
        return ProviderReport(
            status="error",
            provider=chosen,
            registered=known,
            reason=UNKNOWN_PROVIDER,
            action=UNKNOWN_PROVIDER_ACTION,
        )
    stored = session.write_provider_preference(chosen)
    return ProviderReport(
        status="ok",
        provider=stored,
        source=PREFERENCE_SOURCES[2],
        registered=known,
    )


def _sources_by_kind(
    knowledge: KnowledgeRepository, namespace: str
) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for record in knowledge.source_versions():
        if record.source_id == namespace:
            kind = SourceKind.CODEBASE.value
        else:
            kind = source_kind_for(Path(record.locator_root)).value
        counts[kind] = counts.get(kind, 0) + 1
    return _pairs(counts)


def status(
    repo: Path,
    wiring: Wiring | None = None,
    registry: ProviderRegistry | None = None,
) -> StatusReport:
    root = _checked_root(repo)
    composition = _composition(wiring, registry)
    session = Session.open(root)
    with session.open_knowledge() as knowledge:
        entities, relations, evidence, sources = _knowledge_counts(knowledge)
        by_kind = _sources_by_kind(knowledge, session.namespace)
    publication = current_publication(session.publications_dir)
    observed = observe(SnapshotSpec(root=root, excludes=DEFAULT_EXCLUDES)).digest
    state = session.record_observation(observed)
    return StatusReport(
        status="ok",
        root=session.repo.as_posix(),
        snapshot_digest=session.current_snapshot_id(),
        analysis_status=state.analysis_status.value,
        analyzed_digest=state.analyzed_digest,
        observed_digest=observed,
        entities=entities,
        relations=relations,
        evidence=evidence,
        sources=sources,
        sources_by_kind=by_kind,
        pending_update=state.analysis_status is not AnalysisStatus.NEVER
        and not state.is_current_for(observed, state.analyzed_objective_hash),
        last_publication=publication.publication_id if publication else None,
        publication_artifacts=len(publication.manifest.relative_paths) if publication else 0,
        provider_available=bool(composition.registry.available()),
    )
