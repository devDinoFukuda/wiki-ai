from __future__ import annotations

import pytest

from wiki_ai.agent.protocol import ProtocolError, ToolCall, ToolResult
from wiki_ai.agent.session import (
    AgentSession,
    Budget,
    BudgetExhausted,
    BudgetInvalid,
    SessionError,
    ToolSpec,
)

READ = ToolSpec(
    name="repo.read",
    description="read a file range",
    input_schema={"path": "string"},
    output_schema={"text": "string"},
)
SEARCH = ToolSpec(name="repo.search", description="search the snapshot")


def _session(budget: Budget | None = None, started_at: float = 0.0) -> AgentSession:
    return AgentSession(
        objective="how does renewal work",
        tools={READ.name: READ, SEARCH.name: SEARCH},
        budget=budget or Budget(max_tool_calls=3, max_tokens=100, max_seconds=60.0),
        snapshot_id="d" * 64,
        started_at=started_at,
    )


def _pair(number: int) -> tuple[ToolCall, ToolResult]:
    call = ToolCall(name=READ.name, arguments={"path": f"src/{number}.py"})
    result = ToolResult(call_id=f"call-{number}", ok=True, payload={"text": "x"})
    return call, result


def test_session_exposes_objective_tools_and_snapshot() -> None:
    session = _session()
    assert session.objective == "how does renewal work"
    assert session.snapshot_id == "d" * 64
    assert session.tool_names() == ("repo.read", "repo.search")
    assert session.tool("repo.read") == READ


def test_session_rejects_empty_objective() -> None:
    with pytest.raises(SessionError):
        AgentSession("  ", {}, Budget(max_tool_calls=1, max_seconds=1.0), "abc")


def test_session_rejects_empty_snapshot_id() -> None:
    with pytest.raises(SessionError):
        AgentSession("goal", {}, Budget(max_tool_calls=1, max_seconds=1.0), " ")


def test_session_rejects_mismatched_tool_key() -> None:
    with pytest.raises(SessionError):
        AgentSession(
            "goal",
            {"other": READ},
            Budget(max_tool_calls=1, max_seconds=1.0),
            "abc",
        )


def test_tool_spec_requires_name_and_description() -> None:
    with pytest.raises(SessionError):
        ToolSpec(name=" ", description="x")
    with pytest.raises(SessionError):
        ToolSpec(name="x", description="  ")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_tool_calls": 0, "max_seconds": 1.0},
        {"max_tool_calls": 1, "max_seconds": 0.0},
        {"max_tool_calls": 1, "max_tokens": 0, "max_seconds": 1.0},
    ],
)
def test_budget_rejects_non_positive_limits(kwargs: dict) -> None:
    with pytest.raises(BudgetInvalid):
        Budget(**kwargs)


def test_record_appends_to_the_transcript_and_counts_usage() -> None:
    session = _session()
    call, result = _pair(1)
    entry = session.record(call, result, tokens=10, now=1.0)
    assert entry.ordinal == 0
    assert session.transcript[0].call is call
    usage = session.usage(now=1.0)
    assert usage.tool_calls == 1
    assert usage.tokens == 10
    assert usage.seconds == pytest.approx(1.0)


def test_record_rejects_an_unknown_tool() -> None:
    session = _session()
    call = ToolCall(name="repo.absent")
    result = ToolResult(call_id="c", ok=True)
    with pytest.raises(ProtocolError):
        session.record(call, result, now=1.0)


def test_record_rejects_negative_token_counts() -> None:
    session = _session()
    call, result = _pair(1)
    with pytest.raises(SessionError):
        session.record(call, result, tokens=-1, now=1.0)


def test_session_exhausts_the_tool_call_budget() -> None:
    session = _session(Budget(max_tool_calls=2, max_seconds=60.0))
    for number in range(2):
        session.record(*_pair(number), now=1.0)
    assert session.remaining_tool_calls() == 0
    assert session.exhausted(now=1.0) == "max_tool_calls"
    with pytest.raises(BudgetExhausted) as caught:
        session.record(*_pair(9), now=1.0)
    assert caught.value.limit == "max_tool_calls"
    assert len(session.transcript) == 2


def test_session_exhausts_the_token_budget() -> None:
    session = _session(Budget(max_tool_calls=10, max_tokens=15, max_seconds=60.0))
    session.record(*_pair(1), tokens=15, now=1.0)
    assert session.exhausted(now=1.0) == "max_tokens"
    with pytest.raises(BudgetExhausted) as caught:
        session.record(*_pair(2), tokens=1, now=1.0)
    assert caught.value.limit == "max_tokens"


def test_session_exhausts_the_time_budget() -> None:
    session = _session(Budget(max_tool_calls=10, max_seconds=5.0))
    assert session.exhausted(now=4.9) is None
    with pytest.raises(BudgetExhausted) as caught:
        session.record(*_pair(1), now=5.1)
    assert caught.value.limit == "max_seconds"


def test_session_without_token_ceiling_never_exhausts_tokens() -> None:
    session = _session(Budget(max_tool_calls=5, max_tokens=None, max_seconds=60.0))
    session.record(*_pair(1), tokens=10_000, now=1.0)
    assert session.exhausted(now=1.0) is None


def test_session_serializes_transcript_and_budget() -> None:
    session = _session()
    session.record(*_pair(1), tokens=4, now=2.0)
    payload = session.to_dict(now=2.0)
    assert payload["objective"] == "how does renewal work"
    assert payload["budget"]["max_tool_calls"] == 3
    assert payload["usage"]["tool_calls"] == 1
    assert payload["transcript"][0]["tool"] == "repo.read"
    assert payload["transcript"][0]["call_id"] == "call-1"
    assert [tool["name"] for tool in payload["tools"]] == ["repo.read", "repo.search"]


def test_unknown_tool_lookup_is_typed() -> None:
    session = _session()
    with pytest.raises(SessionError):
        session.tool("repo.absent")
