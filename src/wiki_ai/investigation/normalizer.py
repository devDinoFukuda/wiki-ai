from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from wiki_ai.knowledge.errors import InvalidRelationPair, KnowledgeError
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
from wiki_ai.knowledge.repository import KnowledgeRepository, RevisionTransaction
from wiki_ai.knowledge.taxonomy import REQUIRED_ATTRIBUTES, EntityKind, validate_pair
from wiki_ai.repository.snapshot import RepositorySnapshot

from wiki_ai.investigation.evidence import to_knowledge
from wiki_ai.investigation.finding import RelationClaim
from wiki_ai.investigation.verifier import VerifiedFinding

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

_DEFAULT_TARGET_KIND: Mapping[str, EntityKind] = {
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


def entity_of(item: VerifiedFinding, source_version_key: str) -> Entity:
    return Entity.create(
        kind=item.finding.type.value,
        name=item.finding.subject,
        stable_key=item.stable_key,
        attributes=_attribute_payload(item),
        state=_STATE_BY_CONFIDENCE[item.confidence],
        confidence=item.confidence,
        source_versions=(source_version_key,),
    )


def _placeholder_kind(claim: RelationClaim) -> EntityKind:
    if claim.target_type is not None:
        return claim.target_type
    return _DEFAULT_TARGET_KIND.get(claim.kind.value, EntityKind.CAPABILITY)


def _placeholder_attributes(kind: EntityKind, subject: str) -> dict[str, Any]:
    return {name: subject for name in REQUIRED_ATTRIBUTES.get(kind, ())}


def _stable_key(kind: EntityKind, subject: str) -> str:
    return f"{kind.value}::{subject.strip().lower()}"


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
        by_key[item.stable_key] = item
    subject_index: dict[str, str] = {}
    for key, item in by_key.items():
        subject_index.setdefault(item.finding.subject.strip().lower(), key)

    entity_ids: dict[str, EntityId] = {}
    written_evidence: set[str] = set()
    relation_ids: set[str] = set()
    unresolved_relations: list[str] = []
    diagnostics: list[str] = []
    opened_gaps: list[str] = []

    with knowledge.begin_revision(author=AUTHOR, summary=summary) as transaction:
        transaction.put_source_version(version)
        for key in sorted(by_key):
            item = by_key[key]
            entity = entity_of(item, version.key)
            transaction.put_entity(entity)
            entity_ids[key] = entity.id
        _link_evidence(
            transaction, by_key, entity_ids, snapshot, namespace, stamp, written_evidence
        )
        relation_ids.update(
            _write_relations(
                transaction,
                knowledge,
                by_key,
                entity_ids,
                subject_index,
                version,
                unresolved_relations,
                diagnostics,
            )
        )
        for question in _unique(gaps):
            open_gap(transaction, question, blocking=False)
            opened_gaps.append(question)
        for subject in _unique(unresolved_relations):
            question = f"relation target not found in this run: {subject}"
            open_gap(transaction, question, blocking=False)
            opened_gaps.append(question)

    return NormalizationResult(
        entities_written=len(entity_ids),
        relations_written=len(relation_ids),
        evidence_written=len(written_evidence),
        gaps_opened=tuple(opened_gaps),
        unresolved_relations=tuple(_unique(unresolved_relations)),
        entity_ids={key: value.value for key, value in entity_ids.items()},
        diagnostics=tuple(diagnostics),
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


def _write_relations(
    transaction: RevisionTransaction,
    knowledge: KnowledgeRepository,
    by_key: Mapping[str, VerifiedFinding],
    entity_ids: dict[str, EntityId],
    subject_index: Mapping[str, str],
    version: SourceVersion,
    unresolved: list[str],
    diagnostics: list[str],
) -> set[str]:
    written: set[str] = set()
    for key in sorted(by_key):
        item = by_key[key]
        for claim in item.finding.relations:
            target = claim.target_subject.strip().lower()
            target_key = subject_index.get(target)
            if target_key is None:
                kind = _placeholder_kind(claim)
                target_key = _stable_key(kind, claim.target_subject)
                if target_key not in entity_ids:
                    placeholder = Entity.create(
                        kind=kind.value,
                        name=claim.target_subject,
                        stable_key=target_key,
                        attributes=_placeholder_attributes(kind, claim.target_subject),
                        state=KnowledgeState.DECLARED,
                        confidence=Confidence.UNRESOLVED,
                        source_versions=(version.key,),
                    )
                    known = knowledge.get_entity(placeholder.id)
                    if known is None:
                        transaction.put_entity(placeholder)
                        unresolved.append(claim.target_subject)
                    entity_ids[target_key] = placeholder.id
                else:
                    unresolved.append(claim.target_subject)
            source_kind = item.finding.type
            target_kind_value = target_key.split("::", 1)[0]
            try:
                validate_pair(claim.kind, source_kind.value, target_kind_value)
            except InvalidRelationPair as exc:
                diagnostics.append(str(exc))
                continue
            relation = Relation.create(
                kind=claim.kind.value,
                source_id=entity_ids[key],
                target_id=entity_ids[target_key],
                attributes=dict(claim.attributes),
                confidence=_relation_confidence(item.confidence, target_key, by_key),
            )
            try:
                transaction.put_relation(relation)
            except KnowledgeError as exc:
                diagnostics.append(str(exc))
                continue
            written.add(relation.id)
    return written


def _relation_confidence(
    source_confidence: Confidence,
    target_key: str,
    by_key: Mapping[str, VerifiedFinding],
) -> Confidence:
    target = by_key.get(target_key)
    if target is None:
        return Confidence.INFERRED
    if source_confidence is Confidence.SUPPORTED and target.confidence is Confidence.SUPPORTED:
        return Confidence.SUPPORTED
    if Confidence.CONTRADICTED in (source_confidence, target.confidence):
        return Confidence.CONTRADICTED
    return Confidence.INFERRED


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))
