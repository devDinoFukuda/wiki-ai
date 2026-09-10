from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from wiki_ai.knowledge.evidence import supports_implemented
from wiki_ai.knowledge.model import (
    Confidence,
    Entity,
    EntityId,
    KnowledgeState,
    Relation,
)
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import RevisionTransaction
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind, pair_allowed

__all__ = [
    "FLOW_RELATIONS",
    "MAX_DEPTH",
    "StepSpec",
    "DecisionSpec",
    "FlowSpec",
    "build_flows",
    "write_flows",
]

FLOW_RELATIONS: tuple[RelationKind, ...] = (
    RelationKind.VALIDATES,
    RelationKind.CALLS,
    RelationKind.TRIGGERS,
    RelationKind.CONSUMES,
    RelationKind.PERSISTS_TO,
    RelationKind.WRITES,
    RelationKind.READS,
    RelationKind.PUBLISHES,
)

MAX_DEPTH = 12

DERIVED_FROM = "derived_from"

_ORDER: Mapping[str, int] = {
    RelationKind.VALIDATES.value: 0,
    RelationKind.CONSUMES.value: 1,
    RelationKind.CALLS.value: 2,
    RelationKind.TRIGGERS.value: 3,
    RelationKind.READS.value: 4,
    RelationKind.WRITES.value: 5,
    RelationKind.PERSISTS_TO.value: 6,
    RelationKind.PUBLISHES.value: 7,
}

_ENTRY_ORDINAL = 0
_INBOUND = (RelationKind.VALIDATES,)


@dataclass(frozen=True)
class StepSpec:
    ordinal: int
    subject: str
    subject_id: str
    via: str
    entity_kind: str
    evidence_ids: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return bool(self.evidence_ids)

    @property
    def stable_key(self) -> str:
        return f"{self.subject_id}::{self.ordinal}::{self.via}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "subject": self.subject,
            "via": self.via,
            "entity_kind": self.entity_kind,
            "evidence": list(self.evidence_ids),
            "resolved": self.resolved,
        }


@dataclass(frozen=True)
class DecisionSpec:
    subject: str
    criteria: tuple[str, ...]
    outcomes: tuple[str, ...]
    source_id: str
    evidence_ids: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return bool(self.evidence_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "criteria": list(self.criteria),
            "outcomes": list(self.outcomes),
            "evidence": list(self.evidence_ids),
            "resolved": self.resolved,
        }


@dataclass(frozen=True)
class FlowSpec:
    capability_id: str
    capability_name: str
    entrypoint_id: str
    entrypoint_name: str
    steps: tuple[StepSpec, ...] = ()
    decisions: tuple[DecisionSpec, ...] = ()

    @property
    def name(self) -> str:
        return f"{self.capability_name} via {self.entrypoint_name}"

    @property
    def stable_key(self) -> str:
        return f"flow::{self.capability_id}::{self.entrypoint_id}"

    @property
    def resolved(self) -> bool:
        return all(step.resolved for step in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability_name,
            "entrypoint": self.entrypoint_name,
            "name": self.name,
            "steps": [step.to_dict() for step in self.steps],
            "decisions": [item.to_dict() for item in self.decisions],
            "resolved": self.resolved,
        }


def build_flows(
    query: KnowledgeQuery, capability_id: EntityId, max_depth: int = MAX_DEPTH
) -> tuple[FlowSpec, ...]:
    profile = query.capability_profile(capability_id)
    if profile is None:
        return ()
    entrypoints = sorted(profile.entrypoints, key=lambda item: (item.name, item.id.value))
    found: list[FlowSpec] = []
    for entrypoint in entrypoints:
        steps = _walk(query, entrypoint, max_depth)
        decisions = _decisions(query, profile.rules + profile.decisions)
        found.append(
            FlowSpec(
                capability_id=capability_id.value,
                capability_name=profile.capability.name,
                entrypoint_id=entrypoint.id.value,
                entrypoint_name=entrypoint.name,
                steps=steps,
                decisions=decisions,
            )
        )
    return tuple(found)


def write_flows(
    transaction: RevisionTransaction,
    query: KnowledgeQuery,
    capability_id: EntityId,
    flows: Sequence[FlowSpec],
) -> tuple[EntityId, ...]:
    written: list[EntityId] = []
    for flow in flows:
        flow_entity = Entity.create(
            kind=EntityKind.FLOW.value,
            name=flow.name,
            stable_key=flow.stable_key,
            attributes={
                "entrypoint": flow.entrypoint_name,
                "capability": flow.capability_name,
                "step_count": len(flow.steps),
            },
            state=KnowledgeState.IMPLEMENTED,
            confidence=Confidence.INFERRED if flow.resolved else Confidence.UNRESOLVED,
        )
        transaction.put_entity(flow_entity)
        transaction.put_relation(
            Relation.create(
                RelationKind.BELONGS_TO.value,
                flow_entity.id,
                capability_id,
                confidence=Confidence.INFERRED,
            )
        )
        written.append(flow_entity.id)
        _write_steps(transaction, query, flow, flow_entity.id)
        _write_decisions(transaction, query, flow, flow_entity.id, capability_id)
    return tuple(written)


def _write_steps(
    transaction: RevisionTransaction,
    query: KnowledgeQuery,
    flow: FlowSpec,
    flow_id: EntityId,
) -> None:
    previous: EntityId | None = None
    for step in flow.steps:
        entity = Entity.create(
            kind=EntityKind.FLOW_STEP.value,
            name=f"{flow.name} step {step.ordinal}: {step.subject}",
            stable_key=f"{flow.stable_key}::{step.stable_key}",
            attributes={
                "ordinal": step.ordinal,
                "via": step.via,
                "subject": step.subject,
                "subject_kind": step.entity_kind,
            },
            state=KnowledgeState.IMPLEMENTED,
            confidence=Confidence.SUPPORTED if step.resolved else Confidence.UNRESOLVED,
        )
        transaction.put_entity(entity)
        transaction.put_relation(
            Relation.create(
                RelationKind.BELONGS_TO.value,
                entity.id,
                flow_id,
                confidence=Confidence.INFERRED,
            )
        )
        target = EntityId(step.subject_id)
        if _pair_ok(RelationKind.CALLS, EntityKind.FLOW_STEP, query, target):
            transaction.put_relation(
                Relation.create(
                    RelationKind.CALLS.value,
                    entity.id,
                    target,
                    attributes={"via": step.via},
                    confidence=Confidence.INFERRED,
                )
            )
        if previous is not None:
            transaction.put_relation(
                Relation.create(
                    RelationKind.TRIGGERS.value,
                    previous,
                    entity.id,
                    attributes={"ordinal": step.ordinal},
                    confidence=Confidence.INFERRED,
                )
            )
        _inherit(transaction, query, step.evidence_ids, entity.id)
        previous = entity.id


def _write_decisions(
    transaction: RevisionTransaction,
    query: KnowledgeQuery,
    flow: FlowSpec,
    flow_id: EntityId,
    capability_id: EntityId,
) -> None:
    for decision in flow.decisions:
        entity = Entity.create(
            kind=EntityKind.DECISION.value,
            name=decision.subject,
            stable_key=f"{flow.stable_key}::decision::{decision.source_id}",
            attributes={
                "criteria": list(decision.criteria),
                "outcomes": list(decision.outcomes),
                DERIVED_FROM: decision.source_id,
            },
            state=KnowledgeState.IMPLEMENTED,
            confidence=(
                Confidence.SUPPORTED if decision.resolved else Confidence.UNRESOLVED
            ),
        )
        transaction.put_entity(entity)
        transaction.put_relation(
            Relation.create(
                RelationKind.BELONGS_TO.value,
                entity.id,
                capability_id,
                confidence=Confidence.INFERRED,
            )
        )
        transaction.put_relation(
            Relation.create(
                RelationKind.TRIGGERS.value,
                entity.id,
                flow_id,
                attributes={"criteria": list(decision.criteria)},
                confidence=Confidence.INFERRED,
            )
        )
        _inherit(transaction, query, decision.evidence_ids, entity.id)


def _inherit(
    transaction: RevisionTransaction,
    query: KnowledgeQuery,
    evidence_ids: Sequence[str],
    holder: EntityId,
) -> None:
    for evidence_id in evidence_ids:
        stored = query.repository.get_evidence(evidence_id)
        if stored is None:
            continue
        transaction.put_evidence(stored, entity_ids=(holder,))


def _pair_ok(
    relation: RelationKind,
    source: EntityKind,
    query: KnowledgeQuery,
    target_id: EntityId,
) -> bool:
    target = query.repository.get_entity(target_id)
    if target is None:
        return False
    try:
        target_kind = EntityKind(target.kind)
    except ValueError:
        return False
    return pair_allowed(relation, source, target_kind)


def _walk(query: KnowledgeQuery, entrypoint: Entity, max_depth: int) -> tuple[StepSpec, ...]:
    ordinal = _ENTRY_ORDINAL
    steps: list[StepSpec] = [
        StepSpec(
            ordinal=ordinal,
            subject=entrypoint.name,
            subject_id=entrypoint.id.value,
            via=RelationKind.IMPLEMENTS.value,
            entity_kind=entrypoint.kind,
            evidence_ids=_evidence_of(query, entrypoint.id),
        )
    ]
    seen = {entrypoint.id.value}
    frontier: list[EntityId] = [entrypoint.id]
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        following: list[EntityId] = []
        for node in frontier:
            for relation, entity in _edges(query, node):
                if entity.id.value in seen:
                    continue
                seen.add(entity.id.value)
                ordinal += 1
                steps.append(
                    StepSpec(
                        ordinal=ordinal,
                        subject=entity.name,
                        subject_id=entity.id.value,
                        via=relation.kind,
                        entity_kind=entity.kind,
                        evidence_ids=_evidence_of(query, entity.id),
                    )
                )
                following.append(entity.id)
        frontier = following
    return tuple(steps)


def _edges(
    query: KnowledgeQuery, node: EntityId
) -> tuple[tuple[Relation, Entity], ...]:
    found: list[tuple[Relation, Entity]] = []
    for relation, entity in query.neighbors(
        node,
        tuple(kind for kind in FLOW_RELATIONS if kind not in _INBOUND),
        direction="out",
        limit=1000,
    ):
        found.append((relation, entity))
    for relation, entity in query.neighbors(
        node, _INBOUND, direction="in", limit=1000
    ):
        found.append((relation, entity))
    found.sort(key=lambda pair: (_ORDER.get(pair[0].kind, 99), pair[1].name, pair[1].id.value))
    return tuple(found)


def _evidence_of(query: KnowledgeQuery, entity_id: EntityId) -> tuple[str, ...]:
    return tuple(
        sorted(
            item.id
            for item in query.repository.evidence_for(entity_id)
            if supports_implemented(item)
        )
    )


def _decisions(
    query: KnowledgeQuery, rules: Sequence[Entity]
) -> tuple[DecisionSpec, ...]:
    found: list[DecisionSpec] = []
    for rule in sorted(rules, key=lambda item: (item.name, item.id.value)):
        if rule.attributes.get(DERIVED_FROM):
            continue
        criteria = _texts(rule.attributes.get("conditions") or rule.attributes.get("criteria"))
        if not criteria:
            continue
        found.append(
            DecisionSpec(
                subject=f"decision on {rule.name}",
                criteria=criteria,
                outcomes=_texts(rule.attributes.get("effects")),
                source_id=rule.id.value,
                evidence_ids=_evidence_of(query, rule.id),
            )
        )
    return tuple(found)


def _texts(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value),)
