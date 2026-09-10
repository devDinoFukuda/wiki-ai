from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    CodeContent,
    CodeLocator,
    Confidence,
    Entity,
    KnowledgeRepository,
    KnowledgeRule,
    Relation,
    RelationEvidenceRequired,
    SourceVersion,
    knowledge_gate,
    make_evidence,
)
from wiki_ai.knowledge.repository import DATABASE_FILENAME

CAPTURED = "2026-01-01T00:00:00+00:00"


@pytest.fixture()
def repository(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    yield repo
    repo.close()


def a_source_version() -> SourceVersion:
    return SourceVersion(
        source_id="src_repo",
        version_hash="v1",
        locator_root="/repo",
        captured_at=CAPTURED,
    )


def code_evidence(version: SourceVersion, path: str = "src/order.py"):
    return make_evidence(
        version.source_id,
        version.version_hash,
        CodeLocator(path=path, line_start=1, line_end=20, content=CodeContent.EXECUTABLE),
        "codigo",
        CAPTURED,
    )


def supported_pair(
    revision, version: SourceVersion
) -> tuple[Entity, Entity]:
    left = Entity.create(kind="capability", name="Pedido", confidence=Confidence.SUPPORTED)
    right = Entity.create(kind="capability", name="Fatura", confidence=Confidence.SUPPORTED)
    revision.put_entity(left)
    revision.put_entity(right)
    revision.put_evidence(code_evidence(version, "src/left.py"), [left.id])
    revision.put_evidence(code_evidence(version, "src/right.py"), [right.id])
    return left, right


def test_supported_relation_between_supported_endpoints_is_rejected(repository):
    version = a_source_version()
    with pytest.raises(RelationEvidenceRequired) as raised:
        with repository.begin_revision("pipeline", "sem lastro") as revision:
            revision.put_source_version(version)
            left, right = supported_pair(revision, version)
            revision.put_relation(
                Relation.create(
                    "calls", left.id, right.id, confidence=Confidence.SUPPORTED
                )
            )

    assert raised.value.kind == "calls"
    assert repository.relation_count() == 0
    assert repository.entity_count() == 0


def test_supported_relation_with_its_own_evidence_is_accepted(repository):
    version = a_source_version()
    with repository.begin_revision("pipeline", "com lastro") as revision:
        revision.put_source_version(version)
        left, right = supported_pair(revision, version)
        relation = Relation.create(
            "calls", left.id, right.id, confidence=Confidence.SUPPORTED
        )
        revision.put_relation(relation)
        revision.put_evidence(
            code_evidence(version, "src/edge.py"), relation_ids=(relation.id,)
        )

    stored = repository.get_relation(relation.id)
    assert stored is not None
    assert stored.confidence is Confidence.SUPPORTED
    assert repository.evidence_for_relation(relation.id)


def test_inferred_relation_needs_no_evidence(repository):
    version = a_source_version()
    with repository.begin_revision("pipeline", "inferida") as revision:
        revision.put_source_version(version)
        left, right = supported_pair(revision, version)
        relation = Relation.create(
            "calls", left.id, right.id, confidence=Confidence.INFERRED
        )
        revision.put_relation(relation)

    stored = repository.get_relation(relation.id)
    assert stored is not None
    assert stored.confidence is Confidence.INFERRED
    assert repository.evidence_for_relation(relation.id) == []


def test_relation_evidence_required_carries_the_endpoints(repository):
    version = a_source_version()
    with pytest.raises(RelationEvidenceRequired) as raised:
        with repository.begin_revision("pipeline", "sem lastro") as revision:
            revision.put_source_version(version)
            left, right = supported_pair(revision, version)
            relation = Relation.create(
                "calls", left.id, right.id, confidence=Confidence.SUPPORTED
            )
            revision.put_relation(relation)

    assert raised.value.relation_id == relation.id
    assert raised.value.source_id == left.id.value
    assert raised.value.target_id == right.id.value


def test_gate_flags_supported_relation_without_its_own_evidence(repository):
    version = a_source_version()
    with repository.begin_revision("pipeline", "inferida") as revision:
        revision.put_source_version(version)
        left, right = supported_pair(revision, version)
        relation = Relation.create(
            "calls", left.id, right.id, confidence=Confidence.INFERRED
        )
        revision.put_relation(relation)
    repository.conn.execute(
        "UPDATE relations SET confidence=? WHERE relation_id=?",
        (Confidence.SUPPORTED.value, relation.id),
    )

    violations = knowledge_gate(repository)

    relation_violations = [
        item for item in violations if item.target_id == relation.id
    ]
    assert len(relation_violations) == 1
    assert relation_violations[0].rule is KnowledgeRule.SUPPORTED_WITHOUT_EVIDENCE
    assert "evidência própria" in relation_violations[0].detail


def test_gate_accepts_supported_relation_carrying_evidence(repository):
    version = a_source_version()
    with repository.begin_revision("pipeline", "com lastro") as revision:
        revision.put_source_version(version)
        left, right = supported_pair(revision, version)
        relation = Relation.create(
            "calls", left.id, right.id, confidence=Confidence.SUPPORTED
        )
        revision.put_relation(relation)
        revision.put_evidence(
            code_evidence(version, "src/edge.py"), relation_ids=(relation.id,)
        )

    assert [item for item in knowledge_gate(repository) if item.target_id == relation.id] == []
