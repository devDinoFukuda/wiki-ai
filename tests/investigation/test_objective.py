from __future__ import annotations

import pytest

from wiki_ai.investigation.objective import (
    DEFAULT_GOAL,
    Objective,
    ObjectiveError,
    ObjectiveKind,
    Scope,
    from_mapping,
    objective_hash,
    parse,
)


def test_bare_request_becomes_system_analysis_with_default_goal() -> None:
    parsed = parse("analyze repository")
    assert parsed.kind is ObjectiveKind.SYSTEM_ANALYSIS
    assert parsed.goal == DEFAULT_GOAL
    assert parsed.scope.is_repository_wide


def test_impact_wording_selects_impact_analysis() -> None:
    parsed = parse("identify behavior affected by renewal rule change")
    assert parsed.kind is ObjectiveKind.IMPACT_ANALYSIS
    assert parsed.goal == "identify behavior affected by renewal rule change"


def test_question_wording_selects_question() -> None:
    assert parse("how does the order flow persist data?").kind is ObjectiveKind.QUESTION


def test_capability_wording_selects_capability_analysis() -> None:
    assert (
        parse("describe the capability of placing orders").kind
        is ObjectiveKind.CAPABILITY_ANALYSIS
    )


def test_reinvestigation_wording_is_targeted() -> None:
    assert (
        parse("reinvestigate the renewal rule").kind
        is ObjectiveKind.TARGETED_REINVESTIGATION
    )


def test_paths_and_symbols_are_extracted_into_scope() -> None:
    parsed = parse("impact of changing src/main/java/OrderService.java and OrderRepository")
    assert "src/main/java/OrderService.java" in parsed.scope.paths
    assert "OrderRepository" in parsed.scope.symbols


def test_entity_ids_are_extracted_into_scope() -> None:
    parsed = parse("reinvestigate ent_0123456789abcdef")
    assert parsed.scope.entities == ("ent_0123456789abcdef",)


def test_hash_is_deterministic_and_scope_sensitive() -> None:
    first = parse("analyze repository")
    second = parse("analyse codebase")
    assert objective_hash(first) == objective_hash(second)
    scoped = Objective(goal=DEFAULT_GOAL, scope=Scope(paths=("a/b.py",)))
    assert objective_hash(scoped) != objective_hash(first)


def test_empty_text_is_rejected() -> None:
    with pytest.raises(ObjectiveError):
        parse("   ")


def test_objective_without_goal_is_rejected() -> None:
    with pytest.raises(ObjectiveError):
        Objective(goal="  ")


def test_from_mapping_reads_the_plan_shape() -> None:
    parsed = from_mapping(
        {"kind": "system_analysis", "scope": "repository", "goal": DEFAULT_GOAL}
    )
    assert parsed.kind is ObjectiveKind.SYSTEM_ANALYSIS
    assert parsed.scope.is_repository_wide


def test_from_mapping_rejects_unknown_kind() -> None:
    with pytest.raises(ObjectiveError):
        from_mapping({"kind": "form_filling", "goal": "x"})


def test_explicit_kind_overrides_detection() -> None:
    parsed = parse("analyze repository", kind=ObjectiveKind.QUESTION)
    assert parsed.kind is ObjectiveKind.QUESTION
