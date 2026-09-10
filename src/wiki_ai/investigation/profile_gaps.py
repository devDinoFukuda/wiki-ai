from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from wiki_ai.knowledge.gaps import open_gap
from wiki_ai.knowledge.model import Entity, EntityId
from wiki_ai.knowledge.query import CapabilityProfile, KnowledgeQuery
from wiki_ai.knowledge.repository import RevisionTransaction
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind

__all__ = [
    "SECTION_ATTRIBUTE",
    "Applicability",
    "SectionRule",
    "SECTION_RULES",
    "SectionGap",
    "profile_gaps",
    "open_profile_gaps",
]

SECTION_ATTRIBUTE = "section"
_CAPABILITY_ATTRIBUTE = "capability"


class Applicability(str, Enum):
    ALWAYS = "always"
    WHEN_SIGNAL = "when_signal"


@dataclass(frozen=True)
class SectionRule:
    section: str
    question: str
    applicability: Applicability
    blocking: bool
    signal_relations: tuple[RelationKind, ...] = ()
    signal_kinds: tuple[EntityKind, ...] = ()
    signal_attributes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "question": self.question,
            "applicability": self.applicability.value,
            "blocking": self.blocking,
            "signal_relations": [kind.value for kind in self.signal_relations],
            "signal_kinds": [kind.value for kind in self.signal_kinds],
            "signal_attributes": list(self.signal_attributes),
        }


SECTION_RULES: tuple[SectionRule, ...] = (
    SectionRule(
        section="purpose",
        question="what does this capability do, as decided by its executable code",
        applicability=Applicability.ALWAYS,
        blocking=True,
        signal_attributes=("statement", "purpose"),
    ),
    SectionRule(
        section="entrypoints",
        question="which entry points reach this capability",
        applicability=Applicability.ALWAYS,
        blocking=True,
    ),
    SectionRule(
        section="inputs",
        question="what data enters this capability and with which shape",
        applicability=Applicability.ALWAYS,
        blocking=True,
    ),
    SectionRule(
        section="outputs",
        question="what does this capability return or emit",
        applicability=Applicability.ALWAYS,
        blocking=True,
    ),
    SectionRule(
        section="preconditions",
        question="what must hold before this capability runs",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=False,
        signal_relations=(RelationKind.VALIDATES,),
        signal_kinds=(EntityKind.VALIDATION, EntityKind.INPUT),
    ),
    SectionRule(
        section="rules",
        question="which business rules does this capability implement",
        applicability=Applicability.ALWAYS,
        blocking=True,
    ),
    SectionRule(
        section="invariants",
        question="what must stay true across this capability",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=False,
        signal_kinds=(EntityKind.BUSINESS_RULE, EntityKind.STATE),
    ),
    SectionRule(
        section="decisions",
        question="which branches decide the behaviour of this capability",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=True,
        signal_kinds=(EntityKind.BUSINESS_RULE,),
        signal_attributes=("conditions",),
    ),
    SectionRule(
        section="states",
        question="which states does this capability move between",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=False,
        signal_relations=(RelationKind.WRITES, RelationKind.TRANSITIONS_TO),
        signal_kinds=(EntityKind.STATE, EntityKind.STATE_TRANSITION),
    ),
    SectionRule(
        section="persistence",
        question="what does this capability read or write and where",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=True,
        signal_relations=(
            RelationKind.PERSISTS_TO,
            RelationKind.WRITES,
            RelationKind.READS,
        ),
        signal_kinds=(
            EntityKind.TABLE,
            EntityKind.DATA_ENTITY,
            EntityKind.PERSISTENCE,
            EntityKind.QUERY,
        ),
    ),
    SectionRule(
        section="integrations",
        question="which external systems does this capability talk to",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=True,
        signal_relations=(RelationKind.CALLS, RelationKind.CONSUMES),
        signal_kinds=(
            EntityKind.INTEGRATION,
            EntityKind.ENDPOINT,
            EntityKind.PROTOCOL,
            EntityKind.DEPENDENCY,
        ),
    ),
    SectionRule(
        section="events",
        question="which events does this capability publish or consume",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=True,
        signal_relations=(RelationKind.PUBLISHES, RelationKind.CONSUMES),
        signal_kinds=(EntityKind.EVENT, EntityKind.TOPIC, EntityKind.QUEUE),
    ),
    SectionRule(
        section="failures",
        question="how does this capability fail and what happens then",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=True,
        signal_relations=(RelationKind.HANDLES,),
        signal_kinds=(
            EntityKind.FAILURE_MODE,
            EntityKind.INTEGRATION,
            EntityKind.PERSISTENCE,
        ),
    ),
    SectionRule(
        section="retries",
        question="does this capability retry, how many times and with which interval",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=False,
        signal_relations=(RelationKind.RETRIES,),
        signal_kinds=(
            EntityKind.INTEGRATION,
            EntityKind.RETRY_POLICY,
            EntityKind.FAILURE_MODE,
        ),
    ),
    SectionRule(
        section="fallbacks",
        question="what does this capability do when the main path is unavailable",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=False,
        signal_relations=(RelationKind.FALLS_BACK_TO,),
        signal_kinds=(
            EntityKind.INTEGRATION,
            EntityKind.FALLBACK,
            EntityKind.FAILURE_MODE,
        ),
    ),
    SectionRule(
        section="timeouts",
        question="which timeouts bound this capability",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=False,
        signal_kinds=(EntityKind.INTEGRATION, EntityKind.TIMEOUT_POLICY),
    ),
    SectionRule(
        section="idempotency",
        question="what happens when this capability runs twice with the same input",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=False,
        signal_relations=(RelationKind.PERSISTS_TO, RelationKind.PUBLISHES),
        signal_kinds=(
            EntityKind.IDEMPOTENCY_POLICY,
            EntityKind.EVENT,
            EntityKind.PERSISTENCE,
        ),
    ),
    SectionRule(
        section="edge_cases",
        question="which boundary inputs does the code treat differently",
        applicability=Applicability.ALWAYS,
        blocking=False,
    ),
    SectionRule(
        section="tests",
        question="which executable tests exercise this capability",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=False,
        signal_kinds=(EntityKind.TEST_SCENARIO,),
    ),
    SectionRule(
        section="dependencies",
        question="what does this capability depend on to run",
        applicability=Applicability.WHEN_SIGNAL,
        blocking=False,
        signal_relations=(RelationKind.DEPENDS_ON, RelationKind.CALLS),
        signal_kinds=(EntityKind.DEPENDENCY, EntityKind.MODULE),
    ),
)

_RULES_BY_SECTION: Mapping[str, SectionRule] = {
    rule.section: rule for rule in SECTION_RULES
}


@dataclass(frozen=True)
class SectionGap:
    section: str
    question: str
    blocking: bool
    signals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "question": self.question,
            "blocking": self.blocking,
            "signals": list(self.signals),
        }


def profile_gaps(
    query: KnowledgeQuery,
    capability_id: EntityId,
    rules: Sequence[SectionRule] = SECTION_RULES,
) -> tuple[SectionGap, ...]:
    profile = query.capability_profile(capability_id)
    if profile is None:
        return ()
    filled = _filled_sections(profile)
    neighbourhood = _neighbourhood(query, capability_id)
    found: list[SectionGap] = []
    for rule in rules:
        if rule.section in filled:
            continue
        signals = _signals(rule, profile, neighbourhood)
        if rule.applicability is Applicability.WHEN_SIGNAL and not signals:
            continue
        found.append(
            SectionGap(
                section=rule.section,
                question=f"{rule.question} ({profile.capability.name})",
                blocking=rule.blocking,
                signals=signals,
            )
        )
    return tuple(found)


def open_profile_gaps(
    transaction: RevisionTransaction,
    query: KnowledgeQuery,
    capability_id: EntityId,
    rules: Sequence[SectionRule] = SECTION_RULES,
) -> tuple[SectionGap, ...]:
    found = profile_gaps(query, capability_id, rules)
    for gap in found:
        open_gap(
            transaction,
            gap.question,
            about=capability_id,
            blocking=gap.blocking,
            attributes={
                SECTION_ATTRIBUTE: gap.section,
                _CAPABILITY_ATTRIBUTE: capability_id.value,
                "signals": list(gap.signals),
            },
        )
    return found


def _filled_sections(profile: CapabilityProfile) -> frozenset[str]:
    filled = {name for name, values in profile.sections().items() if values}
    if _purpose_of(profile.capability):
        filled.add("purpose")
    return frozenset(filled)


def _purpose_of(capability: Entity) -> str:
    for name in ("statement", "purpose", "description"):
        value = capability.attributes.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _neighbourhood(
    query: KnowledgeQuery, capability_id: EntityId
) -> tuple[tuple[str, str, str], ...]:
    found: list[tuple[str, str, str]] = []
    for relation, entity in query.neighbors(capability_id, limit=1000):
        found.append((relation.kind, entity.kind, entity.name))
    for member_relation, member in query.neighbors(
        capability_id, RelationKind.BELONGS_TO, limit=1000
    ):
        for relation, entity in query.neighbors(member.id, limit=1000):
            found.append((relation.kind, entity.kind, entity.name))
        found.append((member_relation.kind, member.kind, member.name))
    return tuple(dict.fromkeys(found))


def _signals(
    rule: SectionRule,
    profile: CapabilityProfile,
    neighbourhood: Sequence[tuple[str, str, str]],
) -> tuple[str, ...]:
    relation_values = {kind.value for kind in rule.signal_relations}
    kind_values = {kind.value for kind in rule.signal_kinds}
    found: list[str] = []
    for relation_kind, entity_kind, name in neighbourhood:
        if relation_kind in relation_values:
            found.append(f"{relation_kind} {name}")
        elif entity_kind in kind_values:
            found.append(f"{entity_kind} {name}")
    for attribute in rule.signal_attributes:
        for entity in _members_with_attribute(profile, attribute):
            found.append(f"{entity.kind}.{attribute} {entity.name}")
    return tuple(dict.fromkeys(found))


def _members_with_attribute(
    profile: CapabilityProfile, attribute: str
) -> tuple[Entity, ...]:
    found: list[Entity] = []
    for entities in profile.sections().values():
        for entity in entities:
            value = entity.attributes.get(attribute)
            if value and entity not in found:
                found.append(entity)
    return tuple(found)


def rule_for(section: str) -> SectionRule | None:
    return _RULES_BY_SECTION.get(section)
