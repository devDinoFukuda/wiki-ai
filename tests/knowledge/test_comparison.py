from __future__ import annotations

import pytest

from wiki_ai.knowledge import Entity, KnowledgeState, KnowledgeQuery, Relation
from wiki_ai.knowledge.comparison import (
    DECISION_SUPERSEDES,
    DECLARED_NOT_IMPLEMENTED,
    IMPLEMENTED_NOT_DOCUMENTED,
    PROPOSAL_CONFLICT,
    SOURCE_CONTRADICTS,
    compare,
)

from .graph_fixture import build


@pytest.fixture()
def graph(tmp_path):
    fixture = build(tmp_path)
    yield fixture
    fixture.repository.close()


@pytest.fixture()
def report(graph):
    return compare(graph.repository)


def ids(findings):
    return sorted(finding.entity_id for finding in findings)


def _correlated(findings):
    return [item for item in findings if item.counterpart_id is not None]


def test_declared_but_not_implemented(report, graph):
    assert ids(_correlated(report.declared_not_implemented)) == [
        graph.id("capability_declared").value
    ]
    finding = _correlated(report.declared_not_implemented)[0]
    assert finding.category == DECLARED_NOT_IMPLEMENTED
    assert finding.counterpart_id == graph.id("requirement").value


def test_declarer_without_correlation_is_reported(report, graph):
    assert graph.id("decision").value in ids(report.declared_not_implemented)


def test_declared_and_implemented_is_not_reported(graph):
    repo = graph.repository
    with repo.begin_revision("pipeline", "implementa") as revision:
        revision.put_relation(
            Relation.create(
                "implements", graph.id("capability"), graph.id("capability_declared")
            )
        )
    assert _correlated(compare(repo).declared_not_implemented) == []


def test_declared_entity_already_implemented_is_not_reported(graph):
    repo = graph.repository
    stored = repo.get_entity(graph.id("capability_declared"))
    with repo.begin_revision("pipeline", "promove") as revision:
        revision.put_entity(
            Entity(
                id=stored.id,
                kind=stored.kind,
                name=stored.name,
                attributes=stored.attributes,
                state=KnowledgeState.IMPLEMENTED,
                confidence=stored.confidence,
            )
        )
    assert _correlated(compare(repo).declared_not_implemented) == []


def test_implemented_but_not_documented(report, graph):
    found = ids(report.implemented_not_documented)
    assert graph.id("capability").value in found
    assert graph.id("rule").value in found
    assert graph.id("capability_declared").value not in found
    assert report.implemented_not_documented[0].category == IMPLEMENTED_NOT_DOCUMENTED


def test_declaring_an_implemented_entity_removes_it_from_the_undocumented_list(graph):
    repo = graph.repository
    with repo.begin_revision("pipeline", "documenta") as revision:
        revision.put_relation(
            Relation.create("declares", graph.id("source_a"), graph.id("capability"))
        )
    assert graph.id("capability").value not in ids(
        compare(repo).implemented_not_documented
    )


def test_proposal_conflicts_with_current_behavior(report, graph):
    assert ids(report.proposal_conflicts) == [graph.id("proposal").value]
    finding = report.proposal_conflicts[0]
    assert finding.category == PROPOSAL_CONFLICT
    assert finding.counterpart_id == graph.id("rule").value


def test_proposal_against_non_implemented_target_is_not_a_conflict(graph):
    repo = graph.repository
    with repo.begin_revision("pipeline", "proposta futura") as revision:
        revision.put_relation(
            Relation.create(
                "proposes_change_to",
                graph.id("proposal"),
                graph.id("capability_declared"),
            )
        )
    conflicts = compare(repo).proposal_conflicts
    assert [finding.counterpart_id for finding in conflicts] == [graph.id("rule").value]


def test_decision_supersedes_prior_proposal(report, graph):
    assert ids(report.decision_supersedes) == [graph.id("decision").value]
    finding = report.decision_supersedes[0]
    assert finding.category == DECISION_SUPERSEDES
    assert finding.counterpart_id == graph.id("proposal_old").value


def test_source_contradicts_source(report, graph):
    assert ids(report.source_contradicts_source) == [graph.id("source_a").value]
    finding = report.source_contradicts_source[0]
    assert finding.category == SOURCE_CONTRADICTS
    assert finding.counterpart_id == graph.id("source_b").value


def test_report_covers_every_plan_12_2_question(report):
    categories = {finding.category for finding in report.all_findings()}
    assert categories == {
        DECLARED_NOT_IMPLEMENTED,
        IMPLEMENTED_NOT_DOCUMENTED,
        PROPOSAL_CONFLICT,
        DECISION_SUPERSEDES,
        SOURCE_CONTRADICTS,
    }


def test_report_total_matches_the_sum_of_the_buckets(report):
    assert report.total == (
        len(report.declared_not_implemented)
        + len(report.implemented_not_documented)
        + len(report.proposal_conflicts)
        + len(report.decision_supersedes)
        + len(report.source_contradicts_source)
    )


def test_comparison_is_deterministic(graph):
    assert compare(graph.repository) == compare(graph.repository)


def test_query_compare_paginates(graph):
    query = KnowledgeQuery(graph.repository)
    full = query.compare(limit=1000)
    page = query.compare(limit=1)
    assert len(page.implemented_not_documented) == 1
    assert page.implemented_not_documented[0] == full.implemented_not_documented[0]


def test_query_compare_offset_skips(graph):
    query = KnowledgeQuery(graph.repository)
    full = query.compare(limit=1000)
    page = query.compare(limit=1, offset=1)
    assert page.implemented_not_documented[0] == full.implemented_not_documented[1]
