from __future__ import annotations

import pytest

from tests.publishing.relation_confidence import set_relation_confidence
from wiki_ai.knowledge import Confidence, GraphPolicy
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.publishing.answer_session import (
    INFERRED_CONFIDENCE,
    INFERRED_LABEL,
    ClaimVerdict,
    RejectionReason,
    build_briefing,
    compose_answer,
    validate_envelope,
)
from wiki_ai.publishing.diagrams import flowchart, sequence
from wiki_ai.publishing.model import DocumentKind, INFERRED_SECTION_TITLE
from wiki_ai.publishing.narrative import INFERRED_RELATION_QUALIFIER, NarrativeBuilder
from wiki_ai.publishing.planner import plan
from wiki_ai.publishing.query_harness import (
    INCLUDE_INFERRED,
    INFERRED_TOOLS,
    InvalidQueryArguments,
    KnowledgeQueryHarness,
    TOOL_IMPACT,
    TOOL_NEIGHBORS,
)


@pytest.fixture
def harness(graph) -> KnowledgeQueryHarness:
    return KnowledgeQueryHarness(graph.repository, "ns")


def _calls_billing(graph, confidence: Confidence):
    return set_relation_confidence(
        graph.repository,
        "calls",
        graph.id("capability").value,
        graph.id("integration").value,
        confidence,
    )


def _neighbor_names(payload) -> set[str]:
    return {item["entity"]["name"] for item in payload["neighbors"]}


def test_harness_defaults_to_supported_only(harness):
    assert harness.policy is GraphPolicy.SUPPORTED_ONLY
    assert harness.query.policy is GraphPolicy.SUPPORTED_ONLY


def test_harness_refuses_the_whole_graph_as_fact(graph):
    with pytest.raises(InvalidQueryArguments):
        KnowledgeQueryHarness(graph.repository, "ns", policy=GraphPolicy.ALL)


def test_every_relation_tool_offers_the_inferred_switch(harness):
    for name in INFERRED_TOOLS:
        spec = harness.spec_for(name)
        assert INCLUDE_INFERRED in spec.input_schema["properties"]


def test_neighbors_carries_the_relation_confidence(harness, graph):
    payload = harness.invoke(
        TOOL_NEIGHBORS,
        {"entity_id": graph.id("capability").value, "relation_kind": "calls"},
    )
    assert payload["neighbors"]
    assert {item["relation_confidence"] for item in payload["neighbors"]} == {
        Confidence.SUPPORTED.value
    }


def test_demoted_relation_leaves_the_neighbors_tool(graph):
    capability = graph.id("capability").value
    before = KnowledgeQueryHarness(graph.repository, "ns").invoke(
        TOOL_NEIGHBORS, {"entity_id": capability, "relation_kind": "calls"}
    )
    assert "Billing API" in _neighbor_names(before)
    _calls_billing(graph, Confidence.UNRESOLVED)
    after = KnowledgeQueryHarness(graph.repository, "ns").invoke(
        TOOL_NEIGHBORS, {"entity_id": capability, "relation_kind": "calls"}
    )
    assert "Billing API" not in _neighbor_names(after)


def test_demoted_relation_leaves_the_impact_report(graph):
    integration = graph.id("integration").value
    before = KnowledgeQueryHarness(graph.repository, "ns").invoke(
        TOOL_IMPACT, {"entity_id": integration}
    )
    assert "Renovacao" in {item["entity"]["name"] for item in before["impacted"]}
    _calls_billing(graph, Confidence.UNRESOLVED)
    after = KnowledgeQueryHarness(graph.repository, "ns").invoke(
        TOOL_IMPACT, {"entity_id": integration}
    )
    assert "Renovacao" not in {item["entity"]["name"] for item in after["impacted"]}


def test_demoted_relation_leaves_the_diagram(graph):
    before = sequence(KnowledgeQuery(graph.repository), graph.nodes["capability"])
    assert before is not None
    assert "Renovacao chama Billing API" in before.textual_equivalent
    _calls_billing(graph, Confidence.UNRESOLVED)
    after = sequence(KnowledgeQuery(graph.repository), graph.nodes["capability"])
    reached = after.textual_equivalent if after is not None else ()
    assert "Renovacao chama Billing API" not in reached


def test_inferred_relation_only_shows_with_the_switch(graph):
    _calls_billing(graph, Confidence.INFERRED)
    harness = KnowledgeQueryHarness(graph.repository, "ns")
    capability = graph.id("capability").value
    hidden = harness.invoke(
        TOOL_NEIGHBORS, {"entity_id": capability, "relation_kind": "calls"}
    )
    assert "Billing API" not in _neighbor_names(hidden)
    shown = harness.invoke(
        TOOL_NEIGHBORS,
        {
            "entity_id": capability,
            "relation_kind": "calls",
            INCLUDE_INFERRED: True,
        },
    )
    assert "Billing API" in _neighbor_names(shown)
    marked = [
        item["relation_confidence"]
        for item in shown["neighbors"]
        if item["entity"]["name"] == "Billing API"
    ]
    assert marked == [Confidence.INFERRED.value]


def test_briefing_teaches_the_inferred_mark(harness):
    text = build_briefing("pergunta", harness.names(), "ns")
    assert INCLUDE_INFERRED in text
    assert INFERRED_CONFIDENCE in text


def _claim(graph, statement: str, confidence: str = "") -> dict:
    repository = graph.repository
    relation = [
        item
        for item in repository.find_relations("calls")
        if item.source_id == graph.id("capability")
    ][0]
    linked = repository.evidence_for_relation(relation.id, active_only=False)
    owned = repository.evidence_for(graph.id("capability"))
    claim = {
        "statement": statement,
        "entity_ids": [
            graph.id("capability").value,
            graph.id("integration").value,
        ],
        "evidence_ids": [item.id for item in list(owned) + list(linked)],
    }
    if confidence:
        claim["confidence"] = confidence
    return claim


def test_claim_on_an_inferred_relation_without_the_mark_is_refused(graph):
    _calls_billing(graph, Confidence.INFERRED)
    harness = KnowledgeQueryHarness(graph.repository, "ns")
    envelope = {
        "answer": "rascunho",
        "claims": [_claim(graph, "Renovacao chama Billing API")],
    }
    validated = validate_envelope(envelope, harness)
    assert [claim.verdict for claim in validated.claims] == [ClaimVerdict.REJECTED]
    assert validated.claims[0].reason is RejectionReason.UNSUPPORTED_RELATION_CITED


def test_claim_on_an_inferred_relation_with_the_mark_is_accepted(graph):
    _calls_billing(graph, Confidence.INFERRED)
    harness = KnowledgeQueryHarness(graph.repository, "ns")
    envelope = {
        "answer": "rascunho",
        "claims": [
            _claim(
                graph,
                "Renovacao chama Billing API",
                confidence=INFERRED_CONFIDENCE,
            )
        ],
    }
    validated = validate_envelope(envelope, harness)
    assert [claim.verdict for claim in validated.claims] == [ClaimVerdict.VALID]
    assert INFERRED_LABEL in compose_answer(validated, harness)


def test_claim_on_a_supported_relation_needs_no_mark(graph):
    harness = KnowledgeQueryHarness(graph.repository, "ns")
    envelope = {
        "answer": "rascunho",
        "claims": [_claim(graph, "Renovacao chama Billing API")],
    }
    validated = validate_envelope(envelope, harness)
    assert [claim.verdict for claim in validated.claims] == [ClaimVerdict.VALID]
    assert INFERRED_LABEL not in compose_answer(validated, harness)


def test_gaps_report_labels_the_inferred_relations(graph):
    _calls_billing(graph, Confidence.INFERRED)
    query = KnowledgeQuery(graph.repository)
    document = [
        entry
        for entry in plan(query, "ns").documents
        if entry.kind is DocumentKind.GAPS_REPORT
    ][0]
    built = NarrativeBuilder(query).build(document)
    assert INFERRED_SECTION_TITLE in built.section_titles()
    qualifiers = [
        assertion.qualifier
        for block in built.body
        for assertion in block.assertions
    ]
    assert INFERRED_RELATION_QUALIFIER in qualifiers


def test_inferred_relation_stays_out_of_the_diagram(graph):
    _calls_billing(graph, Confidence.INFERRED)
    spec = sequence(KnowledgeQuery(graph.repository), graph.nodes["capability"])
    reached = spec.textual_equivalent if spec is not None else ()
    assert "Renovacao chama Billing API" not in reached


def test_flowchart_drops_a_demoted_step(graph):
    before = flowchart(KnowledgeQuery(graph.repository), graph.nodes["capability"])
    assert before is not None
    assert any(
        "Validar payload" in sentence for sentence in before.textual_equivalent
    )
    set_relation_confidence(
        graph.repository,
        "belongs_to",
        graph.id("step_validate").value,
        graph.id("flow").value,
        Confidence.UNRESOLVED,
    )
    after = flowchart(KnowledgeQuery(graph.repository), graph.nodes["capability"])
    reached = after.textual_equivalent if after is not None else ()
    assert not any("Validar payload" in sentence for sentence in reached)
