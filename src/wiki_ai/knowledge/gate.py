from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .model import Confidence, EntityId
from .repository import FORMAT_VERSION, KnowledgeRepository

ENTITY_TARGET = "entity"
RELATION_TARGET = "relation"
EVIDENCE_TARGET = "evidence"


@dataclass(frozen=True)
class KnowledgeProvenance:
    format_version: str
    revision_count: int
    head_revision_id: str
    head_author: str
    head_created_at: str


def provenance(repository: KnowledgeRepository) -> KnowledgeProvenance:
    head_id = repository.head_revision_id() or ""
    head = repository.get_revision(head_id) if head_id else None
    return KnowledgeProvenance(
        format_version=repository.format_version or FORMAT_VERSION,
        revision_count=repository.revision_count(),
        head_revision_id=head.id if head is not None else "",
        head_author=head.author if head is not None else "",
        head_created_at=head.created_at if head is not None else "",
    )


class KnowledgeRule(Enum):
    SUPPORTED_WITHOUT_EVIDENCE = "supported_without_evidence"
    EVIDENCE_DOES_NOT_RESOLVE = "evidence_does_not_resolve"
    SOURCE_HASH_DIVERGES = "source_hash_diverges"
    OBSOLETE_WITHOUT_INVALIDATION = "obsolete_without_invalidation"


@dataclass(frozen=True)
class KnowledgeViolation:
    rule: KnowledgeRule
    target_kind: str
    target_id: str
    detail: str


def check(
    repository: KnowledgeRepository,
    current_versions: Mapping[str, str] | None = None,
) -> tuple[KnowledgeViolation, ...]:
    found: list[KnowledgeViolation] = []
    found.extend(_supported_without_evidence(repository))
    found.extend(_unresolvable_evidence(repository))
    diverged = _diverged_versions(repository, current_versions or {})
    found.extend(_hash_divergence(repository, diverged))
    found.extend(_missing_invalidation(repository, diverged))
    return tuple(
        sorted(found, key=lambda item: (item.rule.value, item.target_kind, item.target_id))
    )


def _supported_without_evidence(
    repository: KnowledgeRepository,
) -> list[KnowledgeViolation]:
    found: list[KnowledgeViolation] = []
    for entity in repository.find_entities():
        if entity.confidence is not Confidence.SUPPORTED:
            continue
        if repository.evidence_for(entity.id):
            continue
        found.append(
            KnowledgeViolation(
                rule=KnowledgeRule.SUPPORTED_WITHOUT_EVIDENCE,
                target_kind=ENTITY_TARGET,
                target_id=entity.id.value,
                detail=f"{entity.kind} {entity.name} marcado supported sem evidência",
            )
        )
    for relation in repository.find_relations():
        if relation.confidence is not Confidence.SUPPORTED:
            continue
        if repository.evidence_for_relation(relation.id):
            continue
        endpoints = (
            repository.get_entity(relation.source_id),
            repository.get_entity(relation.target_id),
        )
        if all(
            node is not None and node.confidence is Confidence.SUPPORTED
            for node in endpoints
        ):
            continue
        found.append(
            KnowledgeViolation(
                rule=KnowledgeRule.SUPPORTED_WITHOUT_EVIDENCE,
                target_kind=RELATION_TARGET,
                target_id=relation.id,
                detail=(
                    f"relação {relation.kind} supported sem evidência própria "
                    "e sem extremos supported"
                ),
            )
        )
    return found


def _unresolvable_evidence(
    repository: KnowledgeRepository,
) -> list[KnowledgeViolation]:
    known = {version.key for version in repository.source_versions()}
    found: list[KnowledgeViolation] = []
    for evidence_id, key in repository.all_evidence_keys():
        if key in known:
            continue
        found.append(
            KnowledgeViolation(
                rule=KnowledgeRule.EVIDENCE_DOES_NOT_RESOLVE,
                target_kind=EVIDENCE_TARGET,
                target_id=evidence_id,
                detail=f"source_version {key} inexistente no repositório",
            )
        )
    return found


def _diverged_versions(
    repository: KnowledgeRepository, current_versions: Mapping[str, str]
) -> dict[str, tuple[str, str]]:
    diverged: dict[str, tuple[str, str]] = {}
    for version in repository.source_versions():
        expected = current_versions.get(version.source_id)
        if expected is None or expected == version.version_hash:
            continue
        diverged[version.key] = (version.version_hash, expected)
    return diverged


def _hash_divergence(
    repository: KnowledgeRepository, diverged: Mapping[str, tuple[str, str]]
) -> list[KnowledgeViolation]:
    found: list[KnowledgeViolation] = []
    for version in repository.source_versions():
        pair = diverged.get(version.key)
        if pair is None:
            continue
        stored, expected = pair
        found.append(
            KnowledgeViolation(
                rule=KnowledgeRule.SOURCE_HASH_DIVERGES,
                target_kind="source_version",
                target_id=version.key,
                detail=(
                    f"fonte {version.source_id} armazenada em {stored} "
                    f"mas atual é {expected}"
                ),
            )
        )
    return found


def _missing_invalidation(
    repository: KnowledgeRepository, diverged: Mapping[str, tuple[str, str]]
) -> list[KnowledgeViolation]:
    invalidated_entities = set(repository.invalidated(ENTITY_TARGET))
    invalidated_evidence = set(repository.invalidated(EVIDENCE_TARGET))
    found: list[KnowledgeViolation] = []
    for key in sorted(diverged):
        for evidence_id in repository.evidence_using(key):
            if evidence_id in invalidated_evidence:
                continue
            found.append(
                KnowledgeViolation(
                    rule=KnowledgeRule.OBSOLETE_WITHOUT_INVALIDATION,
                    target_kind=EVIDENCE_TARGET,
                    target_id=evidence_id,
                    detail=f"depende de {key} obsoleta sem registro de invalidação",
                )
            )
        for entity_id in repository.entities_using(key):
            if entity_id in invalidated_entities:
                continue
            entity = repository.get_entity(EntityId(entity_id))
            if entity is None:
                continue
            found.append(
                KnowledgeViolation(
                    rule=KnowledgeRule.OBSOLETE_WITHOUT_INVALIDATION,
                    target_kind=ENTITY_TARGET,
                    target_id=entity_id,
                    detail=f"depende de {key} obsoleta sem registro de invalidação",
                )
            )
    return found
