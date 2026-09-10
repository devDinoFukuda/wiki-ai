from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiki_ai.agent.envelope import EnvelopeIdentity, seal
from wiki_ai.investigation import state as state_module
from wiki_ai.investigation.state import (
    CorruptStore,
    Snapshot,
    Store,
    StoreLocked,
    UnknownTask,
    decode,
    encode,
)
from wiki_ai.investigation.tasks import Lease, Task, TaskState


def sample() -> Snapshot:
    task = Task("t1", "obj", "snap", TaskState.LEASED, attempt=2, reason="despachada")
    lease = Lease("t1", "w1", 100.0, 130.0, "tok-1")
    envelope = seal(EnvelopeIdentity("t1", "snap", "obj", "in"), payload={"n": 1}, produced_at=5.0)
    return Snapshot().with_task(task).with_lease(lease).with_envelope(envelope)


def test_snapshot_updates_are_copies():
    empty = Snapshot()
    filled = empty.with_task(Task("t1", "obj", "snap"))
    assert empty.tasks == {}
    assert filled.task("t1").task_id == "t1"


def test_unknown_task_is_typed():
    with pytest.raises(UnknownTask):
        Snapshot().task("t9")


def test_lease_can_be_dropped_without_touching_the_task():
    snapshot = sample().without_lease("t1")
    assert snapshot.lease("t1") is None
    assert snapshot.task("t1").state is TaskState.LEASED


def test_round_trip_preserves_every_value(tmp_path: Path):
    store = Store(tmp_path / "runtime.json")
    store.save(sample())
    loaded = store.load()
    assert loaded == sample()
    assert loaded.envelope("t1").payload_hash == sample().envelope("t1").payload_hash


def test_missing_file_loads_as_empty_state(tmp_path: Path):
    assert Store(tmp_path / "absent.json").load() == Snapshot()


def test_sections_are_prefixed_by_record_kind(tmp_path: Path):
    path = tmp_path / "runtime.json"
    Store(path).save(sample())
    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document) == {"version", "task_records", "lease_records", "envelope_records"}


def test_unreadable_content_is_reported_not_swallowed(tmp_path: Path):
    path = tmp_path / "runtime.json"
    path.write_text("{isto não é json", encoding="utf-8")
    with pytest.raises(CorruptStore):
        Store(path).load()


def test_unknown_version_is_refused(tmp_path: Path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    with pytest.raises(CorruptStore):
        Store(path).load()


def test_missing_section_is_refused(tmp_path: Path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"version": 1, "task_records": []}), encoding="utf-8")
    with pytest.raises(CorruptStore):
        Store(path).load()


def test_decode_of_encode_is_identity():
    assert decode(encode(sample()), Path("x")) == sample()


def test_failure_during_publication_keeps_the_previous_state(tmp_path, monkeypatch):
    path = tmp_path / "runtime.json"
    store = Store(path)
    store.save(sample())
    before = path.read_text(encoding="utf-8")

    def explode(src: str, dst: str) -> None:
        raise OSError("disco cheio")

    monkeypatch.setattr(state_module.os, "replace", explode)
    with pytest.raises(OSError):
        store.save(sample().with_task(Task("t2", "obj", "snap")))
    assert path.read_text(encoding="utf-8") == before
    assert store.load() == sample()


def test_failure_during_publication_leaves_no_partial_file(tmp_path, monkeypatch):
    path = tmp_path / "runtime.json"
    store = Store(path)
    store.save(sample())

    def explode(src: str, dst: str) -> None:
        raise OSError("disco cheio")

    monkeypatch.setattr(state_module.os, "replace", explode)
    with pytest.raises(OSError):
        store.save(sample())
    assert [p.name for p in tmp_path.iterdir()] == ["runtime.json"]


def test_mutate_reads_changes_and_writes_under_one_lock(tmp_path: Path):
    store = Store(tmp_path / "runtime.json")
    store.save(sample())
    updated = store.mutate(lambda snap: snap.without_lease("t1"))
    assert updated.lease("t1") is None
    assert store.load().lease("t1") is None
    assert not (tmp_path / "runtime.json.lock").exists()


def test_lock_is_released_even_when_the_change_raises(tmp_path: Path):
    store = Store(tmp_path / "runtime.json")

    def broken(snapshot: Snapshot) -> Snapshot:
        raise ValueError("mudança inválida")

    with pytest.raises(ValueError):
        store.mutate(broken)
    assert not (tmp_path / "runtime.json.lock").exists()


def test_concurrent_holder_of_the_lock_is_refused_after_the_timeout(tmp_path: Path):
    path = tmp_path / "runtime.json"
    path.with_name("runtime.json.lock").write_text("", encoding="utf-8")
    store = Store(path, lock_timeout_s=0.05)
    with pytest.raises(StoreLocked):
        store.mutate(lambda snap: snap)
