from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "ProtocolError",
    "ToolCall",
    "ToolResult",
    "AgentCapabilities",
]


class ProtocolError(Exception):
    pass


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ProtocolError("chamada de ferramenta sem nome")


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    ok: bool
    payload: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ProtocolError("resultado de ferramenta sem call_id")
        if self.ok and self.error:
            raise ProtocolError(f"resultado {self.call_id!r} é ok e traz erro {self.error!r}")
        if not self.ok and not self.error:
            raise ProtocolError(f"resultado {self.call_id!r} falhou sem erro declarado")


@dataclass(frozen=True)
class AgentCapabilities:
    tools: tuple[str, ...] = ()
    max_context_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_context_tokens is not None and self.max_context_tokens <= 0:
            raise ProtocolError(f"max_context_tokens inválido: {self.max_context_tokens!r}")

    def supports(self, tool: str) -> bool:
        return tool in self.tools

    def require(self, *tools: str) -> None:
        missing = tuple(t for t in tools if t not in self.tools)
        if missing:
            raise ProtocolError(f"ferramentas ausentes no agente: {sorted(missing)}")
