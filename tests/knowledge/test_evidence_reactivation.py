from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    CodeContent,
    CodeLocator,
    Confidence,
    Entity,
    KnowledgeRepository,
    KnowledgeState,
    Relation,
    SourceVersion,
    make_evidence,
)
from wiki_ai.knowledge.errors import PayloadInvalid
from wiki_ai.knowledge.repository import DATABASE_FILENAME

CAPTURED = "2026-01-01T00:00:00+00:00"
LATER = "2026-02-01T00:00:00+00:00"
SOURCE = "src_repo"
HASH = "codehash1"


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


def locator(path: str = "src/renewal.py") -> CodeLocator:
    return CodeLocator(
        path=path,
        line_start=1,
        line_end=40,
        symbol="renew",
        content=CodeContent.EXECUTABLE,
    )


def evidence(excerpt: str = "def renew(order): return order"):
    return make_evidence(SOURCE, HASH, locator(), excerpt, CAPTURED)


def capability() -> Entity:
    return Entity.create(
        kind="capability",
        name="Renovacao",
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.INFERRED,
    )


def test_reput_of_an_invalidated_evidence_reactivates_it(repository) -> None:
    entity = capability()
    item = evidence()
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(entity)
        revision.put_evidence(item, [entity.id])
    with repository.begin_revision("pipeline", "invalidacao") as revision:
        revision.invalidate_evidence([item.id], LATER)

    assert repository.evidence_for(entity.id) == []
    assert repository.evidence_for(entity.id, active_only=False)

    with repository.begin_revision("pipeline", "reingestao") as revision:
        revision.put_evidence(item, [entity.id])

    active = repository.evidence_for(entity.id)
    assert [found.id for found in active] == [item.id]
    assert active[0].active is True
    assert active[0].invalidated_at == ""


def test_reactivated_evidence_sustains_a_supported_entity(repository) -> None:
    entity = capability()
    item = evidence()
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(entity)
        revision.put_evidence(item, [entity.id])
    with repository.begin_revision("pipeline", "invalidacao") as revision:
        revision.invalidate_evidence([item.id], LATER)
    with repository.begin_revision("pipeline", "reingestao") as revision:
        revision.put_evidence(item, [entity.id])
        revision.put_entity(entity.with_confidence(Confidence.SUPPORTED))

    stored = repository.get_entity(entity.id)
    assert stored is not None
    assert stored.confidence is Confidence.SUPPORTED


def test_reput_of_an_invalidated_relation_evidence_reactivates_it(repository) -> None:
    left = capability()
    right = Entity.create(
        kind="integration",
        name="Billing API",
        attributes={"direction": "outbound", "protocol": "http"},
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.INFERRED,
    )
    relation = Relation.create("calls", left.id, right.id)
    item = evidence("self.billing.charge(order)")
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(relation)
        revision.put_evidence(item, (), [relation.id])
    with repository.begin_revision("pipeline", "invalidacao") as revision:
        revision.invalidate_evidence([item.id], LATER)

    assert repository.evidence_for_relation(relation.id) == []

    with repository.begin_revision("pipeline", "reingestao") as revision:
        revision.put_evidence(item, (), [relation.id])
        revision.put_relation(relation.with_confidence(Confidence.SUPPORTED))

    stored = repository.get_relation(relation.id)
    assert stored is not None
    assert stored.confidence is Confidence.SUPPORTED
    assert [found.id for found in repository.evidence_for_relation(relation.id)] == [
        item.id
    ]


def test_reput_with_a_different_excerpt_under_the_same_id_is_rejected(
    repository,
) -> None:
    entity = capability()
    first = evidence()
    second = evidence("def renew(order): raise Rejected(order)")
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(entity)
        revision.put_evidence(first, [entity.id])

    assert second.id == first.id
    assert second.excerpt_hash != first.excerpt_hash

    with pytest.raises(PayloadInvalid):
        with repository.begin_revision("pipeline", "reingestao") as revision:
            revision.put_evidence(second, [entity.id])

    stored = repository.evidence_for(entity.id)
    assert [found.excerpt_hash for found in stored] == [first.excerpt_hash]


def test_reput_with_a_different_excerpt_is_rejected_after_invalidation(
    repository,
) -> None:
    entity = capability()
    first = evidence()
    second = evidence("def renew(order): raise Rejected(order)")
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(entity)
        revision.put_evidence(first, [entity.id])
    with repository.begin_revision("pipeline", "invalidacao") as revision:
        revision.invalidate_evidence([first.id], LATER)

    with pytest.raises(PayloadInvalid):
        with repository.begin_revision("pipeline", "reingestao") as revision:
            revision.put_evidence(second, [entity.id])

    assert repository.evidence_for(entity.id) == []


def test_invalidation_of_one_evidence_does_not_reactivate_another(repository) -> None:
    entity = capability()
    first = evidence()
    second = make_evidence(
        SOURCE, HASH, locator("src/other.py"), "def other(): pass", CAPTURED
    )
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version())
        revision.put_entity(entity)
        revision.put_evidence(first, [entity.id])
        revision.put_evidence(second, [entity.id])
    with repository.begin_revision("pipeline", "invalidacao") as revision:
        revision.invalidate_evidence([first.id, second.id], LATER)
    with repository.begin_revision("pipeline", "reingestao") as revision:
        revision.put_evidence(first, [entity.id])

    assert [found.id for found in repository.evidence_for(entity.id)] == [first.id]
    assert len(repository.evidence_for(entity.id, active_only=False)) == 2
