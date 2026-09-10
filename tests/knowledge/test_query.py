from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    Confidence,
    EntityId,
    KnowledgeState,
    KnowledgeQuery,
    Relation,
)
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind

from .graph_fixture import build


@pytest.fixture()
def graph(tmp_path):
    fixture = build(tmp_path)
    yield fixture
    fixture.repository.close()


@pytest.fixture()
def query(graph):
    return KnowledgeQuery(graph.repository)


def names(entities):
    return sorted(entity.name for entity in entities)


def test_entities_without_filters_returns_the_whole_graph(query, graph):
    assert len(query.entities(limit=1000)) == graph.repository.entity_count()


def test_entities_filters_by_kind(query):
    found = query.entities(kind=EntityKind.FLOW_STEP)
    assert names(found) == [
        "Checar elegibilidade",
        "Gravar renovacao",
        "Validar payload",
    ]


def test_entities_accepts_kind_as_string(query):
    assert query.entities(kind="flow_step") == query.entities(kind=EntityKind.FLOW_STEP)


def test_entities_filters_by_state(query):
    proposed = query.entities(state=KnowledgeState.PROPOSED)
    assert names(proposed) == ["ADR 001", "Migrar renovacao para Salesforce"]


def test_entities_filters_by_confidence(query):
    unresolved = query.entities(confidence=Confidence.UNRESOLVED)
    assert names(unresolved) == ["Qual a janela de renovacao?", "Quem e o dono?"]


def test_entities_filters_by_name_contains(query):
    found = names(query.entities(name_contains="janela"))
    assert found == ["Qual a janela de renovacao?"]


def test_entities_name_contains_is_case_insensitive(query):
    assert names(query.entities(name_contains="BILLING")) == names(
        query.entities(name_contains="billing")
    )
    assert "Billing API" in names(query.entities(name_contains="BILLING"))


def test_entities_combines_filters(query):
    found = query.entities(
        kind=EntityKind.CAPABILITY, state=KnowledgeState.DECLARED
    )
    assert names(found) == ["Renovacao automatica"]


def test_entities_paginates(query):
    page_one = query.entities(kind=EntityKind.FLOW_STEP, limit=2, offset=0)
    page_two = query.entities(kind=EntityKind.FLOW_STEP, limit=2, offset=2)
    assert len(page_one) == 2
    assert len(page_two) == 1
    assert {e.id.value for e in page_one}.isdisjoint({e.id.value for e in page_two})


def test_search_matches_name(query):
    assert "Billing API" in names(query.search("Billing"))


def test_search_matches_attributes(query):
    assert names(query.search("adimplente")) == [
        "Elegibilidade de renovacao",
        "Renovacao feliz",
    ]


def test_search_escapes_like_wildcards(query):
    assert query.search("%") == ()


def test_search_paginates(query):
    everything = query.search("renovacao", limit=1000)
    assert query.search("renovacao", limit=1, offset=1) == (everything[1],)


def test_neighbors_both_directions(query, graph):
    found = query.neighbors(graph.id("capability"), limit=1000)
    assert len(found) == len(
        graph.repository.relations_of(graph.id("capability"), "both")
    )


def test_neighbors_filters_by_relation_kind(query, graph):
    found = query.neighbors(
        graph.id("capability"), relation_kind=RelationKind.CALLS, direction="out"
    )
    assert [entity.name for _, entity in found] == ["Billing API"]


def test_neighbors_accepts_several_relation_kinds(query, graph):
    found = query.neighbors(
        graph.id("capability"),
        relation_kind=["calls", "publishes"],
        direction="out",
        limit=1000,
    )
    assert sorted(relation.kind for relation, _ in found) == [
        "calls",
        "publishes",
        "publishes",
    ]


def test_neighbors_direction_in(query, graph):
    found = query.neighbors(
        graph.id("capability"), relation_kind="tests", direction="in"
    )
    assert [entity.name for _, entity in found] == ["Renovacao feliz"]


def test_neighbors_paginates(query, graph):
    everything = query.neighbors(graph.id("capability"), limit=1000)
    assert query.neighbors(graph.id("capability"), limit=2, offset=1) == everything[1:3]


def test_paths_finds_entrypoint_to_flow(query, graph):
    found = query.paths(graph.id("entry_point"), graph.id("flow"), max_depth=5)
    assert found
    shortest = found[0]
    assert shortest.nodes[0] == graph.id("entry_point").value
    assert shortest.nodes[-1] == graph.id("flow").value
    assert shortest.depth == 1


def test_paths_respects_max_depth(query, graph):
    assert query.paths(graph.id("step_validate"), graph.id("step_persist"), max_depth=1) == ()


def test_paths_returns_nothing_between_unconnected_nodes(query, graph):
    assert query.paths(graph.id("table"), graph.id("entry_point"), max_depth=6) == ()


def test_paths_walks_capability_to_table(query, graph):
    found = query.paths(graph.id("capability"), graph.id("table"), max_depth=3)
    assert found
    assert found[0].nodes[-1] == graph.id("table").value


def test_paths_filters_by_relation_kind(query, graph):
    found = query.paths(
        graph.id("step_validate"),
        graph.id("step_persist"),
        max_depth=4,
        relation_kind="triggers",
    )
    assert len(found) == 1
    assert found[0].depth == 2


def test_paths_does_not_revisit_nodes(query, graph):
    for path in query.paths(graph.id("module"), graph.id("table"), max_depth=5):
        assert len(set(path.nodes)) == len(path.nodes)


def test_flow_orders_steps_by_ordinal(query, graph):
    steps = query.flow(graph.id("flow"))
    assert [step.entity.name for step in steps][:3] == [
        "Validar payload",
        "Checar elegibilidade",
        "Gravar renovacao",
    ]
    assert [step.ordinal for step in steps][:3] == [0, 1, 2]


def test_flow_records_the_relation_that_reached_each_node(query, graph):
    steps = query.flow(graph.id("flow"))
    assert steps[0].via == RelationKind.BELONGS_TO.value


def test_flow_follows_transitions_to(query, graph):
    steps = query.flow(graph.id("state_active"))
    assert [step.entity.name for step in steps] == ["Renovada"]
    assert steps[0].via == RelationKind.TRANSITIONS_TO.value


def test_flow_of_unknown_entity_is_empty(query):
    assert query.flow(EntityId("ent_fantasma")) == ()


def test_flow_paginates(query, graph):
    assert len(query.flow(graph.id("flow"), limit=2)) == 2


def test_capability_profile_aggregates_every_section(query, graph):
    profile = query.capability_profile(graph.id("capability"))
    assert profile.capability.name == "Renovacao"
    assert names(profile.entrypoints) == ["POST /renewals"]
    assert names(profile.inputs) == ["RenewalRequest"]
    assert names(profile.outputs) == ["RenewalResponse"]
    assert names(profile.rules) == ["Elegibilidade de renovacao"]
    assert names(profile.states) == ["Ativa", "Renovada"]
    assert names(profile.persistence) == ["renewals"]
    assert names(profile.integrations) == ["Billing API"]
    assert names(profile.events) == ["RenewalCompleted"]
    assert names(profile.failures) == ["Timeout no billing"]
    assert names(profile.retries) == ["Retry billing"]
    assert names(profile.fallbacks) == ["Fila manual"]
    assert names(profile.edge_cases) == ["Cliente sem historico"]
    assert names(profile.tests) == ["Renovacao feliz"]
    assert names(profile.dependencies) == ["billing-sdk"]
    assert names(profile.gaps) == ["Qual a janela de renovacao?", "Quem e o dono?"]
    assert len(profile.evidence) == 1


def test_capability_profile_sections_cover_plan_10_2(query, graph):
    profile = query.capability_profile(graph.id("capability"))
    assert set(profile.sections()) == {
        "entrypoints",
        "inputs",
        "outputs",
        "preconditions",
        "rules",
        "invariants",
        "decisions",
        "states",
        "persistence",
        "integrations",
        "events",
        "failures",
        "retries",
        "fallbacks",
        "timeouts",
        "idempotency",
        "edge_cases",
        "tests",
        "dependencies",
        "gaps",
    }


def test_capability_profile_of_unknown_entity_is_none(query):
    assert query.capability_profile(EntityId("ent_fantasma")) is None


def test_gaps_lists_every_gap(query):
    assert names(query.gaps()) == [
        "Qual a janela de renovacao?",
        "Quem e o dono?",
    ]


def test_gaps_blocking_only(query):
    assert names(query.gaps(blocking_only=True)) == ["Qual a janela de renovacao?"]


def test_gaps_paginates(query):
    assert len(query.gaps(limit=1)) == 1


def test_impact_walks_reverse_depends_on_and_calls(query, graph):
    report = query.impact(graph.id("dependency"), limit=1000)
    assert "Renovacao" in names(report.impacted)
    assert "Billing" in names(report.impacted)
    assert report.depth_by_entity[graph.id("capability").value] == 1
    assert report.depth_by_entity[graph.id("module").value] == 2


def test_impact_includes_affects_reverse(query, graph):
    report = query.impact(graph.id("capability"), limit=1000)
    assert "Qual a janela de renovacao?" in names(report.impacted)


def test_impact_of_leaf_is_empty(query, graph):
    assert query.impact(graph.id("table")).total == 0


def test_impact_respects_max_depth(query, graph):
    shallow = query.impact(graph.id("dependency"), max_depth=1, limit=1000)
    assert "Billing" not in names(shallow.impacted)


def test_impact_paginates(query, graph):
    assert len(query.impact(graph.id("dependency"), limit=1).impacted) == 1


def test_evidence_of_returns_linked_evidence(query, graph):
    found = query.evidence_of(graph.id("capability"))
    assert len(found) == 1
    assert found[0].source_id == graph.code_version.source_id


def test_evidence_of_unlinked_entity_is_empty(query, graph):
    assert query.evidence_of(graph.id("table")) == ()


def test_supported_relation_needs_evidence_or_supported_endpoints(graph):
    repo = graph.repository
    with pytest.raises(Exception) as failure:
        with repo.begin_revision("pipeline", "relacao sem lastro") as revision:
            revision.put_relation(
                Relation.create(
                    "calls",
                    graph.id("capability"),
                    graph.id("integration"),
                    confidence=Confidence.SUPPORTED,
                )
            )
    assert "supported" in str(failure.value)
