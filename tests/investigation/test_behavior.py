from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from wiki_ai.knowledge.evidence import CodeContent, CodeLocator
from wiki_ai.knowledge.identity import excerpt_digest
from wiki_ai.knowledge.model import (
    Confidence,
    Entity,
    EntityId,
    KnowledgeState,
    Evidence,
    Relation,
    SourceVersion,
)
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind
from wiki_ai.investigation.behavior import build_flows, write_flows

AUTHOR = "test"
NAMESPACE = "acme"
DIGEST = "d" * 64
STAMP = "2026-09-10T00:00:00+00:00"
VERSION = SourceVersion(
    source_id=NAMESPACE, version_hash=DIGEST, locator_root="/repo", captured_at=STAMP
)


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
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.INFERRED,
        source_versions=(VERSION.key,),
    )


def evidence_at(path: str, start: int, end: int) -> Evidence:
    locator = CodeLocator(
        path=path, line_start=start, line_end=end, content=CodeContent.EXECUTABLE
    )
    excerpt = f"public void handle() {{ /* {path}:{start}-{end} */ }}"
    return Evidence(
        id=f"ev_{path}_{start}_{end}".replace("/", "_").replace(".", "_"),
        source_id=NAMESPACE,
        version_hash=DIGEST,
        locator=locator,
        excerpt_hash=excerpt_digest(excerpt),
        captured_at=STAMP,
        excerpt=excerpt,
    )


def java_graph(repository: KnowledgeRepository) -> Entity:
    capability = entity(EntityKind.CAPABILITY, "place order", {"statement": "places"})
    entry = entity(
        EntityKind.ENTRY_POINT,
        "OrderController place",
        {"mechanism": "http", "location": "OrderController.place"},
    )
    validation = entity(
        EntityKind.VALIDATION, "reference is present", {"rule": "reference not blank"}
    )
    operation = entity(EntityKind.OPERATION, "OrderService place", {"verb": "place"})
    table = entity(EntityKind.TABLE, "ORDERS", {"schema": "public"})
    event = entity(EntityKind.EVENT, "order placed")
    rule = entity(
        EntityKind.BUSINESS_RULE,
        "an order without a reference is rejected",
        {
            "statement": "an order without a reference is rejected",
            "conditions": ["the reference is blank"],
            "effects": ["reject the order"],
        },
    )
    with repository.begin_revision(author=AUTHOR, summary="java") as revision:
        revision.put_source_version(VERSION)
        for item in (capability, entry, validation, operation, table, event, rule):
            revision.put_entity(item)
        for kind, source, target in (
            (RelationKind.BELONGS_TO, entry, capability),
            (RelationKind.BELONGS_TO, rule, capability),
            (RelationKind.VALIDATES, validation, entry),
            (RelationKind.CALLS, entry, operation),
            (RelationKind.PERSISTS_TO, operation, table),
            (RelationKind.PUBLISHES, operation, event),
        ):
            revision.put_relation(Relation.create(kind.value, source.id, target.id))
        revision.put_evidence(
            evidence_at("OrderController.java", 15, 17), entity_ids=(entry.id,)
        )
        revision.put_evidence(
            evidence_at("OrderService.java", 10, 12), entity_ids=(operation.id,)
        )
        revision.put_evidence(
            evidence_at("OrderRepository.java", 3, 4), entity_ids=(table.id,)
        )
        revision.put_evidence(
            evidence_at("OrderProducer.java", 10, 12), entity_ids=(event.id,)
        )
        revision.put_evidence(
            evidence_at("OrderService.java", 10, 12), entity_ids=(rule.id,)
        )
    return capability


def test_a_flow_starts_at_the_entrypoint_and_follows_the_typed_relations(
    knowledge: KnowledgeRepository,
) -> None:
    capability = java_graph(knowledge)
    flows = build_flows(KnowledgeQuery(knowledge), capability.id)
    assert len(flows) == 1
    subjects = [step.subject for step in flows[0].steps]
    assert subjects[0] == "OrderController place"
    assert "OrderService place" in subjects
    assert "ORDERS" in subjects
    assert "order placed" in subjects


def test_the_step_order_follows_validation_call_persistence_publication(
    knowledge: KnowledgeRepository,
) -> None:
    capability = java_graph(knowledge)
    flows = build_flows(KnowledgeQuery(knowledge), capability.id)
    order = {step.subject: step.ordinal for step in flows[0].steps}
    assert order["OrderController place"] == 0
    assert order["reference is present"] < order["OrderService place"]
    assert order["OrderService place"] < order["ORDERS"]
    assert order["ORDERS"] < order["order placed"]
    assert [step.ordinal for step in flows[0].steps] == sorted(
        step.ordinal for step in flows[0].steps
    )


def test_each_step_inherits_the_evidence_of_the_finding_it_came_from(
    knowledge: KnowledgeRepository,
) -> None:
    capability = java_graph(knowledge)
    flows = build_flows(KnowledgeQuery(knowledge), capability.id)
    by_subject = {step.subject: step for step in flows[0].steps}
    assert by_subject["OrderService place"].evidence_ids
    assert by_subject["ORDERS"].evidence_ids
    assert by_subject["OrderController place"].resolved


def test_a_step_without_evidence_stays_unresolved(
    knowledge: KnowledgeRepository,
) -> None:
    capability = java_graph(knowledge)
    orphan = entity(EntityKind.OPERATION, "OrderAudit log", {"verb": "log"})
    with knowledge.begin_revision(author=AUTHOR, summary="orphan") as revision:
        revision.put_entity(orphan)
        revision.put_relation(
            Relation.create(
                RelationKind.CALLS.value,
                EntityId(
                    [
                        item.id.value
                        for item in knowledge.find_entities(EntityKind.OPERATION.value)
                        if item.name == "OrderService place"
                    ][0]
                ),
                orphan.id,
            )
        )
    flows = build_flows(KnowledgeQuery(knowledge), capability.id)
    step = [item for item in flows[0].steps if item.subject == "OrderAudit log"][0]
    assert not step.resolved
    assert not flows[0].resolved


def test_a_decision_comes_from_a_business_rule_with_conditions(
    knowledge: KnowledgeRepository,
) -> None:
    capability = java_graph(knowledge)
    flows = build_flows(KnowledgeQuery(knowledge), capability.id)
    decisions = flows[0].decisions
    assert len(decisions) == 1
    assert decisions[0].criteria == ("the reference is blank",)
    assert decisions[0].outcomes == ("reject the order",)
    assert decisions[0].resolved


def test_write_flows_persists_flow_steps_and_decisions_with_evidence(
    knowledge: KnowledgeRepository,
) -> None:
    capability = java_graph(knowledge)
    query = KnowledgeQuery(knowledge)
    flows = build_flows(query, capability.id)
    with knowledge.begin_revision(author=AUTHOR, summary="flows") as revision:
        written = write_flows(revision, query, capability.id, flows)
    assert len(written) == 1
    steps = knowledge.find_entities(EntityKind.FLOW_STEP.value)
    assert len(steps) == len(flows[0].steps)
    assert all("ordinal" in step.attributes for step in steps)
    assert knowledge.find_entities(EntityKind.DECISION.value)
    stored = query.flow(written[0])
    assert [view.entity.attributes["ordinal"] for view in stored] == sorted(
        view.entity.attributes["ordinal"] for view in stored
    )
    first = [item for item in steps if item.attributes["ordinal"] == 0][0]
    assert knowledge.evidence_for(first.id)


def test_the_written_flow_belongs_to_the_capability(
    knowledge: KnowledgeRepository,
) -> None:
    capability = java_graph(knowledge)
    query = KnowledgeQuery(knowledge)
    with knowledge.begin_revision(author=AUTHOR, summary="flows") as revision:
        write_flows(revision, query, capability.id, build_flows(query, capability.id))
    profile = query.capability_profile(capability.id)
    assert profile is not None
    flows = knowledge.find_entities(EntityKind.FLOW.value)
    assert flows
    relations = knowledge.relations_of(
        flows[0].id, "out", (RelationKind.BELONGS_TO.value,)
    )
    assert [relation.target_id for relation in relations] == [capability.id]


def test_building_and_writing_twice_is_idempotent(
    knowledge: KnowledgeRepository,
) -> None:
    capability = java_graph(knowledge)
    query = KnowledgeQuery(knowledge)
    first = build_flows(query, capability.id)
    for _ in range(2):
        with knowledge.begin_revision(author=AUTHOR, summary="flows") as revision:
            write_flows(revision, query, capability.id, build_flows(query, capability.id))
    assert build_flows(query, capability.id)[0].to_dict() == first[0].to_dict()
    counts = {
        kind: len(knowledge.find_entities(kind.value))
        for kind in (EntityKind.FLOW, EntityKind.FLOW_STEP, EntityKind.DECISION)
    }
    with knowledge.begin_revision(author=AUTHOR, summary="flows") as revision:
        write_flows(revision, query, capability.id, build_flows(query, capability.id))
    assert counts == {
        kind: len(knowledge.find_entities(kind.value))
        for kind in (EntityKind.FLOW, EntityKind.FLOW_STEP, EntityKind.DECISION)
    }
    subjects = {step.subject for step in build_flows(query, capability.id)[0].steps}
    assert not any(subject.startswith("place order via") for subject in subjects)


def test_a_capability_without_entrypoints_produces_no_flow(
    knowledge: KnowledgeRepository,
) -> None:
    capability = entity(EntityKind.CAPABILITY, "orphan capability")
    with knowledge.begin_revision(author=AUTHOR, summary="orphan") as revision:
        revision.put_source_version(VERSION)
        revision.put_entity(capability)
    assert build_flows(KnowledgeQuery(knowledge), capability.id) == ()


def test_an_unknown_capability_produces_no_flow(
    knowledge: KnowledgeRepository,
) -> None:
    assert build_flows(KnowledgeQuery(knowledge), EntityId("ent_missing")) == ()
