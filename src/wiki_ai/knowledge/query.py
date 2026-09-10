from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .comparison import ComparisonFinding, ComparisonReport
from .comparison import compare as compare_repository
from .gaps import blocking_gaps, open_gaps
from .model import Confidence, Entity, EntityId, KnowledgeState, Evidence, Relation
from .repository import ENTITY_COLUMNS, KnowledgeRepository
from .taxonomy import EntityKind, RelationKind

__all__ = [
    "CapabilityProfile",
    "ComparisonFinding",
    "ComparisonReport",
    "DEFAULT_LIMIT",
    "FlowStepView",
    "ImpactReport",
    "KnowledgeQuery",
    "Page",
    "PathResult",
]

DEFAULT_LIMIT = 100

DIRECTION_OUT = "out"
DIRECTION_IN = "in"
DIRECTION_BOTH = "both"

IMPACT_RELATIONS: tuple[RelationKind, ...] = (
    RelationKind.DEPENDS_ON,
    RelationKind.CALLS,
    RelationKind.CONSUMES,
    RelationKind.AFFECTS,
)

FLOW_RELATIONS: tuple[RelationKind, ...] = (
    RelationKind.TRIGGERS,
    RelationKind.TRANSITIONS_TO,
)


@dataclass(frozen=True)
class Page:
    limit: int = DEFAULT_LIMIT
    offset: int = 0

    def slice(self, items: Sequence[Any]) -> tuple[Any, ...]:
        if self.limit <= 0:
            return ()
        start = max(0, self.offset)
        return tuple(items[start : start + self.limit])


@dataclass(frozen=True)
class PathResult:
    nodes: tuple[str, ...]
    relations: tuple[str, ...]

    @property
    def depth(self) -> int:
        return len(self.relations)


@dataclass(frozen=True)
class FlowStepView:
    entity: Entity
    ordinal: int
    via: str | None


@dataclass(frozen=True)
class CapabilityProfile:
    capability: Entity
    entrypoints: tuple[Entity, ...] = ()
    inputs: tuple[Entity, ...] = ()
    outputs: tuple[Entity, ...] = ()
    preconditions: tuple[Entity, ...] = ()
    rules: tuple[Entity, ...] = ()
    invariants: tuple[Entity, ...] = ()
    decisions: tuple[Entity, ...] = ()
    states: tuple[Entity, ...] = ()
    persistence: tuple[Entity, ...] = ()
    integrations: tuple[Entity, ...] = ()
    events: tuple[Entity, ...] = ()
    failures: tuple[Entity, ...] = ()
    retries: tuple[Entity, ...] = ()
    fallbacks: tuple[Entity, ...] = ()
    timeouts: tuple[Entity, ...] = ()
    idempotency: tuple[Entity, ...] = ()
    edge_cases: tuple[Entity, ...] = ()
    tests: tuple[Entity, ...] = ()
    dependencies: tuple[Entity, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    gaps: tuple[Entity, ...] = ()

    def sections(self) -> Mapping[str, tuple[Entity, ...]]:
        return {
            "entrypoints": self.entrypoints,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "preconditions": self.preconditions,
            "rules": self.rules,
            "invariants": self.invariants,
            "decisions": self.decisions,
            "states": self.states,
            "persistence": self.persistence,
            "integrations": self.integrations,
            "events": self.events,
            "failures": self.failures,
            "retries": self.retries,
            "fallbacks": self.fallbacks,
            "timeouts": self.timeouts,
            "idempotency": self.idempotency,
            "edge_cases": self.edge_cases,
            "tests": self.tests,
            "dependencies": self.dependencies,
            "gaps": self.gaps,
        }


@dataclass(frozen=True)
class ImpactReport:
    origin: EntityId
    impacted: tuple[Entity, ...] = ()
    depth_by_entity: Mapping[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.impacted)


def _kind_value(kind: str | EntityKind | None) -> str | None:
    if kind is None:
        return None
    return kind.value if isinstance(kind, EntityKind) else str(kind)


def _relation_values(
    kinds: str | RelationKind | Iterable[str | RelationKind] | None,
) -> tuple[str, ...]:
    if kinds is None:
        return ()
    if isinstance(kinds, (str, RelationKind)):
        candidates: Iterable[str | RelationKind] = (kinds,)
    else:
        candidates = kinds
    return tuple(
        item.value if isinstance(item, RelationKind) else str(item) for item in candidates
    )


class KnowledgeQuery:
    def __init__(self, repository: KnowledgeRepository) -> None:
        self._repo = repository

    @property
    def repository(self) -> KnowledgeRepository:
        return self._repo

    def entities(
        self,
        kind: str | EntityKind | None = None,
        state: KnowledgeState | None = None,
        confidence: Confidence | None = None,
        name_contains: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Entity, ...]:
        clauses: list[str] = []
        params: list[Any] = []
        kind_value = _kind_value(kind)
        if kind_value is not None:
            clauses.append("kind = ?")
            params.append(kind_value)
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)
        if confidence is not None:
            clauses.append("confidence = ?")
            params.append(confidence.value)
        if name_contains:
            clauses.append("name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(name_contains)}%")
        sql = f"SELECT {ENTITY_COLUMNS} FROM entities"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY kind, name, entity_id LIMIT ? OFFSET ?"
        params.extend([max(0, limit), max(0, offset)])
        return tuple(
            self._repo.row_to_entity(row) for row in self._repo.conn.execute(sql, params)
        )

    def search(
        self, text: str, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Entity, ...]:
        pattern = f"%{_escape_like(text)}%"
        sql = (
            f"SELECT {ENTITY_COLUMNS} FROM entities WHERE name LIKE ? ESCAPE '\\' "
            "OR attributes_json LIKE ? ESCAPE '\\' ORDER BY kind, name, entity_id "
            "LIMIT ? OFFSET ?"
        )
        params = [pattern, pattern, max(0, limit), max(0, offset)]
        return tuple(
            self._repo.row_to_entity(row) for row in self._repo.conn.execute(sql, params)
        )

    def neighbors(
        self,
        entity_id: EntityId,
        relation_kind: str | RelationKind | Iterable[str | RelationKind] | None = None,
        direction: str = DIRECTION_BOTH,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[tuple[Relation, Entity], ...]:
        kinds = _relation_values(relation_kind)
        found = self._repo.relations_of(entity_id, direction, kinds or None)
        pairs: list[tuple[Relation, Entity]] = []
        for relation in found:
            other = (
                relation.target_id
                if relation.source_id == entity_id
                else relation.source_id
            )
            entity = self._repo.get_entity(other)
            if entity is not None:
                pairs.append((relation, entity))
        return Page(limit, offset).slice(pairs)

    def paths(
        self,
        from_id: EntityId,
        to_id: EntityId,
        max_depth: int = 4,
        relation_kind: str | RelationKind | Iterable[str | RelationKind] | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[PathResult, ...]:
        kinds = _relation_values(relation_kind)
        results: list[PathResult] = []
        queue: deque[tuple[tuple[str, ...], tuple[str, ...]]] = deque(
            [((from_id.value,), ())]
        )
        while queue:
            nodes, edges = queue.popleft()
            if nodes[-1] == to_id.value and edges:
                results.append(PathResult(nodes=nodes, relations=edges))
                continue
            if len(edges) >= max(0, max_depth):
                continue
            for relation in self._repo.relations_of(
                EntityId(nodes[-1]), DIRECTION_OUT, kinds or None
            ):
                target = relation.target_id.value
                if target in nodes:
                    continue
                queue.append((nodes + (target,), edges + (relation.id,)))
        results.sort(key=lambda item: (item.depth, item.nodes))
        return Page(limit, offset).slice(results)

    def flow(
        self, entity_id: EntityId, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[FlowStepView, ...]:
        root = self._repo.get_entity(entity_id)
        if root is None:
            return ()
        steps = self._ordered_flow_steps(entity_id)
        views: list[FlowStepView] = [
            FlowStepView(entity=step, ordinal=index, via=via)
            for index, (step, via) in enumerate(steps)
        ]
        return Page(limit, offset).slice(views)

    def _ordered_flow_steps(
        self, entity_id: EntityId
    ) -> tuple[tuple[Entity, str | None], ...]:
        owned: list[tuple[int, Entity]] = []
        for relation in self._repo.relations_of(
            entity_id, DIRECTION_IN, (RelationKind.BELONGS_TO.value,)
        ):
            step = self._repo.get_entity(relation.source_id)
            if step is None or step.kind != EntityKind.FLOW_STEP.value:
                continue
            owned.append((_ordinal_of(step), step))
        owned.sort(key=lambda item: (item[0], item[1].name))
        ordered: list[tuple[Entity, str | None]] = [
            (step, RelationKind.BELONGS_TO.value) for _, step in owned
        ]
        seen = {step.id.value for _, step in owned}
        frontier: list[EntityId] = [step.id for _, step in owned] or [entity_id]
        while frontier:
            current = frontier.pop(0)
            for relation in self._repo.relations_of(
                current,
                DIRECTION_OUT,
                tuple(kind.value for kind in FLOW_RELATIONS),
            ):
                target = self._repo.get_entity(relation.target_id)
                if target is None or target.id.value in seen:
                    continue
                seen.add(target.id.value)
                ordered.append((target, relation.kind))
                frontier.append(target.id)
        return tuple(ordered)

    def capability_profile(self, capability_id: EntityId) -> CapabilityProfile | None:
        capability = self._repo.get_entity(capability_id)
        if capability is None:
            return None
        members = self._members(capability_id)
        return CapabilityProfile(
            capability=capability,
            entrypoints=_merge(
                self._targets(
                    capability_id,
                    RelationKind.IMPLEMENTS,
                    DIRECTION_IN,
                    EntityKind.ENTRY_POINT,
                ),
                _of_kind(members, EntityKind.ENTRY_POINT),
            ),
            inputs=_merge(
                self._consumed(capability_id, EntityKind.INPUT),
                _of_kind(members, EntityKind.INPUT),
            ),
            outputs=_merge(
                self._published(capability_id, EntityKind.OUTPUT),
                _of_kind(members, EntityKind.OUTPUT),
            ),
            preconditions=_merge(
                _of_kind(members, EntityKind.PRECONDITION),
                self._validators(capability_id, EntityKind.PRECONDITION),
            ),
            rules=_merge(
                _of_kind(members, EntityKind.BUSINESS_RULE),
                self._validators(capability_id, EntityKind.BUSINESS_RULE),
            ),
            invariants=_merge(
                _of_kind(members, EntityKind.INVARIANT),
                self._validators(capability_id, EntityKind.INVARIANT),
            ),
            decisions=_merge(_of_kind(members, EntityKind.DECISION)),
            states=_merge(
                _of_kind(members, EntityKind.STATE),
                self._targets(
                    capability_id, RelationKind.WRITES, DIRECTION_OUT, EntityKind.STATE
                ),
            ),
            persistence=_merge(
                self._persistence(capability_id),
                _of_kind(members, EntityKind.PERSISTENCE),
            ),
            integrations=_merge(
                self._targets(
                    capability_id,
                    RelationKind.CALLS,
                    DIRECTION_OUT,
                    EntityKind.INTEGRATION,
                ),
                _of_kind(members, EntityKind.INTEGRATION),
            ),
            events=_merge(
                self._published(capability_id, EntityKind.EVENT),
                self._consumed(capability_id, EntityKind.EVENT),
                _of_kind(members, EntityKind.EVENT),
            ),
            failures=_merge(
                self._targets(
                    capability_id,
                    RelationKind.HANDLES,
                    DIRECTION_OUT,
                    EntityKind.FAILURE_MODE,
                ),
                _of_kind(members, EntityKind.FAILURE_MODE),
            ),
            retries=_merge(
                self._sources(
                    capability_id, RelationKind.RETRIES, EntityKind.RETRY_POLICY
                ),
                _of_kind(members, EntityKind.RETRY_POLICY),
            ),
            fallbacks=_merge(
                self._targets(
                    capability_id,
                    RelationKind.FALLS_BACK_TO,
                    DIRECTION_OUT,
                    EntityKind.FALLBACK,
                ),
                _of_kind(members, EntityKind.FALLBACK),
            ),
            timeouts=_merge(_of_kind(members, EntityKind.TIMEOUT_POLICY)),
            idempotency=_merge(_of_kind(members, EntityKind.IDEMPOTENCY_POLICY)),
            edge_cases=_merge(
                self._targets(
                    capability_id,
                    RelationKind.HANDLES,
                    DIRECTION_OUT,
                    EntityKind.EDGE_CASE,
                ),
                _of_kind(members, EntityKind.EDGE_CASE),
            ),
            tests=_merge(
                self._sources(
                    capability_id, RelationKind.TESTS, EntityKind.TEST_SCENARIO
                )
            ),
            dependencies=_merge(
                self._targets(capability_id, RelationKind.DEPENDS_ON, DIRECTION_OUT, None)
            ),
            evidence=tuple(self._repo.evidence_for(capability_id)),
            gaps=_merge(
                self._gaps_about(capability_id), _of_kind(members, EntityKind.GAP)
            ),
        )

    def gaps(
        self, blocking_only: bool = False, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Entity, ...]:
        found = blocking_gaps(self._repo) if blocking_only else open_gaps(self._repo)
        return Page(limit, offset).slice(found)

    def impact(
        self, entity_id: EntityId, max_depth: int = 8, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> ImpactReport:
        kinds = tuple(kind.value for kind in IMPACT_RELATIONS)
        depth: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque([(entity_id.value, 0)])
        seen = {entity_id.value}
        while queue:
            node, level = queue.popleft()
            if level >= max(0, max_depth):
                continue
            for relation in self._repo.relations_of(EntityId(node), DIRECTION_IN, kinds):
                origin = relation.source_id.value
                if origin in seen:
                    continue
                seen.add(origin)
                depth[origin] = level + 1
                queue.append((origin, level + 1))
        ordered = sorted(depth.items(), key=lambda item: (item[1], item[0]))
        impacted: list[Entity] = []
        for node, _ in ordered:
            entity = self._repo.get_entity(EntityId(node))
            if entity is not None:
                impacted.append(entity)
        return ImpactReport(
            origin=entity_id,
            impacted=Page(limit, offset).slice(impacted),
            depth_by_entity=dict(depth),
        )

    def evidence_of(
        self, entity_id: EntityId, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Evidence, ...]:
        return Page(limit, offset).slice(self._repo.evidence_for(entity_id))

    def compare(self, limit: int = DEFAULT_LIMIT, offset: int = 0) -> ComparisonReport:
        page = Page(limit, offset)
        full = compare_repository(self._repo)
        return ComparisonReport(
            declared_not_implemented=page.slice(full.declared_not_implemented),
            implemented_not_documented=page.slice(full.implemented_not_documented),
            proposal_conflicts=page.slice(full.proposal_conflicts),
            decision_supersedes=page.slice(full.decision_supersedes),
            source_contradicts_source=page.slice(full.source_contradicts_source),
        )

    def _members(self, container_id: EntityId) -> tuple[Entity, ...]:
        found: list[Entity] = []
        for relation in self._repo.relations_of(
            container_id, DIRECTION_IN, (RelationKind.BELONGS_TO.value,)
        ):
            member = self._repo.get_entity(relation.source_id)
            if member is not None:
                found.append(member)
        return tuple(found)

    def _targets(
        self,
        entity_id: EntityId,
        relation: RelationKind,
        direction: str,
        kind: EntityKind | None,
    ) -> tuple[Entity, ...]:
        found: list[Entity] = []
        for edge in self._repo.relations_of(entity_id, direction, (relation.value,)):
            other = edge.target_id if direction == DIRECTION_OUT else edge.source_id
            node = self._repo.get_entity(other)
            if node is None:
                continue
            if kind is not None and node.kind != kind.value:
                continue
            found.append(node)
        return tuple(found)

    def _sources(
        self, entity_id: EntityId, relation: RelationKind, kind: EntityKind | None
    ) -> tuple[Entity, ...]:
        return self._targets(entity_id, relation, DIRECTION_IN, kind)

    def _consumed(self, entity_id: EntityId, kind: EntityKind) -> tuple[Entity, ...]:
        return self._targets(entity_id, RelationKind.CONSUMES, DIRECTION_OUT, kind)

    def _published(self, entity_id: EntityId, kind: EntityKind) -> tuple[Entity, ...]:
        return self._targets(entity_id, RelationKind.PUBLISHES, DIRECTION_OUT, kind)

    def _validators(self, entity_id: EntityId, kind: EntityKind) -> tuple[Entity, ...]:
        return self._targets(entity_id, RelationKind.VALIDATES, DIRECTION_IN, kind)

    def _persistence(self, entity_id: EntityId) -> tuple[Entity, ...]:
        found: list[Entity] = []
        for relation_kind in (
            RelationKind.PERSISTS_TO,
            RelationKind.WRITES,
            RelationKind.READS,
        ):
            for node in self._targets(entity_id, relation_kind, DIRECTION_OUT, None):
                if node.kind in _STORAGE_KIND_VALUES and node not in found:
                    found.append(node)
        return tuple(found)

    def _gaps_about(self, entity_id: EntityId) -> tuple[Entity, ...]:
        return self._targets(entity_id, RelationKind.AFFECTS, DIRECTION_IN, EntityKind.GAP)


_STORAGE_KIND_VALUES: frozenset[str] = frozenset(
    {
        EntityKind.TABLE.value,
        EntityKind.DATA_ENTITY.value,
        EntityKind.PERSISTENCE.value,
        EntityKind.QUEUE.value,
        EntityKind.TOPIC.value,
    }
)

def _escape_like(text: str) -> str:
    return (
        str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _ordinal_of(entity: Entity) -> int:
    raw = entity.attributes.get("ordinal")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0
    return int(raw)


def _merge(*groups: Sequence[Entity]) -> tuple[Entity, ...]:
    found: list[Entity] = []
    seen: set[str] = set()
    for group in groups:
        for entity in group:
            if entity.id.value in seen:
                continue
            seen.add(entity.id.value)
            found.append(entity)
    return tuple(found)


def _of_kind(entities: Sequence[Entity], kind: EntityKind) -> tuple[Entity, ...]:
    return tuple(entity for entity in entities if entity.kind == kind.value)
