from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from wiki_ai.ingestion.source import Block, BlockKind

__all__ = [
    "CandidateKind",
    "Hint",
    "normalize_text",
    "stable_key",
    "detect",
    "hints_for",
]


class CandidateKind(str, Enum):
    DECISION = "decision"
    QUESTION = "question"
    HYPOTHESIS = "hypothesis"
    ACTION = "action"
    REQUIREMENT = "requirement"
    RISK = "risk"
    RULE_TABLE = "rule_table"
    TRANSITION = "transition"
    THRESHOLD = "threshold"


def normalize_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value))
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def stable_key(kind: str, subject: str) -> str:
    return f"{kind}::{normalize_text(subject)}"


_PATTERNS: tuple[tuple[CandidateKind, re.Pattern[str]], ...] = (
    (
        CandidateKind.DECISION,
        re.compile(
            r"\b(decidimos|decidiu|decidido|ficou\s+decidido|definimos|"
            r"we\s+decided|decision|agreed\s+to|resolvemos)\b"
        ),
    ),
    (
        CandidateKind.QUESTION,
        re.compile(
            r"(\?\s*$)|\b(qual|quais|quando|como|por\s+que|porque|"
            r"open\s+question|duvida|em\s+aberto|to\s+be\s+defined|tbd)\b"
        ),
    ),
    (
        CandidateKind.HYPOTHESIS,
        re.compile(
            r"\b(talvez|acredito|suponho|hipotese|assumindo|provavelmente|"
            r"we\s+assume|assumption|hypothesis|likely)\b"
        ),
    ),
    (
        CandidateKind.ACTION,
        re.compile(
            r"\b(vou|vamos|precisa\s+ser\s+feito|acao|action\s+item|"
            r"next\s+step|proximo\s+passo|responsavel\s+por|i\s+will|we\s+will)\b"
        ),
    ),
    (
        CandidateKind.REQUIREMENT,
        re.compile(
            r"\b(deve|devera|precisa|obrigatorio|requisito|requirement|"
            r"must|shall|is\s+required|nao\s+pode)\b"
        ),
    ),
    (
        CandidateKind.RISK,
        re.compile(
            r"\b(risco|arriscado|perigo|impacto\s+negativo|risk|"
            r"blocker|bloqueio|preocupa|concern)\b"
        ),
    ),
    (
        CandidateKind.TRANSITION,
        re.compile(
            r"\b(passa\s+para|muda\s+para|transita|vai\s+de\s+.+\s+para|"
            r"transitions?\s+to|moves?\s+to|becomes)\b"
        ),
    ),
    (
        CandidateKind.THRESHOLD,
        re.compile(
            r"(>=|<=|>|<)\s*\d|\b(limite|threshold|acima\s+de|abaixo\s+de|"
            r"maior\s+que|menor\s+que|at\s+least|at\s+most)\b"
        ),
    ),
)

_TABLE_HEADERS: Mapping[CandidateKind, tuple[str, ...]] = {
    CandidateKind.RULE_TABLE: (
        "regra",
        "rule",
        "condicao",
        "condition",
        "acao",
        "action",
        "efeito",
        "effect",
        "resultado",
        "outcome",
    ),
    CandidateKind.THRESHOLD: (
        "limite",
        "threshold",
        "minimo",
        "maximo",
        "min",
        "max",
        "faixa",
        "range",
    ),
    CandidateKind.TRANSITION: (
        "estado",
        "state",
        "de",
        "para",
        "from",
        "to",
        "status",
        "transicao",
    ),
}

_MIN_HEADER_HITS = 2
_SCORE_TEXT = 0.4
_SCORE_HEADER = 0.5
_SCORE_MAX = 0.9


@dataclass(frozen=True)
class Hint:
    block_id: str
    candidate_kind: CandidateKind
    score: float
    marker: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", min(_SCORE_MAX, round(float(self.score), 4)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "candidate_kind": self.candidate_kind.value,
            "score": self.score,
            "marker": self.marker,
            "authoritative": False,
        }


def _header_values(block: Block) -> tuple[str, ...]:
    raw = block.attributes.get("header_row")
    if isinstance(raw, (list, tuple)):
        return tuple(normalize_text(str(item)) for item in raw if str(item).strip())
    column = block.attributes.get("column")
    if isinstance(column, str) and column.strip():
        return (normalize_text(column),)
    return ()


def _from_headers(block: Block) -> list[Hint]:
    headers = _header_values(block)
    if not headers:
        return []
    found: list[Hint] = []
    for kind, vocabulary in _TABLE_HEADERS.items():
        matched = tuple(item for item in headers if item in vocabulary)
        if len(matched) < _MIN_HEADER_HITS:
            continue
        found.append(
            Hint(
                block_id=block.id,
                candidate_kind=kind,
                score=_SCORE_HEADER + 0.1 * (len(matched) - _MIN_HEADER_HITS),
                marker="header:" + ",".join(sorted(matched)),
            )
        )
    return found


def _from_text(block: Block) -> list[Hint]:
    text = normalize_text(block.text)
    if not text:
        return []
    found: list[Hint] = []
    for kind, pattern in _PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        found.append(
            Hint(
                block_id=block.id,
                candidate_kind=kind,
                score=_SCORE_TEXT,
                marker=match.group(0).strip(),
            )
        )
    return found


def detect(block: Block) -> tuple[Hint, ...]:
    found = _from_text(block)
    if block.kind in (BlockKind.TABLE, BlockKind.CELL):
        found.extend(_from_headers(block))
    best: dict[CandidateKind, Hint] = {}
    for hint in found:
        current = best.get(hint.candidate_kind)
        if current is None or hint.score > current.score:
            best[hint.candidate_kind] = hint
    return tuple(best[kind] for kind in sorted(best, key=lambda item: item.value))


def hints_for(
    blocks: Iterable[Block], kinds: Sequence[CandidateKind] = ()
) -> tuple[Hint, ...]:
    wanted = frozenset(kinds)
    collected: list[Hint] = []
    for block in blocks:
        for hint in detect(block):
            if wanted and hint.candidate_kind not in wanted:
                continue
            collected.append(hint)
    return tuple(collected)
