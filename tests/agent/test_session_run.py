from __future__ import annotations

import pytest

from wiki_ai.agent.protocol import ProtocolError, ToolCall, ToolResult
from wiki_ai.agent.session import (
    AgentRun,
    AgentSession,
    Budget,
    BudgetExhausted,
    RunStatus,
    SessionAlreadyFinished,
    SessionError,
    ToolSpec,
)

READ = ToolSpec(name="repo.read", description="read a file range")
FINDING = {
    "claim": "renewal is monthly",
    "evidence": [
        {"source_id": "src", "version": "v1", "locator": "billing.py:10-20"}
    ],
}


def _session(executor=None, budget: Budget | None = None) -> AgentSession:
    return AgentSession(
        objective="how does renewal work",
        tools={READ.name: READ},
        budget=budget or Budget(max_tool_calls=3, max_seconds=60.0),
        snapshot_id="d" * 64,
        executor=executor,
        started_at=0.0,
    )


def _ok(call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.name, ok=True, payload={"text": "body"})


def test_invoke_delegates_to_the_executor_and_records_the_transcript() -> None:
    seen: list[ToolCall] = []

    def executor(call: ToolCall) -> ToolResult:
        seen.append(call)
        return _ok(call)

    session = _session(executor)
    result = session.invoke(ToolCall("repo.read", {"path": "a.py"}), now=1.0)
    assert result.ok
    assert seen[0].arguments == {"path": "a.py"}
    assert len(session.transcript) == 1
    assert session.transcript[0].call.name == "repo.read"


def test_invoke_refuses_a_tool_outside_the_session() -> None:
    session = _session(_ok)
    with pytest.raises(ProtocolError):
        session.invoke(ToolCall("repo.write"), now=1.0)
    assert session.transcript == ()


def test_invoke_without_an_executor_is_typed() -> None:
    session = _session()
    with pytest.raises(SessionError):
        session.invoke(ToolCall("repo.read"), now=1.0)


def test_invoke_applies_the_budget_before_calling_the_executor() -> None:
    calls: list[ToolCall] = []

    def executor(call: ToolCall) -> ToolResult:
        calls.append(call)
        return _ok(call)

    session = _session(executor, Budget(max_tool_calls=1, max_seconds=60.0))
    session.invoke(ToolCall("repo.read"), now=1.0)
    with pytest.raises(BudgetExhausted):
        session.invoke(ToolCall("repo.read"), now=1.0)
    assert len(calls) == 1


def test_finish_seals_a_completed_run() -> None:
    session = _session(_ok)
    session.invoke(ToolCall("repo.read"), now=1.0)
    run = session.finish([FINDING], now=2.0)
    assert run.status is RunStatus.COMPLETED
    assert run.findings == (FINDING,)
    assert run.usage.tool_calls == 1
    assert len(run.transcript) == 1
    assert session.run is run


def test_a_session_produces_exactly_one_run() -> None:
    session = _session(_ok)
    session.finish([], now=1.0)
    with pytest.raises(SessionAlreadyFinished):
        session.finish([], now=1.0)
    with pytest.raises(SessionAlreadyFinished):
        session.invoke(ToolCall("repo.read"), now=1.0)


@pytest.mark.parametrize(
    "method,status",
    [
        ("fail", RunStatus.FAILED),
        ("cancelled", RunStatus.CANCELLED),
        ("budget_exhausted", RunStatus.BUDGET_EXHAUSTED),
    ],
)
def test_terminal_runs_carry_a_reason_and_no_findings(method: str, status) -> None:
    session = _session(_ok)
    run = getattr(session, method)("because", now=1.0)
    assert run.status is status
    assert run.reason == "because"
    assert run.findings == ()


def test_a_failed_run_may_not_carry_findings() -> None:
    with pytest.raises(SessionError):
        AgentRun(
            status=RunStatus.FAILED,
            findings=(FINDING,),
            transcript=(),
            usage=_session().usage(now=0.0),
            reason="x",
        )


def test_a_non_completed_run_requires_a_reason() -> None:
    with pytest.raises(SessionError):
        AgentRun(
            status=RunStatus.FAILED,
            findings=(),
            transcript=(),
            usage=_session().usage(now=0.0),
            reason="  ",
        )


def test_a_completed_run_carries_no_reason() -> None:
    with pytest.raises(SessionError):
        AgentRun(
            status=RunStatus.COMPLETED,
            findings=(),
            transcript=(),
            usage=_session().usage(now=0.0),
            reason="x",
        )


def test_the_session_publishes_the_finding_schema() -> None:
    session = _session(_ok)
    schema = session.finding_schema
    assert schema["required"] == ["claim", "evidence"]
    assert "evidence" in schema["properties"]


def test_run_serializes_for_the_harness() -> None:
    session = _session(_ok)
    session.invoke(ToolCall("repo.read"), now=1.0)
    payload = session.finish([FINDING], now=2.0).to_dict()
    assert payload["status"] == "completed"
    assert payload["findings"][0]["claim"] == "renewal is monthly"
    assert payload["usage"]["tool_calls"] == 1
