from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .matching import normalize
from .model import Entity

__all__ = [
    "Contradiction",
    "NEGATION_MARKERS",
    "OPPOSITE_EFFECTS",
    "STATEMENT_ATTRIBUTES",
    "UNIT_LABELS",
    "contradiction_between",
    "measures",
    "payload_of",
]

STATEMENT_ATTRIBUTES: tuple[str, ...] = (
    "statement",
    "decision",
    "rule",
    "condition",
    "expected",
    "behaviour",
    "effect",
)

SEQUENCE_ATTRIBUTES: tuple[str, ...] = ("conditions", "effects")

NEGATION_MARKERS: tuple[str, ...] = (
    "nao",
    "nunca",
    "jamais",
    "sem",
    "not",
    "never",
    "no",
    "without",
)

OPPOSITE_EFFECTS: tuple[tuple[str, str], ...] = (
    ("aprovada", "recusada"),
    ("aprovado", "recusado"),
    ("aprova", "recusa"),
    ("permite", "bloqueia"),
    ("permitido", "bloqueado"),
    ("habilita", "desabilita"),
    ("ativa", "inativa"),
    ("aceita", "rejeita"),
    ("allow", "deny"),
    ("allowed", "denied"),
    ("enable", "disable"),
    ("accept", "reject"),
    ("approve", "decline"),
)

_MEASURE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>dias|dia|days|day|horas|hora|hours|hour|"
    r"meses|mes|months|month|minutos|minuto|minutes|minute|por\s?cento|porcento|%)"
)

UNIT_LABELS: Mapping[str, str] = {
    "day": "dias",
    "hour": "horas",
    "month": "meses",
    "minute": "minutos",
    "percent": "por cento",
}

_UNIT_ALIASES: Mapping[str, str] = {
    "dia": "day",
    "dias": "day",
    "day": "day",
    "days": "day",
    "hora": "hour",
    "horas": "hour",
    "hour": "hour",
    "hours": "hour",
    "mes": "month",
    "meses": "month",
    "month": "month",
    "months": "month",
    "minuto": "minute",
    "minutos": "minute",
    "minute": "minute",
    "minutes": "minute",
    "por cento": "percent",
    "porcento": "percent",
    "%": "percent",
}


@dataclass(frozen=True)
class Contradiction:
    reason: str
    detail: str


def _texts(entity: Entity) -> tuple[str, ...]:
    found: list[str] = []
    for name in STATEMENT_ATTRIBUTES:
        value = entity.attributes.get(name)
        if isinstance(value, str) and value.strip():
            found.append(value)
    for name in SEQUENCE_ATTRIBUTES:
        value = entity.attributes.get(name)
        if isinstance(value, (list, tuple)):
            found.extend(str(item) for item in value if str(item).strip())
    return tuple(found)


def _joined(entity: Entity) -> str:
    return normalize(" ".join((entity.name,) + _texts(entity)))


def measures(text: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for match in _MEASURE.finditer(normalize(text)):
        unit = _UNIT_ALIASES.get(match.group("unit").strip(), match.group("unit"))
        value = float(match.group("value").replace(",", "."))
        found.setdefault(unit, value)
    return found


def _threshold_conflict(left: Entity, right: Entity) -> Contradiction | None:
    left_measures = measures(_joined(left))
    right_measures = measures(_joined(right))
    for unit, value in sorted(left_measures.items()):
        other = right_measures.get(unit)
        if other is None or other == value:
            continue
        label = UNIT_LABELS.get(unit, unit)
        return Contradiction(
            reason="numeric_threshold",
            detail=f"limite de {_render(value)} {label} contra {_render(other)} {label}",
        )
    return None


def _render(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _negation_conflict(left: Entity, right: Entity) -> Contradiction | None:
    left_words = _joined(left).split()
    right_words = _joined(right).split()
    left_negated = _negated(left_words)
    right_negated = _negated(right_words)
    if left_negated == right_negated:
        return None
    shared = set(_content(left_words)) & set(_content(right_words))
    if not shared:
        return None
    return Contradiction(
        reason="negation",
        detail=(
            "uma fonte nega o que a outra afirma sobre "
            + ", ".join(sorted(shared)[:3])
        ),
    )


def _negated(words: Sequence[str]) -> bool:
    return any(word in NEGATION_MARKERS for word in words)


def _content(words: Sequence[str]) -> tuple[str, ...]:
    return tuple(word for word in words if word not in NEGATION_MARKERS and len(word) > 3)


def _effect_conflict(left: Entity, right: Entity) -> Contradiction | None:
    left_words = set(_joined(left).split())
    right_words = set(_joined(right).split())
    for first, second in OPPOSITE_EFFECTS:
        if (first in left_words and second in right_words) or (
            second in left_words and first in right_words
        ):
            return Contradiction(
                reason="opposite_effect",
                detail=f"efeitos opostos: {first} contra {second}",
            )
    return None


def contradiction_between(left: Entity, right: Entity) -> Contradiction | None:
    if left.kind != right.kind:
        return None
    for detector in (_threshold_conflict, _effect_conflict, _negation_conflict):
        found = detector(left, right)
        if found is not None:
            return found
    return None


def payload_of(found: Contradiction) -> dict[str, Any]:
    return {"contradiction_reason": found.reason, "contradiction_detail": found.detail}
