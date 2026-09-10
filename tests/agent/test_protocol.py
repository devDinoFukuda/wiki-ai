from __future__ import annotations

import pytest

from wiki_ai.agent.protocol import (
    AgentCapabilities,
    ProtocolError,
    ToolCall,
    ToolResult,
)
from wiki_ai.agent.session import (
    AgentProvider,
    AgentRun,
    AgentSession,
    Budget,
    ToolSpec,
)


class Recorder:
    def __init__(self) -> None:
        self.connected = False
        self.cancelled = False

    def connect(self) -> None:
        self.connected = True

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(tools=("read_file",), max_context_tokens=1000)

    def run(self, session: AgentSession) -> AgentRun:
        return session.finish([])

    def cancel(self) -> None:
        self.cancelled = True


def test_tool_call_requires_a_name():
    assert ToolCall("read_file", {"path": "a"}).name == "read_file"
    with pytest.raises(ProtocolError):
        ToolCall(" ")


def test_tool_result_ok_and_error_are_exclusive():
    assert ToolResult("c1", True, {"data": 1}).ok
    assert ToolResult("c1", False, error="timeout").error == "timeout"
    with pytest.raises(ProtocolError):
        ToolResult("c1", True, error="timeout")
    with pytest.raises(ProtocolError):
        ToolResult("c1", False)
    with pytest.raises(ProtocolError):
        ToolResult("", True)


def test_capabilities_reject_non_positive_context_window():
    assert AgentCapabilities().max_context_tokens is None
    with pytest.raises(ProtocolError):
        AgentCapabilities(max_context_tokens=0)


def test_capabilities_require_reports_missing_tools():
    caps = AgentCapabilities(tools=("read_file", "grep"))
    assert caps.supports("grep")
    caps.require("grep")
    with pytest.raises(ProtocolError) as info:
        caps.require("grep", "write_file")
    assert "write_file" in str(info.value)


def test_any_object_with_the_four_methods_is_a_provider():
    recorder = Recorder()
    assert isinstance(recorder, AgentProvider)
    recorder.connect()
    recorder.cancel()
    assert recorder.connected and recorder.cancelled
    session = AgentSession(
        objective="probe",
        tools={"repo.read": ToolSpec("repo.read", "read a file")},
        budget=Budget(max_tool_calls=1, max_seconds=1.0),
        snapshot_id="snap",
    )
    assert isinstance(recorder.run(session), AgentRun)


def test_incomplete_object_is_not_a_provider():
    class Partial:
        def connect(self) -> None:
            return None

    assert not isinstance(Partial(), AgentProvider)
