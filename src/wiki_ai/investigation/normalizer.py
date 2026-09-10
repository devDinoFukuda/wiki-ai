from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from wiki_ai.knowledge.errors import (
    IdentityCollision,
    InvalidRelationPair,
    KnowledgeError,
)
from wiki_ai.knowledge.identity import contextual_key
from wiki_ai.knowledge.gaps import open_gap
from wiki_ai.knowledge.model import (
    Confidence,
    Entity,
    EntityId,
    KnowledgeState,
    Evidence,
    Relation,
    SourceVersion,
)
from wiki_ai.knowledge.repository import (
    NAMESPACE_ATTRIBUTE,
    KnowledgeRepository,
    RevisionTransaction,
)
from wiki_ai.knowledge.taxonomy import REQUIRED_ATTRIBUTES, EntityKind, validate_pair
from wiki_ai.repository.snapshot import RepositorySnapshot

from wiki_ai.investigation.evidence import to_knowledge
from wiki_ai.investigation.finding import RelationClaim
from wiki_ai.investigation.target import (
    RoundIndex,
    TargetResolution,
    ambiguity_diagnostic,
    ambiguity_question,
    index_of,
    placeholder_kind,
    resolve_target,
)
from wiki_ai.investigation.verifier import RelationCheck, VerifiedFinding

__all__ = [
    "NormalizationResult",
    "AUTHOR",
    "entity_of",
    "normalize",
]

AUTHOR = "investigation"

_STATE_BY_CONFIDENCE: Mapping[Confidence, KnowledgeState] = {
    Confidence.SUPPORTED: KnowledgeState.IMPLEMENTED,
    Confidence.INFERRED: KnowledgeState.IMPLEMENTED,
    Confidence.CONTRADICTED: KnowledgeState.DECLARED,
    Confidence.UNRESOLVED: KnowledgeState.DECLARED,
}


@dataclass(frozen=True)
class NormalizationResult:
    entities_written: int = 0
    relations_written: int = 0
    evidence_written: int = 0
    gaps_opened: tuple[str, ...] = ()
    unresolved_relations: tuple[str, ...] = ()
    entity_ids: Mapping[str, str] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "entities_written": self.entities_written,
            "relations_written": self.relations_written,
            "evidence_written": self.evidence_written,
            "gaps_opened": list(self.gaps_opened),
            "unresolved_relations": list(self.unresolved_relations),
            "diagnostics": list(self.diagnostics),
        }


def _attribute_payload(item: VerifiedFinding) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in item.finding.attributes.items():
        payload[key] = list(value) if isinstance(value, tuple) else value
    if item.finding.statement:
        payload["statement"] = item.finding.statement
    if item.finding.conditions:
        payload["conditions"] = list(item.finding.conditions)
    if item.finding.effects:
        payload["effects"] = list(item.finding.effects)
    if item.reasons:
        payload["verification_notes"] = list(item.reasons)
    return payload


def entity_of(
    item: VerifiedFinding,
    source_version_key: str,
    stable_key: str,
    owner_id: EntityId | None = None,
    namespace: str = "",
) -> Entity:
    attributes = _attribute_payload(item)
    attributes[NAMESPACE_ATTRIBUTE] = namespace
    return Entity.create(
        kind=item.finding.type.value,
        name=item.finding.subject,
        stable_key=stable_key,
        attributes=attributes,
        state=_STATE_BY_CONFIDENCE[item.confidence],
        confidence=item.confidence,
        source_versions=(source_version_key,),
        owner_id=owner_id,
    )


def _placeholder_attributes(kind: EntityKind, subject: str) -> dict[str, Any]:
    return {name: subject for name in REQUIRED_ATTRIBUTES.get(kind, ())}


def _owner_entity_id(
    namespace: str, owner: str | None, entity_ids: Mapping[str, EntityId]
) -> EntityId | None:
    if not owner:
        return None
    for kind in (EntityKind.CAPABILITY, EntityKind.MODULE, EntityKind.SYSTEM):
        key = contextual_key(namespace, kind.value, None, owner)
        found = entity_ids.get(key)
        if found is not None:
            return found
    return None


def normalize(
    items: Sequence[VerifiedFinding],
    knowledge: KnowledgeRepository,
    snapshot: RepositorySnapshot,
    namespace: str,
    summary: str,
    gaps: Sequence[str] = (),
    captured_at: str = "",
) -> NormalizationResult:
    stamp = captured_at or snapshot.taken_at
    version = SourceVersion(
        source_id=namespace,
        version_hash=snapshot.digest,
        locator_root=snapshot.root,
        captured_at=stamp,
    )
    by_key: dict[str, VerifiedFinding] = {}
    for item in items:
        by_key[item.finding.stable_key(namespace)] = item

    entity_ids: dict[str, EntityId] = {}
    written_entities: set[str] = set()
    written_evidence: set[str] = set()
    relation_ids: set[str] = set()
    unresolved_relations: list[str] = []
    ambiguous_questions: list[str] = []
    diagnostics: list[str] = []
    opened_gaps: list[str] = []
    collisions: list[str] = []
    rejected: set[str] = set()

    with knowledge.begin_revision(author=AUTHOR, summary=summary) as transaction:
        transaction.put_source_version(version)
        for key in _ownership_order(by_key):
            item = by_key[key]
            owner_id = _owner_entity_id(namespace, item.finding.owner, entity_ids)
            entity = entity_of(item, version.key, key, owner_id, namespace)
            try:
                transaction.put_entity(entity)
            except IdentityCollision as exc:
                rejected.add(key)
                message = (
                    f"identity collision for {item.finding.type.value} "
                    f"{item.finding.subject!r}: {exc}"
                )
                if message not in collisions:
                    collisions.append(message)
                continue
            entity_ids[key] = entity.id
            written_entities.add(entity.id.value)
        for key in rejected:
            by_key.pop(key, None)
        _link_evidence(
            transaction, by_key, entity_ids, snapshot, namespace, stamp, written_evidence
        )
        index = index_of(
            namespace,
            tuple(
                (key, item.finding.type, item.finding.subject)
                for key, item in by_key.items()
            ),
        )
        relation_ids.update(
            _write_relations(
                transaction,
                knowledge,
                by_key,
                entity_ids,
                index,
                version,
                snapshot,
                stamp,
                unresolved_relations,
                ambiguous_questions,
                diagnostics,
                written_entities,
                written_evidence,
            )
        )
        for question in _unique(tuple(gaps) + tuple(collisions)):
            open_gap(transaction, question, blocking=bool(question in collisions))
            opened_gaps.append(question)
        for question in _unique(ambiguous_questions):
            open_gap(transaction, question, blocking=False)
            opened_gaps.append(question)
        for subject in _unique(unresolved_relations):
            question = f"relation target not found in this run: {subject}"
            open_gap(transaction, question, blocking=False)
            opened_gaps.append(question)

    return NormalizationResult(
        entities_written=len(written_entities),
        relations_written=len(relation_ids),
        evidence_written=len(written_evidence),
        gaps_opened=tuple(opened_gaps),
        unresolved_relations=tuple(_unique(unresolved_relations)),
        entity_ids={key: value.value for key, value in entity_ids.items()},
        diagnostics=tuple(diagnostics + collisions),
    )


def _link_evidence(
    transaction: RevisionTransaction,
    by_key: Mapping[str, VerifiedFinding],
    entity_ids: Mapping[str, EntityId],
    snapshot: RepositorySnapshot,
    namespace: str,
    stamp: str,
    written: set[str],
) -> None:
    per_evidence: dict[str, tuple[Evidence, list[EntityId]]] = {}
    for key in sorted(by_key):
        item = by_key[key]
        for resolved in item.evidence:
            _version, evidence = to_knowledge(
                resolved.capture, snapshot, namespace, stamp
            )
            slot = per_evidence.setdefault(evidence.id, (evidence, []))
            if entity_ids[key] not in slot[1]:
                slot[1].append(entity_ids[key])
    for evidence_id in sorted(per_evidence):
        evidence, holders = per_evidence[evidence_id]
        transaction.put_evidence(evidence, entity_ids=tuple(holders))
        written.add(evidence_id)


def _placeholder_entity(
    claim: RelationClaim, target: TargetResolution, version: SourceVersion
) -> Entity:
    kind = target.kind or placeholder_kind(claim)
    name = claim.target_subject or claim.label
    attributes = _placeholder_attributes(kind, name)
    attributes[NAMESPACE_ATTRIBUTE] = version.source_id
    return Entity.create(
        kind=kind.value,
        name=name,
        stable_key=target.stable_key,
        attributes=attributes,
        state=KnowledgeState.DECLARED,
        confidence=Confidence.UNRESOLVED,
        source_versions=(version.key,),
    )


def _relation_confidence(
    check: RelationCheck, target: TargetResolution, source: Confidence
) -> Confidence:
    if not target.resolved:
        return Confidence.UNRESOLVED
    if source is Confidence.CONTRADICTED:
        return Confidence.CONTRADICTED
    if check.confidence is Confidence.SUPPORTED and source is Confidence.SUPPORTED:
        return Confidence.SUPPORTED
    if check.confidence is Confidence.UNRESOLVED:
        return Confidence.UNRESOLVED
    return Confidence.INFERRED


def _write_relations(
    transaction: RevisionTransaction,
    knowledge: KnowledgeRepository,
    by_key: Mapping[str, VerifiedFinding],
    entity_ids: dict[str, EntityId],
    index: RoundIndex,
    version: SourceVersion,
    snapshot: RepositorySnapshot,
    stamp: str,
    unresolved: list[str],
    ambiguous: list[str],
    diagnostics: list[str],
    written_entities: set[str],
    written_evidence: set[str],
) -> set[str]:
    written: set[str] = set()
    for key in sorted(by_key):
        item = by_key[key]
        for position, claim in enumerate(item.finding.relations):
            target = resolve_target(claim, item.finding.owner, index, knowledge)
            if target.ambiguous:
                unresolved.append(claim.label)
                ambiguous.append(ambiguity_question(claim, target.candidates))
                diagnostics.append(ambiguity_diagnostic(claim))
                continue
            target_id = target.entity_id
            if target_id is None:
                continue
            if not target.resolved:
                if target.stable_key not in entity_ids:
                    placeholder = _placeholder_entity(claim, target, version)
                    if knowledge.get_entity(placeholder.id) is None:
                        transaction.put_entity(placeholder)
                    entity_ids[target.stable_key] = placeholder.id
                    written_entities.add(placeholder.id.value)
                unresolved.append(claim.label)
            target_kind = _kind_of(target, by_key)
            try:
                validate_pair(claim.kind, item.finding.type.value, target_kind)
            except InvalidRelationPair as exc:
                diagnostics.append(str(exc))
                continue
            check = item.relation_check(position)
            relation = Relation.create(
                kind=claim.kind.value,
                source_id=entity_ids[key],
                target_id=target_id,
                attributes=dict(claim.attributes),
                confidence=_relation_confidence(check, target, item.confidence),
            )
            try:
                transaction.put_relation(relation)
            except KnowledgeError as exc:
                diagnostics.append(str(exc))
                continue
            _link_relation_evidence(
                transaction,
                check,
                relation.id,
                snapshot,
                version.source_id,
                stamp,
                written_evidence,
            )
            written.add(relation.id)
    return written


def _link_relation_evidence(
    transaction: RevisionTransaction,
    check: RelationCheck,
    relation_id: str,
    snapshot: RepositorySnapshot,
    namespace: str,
    stamp: str,
    written: set[str],
) -> None:
    for resolved in check.evidence:
        _version, evidence = to_knowledge(resolved.capture, snapshot, namespace, stamp)
        transaction.put_evidence(evidence, relation_ids=(relation_id,))
        written.add(evidence.id)


def _ownership_order(by_key: Mapping[str, VerifiedFinding]) -> tuple[str, ...]:
    owners = {
        (item.finding.owner or "").strip().lower()
        for item in by_key.values()
        if item.finding.owner
    }
    first = sorted(
        key
        for key in by_key
        if by_key[key].finding.subject.strip().lower() in owners
        and not by_key[key].finding.owner
    )
    rest = sorted(key for key in by_key if key not in set(first))
    return tuple(first) + tuple(rest)


def _kind_of(target: TargetResolution, by_key: Mapping[str, VerifiedFinding]) -> str:
    found = by_key.get(target.stable_key)
    if found is not None:
        return found.finding.type.value
    if target.kind is not None:
        return target.kind.value
    return EntityKind.CAPABILITY.value


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))
