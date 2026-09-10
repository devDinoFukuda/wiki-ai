from __future__ import annotations

import re

import pytest

from wiki_ai.knowledge.correlation import correlate
from wiki_ai.publishing.answer import Answerer, Intent, classify

from tests.knowledge.inception_fixture import SOURCE_IDS, build

_INTERNAL_ID = re.compile(r"(ent_|rel_|evd_|srv_)[0-9a-f]{8,}")

COMPLEXITY = "Quais sistemas, regras e integrações tornam a migração complexa?"
COMPARISON = "Compare o comportamento implementado com a decisão tomada na inception."
EVIDENCE = "Quais pontos desta migração ainda não possuem evidência?"

INCEPTION_QUESTIONS: tuple[tuple[str, Intent], ...] = (
    (COMPLEXITY, Intent.INCEPTION),
    (COMPARISON, Intent.COMPARISON),
    (EVIDENCE, Intent.MISSING_EVIDENCE),
)


@pytest.fixture()
def inception(tmp_path):
    fixture = build(tmp_path)
    correlate(fixture.repository, "ns")
    yield fixture
    fixture.repository.close()


def _answer(inception, question):
    return Answerer().run(question, inception.repository, None, "ns")


def _sources(inception, outcome):
    return {
        inception.repository.get_evidence(identifier).source_id
        for identifier in outcome.evidence_ids
    }


def test_every_inception_question_is_classified(inception):
    for question, intent in INCEPTION_QUESTIONS:
        assert classify(question) is intent


def test_every_inception_question_is_answered_in_portuguese(inception):
    for question, _intent in INCEPTION_QUESTIONS:
        answer = _answer(inception, question).answer
        assert answer.strip()
        assert not _INTERNAL_ID.search(answer)


def test_complexity_answer_correlates_the_proposal_with_the_codebase(inception):
    answer = _answer(inception, COMPLEXITY).answer
    assert "Migrar Renovacao para Salesforce" in answer
    assert "Renovacao" in answer
    assert "Billing" in answer
    assert "Eligibility" in answer
    assert "RenewalEvent" in answer


def test_complexity_answer_shows_provenance_of_every_source(inception):
    answer = _answer(inception, COMPLEXITY).answer
    assert "transcrição de Ana" in answer
    assert "planilha regras.xlsx, aba Renovacao, intervalo B4:D9" in answer
    assert "diagrama arquitetura.drawio, página Integracoes" in answer
    assert "código src/renewal/service.py" in answer
    assert "documento, trecho Escopo > Cancelamento" in answer


def test_complexity_answer_carries_evidence_from_every_source(inception):
    outcome = _answer(inception, COMPLEXITY)
    assert _sources(inception, outcome) == set(SOURCE_IDS)


def test_complexity_answer_ends_with_the_missing_evidence_section(inception):
    answer = _answer(inception, COMPLEXITY).answer
    assert "Sem evidência:" in answer
    assert answer.index("Sem evidência:") > answer.index("Migrar Renovacao")
    assert "ambiguous correlation" in answer.split("Sem evidência:")[1]


def test_comparison_answer_covers_the_five_buckets_with_both_sides(inception):
    outcome = _answer(inception, COMPARISON)
    for label in (
        "Declarado sem implementação encontrada",
        "Implementado sem documento que descreva",
        "Proposta em conflito com o comportamento atual",
        "Decisão que substitui proposta anterior",
        "Fonte que contradiz outra fonte",
    ):
        assert label in outcome.answer
    assert "de um lado, planilha regras.xlsx" in outcome.answer
    assert "do outro, código src/renewal/rules.py" in outcome.answer
    assert "Sem evidência:" in outcome.answer


def test_comparison_answer_carries_evidence_from_every_source(inception):
    outcome = _answer(inception, COMPARISON)
    assert _sources(inception, outcome) == set(SOURCE_IDS)


def test_missing_evidence_answer_lists_the_ambiguity_and_the_declared_gap(inception):
    outcome = _answer(inception, EVIDENCE)
    assert "ambiguous correlation" in outcome.answer
    assert "Cancelamento" in outcome.answer
    assert outcome.unresolved


def test_inception_answers_are_deterministic(inception):
    for question, _intent in INCEPTION_QUESTIONS:
        assert _answer(inception, question) == _answer(inception, question)


def test_answer_without_any_inception_record_says_so(tmp_path):
    from tests.knowledge.graph_fixture import build as plain

    graph = plain(tmp_path / "plain")
    outcome = Answerer().run(COMPLEXITY, graph.repository, None, "ns")
    assert outcome.answer.strip()
    graph.repository.close()
