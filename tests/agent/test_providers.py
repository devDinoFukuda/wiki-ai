from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from tests.agent import fake_cli
from wiki_ai.agent.protocol import AgentProvider, ToolCall, ToolResult
from wiki_ai.agent.providers.bridge import FINISH_TOOL
from wiki_ai.agent.providers.broker import ToolBrokerServer
from wiki_ai.agent.providers.claude import (
    DISALLOWED_TOOLS,
    ClaudeProvider,
    allowed_tools,
)
from wiki_ai.agent.providers.cli_common import BinaryMissing, CliRunner, minimal_env
from wiki_ai.agent.providers.codex import CodexProvider, config_document
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.agent.session import AgentSession, Budget, RunStatus, ToolSpec

READ = ToolSpec(
    name="repo.read",
    description="read a file range",
    input_schema={"path": {"type": "string"}},
)
SEARCH = ToolSpec(name="repo.search", description="search the snapshot")
_TIMEOUT = 60.0


def _session(budget: Budget | None = None, calls: list | None = None) -> AgentSession:
    def executor(call: ToolCall) -> ToolResult:
        if calls is not None:
            calls.append(call)
        return ToolResult(call_id=call.name, ok=True, payload={"text": "monthly"})

    return AgentSession(
        objective="how does renewal work",
        tools={READ.name: READ, SEARCH.name: SEARCH},
        budget=budget or Budget(max_tool_calls=5, max_seconds=120.0),
        snapshot_id="d" * 64,
        executor=executor,
    )


LAUNCHER = (sys.executable,)


def _providers(binary: Path, env: dict, timeout: float = _TIMEOUT) -> dict:
    return {
        "config-flag": ClaudeProvider(
            binary=str(binary),
            timeout=timeout,
            env={**env, "FAKE_STYLE": "mcp-config"},
            launcher=LAUNCHER,
        ),
        "config-home": CodexProvider(
            binary=str(binary),
            timeout=timeout,
            env={**env, "FAKE_STYLE": "home-toml"},
            launcher=LAUNCHER,
        ),
    }


@pytest.fixture()
def binary(tmp_path: Path) -> Path:
    return fake_cli.install(tmp_path)


def _plan_env(plan: list, **extra: str) -> dict:
    return {"FAKE_PLAN": json.dumps(plan), **extra}


@pytest.mark.parametrize("style", ["config-flag", "config-home"])
def test_a_provider_runs_the_agent_and_collects_findings(
    binary: Path, style: str
) -> None:
    calls: list[ToolCall] = []
    session = _session(calls=calls)
    provider = _providers(binary, _plan_env(fake_cli.READ_THEN_FINISH))[style]
    run = provider.run(session)
    assert run.status is RunStatus.COMPLETED
    assert run.findings == (fake_cli.FINDING,)
    assert [call.name for call in calls] == ["repo.read"]
    assert run.usage.tool_calls == 1


def test_both_providers_produce_an_identical_run(binary: Path) -> None:
    env = _plan_env(fake_cli.READ_THEN_FINISH)
    outcomes = {}
    for style, provider in _providers(binary, env).items():
        run = provider.run(_session())
        outcomes[style] = {
            "status": run.status.value,
            "findings": [dict(f) for f in run.findings],
            "transcript": [entry.to_dict() for entry in run.transcript],
        }
    assert outcomes["config-flag"] == outcomes["config-home"]


def _report(provider, session) -> dict:
    captured: dict[str, str] = {}
    runner = provider._runner
    original = runner.execute

    def spy(argv, cwd, timeout, env=None, stdin_text=None):
        result = original(argv, cwd, timeout, env, stdin_text)
        captured["stdout"] = result.stdout
        return result

    runner.execute = spy
    try:
        provider.run(session)
    finally:
        runner.execute = original
    return fake_cli.report_of(captured.get("stdout", ""))


@pytest.mark.parametrize("style", ["config-flag", "config-home"])
def test_only_the_session_tools_are_exposed(binary: Path, style: str) -> None:
    provider = _providers(binary, _plan_env(fake_cli.READ_THEN_FINISH))[style]
    report = _report(provider, _session())
    assert report["tools"] == ["repo.read", "repo.search", FINISH_TOOL]


@pytest.mark.parametrize("style", ["config-flag", "config-home"])
def test_the_agent_workspace_is_empty(binary: Path, style: str) -> None:
    provider = _providers(binary, _plan_env(fake_cli.READ_THEN_FINISH))[style]
    report = _report(provider, _session())
    assert report["cwd"] == []


@pytest.mark.parametrize("style", ["config-flag", "config-home"])
def test_an_agent_that_never_finishes_fails_with_a_reason(
    binary: Path, style: str
) -> None:
    plan = [{"name": "repo.read", "arguments": {"path": "a.py"}}]
    provider = _providers(binary, _plan_env(plan))[style]
    run = provider.run(_session())
    assert run.status is RunStatus.FAILED
    assert run.reason == "findings_not_delivered"
    assert run.findings == ()


@pytest.mark.parametrize("style", ["config-flag", "config-home"])
def test_an_unknown_tool_does_not_break_the_session(binary: Path, style: str) -> None:
    plan = [
        {"name": "repo.write", "arguments": {"path": "a.py"}},
        {"name": "repo.read", "arguments": {"path": "a.py"}},
        {"name": FINISH_TOOL, "arguments": {"findings": [fake_cli.FINDING]}},
    ]
    provider = _providers(binary, _plan_env(plan))[style]
    run = provider.run(_session())
    assert run.status is RunStatus.COMPLETED
    assert run.findings == (fake_cli.FINDING,)
    assert [entry.call.name for entry in run.transcript] == ["repo.read"]


@pytest.mark.parametrize("style", ["config-flag", "config-home"])
def test_an_exhausted_budget_stops_the_run(binary: Path, style: str) -> None:
    plan = [
        {"name": "repo.read", "arguments": {"path": "a.py"}},
        {"name": "repo.read", "arguments": {"path": "b.py"}},
        {"name": FINISH_TOOL, "arguments": {"findings": [fake_cli.FINDING]}},
    ]
    session = _session(Budget(max_tool_calls=1, max_seconds=120.0))
    provider = _providers(binary, _plan_env(plan))[style]
    run = provider.run(session)
    assert run.status is RunStatus.BUDGET_EXHAUSTED
    assert run.usage.tool_calls == 1
    assert run.findings == ()


@pytest.mark.parametrize("style", ["config-flag", "config-home"])
def test_a_timeout_fails_the_run(binary: Path, style: str) -> None:
    plan = [{"name": "repo.read", "arguments": {"path": "a.py"}}]
    env = _plan_env(plan, FAKE_HANG="30")
    run = _providers(binary, env, timeout=5.0)[style].run(_session())
    assert run.status is RunStatus.FAILED
    assert run.reason == "timeout"


@pytest.mark.parametrize("style", ["config-flag", "config-home"])
def test_cancel_kills_the_subprocess(binary: Path, style: str) -> None:
    plan = [{"name": "repo.read", "arguments": {"path": "a.py"}}]
    env = _plan_env(plan, FAKE_HANG="30")
    provider = _providers(binary, env)[style]
    timer = threading.Timer(4.0, provider.cancel)
    timer.start()
    try:
        run = provider.run(_session())
    finally:
        timer.cancel()
    assert run.status is RunStatus.CANCELLED
    assert run.reason == "cancelled"


@pytest.mark.parametrize("style", ["config-flag", "config-home"])
def test_connect_accepts_a_reachable_binary(binary: Path, style: str) -> None:
    provider = _providers(binary, {})[style]
    provider.connect()
    assert provider.capabilities().supports(FINISH_TOOL)


def test_connect_reports_a_missing_binary() -> None:
    with pytest.raises(BinaryMissing):
        CliRunner.locate("wiki-ai-absent-agent-binary")


@pytest.mark.parametrize("style", ["config-flag", "config-home"])
def test_a_provider_satisfies_the_agent_contract(binary: Path, style: str) -> None:
    assert isinstance(_providers(binary, {})[style], AgentProvider)


def test_allowed_tools_are_restricted_to_the_session() -> None:
    session = _session()
    assert allowed_tools(session) == (
        "mcp__wiki__repo.read",
        "mcp__wiki__repo.search",
        f"mcp__wiki__{FINISH_TOOL}",
    )


def _command_line(binary: Path) -> list[str]:
    provider = _providers(binary, {})["config-flag"]
    broker = ToolBrokerServer(_session())
    broker.start()
    settings = Path(tempfile.mkdtemp())
    try:
        return provider._argv(_session(), settings, broker)
    finally:
        broker.stop()
        shutil.rmtree(settings, ignore_errors=True)


def test_the_command_line_never_bypasses_permissions(binary: Path) -> None:
    argv = _command_line(binary)
    assert "--permission-mode" not in argv
    assert "bypassPermissions" not in argv


def test_the_command_line_denies_the_built_in_write_tools(binary: Path) -> None:
    argv = _command_line(binary)
    index = argv.index("--disallowedTools")
    assert argv[index + 1 : index + 1 + len(DISALLOWED_TOOLS)] == list(DISALLOWED_TOOLS)
    for tool in ("Bash", "Edit", "Write", "WebFetch"):
        assert tool in argv


def test_the_command_line_allows_only_the_session_mcp_tools(binary: Path) -> None:
    argv = _command_line(binary)
    index = argv.index("--allowedTools")
    granted = argv[index + 1 : index + 1 + len(allowed_tools(_session()))]
    assert granted == list(allowed_tools(_session()))
    assert all(name.startswith("mcp__wiki__") for name in granted)


def test_the_config_document_declares_only_session_tools() -> None:
    document = config_document("127.0.0.1", 5000, 30.0, ("repo.read", FINISH_TOOL))
    assert "[mcp_servers.wiki]" in document
    assert '"repo.read"' in document
    assert 'inherit = "none"' in document


def test_the_minimal_env_drops_unlisted_variables(monkeypatch) -> None:
    monkeypatch.setenv("WIKI_AI_SECRET_TOKEN", "s3cret")
    assert "WIKI_AI_SECRET_TOKEN" not in minimal_env()


def test_the_registry_offers_both_adapters() -> None:
    registry = ProviderRegistry(adapters=True)
    assert len(registry.registered()) == 2
