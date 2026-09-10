from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

__all__ = [
    "Anchor",
    "TruthItem",
    "RuleTruth",
    "EdgeCaseTruth",
    "InvariantTruth",
    "IntegrationTruth",
    "EntryPointTruth",
    "StateTruth",
    "TransitionTruth",
    "FailureModeTruth",
    "ContradictionTruth",
    "RepositoryTruth",
]


@dataclass(frozen=True)
class Anchor:
    path: str
    line_start: int
    line_end: int

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ValueError("Anchor.path vazio")
        if self.line_start < 1 or self.line_end < self.line_start:
            raise ValueError(
                f"Anchor invalido em {self.path}: {self.line_start}..{self.line_end}"
            )

    def contains(self, line_start: int, line_end: int) -> bool:
        return line_start <= self.line_end and line_end >= self.line_start

    def covers(self, line_start: int, line_end: int) -> bool:
        return line_start <= self.line_start and line_end >= self.line_end


@dataclass(frozen=True)
class TruthItem:
    key: str
    statement: str
    key_terms: tuple[str, ...]
    anchors: tuple[Anchor, ...]

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("TruthItem.key vazio")
        if not self.statement.strip():
            raise ValueError(f"TruthItem {self.key} sem statement")
        if not self.key_terms:
            raise ValueError(f"TruthItem {self.key} sem key_terms")
        if not self.anchors:
            raise ValueError(f"TruthItem {self.key} sem anchors")


@dataclass(frozen=True)
class RuleTruth(TruthItem):
    conditions: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.conditions:
            raise ValueError(f"RuleTruth {self.key} sem conditions")
        if not self.effects:
            raise ValueError(f"RuleTruth {self.key} sem effects")


@dataclass(frozen=True)
class EdgeCaseTruth(TruthItem):
    condition: str = ""
    expected: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.condition.strip() or not self.expected.strip():
            raise ValueError(f"EdgeCaseTruth {self.key} exige condition e expected")


@dataclass(frozen=True)
class InvariantTruth(TruthItem):
    pass


@dataclass(frozen=True)
class IntegrationTruth(TruthItem):
    direction: str = ""
    protocol: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.direction.strip() or not self.protocol.strip():
            raise ValueError(f"IntegrationTruth {self.key} exige direction e protocol")


@dataclass(frozen=True)
class EntryPointTruth(TruthItem):
    mechanism: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.mechanism.strip():
            raise ValueError(f"EntryPointTruth {self.key} exige mechanism")


@dataclass(frozen=True)
class StateTruth(TruthItem):
    pass


@dataclass(frozen=True)
class TransitionTruth(TruthItem):
    from_state: str = ""
    to_state: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.from_state.strip() or not self.to_state.strip():
            raise ValueError(f"TransitionTruth {self.key} exige from_state e to_state")


@dataclass(frozen=True)
class FailureModeTruth(TruthItem):
    trigger: str = ""
    effect: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.trigger.strip() or not self.effect.strip():
            raise ValueError(f"FailureModeTruth {self.key} exige trigger e effect")


@dataclass(frozen=True)
class ContradictionTruth:
    key: str
    document_claim: str
    code_reality: str
    key_terms: tuple[str, ...]
    code_anchors: tuple[Anchor, ...]
    document_name: str

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("ContradictionTruth.key vazio")
        if not self.code_anchors:
            raise ValueError(f"ContradictionTruth {self.key} sem code_anchors")
        if not self.document_name.strip():
            raise ValueError(f"ContradictionTruth {self.key} sem document_name")


@dataclass(frozen=True)
class RepositoryTruth:
    name: str
    language: str
    business_rules: tuple[RuleTruth, ...] = ()
    edge_cases: tuple[EdgeCaseTruth, ...] = ()
    invariants: tuple[InvariantTruth, ...] = ()
    integrations: tuple[IntegrationTruth, ...] = ()
    entrypoints: tuple[EntryPointTruth, ...] = ()
    states: tuple[StateTruth, ...] = ()
    transitions: tuple[TransitionTruth, ...] = ()
    failure_modes: tuple[FailureModeTruth, ...] = ()
    contradictions: tuple[ContradictionTruth, ...] = ()
    symbols_by_path: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def all_items(self) -> tuple[TruthItem, ...]:
        groups: Sequence[Sequence[TruthItem]] = (
            self.business_rules,
            self.edge_cases,
            self.invariants,
            self.integrations,
            self.entrypoints,
            self.states,
            self.transitions,
            self.failure_modes,
        )
        collected: list[TruthItem] = []
        for group in groups:
            collected.extend(group)
        return tuple(collected)

    def anchor_paths(self) -> tuple[str, ...]:
        paths = {anchor.path for item in self.all_items() for anchor in item.anchors}
        return tuple(sorted(paths))
