from __future__ import annotations

import argparse
import socket
import sys
from typing import Any, BinaryIO, Mapping

from wiki_ai.agent.providers.jsonrpc import (
    ErrorCode,
    JsonRpcError,
    PROTOCOL_VERSION,
    decode,
    encode,
    error_message,
    read_message,
    result_message,
    write_message,
)

__all__ = [
    "SERVER_NAME",
    "FINISH_TOOL",
    "BrokerClient",
    "serve",
    "main",
]

SERVER_NAME = "wiki"
FINISH_TOOL = "wiki.finish"
_INITIALIZE = "initialize"
_TOOLS_LIST = "tools/list"
_TOOLS_CALL = "tools/call"
_SHUTDOWN = "shutdown"


class BrokerClient:
    def __init__(self, host: str, port: int, timeout: float = 60.0) -> None:
        self._address = (host, port)
        self._timeout = timeout
        self._socket: socket.socket | None = None
        self._stream: BinaryIO | None = None
        self._counter = 0

    def connect(self) -> None:
        if self._socket is not None:
            return
        connection = socket.create_connection(self._address, timeout=self._timeout)
        self._socket = connection
        self._stream = connection.makefile("rwb")

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self.connect()
        stream = self._stream
        if stream is None:
            raise JsonRpcError(ErrorCode.INTERNAL_ERROR, "broker stream is closed")
        self._counter += 1
        stream.write(
            encode(
                {
                    "jsonrpc": "2.0",
                    "id": self._counter,
                    "method": method,
                    "params": dict(params),
                }
            )
        )
        stream.flush()
        line = stream.readline()
        if not line:
            raise JsonRpcError(ErrorCode.INTERNAL_ERROR, "broker closed the connection")
        payload = decode(line)
        failure = payload.get("error")
        if isinstance(failure, Mapping):
            raise JsonRpcError(ErrorCode.INTERNAL_ERROR, str(failure.get("message", "")))
        result = payload.get("result")
        return dict(result) if isinstance(result, Mapping) else {}


def _tools(client: BrokerClient) -> list[dict[str, Any]]:
    listing = client.request(_TOOLS_LIST, {})
    found = listing.get("tools")
    return [dict(item) for item in found] if isinstance(found, list) else []


def _call(client: BrokerClient, params: Mapping[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, "tool call without a name")
    arguments = params.get("arguments")
    payload = dict(arguments) if isinstance(arguments, Mapping) else {}
    return client.request(_TOOLS_CALL, {"name": name, "arguments": payload})


def _dispatch(
    client: BrokerClient, method: str, params: Mapping[str, Any]
) -> dict[str, Any]:
    if method == _INITIALIZE:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        }
    if method == _TOOLS_LIST:
        return {"tools": _tools(client)}
    if method == _TOOLS_CALL:
        return _call(client, params)
    if method == _SHUTDOWN:
        return {}
    raise JsonRpcError(ErrorCode.METHOD_NOT_FOUND, f"unknown method: {method}")


def serve(client: BrokerClient, source: BinaryIO, sink: BinaryIO) -> None:
    while True:
        try:
            message = read_message(source)
        except JsonRpcError as exc:
            write_message(sink, error_message(None, exc.code, exc.detail))
            continue
        if message is None:
            return
        identifier = message.get("id")
        method = message.get("method")
        raw_params = message.get("params")
        params = dict(raw_params) if isinstance(raw_params, Mapping) else {}
        if not isinstance(method, str):
            if identifier is not None:
                write_message(
                    sink,
                    error_message(
                        identifier, ErrorCode.INVALID_REQUEST, "message without a method"
                    ),
                )
            continue
        try:
            result = _dispatch(client, method, params)
        except JsonRpcError as exc:
            if identifier is not None:
                write_message(sink, error_message(identifier, exc.code, exc.detail))
            continue
        except OSError as exc:
            if identifier is not None:
                write_message(
                    sink, error_message(identifier, ErrorCode.INTERNAL_ERROR, str(exc))
                )
            continue
        if identifier is not None:
            write_message(sink, result_message(identifier, result))
        if method == _SHUTDOWN:
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    options = parser.parse_args(argv)
    client = BrokerClient(options.host, options.port, options.timeout)
    try:
        client.connect()
    except OSError:
        return 1
    try:
        serve(client, sys.stdin.buffer, sys.stdout.buffer)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
