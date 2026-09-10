from __future__ import annotations

from typing import Any, Mapping, Sequence

from .errors import GapNotFound
from .model import Confidence, Entity, EntityId, EpistemicStatus, Relation
from .repository import KnowledgeRepository, RevisionTransaction
from .taxonomy import EntityKind, RelationKind

GAP_KIND = EntityKind.GAP.value
GAP_OPEN = "open"
GAP_CLOSED = "closed"
GAP_STATUS = "status"
GAP_QUESTION = "question"
GAP_BLOCKING = "blocking"
GAP_RESOLUTION = "resolution"


def gap_key(question: str, about: EntityId | None) -> str:
    anchor = about.value if about is not None else "global"
    return f"{anchor}::{question.strip()}"


def make_gap(
    question: str,
    about: EntityId | None = None,
    blocking: bool = False,
    attributes: Mapping[str, Any] | None = None,
) -> Entity:
    payload: dict[str, Any] = dict(attributes or {})
    payload[GAP_QUESTION] = question
    payload[GAP_BLOCKING] = bool(blocking)
    payload.setdefault(GAP_STATUS, GAP_OPEN)
    return Entity.create(
        kind=GAP_KIND,
        name=question,
        stable_key=gap_key(question, about),
        attributes=payload,
        epistemic=EpistemicStatus.DECLARED,
        confidence=Confidence.UNRESOLVED,
    )


def open_gap(
    transaction: RevisionTransaction,
    question: str,
    about: EntityId | None = None,
    blocking: bool = False,
    attributes: Mapping[str, Any] | None = None,
) -> EntityId:
    gap = make_gap(question, about, blocking, attributes)
    transaction.put_entity(gap)
    if about is not None:
        transaction.put_relation(
            Relation.create(RelationKind.AFFECTS.value, gap.id, about)
        )
    return gap.id


def close_gap(
    transaction: RevisionTransaction,
    repository: KnowledgeRepository,
    gap_id: EntityId,
    resolution: str,
) -> EntityId:
    stored = repository.get_entity(gap_id)
    if stored is None or stored.kind != GAP_KIND:
        raise GapNotFound(f"gap inexistente ou de outro kind: {gap_id.value}")
    payload = dict(stored.attributes)
    payload[GAP_STATUS] = GAP_CLOSED
    payload[GAP_RESOLUTION] = resolution
    payload[GAP_BLOCKING] = False
    transaction.put_entity(
        Entity(
            id=stored.id,
            kind=stored.kind,
            name=stored.name,
            attributes=payload,
            epistemic=stored.epistemic,
            confidence=stored.confidence,
            source_versions=stored.source_versions,
        )
    )
    return stored.id


def is_open(gap: Entity) -> bool:
    return str(gap.attributes.get(GAP_STATUS, GAP_OPEN)) == GAP_OPEN


def is_blocking(gap: Entity) -> bool:
    return bool(gap.attributes.get(GAP_BLOCKING)) and is_open(gap)


def open_gaps(repository: KnowledgeRepository) -> tuple[Entity, ...]:
    return tuple(
        gap for gap in repository.find_entities(GAP_KIND) if is_open(gap)
    )


def blocking_gaps(repository: KnowledgeRepository) -> tuple[Entity, ...]:
    return tuple(gap for gap in open_gaps(repository) if is_blocking(gap))


def gaps_about(
    repository: KnowledgeRepository, entity_id: EntityId
) -> tuple[Entity, ...]:
    found: list[Entity] = []
    for relation in repository.relations_of(
        entity_id, "in", (RelationKind.AFFECTS.value,)
    ):
        candidate = repository.get_entity(relation.source_id)
        if candidate is not None and candidate.kind == GAP_KIND:
            found.append(candidate)
    return tuple(found)


def questions(gaps: Sequence[Entity]) -> tuple[str, ...]:
    return tuple(str(gap.attributes.get(GAP_QUESTION, gap.name)) for gap in gaps)
