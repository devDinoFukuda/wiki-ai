from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "VocabularyError",
    "VOCABULARY_PATH",
    "load",
    "terms",
    "mapping",
    "number",
    "text",
]

VOCABULARY_PATH = Path(__file__).with_suffix(".json")


class VocabularyError(Exception):
    pass


@lru_cache(maxsize=1)
def load() -> Mapping[str, Any]:
    try:
        raw = VOCABULARY_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise VocabularyError(f"{VOCABULARY_PATH}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VocabularyError(f"{VOCABULARY_PATH}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise VocabularyError(f"{VOCABULARY_PATH}: payload must be a mapping")
    return payload


def _entry(key: str) -> Any:
    payload = load()
    if key not in payload:
        raise VocabularyError(f"{VOCABULARY_PATH}: missing key {key}")
    return payload[key]


def terms(key: str) -> tuple[str, ...]:
    value = _entry(key)
    if not isinstance(value, (list, tuple)):
        raise VocabularyError(f"{VOCABULARY_PATH}: {key} must be a sequence")
    return tuple(str(item) for item in value)


def mapping(key: str) -> Mapping[str, tuple[str, ...]]:
    value = _entry(key)
    if not isinstance(value, Mapping):
        raise VocabularyError(f"{VOCABULARY_PATH}: {key} must be a mapping")
    return {
        str(name): tuple(str(item) for item in targets)
        for name, targets in value.items()
    }


def number(key: str) -> int:
    value = _entry(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise VocabularyError(f"{VOCABULARY_PATH}: {key} must be an integer")
    return value


def text(key: str) -> str:
    return str(_entry(key))
