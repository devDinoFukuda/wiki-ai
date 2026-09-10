from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from wiki_ai.knowledge.model import Confidence
from wiki_ai.knowledge.taxonomy import ENTITY_KIND_VALUES, RELATION_KIND_VALUES

__all__ = [
    "QueryToolError",
    "UnknownQueryTool",
    "InvalidQueryArguments",
    "QueryToolSpec",
    "QueryLimits",
    "MAX_ATTRIBUTE_CHARS",
    "MAX_EXCERPT_CHARS",
    "MAX_SUMMARY_CHARS",
    "INCLUDE_INFERRED",
    "INFERRED_TOOLS",
    "RULE_KIND",
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
    "build_specs",
    "text_of",
    "string_list",
    "weakest",
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

INCLUDE_INFERRED = "include_inferred"

INFERRED_TOOLS: tuple[str, ...] = (
    TOOL_NEIGHBORS,
    TOOL_FLOW,
    TOOL_RULES,
    TOOL_COMPARE,
    TOOL_IMPACT,
)

_CONFIDENCE_ORDER: tuple[str, ...] = (
    Confidence.SUPPORTED.value,
    Confidence.INFERRED.value,
    Confidence.UNRESOLVED.value,
    Confidence.CONTRADICTED.value,
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


def text_of(value: Any, ceiling: int = MAX_ATTRIBUTE_CHARS) -> str:
    body = str(value).strip()
    if len(body) <= ceiling:
        return body
    return body[:ceiling].rstrip() + "…"


def string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [text_of(item) for item in value]


def _include_inferred_property() -> dict[str, Any]:
    return {
        "type": "boolean",
        "description": (
            "false by default: only relations the evidence sustains are walked. "
            "true also walks relations the knowledge only infers, and every "
            "inferred relation comes back marked confidence \"inferred\": a "
            "claim built on it must carry that same mark."
        ),
    }


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
                    INCLUDE_INFERRED: _include_inferred_property(),
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
                                "relation_confidence": {"type": "string"},
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
                    INCLUDE_INFERRED: _include_inferred_property(),
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
                                "relation_confidence": {"type": "string"},
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
                    INCLUDE_INFERRED: _include_inferred_property(),
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
                                "relation_confidence": {"type": "string"},
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
                    INCLUDE_INFERRED: _include_inferred_property(),
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
                    INCLUDE_INFERRED: _include_inferred_property(),
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
                                "relation_confidence": {"type": "string"},
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


def weakest(first: str, second: str) -> str:
    if not first:
        return second
    if not second:
        return first
    return max(first, second, key=_CONFIDENCE_ORDER.index)
