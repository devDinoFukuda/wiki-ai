from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from .gaps import open_gap
from .model import Confidence, EntityId, KnowledgeState, SourceVersion
from .repository import KnowledgeRepository, RevisionTransaction

ENTITY_TARGET = "entity"
EVIDENCE_TARGET = "evidence"

REVALIDATION_EXEMPT: frozenset[KnowledgeState] = frozenset(
    {KnowledgeState.HISTORICAL}
)


@dataclass(frozen=True)
class Invalidation:
    revision_id: str
    obsolete_versions: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.entities) + len(self.evidence)


def obsolete_versions(
    repository: KnowledgeRepository, incoming: Sequence[SourceVersion]
) -> tuple[str, ...]:
    obsolete: set[str] = set()
    for version in incoming:
        for stored in repository.source_versions(version.source_id):
            if stored.version_hash != version.version_hash:
                obsolete.add(stored.key)
    return tuple(sorted(obsolete))


def dependents(
    repository: KnowledgeRepository, keys: Sequence[str]
) -> tuple[dict[str, str], dict[str, str]]:
    entities: dict[str, str] = {}
    evidence: dict[str, str] = {}
    for key in keys:
        for evidence_id in repository.evidence_using(key):
            evidence.setdefault(evidence_id, key)
        for entity_id in repository.entities_using(key):
            entity = repository.get_entity(EntityId(entity_id))
            if entity is None or entity.state in REVALIDATION_EXEMPT:
                continue
            entities.setdefault(entity_id, key)
    return entities, evidence


def invalidate(
    repository: KnowledgeRepository,
    incoming: Sequence[SourceVersion],
    author: str,
    summary: str = "",
) -> Invalidation:
    keys = obsolete_versions(repository, incoming)
    entities, evidence = dependents(repository, keys)
    reason = summary or f"invalidação por {len(keys)} versão(ões) substituída(s) de fonte"
    with repository.begin_revision(author, reason) as revision:
        for version in incoming:
            revision.put_source_version(version)
        _mark(revision, repository, entities, evidence)
        revision_id = revision.revision_id
    return Invalidation(
        revision_id=revision_id,
        obsolete_versions=keys,
        entities=tuple(sorted(entities)),
        evidence=tuple(sorted(evidence)),
    )


def _mark(
    transaction: RevisionTransaction,
    repository: KnowledgeRepository,
    entities: Mapping[str, str],
    evidence: Mapping[str, str],
) -> None:
    for evidence_id, key in sorted(evidence.items()):
        transaction.record_invalidation(EVIDENCE_TARGET, evidence_id, key)
    for entity_id, key in sorted(entities.items()):
        entity = repository.get_entity(EntityId(entity_id))
        if entity is None:
            continue
        transaction.put_entity(entity.with_confidence(Confidence.UNRESOLVED))
        transaction.record_invalidation(ENTITY_TARGET, entity_id, key)


RELATION_TARGET = "relation"


@dataclass(frozen=True)
class TargetedInvalidation:
    revision_id: str
    source_version_key: str
    entities: tuple[str, ...] = ()
    relations: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    historical: tuple[str, ...] = ()
    carried_over_entities: tuple[str, ...] = ()
    carried_over_evidence: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.entities) + len(self.relations) + len(self.evidence)


def entities_of_evidence(
    repository: KnowledgeRepository, evidence_ids: Sequence[str]
) -> tuple[str, ...]:
    wanted = set(evidence_ids)
    found: set[str] = set()
    for entity in repository.find_entities():
        for item in repository.evidence_for(entity.id):
            if item.id in wanted:
                found.add(entity.id.value)
                break
    return tuple(sorted(found))


def relations_of_entities(
    repository: KnowledgeRepository, entity_ids: Sequence[str]
) -> tuple[str, ...]:
    found: set[str] = set()
    for raw in entity_ids:
        for relation in repository.relations_of(EntityId(raw)):
            found.add(relation.id)
    return tuple(sorted(found))


def surviving_evidence(
    repository: KnowledgeRepository, entity_id: str, dropped: Sequence[str]
) -> tuple[str, ...]:
    lost = set(dropped)
    return tuple(
        item.id
        for item in repository.evidence_for(EntityId(entity_id))
        if item.id not in lost
    )


def apply_targeted(
    repository: KnowledgeRepository,
    incoming: SourceVersion,
    obsolete_key: str,
    evidence_ids: Sequence[str],
    entity_ids: Sequence[str],
    relation_ids: Sequence[str],
    historical_ids: Sequence[str],
    carried_entities: Sequence[str],
    carried_evidence: Sequence[str],
    gap_questions: Mapping[str, str],
    author: str,
    summary: str,
) -> TargetedInvalidation:
    historical = set(historical_ids)
    opened: list[str] = []
    with repository.begin_revision(author, summary) as revision:
        revision.put_source_version(incoming)
        for evidence_id in sorted(set(evidence_ids) | set(carried_evidence)):
            revision.record_invalidation(EVIDENCE_TARGET, evidence_id, obsolete_key)
        for entity_id in sorted(set(entity_ids) | set(carried_entities)):
            revision.record_invalidation(ENTITY_TARGET, entity_id, obsolete_key)
        for relation_id in sorted(set(relation_ids)):
            relation = repository.get_relation(relation_id)
            if relation is None:
                continue
            revision.put_relation(relation.with_confidence(Confidence.UNRESOLVED))
            revision.record_invalidation(RELATION_TARGET, relation_id, obsolete_key)
        for entity_id in sorted(set(entity_ids)):
            entity = repository.get_entity(EntityId(entity_id))
            if entity is None:
                continue
            demoted = entity.with_confidence(Confidence.UNRESOLVED)
            if entity_id in historical:
                demoted = replace(demoted, state=KnowledgeState.HISTORICAL)
            revision.put_entity(demoted)
            question = gap_questions.get(entity_id)
            if question:
                open_gap(revision, question, about=entity.id, blocking=False)
                opened.append(question)
        revision_id = revision.revision_id
    return TargetedInvalidation(
        revision_id=revision_id,
        source_version_key=obsolete_key,
        entities=tuple(sorted(set(entity_ids))),
        relations=tuple(sorted(set(relation_ids))),
        evidence=tuple(sorted(set(evidence_ids))),
        historical=tuple(sorted(historical & set(entity_ids))),
        carried_over_entities=tuple(sorted(set(carried_entities))),
        carried_over_evidence=tuple(sorted(set(carried_evidence))),
        gaps=tuple(dict.fromkeys(opened)),
    )
