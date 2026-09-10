from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from wiki_ai.knowledge.gaps import GAP_BLOCKING, GAP_QUESTION, GAP_STATUS
from wiki_ai.knowledge.grounding import excerpt_vocabulary
from wiki_ai.knowledge.matching import aliases_of
from wiki_ai.knowledge.model import (
    Confidence,
    Entity,
    EntityId,
    Evidence,
    GraphPolicy,
    Locator,
    Relation,
)
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository

from .query_tools import (
    INCLUDE_INFERRED,
    INFERRED_TOOLS,
    MAX_ATTRIBUTE_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_SUMMARY_CHARS,
    RULE_KIND,
    InvalidQueryArguments,
    QueryLimits,
    QueryToolError,
    QueryToolSpec,
    TOOL_COMPARE,
    TOOL_ENTITY,
    TOOL_EVIDENCE,
    TOOL_FLOW,
    TOOL_GAPS,
    TOOL_IMPACT,
    TOOL_NAMES,
    TOOL_NEIGHBORS,
    TOOL_RULES,
    TOOL_SEARCH,
    TOOL_SOURCE,
    UnknownQueryTool,
    build_specs,
    string_list,
    text_of,
    validate_arguments,
    weakest,
)

__all__ = [
    "QueryToolError",
    "UnknownQueryTool",
    "InvalidQueryArguments",
    "QueryToolSpec",
    "QueryLimits",
    "QueryFactory",
    "KnowledgeQueryHarness",
    "GroundingVocabulary",
    "MAX_EXCERPT_CHARS",
    "INCLUDE_INFERRED",
    "INFERRED_TOOLS",
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



@dataclass(frozen=True)
class GroundingVocabulary:
    referential: frozenset[str] = frozenset()
    semantic: frozenset[str] = frozenset()


def where_of(locator: Locator) -> str:
    payload = locator.to_dict() if hasattr(locator, "to_dict") else {}
    parts = [
        f"{key}={text_of(value, 120)}"
        for key, value in sorted(payload.items())
        if key != "kind" and value not in (None, "", (), [])
    ]
    return "; ".join(parts)


QueryFactory = Callable[..., KnowledgeQuery]


class KnowledgeQueryHarness:
    def __init__(
        self,
        knowledge: KnowledgeRepository,
        namespace: str,
        limits: QueryLimits | None = None,
        query_factory: QueryFactory = KnowledgeQuery,
        policy: GraphPolicy = GraphPolicy.SUPPORTED_ONLY,
    ) -> None:
        if policy is GraphPolicy.ALL:
            raise InvalidQueryArguments(
                "the answering harness never walks the whole graph as fact"
            )
        self._knowledge = knowledge
        self._namespace = namespace
        self._limits = limits or QueryLimits()
        self._policy = policy
        self._query_factory = query_factory
        self._query = query_factory(knowledge, policy=policy)
        self._inferred_query = query_factory(
            knowledge, policy=GraphPolicy.SUPPORTED_AND_INFERRED
        )
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

    @property
    def policy(self) -> GraphPolicy:
        return self._policy

    def _query_for(self, arguments: Mapping[str, Any]) -> KnowledgeQuery:
        if bool(arguments.get(INCLUDE_INFERRED, False)):
            return self._inferred_query
        return self._query

    def cited_relations(
        self, entity_ids: Sequence[str], evidence_ids: Sequence[str]
    ) -> tuple[Relation, ...]:
        wanted = {str(item) for item in evidence_ids}
        scope = {str(item) for item in entity_ids}
        found: list[Relation] = []
        seen: set[str] = set()
        for identifier in scope:
            owner = EntityId(identifier)
            for relation in self._knowledge.relations_of(
                owner, policy=GraphPolicy.ALL
            ):
                if relation.id in seen:
                    continue
                both_cited = (
                    relation.source_id.value in scope
                    and relation.target_id.value in scope
                )
                linked = any(
                    item.id in wanted
                    for item in self._knowledge.evidence_for_relation(relation.id)
                )
                if not both_cited and not linked:
                    continue
                seen.add(relation.id)
                found.append(relation)
        return tuple(sorted(found, key=lambda item: item.id))

    def unsupported_relations(
        self, entity_ids: Sequence[str], evidence_ids: Sequence[str]
    ) -> tuple[Relation, ...]:
        return tuple(
            relation
            for relation in self.cited_relations(entity_ids, evidence_ids)
            if relation.confidence is not Confidence.SUPPORTED
        )

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
            for relation in self._knowledge.relations_of(
                owner, policy=GraphPolicy.ALL
            ):
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
    ) -> GroundingVocabulary:
        semantic: set[str] = set()
        for identifier in evidence_ids:
            evidence = self.evidence_by_id(identifier)
            if evidence is None:
                continue
            semantic |= excerpt_vocabulary(evidence.excerpt)
        referential: set[str] = set()
        for identifier in entity_ids:
            entity = self.entity_by_id(identifier)
            if entity is None:
                continue
            referential |= excerpt_vocabulary(entity.name)
            referential |= excerpt_vocabulary(entity.kind)
            for alias in aliases_of(entity.name, entity.attributes):
                referential |= excerpt_vocabulary(alias)
        return GroundingVocabulary(
            referential=frozenset(referential), semantic=frozenset(semantic)
        )

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
            "name": text_of(entity.name, MAX_SUMMARY_CHARS),
            "state": entity.state.value,
            "confidence": entity.confidence.value,
            "summary": text_of(summary or entity.name, MAX_SUMMARY_CHARS),
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
            key: text_of(value) if isinstance(value, str) else value
            for key, value in entity.attributes.items()
        }
        return {"entity": self._view(entity), "attributes": attributes}

    def _neighbors(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        identifier = self._known(arguments["entity_id"])
        limit = self._limits.results(arguments.get("max_results"))
        pairs = self._query_for(arguments).neighbors(
            identifier,
            relation_kind=arguments.get("relation_kind"),
            direction=str(arguments.get("direction", "both")),
            limit=limit + 1,
        )
        rendered = [
            {
                "relation_kind": relation.kind,
                "direction": "out" if relation.source_id == identifier else "in",
                "relation_confidence": relation.confidence.value,
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
                "excerpt": text_of(evidence.excerpt, MAX_EXCERPT_CHARS),
            }
            for evidence in found[:limit]
        ]
        return {"evidence": rendered, "truncated": len(found) > limit}

    def _flow(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        identifier = self._known(arguments["entity_id"])
        limit = self._limits.results(arguments.get("max_results"))
        steps = self._query_for(arguments).flow(identifier, limit=limit + 1)
        rendered = [
            {
                "ordinal": step.ordinal,
                "via": step.via or "",
                "relation_confidence": self._path_confidence(
                    identifier, steps[:limit], step
                ),
                "entity": self._view(step.entity),
            }
            for step in steps[:limit]
        ]
        return {"steps": rendered, "truncated": len(steps) > limit}

    def _path_confidence(
        self, origin: EntityId, steps: Sequence[Any], target: Any
    ) -> str:
        chain = [origin] + [item.entity.id for item in steps]
        index = chain.index(target.entity.id)
        found = ""
        for position in range(index):
            edge = self._edge_confidence(chain[position], chain[position + 1])
            found = weakest(found, edge)
        return found

    def _reached_confidence(
        self,
        entity: Entity,
        origin: EntityId,
        reached: Sequence[Entity],
        depth_by_entity: Mapping[str, int],
    ) -> str:
        level = int(depth_by_entity.get(entity.id.value, 0))
        closer = [origin] if level <= 1 else [
            item.id
            for item in reached
            if int(depth_by_entity.get(item.id.value, 0)) == level - 1
        ]
        found = ""
        for other in closer:
            edge = self._edge_confidence(entity.id, other)
            if edge:
                found = weakest(found, edge)
        return found

    def _edge_confidence(self, first: EntityId, second: EntityId) -> str:
        ends = {first.value, second.value}
        found = ""
        for relation in self._knowledge.relations_of(first, policy=GraphPolicy.ALL):
            if {relation.source_id.value, relation.target_id.value} != ends:
                continue
            found = weakest(found, relation.confidence.value)
        return found

    def _rule_view(self, entity: Entity, relation_confidence: str) -> dict[str, Any]:
        conditions = entity.attributes.get("conditions")
        effects = entity.attributes.get("effects")
        return {
            "entity": self._view(entity),
            "relation_confidence": relation_confidence,
            "statement": text_of(entity.attributes.get("statement") or entity.name),
            "conditions": string_list(conditions),
            "effects": string_list(effects),
        }

    def _rules(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        limit = self._limits.results(arguments.get("max_results"))
        raw = arguments.get("entity_id")
        query = self._query_for(arguments)
        found: list[tuple[Entity, str]]
        if raw is None:
            found = [
                (entity, "")
                for entity in query.entities(RULE_KIND, limit=limit + 1)
            ]
        else:
            identifier = self._known(raw)
            pairs = query.neighbors(
                identifier, direction="both", limit=self._limits.max_results_ceiling
            )
            found = [
                (entity, relation.confidence.value)
                for relation, entity in pairs
                if entity.kind == RULE_KIND
            ]
        return {
            "rules": [
                self._rule_view(entity, confidence)
                for entity, confidence in found[:limit]
            ],
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
                "question": text_of(gap.attributes.get(GAP_QUESTION) or gap.name),
                "blocking": bool(gap.attributes.get(GAP_BLOCKING, False)),
                "status": str(gap.attributes.get(GAP_STATUS, "")),
            }
            for gap in found[:limit]
        ]
        return {"gaps": rendered, "truncated": len(found) > limit}

    def _compare(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        limit = self._limits.results(arguments.get("max_results"))
        report = self._query_for(arguments).compare(limit=limit)
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
                        "entity_name": text_of(finding.entity_name, MAX_SUMMARY_CHARS),
                        "counterpart_id": finding.counterpart_id or "",
                        "counterpart_name": text_of(
                            finding.counterpart_name or "", MAX_SUMMARY_CHARS
                        ),
                        "detail": text_of(finding.detail),
                    }
                )
        return {"findings": findings[:limit]}

    def _impact(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        identifier = self._known(arguments["entity_id"])
        limit = self._limits.results(arguments.get("max_results"))
        depth = self._limits.depth(arguments.get("max_depth"))
        report = self._query_for(arguments).impact(
            identifier, max_depth=depth, limit=limit + 1
        )
        rendered = [
            {
                "depth": int(report.depth_by_entity.get(entity.id.value, 0)),
                "relation_confidence": self._reached_confidence(
                    entity, identifier, report.impacted, report.depth_by_entity
                ),
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
