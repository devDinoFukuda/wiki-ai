from __future__ import annotations

import pytest

from wiki_ai.agent.envelope import (
    EnvelopeIdentity,
    StaleReason,
    StaleResult,
    content_hash,
    ensure_fresh,
    envelope_id,
    is_duplicate,
    is_stale,
    seal,
    staleness,
)


def identity(**overrides: str) -> EnvelopeIdentity:
    base = {
        "task_id": "t1",
        "snapshot_hash": "snap-a",
        "objective_hash": "obj-a",
        "input_hash": "in-a",
    }
    base.update(overrides)
    return EnvelopeIdentity(**base)


def test_content_hash_ignores_key_order():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


def test_envelope_id_is_content_addressed():
    assert envelope_id(identity()) == envelope_id(identity())


def test_envelope_id_changes_with_every_component():
    base = envelope_id(identity())
    assert envelope_id(identity(task_id="t2")) != base
    assert envelope_id(identity(snapshot_hash="snap-b")) != base
    assert envelope_id(identity(objective_hash="obj-b")) != base
    assert envelope_id(identity(input_hash="in-b")) != base


def test_seal_copies_identity_from_the_task_not_from_the_payload():
    envelope = seal(identity(), payload={"claims": []}, produced_at=12.0)
    assert envelope.task_id == "t1"
    assert envelope.produced_for_snapshot == "snap-a"
    assert envelope.envelope_id == envelope_id(identity())
    assert envelope.payload_hash == content_hash({"claims": []})


def test_is_stale_compares_snapshot_only():
    envelope = seal(identity(), payload={}, produced_at=1.0)
    assert not is_stale(envelope, "snap-a")
    assert is_stale(envelope, "snap-b")


def test_fresh_envelope_passes_the_gate():
    envelope = seal(identity(), payload={}, produced_at=1.0)
    assert ensure_fresh(envelope, identity()) is envelope


def test_snapshot_change_is_detected_as_stale():
    envelope = seal(identity(), payload={}, produced_at=1.0)
    with pytest.raises(StaleResult) as info:
        ensure_fresh(envelope, identity(snapshot_hash="snap-b"))
    assert info.value.reason is StaleReason.SNAPSHOT_CHANGED


def test_envelope_of_another_task_is_refused():
    envelope = seal(identity(), payload={}, produced_at=1.0)
    with pytest.raises(StaleResult) as info:
        ensure_fresh(envelope, identity(task_id="t2"))
    assert info.value.reason is StaleReason.TASK_MISMATCH


def test_envelope_of_another_objective_is_refused():
    envelope = seal(identity(), payload={}, produced_at=1.0)
    with pytest.raises(StaleResult) as info:
        ensure_fresh(envelope, identity(objective_hash="obj-b"))
    assert info.value.reason is StaleReason.IDENTITY_MISMATCH


def test_envelope_of_another_input_revision_is_refused():
    envelope = seal(identity(), payload={}, produced_at=1.0)
    assert staleness(envelope, identity(input_hash="in-b")) is StaleReason.IDENTITY_MISMATCH


def test_stale_result_carries_the_refused_envelope():
    envelope = seal(identity(), payload={}, produced_at=1.0)
    error = StaleResult(envelope, StaleReason.SNAPSHOT_CHANGED)
    assert error.envelope is envelope
    assert "snapshot_changed" in str(error)


def test_same_envelope_and_payload_is_duplicate():
    first = seal(identity(), payload={"n": 1}, produced_at=1.0)
    again = seal(identity(), payload={"n": 1}, produced_at=99.0)
    other = seal(identity(), payload={"n": 2}, produced_at=1.0)
    assert is_duplicate(again, first)
    assert not is_duplicate(other, first)
    assert not is_duplicate(first, None)
