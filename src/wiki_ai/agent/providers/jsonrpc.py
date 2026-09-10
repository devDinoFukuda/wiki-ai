from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, BinaryIO, Mapping

__all__ = [
    "JsonRpcError",
    "ErrorCode",
    "PROTOCOL_VERSION",
    "Request",
    "encode",
    "decode",
    "read_message",
    "write_message",
    "result_message",
    "error_message",
]

PROTOCOL_VERSION = "2024-11-05"
_VERSION = "2.0"
_ENCODING = "utf-8"


class ErrorCode(Enum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


class JsonRpcError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.detail = message


@dataclass(frozen=True)
class Request:
    method: str
    params: Mapping[str, Any]
    identifier: Any = None

    @property
    def is_notification(self) -> bool:
        return self.identifier is None


def encode(payload: Mapping[str, Any]) -> bytes:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return line.encode(_ENCODING) + b"\n"


def decode(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode(_ENCODING))
    except (ValueError, UnicodeDecodeError) as exc:
        raise JsonRpcError(ErrorCode.PARSE_ERROR, str(exc)) from exc
    if not isinstance(payload, dict):
        raise JsonRpcError(ErrorCode.INVALID_REQUEST, "message is not an object")
    return payload


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    line = stream.readline()
    while line in (b"\n", b"\r\n"):
        line = stream.readline()
    if not line:
        return None
    return decode(line)


def write_message(stream: BinaryIO, payload: Mapping[str, Any]) -> None:
    stream.write(encode(payload))
    stream.flush()


def result_message(identifier: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": _VERSION, "id": identifier, "result": dict(result)}


def error_message(identifier: Any, code: ErrorCode, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": _VERSION,
        "id": identifier,
        "error": {"code": code.value, "message": message},
    }
