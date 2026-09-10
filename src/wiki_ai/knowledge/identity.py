from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Mapping

from .errors import InvalidIdentity

ID_LENGTH = 32
FIELD_SEPARATOR = "\x1f"


def content_hash(payload: bytes | str) -> str:
    if isinstance(payload, bytes):
        blob = payload
    elif isinstance(payload, str):
        blob = payload.encode("utf-8")
    else:
        raise InvalidIdentity(
            f"content_hash aceita bytes ou str, recebido {type(payload).__name__}"
        )
    return hashlib.sha256(blob).hexdigest()


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def digest(*parts: str) -> str:
    return content_hash(FIELD_SEPARATOR.join(parts))[:ID_LENGTH]


def _required(value: str, label: str) -> str:
    text = (value or "").strip()
    if not text:
        raise InvalidIdentity(f"{label} vazio: identidade determinística exige valor")
    return text


def entity_id(kind: str, stable_key: str) -> str:
    return "ent_" + digest(_required(kind, "kind"), _required(stable_key, "stable_key"))


def relation_id(kind: str, source_id: str, target_id: str) -> str:
    return "rel_" + digest(
        _required(kind, "kind"),
        _required(source_id, "source_id"),
        _required(target_id, "target_id"),
    )


def source_version_id(source_id: str, version_hash: str) -> str:
    return "srv_" + digest(
        _required(source_id, "source_id"), _required(version_hash, "version_hash")
    )


def evidence_id(
    source_id: str, version_hash: str, locator: Mapping[str, Any]
) -> str:
    return "evd_" + digest(
        _required(source_id, "source_id"),
        _required(version_hash, "version_hash"),
        canonical_json(dict(locator)),
    )


def new_revision_id() -> str:
    return "rev_" + uuid.uuid4().hex
