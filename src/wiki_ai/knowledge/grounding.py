from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import PayloadInvalid
from .invocation import invocation_targets

__all__ = [
    "GroundingCheck",
    "NEGATIONS",
    "NEGATION_MARKER",
    "STOPWORDS",
    "MIN_TERM_LENGTH",
    "INVOCATION_MARKER",
    "ASSIGNMENT_MARKER",
    "SQL_READ_MARKER",
    "SQL_WRITE_MARKER",
    "IO_SEND_MARKER",
    "IO_RECEIVE_MARKER",
    "STRUCTURAL_MARKERS",
    "STRUCTURAL_KINDS",
    "RELATION_PREDICATES_PATH",
    "RELATION_PREDICATE_COMPONENT",
    "RELATION_STATEMENT_COMPONENT",
    "key_terms",
    "mandatory_terms",
    "excerpt_vocabulary",
    "invocation_targets",
    "check_component",
    "check_relation_predicate",
    "relation_predicate_terms",
    "relation_predicate_lexicon",
    "symbol_defined_or_referenced",
]

RELATION_PREDICATES_PATH = Path(__file__).with_name("relation_predicates.json")
RELATION_PREDICATE_COMPONENT = "relation_predicate"
RELATION_STATEMENT_COMPONENT = "relation_statement"

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


NEGATIONS: frozenset[str] = frozenset(
    {
        "not",
        "never",
        "no",
        "none",
        "without",
        "nao",
        "nunca",
        "sem",
        "nenhum",
        "nenhuma",
    }
)

NEGATION_MARKER = "!negated"

INVOCATION_MARKER = "!invocation"
ASSIGNMENT_MARKER = "!assignment"
SQL_READ_MARKER = "!sql_read"
SQL_WRITE_MARKER = "!sql_write"
IO_SEND_MARKER = "!io_send"
IO_RECEIVE_MARKER = "!io_receive"

CALLEE_PREFIX = "!callee:"

STRUCTURAL_MARKERS: frozenset[str] = frozenset(
    {
        INVOCATION_MARKER,
        ASSIGNMENT_MARKER,
        SQL_READ_MARKER,
        SQL_WRITE_MARKER,
        IO_SEND_MARKER,
        IO_RECEIVE_MARKER,
    }
)

STRUCTURAL_KINDS: frozenset[str] = frozenset(
    {
        "calls",
        "triggers",
        "reads",
        "writes",
        "persists_to",
        "publishes",
        "consumes",
    }
)

_ASSIGNMENT = re.compile(
    r"[A-Za-z_][A-Za-z0-9_.\[\]]*\s*(?::=|=)(?!=)\s*\S"
)

_SQL_READ = re.compile(
    r"\bselect\b[\s\S]{0,200}?\bfrom\b", re.IGNORECASE
)

_SQL_WRITE = re.compile(
    r"\b(?:insert\s+into|update\s+[A-Za-z_]|delete\s+from|merge\s+into)",
    re.IGNORECASE,
)

_IO_SEND = re.compile(
    r"\b(?:send|sends|emit|emits|publish|publishes|post)[A-Za-z_]*\s*\(",
    re.IGNORECASE,
)

_IO_RECEIVE = re.compile(
    r"\b(?:subscribe|subscribes|consume|consumes|receive|receives|on)[A-Za-z_]*\s*\(",
    re.IGNORECASE,
)

_ADVERB_SUFFIXES: tuple[str, ...] = ("mente", "ly")

_VERB_SUFFIXES: tuple[str, ...] = (
    "izes",
    "ises",
    "ates",
    "ies",
    "ing",
    "ized",
    "ised",
    "ated",
    "ed",
    "es",
    "am",
    "em",
    "ar",
    "er",
    "ir",
    "a",
    "e",
    "s",
)

_AUXILIARIES: frozenset[str] = frozenset(
    {
        "must",
        "shall",
        "will",
        "should",
        "would",
        "could",
        "may",
        "can",
        "does",
        "did",
        "has",
        "have",
        "had",
        "was",
        "were",
        "are",
        "been",
        "being",
        "deve",
        "pode",
        "vai",
        "tem",
        "esta",
        "sera",
    }
)

_VERB_MARKERS: tuple[str, ...] = (
    "ifies",
    "izes",
    "ises",
    "ates",
    "izing",
    "ising",
    "ating",
    "ized",
    "ised",
    "ated",
    "ing",
)

_VERB_LEXICON: frozenset[str] = frozenset(
    {
        "abort",
        "aborts",
        "aborted",
        "accept",
        "accepts",
        "accepted",
        "allow",
        "allows",
        "allowed",
        "block",
        "blocks",
        "blocked",
        "close",
        "closes",
        "closed",
        "commit",
        "commits",
        "committed",
        "delete",
        "deletes",
        "deleted",
        "deny",
        "denies",
        "denied",
        "discard",
        "discards",
        "discarded",
        "fail",
        "fails",
        "failed",
        "load",
        "loads",
        "loaded",
        "open",
        "opens",
        "opened",
        "persist",
        "persists",
        "persisted",
        "read",
        "reads",
        "reject",
        "rejects",
        "rejected",
        "remove",
        "removes",
        "removed",
        "retry",
        "retries",
        "retried",
        "return",
        "returns",
        "returned",
        "rollback",
        "save",
        "saves",
        "saved",
        "send",
        "sends",
        "sent",
        "skip",
        "skips",
        "skipped",
        "store",
        "stores",
        "stored",
        "throw",
        "throws",
        "thrown",
        "write",
        "writes",
        "wrote",
        "written",
        "aceita",
        "aceitar",
        "apaga",
        "apagar",
        "bloqueia",
        "bloquear",
        "envia",
        "enviar",
        "grava",
        "gravar",
        "lanca",
        "lancar",
        "permite",
        "permitir",
        "rejeita",
        "rejeitar",
        "remover",
        "retorna",
        "retornar",
        "salva",
        "salvar",
    }
)


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


def _stem(word: str) -> str:
    lowered = word.lower()
    for suffix in _ADVERB_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) - len(suffix) >= MIN_TERM_LENGTH:
            lowered = lowered[: -len(suffix)]
            break
    for suffix in _VERB_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) - len(suffix) >= MIN_TERM_LENGTH:
            return lowered[: -len(suffix)]
    return lowered


def _is_verb(word: str) -> bool:
    lowered = word.lower()
    if lowered in _AUXILIARIES:
        return False
    if lowered in _VERB_LEXICON:
        return True
    return any(lowered.endswith(marker) for marker in _VERB_MARKERS)


def _banned_terms(exclude: Sequence[str]) -> set[str]:
    banned: set[str] = set()
    for item in exclude:
        banned |= _split_identifier(_fold(item))
        banned.add(_fold(item).strip().lower())
    return banned


@dataclass(frozen=True)
class GroundingCheck:
    component: str
    terms_required: tuple[str, ...]
    terms_found: tuple[str, ...]
    ok: bool
    required_terms: tuple[str, ...] = ()

    @property
    def terms_missing(self) -> tuple[str, ...]:
        found = set(self.terms_found)
        return tuple(term for term in self.terms_required if term not in found)

    @property
    def required_missing(self) -> tuple[str, ...]:
        found = set(self.terms_found)
        return tuple(term for term in self.required_terms if term not in found)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "terms_required": list(self.terms_required),
            "required_terms": list(self.required_terms),
            "terms_found": list(self.terms_found),
            "terms_missing": list(self.terms_missing),
            "required_missing": list(self.required_missing),
            "ok": self.ok,
        }


def key_terms(text: str, *, exclude: Sequence[str] = ()) -> tuple[str, ...]:
    folded = _fold(text)
    banned = _banned_terms(exclude)
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
            piece for piece in pieces if piece not in STOPWORDS and piece not in banned
        }
        if not meaningful:
            continue
        _add(lowered)
    return tuple(terms)


def mandatory_terms(text: str, *, exclude: Sequence[str] = ()) -> tuple[str, ...]:
    folded = _fold(text)
    banned = _banned_terms(exclude)
    mandatory: list[str] = []

    def _add(value: str) -> None:
        if value and value not in mandatory:
            mandatory.append(value)

    for match in _QUOTED.finditer(folded):
        _add(match.group(1).strip().lower())
    for match in _NUMBER.finditer(folded):
        _add(match.group(0))
    for match in _OPERATOR.finditer(folded):
        _add(match.group(0))
    verbs: list[str] = []
    for match in _IDENTIFIER.finditer(folded):
        token = match.group(0)
        lowered = token.lower()
        if lowered in NEGATIONS:
            _add(NEGATION_MARKER)
            continue
        if lowered in _WORD_NUMBERS:
            _add(_WORD_NUMBERS[lowered])
            continue
        if len(lowered) < MIN_TERM_LENGTH or lowered in banned:
            continue
        if _is_verb(lowered):
            verbs.append(lowered)
    if verbs:
        _add(verbs[0])
    return tuple(mandatory)


def excerpt_vocabulary(excerpt: str) -> frozenset[str]:
    folded = _fold(excerpt)
    vocabulary: set[str] = set()
    for match in _IDENTIFIER.finditer(folded):
        token = match.group(0)
        lowered = token.lower()
        vocabulary.add(lowered)
        vocabulary |= _split_identifier(token)
        if lowered in NEGATIONS:
            vocabulary.add(NEGATION_MARKER)
        if lowered in _WORD_NUMBERS:
            vocabulary.add(_WORD_NUMBERS[lowered])
    for match in _NUMBER.finditer(folded):
        vocabulary.add(match.group(0))
        vocabulary.add(match.group(0).lstrip("0") or "0")
    for match in _QUOTED.finditer(folded):
        vocabulary.add(match.group(1).strip().lower())
    for match in _OPERATOR.finditer(folded):
        vocabulary.add(match.group(0))
    for marker, pattern in (
        (ASSIGNMENT_MARKER, _ASSIGNMENT),
        (SQL_READ_MARKER, _SQL_READ),
        (SQL_WRITE_MARKER, _SQL_WRITE),
        (IO_SEND_MARKER, _IO_SEND),
        (IO_RECEIVE_MARKER, _IO_RECEIVE),
    ):
        if pattern.search(folded):
            vocabulary.add(marker)
    for name in invocation_targets(folded):
        vocabulary.add(INVOCATION_MARKER)
        vocabulary.add(f"{CALLEE_PREFIX}{name}")
        if name.isalpha():
            vocabulary.add(f"{CALLEE_PREFIX}{_stem(name)}")
        for piece in _split_identifier(name):
            vocabulary.add(f"{CALLEE_PREFIX}{piece}")
            vocabulary.add(f"{CALLEE_PREFIX}{_stem(piece)}")
    for token in tuple(vocabulary):
        if token.isalpha():
            vocabulary.add(_stem(token))
    return frozenset(vocabulary)


def _is_bare_name(subject: str) -> bool:
    return len(_fold(subject).strip().split()) == 1


def _matches(term: str, vocabulary: frozenset[str]) -> bool:
    if term in vocabulary:
        return True
    if term == NEGATION_MARKER:
        return False
    if term.isalpha() and _stem(term) in vocabulary:
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
    mandatory = mandatory_terms(text, exclude=exclude)
    universe: list[str] = list(key_terms(text, exclude=exclude))
    for term in mandatory:
        if term not in universe:
            universe.append(term)
    found = tuple(term for term in universe if _matches(term, vocabulary))
    seen = set(found)
    optional = [term for term in universe if term not in mandatory]
    mandatory_ok = all(term in seen for term in mandatory)
    optional_ok = not optional or sum(
        1 for term in optional if term in seen
    ) * 2 >= len(optional)
    ok = bool(universe) and mandatory_ok and optional_ok
    if ok and subject and _is_bare_name(subject):
        naming = _split_identifier(_fold(subject))
        naming.add(_fold(subject).strip().lower())
        if all(term in naming for term in found):
            ok = False
    return GroundingCheck(
        component=component,
        terms_required=tuple(universe),
        terms_found=found,
        ok=ok,
        required_terms=mandatory,
    )


def symbol_defined_or_referenced(
    subject: str, vocabulary: frozenset[str]
) -> GroundingCheck:
    required = key_terms(subject)
    found = tuple(term for term in required if _matches(term, vocabulary))
    bare = _is_bare_name(subject)
    if bare:
        ok = bool(required) and len(found) == len(required)
    else:
        ok = bool(found)
    return GroundingCheck(
        component="symbol",
        terms_required=required,
        terms_found=found,
        ok=ok,
        required_terms=required if bare else (),
    )


@dataclass(frozen=True)
class RelationLexicon:
    kind: str
    strong: tuple[str, ...]
    structural: tuple[str, ...]

    @property
    def terms(self) -> tuple[str, ...]:
        return self.structural + self.strong

    @property
    def demands_structure(self) -> bool:
        return self.kind in STRUCTURAL_KINDS


_EMPTY_LEXICON = RelationLexicon(kind="", strong=(), structural=())


def _terms_of(kind: str, payload: Any, label: str) -> tuple[str, ...]:
    if not isinstance(payload, (list, tuple)):
        raise PayloadInvalid(
            f"{RELATION_PREDICATES_PATH}: {kind}.{label} exige sequência de termos"
        )
    return tuple(dict.fromkeys(str(term).strip().lower() for term in payload if str(term).strip()))


@lru_cache(maxsize=1)
def _relation_predicates() -> Mapping[str, RelationLexicon]:
    try:
        raw = RELATION_PREDICATES_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise PayloadInvalid(f"{RELATION_PREDICATES_PATH}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PayloadInvalid(f"{RELATION_PREDICATES_PATH}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise PayloadInvalid(f"{RELATION_PREDICATES_PATH}: payload não é mapeamento")
    lexicon: dict[str, RelationLexicon] = {}
    for raw_kind, entry in payload.items():
        kind = str(raw_kind).strip().lower()
        if not isinstance(entry, Mapping):
            raise PayloadInvalid(
                f"{RELATION_PREDICATES_PATH}: {kind} exige mapeamento strong/structural"
            )
        strong = _terms_of(kind, entry.get("strong", ()), "strong")
        structural = _terms_of(kind, entry.get("structural", ()), "structural")
        if not strong:
            raise PayloadInvalid(
                f"{RELATION_PREDICATES_PATH}: {kind} sem termos strong"
            )
        unknown = tuple(term for term in structural if term not in STRUCTURAL_MARKERS)
        if unknown:
            raise PayloadInvalid(
                f"{RELATION_PREDICATES_PATH}: {kind} cita marcador estrutural "
                f"desconhecido: {', '.join(unknown)}"
            )
        lexicon[kind] = RelationLexicon(
            kind=kind, strong=strong, structural=structural
        )
    return lexicon


def relation_predicate_lexicon(kind: str) -> RelationLexicon:
    return _relation_predicates().get((kind or "").strip().lower(), _EMPTY_LEXICON)


def relation_predicate_terms(kind: str) -> tuple[str, ...]:
    return relation_predicate_lexicon(kind).terms


def _lexicon_stems(kind: str) -> frozenset[str]:
    stems: set[str] = set()
    for term in relation_predicate_lexicon(kind).strong:
        stems.add(term)
        if term.isalpha():
            stems.add(_stem(term))
    return frozenset(stems)


def _present(term: str, vocabulary: frozenset[str]) -> bool:
    if term in vocabulary:
        return True
    return term.isalpha() and _stem(term) in vocabulary


def _distinct_strong(found: Sequence[str]) -> int:
    roots: set[str] = set()
    for term in found:
        roots.add(_stem(term) if term.isalpha() else term)
    return len(roots)


def check_relation_predicate(
    kind: str, statement: str, vocabulary: frozenset[str]
) -> GroundingCheck:
    lexicon = relation_predicate_lexicon(kind)
    required = lexicon.terms
    strong_found = tuple(
        term for term in lexicon.strong if _present(term, vocabulary)
    )
    callees = tuple(
        term for term in lexicon.strong if f"{CALLEE_PREFIX}{term}" in vocabulary
    )
    structural_found = tuple(
        term for term in lexicon.structural if term in vocabulary
    )
    if callees:
        structural_found = structural_found + tuple(
            f"{CALLEE_PREFIX}{term}" for term in callees
        )
    found = structural_found + strong_found
    if lexicon.demands_structure:
        signal_ok = bool(structural_found) or _distinct_strong(strong_found) >= 2
    else:
        signal_ok = bool(strong_found)
    statement_text = (statement or "").strip()
    statement_ok = False
    if statement_text:
        widened = vocabulary
        if signal_ok:
            widened = vocabulary | _lexicon_stems(kind)
        statement_ok = check_component(
            RELATION_STATEMENT_COMPONENT, statement_text, widened
        ).ok
    return GroundingCheck(
        component=RELATION_PREDICATE_COMPONENT,
        terms_required=required,
        terms_found=found,
        ok=bool(required) and signal_ok and statement_ok,
        required_terms=lexicon.structural or lexicon.strong[:1],
    )
