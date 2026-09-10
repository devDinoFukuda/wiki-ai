from __future__ import annotations

import time
import uuid
from pathlib import Path
from dataclasses import dataclass, replace
from enum import Enum
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
from wiki_ai.knowledge.model import Entity, GraphPolicy
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind
from wiki_ai.repository import schemas
from wiki_ai.repository.harness import RepositoryHarness, ScopeFocus, ToolError
from wiki_ai.repository.snapshot import (
    RepositorySnapshot,
    SnapshotError,
    SnapshotSpec,
    observe,
)
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
from wiki_ai.investigation.finding import finding_schema, parse_findings, with_owner
from wiki_ai.investigation.objective import Objective, ObjectiveError, parse
from wiki_ai.investigation.recovery import (
    Failure,
    RetryPolicy,
    permanent,
    recover,
    stale,
    transient,
)
from wiki_ai.investigation.strategy import (
    Phase,
    Step,
    Strategy,
    StrategyState,
    default_strategy,
)
from wiki_ai.investigation.tasks import (
    TERMINAL_STATES,
    Lease,
    Task,
    TaskState,
    acquire,
    holds,
    renew,
    transition,
)

__all__ = [
    "InvestigationStatus",
    "InvestigationOutcomeData",
    "RoundReport",
    "Investigator",
    "SessionProvider",
    "HarnessFactory",
    "DEFAULT_ROUND_BUDGET",
    "DEFAULT_TOTAL_TOOL_CALLS",
    "ABORT_SNAPSHOT_CHANGED",
    "ABORT_SNAPSHOT_NOT_MATERIALIZED",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_NO_PROVIDER",
    "REASON_COMPLETE",
]


class InvestigationStatus(Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


_TASK_STATE_BY_STATUS: Mapping[InvestigationStatus, TaskState] = {
    InvestigationStatus.COMPLETE: TaskState.SUCCEEDED,
    InvestigationStatus.PARTIAL: TaskState.PARTIAL,
    InvestigationStatus.BLOCKED: TaskState.BLOCKED,
    InvestigationStatus.FAILED: TaskState.FAILED,
}

DEFAULT_ROUND_BUDGET = 40
DEFAULT_TOTAL_TOOL_CALLS = 200
DEFAULT_MAX_SECONDS = 900.0
LEASE_TTL_SECONDS = 300.0
STATE_CHARS = 8000

ABORT_SNAPSHOT_CHANGED = "snapshot_changed_during_run"
ABORT_SNAPSHOT_NOT_MATERIALIZED = "snapshot_not_materialized"
REASON_BUDGET_EXHAUSTED = "budget_exhausted_with_open_frontier"
REASON_NO_PROVIDER = "agent_provider_unavailable"
REASON_COMPLETE = "objective_covered"


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
    status: InvestigationStatus = InvestigationStatus.COMPLETE
    reason: str = REASON_COMPLETE


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
        provider: SessionProvider | None,
        namespace: str,
        spec: SnapshotSpec | None = None,
    ) -> InvestigationOutcomeData:
        parsed = _resolve_objective(objective)
        if provider is None:
            return _aborted(
                parsed, snapshot, InvestigationStatus.BLOCKED, REASON_NO_PROVIDER
            )
        if not snapshot.materialized:
            return _aborted(
                parsed,
                snapshot,
                InvestigationStatus.FAILED,
                ABORT_SNAPSHOT_NOT_MATERIALIZED,
            )
        focus = _focus_of(parsed)
        harness = self._build_harness(snapshot, focus)
        registry = CaptureRegistry()
        task, lease = _lease_task(parsed, snapshot, self._clock)
        state = coverage.initial_state(
            files_total=_focused_total(snapshot, focus),
            entrypoints=_discover_entrypoints(snapshot),
            integrations=_discover_integrations(harness),
        )
        compacted = InvestigationState()
        rounds: list[RoundReport] = []
        totals = {"entities": 0, "relations": 0, "evidence": 0, "tool_calls": 0}
        failures: list[str] = []
        fingerprints: list[str] = []
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
        exhausted = False
        watched = spec if spec is not None else _spec_of(snapshot)

        while True:
            remaining = self._total_tool_calls - totals["tool_calls"]
            if remaining <= 0:
                exhausted = True
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
                    exhausted = True
                    break
                if _source_changed(watched, snapshot):
                    failures.append(f"stale:{ABORT_SNAPSHOT_CHANGED}")
                    fingerprints.append(
                        stale(ABORT_SNAPSHOT_CHANGED, snapshot.digest).fingerprint
                    )
                    abort_reason = ABORT_SNAPSHOT_CHANGED
                    break
                harness = self._build_harness(snapshot, step_focus)
                round_number += 1
                step_rounds += 1
                lease = _keep_lease(lease, self._clock)
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
                    fingerprints.append(failure.fingerprint)
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
                if _source_changed(watched, snapshot):
                    abort_reason = ABORT_SNAPSHOT_CHANGED
                    break
                parsed_findings, rejected = parse_findings(run.findings)
                findings = tuple(
                    with_owner(item, step.subject) for item in parsed_findings
                )
                report = verifier.verify(findings, registry, snapshot, namespace)
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
                state = coverage.update(
                    state,
                    report,
                    bridge.paths,
                    unresolved_relations=written.unresolved_relations,
                    gaps=written.gaps_opened,
                )
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
        unresolved = tuple(
            dict.fromkeys(completeness.outstanding + tuple(state.frontier_keys()))
        )
        blocking = tuple(item for item in gaps if item in set(unresolved))
        status, reason = _status_of(
            abort_reason,
            exhausted,
            unresolved,
            blocking,
            totals["tool_calls"],
            self._total_tool_calls,
        )
        task = _seal(task, status, reason, parsed.hash)
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
            "failure_fingerprints": list(dict.fromkeys(fingerprints)),
            "task_state": task.state.value,
            "steps": executed,
            "flows": flows,
            "focus_paths": list(focus.paths) if focus is not None else [],
            "outside_focus_reads": outside,
            "files_read": files_read,
            "status": status.value,
            "reason": reason,
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
            status=status,
            reason=reason,
        )


def _close_capability(
    knowledge: KnowledgeRepository,
    state: coverage.CoverageState,
    step: Step,
    summary: str,
    gaps: list[str],
    flows: list[dict[str, Any]],
) -> tuple[coverage.CoverageState, int]:
    query = KnowledgeQuery(knowledge, policy=GraphPolicy.SUPPORTED_AND_INFERRED)
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
) -> tuple[Task, Lease]:
    task = Task(
        task_id=uuid.uuid4().hex,
        objective_hash=objective.hash,
        snapshot_hash=snapshot.digest,
        state=TaskState.READY,
    )
    lease = acquire(
        task.task_id,
        normalizer.AUTHOR,
        ttl_seconds=LEASE_TTL_SECONDS,
        clock=clock,
    )
    leased = transition(task, TaskState.LEASED, "investigation round loop started")
    return leased, lease


def _keep_lease(lease: Lease, clock: Callable[[], float]) -> Lease:
    if holds(lease, lease.token, clock()):
        return renew(lease, lease.token, ttl_seconds=LEASE_TTL_SECONDS, clock=clock)
    return acquire(
        lease.task_id,
        normalizer.AUTHOR,
        ttl_seconds=LEASE_TTL_SECONDS,
        clock=clock,
        current=lease,
        token=lease.token,
    )


def _status_of(
    abort_reason: str,
    exhausted: bool,
    unresolved: Sequence[str],
    blocking: Sequence[str],
    spent: int,
    budget: int,
) -> tuple[InvestigationStatus, str]:
    if abort_reason:
        return InvestigationStatus.FAILED, abort_reason
    if exhausted and (unresolved or blocking):
        return InvestigationStatus.PARTIAL, REASON_BUDGET_EXHAUSTED
    if spent >= budget and (unresolved or blocking):
        return InvestigationStatus.PARTIAL, REASON_BUDGET_EXHAUSTED
    if unresolved or blocking:
        return InvestigationStatus.PARTIAL, "open_frontier_or_blocking_gaps"
    return InvestigationStatus.COMPLETE, REASON_COMPLETE


def _seal(
    task: Task, status: InvestigationStatus, reason: str, result_hash: str
) -> Task:
    if task.state in TERMINAL_STATES:
        return task
    target = _TASK_STATE_BY_STATUS[status]
    return transition(task, target, reason, result_hash=result_hash)


def _spec_of(snapshot: RepositorySnapshot) -> SnapshotSpec:
    return SnapshotSpec(root=Path(snapshot.root), excludes=_untracked_roots(snapshot))


def _untracked_roots(snapshot: RepositorySnapshot) -> tuple[str, ...]:
    try:
        present = observe(SnapshotSpec(root=Path(snapshot.root))).file_map()
    except SnapshotError:
        return ()
    tracked = snapshot.file_map()
    roots: list[str] = []
    for path in present:
        if path in tracked:
            continue
        head = path.split("/", 1)[0]
        if head and head not in roots and not any(
            item.startswith(head + "/") or item == head for item in tracked
        ):
            roots.append(head)
    return tuple(roots)


def _source_changed(spec: SnapshotSpec, snapshot: RepositorySnapshot) -> bool:
    try:
        return observe(spec).digest != snapshot.digest
    except SnapshotError:
        return True


def _aborted(
    objective: Objective,
    snapshot: RepositorySnapshot,
    status: InvestigationStatus,
    reason: str,
) -> InvestigationOutcomeData:
    return InvestigationOutcomeData(
        objective=objective.goal,
        entities_written=0,
        relations_written=0,
        evidence_written=0,
        unresolved=(),
        details={
            "objective": objective.to_dict(),
            "objective_hash": objective.hash,
            "snapshot_id": snapshot.digest,
            "rounds": [],
            "round_count": 0,
            "tool_calls": 0,
            "status": status.value,
            "reason": reason,
            "aborted": reason,
        },
        status=status,
        reason=reason,
    )


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
