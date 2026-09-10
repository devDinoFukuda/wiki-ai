from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

import pytest

from tests.investigation.fake_provider import FakeProvider, Script
from wiki_ai.agent.protocol import ToolCall
from wiki_ai.agent.session import AgentSession, RunStatus, SessionError
from wiki_ai.publishing.answer import (
    FALLBACK_NOTE,
    AnswerMode,
    Answerer,
    AnswerStatus,
)
from wiki_ai.publishing.answer_session import (
    ClaimVerdict,
    RejectionReason,
    answer_schema,
    build_briefing,
    validate_envelope,
)
from wiki_ai.publishing.query_harness import (
    InvalidQueryArguments,
    KnowledgeQueryHarness,
    TOOL_NAMES,
    UnknownQueryTool,
)

ARCHITECTURAL = (
    "Qual seria a consequência arquitetural de trocar Kafka por chamada síncrona "
    "neste fluxo?"
)

INTERNAL_ID = re.compile(r"(ent_|rel_|evd_|srcv_)[0-9a-f]{8,}")
PROVIDER_NAMES = ("claude", "codex", "devin", "cursor", "windsurf", "anthropic", "openai")


@pytest.fixture
def harness(graph) -> KnowledgeQueryHarness:
    return KnowledgeQueryHarness(graph.repository, "ns")


def _topic_id(graph) -> str:
    return graph.id("topic").value


def _integration_id(graph) -> str:
    return graph.id("integration").value


def _capability_id(graph) -> str:
    return graph.id("capability").value


def _rule_id(graph) -> str:
    return graph.id("rule").value


def _architectural_script(graph) -> Script:
    topic = _topic_id(graph)
    integration = _integration_id(graph)
    capability = _capability_id(graph)

    def build(_captures: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        return (
            {
                "answer": (
                    "Trocar o tópico assíncrono por uma chamada síncrona acopla a "
                    "capacidade de renovação à disponibilidade do Billing API, que "
                    "hoje é alcançado por integração com política de retry e uma "
                    "fila manual de fallback."
                ),
                "claims": [
                    {
                        "statement": (
                            "A renovação depende do Billing API, que publica o "
                            "tópico renewal-events"
                        ),
                        "entity_ids": [capability, integration, topic],
                        "evidence_ids": [_capability_evidence(graph)],
                        "confidence": "supported",
                    }
                ],
                "unresolved": [
                    "A janela de renovação não está decidida pelo conhecimento"
                ],
            },
        )

    return Script(
        steps=(
            ("knowledge.search", {"text": "renewal-events"}),
            ("knowledge.impact", {"entity_id": integration}),
            ("knowledge.evidence", {"entity_id": capability}),
            ("knowledge.gaps", {"blocking_only": True}),
        ),
        build_findings=build,
    )


def _capability_evidence(graph) -> str:
    found = graph.repository.evidence_for(graph.id("capability"))
    return found[0].id


def test_harness_offers_the_ten_read_only_tools(harness):
    assert set(harness.names()) == set(TOOL_NAMES)
    assert len(harness.names()) == 10


def test_harness_rejects_an_unknown_tool(harness):
    with pytest.raises(UnknownQueryTool):
        harness.invoke("knowledge.mutate", {})


def test_harness_rejects_unknown_and_missing_arguments(harness, graph):
    with pytest.raises(InvalidQueryArguments):
        harness.invoke("knowledge.search", {"texto": "renovacao"})
    with pytest.raises(InvalidQueryArguments):
        harness.invoke("knowledge.entity", {})
    with pytest.raises(InvalidQueryArguments):
        harness.invoke(
            "knowledge.neighbors",
            {"entity_id": _capability_id(graph), "direction": "sideways"},
        )


def test_harness_rejects_an_entity_outside_the_knowledge(harness):
    with pytest.raises(InvalidQueryArguments):
        harness.invoke("knowledge.entity", {"entity_id": "ent_deadbeefdeadbeef"})


def test_harness_results_are_json_serialisable_and_bounded(harness, graph):
    import json

    payload = harness.invoke("knowledge.entity", {"entity_id": _rule_id(graph)})
    text = json.dumps(payload, ensure_ascii=False)
    assert "cliente adimplente pode renovar" in text
    assert len(text) < 4000


def test_harness_caps_the_number_of_results(harness):
    payload = harness.invoke("knowledge.search", {"text": "a", "max_results": 2})
    assert len(payload["entities"]) <= 2


def test_impact_reaches_the_capability_from_the_integration(harness, graph):
    payload = harness.invoke("knowledge.impact", {"entity_id": _integration_id(graph)})
    reached = {item["entity"]["name"] for item in payload["impacted"]}
    assert "Renovacao" in reached


def test_briefing_states_the_question_and_the_method(harness):
    text = build_briefing(ARCHITECTURAL, harness.names(), "ns")
    assert ARCHITECTURAL in text
    assert "knowledge.evidence" in text
    assert "unresolved" in text


def test_answer_schema_declares_the_claim_envelope():
    schema = answer_schema()
    claim = schema["properties"]["claims"]["items"]
    assert set(claim["required"]) == {"statement", "entity_ids", "evidence_ids"}
    assert set(claim["properties"]) == {
        "statement",
        "entity_ids",
        "evidence_ids",
        "confidence",
    }
    assert "unresolved" in schema["properties"]


def test_agent_answers_a_question_outside_the_old_intents(graph):
    provider = FakeProvider(scripts=[_architectural_script(graph)])
    outcome = Answerer().run(ARCHITECTURAL, graph.repository, provider, "ns")
    assert outcome.mode is AnswerMode.AGENTIC
    assert outcome.reason == ""
    assert "Billing API" in outcome.answer
    assert FALLBACK_NOTE not in outcome.answer
    assert _capability_id(graph) in outcome.entity_ids
    assert _capability_evidence(graph) in outcome.evidence_ids


def test_agentic_answer_carries_a_provenance_appendix(graph):
    provider = FakeProvider(scripts=[_architectural_script(graph)])
    outcome = Answerer().run(ARCHITECTURAL, graph.repository, provider, "ns")
    assert "Proveniência:" in outcome.answer
    assert "src/renewal.py" in outcome.answer


def test_agentic_answer_never_exposes_internal_identifiers(graph):
    provider = FakeProvider(scripts=[_architectural_script(graph)])
    outcome = Answerer().run(ARCHITECTURAL, graph.repository, provider, "ns")
    assert not INTERNAL_ID.search(outcome.answer)


def test_the_agent_navigated_the_knowledge_before_answering(graph):
    provider = FakeProvider(scripts=[_architectural_script(graph)])
    Answerer().run(ARCHITECTURAL, graph.repository, provider, "ns")
    assert provider.calls == 1


def test_the_session_offers_the_harness_tools_and_the_answer_schema(graph):
    seen: list[AgentSession] = []

    class Recorder:
        def run(self, session):
            seen.append(session)
            return session.finish(
                [
                    {
                        "answer": "resposta",
                        "claims": [
                            {
                                "statement": "a renovacao existe",
                                "entity_ids": [_capability_id(graph)],
                                "evidence_ids": [_capability_evidence(graph)],
                            }
                        ],
                    }
                ]
            )

    Answerer().run(ARCHITECTURAL, graph.repository, Recorder(), "ns")
    session = seen[0]
    assert set(session.tool_names()) == set(TOOL_NAMES)
    assert session.finding_schema == dict(answer_schema())
    assert session.snapshot_id == "ns"


def test_a_claim_citing_a_forged_evidence_is_discarded(graph):
    forged = "evd_" + "0" * 32

    def build(_captures):
        return (
            {
                "answer": "A renovação chama o Billing API de forma síncrona.",
                "claims": [
                    {
                        "statement": "A chamada ao billing é síncrona",
                        "entity_ids": [_capability_id(graph)],
                        "evidence_ids": [forged],
                    },
                    {
                        "statement": "A capability Renovacao chama a Billing API",
                        "entity_ids": [_capability_id(graph)],
                        "evidence_ids": [_capability_evidence(graph)],
                    },
                ],
            },
        )

    provider = FakeProvider(scripts=[Script(build_findings=build)])
    outcome = Answerer().run(ARCHITECTURAL, graph.repository, provider, "ns")
    assert outcome.mode is AnswerMode.AGENTIC
    assert forged not in outcome.evidence_ids
    assert any(
        RejectionReason.UNKNOWN_EVIDENCE.value in item for item in outcome.unresolved
    )
    assert outcome.status is AnswerStatus.PARTIAL
    assert "A chamada ao billing é síncrona" not in outcome.answer


def test_a_claim_citing_a_forged_entity_is_discarded(harness, graph):
    validated = validate_envelope(
        {
            "answer": "texto",
            "claims": [
                {
                    "statement": "afirmação",
                    "entity_ids": ["ent_" + "1" * 32],
                    "evidence_ids": [_capability_evidence(graph)],
                }
            ],
        },
        harness,
    )
    assert validated.claims[0].verdict is ClaimVerdict.REJECTED
    assert validated.claims[0].reason is RejectionReason.UNKNOWN_ENTITY
    assert not validated.supported_claims


def test_a_claim_without_evidence_is_not_supported(harness):
    validated = validate_envelope(
        {
            "answer": "texto",
            "claims": [
                {"statement": "afirmação", "entity_ids": [], "evidence_ids": []}
            ],
        },
        harness,
    )
    assert validated.claims[0].verdict is ClaimVerdict.REJECTED
    assert validated.claims[0].reason is RejectionReason.NO_EVIDENCE


def _rule_evidence(graph) -> str:
    return graph.repository.evidence_for(graph.id("rule"))[0].id


def _validate_one(harness, statement, entity_ids, evidence_ids):
    validated = validate_envelope(
        {
            "answer": "rascunho",
            "claims": [
                {
                    "statement": statement,
                    "entity_ids": list(entity_ids),
                    "evidence_ids": list(evidence_ids),
                }
            ],
        },
        harness,
    )
    return validated.claims[0]


def test_evidence_of_another_entity_does_not_sustain_the_claim(harness, graph):
    claim = _validate_one(
        harness,
        "A capability Renovacao chama a Billing API",
        [_capability_id(graph)],
        [_rule_evidence(graph)],
    )
    assert claim.verdict is ClaimVerdict.REJECTED
    assert claim.reason is RejectionReason.EVIDENCE_NOT_LINKED
    assert claim.rejected_evidence_ids == (_rule_evidence(graph),)


def test_the_same_evidence_sustains_the_entity_it_is_linked_to(harness, graph):
    claim = _validate_one(
        harness,
        "A capability Renovacao chama a Billing API",
        [_capability_id(graph)],
        [_capability_evidence(graph)],
    )
    assert claim.verdict is ClaimVerdict.VALID
    assert claim.reason is RejectionReason.NONE


def test_a_claim_the_evidence_does_not_sustain_is_rejected(harness, graph):
    claim = _validate_one(
        harness,
        "O sistema deleta todos os clientes aos domingos",
        [_capability_id(graph)],
        [_capability_evidence(graph)],
    )
    assert claim.verdict is ClaimVerdict.REJECTED
    assert claim.reason is RejectionReason.NOT_GROUNDED


def test_an_ungrounded_claim_names_the_terms_without_backing(harness, graph):
    claim = _validate_one(
        harness,
        "O sistema deleta todos os clientes aos domingos",
        [_capability_id(graph)],
        [_capability_evidence(graph)],
    )
    assert claim.reason is RejectionReason.NOT_GROUNDED
    assert "domingos" in claim.missing_terms
    assert "domingos" in claim.to_dict()["missing_terms"]


def test_the_free_answer_text_never_reaches_the_reader(graph):
    def build(_captures):
        return (
            {
                "answer": "O sistema deleta todos os clientes aos domingos",
                "claims": [
                    {
                        "statement": "A Renovacao existe",
                        "entity_ids": [_capability_id(graph)],
                        "evidence_ids": [_capability_evidence(graph)],
                    }
                ],
            },
        )

    provider = FakeProvider(scripts=[Script(build_findings=build)])
    outcome = Answerer().run(ARCHITECTURAL, graph.repository, provider, "ns")
    assert outcome.mode is AnswerMode.AGENTIC
    assert outcome.status is AnswerStatus.ANSWERED
    assert "deleta todos os clientes" not in outcome.answer
    assert "domingos" not in outcome.answer
    assert "A Renovacao existe" in outcome.answer
    assert outcome.draft == "O sistema deleta todos os clientes aos domingos"


def test_only_validated_claims_are_assembled_into_the_answer(graph):
    def build(_captures):
        return (
            {
                "answer": "O sistema deleta todos os clientes aos domingos",
                "claims": [
                    {
                        "statement": "A Renovacao existe",
                        "entity_ids": [_capability_id(graph)],
                        "evidence_ids": [_capability_evidence(graph)],
                    },
                    {
                        "statement": "A Renovacao apaga o cadastro aos domingos",
                        "entity_ids": [_capability_id(graph)],
                        "evidence_ids": [_capability_evidence(graph)],
                    },
                ],
            },
        )

    provider = FakeProvider(scripts=[Script(build_findings=build)])
    outcome = Answerer().run(ARCHITECTURAL, graph.repository, provider, "ns")
    assert outcome.status is AnswerStatus.PARTIAL
    assert "A Renovacao existe" in outcome.answer
    assert "apaga o cadastro" not in outcome.answer
    assert any(
        RejectionReason.NOT_GROUNDED.value in item for item in outcome.unresolved
    )


def test_no_validated_claim_blocks_the_agentic_answer(graph):
    def build(_captures):
        return (
            {
                "answer": "O sistema deleta todos os clientes aos domingos",
                "claims": [
                    {
                        "statement": "A Renovacao apaga o cadastro aos domingos",
                        "entity_ids": [_capability_id(graph)],
                        "evidence_ids": [_capability_evidence(graph)],
                    }
                ],
            },
        )

    provider = FakeProvider(scripts=[Script(build_findings=build)])
    outcome = Answerer().run(ARCHITECTURAL, graph.repository, provider, "ns")
    assert outcome.status is AnswerStatus.BLOCKED
    assert outcome.reason == "no_validated_claims"
    assert "deleta todos os clientes" not in outcome.answer
    assert "apaga o cadastro" not in outcome.answer
    assert outcome.draft == "O sistema deleta todos os clientes aos domingos"


def test_the_briefing_states_that_the_free_answer_is_only_a_draft(harness):
    briefing = build_briefing(ARCHITECTURAL, harness.names(), "ns")
    assert "draft" in briefing
    assert "borrowed from another entity is refused" in briefing


def test_a_rejected_claim_is_reported_with_its_typed_reason(harness, graph):
    claim = _validate_one(
        harness,
        "A capability Renovacao chama a Billing API",
        [_capability_id(graph)],
        [_rule_evidence(graph)],
    )
    assert claim.to_dict()["reason"] == "evidence_not_linked"
    assert claim.to_dict()["verdict"] == "rejected"


def test_every_claim_forged_falls_back_to_the_deterministic_engine(graph):
    forged = "evd_" + "2" * 32

    def build(_captures):
        return (
            {
                "answer": "inventado",
                "claims": [
                    {
                        "statement": "afirmação sem lastro",
                        "entity_ids": [],
                        "evidence_ids": [forged],
                    }
                ],
            },
        )

    provider = FakeProvider(scripts=[Script(build_findings=build)])
    outcome = Answerer().run(ARCHITECTURAL, graph.repository, provider, "ns")
    assert outcome.mode is AnswerMode.DETERMINISTIC_FALLBACK
    assert outcome.status is AnswerStatus.BLOCKED
    assert outcome.reason == "no_validated_claims"
    assert "inventado" not in outcome.answer
    assert outcome.draft == "inventado"


def test_without_a_provider_the_answer_is_an_explicit_fallback(graph):
    outcome = Answerer().run(ARCHITECTURAL, graph.repository, None, "ns")
    assert outcome.mode is AnswerMode.DETERMINISTIC_FALLBACK
    assert outcome.reason == "provider_unavailable"
    assert FALLBACK_NOTE in outcome.answer


def test_a_failing_provider_falls_back_with_a_reason(graph):
    provider = FakeProvider(scripts=[Script(fail_with="provider lost the connection")])
    outcome = Answerer().run(ARCHITECTURAL, graph.repository, provider, "ns")
    assert outcome.mode is AnswerMode.DETERMINISTIC_FALLBACK
    assert outcome.reason.startswith(RunStatus.FAILED.value)
    assert "provider lost the connection" in outcome.reason
    assert FALLBACK_NOTE in outcome.answer


def test_a_provider_exhausting_the_budget_falls_back_with_a_reason(graph):
    provider = FakeProvider(scripts=[Script(exhaust_budget=True)])
    outcome = Answerer().run(ARCHITECTURAL, graph.repository, provider, "ns")
    assert outcome.mode is AnswerMode.DETERMINISTIC_FALLBACK
    assert outcome.reason.startswith(RunStatus.BUDGET_EXHAUSTED.value)


def test_a_provider_raising_falls_back_with_a_reason(graph):
    class Broken:
        def run(self, session):
            raise OSError("socket closed")

    outcome = Answerer().run(ARCHITECTURAL, graph.repository, Broken(), "ns")
    assert outcome.mode is AnswerMode.DETERMINISTIC_FALLBACK
    assert outcome.reason.startswith("provider_unreachable")


def test_a_provider_returning_a_foreign_result_falls_back(graph):
    class Foreign:
        def run(self, session):
            return {"answer": "sem envelope"}

    outcome = Answerer().run(ARCHITECTURAL, graph.repository, Foreign(), "ns")
    assert outcome.mode is AnswerMode.DETERMINISTIC_FALLBACK
    assert outcome.reason.startswith("invalid_run")


def test_a_run_without_an_answer_envelope_falls_back(graph):
    class Silent:
        def run(self, session):
            return session.finish([{"claim": "nada", "evidence": []}])

    outcome = Answerer().run(ARCHITECTURAL, graph.repository, Silent(), "ns")
    assert outcome.mode is AnswerMode.DETERMINISTIC_FALLBACK
    assert outcome.reason == "agent_answer_empty"


def test_a_session_error_falls_back_with_a_reason(graph):
    class Rogue:
        def run(self, session):
            raise SessionError("budget invalid")

    outcome = Answerer().run(ARCHITECTURAL, graph.repository, Rogue(), "ns")
    assert outcome.mode is AnswerMode.DETERMINISTIC_FALLBACK
    assert outcome.reason.startswith("agent_session_error")


def test_a_tool_error_reaches_the_agent_as_a_failed_result(graph):
    seen: list[Any] = []

    class Prober:
        def run(self, session):
            result = session.invoke(
                ToolCall(name="knowledge.entity", arguments={"entity_id": "ent_none"})
            )
            seen.append(result)
            return session.finish(
                [
                    {
                        "answer": "resposta",
                        "claims": [
                            {
                                "statement": "a renovacao existe",
                                "entity_ids": [_capability_id(graph)],
                                "evidence_ids": [_capability_evidence(graph)],
                            }
                        ],
                    }
                ]
            )

    Answerer().run(ARCHITECTURAL, graph.repository, Prober(), "ns")
    assert seen[0].ok is False
    assert "not part of the knowledge" in seen[0].error


def test_no_provider_name_leaks_into_the_answer_modules():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "wiki_ai" / "publishing"
    for name in ("answer.py", "answer_session.py", "answer_fallback.py", "query_harness.py"):
        text = (root / name).read_text(encoding="utf-8").lower()
        for provider in PROVIDER_NAMES:
            assert provider not in text


def test_rules_tool_lists_the_business_rule(harness):
    payload = harness.invoke("knowledge.rules", {})
    statements = {rule["statement"] for rule in payload["rules"]}
    assert "cliente adimplente pode renovar" in statements
    conditions = payload["rules"][0]["conditions"]
    assert "sem debito" in conditions


def test_rules_tool_scoped_to_an_entity_returns_its_rules(harness, graph):
    payload = harness.invoke(
        "knowledge.rules", {"entity_id": _capability_id(graph)}
    )
    assert {rule["entity"]["name"] for rule in payload["rules"]} == {
        "Elegibilidade de renovacao"
    }


def test_flow_tool_returns_the_ordered_steps(harness, graph):
    payload = harness.invoke("knowledge.flow", {"entity_id": graph.id("flow").value})
    names = [step["entity"]["name"] for step in payload["steps"]]
    assert names[:3] == ["Validar payload", "Checar elegibilidade", "Gravar renovacao"]


def test_neighbors_tool_walks_typed_relations(harness, graph):
    payload = harness.invoke(
        "knowledge.neighbors",
        {"entity_id": _capability_id(graph), "relation_kind": "calls"},
    )
    assert {item["entity"]["name"] for item in payload["neighbors"]} == {"Billing API"}


def test_gaps_tool_separates_the_blocking_question(harness):
    blocking = harness.invoke("knowledge.gaps", {"blocking_only": True})
    assert [gap["question"] for gap in blocking["gaps"]] == [
        "Qual a janela de renovacao?"
    ]
    assert all(gap["blocking"] for gap in blocking["gaps"])


def test_compare_tool_surfaces_the_divergences(harness):
    payload = harness.invoke("knowledge.compare", {})
    categories = {finding["category"] for finding in payload["findings"]}
    assert "declared_not_implemented" in categories
    assert "source_contradicts_source" in categories


def test_source_tool_reports_the_captured_versions(harness):
    payload = harness.invoke("knowledge.source", {})
    assert {item["source_id"] for item in payload["sources"]} == {"src_repo", "src_doc"}
    assert all(item["version_hash"] for item in payload["sources"])


def test_evidence_tool_never_returns_the_excerpt_body(harness, graph):
    payload = harness.invoke("knowledge.evidence", {"entity_id": _capability_id(graph)})
    entry = payload["evidence"][0]
    assert set(entry) == {
        "evidence_id",
        "source_id",
        "version_hash",
        "where",
        "locator_kind",
    }
    assert "src/renewal.py" in entry["where"]


def test_the_harness_records_which_entities_were_visited(harness, graph):
    harness.invoke("knowledge.entity", {"entity_id": _capability_id(graph)})
    harness.invoke("knowledge.evidence", {"entity_id": _capability_id(graph)})
    assert harness.visited() == (_capability_id(graph),)
