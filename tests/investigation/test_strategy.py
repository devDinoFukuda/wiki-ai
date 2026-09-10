from __future__ import annotations

from wiki_ai.investigation.coverage import CoverageState, FrontierItem
from wiki_ai.investigation.objective import Objective, ObjectiveKind, Scope, parse
from wiki_ai.investigation.strategy import (
    PROFILE_SECTIONS,
    DeepAnalysisStrategy,
    Phase,
    StrategyState,
    default_strategy,
)

SYSTEM = parse("analyze repository")


def coverage_with(*capabilities: str, **extra) -> CoverageState:
    return CoverageState(files_total=10, capabilities=tuple(capabilities), **extra)


def test_discovery_comes_first_even_without_any_capability() -> None:
    steps = default_strategy(100).plan(SYSTEM, coverage_with())
    assert [step.phase for step in steps] == [Phase.DISCOVERY]
    assert steps[0].objective.kind is ObjectiveKind.SYSTEM_ANALYSIS


def test_one_capability_step_per_capability_in_deterministic_name_order() -> None:
    steps = default_strategy(100).plan(
        SYSTEM, coverage_with("place order", "Cancel Order", "audit trail")
    )
    assert [step.phase for step in steps] == [
        Phase.DISCOVERY,
        Phase.CAPABILITY,
        Phase.CAPABILITY,
        Phase.CAPABILITY,
        Phase.CONSOLIDATION,
    ]
    assert [step.subject for step in steps[1:4]] == [
        "audit trail",
        "Cancel Order",
        "place order",
    ]


def test_the_order_does_not_depend_on_the_discovery_order() -> None:
    forward = default_strategy(100).plan(SYSTEM, coverage_with("b flow", "a flow"))
    backward = default_strategy(100).plan(SYSTEM, coverage_with("a flow", "b flow"))
    assert [step.subject for step in forward] == [step.subject for step in backward]


def test_each_capability_step_asks_for_the_twenty_sections_of_the_profile() -> None:
    steps = default_strategy(100).plan(SYSTEM, coverage_with("place order"))
    capability = [step for step in steps if step.phase is Phase.CAPABILITY][0]
    assert capability.objective.kind is ObjectiveKind.CAPABILITY_ANALYSIS
    assert capability.sections == PROFILE_SECTIONS
    assert len(PROFILE_SECTIONS) == 20


def test_the_capability_scope_carries_the_symbols_and_paths_of_the_capability() -> None:
    state = coverage_with(
        "place order",
        entrypoints=("place order handler", "cancel handler"),
        files_covered=("src/order/place_order.java", "src/audit/log.java"),
    )
    capability = [
        step
        for step in default_strategy(100).plan(SYSTEM, state)
        if step.phase is Phase.CAPABILITY
    ][0]
    assert "place order" in capability.objective.scope.symbols
    assert "place order handler" in capability.objective.scope.symbols
    assert capability.objective.scope.paths == ("src/order/place_order.java",)


def test_the_budget_is_proportional_and_never_a_fixed_round_ceiling() -> None:
    small = default_strategy(100).plan(SYSTEM, coverage_with("a", "b"))
    large = default_strategy(400).plan(SYSTEM, coverage_with("a", "b"))
    for tight, wide in zip(small, large):
        assert wide.tool_budget >= tight.tool_budget * 4 - 4
        assert wide.tool_budget <= tight.tool_budget * 4 + 4
    assert all(step.tool_budget > 0 for step in large)
    assert sum(step.tool_budget for step in large) <= 400


def test_more_capabilities_split_the_same_budget() -> None:
    two = default_strategy(200).plan(SYSTEM, coverage_with("a", "b"))
    four = default_strategy(200).plan(SYSTEM, coverage_with("a", "b", "c", "d"))
    per_two = [step.tool_budget for step in two if step.phase is Phase.CAPABILITY][0]
    per_four = [step.tool_budget for step in four if step.phase is Phase.CAPABILITY][0]
    assert per_four < per_two


def test_next_walks_the_plan_and_stops_when_the_budget_is_gone() -> None:
    strategy = default_strategy(100)
    state = StrategyState(
        objective=SYSTEM, coverage=coverage_with("place order"), total_budget=100
    )
    first = strategy.next(state)
    assert first is not None and first.phase is Phase.DISCOVERY
    state = state.advanced(first, first.tool_budget)
    second = strategy.next(state)
    assert second is not None and second.subject == "place order"
    exhausted = state.advanced(second, 100)
    assert strategy.next(exhausted) is None


def test_next_never_hands_out_more_budget_than_what_is_left() -> None:
    strategy = default_strategy(100)
    state = StrategyState(
        objective=SYSTEM,
        coverage=coverage_with("place order"),
        total_budget=100,
        spent_budget=95,
    )
    step = strategy.next(state)
    assert step is not None
    assert step.tool_budget <= 5


def test_a_capability_discovered_later_gets_its_own_step() -> None:
    strategy = default_strategy(100)
    state = StrategyState(
        objective=SYSTEM, coverage=coverage_with(), total_budget=100
    )
    discovery = strategy.next(state)
    assert discovery is not None
    state = state.advanced(discovery, 10).with_coverage(coverage_with("late finding"))
    following = strategy.next(state)
    assert following is not None and following.subject == "late finding"


def test_a_non_system_objective_is_never_rewritten_into_phases() -> None:
    objective = Objective(
        kind=ObjectiveKind.TARGETED_REINVESTIGATION,
        scope=Scope(paths=("src/a.java",)),
        goal="reinvestigate src/a.java",
    )
    steps = default_strategy(50).plan(objective, coverage_with("place order"))
    assert len(steps) == 1
    assert steps[0].objective is objective


def test_a_declared_scope_overrides_the_inferred_one() -> None:
    scope = Scope(paths=("cobol/PAYRUN.cbl",))
    strategy = DeepAnalysisStrategy(total_budget=60, scopes={"payroll run": scope})
    capability = [
        step
        for step in strategy.plan(SYSTEM, coverage_with("payroll run"))
        if step.phase is Phase.CAPABILITY
    ][0]
    assert capability.objective.scope == scope


def test_the_consolidation_step_asks_for_cross_capability_relations() -> None:
    steps = default_strategy(100).plan(SYSTEM, coverage_with("a", "b"))
    consolidation = steps[-1]
    assert consolidation.phase is Phase.CONSOLIDATION
    joined = " ".join(consolidation.objective.constraints)
    assert "capabilities to each other" in joined
    assert "flows that cross capability boundaries" in joined


def test_the_frontier_alone_implies_a_budget_when_none_is_declared() -> None:
    state = CoverageState(
        files_total=3,
        capabilities=("place order",),
        frontier=(FrontierItem("call", "OrderRepository save", "unknown target"),),
    )
    steps = DeepAnalysisStrategy().plan(SYSTEM, state)
    assert all(step.tool_budget >= 1 for step in steps)
