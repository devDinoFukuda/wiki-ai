from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "GroundingCheck",
    "STOPWORDS",
    "MIN_TERM_LENGTH",
    "key_terms",
    "excerpt_vocabulary",
    "check_component",
    "symbol_defined_or_referenced",
]

MIN_TERM_LENGTH = 3

STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "that",
        "this",
        "these",
        "those",
        "and",
        "but",
        "for",
        "nor",
        "yet",
        "with",
        "without",
        "from",
        "into",
        "onto",
        "over",
        "under",
        "after",
        "before",
        "when",
        "while",
        "until",
        "unless",
        "then",
        "than",
        "will",
        "shall",
        "must",
        "may",
        "can",
        "should",
        "would",
        "could",
        "does",
        "did",
        "done",
        "has",
        "have",
        "had",
        "was",
        "were",
        "been",
        "being",
        "are",
        "its",
        "his",
        "her",
        "their",
        "each",
        "every",
        "any",
        "all",
        "some",
        "none",
        "only",
        "also",
        "such",
        "same",
        "other",
        "another",
        "which",
        "what",
        "whose",
        "there",
        "here",
        "always",
        "never",
        "still",
        "just",
        "very",
        "much",
        "many",
        "more",
        "most",
        "less",
        "least",
        "case",
        "cases",
        "value",
        "values",
        "data",
        "item",
        "items",
        "thing",
        "things",
        "way",
        "ways",
        "step",
        "steps",
        "part",
        "parts",
        "side",
        "kind",
        "type",
        "types",
        "system",
        "service",
        "module",
        "component",
        "process",
        "operation",
        "action",
        "result",
        "results",
        "state",
        "status",
        "rule",
        "rules",
        "logic",
        "flow",
        "used",
        "uses",
        "using",
        "make",
        "makes",
        "made",
        "get",
        "gets",
        "set",
        "sets",
        "put",
        "puts",
        "take",
        "takes",
        "give",
        "gives",
        "goes",
        "come",
        "comes",
        "keep",
        "keeps",
        "let",
        "lets",
        "not",
        "com",
        "org",
        "www",
        "que",
        "para",
        "com",
        "uma",
        "dos",
        "das",
        "nos",
        "nas",
        "pelo",
        "pela",
        "sobre",
        "quando",
        "sempre",
        "nunca",
        "cada",
        "todo",
        "toda",
        "todos",
        "todas",
        "caso",
        "casos",
        "valor",
        "valores",
        "dado",
        "dados",
        "regra",
        "regras",
        "estado",
        "sistema",
        "modulo",
        "processo",
        "acao",
        "resultado",
    }
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"\b\d+(?:[._]\d+)*\b")
_QUOTED = re.compile(r"""["'`]([^"'`\n]{1,80})["'`]""")
_OPERATOR = re.compile(r"(==|!=|<=|>=|&&|\|\||[<>+\-*/%=!]=?|\?\?|=>|->|::)")
_WORD_NUMBERS: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "um": "1",
    "uma": "1",
    "dois": "2",
    "duas": "2",
    "tres": "3",
    "quatro": "4",
    "cinco": "5",
    "seis": "6",
    "sete": "7",
    "oito": "8",
    "nove": "9",
    "dez": "10",
}


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _split_identifier(token: str) -> set[str]:
    pieces: set[str] = set()
    for chunk in re.split(r"[_\-.]+", token):
        if not chunk:
            continue
        for match in re.finditer(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+|\d+", chunk):
            piece = match.group(0).lower()
            if len(piece) >= MIN_TERM_LENGTH:
                pieces.add(piece)
    return pieces


@dataclass(frozen=True)
class GroundingCheck:
    component: str
    terms_required: tuple[str, ...]
    terms_found: tuple[str, ...]
    ok: bool

    @property
    def terms_missing(self) -> tuple[str, ...]:
        found = set(self.terms_found)
        return tuple(term for term in self.terms_required if term not in found)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "terms_required": list(self.terms_required),
            "terms_found": list(self.terms_found),
            "terms_missing": list(self.terms_missing),
            "ok": self.ok,
        }


def key_terms(text: str, *, exclude: Sequence[str] = ()) -> tuple[str, ...]:
    folded = _fold(text)
    banned: set[str] = set()
    for item in exclude:
        banned |= _split_identifier(_fold(item))
        banned.add(_fold(item).strip().lower())
    terms: list[str] = []

    def _add(value: str) -> None:
        if value and value not in terms:
            terms.append(value)

    for match in _QUOTED.finditer(folded):
        _add(match.group(1).strip().lower())
    for match in _NUMBER.finditer(folded):
        _add(match.group(0))
    for match in _OPERATOR.finditer(folded):
        _add(match.group(0))
    for match in _IDENTIFIER.finditer(folded):
        token = match.group(0)
        lowered = token.lower()
        if lowered in _WORD_NUMBERS:
            _add(_WORD_NUMBERS[lowered])
            continue
        if len(lowered) < MIN_TERM_LENGTH:
            continue
        if lowered in STOPWORDS or lowered in banned:
            continue
        pieces = _split_identifier(token)
        meaningful = {
            piece
            for piece in pieces
            if piece not in STOPWORDS and piece not in banned
        }
        if not meaningful:
            continue
        _add(lowered)
    return tuple(terms)


def excerpt_vocabulary(excerpt: str) -> frozenset[str]:
    folded = _fold(excerpt)
    vocabulary: set[str] = set()
    for match in _IDENTIFIER.finditer(folded):
        token = match.group(0)
        vocabulary.add(token.lower())
        vocabulary |= _split_identifier(token)
    for match in _NUMBER.finditer(folded):
        vocabulary.add(match.group(0))
        vocabulary.add(match.group(0).lstrip("0") or "0")
    for match in _QUOTED.finditer(folded):
        vocabulary.add(match.group(1).strip().lower())
    for match in _OPERATOR.finditer(folded):
        vocabulary.add(match.group(0))
    return frozenset(vocabulary)


def _is_bare_name(subject: str) -> bool:
    return len(_fold(subject).strip().split()) == 1


def _matches(term: str, vocabulary: frozenset[str]) -> bool:
    if term in vocabulary:
        return True
    pieces = _split_identifier(term)
    return bool(pieces) and pieces <= vocabulary


def check_component(
    component: str,
    text: str,
    vocabulary: frozenset[str],
    *,
    exclude: Sequence[str] = (),
    subject: str = "",
) -> GroundingCheck:
    required = key_terms(text, exclude=exclude)
    found = tuple(term for term in required if _matches(term, vocabulary))
    ok = bool(required) and bool(found)
    if ok and subject and _is_bare_name(subject):
        naming = _split_identifier(_fold(subject))
        naming.add(_fold(subject).strip().lower())
        if all(term in naming for term in found):
            ok = False
    return GroundingCheck(
        component=component,
        terms_required=required,
        terms_found=found,
        ok=ok,
    )


def symbol_defined_or_referenced(subject: str, vocabulary: frozenset[str]) -> GroundingCheck:
    required = key_terms(subject)
    found = tuple(term for term in required if _matches(term, vocabulary))
    if _is_bare_name(subject):
        ok = bool(required) and len(found) == len(required)
    else:
        ok = bool(found)
    return GroundingCheck(
        component="symbol",
        terms_required=required,
        terms_found=found,
        ok=ok,
    )
