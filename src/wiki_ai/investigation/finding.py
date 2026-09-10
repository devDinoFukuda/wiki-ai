from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from wiki_ai.knowledge.identity import contextual_key
from wiki_ai.knowledge.model import Confidence
from wiki_ai.knowledge.taxonomy import (
    ENTITY_KIND_VALUES,
    RELATION_KIND_VALUES,
    REQUIRED_ATTRIBUTES,
    EntityKind,
    RelationKind,
    entity_kind,
    relation_kind,
)

__all__ = [
    "FindingError",
    "UnknownFindingType",
    "MissingFindingAttribute",
    "MalformedEvidenceRef",
    "EvidenceRef",
    "RelationClaim",
    "EXPLICIT_TARGET_PREFIX",
    "Finding",
    "parse_finding",
    "parse_findings",
    "with_owner",
    "finding_schema",
]


class FindingError(Exception):
    pass


class UnknownFindingType(FindingError):
    pass


class MissingFindingAttribute(FindingError):
    pass


class MalformedEvidenceRef(FindingError):
    pass


class MalformedRelationClaim(FindingError):
    pass


EXPLICIT_TARGET_PREFIX = "id:"


@dataclass(frozen=True)
class EvidenceRef:
    capture_id: str = ""
    path: str = ""
    line_start: int = 0
    line_end: int = 0
    symbol: str = ""

    def __post_init__(self) -> None:
        capture = (self.capture_id or "").strip()
        path = (self.path or "").strip()
        object.__setattr__(self, "capture_id", capture)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "symbol", (self.symbol or "").strip())
        if not capture and not path:
            raise MalformedEvidenceRef(
                "evidence reference needs a capture_id or a path with line range"
            )
        if not capture:
            if self.line_start < 1 or self.line_end < self.line_start:
                raise MalformedEvidenceRef(
                    f"evidence reference for {path!r} has an invalid range: "
                    f"{self.line_start}..{self.line_end}"
                )

    @property
    def key(self) -> str:
        if self.capture_id:
            return self.capture_id
        span = f"{self.line_start}-{self.line_end}"
        return f"{self.path}:{span}#{self.symbol}" if self.symbol else f"{self.path}:{span}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class RelationClaim:
    kind: RelationKind
    target_subject: str = ""
    target_type: EntityKind | None = None
    target_owner: str | None = None
    target_id: str | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        target = (self.target_subject or "").strip()
        explicit = (self.target_id or "").strip()
        if not target and not explicit:
            raise MalformedRelationClaim(
                f"relation {self.kind.value} claimed without a target subject "
                "and without a target id"
            )
        object.__setattr__(self, "target_subject", target)
        object.__setattr__(self, "target_id", explicit or None)
        owner = (self.target_owner or "").strip()
        object.__setattr__(self, "target_owner", owner or None)
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "attributes", dict(self.attributes))

    @property
    def label(self) -> str:
        return self.target_subject or f"{EXPLICIT_TARGET_PREFIX}{self.target_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "target_subject": self.target_subject,
            "target_type": None if self.target_type is None else self.target_type.value,
            "target_owner": self.target_owner,
            "target_id": self.target_id,
            "evidence": [ref.to_dict() for ref in self.evidence],
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RelationClaim":
        return _relation_claim(payload)


@dataclass(frozen=True)
class Finding:
    type: EntityKind
    subject: str
    statement: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)
    conditions: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    confidence: Confidence = Confidence.INFERRED
    relations: tuple[RelationClaim, ...] = ()
    contradicts: tuple[str, ...] = ()
    owner: str | None = None
    explicit_id: str | None = None

    def __post_init__(self) -> None:
        subject = (self.subject or "").strip()
        if not subject:
            raise FindingError(f"finding of type {self.type.value} without a subject")
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "statement", (self.statement or "").strip())
        owner = (self.owner or "").strip()
        object.__setattr__(self, "owner", owner or None)
        explicit = (self.explicit_id or "").strip()
        object.__setattr__(self, "explicit_id", explicit or None)
        merged = dict(self.attributes)
        if self.conditions:
            merged.setdefault("conditions", list(self.conditions))
        if self.effects:
            merged.setdefault("effects", list(self.effects))
        if self.statement:
            merged.setdefault("statement", self.statement)
        object.__setattr__(self, "attributes", merged)
        object.__setattr__(self, "conditions", tuple(self.conditions))
        object.__setattr__(self, "effects", tuple(self.effects))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "relations", tuple(self.relations))
        object.__setattr__(self, "contradicts", tuple(self.contradicts))
        missing = [
            name
            for name in REQUIRED_ATTRIBUTES.get(self.type, ())
            if _blank(merged.get(name))
        ]
        if missing:
            raise MissingFindingAttribute(
                f"finding {self.type.value} {subject!r} lacks required attribute(s): "
                f"{', '.join(missing)}"
            )

    def stable_key(self, namespace: str) -> str:
        return contextual_key(
            namespace, self.type.value, self.owner, self.subject, self.explicit_id
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "subject": self.subject,
            "owner": self.owner,
            "explicit_id": self.explicit_id,
            "statement": self.statement,
            "attributes": dict(self.attributes),
            "conditions": list(self.conditions),
            "effects": list(self.effects),
            "evidence": [ref.to_dict() for ref in self.evidence],
            "confidence": self.confidence.value,
            "relations": [claim.to_dict() for claim in self.relations],
            "contradicts": list(self.contradicts),
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


def _evidence_ref(payload: Any) -> EvidenceRef:
    if isinstance(payload, str):
        return EvidenceRef(capture_id=payload)
    if not isinstance(payload, Mapping):
        raise MalformedEvidenceRef(
            f"evidence reference must be a mapping or capture id, got "
            f"{type(payload).__name__}"
        )
    return EvidenceRef(
        capture_id=str(payload.get("capture_id") or ""),
        path=str(payload.get("path") or ""),
        line_start=int(payload.get("line_start") or 0),
        line_end=int(payload.get("line_end") or 0),
        symbol=str(payload.get("symbol") or ""),
    )


def _relation_claim(payload: Any) -> RelationClaim:
    if not isinstance(payload, Mapping):
        raise MalformedRelationClaim(
            f"relation claim must be a mapping, got {type(payload).__name__}"
        )
    raw_kind = payload.get("kind")
    if raw_kind is None:
        raise MalformedRelationClaim("relation claim without a kind")
    raw_target_type = payload.get("target_type")
    return RelationClaim(
        kind=relation_kind(str(raw_kind)),
        target_subject=str(payload.get("target_subject") or ""),
        target_type=entity_kind(str(raw_target_type)) if raw_target_type else None,
        target_owner=str(payload.get("target_owner") or "") or None,
        target_id=str(payload.get("target_id") or "") or None,
        evidence=tuple(
            _evidence_ref(item) for item in payload.get("evidence") or ()
        ),
        attributes=dict(payload.get("attributes") or {}),
    )


def _confidence(value: Any) -> Confidence:
    if value is None:
        return Confidence.INFERRED
    if isinstance(value, Confidence):
        return value
    try:
        return Confidence(str(value))
    except ValueError:
        raise FindingError(
            f"confidence {value!r} unknown; allowed: "
            f"{', '.join(item.value for item in Confidence)}"
        ) from None


def parse_finding(payload: Mapping[str, Any]) -> Finding:
    if not isinstance(payload, Mapping):
        raise FindingError(f"finding must be a mapping, got {type(payload).__name__}")
    raw_type = payload.get("type")
    if raw_type is None:
        raise UnknownFindingType("finding without a type")
    if str(raw_type) not in ENTITY_KIND_VALUES:
        raise UnknownFindingType(
            f"finding type {raw_type!r} is outside the taxonomy; allowed: "
            f"{', '.join(sorted(ENTITY_KIND_VALUES))}"
        )
    attributes = payload.get("attributes")
    return Finding(
        type=entity_kind(str(raw_type)),
        subject=str(payload.get("subject") or ""),
        statement=str(payload.get("statement") or ""),
        attributes=dict(attributes) if isinstance(attributes, Mapping) else {},
        conditions=_texts(payload.get("conditions")),
        effects=_texts(payload.get("effects")),
        evidence=tuple(_evidence_ref(item) for item in payload.get("evidence") or ()),
        confidence=_confidence(payload.get("confidence")),
        relations=tuple(_relation_claim(item) for item in payload.get("relations") or ()),
        contradicts=_texts(payload.get("contradicts")),
        owner=str(payload.get("owner") or "") or None,
        explicit_id=str(payload.get("explicit_id") or "") or None,
    )


def with_owner(finding: Finding, owner: str) -> Finding:
    if finding.owner or not owner.strip():
        return finding
    return replace(finding, owner=owner.strip())


@dataclass(frozen=True)
class RejectedFinding:
    payload: Mapping[str, Any]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"payload": dict(self.payload), "reason": self.reason}


def parse_findings(
    payloads: Sequence[Mapping[str, Any]],
) -> tuple[tuple[Finding, ...], tuple[RejectedFinding, ...]]:
    accepted: list[Finding] = []
    rejected: list[RejectedFinding] = []
    for payload in payloads:
        try:
            accepted.append(parse_finding(payload))
        except FindingError as exc:
            rejected.append(
                RejectedFinding(
                    payload=dict(payload) if isinstance(payload, Mapping) else {},
                    reason=str(exc),
                )
            )
    return tuple(accepted), tuple(rejected)


def _evidence_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "capture_id": {"type": "string"},
            "path": {"type": "string"},
            "line_start": {"type": "integer"},
            "line_end": {"type": "integer"},
            "symbol": {"type": "string"},
        },
    }


def _relation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["kind"],
        "additionalProperties": False,
        "anyOf": [
            {"required": ["target_subject"]},
            {"required": ["target_id"]},
        ],
        "properties": {
            "kind": {"type": "string", "enum": sorted(RELATION_KIND_VALUES)},
            "target_subject": {"type": "string"},
            "target_type": {"type": "string", "enum": sorted(ENTITY_KIND_VALUES)},
            "target_owner": {"type": "string"},
            "target_id": {"type": "string"},
            "evidence": {"type": "array", "items": _evidence_schema()},
            "attributes": {"type": "object"},
        },
    }


def _type_entries() -> list[dict[str, Any]]:
    return [
        {
            "type": kind.value,
            "required_attributes": list(REQUIRED_ATTRIBUTES.get(kind, ())),
        }
        for kind in EntityKind
    ]


def finding_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["type", "subject"],
        "additionalProperties": False,
        "properties": {
            "type": {"type": "string", "enum": sorted(ENTITY_KIND_VALUES)},
            "subject": {"type": "string"},
            "owner": {"type": "string"},
            "explicit_id": {"type": "string"},
            "statement": {"type": "string"},
            "attributes": {"type": "object"},
            "conditions": {"type": "array", "items": {"type": "string"}},
            "effects": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": _evidence_schema()},
            "confidence": {
                "type": "string",
                "enum": [item.value for item in Confidence],
            },
            "relations": {
                "type": "array",
                "items": _relation_schema(),
            },
            "contradicts": {"type": "array", "items": {"type": "string"}},
        },
        "types": _type_entries(),
    }
