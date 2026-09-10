from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from wiki_ai.investigation.coverage import CoverageState
from wiki_ai.investigation.objective import Objective, ObjectiveKind, Scope

__all__ = [
    "PROFILE_SECTIONS",
    "Phase",
    "Step",
    "StrategyState",
    "Strategy",
    "DeepAnalysisStrategy",
    "default_strategy",
    "DISCOVERY_SHARE",
    "CONSOLIDATION_SHARE",
    "MIN_STEP_BUDGET",
]

PROFILE_SECTIONS: tuple[str, ...] = (
    "purpose",
    "entrypoints",
    "inputs",
    "outputs",
    "preconditions",
    "rules",
    "invariants",
    "decisions",
    "states",
    "persistence",
    "integrations",
    "events",
    "failures",
    "retries",
    "fallbacks",
    "timeouts",
    "idempotency",
    "edge_cases",
    "tests",
    "dependencies",
)

DISCOVERY_SHARE = 0.3
CONSOLIDATION_SHARE = 0.15
MIN_STEP_BUDGET = 1

DISCOVERY_GOAL = (
    "discover the systems, modules, capabilities, entry points and integrations "
    "implemented in this snapshot"
)
CONSOLIDATION_GOAL = (
    "consolidate cross capability relations and end to end flows over what was "
    "already investigated"
)
_CAPABILITY_GOAL = "reconstruct the implemented behaviour of capability {name}"

DISCOVERY_CONSTRAINTS: tuple[str, ...] = (
    "name every capability you find, even when its behaviour is still unknown",
    "bind each entry point to the capability it serves with a typed relation",
    "do not describe behaviour in depth yet; the next step does that per capability",
)
CONSOLIDATION_CONSTRAINTS: tuple[str, ...] = (
    "relate capabilities to each other with calls, triggers, depends_on and publishes",
    "close flows that cross capability boundaries end to end",
    "declare a gap for every crossing you cannot decide from executable code",
)
_CAPABILITY_CONSTRAINTS: tuple[str, ...] = (
    "answer only what the executable code of this capability decides",
    "every section you answer needs its own evidence capture",
    "declare a gap for each section the code does not answer",
)


class Phase(str, Enum):
    DISCOVERY = "discovery"
    CAPABILITY = "capability"
    CONSOLIDATION = "consolidation"


_PHASE_BY_KIND: Mapping[ObjectiveKind, Phase] = {
    ObjectiveKind.CAPABILITY_ANALYSIS: Phase.CAPABILITY,
    ObjectiveKind.IMPACT_ANALYSIS: Phase.CONSOLIDATION,
    ObjectiveKind.QUESTION: Phase.DISCOVERY,
    ObjectiveKind.TARGETED_REINVESTIGATION: Phase.DISCOVERY,
}


@dataclass(frozen=True)
class Step:
    phase: Phase
    objective: Objective
    tool_budget: int
    subject: str = ""
    sections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_budget", max(MIN_STEP_BUDGET, int(self.tool_budget)))
        object.__setattr__(self, "sections", tuple(self.sections))

    @property
    def key(self) -> str:
        return f"{self.phase.value}::{self.subject.strip().lower()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "subject": self.subject,
            "objective": self.objective.to_dict(),
            "tool_budget": self.tool_budget,
            "sections": list(self.sections),
        }


@dataclass(frozen=True)
class StrategyState:
    objective: Objective
    coverage: CoverageState
    total_budget: int
    spent_budget: int = 0
    completed: tuple[str, ...] = ()
    plan: tuple[Step, ...] = ()

    @property
    def remaining_budget(self) -> int:
        return max(0, self.total_budget - self.spent_budget)

    def advanced(self, step: Step, spent: int) -> "StrategyState":
        completed = self.completed
        if step.key not in completed:
            completed = completed + (step.key,)
        return replace(
            self,
            spent_budget=self.spent_budget + max(0, spent),
            completed=completed,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_budget": self.total_budget,
            "spent_budget": self.spent_budget,
            "remaining_budget": self.remaining_budget,
            "completed": list(self.completed),
            "plan": [step.to_dict() for step in self.plan],
        }


@runtime_checkable
class Strategy(Protocol):
    def plan(self, objective: Objective, coverage: CoverageState) -> tuple[Step, ...]: ...

    def next(self, state: StrategyState) -> Step | None: ...


@dataclass(frozen=True)
class DeepAnalysisStrategy:
    total_budget: int = 0
    discovery_share: float = DISCOVERY_SHARE
    consolidation_share: float = CONSOLIDATION_SHARE
    sections: tuple[str, ...] = PROFILE_SECTIONS
    scopes: Mapping[str, Scope] = field(default_factory=dict)

    def plan(self, objective: Objective, coverage: CoverageState) -> tuple[Step, ...]:
        budget = self.total_budget if self.total_budget > 0 else _implied_budget(coverage)
        if objective.kind is not ObjectiveKind.SYSTEM_ANALYSIS:
            return (
                Step(
                    phase=_PHASE_BY_KIND.get(objective.kind, Phase.CAPABILITY),
                    objective=objective,
                    tool_budget=budget,
                    subject="",
                    sections=self.sections
                    if objective.kind is ObjectiveKind.CAPABILITY_ANALYSIS
                    else (),
                ),
            )
        capabilities = _ordered_capabilities(coverage)
        steps: list[Step] = [
            self._discovery_step(objective, budget, bool(capabilities))
        ]
        per_capability = _capability_budget(budget, len(capabilities), self)
        for name in capabilities:
            steps.append(
                self._capability_step(objective, coverage, name, per_capability)
            )
        if capabilities:
            steps.append(self._consolidation_step(objective, budget))
        return tuple(steps)

    def next(self, state: StrategyState) -> Step | None:
        if state.remaining_budget <= 0:
            return None
        for step in self.plan(state.objective, state.coverage):
            if step.key in state.completed:
                continue
            return replace(step, tool_budget=min(step.tool_budget, state.remaining_budget))
        return None

    def _discovery_step(
        self, objective: Objective, budget: int, capabilities_known: bool
    ) -> Step:
        share = (
            int(budget * self.discovery_share)
            if capabilities_known
            else int(budget * max(0.0, 1.0 - self.consolidation_share))
        )
        return Step(
            phase=Phase.DISCOVERY,
            objective=Objective(
                kind=ObjectiveKind.SYSTEM_ANALYSIS,
                scope=objective.scope,
                goal=objective.goal,
                constraints=objective.constraints
                + (f"this step: {DISCOVERY_GOAL}",)
                + DISCOVERY_CONSTRAINTS,
                text=objective.text,
            ),
            tool_budget=share,
            subject="",
        )

    def _capability_step(
        self,
        objective: Objective,
        coverage: CoverageState,
        name: str,
        budget: int,
    ) -> Step:
        return Step(
            phase=Phase.CAPABILITY,
            objective=Objective(
                kind=ObjectiveKind.CAPABILITY_ANALYSIS,
                scope=self._scope_for(objective, coverage, name),
                goal=_CAPABILITY_GOAL.format(name=name),
                constraints=objective.constraints + _CAPABILITY_CONSTRAINTS,
                text=objective.text,
            ),
            tool_budget=budget,
            subject=name,
            sections=self.sections,
        )

    def _consolidation_step(self, objective: Objective, budget: int) -> Step:
        share = int(budget * self.consolidation_share)
        return Step(
            phase=Phase.CONSOLIDATION,
            objective=Objective(
                kind=ObjectiveKind.SYSTEM_ANALYSIS,
                scope=objective.scope,
                goal=CONSOLIDATION_GOAL,
                constraints=objective.constraints + CONSOLIDATION_CONSTRAINTS,
                text=objective.text,
            ),
            tool_budget=share,
            subject="",
        )

    def _scope_for(
        self, objective: Objective, coverage: CoverageState, name: str
    ) -> Scope:
        declared = self.scopes.get(name)
        if declared is not None:
            return declared
        symbols = _symbols_for(coverage, name)
        paths = _paths_for(coverage, name)
        return Scope(
            paths=paths or objective.scope.paths,
            symbols=(name,) + symbols,
            entities=objective.scope.entities,
        )


def default_strategy(total_budget: int) -> DeepAnalysisStrategy:
    return DeepAnalysisStrategy(total_budget=total_budget)


def _implied_budget(coverage: CoverageState) -> int:
    return max(MIN_STEP_BUDGET, len(coverage.frontier) + len(coverage.entrypoints))


def _capability_budget(
    budget: int, capability_count: int, strategy: DeepAnalysisStrategy
) -> int:
    if capability_count <= 0:
        return MIN_STEP_BUDGET
    reserved = strategy.discovery_share + strategy.consolidation_share
    available = int(budget * max(0.0, 1.0 - reserved))
    return max(MIN_STEP_BUDGET, available // capability_count)


def _ordered_capabilities(coverage: CoverageState) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(coverage.capabilities), key=_sort_key))


def _sort_key(name: str) -> tuple[str, str]:
    return (name.strip().lower(), name)


def _symbols_for(coverage: CoverageState, name: str) -> tuple[str, ...]:
    lowered = name.strip().lower()
    found = [
        symbol
        for symbol in coverage.entrypoints
        if lowered in symbol.strip().lower() or symbol.strip().lower() in lowered
    ]
    return tuple(dict.fromkeys(found))


def _paths_for(coverage: CoverageState, name: str) -> tuple[str, ...]:
    tokens = tuple(token for token in _tokens(name) if len(token) > 3)
    if not tokens:
        return ()
    found = [
        path
        for path in coverage.files_covered
        if any(token in path.lower() for token in tokens)
    ]
    return tuple(dict.fromkeys(found))


def _tokens(name: str) -> tuple[str, ...]:
    cleaned = "".join(char if char.isalnum() else " " for char in name.lower())
    return tuple(part for part in cleaned.split() if part)


def sections_of(steps: Sequence[Step]) -> tuple[str, ...]:
    found: list[str] = []
    for step in steps:
        for section in step.sections:
            if section not in found:
                found.append(section)
    return tuple(found)
