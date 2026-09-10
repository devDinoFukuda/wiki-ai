from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol, Sequence, runtime_checkable

from wiki_ai.knowledge.grounding import (
    check_component,
    check_relation_predicate,
    excerpt_vocabulary,
)
from wiki_ai.knowledge.identity import canonical_name, contextual_key
from wiki_ai.knowledge.model import Confidence, Entity, EntityId
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind

from wiki_ai.ingestion.finding import DocumentRelationClaim, VerifiedDocumentFinding
from wiki_ai.ingestion.harness import DocumentEvidenceCapture

__all__ = [
    "AMBIGUOUS_GAP_KIND",
    "DEFAULT_TARGET_KIND",
    "NamedEntityIndex",
    "TargetOutcome",
    "TargetResolution",
    "RelationVerdict",
    "ambiguity_gap",
    "ambiguity_diagnostic",
    "default_target_kind",
    "resolve_target",
    "assess_relation",
]

AMBIGUOUS_GAP_KIND = "ambiguous_relation_target"

DEFAULT_TARGET_KIND: Mapping[str, EntityKind] = {
    RelationKind.CALLS.value: EntityKind.MODULE,
    RelationKind.CONSUMES.value: EntityKind.INTEGRATION,
    RelationKind.PUBLISHES.value: EntityKind.EVENT,
    RelationKind.DEPENDS_ON.value: EntityKind.DEPENDENCY,
    RelationKind.VALIDATES.value: EntityKind.DATA_FIELD,
    RelationKind.TRANSITIONS_TO.value: EntityKind.STATE,
    RelationKind.PERSISTS_TO.value: EntityKind.PERSISTENCE,
    RelationKind.BELONGS_TO.value: EntityKind.MODULE,
    RelationKind.DECLARES.value: EntityKind.BUSINESS_RULE,
    RelationKind.AFFECTS.value: EntityKind.CAPABILITY,
    RelationKind.PROPOSES_CHANGE_TO.value: EntityKind.CAPABILITY,
    RelationKind.SUPERSEDES.value: EntityKind.DECISION_RECORD,
    RelationKind.CONTRADICTS.value: EntityKind.BUSINESS_RULE,
}


@runtime_checkable
class NamedEntityIndex(Protocol):
    def get_entity(self, entity_id: EntityId) -> Entity | None: ...

    def entities_named(
        self,
        namespace: str,
        name: str,
        kind: str | None = None,
        owner_id: EntityId | None = None,
    ) -> tuple[Entity, ...]: ...


class TargetOutcome(str, Enum):
    EXPLICIT = "explicit"
    CONTEXTUAL = "contextual"
    UNIQUE_CANDIDATE = "unique_candidate"
    PLACEHOLDER = "placeholder"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class TargetResolution:
    outcome: TargetOutcome
    key: str = ""
    kind: str = ""
    entity_id: EntityId | None = None
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationVerdict:
    confidence: Confidence
    captures: tuple[DocumentEvidenceCapture, ...] = ()
    reasons: tuple[str, ...] = ()


def default_target_kind(claim: DocumentRelationClaim) -> EntityKind:
    if claim.target_type is not None:
        return claim.target_type
    return DEFAULT_TARGET_KIND.get(claim.kind.value, EntityKind.CAPABILITY)


def ambiguity_gap(claim: DocumentRelationClaim, candidates: Sequence[str]) -> str:
    return (
        f"{AMBIGUOUS_GAP_KIND}: relation {claim.kind.value} names "
        f"{claim.target_subject!r}, which matches "
        f"{', '.join(sorted(candidates))}; declare target_owner or target_id"
    )


def ambiguity_diagnostic(claim: DocumentRelationClaim) -> str:
    return f"relation_target_ambiguous:{claim.target_subject}"


@dataclass(frozen=True)
class _Candidate:
    key: str
    kind: str
    entity_id: EntityId | None = None


def _contextual_candidate(
    claim: DocumentRelationClaim,
    owner: str | None,
    namespace: str,
    by_key: Mapping[str, VerifiedDocumentFinding],
    entity_ids: Mapping[str, EntityId],
    knowledge: NamedEntityIndex,
) -> _Candidate | None:
    kinds = (
        (claim.target_type.value,)
        if claim.target_type is not None
        else tuple(
            dict.fromkeys(
                (default_target_kind(claim).value,)
                + tuple(item.finding.type.value for item in by_key.values())
            )
        )
    )
    for name in kinds:
        key = contextual_key(namespace, name, owner, claim.target_subject)
        item = by_key.get(key)
        if item is not None:
            return _Candidate(key=key, kind=item.finding.type.value)
        if key in entity_ids:
            return _Candidate(key=key, kind=name)
        stored = knowledge.get_entity(EntityId.derive(name, key))
        if stored is not None:
            return _Candidate(key=key, kind=stored.kind, entity_id=stored.id)
    return None


def _round_candidates(
    claim: DocumentRelationClaim,
    by_key: Mapping[str, VerifiedDocumentFinding],
) -> dict[str, _Candidate]:
    wanted = canonical_name(claim.target_subject)
    found: dict[str, _Candidate] = {}
    for key, item in by_key.items():
        if canonical_name(item.finding.subject) != wanted:
            continue
        if claim.target_type is not None and item.finding.type is not claim.target_type:
            continue
        found[EntityId.derive(item.finding.type.value, key).value] = _Candidate(
            key=key, kind=item.finding.type.value
        )
    return found


def resolve_target(
    claim: DocumentRelationClaim,
    source: VerifiedDocumentFinding,
    namespace: str,
    by_key: Mapping[str, VerifiedDocumentFinding],
    entity_ids: Mapping[str, EntityId],
    knowledge: NamedEntityIndex,
) -> TargetResolution:
    if claim.target_id:
        kind = default_target_kind(claim)
        return TargetResolution(
            outcome=TargetOutcome.EXPLICIT,
            key=contextual_key(
                namespace,
                kind.value,
                claim.target_owner,
                claim.target_subject or claim.target_id,
                explicit_id=claim.target_id,
            ),
            kind=kind.value,
        )
    owner = claim.target_owner or source.finding.owner
    exact = _contextual_candidate(
        claim, owner, namespace, by_key, entity_ids, knowledge
    )
    if exact is not None:
        return TargetResolution(
            outcome=TargetOutcome.CONTEXTUAL,
            key=exact.key,
            kind=exact.kind,
            entity_id=exact.entity_id,
        )
    if claim.target_owner:
        return TargetResolution(
            outcome=TargetOutcome.PLACEHOLDER,
            key=contextual_key(
                namespace,
                default_target_kind(claim).value,
                claim.target_owner,
                claim.target_subject,
            ),
            kind=default_target_kind(claim).value,
        )
    candidates = _round_candidates(claim, by_key)
    stored = knowledge.entities_named(
        namespace,
        claim.target_subject,
        kind=None if claim.target_type is None else claim.target_type.value,
    )
    for entity in stored:
        candidates.setdefault(
            entity.id.value,
            _Candidate(key="", kind=entity.kind, entity_id=entity.id),
        )
    if len(candidates) > 1:
        return TargetResolution(
            outcome=TargetOutcome.AMBIGUOUS, candidates=tuple(sorted(candidates))
        )
    if len(candidates) == 1:
        only = next(iter(candidates.values()))
        return TargetResolution(
            outcome=TargetOutcome.UNIQUE_CANDIDATE,
            key=only.key,
            kind=only.kind,
            entity_id=only.entity_id,
        )
    kind = default_target_kind(claim)
    return TargetResolution(
        outcome=TargetOutcome.PLACEHOLDER,
        key=contextual_key(
            namespace, kind.value, source.finding.owner, claim.target_subject
        ),
        kind=kind.value,
    )


def _grounded(
    claim: DocumentRelationClaim,
    source: VerifiedDocumentFinding,
    target_name: str,
    captures: Sequence[DocumentEvidenceCapture],
) -> bool:
    for capture in captures:
        vocabulary = excerpt_vocabulary(capture.excerpt)
        source_side = check_component(
            "relation_source", source.finding.subject, vocabulary
        )
        target_side = check_component("relation_target", target_name, vocabulary)
        predicate = check_relation_predicate(
            claim.kind.value, claim.statement, vocabulary
        )
        if source_side.ok and target_side.ok and predicate.ok:
            return True
    return False


def assess_relation(
    claim: DocumentRelationClaim,
    source: VerifiedDocumentFinding,
    resolution: TargetResolution,
    target_name: str,
    captures: Sequence[DocumentEvidenceCapture],
    rejected: bool,
) -> RelationVerdict:
    if resolution.outcome is TargetOutcome.PLACEHOLDER:
        return RelationVerdict(
            confidence=Confidence.UNRESOLVED,
            reasons=(
                f"relation {claim.kind.value} points at {claim.target_subject!r}, "
                "which no finding of this run states",
            ),
        )
    if not claim.statement:
        return RelationVerdict(
            confidence=Confidence.INFERRED,
            reasons=(
                f"relation {claim.kind.value} to {claim.target_subject!r} "
                "was stated without its own statement",
            ),
        )
    if not claim.evidence:
        return RelationVerdict(
            confidence=Confidence.INFERRED,
            reasons=(
                f"relation {claim.kind.value} to {claim.target_subject!r} "
                "was stated without a capture of its own",
            ),
        )
    if rejected or not captures:
        return RelationVerdict(
            confidence=Confidence.INFERRED,
            reasons=(
                f"relation {claim.kind.value} to {claim.target_subject!r} "
                "cites a capture this run cannot verify",
            ),
        )
    if not _grounded(claim, source, target_name, captures):
        return RelationVerdict(
            confidence=Confidence.INFERRED,
            captures=tuple(captures),
            reasons=(
                f"the captured text of relation {claim.kind.value} does not state "
                f"{source.finding.subject!r} acting on {claim.target_subject!r}",
            ),
        )
    return RelationVerdict(confidence=Confidence.SUPPORTED, captures=tuple(captures))
