from __future__ import annotations

from dataclasses import dataclass

from .model import EntityId, EpistemicStatus
from .repository import KnowledgeRepository
from .taxonomy import RelationKind

DECLARED_NOT_IMPLEMENTED = "declared_but_not_implemented"
IMPLEMENTED_NOT_DOCUMENTED = "implemented_but_not_documented"
PROPOSAL_CONFLICT = "proposal_conflicts_with_current_behavior"
DECISION_SUPERSEDES = "decision_supersedes_prior_proposal"
SOURCE_CONTRADICTS = "source_contradicts_source"


@dataclass(frozen=True)
class ComparisonFinding:
    category: str
    entity_id: str
    entity_kind: str
    entity_name: str
    detail: str
    counterpart_id: str | None = None


@dataclass(frozen=True)
class ComparisonReport:
    declared_not_implemented: tuple[ComparisonFinding, ...] = ()
    implemented_not_documented: tuple[ComparisonFinding, ...] = ()
    proposal_conflicts: tuple[ComparisonFinding, ...] = ()
    decision_supersedes: tuple[ComparisonFinding, ...] = ()
    source_contradicts_source: tuple[ComparisonFinding, ...] = ()

    def all_findings(self) -> tuple[ComparisonFinding, ...]:
        return (
            self.declared_not_implemented
            + self.implemented_not_documented
            + self.proposal_conflicts
            + self.decision_supersedes
            + self.source_contradicts_source
        )

    @property
    def total(self) -> int:
        return len(self.all_findings())


def _sorted(found: list[ComparisonFinding]) -> tuple[ComparisonFinding, ...]:
    return tuple(sorted(found, key=lambda item: (item.entity_id, item.counterpart_id or "")))


def has_implementation(repository: KnowledgeRepository, entity_id: EntityId) -> bool:
    for relation in repository.relations_of(
        entity_id, "in", (RelationKind.IMPLEMENTS.value,)
    ):
        implementor = repository.get_entity(relation.source_id)
        if implementor is not None and implementor.epistemic is EpistemicStatus.IMPLEMENTED:
            return True
    return False


def declared_not_implemented(
    repository: KnowledgeRepository,
) -> tuple[ComparisonFinding, ...]:
    found: list[ComparisonFinding] = []
    for relation in repository.find_relations(RelationKind.DECLARES.value):
        target = repository.get_entity(relation.target_id)
        declarer = repository.get_entity(relation.source_id)
        if target is None or declarer is None:
            continue
        if target.epistemic is EpistemicStatus.IMPLEMENTED:
            continue
        if has_implementation(repository, target.id):
            continue
        found.append(
            ComparisonFinding(
                category=DECLARED_NOT_IMPLEMENTED,
                entity_id=target.id.value,
                entity_kind=target.kind,
                entity_name=target.name,
                detail=f"{declarer.kind} {declarer.name} declara sem implementação",
                counterpart_id=declarer.id.value,
            )
        )
    return _sorted(found)


def implemented_not_documented(
    repository: KnowledgeRepository,
) -> tuple[ComparisonFinding, ...]:
    documented = {
        relation.target_id.value
        for relation in repository.find_relations(RelationKind.DECLARES.value)
    }
    found: list[ComparisonFinding] = []
    for entity in repository.find_entities():
        if entity.epistemic is not EpistemicStatus.IMPLEMENTED:
            continue
        if entity.id.value in documented:
            continue
        found.append(
            ComparisonFinding(
                category=IMPLEMENTED_NOT_DOCUMENTED,
                entity_id=entity.id.value,
                entity_kind=entity.kind,
                entity_name=entity.name,
                detail="implementado sem fonte declarativa que o descreva",
            )
        )
    return _sorted(found)


def proposal_conflicts(
    repository: KnowledgeRepository,
) -> tuple[ComparisonFinding, ...]:
    found: list[ComparisonFinding] = []
    for relation in repository.find_relations(RelationKind.PROPOSES_CHANGE_TO.value):
        proposal = repository.get_entity(relation.source_id)
        target = repository.get_entity(relation.target_id)
        if proposal is None or target is None:
            continue
        if target.epistemic is not EpistemicStatus.IMPLEMENTED:
            continue
        found.append(
            ComparisonFinding(
                category=PROPOSAL_CONFLICT,
                entity_id=proposal.id.value,
                entity_kind=proposal.kind,
                entity_name=proposal.name,
                detail=f"propõe mudança em {target.kind} {target.name} já implementado",
                counterpart_id=target.id.value,
            )
        )
    return _sorted(found)


def decision_supersedes(
    repository: KnowledgeRepository,
) -> tuple[ComparisonFinding, ...]:
    found: list[ComparisonFinding] = []
    for relation in repository.find_relations(RelationKind.SUPERSEDES.value):
        newer = repository.get_entity(relation.source_id)
        older = repository.get_entity(relation.target_id)
        if newer is None or older is None:
            continue
        found.append(
            ComparisonFinding(
                category=DECISION_SUPERSEDES,
                entity_id=newer.id.value,
                entity_kind=newer.kind,
                entity_name=newer.name,
                detail=f"substitui {older.kind} {older.name}",
                counterpart_id=older.id.value,
            )
        )
    return _sorted(found)


def source_contradicts_source(
    repository: KnowledgeRepository,
) -> tuple[ComparisonFinding, ...]:
    found: list[ComparisonFinding] = []
    for relation in repository.find_relations(RelationKind.CONTRADICTS.value):
        left = repository.get_entity(relation.source_id)
        right = repository.get_entity(relation.target_id)
        if left is None or right is None:
            continue
        found.append(
            ComparisonFinding(
                category=SOURCE_CONTRADICTS,
                entity_id=left.id.value,
                entity_kind=left.kind,
                entity_name=left.name,
                detail=f"contradiz {right.kind} {right.name}",
                counterpart_id=right.id.value,
            )
        )
    return _sorted(found)


def compare(repository: KnowledgeRepository) -> ComparisonReport:
    return ComparisonReport(
        declared_not_implemented=declared_not_implemented(repository),
        implemented_not_documented=implemented_not_documented(repository),
        proposal_conflicts=proposal_conflicts(repository),
        decision_supersedes=decision_supersedes(repository),
        source_contradicts_source=source_contradicts_source(repository),
    )
