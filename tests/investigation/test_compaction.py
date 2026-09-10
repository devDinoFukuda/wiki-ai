from __future__ import annotations

from pathlib import Path

import pytest

from tests.investigation.fixtures_snapshots import java_repo

from wiki_ai.agent.protocol import ToolCall, ToolResult
from wiki_ai.agent.session import TranscriptEntry
from wiki_ai.knowledge.taxonomy import EntityKind
from wiki_ai.repository.evidence import capture
from wiki_ai.investigation.compaction import (
    CallSummary,
    FindingSummary,
    InvestigationState,
    compact,
    render,
    state_from,
    summarize_calls,
    summarize_findings,
)
from wiki_ai.investigation.coverage import CoverageState, update
from wiki_ai.investigation.evidence import CaptureRegistry
from wiki_ai.investigation.finding import EvidenceRef, Finding
from wiki_ai.investigation.verifier import verify

SERVICE = "src/main/java/com/acme/order/OrderService.java"


def transcript(count: int) -> tuple[TranscriptEntry, ...]:
    return tuple(
        TranscriptEntry(
            ordinal=index,
            call=ToolCall(name="repo.read", arguments={"path": SERVICE, "start": index}),
            result=ToolResult(call_id=f"c{index}", ok=True, payload={"text": "x" * 4000}),
            tokens=0,
        )
        for index in range(count)
    )


def test_summaries_drop_excerpts_and_payloads() -> None:
    calls = summarize_calls(transcript(3))
    rendered = "\n".join(item.render() for item in calls)
    assert "x" * 100 not in rendered
    assert calls[0].tool == "repo.read"


def test_only_the_last_n_calls_survive() -> None:
    assert len(summarize_calls(transcript(30), max_calls=5)) == 5
    assert summarize_calls(transcript(30), max_calls=5)[0].ordinal == 25


def test_findings_are_reduced_to_subject_type_and_confidence(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    registry = CaptureRegistry()
    registry.register(capture(snapshot, SERVICE, 10, 12))
    finding = Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService place",
        statement="OrderService place delegates to repository save",
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
    )
    report = verify([finding], registry, snapshot)
    summaries = summarize_findings(report.verified)
    assert summaries == (
        FindingSummary("capability", "OrderService place", "supported"),
    )


def test_state_from_keeps_evidence_ids_and_frontier(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    registry = CaptureRegistry()
    registered = registry.register(capture(snapshot, SERVICE, 10, 12))
    finding = Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService place",
        statement="OrderService place delegates to repository save",
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
    )
    report = verify([finding], registry, snapshot)
    coverage_state = update(CoverageState(files_total=7), report)
    state = state_from(1, report.verified, coverage_state, transcript(2))
    assert state.evidence_ids == (registered.identifier,)
    assert state.round_number == 1
    assert state.coverage["files_total"] == 7


def test_compaction_respects_the_char_budget() -> None:
    state = InvestigationState(
        round_number=2,
        accepted=tuple(
            FindingSummary("capability", f"subject {index}", "supported")
            for index in range(50)
        ),
        evidence_ids=tuple(f"cap_{index}" for index in range(50)),
        frontier=tuple(f"call: target {index}" for index in range(50)),
        recent_calls=tuple(
            CallSummary(index, "repo.read", "path=a/b.java", True) for index in range(20)
        ),
    )
    compacted = compact(state, max_chars=400)
    assert len(render(compacted)) <= 400
    assert compacted.accepted
    assert compacted.frontier


def test_compaction_drops_calls_before_findings() -> None:
    state = InvestigationState(
        round_number=1,
        accepted=(FindingSummary("capability", "a", "supported"),),
        recent_calls=tuple(
            CallSummary(index, "repo.read", "path=a", True) for index in range(10)
        ),
    )
    compacted = compact(state, max_chars=120)
    assert compacted.accepted == state.accepted
    assert len(compacted.recent_calls) < len(state.recent_calls)


def test_compaction_rejects_a_non_positive_budget() -> None:
    with pytest.raises(ValueError):
        compact(InvestigationState(), max_chars=0)


def test_empty_state_renders_to_nothing() -> None:
    assert render(InvestigationState()) == ""
    assert InvestigationState().is_empty()
