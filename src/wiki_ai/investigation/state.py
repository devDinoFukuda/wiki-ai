from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from wiki_ai.agent.envelope import ResultEnvelope
from wiki_ai.investigation.tasks import Lease, Task, TaskState

STORE_VERSION = 1
TASK_SECTION = "task_records"
LEASE_SECTION = "lease_records"
ENVELOPE_SECTION = "envelope_records"
LOCK_TIMEOUT_S = 10.0


class StoreError(Exception):
    pass


class UnknownTask(StoreError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"tarefa {task_id!r} não existe no estado persistido")
        self.task_id = task_id


class CorruptStore(StoreError):
    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"estado em {str(path)!r} ilegível: {detail}")
        self.path = path
        self.detail = detail


class StoreLocked(StoreError):
    def __init__(self, path: Path, timeout_s: float) -> None:
        super().__init__(f"trava de {str(path)!r} não liberada em {timeout_s}s")
        self.path = path
        self.timeout_s = timeout_s


@dataclass(frozen=True)
class Snapshot:
    tasks: Mapping[str, Task] = field(default_factory=dict)
    leases: Mapping[str, Lease] = field(default_factory=dict)
    envelopes: Mapping[str, ResultEnvelope] = field(default_factory=dict)

    def task(self, task_id: str) -> Task:
        found = self.tasks.get(task_id)
        if found is None:
            raise UnknownTask(task_id)
        return found

    def lease(self, task_id: str) -> Lease | None:
        return self.leases.get(task_id)

    def envelope(self, task_id: str) -> ResultEnvelope | None:
        return self.envelopes.get(task_id)

    def with_task(self, task: Task) -> "Snapshot":
        return replace(self, tasks={**self.tasks, task.task_id: task})

    def with_lease(self, lease: Lease) -> "Snapshot":
        return replace(self, leases={**self.leases, lease.task_id: lease})

    def without_lease(self, task_id: str) -> "Snapshot":
        return replace(self, leases={k: v for k, v in self.leases.items() if k != task_id})

    def with_envelope(self, envelope: ResultEnvelope) -> "Snapshot":
        return replace(
            self, envelopes={**self.envelopes, envelope.task_id: envelope}
        )


def _task_json(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "objective_hash": task.objective_hash,
        "snapshot_hash": task.snapshot_hash,
        "state": task.state.value,
        "attempt": task.attempt,
        "result_hash": task.result_hash,
        "reason": task.reason,
    }


def _task_of(data: Mapping[str, Any]) -> Task:
    return Task(
        task_id=str(data["task_id"]),
        objective_hash=str(data["objective_hash"]),
        snapshot_hash=str(data["snapshot_hash"]),
        state=TaskState(data["state"]),
        attempt=int(data["attempt"]),
        result_hash=str(data["result_hash"]),
        reason=str(data["reason"]),
    )


def _lease_json(lease: Lease) -> dict[str, Any]:
    return {
        "task_id": lease.task_id,
        "owner": lease.owner,
        "acquired_at": lease.acquired_at,
        "expires_at": lease.expires_at,
        "token": lease.token,
    }


def _lease_of(data: Mapping[str, Any]) -> Lease:
    return Lease(
        task_id=str(data["task_id"]),
        owner=str(data["owner"]),
        acquired_at=float(data["acquired_at"]),
        expires_at=float(data["expires_at"]),
        token=str(data["token"]),
    )


def _envelope_json(envelope: ResultEnvelope) -> dict[str, Any]:
    return {
        "envelope_id": envelope.envelope_id,
        "task_id": envelope.task_id,
        "produced_for_snapshot": envelope.produced_for_snapshot,
        "payload_hash": envelope.payload_hash,
        "produced_at": envelope.produced_at,
    }


def _envelope_of(data: Mapping[str, Any]) -> ResultEnvelope:
    return ResultEnvelope(
        envelope_id=str(data["envelope_id"]),
        task_id=str(data["task_id"]),
        produced_for_snapshot=str(data["produced_for_snapshot"]),
        payload_hash=str(data["payload_hash"]),
        produced_at=float(data["produced_at"]),
    )


def encode(snapshot: Snapshot) -> str:
    document = {
        "version": STORE_VERSION,
        TASK_SECTION: [_task_json(t) for t in snapshot.tasks.values()],
        LEASE_SECTION: [_lease_json(x) for x in snapshot.leases.values()],
        ENVELOPE_SECTION: [_envelope_json(e) for e in snapshot.envelopes.values()],
    }
    return json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2)


def decode(raw: str, path: Path) -> Snapshot:
    try:
        document = json.loads(raw)
        version = int(document["version"])
        if version != STORE_VERSION:
            raise CorruptStore(path, f"versão {version} não é {STORE_VERSION}")
        tasks = {t.task_id: t for t in (_task_of(d) for d in document[TASK_SECTION])}
        leases = {x.task_id: x for x in (_lease_of(d) for d in document[LEASE_SECTION])}
        envelopes = {
            e.task_id: e for e in (_envelope_of(d) for d in document[ENVELOPE_SECTION])
        }
    except CorruptStore:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptStore(path, f"{type(exc).__name__}: {exc}") from exc
    return Snapshot(tasks=tasks, leases=leases, envelopes=envelopes)


@contextlib.contextmanager
def _locked(path: Path, timeout_s: float) -> Iterator[None]:
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise StoreLocked(path, timeout_s) from None
            time.sleep(0.01)
    try:
        os.close(handle)
        yield
    finally:
        with contextlib.suppress(OSError):
            os.unlink(str(lock))


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise


class Store:
    def __init__(self, path: str | os.PathLike[str], *, lock_timeout_s: float = LOCK_TIMEOUT_S):
        self.path = Path(path)
        self.lock_timeout_s = lock_timeout_s

    def load(self) -> Snapshot:
        if not self.path.exists():
            return Snapshot()
        return decode(self.path.read_text(encoding="utf-8"), self.path)

    def save(self, snapshot: Snapshot) -> Snapshot:
        _write_atomic(self.path, encode(snapshot))
        return snapshot

    def mutate(self, change: Callable[[Snapshot], Snapshot]) -> Snapshot:
        with _locked(self.path, self.lock_timeout_s):
            return self.save(change(self.load()))
