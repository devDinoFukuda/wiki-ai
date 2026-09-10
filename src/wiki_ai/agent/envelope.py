from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

ENVELOPE_ID_PREFIX = "env"


def content_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EnvelopeIdentity:
    task_id: str
    snapshot_hash: str
    objective_hash: str
    input_hash: str


def envelope_id(identity: EnvelopeIdentity) -> str:
    digest = content_hash(
        {
            "task_id": identity.task_id,
            "snapshot_hash": identity.snapshot_hash,
            "objective_hash": identity.objective_hash,
            "input_hash": identity.input_hash,
        }
    )
    return f"{ENVELOPE_ID_PREFIX}_{digest}"


@dataclass(frozen=True)
class ResultEnvelope:
    envelope_id: str
    task_id: str
    produced_for_snapshot: str
    payload_hash: str
    produced_at: float


def seal(identity: EnvelopeIdentity, *, payload: Any, produced_at: float) -> ResultEnvelope:
    return ResultEnvelope(
        envelope_id=envelope_id(identity),
        task_id=identity.task_id,
        produced_for_snapshot=identity.snapshot_hash,
        payload_hash=content_hash(payload),
        produced_at=produced_at,
    )


class StaleReason(Enum):
    SNAPSHOT_CHANGED = "snapshot_changed"
    TASK_MISMATCH = "task_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"


class StaleResult(Exception):
    def __init__(self, envelope: ResultEnvelope, reason: StaleReason) -> None:
        super().__init__(
            f"envelope {envelope.envelope_id[:16]!r} da tarefa {envelope.task_id!r} "
            f"recusado: {reason.value}"
        )
        self.envelope = envelope
        self.reason = reason


def is_stale(envelope: ResultEnvelope, current_snapshot_hash: str) -> bool:
    return envelope.produced_for_snapshot != current_snapshot_hash


def staleness(envelope: ResultEnvelope, identity: EnvelopeIdentity) -> StaleReason | None:
    if envelope.task_id != identity.task_id:
        return StaleReason.TASK_MISMATCH
    if is_stale(envelope, identity.snapshot_hash):
        return StaleReason.SNAPSHOT_CHANGED
    if envelope.envelope_id != envelope_id(identity):
        return StaleReason.IDENTITY_MISMATCH
    return None


def ensure_fresh(envelope: ResultEnvelope, identity: EnvelopeIdentity) -> ResultEnvelope:
    reason = staleness(envelope, identity)
    if reason is not None:
        raise StaleResult(envelope, reason)
    return envelope


def is_duplicate(envelope: ResultEnvelope, known: ResultEnvelope | None) -> bool:
    if known is None:
        return False
    return (
        known.envelope_id == envelope.envelope_id
        and known.payload_hash == envelope.payload_hash
    )
