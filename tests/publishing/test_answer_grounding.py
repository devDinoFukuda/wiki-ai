from __future__ import annotations

from typing import Sequence

import pytest

from tests.publishing.grounding_fixture import (
    CAPABILITY_EXCERPT,
    RULE_EXCERPT,
    GroundingGraph,
)
from wiki_ai.knowledge.grounding import excerpt_vocabulary
from wiki_ai.publishing.answer_session import (
    ANSWER_RULES,
    ClaimVerdict,
    RejectionReason,
    build_briefing,
    validate_envelope,
)
from wiki_ai.publishing.query_harness import (
    MAX_EXCERPT_CHARS,
    KnowledgeQueryHarness,
    TOOL_EVIDENCE,
)


def _claim(
    harness: KnowledgeQueryHarness,
    statement: str,
    entity_ids: Sequence[str],
    evidence_ids: Sequence[str],
):
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


def _capability(grounded: GroundingGraph) -> tuple[str, str]:
    return grounded.id("capability").value, grounded.evidence_id("capability")


def _rule(grounded: GroundingGraph) -> tuple[str, str]:
    return grounded.id("rule").value, grounded.evidence_id("rule")


def test_a_claim_faithful_to_the_excerpt_is_valid(grounded_harness, grounded):
    entity, evidence = _capability(grounded)
    claim = _claim(
        grounded_harness,
        "A Renovacao chama o billing_api ao cobrar o contrato",
        [entity],
        [evidence],
    )
    assert claim.verdict is ClaimVerdict.VALID
    assert claim.reason is RejectionReason.NONE
    assert claim.missing_terms == ()


def test_a_claim_quoting_a_symbol_of_the_excerpt_is_valid(grounded_harness, grounded):
    entity, evidence = _capability(grounded)
    claim = _claim(
        grounded_harness,
        "A Renovacao publica RenewalCompleted com o id do contrato",
        [entity],
        [evidence],
    )
    assert claim.verdict is ClaimVerdict.VALID


def test_a_claim_with_a_number_the_excerpt_does_not_carry_is_rejected(
    grounded_harness, grounded
):
    entity, evidence = _rule(grounded)
    claim = _claim(
        grounded_harness,
        "O limite e de 7 tentativas de cobranca por contrato",
        [entity],
        [evidence],
    )
    assert claim.verdict is ClaimVerdict.REJECTED
    assert claim.reason is RejectionReason.NOT_GROUNDED
    assert "7" in claim.missing_terms
    assert "7" in claim.to_dict()["missing_terms"]


def test_a_claim_negating_what_the_excerpt_states_is_rejected(
    grounded_harness, grounded
):
    entity, evidence = _capability(grounded)
    claim = _claim(
        grounded_harness,
        "A Renovacao nao chama o billing_api ao cobrar o contrato",
        [entity],
        [evidence],
    )
    assert claim.verdict is ClaimVerdict.REJECTED
    assert claim.reason is RejectionReason.NOT_GROUNDED
    assert "!negated" in claim.missing_terms


def test_a_claim_with_a_predicate_absent_from_the_excerpt_is_rejected(
    grounded_harness, grounded
):
    entity, evidence = _capability(grounded)
    claim = _claim(
        grounded_harness,
        "A Renovacao envia email de confirmacao ao cliente",
        [entity],
        [evidence],
    )
    assert claim.verdict is ClaimVerdict.REJECTED
    assert claim.reason is RejectionReason.NOT_GROUNDED
    assert "envia" in claim.missing_terms


def test_an_evidence_without_excerpt_sustains_nothing(grounded_harness, grounded):
    entity = grounded.id("silent").value
    evidence = grounded.evidence_id("silent")
    claim = _claim(
        grounded_harness, "O Cancelamento remove o contrato", [entity], [evidence]
    )
    assert claim.verdict is ClaimVerdict.REJECTED
    assert claim.reason is RejectionReason.EVIDENCE_WITHOUT_EXCERPT
    assert claim.reason.value == "evidence_without_excerpt"
    assert claim.rejected_evidence_ids == (evidence,)
    assert claim.to_dict()["reason"] == "evidence_without_excerpt"


def test_an_evidence_without_excerpt_is_refused_even_for_a_literal_claim(
    grounded_harness, grounded
):
    entity = grounded.id("silent").value
    evidence = grounded.evidence_id("silent")
    claim = _claim(grounded_harness, "Cancelamento", [entity], [evidence])
    assert claim.verdict is ClaimVerdict.REJECTED
    assert claim.reason is RejectionReason.EVIDENCE_WITHOUT_EXCERPT


def test_the_locator_is_not_a_source_of_grounding_vocabulary(
    grounded_harness, grounded
):
    entity, evidence = _capability(grounded)
    vocabulary = grounded_harness.grounding_vocabulary([entity], [evidence])
    assert "codehash1" not in vocabulary
    assert "src_repo" not in vocabulary
    assert "line_start" not in vocabulary


def test_the_grounding_vocabulary_is_the_excerpt_plus_the_entity_identity(
    grounded_harness, grounded
):
    entity, evidence = _capability(grounded)
    vocabulary = grounded_harness.grounding_vocabulary([entity], [evidence])
    assert excerpt_vocabulary(CAPABILITY_EXCERPT) <= vocabulary
    assert "renovacao" in vocabulary
    assert "capability" in vocabulary


def test_the_rule_statement_belongs_to_the_grounding_vocabulary(
    grounded_harness, grounded
):
    entity, evidence = _rule(grounded)
    vocabulary = grounded_harness.grounding_vocabulary([entity], [evidence])
    assert excerpt_vocabulary(RULE_EXCERPT) <= vocabulary
    assert "adimplente" in vocabulary


def test_evidence_without_excerpt_is_reported_by_the_harness(
    grounded_harness, grounded
):
    silent = grounded.evidence_id("silent")
    good = grounded.evidence_id("capability")
    assert grounded_harness.evidence_without_excerpt([silent]) == (silent,)
    assert grounded_harness.evidence_without_excerpt([good]) == ()
    assert grounded_harness.evidence_without_excerpt([good, silent]) == (silent,)


def test_the_evidence_tool_hands_the_excerpt_to_the_agent(grounded_harness, grounded):
    payload = grounded_harness.invoke(
        TOOL_EVIDENCE, {"entity_id": grounded.id("capability").value}
    )
    entry = payload["evidence"][0]
    assert entry["excerpt"] == CAPABILITY_EXCERPT.strip()
    assert "billing_api" in entry["excerpt"]


def test_the_evidence_tool_declares_the_excerpt_in_its_output_schema(grounded_harness):
    spec = grounded_harness.spec_for(TOOL_EVIDENCE)
    item = spec.output_schema["properties"]["evidence"]["items"]
    assert item["properties"]["excerpt"] == {"type": "string"}
    assert "excerpt" in spec.description


def test_a_long_excerpt_reaches_the_agent_truncated_with_an_explicit_marker(tmp_path):
    from tests.publishing.grounding_fixture import build_with_excerpt

    long_excerpt = "renovacao do contrato pelo billing api. " * 200
    graph = build_with_excerpt(tmp_path / "long", long_excerpt)
    harness = KnowledgeQueryHarness(graph.repository, "ns")
    assert len(long_excerpt) > MAX_EXCERPT_CHARS
    payload = harness.invoke(
        TOOL_EVIDENCE, {"entity_id": graph.id("capability").value}
    )
    entry = payload["evidence"][0]
    assert len(entry["excerpt"]) <= MAX_EXCERPT_CHARS + 1
    assert entry["excerpt"].endswith("…")
    assert entry["excerpt"] != long_excerpt
    graph.repository.close()


def test_a_truncated_excerpt_still_grounds_what_it_shows(tmp_path):
    from tests.publishing.grounding_fixture import build_with_excerpt

    long_excerpt = "renovacao do contrato pelo billing api. " * 200
    graph = build_with_excerpt(tmp_path / "long", long_excerpt)
    harness = KnowledgeQueryHarness(graph.repository, "ns")
    claim = _claim(
        harness,
        "A renovacao do contrato passa pelo billing api",
        [graph.id("capability").value],
        [graph.evidence_id("capability")],
    )
    assert claim.verdict is ClaimVerdict.VALID
    graph.repository.close()


def test_the_briefing_tells_the_agent_the_claim_is_checked_against_the_excerpt(
    grounded_harness,
):
    briefing = build_briefing("pergunta", grounded_harness.names(), "ns")
    assert "excerpt" in briefing
    assert any("excerpt" in rule for rule in ANSWER_RULES)


@pytest.mark.parametrize(
    "statement",
    (
        "A Renovacao chama o billing_api ao cobrar o contrato",
        "A Renovacao publica RenewalCompleted com o id do contrato",
    ),
)
def test_every_grounded_claim_reaches_the_reader(grounded_harness, grounded, statement):
    entity, evidence = _capability(grounded)
    claim = _claim(grounded_harness, statement, [entity], [evidence])
    assert claim.supported
