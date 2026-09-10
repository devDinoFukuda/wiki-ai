from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    CodeContent,
    CodeLocator,
    Confidence,
    Entity,
    KnowledgeRepository,
    KnowledgeRule,
    KnowledgeState,
    Relation,
    SourceVersion,
    make_evidence,
)
from wiki_ai.knowledge.gate import check
from wiki_ai.knowledge.repository import DATABASE_FILENAME

CAPTURED = "2026-01-01T00:00:00+00:00"
LATER = "2026-02-01T00:00:00+00:00"
SOURCE = "src_repo"
HASH = "codehash1"

WITHOUT_EVIDENCE = KnowledgeRule.SUPPORTED_WITHOUT_EVIDENCE
WITHOUT_ACTIVE = KnowledgeRule.SUPPORTED_WITHOUT_ACTIVE_EVIDENCE


@pytest.fixture()
def repository(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    yield repo
    repo.close()


def version() -> SourceVersion:
    return SourceVersion(
        source_id=SOURCE,
        version_hash=HASH,
        locator_root="/repo",
        captured_at=CAPTURED,
    )


def evidence(path: str = "src/renewal.py"):
    return make_evidence(
        SOURCE,
        HASH,
        CodeLocator(
            path=path,
            line_start=1,
            line_end=40,
            symbol="renew",
            content=CodeContent.EXECUTABLE,
        ),
        "def renew(order): return order",
        CAPTURED,
    )


def capability(name: str = "Renovacao") -> Entity:
    return Entity.create(
        kind="capability",
        name=name,
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.INFERRED,
    )


def promote_entity(repository: KnowledgeRepository, entity: Entity) -> None:
    repository.conn.execute(
        "UPDATE entities SET confidence='supported' WHERE entity_id=?",
        (entity.id.value,),
    )


def promote_relation(repository: KnowledgeRepository, relation_id: str) -> None:
    repository.conn.execute(
        "UPDATE relations SET confidence='supported' WHERE relation_id=?",
        (relation_id,),
    )


def rules_for(repository: KnowledgeRepository, target_id: str) -> list[KnowledgeRule]:
    return [item.rule for item in check(repository) if item.target_id == target_id]


def test_entity_with_no_evidence_at_all_reports_only_without_evidence(
    repository,
) -> None:
    entity = capability()
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(entity)
    promote_entity(repository, entity)

    found = rules_for(repository, entity.id.value)

    assert found == [WITHOUT_EVIDENCE]
    assert WITHOUT_ACTIVE not in found


def test_entity_with_only_invalidated_evidence_reports_only_without_active(
    repository,
) -> None:
    entity = capability()
    item = evidence()
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(entity)
        revision.put_evidence(item, [entity.id])
    with repository.begin_revision("pipeline", "invalidacao") as revision:
        revision.invalidate_evidence([item.id], LATER)
    promote_entity(repository, entity)

    found = rules_for(repository, entity.id.value)

    assert found == [WITHOUT_ACTIVE]
    assert WITHOUT_EVIDENCE not in found


def test_the_two_support_rules_are_mutually_exclusive(repository) -> None:
    empty = capability("Sem evidencia")
    stale = capability("Evidencia invalidada")
    item = evidence("src/stale.py")
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(empty)
        revision.put_entity(stale)
        revision.put_evidence(item, [stale.id])
    with repository.begin_revision("pipeline", "invalidacao") as revision:
        revision.invalidate_evidence([item.id], LATER)
    promote_entity(repository, empty)
    promote_entity(repository, stale)

    by_target: dict[str, set[KnowledgeRule]] = {}
    for violation in check(repository):
        by_target.setdefault(violation.target_id, set()).add(violation.rule)

    for target, found in by_target.items():
        assert not {WITHOUT_EVIDENCE, WITHOUT_ACTIVE} <= found, target
    assert by_target[empty.id.value] == {WITHOUT_EVIDENCE}
    assert by_target[stale.id.value] == {WITHOUT_ACTIVE}


def test_relation_with_no_evidence_reports_only_without_evidence(repository) -> None:
    left = capability("Renovacao")
    right = capability("Cobranca")
    relation = Relation.create("depends_on", left.id, right.id)
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(relation)
    promote_relation(repository, relation.id)

    found = rules_for(repository, relation.id)

    assert found == [WITHOUT_EVIDENCE]


def test_relation_with_only_invalidated_evidence_reports_only_without_active(
    repository,
) -> None:
    left = capability("Renovacao")
    right = capability("Cobranca")
    relation = Relation.create("depends_on", left.id, right.id)
    item = evidence("src/edge.py")
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(relation)
        revision.put_evidence(item, (), [relation.id])
    with repository.begin_revision("pipeline", "invalidacao") as revision:
        revision.invalidate_evidence([item.id], LATER)
    promote_relation(repository, relation.id)

    found = rules_for(repository, relation.id)

    assert found == [WITHOUT_ACTIVE]


def test_reactivated_evidence_clears_both_support_rules(repository) -> None:
    entity = capability()
    item = evidence()
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(entity)
        revision.put_evidence(item, [entity.id])
    with repository.begin_revision("pipeline", "invalidacao") as revision:
        revision.invalidate_evidence([item.id], LATER)
    promote_entity(repository, entity)

    assert rules_for(repository, entity.id.value) == [WITHOUT_ACTIVE]

    with repository.begin_revision("pipeline", "reingestao") as revision:
        revision.put_evidence(item, [entity.id])

    assert rules_for(repository, entity.id.value) == []
