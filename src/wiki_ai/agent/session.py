from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from wiki_ai.agent.protocol import (
    AgentCapabilities,
    ProtocolError,
    ToolCall,
    ToolResult,
)

__all__ = [
    "SessionError",
    "BudgetInvalid",
    "BudgetExhausted",
    "SessionAlreadyFinished",
    "ToolSpec",
    "Budget",
    "BudgetUsage",
    "TranscriptEntry",
    "RunStatus",
    "AgentRun",
    "ToolExecutor",
    "FINDING_SCHEMA",
    "AgentSession",
    "AgentProvider",
]

ToolExecutor = Callable[[ToolCall], ToolResult]

FINDING_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["claim", "evidence"],
    "additionalProperties": False,
    "properties": {
        "claim": {"type": "string"},
        "confidence": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source_id", "version", "locator"],
                "additionalProperties": False,
                "properties": {
                    "source_id": {"type": "string"},
                    "version": {"type": "string"},
                    "locator": {"type": "string"},
                },
            },
        },
    },
}


class SessionError(Exception):
    pass


class BudgetInvalid(SessionError):
    pass


class BudgetExhausted(SessionError):
    def __init__(self, limit: str, allowed: float, used: float) -> None:
        super().__init__(f"budget {limit} exhausted: used {used} of {allowed}")
        self.limit = limit
        self.allowed = allowed
        self.used = used


class SessionAlreadyFinished(SessionError):
    def __init__(self) -> None:
        super().__init__("session already produced a run")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise SessionError("tool spec without a name")
        if not self.description.strip():
            raise SessionError(f"tool spec {self.name!r} without a description")
        object.__setattr__(self, "input_schema", dict(self.input_schema))
        object.__setattr__(self, "output_schema", dict(self.output_schema))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
        }


@dataclass(frozen=True)
class Budget:
    max_tool_calls: int
    max_tokens: int | None = None
    max_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_tool_calls <= 0:
            raise BudgetInvalid(f"max_tool_calls must be positive: {self.max_tool_calls!r}")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise BudgetInvalid(f"max_tokens must be positive: {self.max_tokens!r}")
        if self.max_seconds <= 0:
            raise BudgetInvalid(f"max_seconds must be positive: {self.max_seconds!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
        }


@dataclass(frozen=True)
class BudgetUsage:
    tool_calls: int
    tokens: int
    seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "seconds": round(self.seconds, 6),
        }


@dataclass(frozen=True)
class TranscriptEntry:
    ordinal: int
    call: ToolCall
    result: ToolResult
    tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "tool": self.call.name,
            "arguments": dict(self.call.arguments),
            "call_id": self.result.call_id,
            "ok": self.result.ok,
            "error": self.result.error,
            "tokens": self.tokens,
        }


class RunStatus(Enum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AgentRun:
    status: RunStatus
    findings: tuple[Mapping[str, Any], ...]
    transcript: tuple[TranscriptEntry, ...]
    usage: BudgetUsage
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status is RunStatus.COMPLETED and self.reason:
            raise SessionError("a completed run must not carry a reason")
        if self.status is not RunStatus.COMPLETED and not self.reason.strip():
            raise SessionError(f"run {self.status.value} without a reason")
        if self.status is not RunStatus.COMPLETED and self.findings:
            raise SessionError(f"run {self.status.value} must not carry findings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "findings": [dict(finding) for finding in self.findings],
            "transcript": [entry.to_dict() for entry in self.transcript],
            "usage": self.usage.to_dict(),
        }


class AgentSession:
    def __init__(
        self,
        objective: str,
        tools: Mapping[str, ToolSpec],
        budget: Budget,
        snapshot_id: str,
        executor: ToolExecutor | None = None,
        started_at: float | None = None,
        finding_schema: Mapping[str, Any] | None = None,
    ) -> None:
        if not objective.strip():
            raise SessionError("session objective is empty")
        if not snapshot_id.strip():
            raise SessionError("session without a snapshot identifier")
        for name, spec in tools.items():
            if name != spec.name:
                raise SessionError(f"tool registered as {name!r} declares name {spec.name!r}")
        self._objective = objective
        self._tools: dict[str, ToolSpec] = dict(tools)
        self._budget = budget
        self._snapshot_id = snapshot_id
        self._started_at = time.monotonic() if started_at is None else started_at
        self._transcript: list[TranscriptEntry] = []
        self._tokens = 0
        self._executor = executor
        self._finding_schema: Mapping[str, Any] = dict(
            FINDING_SCHEMA if finding_schema is None else finding_schema
        )
        self._run: AgentRun | None = None

    @property
    def objective(self) -> str:
        return self._objective

    @property
    def snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def budget(self) -> Budget:
        return self._budget

    @property
    def tools(self) -> Mapping[str, ToolSpec]:
        return dict(self._tools)

    def tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def tool(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise SessionError(f"tool not offered to this session: {name}") from exc

    @property
    def transcript(self) -> Sequence[TranscriptEntry]:
        return tuple(self._transcript)

    @property
    def finding_schema(self) -> Mapping[str, Any]:
        return dict(self._finding_schema)

    @property
    def run(self) -> AgentRun | None:
        return self._run

    def elapsed(self, now: float | None = None) -> float:
        moment = time.monotonic() if now is None else now
        return max(0.0, moment - self._started_at)

    def usage(self, now: float | None = None) -> BudgetUsage:
        return BudgetUsage(
            tool_calls=len(self._transcript),
            tokens=self._tokens,
            seconds=self.elapsed(now),
        )

    def remaining_tool_calls(self) -> int:
        return max(0, self._budget.max_tool_calls - len(self._transcript))

    def exhausted(self, now: float | None = None) -> str | None:
        if len(self._transcript) >= self._budget.max_tool_calls:
            return "max_tool_calls"
        if self._budget.max_tokens is not None and self._tokens >= self._budget.max_tokens:
            return "max_tokens"
        if self.elapsed(now) >= self._budget.max_seconds:
            return "max_seconds"
        return None

    def ensure_budget(self, now: float | None = None) -> None:
        limit = self.exhausted(now)
        if limit is None:
            return
        usage = self.usage(now)
        if limit == "max_tool_calls":
            raise BudgetExhausted(limit, self._budget.max_tool_calls, usage.tool_calls)
        if limit == "max_tokens":
            raise BudgetExhausted(limit, float(self._budget.max_tokens or 0), usage.tokens)
        raise BudgetExhausted(limit, self._budget.max_seconds, usage.seconds)

    def record(
        self,
        call: ToolCall,
        result: ToolResult,
        tokens: int = 0,
        now: float | None = None,
    ) -> TranscriptEntry:
        if tokens < 0:
            raise SessionError(f"token count must not be negative: {tokens!r}")
        if call.name not in self._tools:
            raise ProtocolError(f"tool not offered to this session: {call.name}")
        self.ensure_budget(now)
        entry = TranscriptEntry(
            ordinal=len(self._transcript),
            call=call,
            result=result,
            tokens=tokens,
        )
        self._transcript.append(entry)
        self._tokens += tokens
        return entry

    def invoke(
        self,
        call: ToolCall,
        tokens: int = 0,
        now: float | None = None,
    ) -> ToolResult:
        if self._run is not None:
            raise SessionAlreadyFinished()
        if self._executor is None:
            raise SessionError("session has no tool executor")
        if call.name not in self._tools:
            raise ProtocolError(f"tool not offered to this session: {call.name}")
        self.ensure_budget(now)
        result = self._executor(call)
        self.record(call, result, tokens=tokens, now=now)
        return result

    def _seal(
        self,
        status: RunStatus,
        findings: Sequence[Mapping[str, Any]],
        reason: str,
        now: float | None,
    ) -> AgentRun:
        if self._run is not None:
            raise SessionAlreadyFinished()
        run = AgentRun(
            status=status,
            findings=tuple(dict(finding) for finding in findings),
            transcript=tuple(self._transcript),
            usage=self.usage(now),
            reason=reason,
        )
        self._run = run
        return run

    def finish(
        self,
        findings: Sequence[Mapping[str, Any]],
        now: float | None = None,
    ) -> AgentRun:
        return self._seal(RunStatus.COMPLETED, findings, "", now)

    def fail(self, reason: str, now: float | None = None) -> AgentRun:
        return self._seal(RunStatus.FAILED, (), reason, now)

    def cancelled(self, reason: str, now: float | None = None) -> AgentRun:
        return self._seal(RunStatus.CANCELLED, (), reason, now)

    def budget_exhausted(self, reason: str, now: float | None = None) -> AgentRun:
        return self._seal(RunStatus.BUDGET_EXHAUSTED, (), reason, now)

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        return {
            "objective": self._objective,
            "snapshot_id": self._snapshot_id,
            "tools": [self._tools[name].to_dict() for name in self.tool_names()],
            "budget": self._budget.to_dict(),
            "usage": self.usage(now).to_dict(),
            "transcript": [entry.to_dict() for entry in self._transcript],
        }


@runtime_checkable
class AgentProvider(Protocol):
    def connect(self) -> None: ...

    def capabilities(self) -> AgentCapabilities: ...

    def run(self, session: AgentSession) -> AgentRun: ...

    def cancel(self) -> None: ...
