from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .model import Confidence, EntityId, EpistemicStatus, SourceVersion
from .repository import KnowledgeRepository, RevisionTransaction

ENTITY_TARGET = "entity"
EVIDENCE_TARGET = "evidence"

REVALIDATION_EXEMPT: frozenset[EpistemicStatus] = frozenset(
    {EpistemicStatus.HISTORICAL}
)


@dataclass(frozen=True)
class Invalidation:
    revision_id: str
    obsolete_versions: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.entities) + len(self.evidence)


def obsolete_versions(
    repository: KnowledgeRepository, incoming: Sequence[SourceVersion]
) -> tuple[str, ...]:
    obsolete: set[str] = set()
    for version in incoming:
        for stored in repository.source_versions(version.source_id):
            if stored.version_hash != version.version_hash:
                obsolete.add(stored.key)
    return tuple(sorted(obsolete))


def dependents(
    repository: KnowledgeRepository, keys: Sequence[str]
) -> tuple[dict[str, str], dict[str, str]]:
    entities: dict[str, str] = {}
    evidence: dict[str, str] = {}
    for key in keys:
        for evidence_id in repository.evidence_using(key):
            evidence.setdefault(evidence_id, key)
        for entity_id in repository.entities_using(key):
            entity = repository.get_entity(EntityId(entity_id))
            if entity is None or entity.epistemic in REVALIDATION_EXEMPT:
                continue
            entities.setdefault(entity_id, key)
    return entities, evidence


def invalidate(
    repository: KnowledgeRepository,
    incoming: Sequence[SourceVersion],
    author: str,
    summary: str = "",
) -> Invalidation:
    keys = obsolete_versions(repository, incoming)
    entities, evidence = dependents(repository, keys)
    reason = summary or f"invalidação por {len(keys)} versão(ões) substituída(s) de fonte"
    with repository.begin_revision(author, reason) as revision:
        for version in incoming:
            revision.put_source_version(version)
        _mark(revision, repository, entities, evidence)
        revision_id = revision.revision_id
    return Invalidation(
        revision_id=revision_id,
        obsolete_versions=keys,
        entities=tuple(sorted(entities)),
        evidence=tuple(sorted(evidence)),
    )


def _mark(
    transaction: RevisionTransaction,
    repository: KnowledgeRepository,
    entities: Mapping[str, str],
    evidence: Mapping[str, str],
) -> None:
    for evidence_id, key in sorted(evidence.items()):
        transaction.record_invalidation(EVIDENCE_TARGET, evidence_id, key)
    for entity_id, key in sorted(entities.items()):
        entity = repository.get_entity(EntityId(entity_id))
        if entity is None:
            continue
        transaction.put_entity(entity.with_confidence(Confidence.UNRESOLVED))
        transaction.record_invalidation(ENTITY_TARGET, entity_id, key)
