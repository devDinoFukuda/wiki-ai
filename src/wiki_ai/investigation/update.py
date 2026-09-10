from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from wiki_ai.knowledge.evidence import CodeLocator
from wiki_ai.knowledge.invalidation import (
    TargetedInvalidation,
    apply_targeted,
    entities_of_evidence,
    relations_of_entities,
    surviving_evidence,
)
from wiki_ai.knowledge.model import EntityId, Evidence, SourceVersion
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.repository.snapshot import RepositorySnapshot, SnapshotDiff, diff

from wiki_ai.investigation.objective import Objective, ObjectiveKind, Scope, parse
from wiki_ai.investigation.orchestrator import (
    InvestigationOutcomeData,
    Investigator,
    SessionProvider,
)

__all__ = [
    "AUTHOR",
    "SKIPPED_NO_DIFF",
    "SKIPPED_NO_PROVIDER",
    "DependencyKey",
    "InvalidationReport",
    "UpdateOutcome",
    "UpdateEngine",
    "dependency_key",
    "impacted_evidence",
    "invalidate_findings",
    "plan_reinvestigation",
]

AUTHOR = "update"
SKIPPED_NO_DIFF = "no_change"
SKIPPED_NO_PROVIDER = "agent_provider_unavailable"

GAP_PREFIX = "behavior may have changed in "


@dataclass(frozen=True)
class DependencyKey:
    namespace: str
    path: str
    file_sha256: str

    def __str__(self) -> str:
        return f"{self.namespace}::{self.path}::{self.file_sha256}"


def dependency_key(
    namespace: str, path: str, snapshot: RepositorySnapshot
) -> DependencyKey | None:
    record = snapshot.file_map().get(path)
    if record is None:
        return None
    return DependencyKey(namespace=namespace, path=path, file_sha256=record.sha256)


@dataclass(frozen=True)
class InvalidationReport:
    revision_id: str = ""
    source_version_key: str = ""
    entities: tuple[str, ...] = ()
    relations: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    historical: tuple[str, ...] = ()
    carried_over_entities: tuple[str, ...] = ()
    carried_over_evidence: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.entities) + len(self.relations) + len(self.evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "source_version_key": self.source_version_key,
            "entities": list(self.entities),
            "relations": list(self.relations),
            "evidence": list(self.evidence),
            "historical": list(self.historical),
            "carried_over_entities": list(self.carried_over_entities),
            "carried_over_evidence": list(self.carried_over_evidence),
            "gaps": list(self.gaps),
            "keys": list(self.keys),
        }


@dataclass(frozen=True)
class UpdateOutcome:
    diff_summary: Mapping[str, Any]
    invalidated: InvalidationReport
    reinvestigation: InvestigationOutcomeData | None = None
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "diff": dict(self.diff_summary),
            "invalidated": self.invalidated.to_dict(),
            "skipped_reason": self.skipped_reason,
        }
        if self.reinvestigation is not None:
            payload["reinvestigation"] = {
                "objective": self.reinvestigation.objective,
                "entities_written": self.reinvestigation.entities_written,
                "relations_written": self.reinvestigation.relations_written,
                "evidence_written": self.reinvestigation.evidence_written,
                "unresolved": list(self.reinvestigation.unresolved),
            }
        return payload


@runtime_checkable
class ScopedInvestigator(Protocol):
    def run(
        self,
        objective: str,
        snapshot: RepositorySnapshot,
        knowledge: KnowledgeRepository,
        provider: SessionProvider,
        namespace: str,
    ) -> InvestigationOutcomeData: ...


def _code_paths(evidence: Evidence) -> str:
    locator = evidence.locator
    return locator.path if isinstance(locator, CodeLocator) else ""


def impacted_evidence(
    knowledge: KnowledgeRepository,
    previous_snapshot: RepositorySnapshot,
    current_snapshot: RepositorySnapshot,
    namespace: str,
) -> tuple[Evidence, ...]:
    changes = diff(previous_snapshot, current_snapshot)
    touched = set(changes.changed) | set(changes.removed)
    found: list[Evidence] = []
    for evidence_id, _key in knowledge.all_evidence_keys():
        evidence = knowledge.get_evidence(evidence_id)
        if evidence is None:
            continue
        if evidence.source_id != namespace:
            continue
        if evidence.version_hash != previous_snapshot.digest:
            continue
        path = _code_paths(evidence)
        if path and path in touched:
            found.append(evidence)
    return tuple(sorted(found, key=lambda item: item.id))


def _all_namespace_evidence(
    knowledge: KnowledgeRepository, previous_snapshot: RepositorySnapshot, namespace: str
) -> tuple[Evidence, ...]:
    found: list[Evidence] = []
    for evidence_id, _key in knowledge.all_evidence_keys():
        evidence = knowledge.get_evidence(evidence_id)
        if evidence is None:
            continue
        if evidence.source_id != namespace:
            continue
        if evidence.version_hash != previous_snapshot.digest:
            continue
        found.append(evidence)
    return tuple(found)


def invalidate_findings(
    knowledge: KnowledgeRepository,
    impacted: Sequence[Evidence],
    previous_snapshot: RepositorySnapshot,
    current_snapshot: RepositorySnapshot,
    namespace: str,
) -> InvalidationReport:
    changes = diff(previous_snapshot, current_snapshot)
    removed = set(changes.removed)
    previous_version = SourceVersion(
        source_id=namespace,
        version_hash=previous_snapshot.digest,
        locator_root=previous_snapshot.root,
        captured_at=previous_snapshot.taken_at,
    )
    current_version = SourceVersion(
        source_id=namespace,
        version_hash=current_snapshot.digest,
        locator_root=current_snapshot.root,
        captured_at=current_snapshot.taken_at,
    )
    impacted_ids = tuple(item.id for item in impacted)
    entity_ids = entities_of_evidence(knowledge, impacted_ids)
    relation_ids = relations_of_entities(knowledge, entity_ids)
    path_by_entity = _paths_by_entity(knowledge, impacted, entity_ids)
    historical = tuple(
        entity_id
        for entity_id in entity_ids
        if not surviving_evidence(knowledge, entity_id, impacted_ids)
        and _only_removed(path_by_entity.get(entity_id, ()), removed)
    )
    questions = {
        entity_id: GAP_PREFIX + ", ".join(path_by_entity.get(entity_id, ()))
        for entity_id in entity_ids
        if path_by_entity.get(entity_id)
    }
    everything = _all_namespace_evidence(knowledge, previous_snapshot, namespace)
    carried_evidence = tuple(
        item.id for item in everything if item.id not in set(impacted_ids)
    )
    carried_entities = tuple(
        entity_id
        for entity_id in entities_of_evidence(knowledge, carried_evidence)
        if entity_id not in set(entity_ids)
    )
    summary = (
        f"update {previous_snapshot.digest[:12]} -> {current_snapshot.digest[:12]}: "
        f"{len(impacted_ids)} evidence over {len(changes.changed) + len(changes.removed)} file(s)"
    )
    applied = apply_targeted(
        repository=knowledge,
        incoming=current_version,
        obsolete_key=previous_version.key,
        evidence_ids=impacted_ids,
        entity_ids=entity_ids,
        relation_ids=relation_ids,
        historical_ids=historical,
        carried_entities=carried_entities,
        carried_evidence=carried_evidence,
        gap_questions=questions,
        author=AUTHOR,
        summary=summary,
    )
    return _report_of(applied, impacted, namespace, previous_snapshot)


def _only_removed(paths: Sequence[str], removed: set[str]) -> bool:
    return bool(paths) and all(path in removed for path in paths)


def _paths_by_entity(
    knowledge: KnowledgeRepository,
    impacted: Sequence[Evidence],
    entity_ids: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    by_id = {item.id: _code_paths(item) for item in impacted}
    result: dict[str, tuple[str, ...]] = {}
    for entity_id in entity_ids:
        paths: list[str] = []
        for evidence in knowledge.evidence_for(EntityId(entity_id)):
            path = by_id.get(evidence.id, "")
            if path and path not in paths:
                paths.append(path)
        result[entity_id] = tuple(sorted(paths))
    return result


def _report_of(
    applied: TargetedInvalidation,
    impacted: Sequence[Evidence],
    namespace: str,
    previous_snapshot: RepositorySnapshot,
) -> InvalidationReport:
    keys: list[str] = []
    for evidence in impacted:
        path = _code_paths(evidence)
        key = dependency_key(namespace, path, previous_snapshot) if path else None
        if key is not None and str(key) not in keys:
            keys.append(str(key))
    return InvalidationReport(
        revision_id=applied.revision_id,
        source_version_key=applied.source_version_key,
        entities=applied.entities,
        relations=applied.relations,
        evidence=applied.evidence,
        historical=applied.historical,
        carried_over_entities=applied.carried_over_entities,
        carried_over_evidence=applied.carried_over_evidence,
        gaps=applied.gaps,
        keys=tuple(keys),
    )


def plan_reinvestigation(
    report: InvalidationReport, changes: SnapshotDiff
) -> Objective:
    paths = tuple(dict.fromkeys(changes.changed + changes.added))
    text = "reinvestigate " + (" ".join(paths) if paths else "repository")
    return parse(
        text,
        kind=ObjectiveKind.TARGETED_REINVESTIGATION,
        scope=Scope(paths=paths, entities=report.entities),
    )


class UpdateEngine:
    def __init__(self, investigator: ScopedInvestigator | None = None) -> None:
        self._investigator = investigator or Investigator()

    def run(
        self,
        previous_snapshot: RepositorySnapshot,
        current_snapshot: RepositorySnapshot,
        knowledge: KnowledgeRepository,
        provider: SessionProvider | None,
        namespace: str,
    ) -> UpdateOutcome:
        changes = diff(previous_snapshot, current_snapshot)
        summary = changes.to_dict()
        if changes.is_empty():
            return UpdateOutcome(
                diff_summary=summary,
                invalidated=InvalidationReport(),
                skipped_reason=SKIPPED_NO_DIFF,
            )
        impacted = impacted_evidence(
            knowledge, previous_snapshot, current_snapshot, namespace
        )
        report = invalidate_findings(
            knowledge, impacted, previous_snapshot, current_snapshot, namespace
        )
        if provider is None:
            return UpdateOutcome(
                diff_summary=summary,
                invalidated=report,
                skipped_reason=SKIPPED_NO_PROVIDER,
            )
        objective = plan_reinvestigation(report, changes)
        outcome = self._investigator.run(
            objective.text,
            current_snapshot,
            knowledge,
            provider,
            namespace,
        )
        return UpdateOutcome(
            diff_summary=summary,
            invalidated=report,
            reinvestigation=outcome,
            skipped_reason="",
        )
