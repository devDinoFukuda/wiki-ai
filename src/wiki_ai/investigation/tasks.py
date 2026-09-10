from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Mapping

Clock = Callable[[], float]


class TaskState(Enum):
    PENDING = "pending"
    READY = "ready"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.BLOCKED}
)

ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.READY, TaskState.BLOCKED}),
    TaskState.READY: frozenset({TaskState.PENDING, TaskState.LEASED, TaskState.BLOCKED}),
    TaskState.LEASED: frozenset(
        {
            TaskState.READY,
            TaskState.PENDING,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.BLOCKED,
        }
    ),
    TaskState.SUCCEEDED: frozenset({TaskState.PENDING}),
    TaskState.FAILED: frozenset({TaskState.PENDING}),
    TaskState.BLOCKED: frozenset({TaskState.PENDING}),
}


class TaskError(Exception):
    pass


class IllegalTransition(TaskError):
    def __init__(self, task_id: str, source: TaskState, target: TaskState, reason: str) -> None:
        super().__init__(
            f"tarefa {task_id!r}: {source.value} -> {target.value} não é transição "
            f"permitida (motivo declarado: {reason!r})"
        )
        self.task_id = task_id
        self.source = source
        self.target = target
        self.reason = reason


class MissingResult(TaskError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"tarefa {task_id!r} não conclui sem result_hash")
        self.task_id = task_id


class LeaseError(TaskError):
    pass


class LeaseHeld(LeaseError):
    def __init__(self, task_id: str, owner: str, expires_at: float) -> None:
        super().__init__(f"tarefa {task_id!r} já é de {owner!r} até {expires_at}")
        self.task_id = task_id
        self.owner = owner
        self.expires_at = expires_at


class LeaseExpired(LeaseError):
    def __init__(self, task_id: str, token: str, expires_at: float) -> None:
        super().__init__(f"posse {token[:12]!r} de {task_id!r} venceu em {expires_at}")
        self.task_id = task_id
        self.token = token
        self.expires_at = expires_at


class LeaseNotHeld(LeaseError):
    def __init__(self, task_id: str, token: str) -> None:
        super().__init__(f"token {token[:12]!r} não é a posse vigente de {task_id!r}")
        self.task_id = task_id
        self.token = token


@dataclass(frozen=True)
class Task:
    task_id: str
    objective_hash: str
    snapshot_hash: str
    state: TaskState = TaskState.PENDING
    attempt: int = 0
    result_hash: str = ""
    reason: str = ""


def transition(task: Task, to: TaskState, reason: str, *, result_hash: str = "") -> Task:
    if to not in ALLOWED_TRANSITIONS[task.state]:
        raise IllegalTransition(task.task_id, task.state, to, reason)
    sealed = result_hash or task.result_hash
    if to is TaskState.SUCCEEDED and not sealed:
        raise MissingResult(task.task_id)
    return replace(
        task,
        state=to,
        reason=reason,
        result_hash="" if to is TaskState.PENDING else sealed,
        attempt=task.attempt + 1 if to is TaskState.LEASED else task.attempt,
    )


def reset_attempts(task: Task) -> Task:
    return replace(task, attempt=0)


@dataclass(frozen=True)
class Lease:
    task_id: str
    owner: str
    acquired_at: float
    expires_at: float
    token: str

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def remaining(self, now: float) -> float:
        return max(0.0, self.expires_at - now)


def new_token() -> str:
    return uuid.uuid4().hex


def acquire(
    task_id: str,
    owner: str,
    *,
    ttl_seconds: float,
    clock: Clock,
    current: Lease | None = None,
    token: str | None = None,
) -> Lease:
    if ttl_seconds <= 0:
        raise LeaseError(f"ttl_seconds deve ser positivo, recebido {ttl_seconds!r}")
    now = clock()
    if current is not None and not current.is_expired(now):
        raise LeaseHeld(current.task_id, current.owner, current.expires_at)
    return Lease(task_id, owner, now, now + ttl_seconds, token or new_token())


def renew(lease: Lease, token: str, *, ttl_seconds: float, clock: Clock) -> Lease:
    if ttl_seconds <= 0:
        raise LeaseError(f"ttl_seconds deve ser positivo, recebido {ttl_seconds!r}")
    if token != lease.token:
        raise LeaseNotHeld(lease.task_id, token)
    now = clock()
    if lease.is_expired(now):
        raise LeaseExpired(lease.task_id, token, lease.expires_at)
    return replace(lease, expires_at=now + ttl_seconds)


def release(lease: Lease, token: str, *, clock: Clock) -> Lease:
    if token != lease.token:
        raise LeaseNotHeld(lease.task_id, token)
    now = clock()
    if lease.is_expired(now):
        return lease
    return replace(lease, expires_at=now)


def holds(lease: Lease | None, token: str, now: float) -> bool:
    return lease is not None and lease.token == token and not lease.is_expired(now)
