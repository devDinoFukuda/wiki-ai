from __future__ import annotations

from wiki_ai.ingestion.hints import (
    CandidateKind,
    detect,
    hints_for,
    normalize_text,
    stable_key,
)
from wiki_ai.ingestion.source import BlockKind, make_block


def _block(text: str, kind: BlockKind = BlockKind.PARAGRAPH, **attributes: object):
    return make_block("src-1", 0, kind, text, {"section": "s", "block": "b"}, None, attributes)


def test_normalize_strips_accents_and_case() -> None:
    assert normalize_text("Decisão  DE Crédito") == "decisao de credito"


def test_stable_key_is_canonical() -> None:
    assert stable_key("business_rule", "Regra  Ação") == "business_rule::regra acao"


def test_portuguese_decision_is_a_candidate() -> None:
    found = detect(_block("Decidimos manter o corte no dia 5."))
    assert CandidateKind.DECISION in {item.candidate_kind for item in found}


def test_english_decision_is_a_candidate() -> None:
    found = detect(_block("We decided to keep the cut-off."))
    assert CandidateKind.DECISION in {item.candidate_kind for item in found}


def test_question_requirement_risk_and_action_are_detected() -> None:
    cases = {
        "Qual a janela de retry?": CandidateKind.QUESTION,
        "O sistema deve recusar valores negativos.": CandidateKind.REQUIREMENT,
        "Existe risco de perda de dados.": CandidateKind.RISK,
        "Vou revisar a clausula amanha.": CandidateKind.ACTION,
        "Talvez o lote rode a noite.": CandidateKind.HYPOTHESIS,
        "O pedido passa para aprovado.": CandidateKind.TRANSITION,
    }
    for text, expected in cases.items():
        assert expected in {item.candidate_kind for item in detect(_block(text))}, text


def test_threshold_is_detected_from_a_comparison() -> None:
    found = detect(_block("aplicar quando valor > 100"))
    assert CandidateKind.THRESHOLD in {item.candidate_kind for item in found}


def test_rule_table_comes_from_the_header_row() -> None:
    block = _block(
        "Regra\tCondicao\tAcao",
        BlockKind.TABLE,
        header_row=["Regra", "Condicao", "Acao"],
    )
    kinds = {item.candidate_kind for item in detect(block)}
    assert CandidateKind.RULE_TABLE in kinds


def test_a_single_header_hit_is_not_enough() -> None:
    block = _block("Regra\tOwner", BlockKind.TABLE, header_row=["Regra", "Owner"])
    assert CandidateKind.RULE_TABLE not in {item.candidate_kind for item in detect(block)}


def test_a_hint_never_reaches_full_confidence() -> None:
    block = _block(
        "Regra\tCondicao\tAcao\tEfeito",
        BlockKind.TABLE,
        header_row=["Regra", "Condicao", "Acao", "Efeito"],
    )
    assert all(item.score < 1.0 for item in detect(block))


def test_hints_are_never_marked_authoritative() -> None:
    payload = detect(_block("Decidimos parar."))[0].to_dict()
    assert payload["authoritative"] is False


def test_text_without_a_marker_yields_nothing() -> None:
    assert detect(_block("O relatorio saiu ontem.")) == ()


def test_hints_for_filters_by_candidate_kind() -> None:
    blocks = [_block("Decidimos parar."), _block("Qual o prazo?")]
    found = hints_for(blocks, (CandidateKind.QUESTION,))
    assert {item.candidate_kind for item in found} == {CandidateKind.QUESTION}
