from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from wiki_ai.knowledge.errors import KnowledgeError
from wiki_ai.knowledge.model import Confidence, KnowledgeState
from wiki_ai.knowledge.taxonomy import (
    ENTITY_KIND_VALUES,
    RELATION_KIND_VALUES,
    REQUIRED_ATTRIBUTES,
    EntityKind,
    RelationKind,
    entity_kind,
    relation_kind,
)

from wiki_ai.ingestion import briefing
from wiki_ai.ingestion.harness import DocumentEvidenceCapture, DocumentHarness, excerpt_hash
from wiki_ai.ingestion.hints import normalize_text, stable_key
from wiki_ai.ingestion.source import SourceKind

__all__ = [
    "MIN_TERM_LENGTH",
    "DocumentFindingError",
    "DocumentEvidenceRef",
    "DocumentRelationClaim",
    "DocumentFinding",
    "Rejection",
    "VerifiedDocumentFinding",
    "document_finding_schema",
    "parse_finding",
    "parse_findings",
    "state_for",
    "verify",
]

MIN_TERM_LENGTH = 4


class DocumentFindingError(Exception):
    pass


@dataclass(frozen=True)
class DocumentEvidenceRef:
    capture_id: str
    excerpt_hash: str = ""

    def __post_init__(self) -> None:
        if not str(self.capture_id).strip():
            raise DocumentFindingError("evidence reference without a capture id")

    def to_dict(self) -> dict[str, Any]:
        return {"capture_id": self.capture_id, "excerpt_hash": self.excerpt_hash}


@dataclass(frozen=True)
class DocumentRelationClaim:
    kind: RelationKind
    target_subject: str
    target_type: EntityKind | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        target = str(self.target_subject or "").strip()
        if not target:
            raise DocumentFindingError(
                f"relation {self.kind.value} claimed without a target subject"
            )
        object.__setattr__(self, "target_subject", target)
        object.__setattr__(self, "attributes", dict(self.attributes))


@dataclass(frozen=True)
class DocumentFinding:
    type: EntityKind
    subject: str
    statement: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)
    conditions: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    evidence: tuple[DocumentEvidenceRef, ...] = ()
    proposed: bool = False
    relations: tuple[DocumentRelationClaim, ...] = ()
    gap_question: str = ""

    def __post_init__(self) -> None:
        subject = str(self.subject or "").strip()
        if not subject:
            raise DocumentFindingError(f"finding {self.type.value} without a subject")
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "statement", str(self.statement or "").strip())
        merged = dict(self.attributes)
        if self.conditions:
            merged.setdefault("conditions", list(self.conditions))
        if self.effects:
            merged.setdefault("effects", list(self.effects))
        if self.statement:
            merged.setdefault("statement", self.statement)
            merged.setdefault("decision", self.statement)
        object.__setattr__(self, "attributes", merged)
        object.__setattr__(self, "conditions", tuple(self.conditions))
        object.__setattr__(self, "effects", tuple(self.effects))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "relations", tuple(self.relations))
        missing = [
            name
            for name in REQUIRED_ATTRIBUTES.get(self.type, ())
            if _blank(merged.get(name))
        ]
        if missing:
            raise DocumentFindingError(
                f"finding {self.type.value} {subject!r} lacks required attribute(s): "
                f"{', '.join(missing)}"
            )

    @property
    def stable_key(self) -> str:
        return stable_key(self.type.value, self.subject)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "subject": self.subject,
            "statement": self.statement,
            "attributes": dict(self.attributes),
            "conditions": list(self.conditions),
            "effects": list(self.effects),
            "evidence": [item.to_dict() for item in self.evidence],
            "proposed": self.proposed,
            "gap_question": self.gap_question,
        }


class Rejection(str, Enum):
    NO_EVIDENCE = "no_evidence"
    EVIDENCE_UNRESOLVED = "evidence_unresolved"
    EVIDENCE_TAMPERED = "evidence_tampered"
    STATEMENT_UNSUPPORTED_BY_BLOCK = "statement_unsupported_by_block"
    TYPE_OUTSIDE_SOURCE_KIND = "type_outside_source_kind"


@dataclass(frozen=True)
class VerifiedDocumentFinding:
    finding: DocumentFinding
    confidence: Confidence
    state: KnowledgeState
    captures: tuple[DocumentEvidenceCapture, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    reasons: tuple[str, ...] = ()

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
            "state": self.state.value,
            "captures": [item.capture_id for item in self.captures],
            "rejections": [item.value for item in self.rejections],
            "reasons": list(self.reasons),
        }


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _texts(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value),)


def document_finding_schema(kind: SourceKind | None = None) -> dict[str, Any]:
    allowed_types = (
        sorted(item.value for item in briefing.allowed_types(kind))
        if kind is not None
        else sorted(ENTITY_KIND_VALUES)
    )
    allowed_relations = (
        sorted(item.value for item in briefing.allowed_relations(kind))
        if kind is not None
        else sorted(RELATION_KIND_VALUES)
    )
    return {
        "type": "object",
        "required": ["type", "subject"],
        "additionalProperties": False,
        "properties": {
            "type": {"type": "string", "enum": allowed_types},
            "subject": {"type": "string"},
            "statement": {"type": "string"},
            "attributes": {"type": "object"},
            "conditions": {"type": "array", "items": {"type": "string"}},
            "effects": {"type": "array", "items": {"type": "string"}},
            "proposed": {"type": "boolean"},
            "gap_question": {"type": "string"},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["capture_id"],
                    "additionalProperties": False,
                    "properties": {
                        "capture_id": {"type": "string"},
                        "excerpt_hash": {"type": "string"},
                    },
                },
            },
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["kind", "target_subject"],
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": allowed_relations},
                        "target_subject": {"type": "string"},
                        "target_type": {"type": "string", "enum": allowed_types},
                        "attributes": {"type": "object"},
                    },
                },
            },
        },
        "types": [
            {
                "type": value,
                "required_attributes": list(
                    REQUIRED_ATTRIBUTES.get(entity_kind(value), ())
                ),
            }
            for value in allowed_types
        ],
    }


def _evidence_ref(payload: Any) -> DocumentEvidenceRef:
    if isinstance(payload, str):
        return DocumentEvidenceRef(capture_id=payload)
    if not isinstance(payload, Mapping):
        raise DocumentFindingError(
            f"evidence reference must be a mapping or capture id, got "
            f"{type(payload).__name__}"
        )
    return DocumentEvidenceRef(
        capture_id=str(payload.get("capture_id") or ""),
        excerpt_hash=str(payload.get("excerpt_hash") or ""),
    )


def _relation_claim(payload: Any) -> DocumentRelationClaim:
    if not isinstance(payload, Mapping):
        raise DocumentFindingError(
            f"relation claim must be a mapping, got {type(payload).__name__}"
        )
    raw_kind = payload.get("kind")
    if raw_kind is None:
        raise DocumentFindingError("relation claim without a kind")
    raw_target = payload.get("target_type")
    return DocumentRelationClaim(
        kind=relation_kind(str(raw_kind)),
        target_subject=str(payload.get("target_subject") or ""),
        target_type=entity_kind(str(raw_target)) if raw_target else None,
        attributes=dict(payload.get("attributes") or {}),
    )


def parse_finding(payload: Mapping[str, Any]) -> DocumentFinding:
    if not isinstance(payload, Mapping):
        raise DocumentFindingError(
            f"finding must be a mapping, got {type(payload).__name__}"
        )
    raw_type = payload.get("type")
    if raw_type is None or str(raw_type) not in ENTITY_KIND_VALUES:
        raise DocumentFindingError(f"finding type {raw_type!r} is outside the taxonomy")
    attributes = payload.get("attributes")
    return DocumentFinding(
        type=entity_kind(str(raw_type)),
        subject=str(payload.get("subject") or ""),
        statement=str(payload.get("statement") or ""),
        attributes=dict(attributes) if isinstance(attributes, Mapping) else {},
        conditions=_texts(payload.get("conditions")),
        effects=_texts(payload.get("effects")),
        evidence=tuple(_evidence_ref(item) for item in payload.get("evidence") or ()),
        proposed=bool(payload.get("proposed", False)),
        relations=tuple(
            _relation_claim(item) for item in payload.get("relations") or ()
        ),
        gap_question=str(payload.get("gap_question") or ""),
    )


def parse_findings(
    payloads: Sequence[Mapping[str, Any]],
) -> tuple[tuple[DocumentFinding, ...], tuple[str, ...]]:
    accepted: list[DocumentFinding] = []
    rejected: list[str] = []
    for payload in payloads:
        try:
            accepted.append(parse_finding(payload))
        except (DocumentFindingError, KnowledgeError) as exc:
            rejected.append(str(exc))
    return tuple(accepted), tuple(rejected)


def state_for(finding: DocumentFinding) -> KnowledgeState:
    if finding.proposed or finding.type in (EntityKind.PROPOSAL, EntityKind.INITIATIVE):
        return KnowledgeState.PROPOSED
    return KnowledgeState.DECLARED


def _terms(text: str) -> set[str]:
    return {
        token
        for token in normalize_text(text).replace("_", " ").split()
        if len(token) >= MIN_TERM_LENGTH
    }


def _grounded(finding: DocumentFinding, captures: Sequence[DocumentEvidenceCapture]) -> bool:
    claim = _terms(f"{finding.subject} {finding.statement}")
    claim |= _terms(" ".join(finding.conditions + finding.effects))
    if not claim:
        return False
    for capture in captures:
        haystack = _terms(capture.excerpt) | _terms(
            " ".join(str(value) for value in capture.locator.values())
        )
        if claim & haystack:
            return True
    return False


def verify(
    findings: Sequence[DocumentFinding],
    harness: DocumentHarness,
) -> tuple[tuple[VerifiedDocumentFinding, ...], tuple[str, ...]]:
    allowed = frozenset(briefing.allowed_types(harness.kind))
    results: list[VerifiedDocumentFinding] = []
    gaps: list[str] = []
    for finding in findings:
        rejections: list[Rejection] = []
        reasons: list[str] = []
        captures: list[DocumentEvidenceCapture] = []
        for ref in finding.evidence:
            capture = harness.capture(ref.capture_id)
            if capture is None:
                rejections.append(Rejection.EVIDENCE_UNRESOLVED)
                reasons.append(
                    f"capture {ref.capture_id} was never produced in this run"
                )
                continue
            if ref.excerpt_hash and ref.excerpt_hash != capture.excerpt_hash:
                rejections.append(Rejection.EVIDENCE_TAMPERED)
                reasons.append(
                    f"capture {ref.capture_id} does not match the text of its blocks"
                )
                continue
            if capture.excerpt_hash != excerpt_hash(capture.excerpt):
                rejections.append(Rejection.EVIDENCE_TAMPERED)
                reasons.append(f"capture {ref.capture_id} carries a broken hash")
                continue
            captures.append(capture)
        if finding.type not in allowed:
            rejections.append(Rejection.TYPE_OUTSIDE_SOURCE_KIND)
            reasons.append(
                f"{finding.type.value} is not a finding type this "
                f"{harness.kind.value} source may declare"
            )
        confidence = Confidence.UNRESOLVED
        if not finding.evidence:
            rejections.append(Rejection.NO_EVIDENCE)
            reasons.append(
                f"{finding.type.value} {finding.subject!r} was stated without evidence"
            )
        elif captures and Rejection.TYPE_OUTSIDE_SOURCE_KIND not in rejections:
            if _grounded(finding, captures):
                confidence = Confidence.SUPPORTED
            else:
                rejections.append(Rejection.STATEMENT_UNSUPPORTED_BY_BLOCK)
                reasons.append(
                    f"no term of {finding.subject!r} appears in the captured blocks"
                )
                confidence = Confidence.INFERRED
        item = VerifiedDocumentFinding(
            finding=finding,
            confidence=confidence,
            state=state_for(finding),
            captures=tuple(captures),
            rejections=tuple(dict.fromkeys(rejections)),
            reasons=tuple(dict.fromkeys(reasons)),
        )
        results.append(item)
        if confidence is Confidence.UNRESOLVED:
            gaps.append(
                f"missing verifiable evidence for {finding.type.value} "
                f"{finding.subject!r}"
            )
        if finding.gap_question:
            gaps.append(finding.gap_question)
    return tuple(results), tuple(dict.fromkeys(gaps))
