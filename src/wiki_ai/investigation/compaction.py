from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from wiki_ai.agent.session import TranscriptEntry

from wiki_ai.investigation.coverage import CoverageState
from wiki_ai.investigation.verifier import VerifiedFinding

__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_CALLS",
    "FindingSummary",
    "CallSummary",
    "InvestigationState",
    "summarize_findings",
    "summarize_calls",
    "compact",
    "render",
]

DEFAULT_MAX_CHARS = 8000
DEFAULT_MAX_CALLS = 12
_ARGUMENT_CHARS = 120


@dataclass(frozen=True)
class FindingSummary:
    type: str
    subject: str
    confidence: str

    def render(self) -> str:
        return f"{self.type} | {self.subject} | {self.confidence}"


@dataclass(frozen=True)
class CallSummary:
    ordinal: int
    tool: str
    arguments: str
    ok: bool

    def render(self) -> str:
        status = "ok" if self.ok else "error"
        return f"#{self.ordinal} {self.tool}({self.arguments}) -> {status}"


@dataclass(frozen=True)
class InvestigationState:
    round_number: int = 0
    accepted: tuple[FindingSummary, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    frontier: tuple[str, ...] = ()
    recent_calls: tuple[CallSummary, ...] = ()
    unresolved: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    coverage: Mapping[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (
            self.accepted
            or self.evidence_ids
            or self.frontier
            or self.recent_calls
            or self.unresolved
            or self.gaps
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_number,
            "accepted": [item.render() for item in self.accepted],
            "evidence_ids": list(self.evidence_ids),
            "frontier": list(self.frontier),
            "recent_calls": [item.render() for item in self.recent_calls],
            "unresolved": list(self.unresolved),
            "gaps": list(self.gaps),
            "coverage": dict(self.coverage),
        }


def summarize_findings(items: Sequence[VerifiedFinding]) -> tuple[FindingSummary, ...]:
    return tuple(
        FindingSummary(
            type=item.finding.type.value,
            subject=item.finding.subject,
            confidence=item.confidence.value,
        )
        for item in items
    )


def _arguments(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in sorted(payload):
        value = payload[key]
        text = str(value)
        if len(text) > 40:
            text = text[:37] + "..."
        parts.append(f"{key}={text}")
    joined = ", ".join(parts)
    if len(joined) > _ARGUMENT_CHARS:
        joined = joined[: _ARGUMENT_CHARS - 3] + "..."
    return joined


def summarize_calls(
    transcript: Sequence[TranscriptEntry], max_calls: int = DEFAULT_MAX_CALLS
) -> tuple[CallSummary, ...]:
    tail = tuple(transcript)[-max_calls:] if max_calls > 0 else ()
    return tuple(
        CallSummary(
            ordinal=entry.ordinal,
            tool=entry.call.name,
            arguments=_arguments(entry.call.arguments),
            ok=entry.result.ok,
        )
        for entry in tail
    )


def compact(
    state: InvestigationState, max_chars: int = DEFAULT_MAX_CHARS
) -> InvestigationState:
    if max_chars <= 0:
        raise ValueError(f"max_chars must be positive: {max_chars!r}")
    current = state
    while len(render(current)) > max_chars:
        trimmed = _shrink(current)
        if trimmed is None:
            return current
        current = trimmed
    return current


def _shrink(state: InvestigationState) -> InvestigationState | None:
    if state.recent_calls:
        return _replace(state, recent_calls=state.recent_calls[1:])
    if len(state.accepted) > 1:
        return _replace(state, accepted=state.accepted[: len(state.accepted) - 1])
    if len(state.evidence_ids) > 1:
        return _replace(state, evidence_ids=state.evidence_ids[: len(state.evidence_ids) - 1])
    if len(state.gaps) > 1:
        return _replace(state, gaps=state.gaps[: len(state.gaps) - 1])
    if len(state.unresolved) > 1:
        return _replace(state, unresolved=state.unresolved[: len(state.unresolved) - 1])
    if len(state.frontier) > 1:
        return _replace(state, frontier=state.frontier[: len(state.frontier) - 1])
    return None


def _replace(state: InvestigationState, **changes: Any) -> InvestigationState:
    payload: dict[str, Any] = {
        "round_number": state.round_number,
        "accepted": state.accepted,
        "evidence_ids": state.evidence_ids,
        "frontier": state.frontier,
        "recent_calls": state.recent_calls,
        "unresolved": state.unresolved,
        "gaps": state.gaps,
        "coverage": state.coverage,
    }
    payload.update(changes)
    return InvestigationState(**payload)


def render(state: InvestigationState) -> str:
    if state.is_empty():
        return ""
    lines = [f"State after round {state.round_number}:"]
    lines.extend(_section("Accepted findings", [item.render() for item in state.accepted]))
    lines.extend(_section("Evidence ids", list(state.evidence_ids)))
    lines.extend(_section("Unresolved frontier", list(state.frontier)))
    lines.extend(_section("Findings without evidence", list(state.unresolved)))
    lines.extend(_section("Open gaps", list(state.gaps)))
    lines.extend(_section("Recent tool calls", [item.render() for item in state.recent_calls]))
    return "\n".join(lines)


def _section(title: str, values: Sequence[str]) -> list[str]:
    if not values:
        return []
    return [f"{title}:"] + [f"- {value}" for value in values]


def state_from(
    round_number: int,
    verified: Sequence[VerifiedFinding],
    coverage: CoverageState,
    transcript: Sequence[TranscriptEntry],
    max_calls: int = DEFAULT_MAX_CALLS,
) -> InvestigationState:
    evidence_ids: list[str] = []
    for item in verified:
        for resolved in item.evidence:
            if resolved.identifier not in evidence_ids:
                evidence_ids.append(resolved.identifier)
    report = coverage.completeness()
    return InvestigationState(
        round_number=round_number,
        accepted=summarize_findings(verified),
        evidence_ids=tuple(evidence_ids),
        frontier=tuple(
            f"{item.kind}: {item.subject} ({item.reason})" for item in coverage.frontier
        ),
        recent_calls=summarize_calls(transcript, max_calls),
        unresolved=report.missing_evidence,
        gaps=report.explicit_gaps,
        coverage=report.to_dict(),
    )
