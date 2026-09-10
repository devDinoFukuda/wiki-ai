from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ALIAS_ATTRIBUTES",
    "DEFAULT_THRESHOLD",
    "DEFAULT_AMBIGUITY_MARGIN",
    "aliases_of",
    "normalize",
    "singularize",
    "score_names",
    "subject_of",
    "tokens",
]

ALIAS_ATTRIBUTES: tuple[str, ...] = ("aliases", "alias", "name", "names", "synonyms")

DEFAULT_THRESHOLD = 0.6
DEFAULT_AMBIGUITY_MARGIN = 0.08

_PUNCTUATION = re.compile(r"[^a-z0-9]+")
_KEY_SEPARATOR = "::"

_PLURAL_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("oes", "ao"),
    ("aes", "ao"),
    ("ies", "y"),
    ("ches", "ch"),
    ("shes", "sh"),
    ("sses", "ss"),
    ("xes", "x"),
    ("res", "r"),
    ("is", "il"),
    ("ns", "m"),
    ("es", "e"),
    ("s", ""),
)

_MIN_SINGULAR_LENGTH = 4


def normalize(value: Any) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "").lower())
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(_PUNCTUATION.sub(" ", stripped).split())


def singularize(word: str) -> str:
    if len(word) < _MIN_SINGULAR_LENGTH:
        return word
    for suffix, replacement in _PLURAL_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) + len(replacement) >= 2:
            return word[: -len(suffix)] + replacement
    return word


def tokens(value: Any) -> tuple[str, ...]:
    return tuple(singularize(part) for part in normalize(value).split() if part)


def subject_of(stable_key: str) -> str:
    text = str(stable_key or "")
    if _KEY_SEPARATOR in text:
        return text.split(_KEY_SEPARATOR, 1)[1]
    return text


def aliases_of(name: str, attributes: Mapping[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    for candidate in (name,):
        text = normalize(candidate)
        if text and text not in found:
            found.append(text)
    for attribute in ALIAS_ATTRIBUTES:
        raw = attributes.get(attribute)
        for item in _flatten(raw):
            text = normalize(item)
            if text and text not in found:
                found.append(text)
    return tuple(found)


def _flatten(raw: Any) -> Iterable[Any]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(raw)
    return (raw,)


def _overlap(left: Sequence[str], right: Sequence[str]) -> float:
    first = set(left)
    second = set(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def _prefix(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    shortest = min(len(left), len(right))
    shared = 0
    while shared < shortest and left[shared] == right[shared]:
        shared += 1
    longest = max(len(left), len(right))
    return shared / longest


def score_names(left: str, right: str) -> float:
    normalized_left = " ".join(tokens(left))
    normalized_right = " ".join(tokens(right))
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    overlap = _overlap(tokens(left), tokens(right))
    prefix = _prefix(normalized_left, normalized_right)
    return round(0.7 * overlap + 0.3 * prefix, 4)
