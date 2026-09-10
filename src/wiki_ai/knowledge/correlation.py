from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .contradiction import contradiction_between, payload_of
from .gaps import open_gap
from .matching import (
    DEFAULT_AMBIGUITY_MARGIN,
    DEFAULT_THRESHOLD,
    aliases_of,
    score_names,
    subject_of,
    tokens,
)
from .model import Confidence, Entity, EntityId, KnowledgeState, Relation
from .repository import KnowledgeRepository, RevisionTransaction
from .taxonomy import EntityKind, RelationKind, entity_kind, pair_allowed

__all__ = [
    "AMBIGUOUS_PREFIX",
    "BASIS_ALIAS",
    "BASIS_CROSS_OWNER",
    "BASIS_IMPACT",
    "BASIS_STABLE_KEY",
    "CORRELATED_BY",
    "CORRELATION_AUTHOR",
    "CROSS_OWNER_PENALTY",
    "CorrelatedRelation",
    "CorrelationReport",
    "LEFT_SOURCE",
    "RIGHT_SOURCE",
    "SAME_OWNER",
    "SCORE",
    "correlate",
]

CORRELATION_AUTHOR = "correlation"

BASIS_STABLE_KEY = "stable_key"
BASIS_ALIAS = "alias"
BASIS_IMPACT = "impact"
BASIS_CROSS_OWNER = "cross_owner"

CROSS_OWNER_PENALTY = 0.75

SAME_OWNER = "same_owner"

CORRELATED_BY = "correlated_by"
SCORE = "score"
LEFT_SOURCE = "left_source_id"
RIGHT_SOURCE = "right_source_id"

AMBIGUOUS_PREFIX = "ambiguous correlation between "

DECLARING_KINDS: tuple[EntityKind, ...] = (
    EntityKind.REQUIREMENT,
    EntityKind.DECISION_RECORD,
)

DECLARABLE_KINDS: tuple[EntityKind, ...] = (
    EntityKind.CAPABILITY,
    EntityKind.BUSINESS_RULE,
    EntityKind.INTEGRATION,
)

PROPOSING_KINDS: tuple[EntityKind, ...] = (
    EntityKind.PROPOSAL,
    EntityKind.INITIATIVE,
)

AFFECTABLE_KINDS: tuple[EntityKind, ...] = (
    EntityKind.CAPABILITY,
    EntityKind.INTEGRATION,
    EntityKind.BUSINESS_RULE,
    EntityKind.TOPIC,
)

DOCUMENTARY_STATUS: tuple[KnowledgeState, ...] = (
    KnowledgeState.DECLARED,
    KnowledgeState.PROPOSED,
)

DATE_ATTRIBUTES: tuple[str, ...] = ("decided_at", "date", "occurred_at", "recorded_at")

IMPACT_RELATIONS: tuple[str, ...] = (
    RelationKind.DEPENDS_ON.value,
    RelationKind.CALLS.value,
    RelationKind.CONSUMES.value,
    RelationKind.PUBLISHES.value,
    RelationKind.WRITES.value,
    RelationKind.VALIDATES.value,
    RelationKind.BELONGS_TO.value,
)

MAX_IMPACT_DEPTH = 4


@dataclass(frozen=True)
class CorrelatedRelation:
    kind: str
    source_id: str
    target_id: str
    basis: str
    score: float
    left_source_id: str
    right_source_id: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrelationReport:
    relations: tuple[CorrelatedRelation, ...] = ()
    gaps: tuple[str, ...] = ()
    revision_id: str = ""
    threshold: float = DEFAULT_THRESHOLD
    counts: Mapping[str, int] = field(default_factory=dict)

    def of_kind(self, kind: str | RelationKind) -> tuple[CorrelatedRelation, ...]:
        wanted = kind.value if isinstance(kind, RelationKind) else str(kind)
        return tuple(item for item in self.relations if item.kind == wanted)

    @property
    def total(self) -> int:
        return len(self.relations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relations": [
                {
                    "kind": item.kind,
                    "basis": item.basis,
                    "score": item.score,
                }
                for item in self.relations
            ],
            "gaps": list(self.gaps),
            "counts": dict(self.counts),
            "threshold": self.threshold,
        }


@dataclass(frozen=True)
class _Candidate:
    entity: Entity
    score: float
    basis: str
    rank: int = 0


class _Index:
    def __init__(self, repository: KnowledgeRepository) -> None:
        self._repo = repository
        self._entities = tuple(repository.find_entities())
        self._by_kind: dict[str, list[Entity]] = {}
        self._aliases: dict[str, tuple[str, ...]] = {}
        for entity in self._entities:
            self._by_kind.setdefault(entity.kind, []).append(entity)
            self._aliases[entity.id.value] = aliases_of(entity.name, entity.attributes)

    @property
    def entities(self) -> tuple[Entity, ...]:
        return self._entities

    def of_kinds(self, kinds: Sequence[EntityKind]) -> tuple[Entity, ...]:
        found: list[Entity] = []
        for kind in kinds:
            found.extend(self._by_kind.get(kind.value, ()))
        return tuple(sorted(found, key=lambda item: item.id.value))

    def aliases(self, entity: Entity) -> tuple[str, ...]:
        return self._aliases.get(entity.id.value, (entity.name,))

    def source_id(self, entity: Entity) -> str:
        evidences = self._repo.evidence_for(entity.id)
        if evidences:
            return evidences[0].source_id
        keys = entity.source_versions
        return keys[0] if keys else ""


def _same_owner(left: Entity, right: Entity) -> bool:
    return _owner_of(left) == _owner_of(right)


def _owner_of(entity: Entity) -> str:
    return entity.owner_id.value if entity.owner_id is not None else ""


def _match(index: _Index, left: Entity, right: Entity) -> tuple[float, str]:
    best = score_names(subject_of(left.name), right.canonical_name)
    basis = BASIS_STABLE_KEY
    for first in index.aliases(left):
        for second in index.aliases(right):
            score = score_names(first, second)
            if score > best:
                best = score
                basis = BASIS_ALIAS
    if _same_owner(left, right):
        return best, basis
    return round(best * CROSS_OWNER_PENALTY, 4), BASIS_CROSS_OWNER


def _candidates(
    index: _Index,
    subject: Entity,
    pool: Sequence[Entity],
    threshold: float,
    preference: Sequence[EntityKind] = (),
) -> tuple[_Candidate, ...]:
    ranking = {kind.value: position for position, kind in enumerate(preference)}
    found: list[_Candidate] = []
    for other in pool:
        if other.id.value == subject.id.value:
            continue
        score, basis = _match(index, subject, other)
        if score >= threshold:
            found.append(
                _Candidate(
                    entity=other,
                    score=score,
                    basis=basis,
                    rank=ranking.get(other.kind, len(ranking)),
                )
            )
    found.sort(key=lambda item: (-item.score, item.rank, item.entity.id.value))
    return tuple(found)


def _ambiguous(candidates: Sequence[_Candidate], margin: float) -> bool:
    if len(candidates) < 2:
        return False
    if candidates[0].rank != candidates[1].rank:
        return False
    return abs(candidates[0].score - candidates[1].score) <= margin


def _date_of(entity: Entity) -> str:
    for name in DATE_ATTRIBUTES:
        value = entity.attributes.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _reachable(
    repository: KnowledgeRepository, origin: EntityId, kinds: Sequence[EntityKind]
) -> tuple[Entity, ...]:
    allowed = {kind.value for kind in kinds}
    seen: set[str] = {origin.value}
    frontier: list[tuple[str, int]] = [(origin.value, 0)]
    found: list[Entity] = []
    while frontier:
        node, depth = frontier.pop(0)
        if depth >= MAX_IMPACT_DEPTH:
            continue
        for relation in repository.relations_of(
            EntityId(node), "both", IMPACT_RELATIONS
        ):
            for other in (relation.source_id.value, relation.target_id.value):
                if other in seen:
                    continue
                seen.add(other)
                entity = repository.get_entity(EntityId(other))
                if entity is None:
                    continue
                frontier.append((other, depth + 1))
                if entity.kind in allowed:
                    found.append(entity)
    return tuple(sorted(found, key=lambda item: item.id.value))


class _Writer:
    def __init__(
        self,
        transaction: RevisionTransaction,
        repository: KnowledgeRepository,
        index: _Index,
    ) -> None:
        self._transaction = transaction
        self._repo = repository
        self._index = index
        self._written: dict[str, CorrelatedRelation] = {}

    @property
    def written(self) -> tuple[CorrelatedRelation, ...]:
        return tuple(self._written[key] for key in sorted(self._written))

    def targets_of(self, kind: RelationKind, source_id: str) -> tuple[str, ...]:
        return tuple(
            item.target_id
            for item in self.written
            if item.kind == kind.value and item.source_id == source_id
        )

    def add(
        self,
        kind: RelationKind,
        left: Entity,
        right: Entity,
        basis: str,
        score: float,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if left.id.value == right.id.value:
            return
        if not pair_allowed(kind, entity_kind(left.kind), entity_kind(right.kind)):
            return
        left_source = self._index.source_id(left)
        right_source = self._index.source_id(right)
        attributes: dict[str, Any] = {
            CORRELATED_BY: basis,
            SCORE: round(float(score), 4),
            LEFT_SOURCE: left_source,
            RIGHT_SOURCE: right_source,
            SAME_OWNER: _same_owner(left, right),
        }
        attributes.update(dict(extra or {}))
        relation = Relation.create(
            kind=kind.value,
            source_id=left.id,
            target_id=right.id,
            attributes=attributes,
            confidence=Confidence.INFERRED,
        )
        if relation.id in self._written:
            return
        self._transaction.put_relation(relation)
        evidence_ids = self._inherit(relation.id, left, right)
        self._written[relation.id] = CorrelatedRelation(
            kind=relation.kind,
            source_id=left.id.value,
            target_id=right.id.value,
            basis=basis,
            score=float(attributes[SCORE]),
            left_source_id=left_source,
            right_source_id=right_source,
            evidence_ids=evidence_ids,
        )

    def _inherit(
        self, relation_id: str, left: Entity, right: Entity
    ) -> tuple[str, ...]:
        collected: list[str] = []
        for entity in (left, right):
            for evidence in self._repo.evidence_for(entity.id):
                if evidence.id in collected:
                    continue
                collected.append(evidence.id)
                self._transaction.put_evidence(evidence, relation_ids=(relation_id,))
        return tuple(sorted(collected))


def correlate(
    knowledge: KnowledgeRepository,
    namespace: str,
    *,
    author: str = CORRELATION_AUTHOR,
    threshold: float = DEFAULT_THRESHOLD,
    ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
) -> CorrelationReport:
    index = _Index(knowledge)
    implemented = tuple(
        entity
        for entity in index.entities
        if entity.state is KnowledgeState.IMPLEMENTED
    )
    questions: list[str] = []
    with knowledge.begin_revision(
        author=author, summary=f"{namespace}:correlation"
    ) as transaction:
        writer = _Writer(transaction, knowledge, index)
        _declares(index, writer, implemented, threshold, ambiguity_margin, questions)
        _proposes(index, writer, implemented, threshold, ambiguity_margin, questions)
        _affects(knowledge, index, writer)
        _contradicts(index, writer, threshold)
        _supersedes(index, writer, threshold)
        for question in dict.fromkeys(questions):
            open_gap(transaction, question, blocking=False)
        revision_id = transaction.revision_id
        relations = writer.written
    counts: dict[str, int] = {}
    for item in relations:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return CorrelationReport(
        relations=relations,
        gaps=tuple(dict.fromkeys(questions)),
        revision_id=revision_id,
        threshold=threshold,
        counts=counts,
    )


def _documentary(index: _Index, kinds: Sequence[EntityKind]) -> tuple[Entity, ...]:
    return tuple(
        entity
        for entity in index.of_kinds(kinds)
        if entity.state in DOCUMENTARY_STATUS
    )


def _record_ambiguity(
    subject: Entity, candidates: Sequence[_Candidate], questions: list[str]
) -> None:
    names = ", ".join(sorted(item.entity.name for item in candidates[:3]))
    questions.append(f"{AMBIGUOUS_PREFIX}{subject.name} and {names}")


def _declares(
    index: _Index,
    writer: _Writer,
    implemented: Sequence[Entity],
    threshold: float,
    margin: float,
    questions: list[str],
) -> None:
    pool = index.of_kinds(DECLARABLE_KINDS)
    implemented_ids = {entity.id.value for entity in implemented}
    for declarer in _documentary(index, DECLARING_KINDS):
        candidates = _candidates(
            index, declarer, pool, threshold, DECLARABLE_KINDS
        )
        candidates = _prefer_implemented(candidates, implemented_ids)
        if not candidates:
            continue
        if _ambiguous(candidates, margin):
            _record_ambiguity(declarer, candidates, questions)
            continue
        best = candidates[0]
        writer.add(RelationKind.DECLARES, declarer, best.entity, best.basis, best.score)


def _prefer_implemented(
    candidates: Sequence[_Candidate], implemented_ids: set[str]
) -> tuple[_Candidate, ...]:
    supported = tuple(
        item for item in candidates if item.entity.id.value in implemented_ids
    )
    return supported or tuple(candidates)


def _proposes(
    index: _Index,
    writer: _Writer,
    implemented: Sequence[Entity],
    threshold: float,
    margin: float,
    questions: list[str],
) -> None:
    for proposal in _documentary(index, PROPOSING_KINDS):
        candidates = _candidates(
            index, proposal, implemented, threshold, AFFECTABLE_KINDS
        )
        if not candidates:
            continue
        if _ambiguous(candidates, margin):
            _record_ambiguity(proposal, candidates, questions)
            continue
        best = candidates[0]
        writer.add(
            RelationKind.PROPOSES_CHANGE_TO,
            proposal,
            best.entity,
            best.basis,
            best.score,
        )


def _anchors(
    writer: _Writer, repository: KnowledgeRepository, origin: Entity
) -> tuple[EntityId, ...]:
    found: list[str] = list(
        writer.targets_of(RelationKind.PROPOSES_CHANGE_TO, origin.id.value)
    )
    for relation in repository.relations_of(
        origin.id, "out", (RelationKind.PROPOSES_CHANGE_TO.value,)
    ):
        found.append(relation.target_id.value)
    return tuple(EntityId(value) for value in dict.fromkeys(found))


def _affects(repository: KnowledgeRepository, index: _Index, writer: _Writer) -> None:
    allowed = {kind.value for kind in AFFECTABLE_KINDS}
    for origin in _documentary(index, PROPOSING_KINDS):
        reached: dict[str, Entity] = {}
        for anchor in _anchors(writer, repository, origin):
            node = repository.get_entity(anchor)
            if node is not None and node.kind in allowed:
                reached.setdefault(node.id.value, node)
            for entity in _reachable(repository, anchor, AFFECTABLE_KINDS):
                reached.setdefault(entity.id.value, entity)
        for key in sorted(reached):
            writer.add(RelationKind.AFFECTS, origin, reached[key], BASIS_IMPACT, 1.0)


def _pair_score(index: _Index, left: Entity, right: Entity) -> float:
    best = score_names(left.canonical_name, right.canonical_name)
    for first in index.aliases(left):
        for second in index.aliases(right):
            best = max(best, score_names(first, second))
    if _same_owner(left, right):
        return best
    return round(best * CROSS_OWNER_PENALTY, 4)


def _contradicts(index: _Index, writer: _Writer, threshold: float) -> None:
    by_kind: dict[str, list[Entity]] = {}
    for entity in index.entities:
        by_kind.setdefault(entity.kind, []).append(entity)
    for kind in sorted(by_kind):
        group = sorted(by_kind[kind], key=lambda item: item.id.value)
        for position, left in enumerate(group):
            for right in group[position + 1 :]:
                left_source = index.source_id(left)
                right_source = index.source_id(right)
                if left_source and left_source == right_source:
                    continue
                score = _pair_score(index, left, right)
                if score < threshold:
                    continue
                found = contradiction_between(left, right)
                if found is None:
                    continue
                writer.add(
                    RelationKind.CONTRADICTS,
                    left,
                    right,
                    BASIS_STABLE_KEY if score >= 1.0 else BASIS_ALIAS,
                    score,
                    payload_of(found),
                )


def _statement(entity: Entity) -> str:
    for name in ("statement", "decision", "subject"):
        value = entity.attributes.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return entity.name


def _subject_score(index: _Index, left: Entity, right: Entity) -> float:
    best = _pair_score(index, left, right)
    left_subject = " ".join(tokens(_statement(left)))
    right_subject = " ".join(tokens(_statement(right)))
    if left_subject and right_subject:
        best = max(best, score_names(left_subject, right_subject))
    return best


def _supersedes(index: _Index, writer: _Writer, threshold: float) -> None:
    records = _documentary(index, (EntityKind.DECISION_RECORD,))
    earlier = _documentary(index, (EntityKind.PROPOSAL, EntityKind.DECISION_RECORD))
    for record in records:
        record_date = _date_of(record)
        if not record_date:
            continue
        for other in earlier:
            if other.id.value == record.id.value:
                continue
            other_date = _date_of(other)
            if not other_date or other_date >= record_date:
                continue
            score = _subject_score(index, record, other)
            if score < threshold:
                continue
            writer.add(
                RelationKind.SUPERSEDES,
                record,
                other,
                BASIS_STABLE_KEY,
                score,
                {"newer_date": record_date, "older_date": other_date},
            )
