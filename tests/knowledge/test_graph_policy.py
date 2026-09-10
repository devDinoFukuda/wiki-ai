from __future__ import annotations

import pytest

from wiki_ai.knowledge import Confidence, GraphPolicy, KnowledgeQuery
from wiki_ai.knowledge.taxonomy import RelationKind

from .graph_fixture import build


@pytest.fixture()
def graph(tmp_path):
    built = build(tmp_path)
    yield built
    built.repository.close()


def demote(graph, kind: str, source: str, target: str, confidence: Confidence) -> str:
    relation_id = ""
    for relation in graph.repository.relations_of(
        graph.id(source), "out", (kind,), policy=GraphPolicy.ALL
    ):
        if relation.target_id == graph.id(target):
            relation_id = relation.id
            graph.repository.conn.execute(
                "UPDATE relations SET confidence=? WHERE relation_id=?",
                (confidence.value, relation.id),
            )
    assert relation_id
    return relation_id


def test_default_policy_of_a_query_is_supported_only(graph) -> None:
    assert KnowledgeQuery(graph.repository).policy is GraphPolicy.SUPPORTED_ONLY


def test_unresolved_relation_disappears_from_neighbors(graph) -> None:
    demote(graph, "calls", "capability", "integration", Confidence.UNRESOLVED)
    query = KnowledgeQuery(graph.repository)

    reached = {
        entity.id.value
        for _, entity in query.neighbors(graph.id("capability"), RelationKind.CALLS)
    }

    assert graph.id("integration").value not in reached


def test_all_policy_keeps_the_unresolved_relation_with_its_confidence(graph) -> None:
    demote(graph, "calls", "capability", "integration", Confidence.UNRESOLVED)
    query = KnowledgeQuery(graph.repository, GraphPolicy.ALL)

    pairs = query.neighbors(graph.id("capability"), RelationKind.CALLS)

    assert [relation.confidence for relation, _ in pairs] == [Confidence.UNRESOLVED]
    assert [relation.to_dict()["confidence"] for relation, _ in pairs] == ["unresolved"]


def test_supported_and_inferred_policy_keeps_an_inferred_relation(graph) -> None:
    demote(graph, "calls", "capability", "integration", Confidence.INFERRED)

    hidden = KnowledgeQuery(graph.repository).neighbors(
        graph.id("capability"), RelationKind.CALLS
    )
    shown = KnowledgeQuery(graph.repository, GraphPolicy.SUPPORTED_AND_INFERRED).neighbors(
        graph.id("capability"), RelationKind.CALLS
    )

    assert hidden == ()
    assert [relation.confidence for relation, _ in shown] == [Confidence.INFERRED]


def test_unresolved_relation_breaks_the_path(graph) -> None:
    before = KnowledgeQuery(graph.repository).paths(
        graph.id("capability"), graph.id("topic")
    )
    demote(graph, "publishes", "integration", "topic", Confidence.UNRESOLVED)
    after = KnowledgeQuery(graph.repository).paths(
        graph.id("capability"), graph.id("topic")
    )
    unfiltered = KnowledgeQuery(graph.repository, GraphPolicy.ALL).paths(
        graph.id("capability"), graph.id("topic")
    )

    assert before
    assert after == ()
    assert unfiltered


def test_unresolved_transition_disappears_from_flow(graph) -> None:
    demote(graph, "triggers", "step_rule", "step_persist", Confidence.UNRESOLVED)
    query = KnowledgeQuery(graph.repository)

    steps = query.flow(graph.id("flow"))
    names = [view.entity.name for view in steps]

    assert "Gravar renovacao" in names
    reached = [
        view.entity.name
        for view in steps
        if view.via == RelationKind.TRIGGERS.value
    ]
    assert "Gravar renovacao" not in reached


def test_unresolved_relation_disappears_from_impact(graph) -> None:
    demote(graph, "depends_on", "module", "capability", Confidence.UNRESOLVED)

    filtered = KnowledgeQuery(graph.repository).impact(graph.id("capability"))
    unfiltered = KnowledgeQuery(graph.repository, GraphPolicy.ALL).impact(
        graph.id("capability")
    )

    assert graph.id("module").value not in filtered.depth_by_entity
    assert graph.id("module").value in unfiltered.depth_by_entity


def test_unresolved_membership_disappears_from_capability_profile(graph) -> None:
    demote(graph, "belongs_to", "rule", "capability", Confidence.UNRESOLVED)

    filtered = KnowledgeQuery(graph.repository).capability_profile(
        graph.id("capability")
    )
    unfiltered = KnowledgeQuery(graph.repository, GraphPolicy.ALL).capability_profile(
        graph.id("capability")
    )

    assert filtered is not None
    assert unfiltered is not None
    assert graph.id("rule").value not in {rule.id.value for rule in filtered.rules}
    assert graph.id("rule").value in {rule.id.value for rule in unfiltered.rules}


def test_unresolved_integration_disappears_from_capability_profile(graph) -> None:
    demote(graph, "calls", "capability", "integration", Confidence.UNRESOLVED)
    demote(graph, "belongs_to", "integration", "capability", Confidence.UNRESOLVED)

    filtered = KnowledgeQuery(graph.repository).capability_profile(
        graph.id("capability")
    )

    assert filtered is not None
    assert graph.id("integration").value not in {
        node.id.value for node in filtered.integrations
    }


def test_contradicted_relation_is_hidden_from_every_policy_but_all(graph) -> None:
    demote(graph, "calls", "capability", "integration", Confidence.CONTRADICTED)

    for policy in (GraphPolicy.SUPPORTED_ONLY, GraphPolicy.SUPPORTED_AND_INFERRED):
        query = KnowledgeQuery(graph.repository, policy)
        assert query.neighbors(graph.id("capability"), RelationKind.CALLS) == ()

    unfiltered = KnowledgeQuery(graph.repository, GraphPolicy.ALL)
    assert unfiltered.neighbors(graph.id("capability"), RelationKind.CALLS)


def test_neighbor_ids_follow_the_query_policy(graph) -> None:
    demote(graph, "calls", "capability", "integration", Confidence.UNRESOLVED)

    filtered = KnowledgeQuery(graph.repository).neighbor_ids(
        graph.id("capability"), RelationKind.CALLS, "out"
    )
    unfiltered = KnowledgeQuery(graph.repository, GraphPolicy.ALL).neighbor_ids(
        graph.id("capability"), RelationKind.CALLS, "out"
    )

    assert filtered == ()
    assert unfiltered == (graph.id("integration").value,)
