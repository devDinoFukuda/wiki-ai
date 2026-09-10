from __future__ import annotations

import pytest

from wiki_ai.knowledge.errors import (
    InvalidRelationPair,
    MissingRequiredAttribute,
    UnknownKind,
)
from wiki_ai.knowledge.model import KIND_PATTERN
from wiki_ai.knowledge.taxonomy import (
    ALLOWED_PAIRS,
    REQUIRED_ATTRIBUTES,
    EntityKind,
    RelationKind,
    entity_kind,
    pair_allowed,
    relation_kind,
    validate_attributes,
    validate_pair,
)

PLAN_ENTITIES = (
    "System",
    "Module",
    "Capability",
    "EntryPoint",
    "BusinessRule",
    "Invariant",
    "Precondition",
    "Postcondition",
    "EdgeCase",
    "Flow",
    "FlowStep",
    "Decision",
    "State",
    "StateTransition",
    "Input",
    "Output",
    "DataContract",
    "DataField",
    "Validation",
    "Integration",
    "Operation",
    "Endpoint",
    "Protocol",
    "Event",
    "Topic",
    "Queue",
    "Persistence",
    "DataEntity",
    "Table",
    "Query",
    "Procedure",
    "Transaction",
    "FailureMode",
    "RetryPolicy",
    "Fallback",
    "TimeoutPolicy",
    "IdempotencyPolicy",
    "Configuration",
    "Dependency",
    "TestScenario",
    "Initiative",
    "Requirement",
    "Proposal",
    "DecisionRecord",
    "Source",
    "Gap",
)

PLAN_RELATIONS = (
    "implements",
    "calls",
    "consumes",
    "publishes",
    "reads",
    "writes",
    "validates",
    "depends_on",
    "triggers",
    "transitions_to",
    "handles",
    "retries",
    "falls_back_to",
    "persists_to",
    "tests",
    "declares",
    "proposes_change_to",
    "contradicts",
    "supersedes",
    "affects",
    "belongs_to",
)


def camel_to_snake(name: str) -> str:
    parts: list[str] = []
    for char in name:
        if char.isupper() and parts:
            parts.append("_")
        parts.append(char.lower())
    return "".join(parts)


@pytest.mark.parametrize("declared", PLAN_ENTITIES)
def test_every_plan_entity_exists_in_taxonomy(declared):
    assert camel_to_snake(declared) in {kind.value for kind in EntityKind}


def test_taxonomy_has_no_entity_beyond_the_plan():
    expected = {camel_to_snake(name) for name in PLAN_ENTITIES}
    assert {kind.value for kind in EntityKind} == expected


@pytest.mark.parametrize("kind", list(EntityKind))
def test_entity_kind_values_are_snake_case(kind):
    assert KIND_PATTERN.fullmatch(kind.value)


@pytest.mark.parametrize("declared", PLAN_RELATIONS)
def test_every_plan_relation_exists_in_taxonomy(declared):
    assert declared in {kind.value for kind in RelationKind}


def test_taxonomy_has_no_relation_beyond_the_plan():
    assert {kind.value for kind in RelationKind} == set(PLAN_RELATIONS)


@pytest.mark.parametrize("kind", list(RelationKind))
def test_relation_kind_values_are_snake_case(kind):
    assert KIND_PATTERN.fullmatch(kind.value)


@pytest.mark.parametrize("relation", list(RelationKind))
def test_every_relation_has_allowed_pairs(relation):
    assert relation in ALLOWED_PAIRS
    assert ALLOWED_PAIRS[relation]


@pytest.mark.parametrize("relation", list(RelationKind))
def test_allowed_pairs_only_reference_known_kinds(relation):
    for source, target in ALLOWED_PAIRS[relation]:
        assert source is None or isinstance(source, EntityKind)
        assert target is None or isinstance(target, EntityKind)


@pytest.mark.parametrize("kind", sorted(REQUIRED_ATTRIBUTES, key=lambda k: k.value))
def test_required_attributes_reference_known_kinds(kind):
    assert isinstance(kind, EntityKind)
    assert REQUIRED_ATTRIBUTES[kind]


def test_entity_kind_outside_taxonomy_is_rejected():
    with pytest.raises(UnknownKind):
        entity_kind("component")


def test_relation_kind_outside_taxonomy_is_rejected():
    with pytest.raises(UnknownKind):
        relation_kind("mentions")


def test_transitions_to_only_between_states():
    validate_pair(RelationKind.TRANSITIONS_TO, EntityKind.STATE, EntityKind.STATE)
    with pytest.raises(InvalidRelationPair):
        validate_pair(
            RelationKind.TRANSITIONS_TO, EntityKind.CAPABILITY, EntityKind.STATE
        )


def test_tests_accepts_any_target_from_test_scenario():
    for target in EntityKind:
        assert pair_allowed(RelationKind.TESTS, EntityKind.TEST_SCENARIO, target)
    with pytest.raises(InvalidRelationPair):
        validate_pair(RelationKind.TESTS, EntityKind.CAPABILITY, EntityKind.FLOW)


def test_persists_to_accepts_any_source_but_only_storage_targets():
    validate_pair("persists_to", "capability", "table")
    validate_pair("persists_to", "flow_step", "data_entity")
    validate_pair("persists_to", "operation", "persistence")
    with pytest.raises(InvalidRelationPair):
        validate_pair("persists_to", "capability", "event")


@pytest.mark.parametrize("relation", ["contradicts", "supersedes"])
def test_contradicts_and_supersedes_require_the_same_kind(relation):
    validate_pair(relation, "proposal", "proposal")
    with pytest.raises(InvalidRelationPair):
        validate_pair(relation, "proposal", "business_rule")


def test_belongs_to_hierarchy():
    validate_pair("belongs_to", "module", "system")
    validate_pair("belongs_to", "capability", "module")
    validate_pair("belongs_to", "capability", "system")
    validate_pair("belongs_to", "flow_step", "flow")
    validate_pair("belongs_to", "data_field", "data_contract")
    with pytest.raises(InvalidRelationPair):
        validate_pair("belongs_to", "system", "module")
    with pytest.raises(InvalidRelationPair):
        validate_pair("belongs_to", "flow", "flow_step")


def test_depends_on_accepts_any_pair():
    for source in EntityKind:
        assert pair_allowed(RelationKind.DEPENDS_ON, source, EntityKind.DEPENDENCY)


def test_validate_pair_returns_typed_triple():
    relation, source, target = validate_pair("calls", "capability", "capability")
    assert relation is RelationKind.CALLS
    assert source is EntityKind.CAPABILITY
    assert target is EntityKind.CAPABILITY


def test_validate_pair_rejects_unknown_relation_kind():
    with pytest.raises(UnknownKind):
        validate_pair("mentions", "capability", "capability")


def test_business_rule_requires_statement_conditions_effects():
    with pytest.raises(MissingRequiredAttribute):
        validate_attributes(EntityKind.BUSINESS_RULE, {"statement": "x"})
    validate_attributes(
        EntityKind.BUSINESS_RULE,
        {"statement": "x", "conditions": ["c"], "effects": ["e"]},
    )


def test_entry_point_requires_mechanism_and_location():
    with pytest.raises(MissingRequiredAttribute):
        validate_attributes("entry_point", {"mechanism": "http"})
    validate_attributes("entry_point", {"mechanism": "http", "location": "/orders"})


def test_integration_requires_direction_and_protocol():
    with pytest.raises(MissingRequiredAttribute):
        validate_attributes("integration", {"direction": "outbound"})
    validate_attributes("integration", {"direction": "outbound", "protocol": "http"})


def test_failure_mode_requires_trigger_and_effect():
    with pytest.raises(MissingRequiredAttribute):
        validate_attributes("failure_mode", {"trigger": "timeout"})
    validate_attributes("failure_mode", {"trigger": "timeout", "effect": "rejeita"})


def test_edge_case_requires_condition_and_expected():
    with pytest.raises(MissingRequiredAttribute):
        validate_attributes("edge_case", {"condition": "lista vazia"})
    validate_attributes("edge_case", {"condition": "lista vazia", "expected": "erro"})


def test_gap_requires_question_and_boolean_blocking():
    with pytest.raises(MissingRequiredAttribute):
        validate_attributes("gap", {"question": "quem aprova?"})
    with pytest.raises(MissingRequiredAttribute):
        validate_attributes("gap", {"question": "quem aprova?", "blocking": "sim"})
    validate_attributes("gap", {"question": "quem aprova?", "blocking": False})


def test_blank_required_attribute_is_rejected():
    with pytest.raises(MissingRequiredAttribute):
        validate_attributes("requirement", {"statement": "   "})
    with pytest.raises(MissingRequiredAttribute):
        validate_attributes(
            "business_rule", {"statement": "x", "conditions": [], "effects": ["e"]}
        )


def test_kinds_without_required_attributes_accept_empty_mapping():
    validate_attributes(EntityKind.SYSTEM, {})
    validate_attributes(EntityKind.CAPABILITY, {})
    validate_attributes(EntityKind.MODULE, {})
