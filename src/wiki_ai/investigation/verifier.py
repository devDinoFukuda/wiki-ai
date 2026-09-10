from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from wiki_ai.knowledge.evidence import CodeContent
from wiki_ai.knowledge.model import Confidence
from wiki_ai.repository.evidence import EvidenceCapture, verify as verify_capture
from wiki_ai.repository.snapshot import RepositorySnapshot

from wiki_ai.investigation.evidence import CaptureRegistry, capture_id, detect_content
from wiki_ai.investigation.finding import EvidenceRef, Finding

__all__ = [
    "Rejection",
    "ResolvedEvidence",
    "VerifiedFinding",
    "VerificationReport",
    "verify",
]

MIN_TOKEN_LENGTH = 4
_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")
_NEGATIONS = ("not ", "never ", "no ", "não ", "nunca ", "sem ")
_OPPOSITE_PAIRS: tuple[tuple[str, str], ...] = (
    ("accept", "reject"),
    ("allow", "deny"),
    ("enable", "disable"),
    ("create", "delete"),
    ("commit", "rollback"),
    ("persist", "discard"),
    ("retry", "abort"),
    ("publish", "suppress"),
    ("aceita", "rejeita"),
    ("permite", "bloqueia"),
)


class Rejection(Enum):
    EVIDENCE_UNRESOLVED = "evidence_unresolved"
    EVIDENCE_TAMPERED = "evidence_tampered"
    STATEMENT_UNSUPPORTED_BY_EXCERPT = "statement_unsupported_by_excerpt"
    NO_EXECUTABLE_EVIDENCE = "no_executable_evidence"
    NO_EVIDENCE = "no_evidence"
    CONTRADICTED_BY_PEER = "contradicted_by_peer"


@dataclass(frozen=True)
class ResolvedEvidence:
    ref: EvidenceRef
    identifier: str
    capture: EvidenceCapture
    content: CodeContent

    @property
    def executable(self) -> bool:
        return self.content in (CodeContent.EXECUTABLE, CodeContent.CONFIG_VALUE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.identifier,
            "locator": self.capture.locator(),
            "content": self.content.value,
        }


@dataclass(frozen=True)
class VerifiedFinding:
    finding: Finding
    confidence: Confidence
    evidence: tuple[ResolvedEvidence, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def subject(self) -> str:
        return self.finding.subject

    @property
    def stable_key(self) -> str:
        return self.finding.stable_key

    @property
    def persistable(self) -> bool:
        return Rejection.EVIDENCE_TAMPERED not in self.rejections

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.finding.type.value,
            "subject": self.finding.subject,
            "confidence": self.confidence.value,
            "evidence": [item.to_dict() for item in self.evidence],
            "rejections": [item.value for item in self.rejections],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class VerificationReport:
    verified: tuple[VerifiedFinding, ...] = ()
    discarded: tuple[VerifiedFinding, ...] = ()
    gaps: tuple[str, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)

    def by_confidence(self, confidence: Confidence) -> tuple[VerifiedFinding, ...]:
        return tuple(item for item in self.verified if item.confidence is confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": [item.to_dict() for item in self.verified],
            "discarded": [item.to_dict() for item in self.discarded],
            "gaps": list(self.gaps),
            "counts": dict(self.counts),
        }


def _tokens(text: str) -> set[str]:
    return {
        match.group(0).lower()
        for match in _TOKEN_PATTERN.finditer(text)
        if len(match.group(0)) >= MIN_TOKEN_LENGTH
    }


def _split_identifiers(tokens: set[str]) -> set[str]:
    expanded = set(tokens)
    for token in tokens:
        for piece in re.split(r"[_\s]+", token):
            if len(piece) >= MIN_TOKEN_LENGTH:
                expanded.add(piece.lower())
        for match in re.finditer(r"[A-Z]?[a-z]{3,}", token):
            expanded.add(match.group(0).lower())
    return expanded


def _lexically_grounded(finding: Finding, resolved: Sequence[ResolvedEvidence]) -> bool:
    claim = _split_identifiers(_tokens(f"{finding.subject} {finding.statement}"))
    if not claim:
        return False
    for item in resolved:
        haystack = _split_identifiers(
            _tokens(item.capture.excerpt + " " + item.capture.path.replace("/", " "))
        )
        symbol = item.capture.symbol or ""
        if symbol:
            haystack |= _split_identifiers(_tokens(symbol))
        if claim & haystack:
            return True
    return False


def _resolve(
    ref: EvidenceRef, registry: CaptureRegistry, snapshot: RepositorySnapshot
) -> tuple[ResolvedEvidence | None, Rejection | None, str]:
    capture = (
        registry.get(ref.capture_id)
        if ref.capture_id
        else registry.find(ref.path, ref.line_start, ref.line_end, ref.symbol)
    )
    if capture is None:
        return (
            None,
            Rejection.EVIDENCE_UNRESOLVED,
            f"evidence reference {ref.key} was never captured in this run",
        )
    if not verify_capture(snapshot, capture):
        return (
            None,
            Rejection.EVIDENCE_TAMPERED,
            f"evidence {ref.key} does not match snapshot {snapshot.digest[:12]}",
        )
    content = detect_content(capture.path, capture.excerpt)
    return ResolvedEvidence(ref, capture_id(capture), capture, content), None, ""


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def _negated(text: str) -> bool:
    lowered = " " + _normalized(text) + " "
    return any(marker in lowered for marker in _NEGATIONS)


def _opposite_effects(left: Finding, right: Finding) -> bool:
    left_effects = _normalized(" ".join(left.effects) or left.statement)
    right_effects = _normalized(" ".join(right.effects) or right.statement)
    if not left_effects or not right_effects:
        return False
    for first, second in _OPPOSITE_PAIRS:
        if (first in left_effects and second in right_effects) or (
            second in left_effects and first in right_effects
        ):
            return True
    if left_effects == right_effects:
        return False
    return _negated(left_effects) != _negated(right_effects) and bool(
        _split_identifiers(_tokens(left_effects)) & _split_identifiers(_tokens(right_effects))
    )


def _declared_contradiction(left: Finding, right: Finding) -> bool:
    targets = {item.strip().lower() for item in left.contradicts}
    targets |= {item.strip().lower() for item in right.contradicts}
    return (
        left.subject.strip().lower() in targets
        or right.subject.strip().lower() in targets
        or left.stable_key in targets
        or right.stable_key in targets
    )


def _contradiction_groups(findings: Sequence[Finding]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.subject.strip().lower(), []).append(finding)
    flagged: dict[str, list[str]] = {}
    for subject, group in grouped.items():
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                if not (
                    _declared_contradiction(left, right) or _opposite_effects(left, right)
                ):
                    continue
                message = (
                    f"contradictory statements about {subject!r}: "
                    f"{left.statement or left.subject!r} versus "
                    f"{right.statement or right.subject!r}"
                )
                for finding in (left, right):
                    entries = flagged.setdefault(finding.stable_key, [])
                    if message not in entries:
                        entries.append(message)
    return {key: tuple(values) for key, values in flagged.items()}


def verify(
    findings: Sequence[Finding],
    registry: CaptureRegistry,
    snapshot: RepositorySnapshot,
) -> VerificationReport:
    contradictions = _contradiction_groups(findings)
    verified: list[VerifiedFinding] = []
    discarded: list[VerifiedFinding] = []
    gaps: list[str] = []
    counts: dict[str, int] = {}
    for finding in findings:
        resolved: list[ResolvedEvidence] = []
        rejections: list[Rejection] = []
        reasons: list[str] = []
        for ref in finding.evidence:
            item, rejection, reason = _resolve(ref, registry, snapshot)
            if item is not None:
                resolved.append(item)
                continue
            if rejection is not None and rejection not in rejections:
                rejections.append(rejection)
            if reason:
                reasons.append(reason)
        confidence = _confidence_for(finding, resolved, rejections, reasons)
        contradiction = contradictions.get(finding.stable_key, ())
        if contradiction:
            confidence = Confidence.CONTRADICTED
            if Rejection.CONTRADICTED_BY_PEER not in rejections:
                rejections.append(Rejection.CONTRADICTED_BY_PEER)
            reasons.extend(contradiction)
        item = VerifiedFinding(
            finding=finding,
            confidence=confidence,
            evidence=tuple(resolved),
            rejections=tuple(rejections),
            reasons=tuple(dict.fromkeys(reasons)),
        )
        counts[confidence.value] = counts.get(confidence.value, 0) + 1
        if item.persistable:
            verified.append(item)
        else:
            discarded.append(item)
        if confidence is Confidence.UNRESOLVED:
            gaps.append(
                f"missing verifiable evidence for {finding.type.value} "
                f"{finding.subject!r}"
            )
        if confidence is Confidence.CONTRADICTED:
            gaps.extend(contradiction)
    counts["total"] = len(findings)
    counts["discarded"] = len(discarded)
    return VerificationReport(
        verified=tuple(verified),
        discarded=tuple(discarded),
        gaps=tuple(dict.fromkeys(gaps)),
        counts=counts,
    )


def _confidence_for(
    finding: Finding,
    resolved: Sequence[ResolvedEvidence],
    rejections: list[Rejection],
    reasons: list[str],
) -> Confidence:
    if not finding.evidence:
        rejections.append(Rejection.NO_EVIDENCE)
        reasons.append(
            f"{finding.type.value} {finding.subject!r} was stated without evidence"
        )
        return Confidence.UNRESOLVED
    if not resolved:
        return Confidence.UNRESOLVED
    if not _lexically_grounded(finding, resolved):
        rejections.append(Rejection.STATEMENT_UNSUPPORTED_BY_EXCERPT)
        reasons.append(
            f"no term of {finding.subject!r} appears in the captured excerpts or paths"
        )
        return Confidence.INFERRED
    if not any(item.executable for item in resolved):
        rejections.append(Rejection.NO_EXECUTABLE_EVIDENCE)
        kinds = sorted({item.content.value for item in resolved})
        reasons.append(
            f"only non-executable evidence ({', '.join(kinds)}) supports "
            f"{finding.subject!r}"
        )
        return Confidence.INFERRED
    if finding.confidence is Confidence.CONTRADICTED:
        return Confidence.CONTRADICTED
    return Confidence.SUPPORTED
