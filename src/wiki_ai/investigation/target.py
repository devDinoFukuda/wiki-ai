from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from wiki_ai.knowledge.identity import canonical_name, contextual_key
from wiki_ai.knowledge.model import EntityId
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind

from wiki_ai.investigation.finding import RelationClaim

__all__ = [
    "AMBIGUOUS_GAP_KIND",
    "DEFAULT_TARGET_KIND",
    "Resolution",
    "TargetResolution",
    "RoundIndex",
    "index_of",
    "placeholder_kind",
    "resolve_target",
    "ambiguity_question",
    "ambiguity_diagnostic",
]

AMBIGUOUS_GAP_KIND = "ambiguous_relation_target"

DEFAULT_TARGET_KIND: Mapping[str, EntityKind] = {
    "calls": EntityKind.OPERATION,
    "consumes": EntityKind.INPUT,
    "publishes": EntityKind.EVENT,
    "reads": EntityKind.PERSISTENCE,
    "writes": EntityKind.PERSISTENCE,
    "persists_to": EntityKind.PERSISTENCE,
    "validates": EntityKind.INPUT,
    "implements": EntityKind.CAPABILITY,
    "belongs_to": EntityKind.CAPABILITY,
    "triggers": EntityKind.FLOW,
    "handles": EntityKind.FAILURE_MODE,
    "transitions_to": EntityKind.STATE,
    "depends_on": EntityKind.DEPENDENCY,
    "tests": EntityKind.CAPABILITY,
    "affects": EntityKind.CAPABILITY,
    "contradicts": EntityKind.BUSINESS_RULE,
    "supersedes": EntityKind.BUSINESS_RULE,
    "retries": EntityKind.OPERATION,
    "falls_back_to": EntityKind.FALLBACK,
    "declares": EntityKind.CAPABILITY,
    "proposes_change_to": EntityKind.CAPABILITY,
}


class Resolution(Enum):
    EXPLICIT_ID = "explicit_id"
    CONTEXTUAL = "contextual"
    UNIQUE_CANDIDATE = "unique_candidate"
    PLACEHOLDER = "placeholder"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class TargetResolution:
    resolution: Resolution
    stable_key: str = ""
    entity_id: EntityId | None = None
    kind: EntityKind | None = None
    candidates: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.resolution in (
            Resolution.EXPLICIT_ID,
            Resolution.CONTEXTUAL,
            Resolution.UNIQUE_CANDIDATE,
        )

    @property
    def ambiguous(self) -> bool:
        return self.resolution is Resolution.AMBIGUOUS

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution.value,
            "stable_key": self.stable_key,
            "entity_id": None if self.entity_id is None else self.entity_id.value,
            "kind": None if self.kind is None else self.kind.value,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True)
class RoundIndex:
    namespace: str
    kinds: Mapping[str, EntityKind]
    by_name: Mapping[str, tuple[str, ...]]

    def kind_of(self, stable_key: str) -> EntityKind | None:
        return self.kinds.get(stable_key)


def index_of(
    namespace: str, entries: Sequence[tuple[str, EntityKind, str]]
) -> RoundIndex:
    kinds: dict[str, EntityKind] = {}
    by_name: dict[str, list[str]] = {}
    for stable_key, kind, subject in entries:
        kinds[stable_key] = kind
        slot = by_name.setdefault(canonical_name(subject), [])
        if stable_key not in slot:
            slot.append(stable_key)
    return RoundIndex(
        namespace=namespace,
        kinds=kinds,
        by_name={name: tuple(items) for name, items in by_name.items()},
    )


def placeholder_kind(claim: RelationClaim) -> EntityKind:
    if claim.target_type is not None:
        return claim.target_type
    return DEFAULT_TARGET_KIND.get(claim.kind.value, EntityKind.CAPABILITY)


def resolve_target(
    claim: RelationClaim,
    source_owner: str | None,
    index: RoundIndex,
    knowledge: KnowledgeRepository,
) -> TargetResolution:
    kind = placeholder_kind(claim)
    explicit = _by_explicit_id(claim, index, kind)
    if explicit is not None:
        return explicit
    owner = claim.target_owner or source_owner
    exact = _by_contextual_key(claim, owner, index, knowledge, kind)
    if exact is not None:
        return exact
    if claim.target_owner:
        return _placeholder(claim, index, kind, claim.target_owner)
    return _by_candidates(claim, index, knowledge, kind)


def _by_explicit_id(
    claim: RelationClaim, index: RoundIndex, kind: EntityKind
) -> TargetResolution | None:
    if not claim.target_id:
        return None
    key = contextual_key(
        index.namespace,
        kind.value,
        claim.target_owner,
        claim.target_subject or claim.target_id,
        claim.target_id,
    )
    found = index.kind_of(key) or kind
    return TargetResolution(
        resolution=Resolution.EXPLICIT_ID,
        stable_key=key,
        entity_id=EntityId.derive(found.value, key),
        kind=found,
    )


def _by_contextual_key(
    claim: RelationClaim,
    owner: str | None,
    index: RoundIndex,
    knowledge: KnowledgeRepository,
    kind: EntityKind,
) -> TargetResolution | None:
    if not claim.target_subject or not owner:
        return None
    key = contextual_key(index.namespace, kind.value, owner, claim.target_subject)
    found = index.kind_of(key)
    if found is not None:
        return TargetResolution(
            resolution=Resolution.CONTEXTUAL,
            stable_key=key,
            entity_id=EntityId.derive(found.value, key),
            kind=found,
        )
    candidate = EntityId.derive(kind.value, key)
    if knowledge.get_entity(candidate) is not None:
        return TargetResolution(
            resolution=Resolution.CONTEXTUAL,
            stable_key=key,
            entity_id=candidate,
            kind=kind,
        )
    return None


def _round_candidates(claim: RelationClaim, index: RoundIndex) -> tuple[str, ...]:
    names = index.by_name.get(canonical_name(claim.target_subject), ())
    if claim.target_type is None:
        return names
    return tuple(key for key in names if index.kind_of(key) is claim.target_type)


def _by_candidates(
    claim: RelationClaim,
    index: RoundIndex,
    knowledge: KnowledgeRepository,
    kind: EntityKind,
) -> TargetResolution:
    round_keys = _round_candidates(claim, index)
    filter_kind = claim.target_type.value if claim.target_type is not None else None
    persisted = knowledge.entities_named(
        index.namespace, claim.target_subject, filter_kind
    )
    identifiers: list[str] = []
    for key in round_keys:
        found = index.kind_of(key)
        if found is None:
            continue
        value = EntityId.derive(found.value, key).value
        if value not in identifiers:
            identifiers.append(value)
    for entity in persisted:
        if entity.id.value not in identifiers:
            identifiers.append(entity.id.value)
    if len(identifiers) > 1:
        return TargetResolution(
            resolution=Resolution.AMBIGUOUS, candidates=tuple(sorted(identifiers))
        )
    if len(round_keys) == 1:
        key = round_keys[0]
        found = index.kind_of(key)
        if found is not None:
            return TargetResolution(
                resolution=Resolution.UNIQUE_CANDIDATE,
                stable_key=key,
                entity_id=EntityId.derive(found.value, key),
                kind=found,
            )
    if len(persisted) == 1:
        entity = persisted[0]
        return TargetResolution(
            resolution=Resolution.UNIQUE_CANDIDATE,
            entity_id=entity.id,
            kind=EntityKind(entity.kind),
        )
    return _placeholder(claim, index, kind, None)


def _placeholder(
    claim: RelationClaim,
    index: RoundIndex,
    kind: EntityKind,
    owner: str | None,
) -> TargetResolution:
    key = contextual_key(
        index.namespace, kind.value, owner, claim.target_subject or claim.label
    )
    return TargetResolution(
        resolution=Resolution.PLACEHOLDER,
        stable_key=key,
        entity_id=EntityId.derive(kind.value, key),
        kind=kind,
    )


def ambiguity_question(claim: RelationClaim, candidates: Sequence[str]) -> str:
    return (
        f"{AMBIGUOUS_GAP_KIND}: relation {claim.kind.value} names target "
        f"{claim.label!r}, which matches {len(candidates)} entities "
        f"({', '.join(sorted(candidates))}); state target_owner or target_id"
    )


def ambiguity_diagnostic(claim: RelationClaim) -> str:
    return f"relation_target_ambiguous:{claim.label}"
