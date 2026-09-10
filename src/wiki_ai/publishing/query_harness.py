from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from wiki_ai.knowledge.gaps import GAP_BLOCKING, GAP_QUESTION, GAP_STATUS
from wiki_ai.knowledge.grounding import excerpt_vocabulary
from wiki_ai.knowledge.model import Entity, EntityId, Evidence, Locator
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import ENTITY_KIND_VALUES, RELATION_KIND_VALUES

__all__ = [
    "QueryToolError",
    "UnknownQueryTool",
    "InvalidQueryArguments",
    "QueryToolSpec",
    "QueryLimits",
    "KnowledgeQueryHarness",
    "MAX_EXCERPT_CHARS",
    "TOOL_SEARCH",
    "TOOL_ENTITY",
    "TOOL_NEIGHBORS",
    "TOOL_EVIDENCE",
    "TOOL_FLOW",
    "TOOL_RULES",
    "TOOL_GAPS",
    "TOOL_COMPARE",
    "TOOL_IMPACT",
    "TOOL_SOURCE",
    "TOOL_NAMES",
    "validate_arguments",
]

TOOL_SEARCH = "knowledge.search"
TOOL_ENTITY = "knowledge.entity"
TOOL_NEIGHBORS = "knowledge.neighbors"
TOOL_EVIDENCE = "knowledge.evidence"
TOOL_FLOW = "knowledge.flow"
TOOL_RULES = "knowledge.rules"
TOOL_GAPS = "knowledge.gaps"
TOOL_COMPARE = "knowledge.compare"
TOOL_IMPACT = "knowledge.impact"
TOOL_SOURCE = "knowledge.source"

TOOL_NAMES: tuple[str, ...] = (
    TOOL_SEARCH,
    TOOL_ENTITY,
    TOOL_NEIGHBORS,
    TOOL_EVIDENCE,
    TOOL_FLOW,
    TOOL_RULES,
    TOOL_GAPS,
    TOOL_COMPARE,
    TOOL_IMPACT,
    TOOL_SOURCE,
)

RULE_KIND = "business_rule"
MAX_ATTRIBUTE_CHARS = 400
MAX_SUMMARY_CHARS = 240
MAX_EXCERPT_CHARS = 1200


class QueryToolError(Exception):
    pass


class UnknownQueryTool(QueryToolError):
    pass


class InvalidQueryArguments(QueryToolError):
    pass


@dataclass(frozen=True)
class QueryToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
        }


@dataclass(frozen=True)
class QueryLimits:
    max_results: int = 40
    max_results_ceiling: int = 200
    max_depth: int = 4
    max_depth_ceiling: int = 8

    def results(self, requested: Any) -> int:
        if requested is None:
            return self.max_results
        return max(1, min(int(requested), self.max_results_ceiling))

    def depth(self, requested: Any) -> int:
        if requested is None:
            return self.max_depth
        return max(1, min(int(requested), self.max_depth_ceiling))


_TYPE_CHECKS: Mapping[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list, tuple),
}


def _type_matches(value: Any, expected: str) -> bool:
    checks = _TYPE_CHECKS.get(expected)
    if checks is None:
        return True
    if expected == "integer" and isinstance(value, bool):
        return False
    return isinstance(value, checks)


def _check_value(name: str, value: Any, schema: Mapping[str, Any]) -> None:
    declared = schema.get("type")
    expected = (declared,) if isinstance(declared, str) else ()
    if expected and not any(_type_matches(value, item) for item in expected):
        raise InvalidQueryArguments(f"argument {name} must be of type {expected[0]}")
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and value not in enum:
        raise InvalidQueryArguments(f"argument {name} must be one of {list(enum)}")
    minimum = schema.get("minimum")
    if minimum is not None and isinstance(value, int) and value < minimum:
        raise InvalidQueryArguments(f"argument {name} must be >= {minimum}")
    maximum = schema.get("maximum")
    if maximum is not None and isinstance(value, int) and value > maximum:
        raise InvalidQueryArguments(f"argument {name} must be <= {maximum}")
    items = schema.get("items")
    if expected and expected[0] == "array" and isinstance(items, Mapping):
        for index, item in enumerate(value):
            _check_value(f"{name}[{index}]", item, items)


def validate_arguments(
    spec: QueryToolSpec, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise InvalidQueryArguments("arguments must be a mapping")
    schema = spec.input_schema
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    required = schema.get("required")
    names: Sequence[str] = (
        tuple(str(item) for item in required)
        if isinstance(required, (list, tuple))
        else ()
    )
    for key in arguments:
        if key not in properties:
            raise InvalidQueryArguments(f"unknown argument: {key}")
    for key in names:
        value = arguments.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise InvalidQueryArguments(f"missing required argument: {key}")
    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        property_schema = properties.get(key)
        if isinstance(property_schema, Mapping) and value is not None:
            _check_value(key, value, property_schema)
        cleaned[key] = value
    return cleaned


def _text(value: Any, ceiling: int = MAX_ATTRIBUTE_CHARS) -> str:
    body = str(value).strip()
    if len(body) <= ceiling:
        return body
    return body[:ceiling].rstrip() + "…"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_text(item) for item in value]


def _limit_property(ceiling: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": ceiling}


def _entity_id_property(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _entity_output() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string"},
            "kind": {"type": "string"},
            "name": {"type": "string"},
            "state": {"type": "string"},
            "confidence": {"type": "string"},
            "summary": {"type": "string"},
            "evidence_count": {"type": "integer"},
        },
    }


def _listing_output(item_key: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            item_key: {"type": "array", "items": _entity_output()},
            "truncated": {"type": "boolean"},
        },
    }


def build_specs(limits: QueryLimits) -> tuple[QueryToolSpec, ...]:
    ceiling = limits.max_results_ceiling
    return (
        QueryToolSpec(
            name=TOOL_SEARCH,
            description=(
                "Find entities in the curated knowledge whose name or attributes "
                "contain a text, optionally restricted to one entity kind. Returns "
                "identifiers to follow with the other knowledge tools."
            ),
            input_schema={
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": sorted(ENTITY_KIND_VALUES)},
                    "max_results": _limit_property(ceiling),
                },
            },
            output_schema=_listing_output("entities"),
        ),
        QueryToolSpec(
            name=TOOL_ENTITY,
            description=(
                "Read one entity by identifier with its attributes, epistemic state "
                "and how many evidences sustain it."
            ),
            input_schema={
                "type": "object",
                "required": ["entity_id"],
                "properties": {
                    "entity_id": _entity_id_property("identifier returned by another tool")
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "entity": _entity_output(),
                    "attributes": {"type": "object"},
                },
            },
        ),
        QueryToolSpec(
            name=TOOL_NEIGHBORS,
            description=(
                "Follow typed relations out of, into or around one entity to reach "
                "the entities it touches."
            ),
            input_schema={
                "type": "object",
                "required": ["entity_id"],
                "properties": {
                    "entity_id": _entity_id_property("entity to walk from"),
                    "relation_kind": {"type": "string", "enum": sorted(RELATION_KIND_VALUES)},
                    "direction": {"type": "string", "enum": ["out", "in", "both"]},
                    "max_results": _limit_property(ceiling),
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "neighbors": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "relation_kind": {"type": "string"},
                                "direction": {"type": "string"},
                                "entity": _entity_output(),
                            },
                        },
                    },
                    "truncated": {"type": "boolean"},
                },
            },
        ),
        QueryToolSpec(
            name=TOOL_EVIDENCE,
            description=(
                "List the evidences that sustain one entity: evidence identifier, "
                "source, version hash, where the excerpt lives and the excerpt "
                "text itself. Read the excerpt before writing a claim: the wording "
                "of a claim is only sustained by what the excerpt says. Cite these "
                "identifiers in every claim."
            ),
            input_schema={
                "type": "object",
                "required": ["entity_id"],
                "properties": {
                    "entity_id": _entity_id_property("entity to look evidence for"),
                    "max_results": _limit_property(ceiling),
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "evidence_id": {"type": "string"},
                                "source_id": {"type": "string"},
                                "version_hash": {"type": "string"},
                                "where": {"type": "string"},
                                "locator_kind": {"type": "string"},
                                "excerpt": {"type": "string"},
                            },
                        },
                    },
                    "truncated": {"type": "boolean"},
                },
            },
        ),
        QueryToolSpec(
            name=TOOL_FLOW,
            description=(
                "Read the ordered steps of a flow or of the flows owned by an "
                "entity, with the relation that links each step to the next."
            ),
            input_schema={
                "type": "object",
                "required": ["entity_id"],
                "properties": {
                    "entity_id": _entity_id_property("flow or owner of the flow"),
                    "max_results": _limit_property(ceiling),
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "ordinal": {"type": "integer"},
                                "via": {"type": "string"},
                                "entity": _entity_output(),
                            },
                        },
                    },
                    "truncated": {"type": "boolean"},
                },
            },
        ),
        QueryToolSpec(
            name=TOOL_RULES,
            description=(
                "List the business rules of the knowledge, optionally the ones "
                "attached to one entity, with their conditions and effects."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": _entity_id_property("restrict to rules around it"),
                    "max_results": _limit_property(ceiling),
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "entity": _entity_output(),
                                "statement": {"type": "string"},
                                "conditions": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "effects": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                    "truncated": {"type": "boolean"},
                },
            },
        ),
        QueryToolSpec(
            name=TOOL_GAPS,
            description=(
                "List the open questions the knowledge itself declares unresolved, "
                "so the answer can say what it cannot decide."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "blocking_only": {"type": "boolean"},
                    "max_results": _limit_property(ceiling),
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "gaps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "entity_id": {"type": "string"},
                                "question": {"type": "string"},
                                "blocking": {"type": "boolean"},
                                "status": {"type": "string"},
                            },
                        },
                    },
                    "truncated": {"type": "boolean"},
                },
            },
        ),
        QueryToolSpec(
            name=TOOL_COMPARE,
            description=(
                "Read the divergences the knowledge holds between what is declared "
                "and what is implemented, plus proposals, superseded decisions and "
                "sources that contradict each other."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "declared_not_implemented",
                            "implemented_not_documented",
                            "proposal_conflicts",
                            "decision_supersedes",
                            "source_contradicts_source",
                        ],
                    },
                    "max_results": _limit_property(ceiling),
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string"},
                                "entity_id": {"type": "string"},
                                "entity_name": {"type": "string"},
                                "counterpart_id": {"type": "string"},
                                "counterpart_name": {"type": "string"},
                                "detail": {"type": "string"},
                            },
                        },
                    }
                },
            },
        ),
        QueryToolSpec(
            name=TOOL_IMPACT,
            description=(
                "Walk the dependency, call, consumption and affectation relations "
                "backwards from one entity to reach everything that would be "
                "touched if it changed, with the distance of each reached entity."
            ),
            input_schema={
                "type": "object",
                "required": ["entity_id"],
                "properties": {
                    "entity_id": _entity_id_property("entity that would change"),
                    "max_depth": _limit_property(limits.max_depth_ceiling),
                    "max_results": _limit_property(ceiling),
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "origin_id": {"type": "string"},
                    "impacted": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "depth": {"type": "integer"},
                                "entity": _entity_output(),
                            },
                        },
                    },
                    "truncated": {"type": "boolean"},
                },
            },
        ),
        QueryToolSpec(
            name=TOOL_SOURCE,
            description=(
                "Read the sources the knowledge was captured from, with the version "
                "hash and the root each locator is relative to."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "max_results": _limit_property(ceiling),
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_id": {"type": "string"},
                                "version_hash": {"type": "string"},
                                "locator_root": {"type": "string"},
                                "captured_at": {"type": "string"},
                            },
                        },
                    },
                    "truncated": {"type": "boolean"},
                },
            },
        ),
    )


def _flatten(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(
            f"{key} {_flatten(item)}" for key, item in sorted(value.items())
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(_flatten(item) for item in value)
    return str(value)


def where_of(locator: Locator) -> str:
    payload = locator.to_dict() if hasattr(locator, "to_dict") else {}
    parts = [
        f"{key}={_text(value, 120)}"
        for key, value in sorted(payload.items())
        if key != "kind" and value not in (None, "", (), [])
    ]
    return "; ".join(parts)


class KnowledgeQueryHarness:
    def __init__(
        self,
        knowledge: KnowledgeRepository,
        namespace: str,
        limits: QueryLimits | None = None,
        query_factory: Callable[[KnowledgeRepository], KnowledgeQuery] = KnowledgeQuery,
    ) -> None:
        self._knowledge = knowledge
        self._namespace = namespace
        self._limits = limits or QueryLimits()
        self._query = query_factory(knowledge)
        self._specs = build_specs(self._limits)
        self._by_name: Mapping[str, QueryToolSpec] = {
            spec.name: spec for spec in self._specs
        }
        self._handlers: Mapping[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            TOOL_SEARCH: self._search,
            TOOL_ENTITY: self._entity,
            TOOL_NEIGHBORS: self._neighbors,
            TOOL_EVIDENCE: self._evidence,
            TOOL_FLOW: self._flow,
            TOOL_RULES: self._rules,
            TOOL_GAPS: self._gaps,
            TOOL_COMPARE: self._compare,
            TOOL_IMPACT: self._impact,
            TOOL_SOURCE: self._source,
        }
        self._visited: list[str] = []

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def limits(self) -> QueryLimits:
        return self._limits

    @property
    def query(self) -> KnowledgeQuery:
        return self._query

    def visited(self) -> tuple[str, ...]:
        return tuple(self._visited)

    def specs(self) -> tuple[QueryToolSpec, ...]:
        return self._specs

    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs)

    def spec_for(self, name: str) -> QueryToolSpec:
        spec = self._by_name.get(name)
        if spec is None:
            raise UnknownQueryTool(f"unknown tool: {name}")
        return spec

    def invoke(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        spec = self.spec_for(name)
        cleaned = validate_arguments(spec, arguments or {})
        return self._handlers[name](cleaned)

    def entity_exists(self, entity_id: str) -> bool:
        return self._knowledge.get_entity(EntityId(str(entity_id))) is not None

    def evidence_exists(self, evidence_id: str) -> bool:
        return self._knowledge.get_evidence(str(evidence_id)) is not None

    def evidence_by_id(self, evidence_id: str) -> Evidence | None:
        return self._knowledge.get_evidence(str(evidence_id))

    def entity_by_id(self, entity_id: str) -> Entity | None:
        return self._knowledge.get_entity(EntityId(str(entity_id)))

    def evidence_linked_to(self, evidence_id: str, entity_ids: Sequence[str]) -> bool:
        wanted = str(evidence_id)
        for identifier in entity_ids:
            owner = EntityId(str(identifier))
            if any(item.id == wanted for item in self._knowledge.evidence_for(owner)):
                return True
            for relation in self._knowledge.relations_of(owner):
                linked = self._knowledge.evidence_for_relation(relation.id)
                if any(item.id == wanted for item in linked):
                    return True
        return False

    def evidence_without_excerpt(self, evidence_ids: Sequence[str]) -> tuple[str, ...]:
        empty: list[str] = []
        for identifier in evidence_ids:
            evidence = self.evidence_by_id(identifier)
            if evidence is None or not evidence.excerpt.strip():
                if str(identifier) not in empty:
                    empty.append(str(identifier))
        return tuple(empty)

    def grounding_vocabulary(
        self, entity_ids: Sequence[str], evidence_ids: Sequence[str]
    ) -> frozenset[str]:
        vocabulary: set[str] = set()
        for identifier in evidence_ids:
            evidence = self.evidence_by_id(identifier)
            if evidence is None:
                continue
            vocabulary |= excerpt_vocabulary(evidence.excerpt)
        for identifier in entity_ids:
            entity = self.entity_by_id(identifier)
            if entity is None:
                continue
            vocabulary |= excerpt_vocabulary(entity.name)
            vocabulary |= excerpt_vocabulary(entity.kind)
            statement = entity.attributes.get("statement")
            if statement is not None:
                vocabulary |= excerpt_vocabulary(_flatten(statement))
        return frozenset(vocabulary)

    def _known(self, entity_id: Any) -> EntityId:
        identifier = EntityId(str(entity_id))
        if self._knowledge.get_entity(identifier) is None:
            raise InvalidQueryArguments(f"entity is not part of the knowledge: {entity_id}")
        if identifier.value not in self._visited:
            self._visited.append(identifier.value)
        return identifier

    def _view(self, entity: Entity) -> dict[str, Any]:
        summary = entity.attributes.get("statement") or entity.attributes.get("detail")
        return {
            "entity_id": entity.id.value,
            "kind": entity.kind,
            "name": _text(entity.name, MAX_SUMMARY_CHARS),
            "state": entity.state.value,
            "confidence": entity.confidence.value,
            "summary": _text(summary or entity.name, MAX_SUMMARY_CHARS),
            "evidence_count": len(self._knowledge.evidence_for(entity.id)),
        }

    def _search(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        limit = self._limits.results(arguments.get("max_results"))
        text = str(arguments["text"])
        kind = arguments.get("kind")
        found = self._query.search(text, limit=limit + 1)
        if kind is not None:
            found = tuple(entity for entity in found if entity.kind == kind)
        return {
            "entities": [self._view(entity) for entity in found[:limit]],
            "truncated": len(found) > limit,
        }

    def _entity(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        identifier = self._known(arguments["entity_id"])
        entity = self._knowledge.get_entity(identifier)
        if entity is None:
            raise InvalidQueryArguments(f"entity vanished: {identifier.value}")
        attributes = {
            key: _text(value) if isinstance(value, str) else value
            for key, value in entity.attributes.items()
        }
        return {"entity": self._view(entity), "attributes": attributes}

    def _neighbors(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        identifier = self._known(arguments["entity_id"])
        limit = self._limits.results(arguments.get("max_results"))
        pairs = self._query.neighbors(
            identifier,
            relation_kind=arguments.get("relation_kind"),
            direction=str(arguments.get("direction", "both")),
            limit=limit + 1,
        )
        rendered = [
            {
                "relation_kind": relation.kind,
                "direction": "out" if relation.source_id == identifier else "in",
                "entity": self._view(entity),
            }
            for relation, entity in pairs[:limit]
        ]
        return {"neighbors": rendered, "truncated": len(pairs) > limit}

    def _evidence(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        identifier = self._known(arguments["entity_id"])
        limit = self._limits.results(arguments.get("max_results"))
        found = self._query.evidence_of(identifier, limit=limit + 1)
        rendered = [
            {
                "evidence_id": evidence.id,
                "source_id": evidence.source_id,
                "version_hash": evidence.version_hash,
                "where": where_of(evidence.locator),
                "locator_kind": evidence.locator.kind,
                "excerpt": _text(evidence.excerpt, MAX_EXCERPT_CHARS),
            }
            for evidence in found[:limit]
        ]
        return {"evidence": rendered, "truncated": len(found) > limit}

    def _flow(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        identifier = self._known(arguments["entity_id"])
        limit = self._limits.results(arguments.get("max_results"))
        steps = self._query.flow(identifier, limit=limit + 1)
        rendered = [
            {
                "ordinal": step.ordinal,
                "via": step.via or "",
                "entity": self._view(step.entity),
            }
            for step in steps[:limit]
        ]
        return {"steps": rendered, "truncated": len(steps) > limit}

    def _rule_view(self, entity: Entity) -> dict[str, Any]:
        conditions = entity.attributes.get("conditions")
        effects = entity.attributes.get("effects")
        return {
            "entity": self._view(entity),
            "statement": _text(entity.attributes.get("statement") or entity.name),
            "conditions": _string_list(conditions),
            "effects": _string_list(effects),
        }

    def _rules(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        limit = self._limits.results(arguments.get("max_results"))
        raw = arguments.get("entity_id")
        if raw is None:
            found = self._query.entities(RULE_KIND, limit=limit + 1)
        else:
            identifier = self._known(raw)
            pairs = self._query.neighbors(
                identifier, direction="both", limit=self._limits.max_results_ceiling
            )
            found = tuple(
                entity for _relation, entity in pairs if entity.kind == RULE_KIND
            )
        return {
            "rules": [self._rule_view(entity) for entity in found[:limit]],
            "truncated": len(found) > limit,
        }

    def _gaps(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        limit = self._limits.results(arguments.get("max_results"))
        found = self._query.gaps(
            blocking_only=bool(arguments.get("blocking_only", False)), limit=limit + 1
        )
        rendered = [
            {
                "entity_id": gap.id.value,
                "question": _text(gap.attributes.get(GAP_QUESTION) or gap.name),
                "blocking": bool(gap.attributes.get(GAP_BLOCKING, False)),
                "status": str(gap.attributes.get(GAP_STATUS, "")),
            }
            for gap in found[:limit]
        ]
        return {"gaps": rendered, "truncated": len(found) > limit}

    def _compare(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        limit = self._limits.results(arguments.get("max_results"))
        report = self._query.compare(limit=limit)
        wanted = arguments.get("category")
        groups = {
            "declared_not_implemented": report.declared_not_implemented,
            "implemented_not_documented": report.implemented_not_documented,
            "proposal_conflicts": report.proposal_conflicts,
            "decision_supersedes": report.decision_supersedes,
            "source_contradicts_source": report.source_contradicts_source,
        }
        findings: list[dict[str, Any]] = []
        for category, items in groups.items():
            if wanted is not None and category != wanted:
                continue
            for finding in items:
                findings.append(
                    {
                        "category": category,
                        "entity_id": finding.entity_id,
                        "entity_name": _text(finding.entity_name, MAX_SUMMARY_CHARS),
                        "counterpart_id": finding.counterpart_id or "",
                        "counterpart_name": _text(
                            finding.counterpart_name or "", MAX_SUMMARY_CHARS
                        ),
                        "detail": _text(finding.detail),
                    }
                )
        return {"findings": findings[:limit]}

    def _impact(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        identifier = self._known(arguments["entity_id"])
        limit = self._limits.results(arguments.get("max_results"))
        depth = self._limits.depth(arguments.get("max_depth"))
        report = self._query.impact(identifier, max_depth=depth, limit=limit + 1)
        rendered = [
            {
                "depth": int(report.depth_by_entity.get(entity.id.value, 0)),
                "entity": self._view(entity),
            }
            for entity in report.impacted[:limit]
        ]
        return {
            "origin_id": identifier.value,
            "impacted": rendered,
            "truncated": len(report.impacted) > limit,
        }

    def _source(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        limit = self._limits.results(arguments.get("max_results"))
        raw = arguments.get("source_id")
        found = self._knowledge.source_versions(str(raw) if raw is not None else None)
        rendered = [
            {
                "source_id": version.source_id,
                "version_hash": version.version_hash,
                "locator_root": version.locator_root,
                "captured_at": version.captured_at,
            }
            for version in found[:limit]
        ]
        return {"sources": rendered, "truncated": len(found) > limit}
