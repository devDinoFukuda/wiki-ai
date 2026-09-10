from __future__ import annotations

import pytest

from wiki_ai.knowledge.comparison import (
    DECISION_SUPERSEDES,
    DECLARED_NOT_IMPLEMENTED,
    IMPLEMENTED_NOT_DOCUMENTED,
    NO_CORRELATION,
    PROPOSAL_CONFLICT,
    SOURCE_CONTRADICTS,
)
from wiki_ai.knowledge.correlation import correlate
from wiki_ai.knowledge.query import KnowledgeQuery

from .inception_fixture import build


@pytest.fixture()
def inception(tmp_path):
    fixture = build(tmp_path)
    correlate(fixture.repository, "ns")
    yield fixture
    fixture.repository.close()


@pytest.fixture()
def report(inception):
    return KnowledgeQuery(inception.repository).compare()


def _names(findings):
    return {finding.entity_name for finding in findings}


def _pairs(findings):
    return {(finding.entity_name, finding.counterpart_name) for finding in findings}


def _sources(finding):
    return (
        {ref.source_id for ref in finding.left_evidence},
        {ref.source_id for ref in finding.right_evidence},
    )


def test_the_five_buckets_are_all_populated(report):
    assert report.declared_not_implemented
    assert report.implemented_not_documented
    assert report.proposal_conflicts
    assert report.decision_supersedes
    assert report.source_contradicts_source
    assert report.total == len(report.all_findings())


def test_declared_but_not_implemented_names_the_requirement_and_its_target(report):
    assert ("Cancelamento", "Cancelamento") in _pairs(
        report.declared_not_implemented
    )
    assert all(
        finding.category == DECLARED_NOT_IMPLEMENTED
        for finding in report.declared_not_implemented
    )


def test_a_declarer_without_correlation_is_also_declared_but_not_implemented(report):
    uncorrelated = {
        finding.entity_name
        for finding in report.declared_not_implemented
        if finding.detail == NO_CORRELATION
    }
    assert {"Gateway", "Salesforce", "Billing"} <= uncorrelated


def test_a_declared_entity_that_contradicts_the_code_is_not_uncorrelated(report):
    uncorrelated = {
        finding.entity_name
        for finding in report.declared_not_implemented
        if finding.detail == NO_CORRELATION
    }
    assert "Renovação" not in uncorrelated


def test_implemented_but_not_documented_lists_code_only_entities(report):
    assert {"Billing", "Eligibility", "RenewalEvent"} <= _names(
        report.implemented_not_documented
    )
    assert "Cancelamento" not in _names(report.implemented_not_documented)
    assert all(
        finding.category == IMPLEMENTED_NOT_DOCUMENTED
        for finding in report.implemented_not_documented
    )


def test_a_capability_declared_by_a_decision_is_documented(report):
    documented = [
        finding
        for finding in report.implemented_not_documented
        if finding.entity_kind == "capability"
    ]
    assert documented == []


def test_proposal_conflicts_carry_both_sides_with_their_sources(report):
    conflicts = [
        finding
        for finding in report.proposal_conflicts
        if finding.entity_name.startswith("Migrar")
    ]
    assert conflicts
    finding = conflicts[0]
    assert finding.category == PROPOSAL_CONFLICT
    assert finding.counterpart_name == "Renovacao"
    assert _sources(finding) == ({"src_transcript"}, {"src_code"})
    assert finding.has_both_sides


def test_a_contradicting_document_also_conflicts_with_current_behavior(report):
    conflicts = [
        finding
        for finding in report.proposal_conflicts
        if finding.left_source_id == "src_xlsx"
    ]
    assert conflicts
    assert conflicts[0].right_source_id == "src_code"


def test_decision_supersedes_prior_proposal_with_both_locators(report):
    assert len(report.decision_supersedes) == 1
    finding = report.decision_supersedes[0]
    assert finding.category == DECISION_SUPERSEDES
    assert finding.entity_kind == "decision_record"
    assert finding.counterpart_kind == "proposal"
    assert _sources(finding) == ({"src_transcript"}, {"src_transcript"})


def test_source_contradicts_source_reports_the_numeric_divergence(report):
    assert len(report.source_contradicts_source) == 1
    finding = report.source_contradicts_source[0]
    assert finding.category == SOURCE_CONTRADICTS
    assert "45" in finding.detail
    assert "30" in finding.detail
    assert _sources(finding) == ({"src_xlsx"}, {"src_code"})


def test_every_correlated_finding_keeps_the_correlation_basis(report):
    correlated = [
        finding
        for finding in report.all_findings()
        if finding.counterpart_id is not None and finding.basis
    ]
    assert correlated
    assert {finding.basis for finding in correlated} <= {
        "stable_key",
        "alias",
        "impact",
    }


def test_findings_expose_the_locator_of_each_side(report):
    finding = report.source_contradicts_source[0]
    kinds = {ref.locator["kind"] for ref in finding.left_evidence} | {
        ref.locator["kind"] for ref in finding.right_evidence
    }
    assert kinds == {"spreadsheet", "code"}


def test_comparison_is_stable_across_reruns(inception, report):
    correlate(inception.repository, "ns")
    again = KnowledgeQuery(inception.repository).compare()
    assert again.all_findings() == report.all_findings()


def test_query_compare_lists_corroborated_findings_before_one_sided_ones(inception):
    conflicts = KnowledgeQuery(inception.repository).compare().proposal_conflicts
    flags = [item.has_both_sides for item in conflicts]
    assert flags == sorted(flags, reverse=True)


def test_corroborated_keeps_only_findings_with_evidence_on_both_sides(report):
    corroborated = report.corroborated()
    assert corroborated
    assert all(item.has_both_sides for item in corroborated)
    assert len(corroborated) < report.total
