from __future__ import annotations

import io
import json
import socket

import pytest

from wiki_ai.agent.protocol import ToolCall, ToolResult
from wiki_ai.agent.providers.bridge import (
    FINISH_TOOL,
    SERVER_NAME,
    BrokerClient,
    serve,
)
from wiki_ai.agent.providers.broker import ToolBrokerServer
from wiki_ai.agent.providers.jsonrpc import ErrorCode, decode, encode
from wiki_ai.agent.session import AgentSession, Budget, ToolSpec

READ = ToolSpec(
    name="repo.read",
    description="read a file range",
    input_schema={"path": {"type": "string"}},
)
SEARCH = ToolSpec(name="repo.search", description="search the snapshot")


def _executor(call: ToolCall) -> ToolResult:
    if call.arguments.get("path") == "missing.py":
        return ToolResult(call_id=call.name, ok=False, error="not_found")
    return ToolResult(call_id=call.name, ok=True, payload={"text": "body"})


def _session(budget: Budget | None = None) -> AgentSession:
    return AgentSession(
        objective="how does renewal work",
        tools={READ.name: READ, SEARCH.name: SEARCH},
        budget=budget or Budget(max_tool_calls=5, max_seconds=60.0),
        snapshot_id="d" * 64,
        executor=_executor,
    )


def _request(broker: ToolBrokerServer, method: str, params: dict) -> dict:
    with socket.create_connection((broker.host, broker.port), timeout=10) as sock:
        stream = sock.makefile("rwb")
        stream.write(
            encode({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        )
        stream.flush()
        return decode(stream.readline())


@pytest.fixture()
def broker():
    session = _session()
    server = ToolBrokerServer(session)
    server.start()
    yield server
    server.stop()


def test_the_broker_lists_only_session_tools_plus_finish(broker) -> None:
    reply = _request(broker, "tools/list", {})
    names = [tool["name"] for tool in reply["result"]["tools"]]
    assert names == ["repo.read", "repo.search", FINISH_TOOL]


def test_a_tool_call_reaches_the_session_executor(broker) -> None:
    reply = _request(
        broker, "tools/call", {"name": "repo.read", "arguments": {"path": "a.py"}}
    )
    assert reply["result"]["structuredContent"] == {"text": "body"}
    assert reply["result"]["isError"] is False


def test_a_failing_tool_is_reported_without_breaking_the_session(broker) -> None:
    reply = _request(
        broker, "tools/call", {"name": "repo.read", "arguments": {"path": "missing.py"}}
    )
    assert reply["result"]["isError"] is True
    assert reply["result"]["error"] == "not_found"
    follow = _request(
        broker, "tools/call", {"name": "repo.read", "arguments": {"path": "a.py"}}
    )
    assert follow["result"]["isError"] is False


def test_an_unknown_tool_is_a_jsonrpc_error_and_the_session_survives(broker) -> None:
    reply = _request(broker, "tools/call", {"name": "repo.write", "arguments": {}})
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS.value
    follow = _request(broker, "tools/list", {})
    assert follow["result"]["tools"]


def test_an_unknown_method_is_a_jsonrpc_error(broker) -> None:
    reply = _request(broker, "resources/list", {})
    assert reply["error"]["code"] == ErrorCode.METHOD_NOT_FOUND.value


def test_finish_captures_the_findings(broker) -> None:
    finding = {
        "claim": "monthly",
        "evidence": [{"source_id": "s", "version": "v", "locator": "a.py:1"}],
    }
    reply = _request(
        broker, "tools/call", {"name": FINISH_TOOL, "arguments": {"findings": [finding]}}
    )
    assert reply["result"]["isError"] is False
    assert broker.finish.delivered
    assert broker.finish.findings == (finding,)


def test_finish_rejects_a_malformed_payload(broker) -> None:
    reply = _request(
        broker, "tools/call", {"name": FINISH_TOOL, "arguments": {"findings": "no"}}
    )
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS.value
    assert not broker.finish.delivered


def test_the_broker_flags_an_exhausted_budget() -> None:
    server = ToolBrokerServer(_session(Budget(max_tool_calls=1, max_seconds=60.0)))
    server.start()
    try:
        _request(server, "tools/call", {"name": "repo.read", "arguments": {}})
        assert not server.budget_exhausted
        reply = _request(server, "tools/call", {"name": "repo.read", "arguments": {}})
        assert reply["error"]["code"] == ErrorCode.INTERNAL_ERROR.value
        assert server.budget_exhausted
    finally:
        server.stop()


def _serve_once(broker: ToolBrokerServer, messages: list[dict]) -> list[dict]:
    source = io.BytesIO(b"".join(encode(message) for message in messages))
    sink = io.BytesIO()
    client = BrokerClient(broker.host, broker.port, timeout=10.0)
    try:
        serve(client, source, sink)
    finally:
        client.close()
    sink.seek(0)
    return [json.loads(line) for line in sink.read().splitlines() if line.strip()]


def test_the_bridge_relays_list_and_call_to_the_broker(broker) -> None:
    replies = _serve_once(
        broker,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "repo.read", "arguments": {"path": "a.py"}},
            },
        ],
    )
    assert replies[0]["result"]["serverInfo"]["name"] == SERVER_NAME
    names = [tool["name"] for tool in replies[1]["result"]["tools"]]
    assert names == ["repo.read", "repo.search", FINISH_TOOL]
    assert replies[2]["result"]["structuredContent"] == {"text": "body"}


def test_the_bridge_surfaces_an_unknown_tool_as_an_error(broker) -> None:
    replies = _serve_once(
        broker,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "repo.write", "arguments": {}},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ],
    )
    assert "error" in replies[0]
    assert replies[1]["result"]["tools"]


def test_the_bridge_rejects_an_unknown_method(broker) -> None:
    replies = _serve_once(
        broker, [{"jsonrpc": "2.0", "id": 1, "method": "prompts/list", "params": {}}]
    )
    assert replies[0]["error"]["code"] == ErrorCode.METHOD_NOT_FOUND.value


def test_the_bridge_survives_a_malformed_line(broker) -> None:
    source = io.BytesIO(
        b"{not json}\n" + encode({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    )
    sink = io.BytesIO()
    client = BrokerClient(broker.host, broker.port, timeout=10.0)
    try:
        serve(client, source, sink)
    finally:
        client.close()
    sink.seek(0)
    replies = [json.loads(line) for line in sink.read().splitlines() if line.strip()]
    assert replies[0]["error"]["code"] == ErrorCode.PARSE_ERROR.value
    assert replies[1]["result"]["tools"]
