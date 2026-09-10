from __future__ import annotations

import socketserver
import threading
from typing import Any, Mapping

from wiki_ai.agent.protocol import ProtocolError, ToolCall
from wiki_ai.agent.providers.bridge import FINISH_TOOL
from wiki_ai.agent.providers.jsonrpc import (
    ErrorCode,
    JsonRpcError,
    decode,
    encode,
    error_message,
    parse_request,
    result_message,
)
from wiki_ai.agent.session import AgentSession, BudgetExhausted, SessionError

__all__ = [
    "FINISH_TOOL",
    "FinishRecord",
    "ToolBrokerServer",
]

_TOOLS_LIST = "tools/list"
_TOOLS_CALL = "tools/call"
_FINDINGS_KEY = "findings"


class FinishRecord:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._findings: tuple[Mapping[str, Any], ...] | None = None

    def deliver(self, findings: tuple[Mapping[str, Any], ...]) -> None:
        with self._lock:
            self._findings = findings

    @property
    def delivered(self) -> bool:
        with self._lock:
            return self._findings is not None

    @property
    def findings(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            return self._findings or ()


def _finish_spec(session: AgentSession) -> dict[str, Any]:
    return {
        "name": FINISH_TOOL,
        "description": "deliver the structured findings that answer the objective",
        "inputSchema": {
            "type": "object",
            "required": [_FINDINGS_KEY],
            "additionalProperties": False,
            "properties": {
                _FINDINGS_KEY: {
                    "type": "array",
                    "items": dict(session.finding_schema),
                }
            },
        },
    }


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        broker: ToolBrokerServer = self.server
        while True:
            line = self.rfile.readline()
            if not line:
                return
            try:
                message = decode(line)
            except JsonRpcError as exc:
                self.wfile.write(encode(error_message(None, exc.code, exc.detail)))
                self.wfile.flush()
                continue
            try:
                request = parse_request(message)
            except JsonRpcError:
                continue
            try:
                result = broker.dispatch(request.method, request.params)
            except JsonRpcError as exc:
                if not request.is_notification:
                    self.wfile.write(
                        encode(
                            error_message(request.identifier, exc.code, exc.detail)
                        )
                    )
                    self.wfile.flush()
                continue
            if not request.is_notification:
                self.wfile.write(encode(result_message(request.identifier, result)))
                self.wfile.flush()

    def handle_error(self, request: Any, client_address: Any) -> None:
        return None


class ToolBrokerServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, session: AgentSession, host: str = "127.0.0.1") -> None:
        super().__init__((host, 0), _Handler)
        self._session = session
        self._finish = FinishRecord()
        self._lock = threading.Lock()
        self._exhausted = False
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return self.server_address[0]

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def finish(self) -> FinishRecord:
        return self._finish

    @property
    def budget_exhausted(self) -> bool:
        with self._lock:
            return self._exhausted

    def start(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _listing(self) -> dict[str, Any]:
        tools = [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": {
                    "type": "object",
                    "properties": dict(spec.input_schema),
                },
            }
            for spec in (
                self._session.tool(name) for name in self._session.tool_names()
            )
        ]
        tools.append(_finish_spec(self._session))
        return {"tools": tools}

    def _deliver(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        raw = arguments.get(_FINDINGS_KEY)
        if not isinstance(raw, list):
            raise JsonRpcError(
                ErrorCode.INVALID_PARAMS, f"{_FINDINGS_KEY} must be an array"
            )
        findings: list[Mapping[str, Any]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise JsonRpcError(
                    ErrorCode.INVALID_PARAMS, "each finding must be an object"
                )
            findings.append(dict(item))
        self._finish.deliver(tuple(findings))
        return {"content": [{"type": "text", "text": "accepted"}], "isError": False}

    def _invoke(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        call = ToolCall(name=name, arguments=dict(arguments))
        try:
            result = self._session.invoke(call)
        except BudgetExhausted as exc:
            with self._lock:
                self._exhausted = True
            raise JsonRpcError(ErrorCode.INTERNAL_ERROR, str(exc)) from exc
        except (ProtocolError, SessionError) as exc:
            raise JsonRpcError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        return {
            "content": [{"type": "text", "text": ""}],
            "isError": not result.ok,
            "structuredContent": dict(result.payload) if result.ok else {},
            "error": result.error,
        }

    def dispatch(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == _TOOLS_LIST:
            return self._listing()
        if method != _TOOLS_CALL:
            raise JsonRpcError(ErrorCode.METHOD_NOT_FOUND, f"unknown method: {method}")
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise JsonRpcError(ErrorCode.INVALID_PARAMS, "tool call without a name")
        raw = params.get("arguments")
        arguments = dict(raw) if isinstance(raw, Mapping) else {}
        if name == FINISH_TOOL:
            return self._deliver(arguments)
        return self._invoke(name, arguments)
