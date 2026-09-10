from __future__ import annotations

import pytest

from wiki_ai.knowledge.contradiction import contradiction_between, measures
from wiki_ai.knowledge.correlation import (
    AMBIGUOUS_PREFIX,
    BASIS_IMPACT,
    CORRELATED_BY,
    LEFT_SOURCE,
    RIGHT_SOURCE,
    SCORE,
    correlate,
)
from wiki_ai.knowledge.gaps import open_gaps
from wiki_ai.knowledge.matching import score_names, singularize, tokens
from wiki_ai.knowledge.model import Confidence, Entity, EpistemicStatus
from wiki_ai.knowledge.taxonomy import RelationKind

from .inception_fixture import build


@pytest.fixture()
def inception(tmp_path):
    fixture = build(tmp_path)
    yield fixture
    fixture.repository.close()


@pytest.fixture()
def report(inception):
    return correlate(inception.repository, "ns")


def _targets(inception, kind: RelationKind, source_key: str) -> set[str]:
    origin = inception.id(source_key).value
    return {
        inception.repository.get_entity(relation.target_id).name
        for relation in inception.repository.find_relations(kind.value)
        if relation.source_id.value == origin
    }


def test_normalization_folds_accents_punctuation_and_plural():
    assert tokens("Renovações!") == tokens("renovacao")
    assert singularize("renewals") == "renewal"
    assert score_names("Renovação", "Renovacao") == 1.0


def test_numeric_measures_are_extracted_in_both_languages():
    assert measures("ate 45 dias") == {"day": 45.0}
    assert measures("within 30 days") == {"day": 30.0}


def test_requirement_declares_the_capability_it_names(inception, report):
    assert "Cancelamento" in _targets(
        inception, RelationKind.DECLARES, "requirement_cancel"
    )


def test_decision_declares_the_implemented_capability(inception, report):
    assert "Renovacao" in _targets(inception, RelationKind.DECLARES, "decision")


def test_proposal_proposes_change_to_the_implemented_capability(inception, report):
    assert _targets(inception, RelationKind.PROPOSES_CHANGE_TO, "proposal") == {
        "Renovacao"
    }


def test_proposal_affects_everything_reachable_by_impact(inception, report):
    assert _targets(inception, RelationKind.AFFECTS, "proposal") >= {
        "Renovacao",
        "Billing",
        "Eligibility",
        "RenewalEvent",
    }


def test_affects_relations_declare_impact_as_the_basis(report):
    affects = report.of_kind(RelationKind.AFFECTS)
    assert affects
    assert {item.basis for item in affects} == {BASIS_IMPACT}


def test_spreadsheet_rule_contradicts_the_code_rule_on_the_threshold(
    inception, report
):
    found = report.of_kind(RelationKind.CONTRADICTS)
    assert len(found) == 1
    relation = inception.repository.get_relation(
        [
            item.id
            for item in inception.repository.find_relations(
                RelationKind.CONTRADICTS.value
            )
        ][0]
    )
    assert relation.attributes["contradiction_reason"] == "numeric_threshold"
    assert "45" in relation.attributes["contradiction_detail"]
    assert "30" in relation.attributes["contradiction_detail"]


def test_contradiction_needs_the_same_kind(inception):
    left = inception.nodes["rule_code"]
    right = inception.nodes["integration_billing"]
    assert contradiction_between(left, right) is None


def test_later_decision_supersedes_the_earlier_proposal(inception, report):
    found = report.of_kind(RelationKind.SUPERSEDES)
    assert len(found) == 1
    assert found[0].source_id == inception.id("decision").value
    assert found[0].target_id == inception.id("proposal").value


def test_ambiguous_candidates_become_a_gap_instead_of_a_relation(report, inception):
    assert any(item.startswith(AMBIGUOUS_PREFIX) for item in report.gaps)
    assert any(
        gap.name.startswith(AMBIGUOUS_PREFIX)
        for gap in open_gaps(inception.repository)
    )
    assert not _targets(inception, RelationKind.DECLARES, "requirement_gateway")


def test_every_created_relation_is_inferred_never_supported(inception, report):
    for item in report.relations:
        relation = inception.repository.get_relation(
            _relation_id(inception, item)
        )
        assert relation.confidence is Confidence.INFERRED


def _relation_id(inception, item):
    for relation in inception.repository.find_relations(item.kind):
        if (
            relation.source_id.value == item.source_id
            and relation.target_id.value == item.target_id
        ):
            return relation.id
    raise AssertionError(f"relação não encontrada: {item.kind}")


def test_relations_inherit_evidence_from_both_ends(inception, report):
    for item in report.relations:
        stored = inception.repository.evidence_for_relation(
            _relation_id(inception, item)
        )
        assert {evidence.id for evidence in stored} == set(item.evidence_ids)
        assert item.evidence_ids


def test_relations_keep_the_origin_of_the_correlation(inception, report):
    for item in report.relations:
        relation = inception.repository.get_relation(
            _relation_id(inception, item)
        )
        assert relation.attributes[CORRELATED_BY] in (
            "stable_key",
            "alias",
            "impact",
        )
        assert isinstance(relation.attributes[SCORE], float)
        assert LEFT_SOURCE in relation.attributes
        assert RIGHT_SOURCE in relation.attributes


def test_correlation_records_the_source_of_each_side(inception, report):
    contradicts = report.of_kind(RelationKind.CONTRADICTS)[0]
    assert {contradicts.left_source_id, contradicts.right_source_id} == {
        "src_xlsx",
        "src_code",
    }


def test_correlation_is_idempotent(inception, report):
    entities = inception.repository.entity_count()
    relations = inception.repository.relation_count()
    again = correlate(inception.repository, "ns")
    third = correlate(inception.repository, "ns")
    assert inception.repository.entity_count() == entities
    assert inception.repository.relation_count() == relations
    assert again.counts == report.counts == third.counts
    assert again.gaps == report.gaps


def test_correlation_writes_a_single_revision(inception):
    before = inception.repository.revision_count()
    correlate(inception.repository, "ns")
    assert inception.repository.revision_count() == before + 1


def test_report_serializes_without_prose(report):
    payload = report.to_dict()
    assert payload["counts"]
    assert payload["threshold"] > 0
    assert len(payload["relations"]) == report.total


def test_threshold_is_data_and_can_suppress_every_match(inception):
    strict = correlate(inception.repository, "ns", threshold=1.1)
    assert strict.of_kind(RelationKind.DECLARES) == ()
    assert strict.of_kind(RelationKind.CONTRADICTS) == ()


def test_a_chain_of_decisions_supersedes_without_closing_a_cycle(inception):
    repository = inception.repository
    newer = _decision(
        "decision_record::migrar renovacao para salesforce v3",
        "2026-05-20",
    )
    with repository.begin_revision("pipeline", "terceira decisao") as revision:
        revision.put_entity(newer)
    report = correlate(repository, "ns")
    chain = report.of_kind(RelationKind.SUPERSEDES)
    assert len(chain) >= 2
    assert newer.id.value in {item.source_id for item in chain}


def _decision(key: str, decided_at: str) -> Entity:
    return Entity.create(
        kind="decision_record",
        name="Migrar Renovacao para Salesforce",
        stable_key=key,
        attributes={
            "decision": "migrar renovacao para Salesforce",
            "decided_at": decided_at,
        },
        epistemic=EpistemicStatus.PROPOSED,
        confidence=Confidence.INFERRED,
    )
