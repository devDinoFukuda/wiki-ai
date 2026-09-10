from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.investigation.fake_provider import FakeProvider, Script
from tests.investigation.fixtures_snapshots import java_repo

from wiki_ai.investigation.briefing import REPOSITORY_FOCUS
from wiki_ai.investigation.objective import Objective, ObjectiveKind, Scope, parse
from wiki_ai.investigation.orchestrator import Investigator
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.repository.harness import RepositoryHarness, ScopeFocus

CONTROLLER = "src/main/java/com/acme/order/OrderController.java"
SERVICE = "src/main/java/com/acme/order/OrderService.java"


def knowledge_at(tmp_path: Path) -> KnowledgeRepository:
    return KnowledgeRepository.open(str(tmp_path / "knowledge" / "state.db"))


def _objective() -> Objective:
    return Objective(
        kind=ObjectiveKind.TARGETED_REINVESTIGATION,
        scope=Scope(paths=(SERVICE,)),
        goal="reinvestigate " + SERVICE,
        text="reinvestigate " + SERVICE,
    )


def _finding() -> dict[str, Any]:
    return {
        "type": "business_rule",
        "subject": "order reference is persisted",
        "statement": "OrderService place saves the reference in the repository",
        "conditions": ["a reference is supplied"],
        "effects": ["the reference reaches OrderRepository save"],
        "evidence": [{"path": SERVICE, "line_start": 10, "line_end": 12}],
        "confidence": "supported",
    }


def _run(
    tmp_path: Path, objective: str | Objective, script: Script
) -> tuple[Any, FakeProvider]:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(scripts=[script])
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator(total_tool_calls=8, round_budget=8).run(
            objective, snapshot, knowledge, provider, "acme"
        )
    return outcome, provider


def test_a_ready_objective_is_used_without_reparsing(tmp_path: Path) -> None:
    objective = _objective()
    outcome, _ = _run(
        tmp_path,
        objective,
        Script(steps=(("repo.search", {"pattern": "place"}),)),
    )
    assert outcome.details["objective_hash"] == objective.hash
    assert outcome.details["objective"] == objective.to_dict()
    assert outcome.objective == objective.goal


def test_a_text_objective_and_the_parsed_objective_reach_the_same_hash(
    tmp_path: Path,
) -> None:
    text = "reinvestigate " + SERVICE
    parsed = parse(text, kind=ObjectiveKind.TARGETED_REINVESTIGATION)
    from_text, _ = _run(tmp_path / "a", text, Script())
    from_objective, _ = _run(tmp_path / "b", parsed, Script())
    assert from_text.details["objective_hash"] == from_objective.details["objective_hash"]


def test_the_scope_of_a_ready_objective_focuses_the_harness(tmp_path: Path) -> None:
    outcome, provider = _run(
        tmp_path,
        _objective(),
        Script(
            steps=(
                ("repo.search", {"pattern": "place"}),
                ("repo.read", {"path": SERVICE}),
                ("evidence.capture", {"path": SERVICE, "line_start": 10, "line_end": 12}),
            ),
            findings=[_finding()],
        ),
    )
    assert outcome.details["focus_paths"] == [SERVICE]
    assert outcome.details["outside_focus_reads"] == 0
    assert outcome.details["files_read"] == [SERVICE]
    briefing = provider.seen_objectives[0]
    assert f"Focus:\n- {SERVICE}" in briefing
    assert "scope=repository" in briefing


def test_following_a_caller_outside_the_focus_is_counted_in_the_details(
    tmp_path: Path,
) -> None:
    outcome, _ = _run(
        tmp_path,
        _objective(),
        Script(
            steps=(
                ("repo.read", {"path": SERVICE}),
                ("repo.read", {"path": CONTROLLER}),
            )
        ),
    )
    assert outcome.details["outside_focus_reads"] == 1
    assert set(outcome.details["files_read"]) == {SERVICE, CONTROLLER}


def test_an_objective_without_paths_stays_repository_wide(tmp_path: Path) -> None:
    outcome, provider = _run(
        tmp_path,
        Objective(goal="reconstruct implemented behavior"),
        Script(steps=(("repo.inventory", {}),)),
    )
    assert outcome.details["focus_paths"] == []
    assert outcome.details["outside_focus_reads"] == 0
    assert f"- {REPOSITORY_FOCUS}" in provider.seen_objectives[0]


def test_the_default_factory_builds_a_focused_harness(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path / "repo")
    harness = RepositoryHarness(snapshot, None, ScopeFocus(paths=(SERVICE,)))
    entries = harness.invoke("repo.inventory", {})["entries"]
    assert [entry["path"] for entry in entries] == [SERVICE]
