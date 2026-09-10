from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from wiki_ai.knowledge.gaps import GAP_KIND, is_blocking, is_open
from wiki_ai.knowledge.model import Confidence, Entity, EntityId, EpistemicStatus, Relation
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind
from wiki_ai.investigation.profile_gaps import (
    SECTION_ATTRIBUTE,
    SECTION_RULES,
    Applicability,
    open_profile_gaps,
    profile_gaps,
)

AUTHOR = "test"


@pytest.fixture
def knowledge(tmp_path: Path):
    with KnowledgeRepository.open(str(tmp_path / "state.db")) as repository:
        yield repository


def entity(
    kind: EntityKind, name: str, attributes: Mapping[str, Any] | None = None
) -> Entity:
    return Entity.create(
        kind=kind.value,
        name=name,
        attributes=dict(attributes or {}),
        epistemic=EpistemicStatus.IMPLEMENTED,
        confidence=Confidence.INFERRED,
    )


def store(
    repository: KnowledgeRepository,
    entities: tuple[Entity, ...],
    relations: tuple[tuple[RelationKind, Entity, Entity], ...] = (),
) -> None:
    with repository.begin_revision(author=AUTHOR, summary="fixture") as revision:
        for item in entities:
            revision.put_entity(item)
        for kind, source, target in relations:
            revision.put_relation(
                Relation.create(kind.value, source.id, target.id)
            )


def sections_of(gaps) -> set[str]:
    return {gap.section for gap in gaps}


def test_the_always_applicable_sections_are_gaps_on_a_bare_capability(
    knowledge: KnowledgeRepository,
) -> None:
    capability = entity(EntityKind.CAPABILITY, "place order")
    store(knowledge, (capability,))
    found = profile_gaps(KnowledgeQuery(knowledge), capability.id)
    always = {
        rule.section
        for rule in SECTION_RULES
        if rule.applicability is Applicability.ALWAYS
    }
    assert always <= sections_of(found)


def test_retries_is_not_a_gap_when_there_is_no_integration_at_all(
    knowledge: KnowledgeRepository,
) -> None:
    capability = entity(EntityKind.CAPABILITY, "place order")
    store(knowledge, (capability,))
    found = profile_gaps(KnowledgeQuery(knowledge), capability.id)
    assert "retries" not in sections_of(found)
    assert "fallbacks" not in sections_of(found)
    assert "timeouts" not in sections_of(found)


def test_retries_becomes_a_gap_once_an_integration_is_reachable(
    knowledge: KnowledgeRepository,
) -> None:
    capability = entity(EntityKind.CAPABILITY, "place order")
    integration = entity(
        EntityKind.INTEGRATION,
        "orders kafka",
        {"direction": "outbound", "protocol": "kafka"},
    )
    store(
        knowledge,
        (capability, integration),
        ((RelationKind.CALLS, capability, integration),),
    )
    found = profile_gaps(KnowledgeQuery(knowledge), capability.id)
    assert "retries" in sections_of(found)
    assert "timeouts" in sections_of(found)


def test_persistence_is_not_a_gap_when_the_capability_already_names_its_storage(
    knowledge: KnowledgeRepository,
) -> None:
    capability = entity(EntityKind.CAPABILITY, "place order")
    table = entity(EntityKind.TABLE, "ORDERS", {"schema": "public"})
    store(
        knowledge,
        (capability, table),
        ((RelationKind.PERSISTS_TO, capability, table),),
    )
    found = profile_gaps(KnowledgeQuery(knowledge), capability.id)
    assert "persistence" not in sections_of(found)


def test_persistence_is_a_blocking_gap_when_only_a_member_touches_storage(
    knowledge: KnowledgeRepository,
) -> None:
    capability = entity(EntityKind.CAPABILITY, "place order")
    entry = entity(
        EntityKind.ENTRY_POINT,
        "OrderController place",
        {"mechanism": "http", "location": "OrderController.place"},
    )
    table = entity(EntityKind.TABLE, "ORDERS", {"schema": "public"})
    store(
        knowledge,
        (capability, entry, table),
        (
            (RelationKind.BELONGS_TO, entry, capability),
            (RelationKind.PERSISTS_TO, entry, table),
        ),
    )
    found = profile_gaps(KnowledgeQuery(knowledge), capability.id)
    persistence = [gap for gap in found if gap.section == "persistence"]
    assert persistence and persistence[0].blocking
    assert persistence[0].signals


def test_a_filled_section_is_never_reopened_as_a_gap(
    knowledge: KnowledgeRepository,
) -> None:
    capability = entity(EntityKind.CAPABILITY, "place order")
    rule = entity(
        EntityKind.BUSINESS_RULE,
        "the reference is mandatory",
        {
            "statement": "the reference is mandatory",
            "conditions": ["no reference"],
            "effects": ["reject"],
        },
    )
    store(
        knowledge,
        (capability, rule),
        ((RelationKind.BELONGS_TO, rule, capability),),
    )
    found = profile_gaps(KnowledgeQuery(knowledge), capability.id)
    assert "rules" not in sections_of(found)


def test_decisions_is_a_gap_only_when_a_rule_declares_conditions(
    knowledge: KnowledgeRepository,
) -> None:
    capability = entity(EntityKind.CAPABILITY, "place order")
    rule = entity(
        EntityKind.BUSINESS_RULE,
        "the reference is mandatory",
        {
            "statement": "the reference is mandatory",
            "conditions": ["no reference"],
            "effects": ["reject"],
        },
    )
    store(
        knowledge,
        (capability, rule),
        ((RelationKind.BELONGS_TO, rule, capability),),
    )
    found = profile_gaps(KnowledgeQuery(knowledge), capability.id)
    decisions = [gap for gap in found if gap.section == "decisions"]
    assert decisions and decisions[0].blocking


def test_the_purpose_section_is_filled_by_the_capability_statement(
    knowledge: KnowledgeRepository,
) -> None:
    capability = entity(
        EntityKind.CAPABILITY, "place order", {"statement": "places an order"}
    )
    store(knowledge, (capability,))
    found = profile_gaps(KnowledgeQuery(knowledge), capability.id)
    assert "purpose" not in sections_of(found)


def test_open_profile_gaps_writes_typed_gaps_about_the_capability(
    knowledge: KnowledgeRepository,
) -> None:
    capability = entity(EntityKind.CAPABILITY, "place order")
    store(knowledge, (capability,))
    query = KnowledgeQuery(knowledge)
    with knowledge.begin_revision(author=AUTHOR, summary="gaps") as revision:
        opened = open_profile_gaps(revision, query, capability.id)
    stored = knowledge.find_entities(GAP_KIND)
    assert len(stored) == len(opened)
    assert all(is_open(gap) for gap in stored)
    assert {str(gap.attributes[SECTION_ATTRIBUTE]) for gap in stored} == sections_of(
        opened
    )
    assert any(is_blocking(gap) for gap in stored)
    profile = query.capability_profile(capability.id)
    assert profile is not None
    assert len(profile.gaps) == len(stored)


def test_opening_the_same_gaps_twice_does_not_duplicate_them(
    knowledge: KnowledgeRepository,
) -> None:
    capability = entity(EntityKind.CAPABILITY, "place order")
    store(knowledge, (capability,))
    query = KnowledgeQuery(knowledge)
    for _ in range(2):
        with knowledge.begin_revision(author=AUTHOR, summary="gaps") as revision:
            open_profile_gaps(revision, query, capability.id)
    stored = knowledge.find_entities(GAP_KIND)
    assert len(stored) == len({gap.id.value for gap in stored})


def test_an_unknown_capability_yields_no_gap(knowledge: KnowledgeRepository) -> None:
    assert profile_gaps(KnowledgeQuery(knowledge), EntityId("ent_missing")) == ()
