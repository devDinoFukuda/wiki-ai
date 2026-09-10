from __future__ import annotations

from wiki_ai.knowledge.model import Entity, EntityId
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind

from .model import DiagramKind, DiagramSpec

__all__ = [
    "DIAGRAM_LIMIT",
    "flowchart",
    "sequence",
    "state_machine",
    "dependency",
    "diagrams_for",
]

DIAGRAM_LIMIT = 200

_SEQUENCE_RELATIONS: tuple[RelationKind, ...] = (
    RelationKind.CALLS,
    RelationKind.CONSUMES,
    RelationKind.PERSISTS_TO,
)
_DEPENDENCY_HOLDERS: frozenset[str] = frozenset(
    {
        EntityKind.MODULE.value,
        EntityKind.SYSTEM.value,
        EntityKind.INTEGRATION.value,
    }
)
_DEPENDENCY_RELATIONS: tuple[RelationKind, ...] = (
    RelationKind.DEPENDS_ON,
    RelationKind.CALLS,
)


class _Aliases:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._by_entity: dict[str, str] = {}

    def of(self, entity_id: str) -> str:
        known = self._by_entity.get(entity_id)
        if known is not None:
            return known
        alias = f"{self._prefix}{len(self._by_entity) + 1}"
        self._by_entity[entity_id] = alias
        return alias


def _label(text: str) -> str:
    return str(text).replace('"', "'").replace("\n", " ").strip()


def _decision_label(entity: Entity) -> str:
    criteria = entity.attributes.get("criteria")
    if isinstance(criteria, str) and criteria.strip():
        return f"{entity.name}: {criteria.strip()}"
    return entity.name


def _flow_entities(query: KnowledgeQuery, subject_id: EntityId) -> tuple[Entity, ...]:
    flows = [
        node
        for _relation, node in query.neighbors(
            subject_id, RelationKind.BELONGS_TO, "in", limit=DIAGRAM_LIMIT
        )
        if node.kind == EntityKind.FLOW.value
    ]
    return tuple(sorted(flows, key=lambda item: (item.name, item.id.value)))


def flowchart(query: KnowledgeQuery, subject: Entity) -> DiagramSpec | None:
    flows = _flow_entities(query, subject.id)
    if not flows:
        return None
    lines = ["flowchart TD"]
    sentences: list[str] = []
    alias = _Aliases("n")
    for flow in flows:
        steps = query.flow(flow.id, limit=DIAGRAM_LIMIT)
        if not steps:
            continue
        entry = alias.of(flow.id.value)
        lines.append(f'    {entry}["{_label(flow.name)}"]')
        previous = entry
        previous_name = flow.name
        for step in steps:
            node = alias.of(step.entity.id.value)
            if step.entity.kind == EntityKind.DECISION.value:
                lines.append(f'    {node}{{"{_label(_decision_label(step.entity))}"}}')
            else:
                lines.append(f'    {node}["{_label(step.entity.name)}"]')
            lines.append(f"    {previous} --> {node}")
            sentences.append(
                f"No fluxo {flow.name}, {previous_name} leva a {step.entity.name}"
            )
            previous = node
            previous_name = step.entity.name
    if len(lines) == 1 or not sentences:
        return None
    return DiagramSpec(
        kind=DiagramKind.FLOWCHART,
        title=f"Fluxo principal de {subject.name}",
        mermaid_text="\n".join(lines),
        textual_equivalent=tuple(sentences),
    )


def _sequence_partners(
    query: KnowledgeQuery, source: Entity
) -> tuple[tuple[Entity, str], ...]:
    found: list[tuple[Entity, str]] = []
    seen: set[str] = set()
    for relation_kind in _SEQUENCE_RELATIONS:
        for relation, node in query.neighbors(
            source.id, relation_kind, "out", limit=DIAGRAM_LIMIT
        ):
            key = f"{relation.kind}:{node.id.value}"
            if key in seen:
                continue
            seen.add(key)
            found.append((node, relation.kind))
    return tuple(sorted(found, key=lambda item: (item[1], item[0].name)))


def _sequence_verb(relation_kind: str) -> str:
    if relation_kind == RelationKind.CALLS.value:
        return "chama"
    if relation_kind == RelationKind.CONSUMES.value:
        return "consome"
    return "grava em"


def sequence(query: KnowledgeQuery, subject: Entity) -> DiagramSpec | None:
    entrypoints = tuple(
        sorted(
            (
                node
                for _relation, node in query.neighbors(
                    subject.id, RelationKind.BELONGS_TO, "in", limit=DIAGRAM_LIMIT
                )
                if node.kind == EntityKind.ENTRY_POINT.value
            ),
            key=lambda item: (item.name, item.id.value),
        )
    )
    partners = _sequence_partners(query, subject)
    if not entrypoints or not partners:
        return None
    lines = ["sequenceDiagram"]
    sentences: list[str] = []
    alias = _Aliases("a")
    capability_actor = alias.of(subject.id.value)
    lines.append(f'    participant {capability_actor} as {_label(subject.name)}')
    for entry in entrypoints:
        actor = alias.of(entry.id.value)
        lines.append(f'    participant {actor} as {_label(entry.name)}')
    for node, _kind in partners:
        actor = alias.of(node.id.value)
        lines.append(f'    participant {actor} as {_label(node.name)}')
    for entry in entrypoints:
        actor = alias.of(entry.id.value)
        lines.append(f"    {actor}->>{capability_actor}: aciona")
        sentences.append(f"{entry.name} aciona {subject.name}")
    for node, kind in partners:
        actor = alias.of(node.id.value)
        verb = _sequence_verb(kind)
        lines.append(f"    {capability_actor}->>{actor}: {verb}")
        sentences.append(f"{subject.name} {verb} {node.name}")
    return DiagramSpec(
        kind=DiagramKind.SEQUENCE,
        title=f"Sequência de {subject.name}",
        mermaid_text="\n".join(lines),
        textual_equivalent=tuple(sentences),
    )


def _states(query: KnowledgeQuery, subject: Entity) -> tuple[Entity, ...]:
    found = [
        node
        for _relation, node in query.neighbors(
            subject.id, RelationKind.BELONGS_TO, "in", limit=DIAGRAM_LIMIT
        )
        if node.kind == EntityKind.STATE.value
    ]
    return tuple(sorted(found, key=lambda item: (item.name, item.id.value)))


def state_machine(query: KnowledgeQuery, subject: Entity) -> DiagramSpec | None:
    states = _states(query, subject)
    if not states:
        return None
    known = {state.id.value for state in states}
    lines = ["stateDiagram-v2"]
    sentences: list[str] = []
    alias = _Aliases("s")
    for state in states:
        lines.append(f'    {alias.of(state.id.value)} : {_label(state.name)}')
    for state in states:
        for relation, target in query.neighbors(
            state.id, RelationKind.TRANSITIONS_TO, "out", limit=DIAGRAM_LIMIT
        ):
            if target.id.value not in known:
                continue
            trigger = relation.attributes.get("trigger")
            source_node = alias.of(state.id.value)
            target_node = alias.of(target.id.value)
            if isinstance(trigger, str) and trigger.strip():
                lines.append(f"    {source_node} --> {target_node} : {_label(trigger)}")
                sentences.append(
                    f"o estado {state.name} vai para {target.name} quando {trigger.strip()}"
                )
            else:
                lines.append(f"    {source_node} --> {target_node}")
                sentences.append(f"o estado {state.name} vai para {target.name}")
    if not sentences:
        return None
    return DiagramSpec(
        kind=DiagramKind.STATE,
        title=f"Estados de {subject.name}",
        mermaid_text="\n".join(lines),
        textual_equivalent=tuple(sentences),
    )


def _dependency_scope(
    query: KnowledgeQuery, subject: Entity
) -> tuple[Entity, ...]:
    visited: set[str] = {subject.id.value}
    frontier: list[Entity] = [subject]
    collected: list[Entity] = [subject] if subject.kind in _DEPENDENCY_HOLDERS else []
    while frontier:
        current = frontier.pop(0)
        for _relation, node in query.neighbors(
            current.id, RelationKind.BELONGS_TO, "in", limit=DIAGRAM_LIMIT
        ):
            if node.id.value in visited:
                continue
            visited.add(node.id.value)
            frontier.append(node)
            if node.kind in _DEPENDENCY_HOLDERS:
                collected.append(node)
    return tuple(sorted(collected, key=lambda item: (item.name, item.id.value)))


def dependency(query: KnowledgeQuery, subject: Entity) -> DiagramSpec | None:
    scope = _dependency_scope(query, subject)
    if not scope:
        return None
    lines = ["flowchart LR"]
    sentences: list[str] = []
    alias = _Aliases("d")
    declared: set[str] = set()
    for node in scope:
        node_id = alias.of(node.id.value)
        if node_id not in declared:
            declared.add(node_id)
            lines.append(f'    {node_id}["{_label(node.name)}"]')
    for node in scope:
        edges: list[tuple[str, Entity]] = []
        for relation_kind in _DEPENDENCY_RELATIONS:
            for _relation, target in query.neighbors(
                node.id, relation_kind, "out", limit=DIAGRAM_LIMIT
            ):
                if target.kind in _DEPENDENCY_HOLDERS:
                    edges.append((relation_kind.value, target))
        for kind_value, target in sorted(edges, key=lambda item: (item[1].name, item[0])):
            source_node = alias.of(node.id.value)
            target_node = alias.of(target.id.value)
            if target_node not in declared:
                declared.add(target_node)
                lines.append(f'    {target_node}["{_label(target.name)}"]')
            lines.append(f"    {source_node} --> {target_node}")
            verb = "chama" if kind_value == RelationKind.CALLS.value else "depende de"
            sentences.append(f"{node.name} {verb} {target.name}")
    if not sentences:
        return None
    return DiagramSpec(
        kind=DiagramKind.DEPENDENCY,
        title=f"Dependências de {subject.name}",
        mermaid_text="\n".join(lines),
        textual_equivalent=tuple(sentences),
    )


def diagrams_for(query: KnowledgeQuery, subject: Entity) -> tuple[DiagramSpec, ...]:
    builders = (flowchart, sequence, state_machine, dependency)
    found: list[DiagramSpec] = []
    for builder in builders:
        spec = builder(query, subject)
        if spec is not None:
            found.append(spec)
    return tuple(found)
