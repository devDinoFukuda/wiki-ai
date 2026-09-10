from __future__ import annotations

import re

from wiki_ai.publishing.answer import Answerer, Intent, classify

_INTERNAL_ID = re.compile(r"(ent_|rel_|evd_|srcv_)[0-9a-f]{8,}")

QUESTIONS: tuple[tuple[str, Intent], ...] = (
    ("Analise profundamente este sistema.", Intent.DEEP_ANALYSIS),
    ("Como funciona a renovação?", Intent.CAPABILITY_PROFILE),
    ("Quais regras de negócio participam deste fluxo?", Intent.BUSINESS_RULES),
    (
        "Quais são os inputs, outputs, invariantes e edge cases?",
        Intent.CONTRACT,
    ),
    ("Quais integrações são críticas?", Intent.CRITICAL_INTEGRATIONS),
    ("O que pode quebrar se esta regra mudar?", Intent.IMPACT),
    (
        "Compare o comportamento implementado com a decisão tomada na inception.",
        Intent.COMPARISON,
    ),
    (
        "Quais pontos desta migração ainda não possuem evidência?",
        Intent.MISSING_EVIDENCE,
    ),
    (
        "Gere o material atualizado para publicação no SharePoint.",
        Intent.PUBLICATION,
    ),
)


def _answer(graph, question):
    return Answerer().run(question, graph.repository, None, "ns")


def test_every_question_is_classified_as_expected():
    for question, intent in QUESTIONS:
        assert classify(question) is intent


def test_every_question_produces_an_answer(graph):
    for question, _intent in QUESTIONS:
        outcome = _answer(graph, question)
        assert outcome.question == question
        assert outcome.answer.strip()


def test_answers_never_expose_internal_identifiers(graph):
    for question, _intent in QUESTIONS:
        assert not _INTERNAL_ID.search(_answer(graph, question).answer)


def test_capability_answer_describes_the_renewal(graph):
    outcome = _answer(graph, "Como funciona a renovação?")
    assert "Renovacao" in outcome.answer
    assert "POST /renewals" in outcome.answer
    assert "Billing API" in outcome.answer
    assert outcome.entity_ids
    assert outcome.evidence_ids


def test_business_rules_answer_lists_the_rule(graph):
    outcome = _answer(graph, "Quais regras de negócio participam deste fluxo?")
    assert "cliente adimplente pode renovar" in outcome.answer


def test_contract_answer_covers_inputs_outputs_and_edge_cases(graph):
    outcome = _answer(
        graph, "Quais são os inputs, outputs, invariantes e edge cases?"
    )
    assert "RenewalRequest" in outcome.answer
    assert "RenewalResponse" in outcome.answer
    assert "Cliente sem historico" in outcome.answer


def test_integration_answer_covers_failure_modes(graph):
    outcome = _answer(graph, "Quais integrações são críticas?")
    assert "Billing API" in outcome.answer
    assert "billing lento" in outcome.answer


def test_impact_answer_names_the_rule_and_its_tests(graph):
    outcome = _answer(graph, "O que pode quebrar se esta regra mudar?")
    assert "Elegibilidade de renovacao" in outcome.answer
    assert "Renovacao feliz" in outcome.answer


def test_comparison_answer_covers_the_five_categories(graph):
    outcome = _answer(
        graph,
        "Compare o comportamento implementado com a decisão tomada na inception.",
    )
    assert "Declarado sem implementação encontrada" in outcome.answer
    assert "Proposta em conflito com o comportamento atual" in outcome.answer
    assert "Decisão que substitui proposta anterior" in outcome.answer
    assert "Fonte que contradiz outra fonte" in outcome.answer
    assert outcome.unresolved


def test_missing_evidence_answer_lists_open_gaps(graph):
    outcome = _answer(graph, "Quais pontos desta migração ainda não possuem evidência?")
    assert "Qual a janela de renovacao?" in outcome.answer
    assert "bloqueante" in outcome.answer
    assert "Qual a janela de renovacao?" in outcome.unresolved


def test_publication_answer_describes_the_material(graph):
    outcome = _answer(graph, "Gere o material atualizado para publicação no SharePoint.")
    assert "capacidade" in outcome.answer
    assert "1 sistema(s)" in outcome.answer


def test_deep_analysis_lists_systems_and_capabilities(graph):
    outcome = _answer(graph, "Analise profundamente este sistema.")
    assert "Plataforma" in outcome.answer
    assert "Renovacao" in outcome.answer
    assert outcome.unresolved


def test_answers_are_deterministic(graph):
    for question, _intent in QUESTIONS:
        assert _answer(graph, question).answer == _answer(graph, question).answer


def test_provider_is_ignored_in_this_wave(graph):
    class Provider:
        pass

    plain = Answerer().run("Como funciona a renovação?", graph.repository, None, "ns")
    with_provider = Answerer().run(
        "Como funciona a renovação?", graph.repository, Provider(), "ns"
    )
    assert plain.answer == with_provider.answer


def test_enricher_extension_point_can_rewrite_the_answer(graph):
    class Shout:
        def enrich(self, intent, question, answer):
            return answer.upper()

    outcome = Answerer(enricher=Shout()).run(
        "Como funciona a renovação?", graph.repository, None, "ns"
    )
    assert outcome.answer == outcome.answer.upper()


def test_enricher_falling_back_keeps_the_deterministic_answer(graph):
    class Empty:
        def enrich(self, intent, question, answer):
            return "   "

    plain = Answerer().run("Como funciona a renovação?", graph.repository, None, "ns")
    fallback = Answerer(enricher=Empty()).run(
        "Como funciona a renovação?", graph.repository, None, "ns"
    )
    assert plain.answer == fallback.answer
