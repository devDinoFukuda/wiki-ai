from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum

from wiki_ai.agent.envelope import StaleResult
from wiki_ai.investigation.tasks import (
    Task,
    TaskState,
    reset_attempts,
    transition,
)

_VOLATILE = (
    re.compile(r"\b[0-9a-f]{8,}\b"),
    re.compile(r"\b\d{2,}\b"),
    re.compile(r"0x[0-9a-f]+"),
)


class RecoveryError(Exception):
    pass


class InvalidPolicy(RecoveryError):
    pass


class InvalidAttempt(RecoveryError):
    def __init__(self, attempt: int) -> None:
        super().__init__(f"attempt deve ser >= 1, recebido {attempt!r}")
        self.attempt = attempt


class FailureKind(Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    STALE = "stale"


def normalize_detail(detail: str) -> str:
    text = " ".join(str(detail).split()).lower()
    for pattern in _VOLATILE:
        text = pattern.sub("~", text)
    return text.strip()


@dataclass(frozen=True)
class Failure:
    kind: FailureKind
    code: str
    detail: str = ""

    @property
    def fingerprint(self) -> str:
        raw = f"{self.kind.value}\n{self.code}\n{normalize_detail(self.detail)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def transient(code: str, detail: str = "") -> Failure:
    return Failure(FailureKind.TRANSIENT, code, detail)


def permanent(code: str, detail: str = "") -> Failure:
    return Failure(FailureKind.PERMANENT, code, detail)


def stale(code: str, detail: str = "") -> Failure:
    return Failure(FailureKind.STALE, code, detail)


def from_stale_result(error: StaleResult) -> Failure:
    return Failure(FailureKind.STALE, error.reason.value, error.envelope.envelope_id)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise InvalidPolicy(f"max_attempts deve ser >= 1, recebido {self.max_attempts!r}")
        if any(d < 0 for d in self.backoff):
            raise InvalidPolicy(f"backoff não admite espera negativa: {self.backoff!r}")


class Outcome(Enum):
    RETRY = "retry"
    GIVE_UP = "give_up"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    delay: float
    reason: str

    @property
    def retry(self) -> bool:
        return self.outcome is Outcome.RETRY


def _delay(policy: RetryPolicy, attempt: int) -> float:
    if not policy.backoff:
        return 0.0
    return float(policy.backoff[min(attempt - 1, len(policy.backoff) - 1)])


def next_attempt(policy: RetryPolicy, attempt: int) -> Decision:
    if attempt < 1:
        raise InvalidAttempt(attempt)
    if attempt >= policy.max_attempts:
        return Decision(
            Outcome.GIVE_UP,
            0.0,
            f"tentativas esgotadas: {attempt} de {policy.max_attempts}",
        )
    return Decision(
        Outcome.RETRY,
        _delay(policy, attempt),
        f"tentativa {attempt + 1} de {policy.max_attempts}",
    )


@dataclass(frozen=True)
class Recovery:
    task: Task
    decision: Decision
    failure: Failure


def recover(task: Task, failure: Failure, policy: RetryPolicy) -> Recovery:
    if failure.kind is FailureKind.STALE:
        decision = Decision(Outcome.RETRY, 0.0, f"resultado obsoleto: {failure.code}")
        replanned = transition(task, TaskState.PENDING, decision.reason)
        return Recovery(reset_attempts(replanned), decision, failure)
    if failure.kind is FailureKind.PERMANENT:
        decision = Decision(Outcome.GIVE_UP, 0.0, f"falha permanente: {failure.code}")
        return Recovery(transition(task, TaskState.FAILED, decision.reason), decision, failure)
    decision = next_attempt(policy, max(1, task.attempt))
    target = TaskState.PENDING if decision.retry else TaskState.FAILED
    return Recovery(transition(task, target, decision.reason), decision, failure)
