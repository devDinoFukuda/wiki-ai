from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from wiki_ai.knowledge.evidence import CodeContent
from wiki_ai.knowledge.model import Confidence
from wiki_ai.knowledge.taxonomy import EntityKind
from wiki_ai.repository.evidence import EvidenceCapture, verify as verify_capture
from wiki_ai.repository.snapshot import RepositorySnapshot

from wiki_ai.investigation.evidence import CaptureRegistry, capture_id, detect_content
from wiki_ai.investigation.finding import EvidenceRef, Finding, RelationClaim
from wiki_ai.knowledge.grounding import (
    GroundingCheck,
    check_component,
    check_relation_predicate,
    excerpt_vocabulary,
    symbol_defined_or_referenced,
)

__all__ = [
    "Rejection",
    "ResolvedEvidence",
    "GroundingCheck",
    "RelationCheck",
    "verify_relation",
    "VerifiedFinding",
    "VerificationReport",
    "DECOMPOSED_KINDS",
    "DEFAULT_NAMESPACE",
    "verify",
]

DECOMPOSED_KINDS: frozenset[EntityKind] = frozenset(
    {
        EntityKind.BUSINESS_RULE,
        EntityKind.VALIDATION,
        EntityKind.EDGE_CASE,
        EntityKind.INVARIANT,
    }
)

DEFAULT_NAMESPACE = "repository"
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
class RelationCheck:
    index: int
    evidence_valid: bool = False
    claim_supported: bool = False
    confidence: Confidence = Confidence.INFERRED
    evidence: tuple[ResolvedEvidence, ...] = ()
    grounding: tuple[GroundingCheck, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "evidence_valid": self.evidence_valid,
            "claim_supported": self.claim_supported,
            "confidence": self.confidence.value,
            "evidence": [item.to_dict() for item in self.evidence],
            "grounding": [item.to_dict() for item in self.grounding],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class VerifiedFinding:
    finding: Finding
    confidence: Confidence
    evidence: tuple[ResolvedEvidence, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    reasons: tuple[str, ...] = ()
    evidence_valid: bool = False
    claim_supported: bool = False
    grounding: tuple[GroundingCheck, ...] = ()
    relation_checks: tuple[RelationCheck, ...] = ()
    stable_key: str = ""

    def relation_check(self, index: int) -> RelationCheck:
        for item in self.relation_checks:
            if item.index == index:
                return item
        return RelationCheck(index=index)

    @property
    def subject(self) -> str:
        return self.finding.subject

    @property
    def persistable(self) -> bool:
        return Rejection.EVIDENCE_TAMPERED not in self.rejections

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.finding.type.value,
            "subject": self.finding.subject,
            "confidence": self.confidence.value,
            "evidence_valid": self.evidence_valid,
            "claim_supported": self.claim_supported,
            "grounding": [item.to_dict() for item in self.grounding],
            "evidence": [item.to_dict() for item in self.evidence],
            "relation_checks": [item.to_dict() for item in self.relation_checks],
            "rejections": [item.value for item in self.rejections],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class VerificationReport:
    verified: tuple[VerifiedFinding, ...] = ()
    discarded: tuple[VerifiedFinding, ...] = ()
    gaps: tuple[str, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)

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


def _executable_vocabulary(resolved: Sequence[ResolvedEvidence]) -> frozenset[str]:
    vocabulary: set[str] = set()
    for item in resolved:
        if not item.executable:
            continue
        vocabulary |= excerpt_vocabulary(item.capture.excerpt)
        symbol = item.capture.symbol or ""
        if symbol:
            vocabulary |= excerpt_vocabulary(symbol)
    return frozenset(vocabulary)


def _any_vocabulary(resolved: Sequence[ResolvedEvidence]) -> frozenset[str]:
    vocabulary: set[str] = set()
    for item in resolved:
        vocabulary |= excerpt_vocabulary(item.capture.excerpt)
        symbol = item.capture.symbol or ""
        if symbol:
            vocabulary |= excerpt_vocabulary(symbol)
    return frozenset(vocabulary)


def _component_texts(finding: Finding) -> tuple[tuple[str, str], ...]:
    components: list[tuple[str, str]] = []
    for index, condition in enumerate(finding.conditions):
        components.append((f"conditions[{index}]", condition))
    for index, effect in enumerate(finding.effects):
        components.append((f"effects[{index}]", effect))
    if components:
        return tuple(components)
    for name in ("condition", "expected", "rule"):
        value = finding.attributes.get(name)
        if isinstance(value, str) and value.strip():
            components.append((name, value))
    if components:
        return tuple(components)
    if finding.statement:
        return (("statement", finding.statement),)
    return ()


def ground(
    finding: Finding, resolved: Sequence[ResolvedEvidence]
) -> tuple[tuple[GroundingCheck, ...], bool]:
    executable = _executable_vocabulary(resolved)
    vocabulary = executable or _any_vocabulary(resolved)
    if not vocabulary:
        return (), False
    symbol = symbol_defined_or_referenced(finding.subject, vocabulary)
    if finding.type not in DECOMPOSED_KINDS:
        statement = finding.statement or finding.subject
        claim = check_component(
            "statement", statement, vocabulary, exclude=()
        )
        checks = (symbol, claim)
        return checks, symbol.ok or claim.ok
    components = _component_texts(finding)
    if not components:
        return (symbol,), False
    checks: list[GroundingCheck] = [symbol]
    for name, text in components:
        checks.append(
            check_component(name, text, executable, subject=finding.subject)
        )
    decomposed = tuple(item for item in checks if item.component != "symbol")
    supported = bool(decomposed) and all(item.ok for item in decomposed)
    return tuple(checks), supported and symbol.ok


def _relation_grounding(
    finding: Finding,
    claim: RelationClaim,
    resolved: Sequence[ResolvedEvidence],
) -> tuple[tuple[GroundingCheck, ...], bool]:
    executable = _executable_vocabulary(resolved)
    if not executable:
        return (), False
    source = symbol_defined_or_referenced(finding.subject, executable)
    target_text = claim.target_subject or claim.target_id or ""
    target = check_component("relation_target", target_text, executable)
    predicate = check_relation_predicate(
        claim.kind.value, claim.statement, executable
    )
    checks = (source, target, predicate)
    return checks, source.ok and target.ok and predicate.ok


def verify_relation(
    finding: Finding,
    claim: RelationClaim,
    index: int,
    registry: CaptureRegistry,
    snapshot: RepositorySnapshot,
) -> RelationCheck:
    resolved: list[ResolvedEvidence] = []
    reasons: list[str] = []
    for ref in claim.evidence:
        item, rejection, reason = _resolve(ref, registry, snapshot)
        if item is not None:
            resolved.append(item)
            continue
        if reason:
            reasons.append(reason)
        if rejection is Rejection.EVIDENCE_TAMPERED:
            return RelationCheck(
                index=index,
                confidence=Confidence.UNRESOLVED,
                reasons=tuple(reasons),
            )
    if not claim.evidence:
        return RelationCheck(
            index=index,
            confidence=Confidence.INFERRED,
            reasons=(
                f"relation {claim.kind.value} to {claim.label!r} carries no evidence "
                "of its own",
            ),
        )
    if not resolved:
        return RelationCheck(
            index=index,
            confidence=Confidence.INFERRED,
            reasons=tuple(reasons),
        )
    grounding, grounded = _relation_grounding(finding, claim, resolved)
    executable = any(item.executable for item in resolved)
    if not grounded or not executable:
        reasons.append(
            f"relation {claim.kind.value} to {claim.label!r} is not grounded in an "
            f"executable excerpt naming {finding.subject!r}, the target and the "
            f"{claim.kind.value} action itself"
        )
        return RelationCheck(
            index=index,
            evidence_valid=True,
            confidence=Confidence.INFERRED,
            evidence=tuple(resolved),
            grounding=grounding,
            reasons=tuple(reasons),
        )
    return RelationCheck(
        index=index,
        evidence_valid=True,
        claim_supported=True,
        confidence=Confidence.SUPPORTED,
        evidence=tuple(resolved),
        grounding=grounding,
        reasons=tuple(reasons),
    )


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


def _declared_contradiction(
    left: Finding, right: Finding, keys: Mapping[int, str]
) -> bool:
    targets = {item.strip().lower() for item in left.contradicts}
    targets |= {item.strip().lower() for item in right.contradicts}
    return (
        left.subject.strip().lower() in targets
        or right.subject.strip().lower() in targets
        or keys[id(left)] in targets
        or keys[id(right)] in targets
    )


def _contradiction_groups(
    findings: Sequence[Finding], keys: Mapping[int, str]
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.subject.strip().lower(), []).append(finding)
    flagged: dict[str, list[str]] = {}
    for subject, group in grouped.items():
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                if not (
                    _declared_contradiction(left, right, keys)
                    or _opposite_effects(left, right)
                ):
                    continue
                message = (
                    f"contradictory statements about {subject!r}: "
                    f"{left.statement or left.subject!r} versus "
                    f"{right.statement or right.subject!r}"
                )
                for finding in (left, right):
                    entries = flagged.setdefault(keys[id(finding)], [])
                    if message not in entries:
                        entries.append(message)
    return {key: tuple(values) for key, values in flagged.items()}


def verify(
    findings: Sequence[Finding],
    registry: CaptureRegistry,
    snapshot: RepositorySnapshot,
    namespace: str = DEFAULT_NAMESPACE,
) -> VerificationReport:
    keys = {id(finding): finding.stable_key(namespace) for finding in findings}
    contradictions = _contradiction_groups(findings, keys)
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
        assessment = _assess(finding, resolved, rejections, reasons)
        confidence = assessment.confidence
        stable_key = keys[id(finding)]
        contradiction = contradictions.get(stable_key, ())
        if contradiction:
            confidence = Confidence.CONTRADICTED
            if Rejection.CONTRADICTED_BY_PEER not in rejections:
                rejections.append(Rejection.CONTRADICTED_BY_PEER)
            reasons.extend(contradiction)
        relation_checks = tuple(
            verify_relation(finding, claim, index, registry, snapshot)
            for index, claim in enumerate(finding.relations)
        )
        item = VerifiedFinding(
            finding=finding,
            confidence=confidence,
            evidence=tuple(resolved),
            rejections=tuple(rejections),
            reasons=tuple(dict.fromkeys(reasons)),
            evidence_valid=assessment.evidence_valid,
            claim_supported=assessment.claim_supported,
            grounding=assessment.grounding,
            relation_checks=relation_checks,
            stable_key=stable_key,
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


@dataclass(frozen=True)
class _Assessment:
    confidence: Confidence
    evidence_valid: bool
    claim_supported: bool
    grounding: tuple[GroundingCheck, ...]


def _assess(
    finding: Finding,
    resolved: Sequence[ResolvedEvidence],
    rejections: list[Rejection],
    reasons: list[str],
) -> _Assessment:
    if not finding.evidence:
        rejections.append(Rejection.NO_EVIDENCE)
        reasons.append(
            f"{finding.type.value} {finding.subject!r} was stated without evidence"
        )
        return _Assessment(Confidence.UNRESOLVED, False, False, ())
    if not resolved:
        return _Assessment(Confidence.UNRESOLVED, False, False, ())
    grounding, supported = ground(finding, resolved)
    executable = any(item.executable for item in resolved)
    if not supported:
        rejections.append(Rejection.STATEMENT_UNSUPPORTED_BY_EXCERPT)
        reasons.append(_grounding_reason(finding, grounding))
        return _Assessment(Confidence.INFERRED, True, False, grounding)
    if not executable:
        rejections.append(Rejection.NO_EXECUTABLE_EVIDENCE)
        kinds = sorted({item.content.value for item in resolved})
        reasons.append(
            f"only non-executable evidence ({', '.join(kinds)}) supports "
            f"{finding.subject!r}"
        )
        return _Assessment(Confidence.INFERRED, True, False, grounding)
    if finding.confidence is Confidence.CONTRADICTED:
        return _Assessment(Confidence.CONTRADICTED, True, True, grounding)
    return _Assessment(Confidence.SUPPORTED, True, True, grounding)


def _grounding_reason(
    finding: Finding, grounding: Sequence[GroundingCheck]
) -> str:
    failed = [item for item in grounding if not item.ok]
    if not failed:
        return (
            f"no executable excerpt sustains the claim about {finding.subject!r}"
        )
    parts: list[str] = []
    for item in failed:
        missing = item.terms_missing or item.terms_required
        parts.append(f"{item.component} needs {', '.join(missing) or 'key terms'}")
    return (
        f"{finding.type.value} {finding.subject!r} is not grounded in the "
        f"executable excerpt: " + "; ".join(parts)
    )
