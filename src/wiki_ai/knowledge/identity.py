from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from typing import Any, Mapping

from .errors import InvalidIdentity

ID_LENGTH = 32
FIELD_SEPARATOR = "\x1f"
KEY_SEPARATOR = "::"
EXPLICIT_PREFIX = "explicit"
GLOBAL_OWNER = "global"

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


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


def canonical_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "").lower())
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return "_".join(_NON_ALPHANUMERIC.sub(" ", stripped).split())


def contextual_key(
    namespace: str,
    kind: str,
    owner: str | None,
    name: str,
    explicit_id: str | None = None,
) -> str:
    explicit = (explicit_id or "").strip()
    if explicit:
        return f"{EXPLICIT_PREFIX}{KEY_SEPARATOR}{explicit}"
    parts = (
        canonical_name(_required(namespace, "namespace")),
        canonical_name(_required(kind, "kind")),
        canonical_name(owner) or GLOBAL_OWNER,
        canonical_name(_required(name, "name")),
    )
    if not parts[3]:
        raise InvalidIdentity("name sem caracteres alfanuméricos: identidade indefinida")
    return KEY_SEPARATOR.join(parts)


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
