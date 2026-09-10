from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from wiki_ai.agent.protocol import ProtocolError, ToolCall, ToolResult
from wiki_ai.agent.session import (
    AgentRun,
    AgentSession,
    Budget,
    BudgetExhausted,
    RunStatus,
    ToolSpec,
)
from wiki_ai.knowledge.model import Entity
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind
from wiki_ai.repository import schemas
from wiki_ai.repository.harness import RepositoryHarness, ScopeFocus, ToolError
from wiki_ai.repository.snapshot import RepositorySnapshot
from wiki_ai.repository.symbols import find_symbols

from wiki_ai.investigation import (
    behavior,
    briefing,
    compaction,
    coverage,
    normalizer,
    profile_gaps,
    verifier,
)
from wiki_ai.investigation.compaction import InvestigationState
from wiki_ai.investigation.coverage import ENTRY_LIKE_SYMBOL_KINDS, CoverageState
from wiki_ai.investigation.evidence import CaptureRegistry
from wiki_ai.investigation.finding import finding_schema, parse_findings
from wiki_ai.investigation.objective import Objective, ObjectiveError, parse
from wiki_ai.investigation.recovery import Failure, RetryPolicy, permanent, recover, transient
from wiki_ai.investigation.strategy import (
    Phase,
    Step,
    Strategy,
    StrategyState,
    default_strategy,
)
from wiki_ai.investigation.tasks import Task, TaskState, acquire, transition

__all__ = [
    "InvestigationOutcomeData",
    "RoundReport",
    "Investigator",
    "SessionProvider",
    "HarnessFactory",
    "DEFAULT_ROUND_BUDGET",
    "DEFAULT_TOTAL_TOOL_CALLS",
]

DEFAULT_ROUND_BUDGET = 40
DEFAULT_TOTAL_TOOL_CALLS = 200
DEFAULT_MAX_SECONDS = 900.0
LEASE_TTL_SECONDS = 300.0
STATE_CHARS = 8000

ABORT_SNAPSHOT_CHANGED = "snapshot_changed_during_run"


@runtime_checkable
class SessionProvider(Protocol):
    def run(self, session: AgentSession) -> AgentRun: ...


@runtime_checkable
class HarnessFactory(Protocol):
    def __call__(
        self, snapshot: RepositorySnapshot, focus: ScopeFocus | None = None
    ) -> RepositoryHarness: ...


def _default_harness(
    snapshot: RepositorySnapshot, focus: ScopeFocus | None = None
) -> RepositoryHarness:
    return RepositoryHarness(snapshot, None, focus)


@dataclass(frozen=True)
class RoundReport:
    round_number: int
    status: str
    tool_calls: int
    findings_received: int
    findings_verified: int
    findings_rejected: int
    entities_written: int
    relations_written: int
    evidence_written: int
    reason: str = ""
    phase: str = ""
    subject: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_number,
            "phase": self.phase,
            "subject": self.subject,
            "status": self.status,
            "tool_calls": self.tool_calls,
            "findings_received": self.findings_received,
            "findings_verified": self.findings_verified,
            "findings_rejected": self.findings_rejected,
            "entities_written": self.entities_written,
            "relations_written": self.relations_written,
            "evidence_written": self.evidence_written,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class InvestigationOutcomeData:
    objective: str
    entities_written: int
    relations_written: int
    evidence_written: int
    unresolved: tuple[str, ...]
    details: Mapping[str, Any]


class _HarnessBridge:
    def __init__(self, harness: RepositoryHarness, registry: CaptureRegistry) -> None:
        self._harness = harness
        self._registry = registry
        self._paths: list[str] = []

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(self._paths)

    def __call__(self, call: ToolCall) -> ToolResult:
        call_id = uuid.uuid4().hex
        try:
            payload = self._harness.invoke(call.name, call.arguments)
        except ToolError as exc:
            return ToolResult(call_id=call_id, ok=False, payload={}, error=str(exc))
        if call.name == schemas.TOOL_EVIDENCE_CAPTURE:
            registered = self._registry.register_payload(payload)
            payload = dict(payload)
            payload["capture_id"] = registered.identifier
        for key in ("path",):
            value = payload.get(key)
            if isinstance(value, str) and value and value not in self._paths:
                self._paths.append(value)
        return ToolResult(call_id=call_id, ok=True, payload=payload)


class Investigator:
    def __init__(
        self,
        harness_factory: HarnessFactory | None = None,
        clock: Callable[[], float] | None = None,
        round_budget: int = DEFAULT_ROUND_BUDGET,
        total_tool_calls: int = DEFAULT_TOTAL_TOOL_CALLS,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        retry_policy: RetryPolicy | None = None,
        max_state_chars: int = STATE_CHARS,
        strategy: Strategy | None = None,
    ) -> None:
        self._harness_factory = harness_factory or _default_harness
        self._clock = clock or time.monotonic
        self._round_budget = round_budget
        self._total_tool_calls = total_tool_calls
        self._max_seconds = max_seconds
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=2)
        self._max_state_chars = max_state_chars
        self._strategy = strategy or default_strategy(total_tool_calls)

    def _build_harness(
        self, snapshot: RepositorySnapshot, focus: ScopeFocus | None
    ) -> RepositoryHarness:
        return self._harness_factory(snapshot, focus)

    def run(
        self,
        objective: str | Objective,
        snapshot: RepositorySnapshot,
        knowledge: KnowledgeRepository,
        provider: SessionProvider,
        namespace: str,
    ) -> InvestigationOutcomeData:
        parsed = _resolve_objective(objective)
        focus = _focus_of(parsed)
        harness = self._build_harness(snapshot, focus)
        registry = CaptureRegistry()
        task = _lease_task(parsed, snapshot, self._clock)
        state = coverage.initial_state(
            files_total=_focused_total(snapshot, focus),
            entrypoints=_discover_entrypoints(snapshot),
            integrations=_discover_integrations(harness),
        )
        compacted = InvestigationState()
        rounds: list[RoundReport] = []
        totals = {"entities": 0, "relations": 0, "evidence": 0, "tool_calls": 0}
        failures: list[str] = []
        gaps: list[str] = []
        flows: list[dict[str, Any]] = []
        aggregate = verifier.VerificationReport()
        abort_reason = ""
        round_number = 0
        strategy_state = StrategyState(
            objective=parsed, coverage=state, total_budget=self._total_tool_calls
        )
        executed: list[dict[str, Any]] = []
        analyzed: set[str] = set()
        files_read: list[str] = []
        outside = 0

        while True:
            remaining = self._total_tool_calls - totals["tool_calls"]
            if remaining <= 0:
                break
            strategy_state = replace(
                strategy_state, coverage=state, spent_budget=totals["tool_calls"]
            )
            step = self._strategy.next(strategy_state)
            if step is None:
                break
            if (
                step.phase is Phase.CAPABILITY
                and step.subject in analyzed
                and state.is_done(remaining)
            ):
                strategy_state = strategy_state.advanced(step, 0)
                continue
            step_calls = 0
            step_rounds = 0
            step_focus = _focus_of(step.objective) or focus
            executed.append(
                {
                    "phase": step.phase.value,
                    "subject": step.subject,
                    "goal": step.objective.goal,
                    "tool_budget": step.tool_budget,
                }
            )
            while step_calls < step.tool_budget:
                remaining = self._total_tool_calls - totals["tool_calls"]
                if remaining <= 0:
                    break
                harness = self._build_harness(snapshot, step_focus)
                if snapshot.digest != harness.snapshot.digest:
                    abort_reason = ABORT_SNAPSHOT_CHANGED
                    break
                round_number += 1
                step_rounds += 1
                if task.state is TaskState.PENDING:
                    task = transition(task, TaskState.READY, "retrying after recovery")
                if task.state is TaskState.READY:
                    task = transition(task, TaskState.LEASED, f"round {round_number}")
                bridge = _HarnessBridge(harness, registry)
                allowance = min(
                    self._round_budget, remaining, step.tool_budget - step_calls
                )
                session = _build_session(
                    step.objective,
                    compacted,
                    harness,
                    snapshot,
                    bridge,
                    allowance,
                    self._max_seconds,
                    round_number,
                    coverage.sections_of(state, step.subject) or step.sections,
                    step.subject,
                    step_focus,
                )
                run, failure = _invoke(provider, session)
                if failure is not None:
                    task = recover(task, failure, self._retry_policy).task
                    failures.append(f"{failure.kind.value}:{failure.code}")
                    if task.state is TaskState.FAILED:
                        abort_reason = failure.code
                        break
                    continue
                totals["tool_calls"] += run.usage.tool_calls
                step_calls += max(1, run.usage.tool_calls)
                stats = harness.stats()
                outside += stats.outside_focus_reads
                for path in stats.files_read:
                    if path not in files_read:
                        files_read.append(path)
                if self._build_harness(snapshot, step_focus).snapshot.digest != snapshot.digest:
                    abort_reason = ABORT_SNAPSHOT_CHANGED
                    break
                findings, rejected = parse_findings(run.findings)
                report = verifier.verify(findings, registry, snapshot)
                aggregate = _merge(aggregate, report)
                written = normalizer.normalize(
                    items=report.verified,
                    knowledge=knowledge,
                    snapshot=snapshot,
                    namespace=namespace,
                    summary=parsed.hash,
                    gaps=report.gaps,
                    captured_at=snapshot.taken_at,
                )
                totals["entities"] += written.entities_written
                totals["relations"] += written.relations_written
                totals["evidence"] += written.evidence_written
                gaps.extend(written.gaps_opened)
                previous_frontier = state.frontier_keys()
                state = coverage.update(state, report, bridge.paths)
                stalled = (
                    run.usage.tool_calls == 0
                    and not run.findings
                    and state.frontier_keys() == previous_frontier
                )
                compacted = compaction.compact(
                    compaction.state_from(
                        round_number, report.verified, state, run.transcript
                    ),
                    self._max_state_chars,
                )
                rounds.append(
                    RoundReport(
                        round_number=round_number,
                        status=run.status.value,
                        tool_calls=run.usage.tool_calls,
                        findings_received=len(run.findings),
                        findings_verified=len(report.verified),
                        findings_rejected=len(rejected) + len(report.discarded),
                        entities_written=written.entities_written,
                        relations_written=written.relations_written,
                        evidence_written=written.evidence_written,
                        reason=run.reason,
                        phase=step.phase.value,
                        subject=step.subject,
                    )
                )
                if run.status is not RunStatus.COMPLETED:
                    abort_reason = run.reason if run.status is RunStatus.FAILED else ""
                    break
                if stalled:
                    break
            if step.phase is Phase.CAPABILITY and step_rounds:
                state, closed = _close_capability(
                    knowledge, state, step, parsed.hash, gaps, flows
                )
                totals["entities"] += closed
                analyzed.add(step.subject)
            strategy_state = strategy_state.advanced(step, step_calls)
            if abort_reason:
                break

        completeness = state.completeness()
        task = _seal(task, abort_reason, parsed.hash)
        unresolved = tuple(
            dict.fromkeys(completeness.outstanding + tuple(state.frontier_keys()))
        )
        details: dict[str, Any] = {
            "objective": parsed.to_dict(),
            "objective_hash": parsed.hash,
            "snapshot_id": snapshot.digest,
            "rounds": [item.to_dict() for item in rounds],
            "round_count": len(rounds),
            "tool_calls": totals["tool_calls"],
            "coverage": completeness.to_dict(),
            "verification": aggregate.to_dict(),
            "gaps_opened": list(dict.fromkeys(gaps)),
            "captures": list(registry.identifiers()),
            "failures": failures,
            "task_state": task.state.value,
            "steps": executed,
            "flows": flows,
            "focus_paths": list(focus.paths) if focus is not None else [],
            "outside_focus_reads": outside,
            "files_read": files_read,
        }
        if abort_reason:
            details["aborted"] = abort_reason
        return InvestigationOutcomeData(
            objective=parsed.goal,
            entities_written=totals["entities"],
            relations_written=totals["relations"],
            evidence_written=totals["evidence"],
            unresolved=unresolved,
            details=details,
        )


def _close_capability(
    knowledge: KnowledgeRepository,
    state: coverage.CoverageState,
    step: Step,
    summary: str,
    gaps: list[str],
    flows: list[dict[str, Any]],
) -> tuple[coverage.CoverageState, int]:
    query = KnowledgeQuery(knowledge)
    capability = _capability_entity(query, step.subject)
    if capability is None:
        return state, 0
    specs = behavior.build_flows(query, capability.id)
    found = profile_gaps.profile_gaps(query, capability.id)
    written = 0
    with knowledge.begin_revision(author=normalizer.AUTHOR, summary=summary) as revision:
        profile_gaps.open_profile_gaps(revision, query, capability.id)
        written += len(behavior.write_flows(revision, query, capability.id, specs))
    for gap in found:
        if gap.question not in gaps:
            gaps.append(gap.question)
    for spec in specs:
        payload = spec.to_dict()
        if payload not in flows:
            flows.append(payload)
    return (
        coverage.with_sections(
            state, step.subject, tuple(gap.section for gap in found)
        ),
        written,
    )


def _capability_entity(query: KnowledgeQuery, subject: str) -> Entity | None:
    lowered = subject.strip().lower()
    for kind in (EntityKind.CAPABILITY, EntityKind.MODULE, EntityKind.FLOW):
        for entity in query.repository.find_entities(kind.value):
            if entity.name.strip().lower() == lowered:
                return entity
    return None


def _merge(
    left: verifier.VerificationReport, right: verifier.VerificationReport
) -> verifier.VerificationReport:
    counts = dict(left.counts)
    for key, value in right.counts.items():
        counts[key] = counts.get(key, 0) + value
    return verifier.VerificationReport(
        verified=left.verified + right.verified,
        discarded=left.discarded + right.discarded,
        gaps=tuple(dict.fromkeys(left.gaps + right.gaps)),
        counts=counts,
    )


def _resolve_objective(objective: str | Objective) -> Objective:
    if isinstance(objective, Objective):
        return objective
    try:
        return parse(objective)
    except ObjectiveError:
        raise


def _focus_of(objective: Objective) -> ScopeFocus | None:
    paths = objective.scope.paths
    if not paths:
        return None
    return ScopeFocus(paths=paths)


def _focused_total(snapshot: RepositorySnapshot, focus: ScopeFocus | None) -> int:
    if focus is None:
        return len(snapshot.files)
    return sum(1 for record in snapshot.files if focus.contains(record.path))


def _lease_task(
    objective: Objective, snapshot: RepositorySnapshot, clock: Callable[[], float]
) -> Task:
    task = Task(
        task_id=uuid.uuid4().hex,
        objective_hash=objective.hash,
        snapshot_hash=snapshot.digest,
        state=TaskState.READY,
    )
    acquire(
        task.task_id,
        normalizer.AUTHOR,
        ttl_seconds=LEASE_TTL_SECONDS,
        clock=clock,
    )
    return transition(task, TaskState.LEASED, "investigation round loop started")


def _seal(task: Task, abort_reason: str, result_hash: str) -> Task:
    if task.state in (TaskState.FAILED, TaskState.BLOCKED, TaskState.SUCCEEDED):
        return task
    if abort_reason:
        return transition(task, TaskState.FAILED, abort_reason)
    return transition(task, TaskState.SUCCEEDED, "investigation finished", result_hash=result_hash)


def _build_session(
    objective: Objective,
    state: InvestigationState,
    harness: RepositoryHarness,
    snapshot: RepositorySnapshot,
    bridge: _HarnessBridge,
    round_calls: int,
    max_seconds: float,
    round_number: int,
    open_sections: Sequence[str] = (),
    subject: str = "",
    focus: ScopeFocus | None = None,
) -> AgentSession:
    specs = {
        spec.name: ToolSpec(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
        )
        for spec in harness.specs()
    }
    text = briefing.build(
        objective,
        state,
        tuple(sorted(specs)),
        round_number,
        open_sections,
        subject,
        focus.paths if focus is not None else (),
    )
    return AgentSession(
        objective=text,
        tools=specs,
        budget=Budget(
            max_tool_calls=max(1, round_calls),
            max_seconds=max_seconds,
        ),
        snapshot_id=snapshot.digest,
        executor=bridge,
        finding_schema=finding_schema(),
    )


def _invoke(
    provider: SessionProvider, session: AgentSession
) -> tuple[AgentRun, None] | tuple[AgentRun, Failure]:
    try:
        run = provider.run(session)
    except BudgetExhausted as exc:
        return session.budget_exhausted(str(exc)), None
    except ProtocolError as exc:
        return session.fail(str(exc)), permanent("protocol_error", str(exc))
    except (OSError, TimeoutError) as exc:
        return session.fail(str(exc)), transient("provider_unavailable", str(exc))
    if not isinstance(run, AgentRun):
        return session.fail("provider returned a foreign result"), permanent(
            "invalid_run", type(run).__name__
        )
    if run.status is RunStatus.FAILED:
        return run, transient("agent_run_failed", run.reason)
    return run, None


def _discover_entrypoints(snapshot: RepositorySnapshot) -> tuple[str, ...]:
    found = find_symbols(
        snapshot,
        None,
        None,
        kinds=ENTRY_LIKE_SYMBOL_KINDS,
        max_results=200,
    )
    return tuple(dict.fromkeys(item.name for item in found))


def _discover_integrations(harness: RepositoryHarness) -> tuple[str, ...]:
    try:
        payload = harness.invoke(schemas.TOOL_DEPENDENCIES, {})
    except ToolError:
        return ()
    names: list[str] = []
    for entry in _entries(payload, "endpoints"):
        value = str(entry.get("url") or entry.get("host") or "")
        if value and value not in names:
            names.append(value)
    for entry in _entries(payload, "messaging"):
        technology = str(entry.get("technology") or "")
        path = str(entry.get("path") or "")
        value = f"{technology} in {path}" if technology and path else technology or path
        if value and value not in names:
            names.append(value)
    return tuple(names)


def _entries(payload: Mapping[str, Any], key: str) -> Sequence[Mapping[str, Any]]:
    raw = payload.get(key)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))
