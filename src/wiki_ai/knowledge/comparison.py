from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .model import Confidence, Entity, EntityId, EpistemicStatus
from .repository import KnowledgeRepository
from .taxonomy import EntityKind, RelationKind

DECLARED_NOT_IMPLEMENTED = "declared_but_not_implemented"
IMPLEMENTED_NOT_DOCUMENTED = "implemented_but_not_documented"
PROPOSAL_CONFLICT = "proposal_conflicts_with_current_behavior"
DECISION_SUPERSEDES = "decision_supersedes_prior_proposal"
SOURCE_CONTRADICTS = "source_contradicts_source"

NO_CORRELATION = "sem correlação com implementação"

DECLARING_KINDS: tuple[str, ...] = (
    EntityKind.REQUIREMENT.value,
    EntityKind.DECISION_RECORD.value,
)


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    source_id: str
    version_hash: str
    locator: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "version_hash": self.version_hash,
            "locator": dict(self.locator),
        }


@dataclass(frozen=True)
class ComparisonFinding:
    category: str
    entity_id: str
    entity_kind: str
    entity_name: str
    detail: str
    counterpart_id: str | None = None
    counterpart_kind: str | None = None
    counterpart_name: str | None = None
    left_source_id: str = ""
    right_source_id: str = ""
    left_evidence: tuple[EvidenceRef, ...] = ()
    right_evidence: tuple[EvidenceRef, ...] = ()
    basis: str = ""

    @property
    def has_both_sides(self) -> bool:
        return bool(self.left_evidence) and bool(self.right_evidence)


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
    return tuple(
        sorted(found, key=lambda item: (item.entity_id, item.counterpart_id or ""))
    )


def evidence_refs(
    repository: KnowledgeRepository, entity_id: EntityId
) -> tuple[EvidenceRef, ...]:
    return tuple(
        EvidenceRef(
            evidence_id=item.id,
            source_id=item.source_id,
            version_hash=item.version_hash,
            locator=item.locator.to_dict(),
        )
        for item in repository.evidence_for(entity_id)
    )


def _source_of(refs: Sequence[EvidenceRef], entity: Entity) -> str:
    if refs:
        return refs[0].source_id
    keys = entity.source_versions
    return keys[0] if keys else ""


def _finding(
    repository: KnowledgeRepository,
    category: str,
    left: Entity,
    right: Entity | None,
    detail: str,
    basis: str = "",
) -> ComparisonFinding:
    left_refs = evidence_refs(repository, left.id)
    right_refs = evidence_refs(repository, right.id) if right is not None else ()
    return ComparisonFinding(
        category=category,
        entity_id=left.id.value,
        entity_kind=left.kind,
        entity_name=left.name,
        detail=detail,
        counterpart_id=right.id.value if right is not None else None,
        counterpart_kind=right.kind if right is not None else None,
        counterpart_name=right.name if right is not None else None,
        left_source_id=_source_of(left_refs, left),
        right_source_id=_source_of(right_refs, right) if right is not None else "",
        left_evidence=left_refs,
        right_evidence=right_refs,
        basis=basis,
    )


def _basis_of(attributes: Mapping[str, Any]) -> str:
    value = attributes.get("correlated_by")
    return str(value) if isinstance(value, str) else ""


def has_implementation(repository: KnowledgeRepository, entity_id: EntityId) -> bool:
    for relation in repository.relations_of(
        entity_id, "in", (RelationKind.IMPLEMENTS.value,)
    ):
        implementor = repository.get_entity(relation.source_id)
        if (
            implementor is not None
            and implementor.epistemic is EpistemicStatus.IMPLEMENTED
        ):
            return True
    return False


def declared_not_implemented(
    repository: KnowledgeRepository,
) -> tuple[ComparisonFinding, ...]:
    found: list[ComparisonFinding] = []
    declarers_with_target: set[str] = set()
    for relation in repository.find_relations(RelationKind.DECLARES.value):
        target = repository.get_entity(relation.target_id)
        declarer = repository.get_entity(relation.source_id)
        if target is None or declarer is None:
            continue
        declarers_with_target.add(declarer.id.value)
        if target.epistemic is EpistemicStatus.IMPLEMENTED:
            continue
        if has_implementation(repository, target.id):
            continue
        found.append(
            _finding(
                repository,
                DECLARED_NOT_IMPLEMENTED,
                target,
                declarer,
                f"{declarer.kind} {declarer.name} declara sem implementação",
                _basis_of(relation.attributes),
            )
        )
    for kind in DECLARING_KINDS:
        for declarer in repository.find_entities(kind):
            if declarer.id.value in declarers_with_target:
                continue
            found.append(
                _finding(
                    repository,
                    DECLARED_NOT_IMPLEMENTED,
                    declarer,
                    None,
                    NO_CORRELATION,
                )
            )
    reported = {item.entity_id for item in found}
    for entity in repository.find_entities():
        if entity.epistemic is not EpistemicStatus.DECLARED:
            continue
        if entity.kind in DECLARING_KINDS or entity.kind == EntityKind.GAP.value:
            continue
        if entity.id.value in reported:
            continue
        if has_implementation(repository, entity.id):
            continue
        if _correlated_to_implementation(repository, entity.id):
            continue
        found.append(
            _finding(
                repository,
                DECLARED_NOT_IMPLEMENTED,
                entity,
                None,
                NO_CORRELATION,
            )
        )
    return _sorted(found)


def _correlated_to_implementation(
    repository: KnowledgeRepository, entity_id: EntityId
) -> bool:
    for kind in (RelationKind.CONTRADICTS, RelationKind.DECLARES):
        for relation in repository.relations_of(entity_id, "both", (kind.value,)):
            other_id = (
                relation.target_id
                if relation.source_id == entity_id
                else relation.source_id
            )
            other = repository.get_entity(other_id)
            if other is not None and other.epistemic is EpistemicStatus.IMPLEMENTED:
                return True
    return False


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
            _finding(
                repository,
                IMPLEMENTED_NOT_DOCUMENTED,
                entity,
                None,
                "implementado sem fonte declarativa que o descreva",
            )
        )
    return _sorted(found)


def _is_current_behavior(entity: Entity) -> bool:
    return (
        entity.epistemic is EpistemicStatus.IMPLEMENTED
        and entity.confidence is Confidence.SUPPORTED
    )


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
            _finding(
                repository,
                PROPOSAL_CONFLICT,
                proposal,
                target,
                f"propõe mudança em {target.kind} {target.name} já implementado",
                _basis_of(relation.attributes),
            )
        )
    for relation in repository.find_relations(RelationKind.CONTRADICTS.value):
        left = repository.get_entity(relation.source_id)
        right = repository.get_entity(relation.target_id)
        if left is None or right is None:
            continue
        for proposed, current in ((left, right), (right, left)):
            if proposed.epistemic not in (
                EpistemicStatus.PROPOSED,
                EpistemicStatus.DECLARED,
            ):
                continue
            if not _is_current_behavior(current):
                continue
            found.append(
                _finding(
                    repository,
                    PROPOSAL_CONFLICT,
                    proposed,
                    current,
                    f"contradiz o comportamento implementado de {current.name}",
                    _basis_of(relation.attributes),
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
            _finding(
                repository,
                DECISION_SUPERSEDES,
                newer,
                older,
                f"substitui {older.kind} {older.name}",
                _basis_of(relation.attributes),
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
        detail = str(
            relation.attributes.get("contradiction_detail")
            or f"contradiz {right.kind} {right.name}"
        )
        found.append(
            _finding(
                repository,
                SOURCE_CONTRADICTS,
                left,
                right,
                detail,
                _basis_of(relation.attributes),
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
