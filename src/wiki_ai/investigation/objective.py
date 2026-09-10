from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

__all__ = [
    "ObjectiveError",
    "ObjectiveKind",
    "Scope",
    "Objective",
    "DEFAULT_GOAL",
    "parse",
    "objective_hash",
]


class ObjectiveError(Exception):
    pass


class ObjectiveKind(str, Enum):
    SYSTEM_ANALYSIS = "system_analysis"
    CAPABILITY_ANALYSIS = "capability_analysis"
    IMPACT_ANALYSIS = "impact_analysis"
    QUESTION = "question"
    TARGETED_REINVESTIGATION = "targeted_reinvestigation"


DEFAULT_GOAL = "reconstruct implemented behavior"

_PATH_PATTERN = re.compile(r"[\w.\-]+/[\w./\-]+")
_SYMBOL_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\b")
_ENTITY_PATTERN = re.compile(r"\bent_[0-9a-f]{8,}\b")
_QUESTION_STARTERS = (
    "what",
    "why",
    "how",
    "when",
    "where",
    "which",
    "who",
    "does",
    "do",
    "is",
    "are",
    "can",
    "should",
    "qual",
    "quais",
    "como",
    "onde",
    "quando",
    "por que",
    "porque",
    "quem",
)

_KIND_MARKERS: tuple[tuple[ObjectiveKind, tuple[str, ...]], ...] = (
    (
        ObjectiveKind.IMPACT_ANALYSIS,
        (
            "impact",
            "impacto",
            "affected by",
            "afetado",
            "blast radius",
            "what breaks",
            "change to",
        ),
    ),
    (
        ObjectiveKind.TARGETED_REINVESTIGATION,
        (
            "reinvestigate",
            "reinvestigar",
            "re-investigate",
            "revisit",
            "recheck",
            "reanalyse",
            "reanalyze",
            "reanalisar",
        ),
    ),
    (
        ObjectiveKind.CAPABILITY_ANALYSIS,
        (
            "capability",
            "capabilities",
            "funcionalidade",
            "feature",
            "use case",
            "caso de uso",
            "fluxo de",
        ),
    ),
)


@dataclass(frozen=True)
class Scope:
    paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", _clean(self.paths))
        object.__setattr__(self, "symbols", _clean(self.symbols))
        object.__setattr__(self, "entities", _clean(self.entities))

    @property
    def is_repository_wide(self) -> bool:
        return not (self.paths or self.symbols or self.entities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "symbols": list(self.symbols),
            "entities": list(self.entities),
        }

    def describe(self) -> str:
        if self.is_repository_wide:
            return "repository"
        parts: list[str] = []
        if self.paths:
            parts.append("paths=" + ",".join(self.paths))
        if self.symbols:
            parts.append("symbols=" + ",".join(self.symbols))
        if self.entities:
            parts.append("entities=" + ",".join(self.entities))
        return " ".join(parts)


@dataclass(frozen=True)
class Objective:
    kind: ObjectiveKind = ObjectiveKind.SYSTEM_ANALYSIS
    scope: Scope = field(default_factory=Scope)
    goal: str = DEFAULT_GOAL
    constraints: tuple[str, ...] = ()
    text: str = ""

    def __post_init__(self) -> None:
        goal = (self.goal or "").strip()
        if not goal:
            raise ObjectiveError("objective without a goal")
        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "constraints", _clean(self.constraints))
        object.__setattr__(self, "text", (self.text or goal).strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "scope": self.scope.to_dict(),
            "goal": self.goal,
            "constraints": list(self.constraints),
        }

    @property
    def hash(self) -> str:
        return objective_hash(self)


def _clean(values: Sequence[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for raw in values:
        item = str(raw).strip()
        if item and item not in seen:
            seen.append(item)
    return tuple(seen)


def objective_hash(objective: Objective) -> str:
    payload = objective.to_dict()
    blob = "\x1f".join(
        (
            str(payload["kind"]),
            "|".join(payload["scope"]["paths"]),
            "|".join(payload["scope"]["symbols"]),
            "|".join(payload["scope"]["entities"]),
            str(payload["goal"]),
            "|".join(payload["constraints"]),
        )
    )
    return "obj_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _detect_kind(lowered: str) -> ObjectiveKind:
    for kind, markers in _KIND_MARKERS:
        if any(marker in lowered for marker in markers):
            return kind
    if lowered.endswith("?") or lowered.startswith(_QUESTION_STARTERS):
        return ObjectiveKind.QUESTION
    return ObjectiveKind.SYSTEM_ANALYSIS


def _detect_paths(text: str) -> tuple[str, ...]:
    return tuple(match.group(0).rstrip(".,;:") for match in _PATH_PATTERN.finditer(text))


def _detect_entities(text: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _ENTITY_PATTERN.finditer(text))


def _detect_symbols(text: str, paths: Sequence[str]) -> tuple[str, ...]:
    consumed = " ".join(paths)
    found: list[str] = []
    for match in _SYMBOL_PATTERN.finditer(text):
        token = match.group(0)
        if token in consumed or token.startswith("ent_"):
            continue
        if len(token) < 3:
            continue
        has_upper_tail = any(char.isupper() for char in token[1:])
        if has_upper_tail or ("_" in token and token.islower() and len(token) > 5):
            found.append(token)
    return tuple(found)


def parse(
    text: str,
    kind: ObjectiveKind | str | None = None,
    scope: Scope | None = None,
    constraints: Sequence[str] = (),
) -> Objective:
    raw = (text or "").strip()
    if not raw:
        raise ObjectiveError("objective text is empty")
    lowered = raw.lower()
    resolved_kind = (
        _detect_kind(lowered)
        if kind is None
        else (kind if isinstance(kind, ObjectiveKind) else _kind_of(str(kind)))
    )
    if scope is None:
        paths = _detect_paths(raw)
        scope = Scope(
            paths=paths,
            symbols=_detect_symbols(raw, paths),
            entities=_detect_entities(raw),
        )
    goal = raw if resolved_kind is not ObjectiveKind.SYSTEM_ANALYSIS else _system_goal(raw)
    return Objective(
        kind=resolved_kind,
        scope=scope,
        goal=goal,
        constraints=tuple(constraints),
        text=raw,
    )


def _system_goal(raw: str) -> str:
    lowered = raw.lower()
    generic = {
        "analyze",
        "analyse",
        "analisar",
        "analise",
        "análise",
        "investigate",
        "investigar",
        "repository",
        "repositorio",
        "repositório",
        "codebase",
        "system",
        "sistema",
        "this",
        "the",
        "o",
        "a",
        "do",
        "da",
    }
    words = [word for word in re.split(r"\W+", lowered) if word]
    if words and all(word in generic for word in words):
        return DEFAULT_GOAL
    return raw


def _kind_of(value: str) -> ObjectiveKind:
    try:
        return ObjectiveKind(value)
    except ValueError:
        raise ObjectiveError(
            f"objective kind {value!r} unknown; allowed: "
            f"{', '.join(item.value for item in ObjectiveKind)}"
        ) from None


def from_mapping(payload: Mapping[str, Any]) -> Objective:
    raw_scope = payload.get("scope")
    scope = Scope()
    if isinstance(raw_scope, Mapping):
        scope = Scope(
            paths=tuple(str(item) for item in raw_scope.get("paths", ())),
            symbols=tuple(str(item) for item in raw_scope.get("symbols", ())),
            entities=tuple(str(item) for item in raw_scope.get("entities", ())),
        )
    elif isinstance(raw_scope, str) and raw_scope.strip() not in ("", "repository"):
        scope = Scope(paths=(raw_scope.strip(),))
    goal = str(payload.get("goal") or DEFAULT_GOAL)
    kind_value = payload.get("kind")
    return Objective(
        kind=_kind_of(str(kind_value)) if kind_value else ObjectiveKind.SYSTEM_ANALYSIS,
        scope=scope,
        goal=goal,
        constraints=tuple(str(item) for item in payload.get("constraints", ())),
        text=str(payload.get("text") or goal),
    )
