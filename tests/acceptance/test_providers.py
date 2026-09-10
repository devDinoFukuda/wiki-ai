from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from tests.acceptance.scenarios import (
    CAPABILITY,
    CONTROLLER,
    NAMESPACE_OBJECTIVE,
    java_acceptance_repo,
)
from tests.agent import fake_cli
from wiki_ai.agent.protocol import ToolCall, ToolResult
from wiki_ai.agent.providers.bridge import FINISH_TOOL
from wiki_ai.agent.providers.claude import ClaudeProvider
from wiki_ai.agent.providers.codex import CodexProvider
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.agent.session import AgentSession, Budget, ToolSpec
from wiki_ai.app import api
from wiki_ai.app.session import Session
from wiki_ai.app.wiring import InvestigationAdapter, Wiring
from wiki_ai.investigation.orchestrator import Investigator
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.repository.harness import RepositoryHarness
from wiki_ai.repository.schemas import TOOL_NAMES
from wiki_ai.repository.snapshot import SnapshotSpec, take_snapshot

LAUNCHER = (sys.executable,)
TIMEOUT = 120.0
TOOL_BUDGET = 4

FINDING: Mapping[str, Any] = {
    "type": "capability",
    "subject": CAPABILITY,
    "statement": "OrderController place delegates the reference to OrderService place",
    "evidence": [{"path": CONTROLLER, "line_start": 15, "line_end": 17}],
    "confidence": "supported",
}

PLAN = [
    {
        "name": "evidence.capture",
        "arguments": {
            "path": CONTROLLER,
            "line_start": 15,
            "line_end": 17,
            "symbol": "place",
        },
    },
    {"name": FINISH_TOOL, "arguments": {"findings": [FINDING]}},
]

VOLATILE = ("captures", "snapshot_id", "objective_hash", "coverage")


class _BudgetedWiring(Wiring):
    def investigation_runner(self) -> InvestigationAdapter:
        return InvestigationAdapter(
            Investigator(total_tool_calls=TOOL_BUDGET, round_budget=TOOL_BUDGET)
        )


@pytest.fixture()
def binary(tmp_path: Path) -> Path:
    workspace = tmp_path / "cli"
    workspace.mkdir(parents=True, exist_ok=True)
    return fake_cli.install(workspace)


def _env(style: str) -> dict[str, str]:
    return {"FAKE_PLAN": json.dumps(PLAN), "FAKE_STYLE": style}


def _wiring(name: str, binary: Path) -> Wiring:
    registry = ProviderRegistry()
    if name == "claude":
        registry.register(
            name,
            lambda: ClaudeProvider(
                binary=str(binary),
                timeout=TIMEOUT,
                env=_env("mcp-config"),
                launcher=LAUNCHER,
            ),
        )
    else:
        registry.register(
            name,
            lambda: CodexProvider(
                binary=str(binary),
                timeout=TIMEOUT,
                env=_env("home-toml"),
                launcher=LAUNCHER,
            ),
        )
    return _BudgetedWiring(registry)


def _stable(details: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(details)
    for key in VOLATILE:
        payload.pop(key, None)
    return json.loads(json.dumps(payload, sort_keys=True))


def _knowledge_shape(repo: Path) -> list[dict[str, Any]]:
    with Session.open(repo).open_knowledge() as knowledge:
        return _entities(knowledge)


def _entities(knowledge: KnowledgeRepository) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for entity in sorted(knowledge.find_entities(), key=lambda item: item.id.value):
        found.append(
            {
                "kind": entity.kind,
                "name": entity.name,
                "confidence": entity.confidence.value,
                "state": entity.state.value,
                "attributes": dict(entity.attributes),
                "locators": sorted(
                    evidence.locator.to_dict()["path"]
                    for evidence in knowledge.evidence_for(entity.id)
                ),
            }
        )
    return found


def _run(name: str, binary: Path, root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    repo = java_acceptance_repo(root)
    report = api.analyze(repo, NAMESPACE_OBJECTIVE, wiring=_wiring(name, binary))
    assert report.status == "ok", report.to_dict()
    return report.to_dict(), _knowledge_shape(repo)


@pytest.fixture()
def outcomes(binary: Path, tmp_path: Path) -> dict[str, tuple[dict[str, Any], list]]:
    return {
        name: _run(name, binary, tmp_path / name)
        for name in ("claude", "codex")
    }


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_each_provider_runs_the_objective_through_the_app(
    binary: Path, tmp_path: Path, name: str
) -> None:
    payload, entities = _run(name, binary, tmp_path / name)
    assert payload["details"]["entities_written"] > 0
    assert payload["details"]["evidence_written"] > 0
    assert any(item["kind"] == "capability" for item in entities)


def test_the_workflow_is_the_same_for_both_providers(
    outcomes: dict[str, tuple[dict[str, Any], list]]
) -> None:
    claude = _stable(outcomes["claude"][0]["details"]["details"])
    codex = _stable(outcomes["codex"][0]["details"]["details"])
    assert claude == codex


def test_the_persistence_is_the_same_for_both_providers(
    outcomes: dict[str, tuple[dict[str, Any], list]]
) -> None:
    assert outcomes["claude"][1] == outcomes["codex"][1]


def test_the_reported_counts_are_the_same_for_both_providers(
    outcomes: dict[str, tuple[dict[str, Any], list]]
) -> None:
    keys = ("entities_written", "relations_written", "evidence_written", "unresolved")
    claude = {key: outcomes["claude"][0]["details"][key] for key in keys}
    codex = {key: outcomes["codex"][0]["details"][key] for key in keys}
    assert claude == codex


def test_both_providers_receive_the_same_tools_and_workspace(
    binary: Path, tmp_path: Path
) -> None:
    repo = java_acceptance_repo(tmp_path / "tools")
    harness = RepositoryHarness(take_snapshot(SnapshotSpec(root=repo)))
    reports = {
        "claude": _report(
            ClaudeProvider(
                binary=str(binary),
                timeout=TIMEOUT,
                env=_env("mcp-config"),
                launcher=LAUNCHER,
            ),
            harness,
        ),
        "codex": _report(
            CodexProvider(
                binary=str(binary),
                timeout=TIMEOUT,
                env=_env("home-toml"),
                launcher=LAUNCHER,
            ),
            harness,
        ),
    }
    assert reports["claude"]["tools"] == reports["codex"]["tools"]
    assert set(reports["claude"]["tools"]) == set(TOOL_NAMES) | {FINISH_TOOL}
    assert reports["claude"]["cwd"] == []
    assert reports["codex"]["cwd"] == []


def _session_for(harness: RepositoryHarness) -> AgentSession:
    specs = {
        spec.name: ToolSpec(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
        )
        for spec in harness.specs()
    }
    return AgentSession(
        objective="describe how this system works",
        tools=specs,
        budget=Budget(max_tool_calls=2, max_seconds=TIMEOUT),
        snapshot_id=harness.snapshot.digest,
        executor=_ok,
    )


def _report(provider: Any, harness: RepositoryHarness) -> dict[str, Any]:
    captured: dict[str, str] = {}
    runner = provider._runner
    original = runner.execute

    def spy(argv, cwd, timeout, env=None, stdin_text=None):
        result = original(argv, cwd, timeout, env, stdin_text)
        captured["stdout"] = result.stdout
        return result

    runner.execute = spy
    try:
        provider.run(_session_for(harness))
    finally:
        runner.execute = original
    return fake_cli.report_of(captured.get("stdout", ""))


def _ok(call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.name, ok=True, payload={"text": "ok"})
