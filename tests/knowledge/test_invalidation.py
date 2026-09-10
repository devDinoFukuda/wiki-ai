from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    CodeLocator,
    Confidence,
    Entity,
    EpistemicStatus,
    KnowledgeRepository,
    SourceVersion,
    invalidate,
    make_evidence,
)
from wiki_ai.knowledge.invalidation import (
    ENTITY_TARGET,
    EVIDENCE_TARGET,
    dependents,
    obsolete_versions,
)
from wiki_ai.knowledge.repository import DATABASE_FILENAME

CAPTURED = "2026-01-01T00:00:00+00:00"


def version_of(version_hash, source_id="src_repo"):
    return SourceVersion(source_id, version_hash, "/repo", CAPTURED)


def evidence_of(version, path="src/a.py"):
    locator = CodeLocator(path=path, line_start=1, line_end=10)
    return make_evidence(version.source_id, version.version_hash, locator, "codigo", CAPTURED)


@pytest.fixture()
def loaded(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    first = version_of("v1")
    other = version_of("w1", source_id="src_docs")
    dependent = Entity.create(
        kind="capability",
        name="Pedido",
        epistemic=EpistemicStatus.IMPLEMENTED,
        confidence=Confidence.SUPPORTED,
        source_versions=(first.key,),
    )
    untouched = Entity.create(
        kind="capability",
        name="Catálogo",
        epistemic=EpistemicStatus.DECLARED,
        confidence=Confidence.SUPPORTED,
        source_versions=(other.key,),
    )
    evidence = evidence_of(first)
    with repo.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(first)
        revision.put_source_version(other)
        revision.put_entity(dependent)
        revision.put_entity(untouched)
        revision.put_evidence(evidence, [dependent.id])
    yield repo, first, other, dependent, untouched, evidence
    repo.close()


def test_obsolete_versions_lists_previous_hash_of_same_source(loaded):
    repo, first, _other, _dep, _untouched, _ev = loaded
    assert obsolete_versions(repo, [version_of("v2")]) == (first.key,)


def test_unchanged_hash_is_not_obsolete(loaded):
    repo, first, *_ = loaded
    assert obsolete_versions(repo, [version_of(first.version_hash)]) == ()


def test_dependents_reach_entities_and_evidence(loaded):
    repo, first, _other, dependent, _untouched, evidence = loaded
    entities, evidences = dependents(repo, [first.key])
    assert set(entities) == {dependent.id.value}
    assert set(evidences) == {evidence.id}


def test_invalidate_marks_dependent_entity_unresolved(loaded):
    repo, _first, _other, dependent, _untouched, _ev = loaded
    result = invalidate(repo, [version_of("v2")], author="pipeline")
    stored = repo.get_entity(dependent.id)
    assert stored.confidence is Confidence.UNRESOLVED
    assert dependent.id.value in result.entities


def test_invalidate_preserves_the_epistemic_axis(loaded):
    repo, _first, _other, dependent, _untouched, _ev = loaded
    invalidate(repo, [version_of("v2")], author="pipeline")
    assert repo.get_entity(dependent.id).epistemic is EpistemicStatus.IMPLEMENTED


def test_invalidate_does_not_delete_anything(loaded):
    repo, _first, _other, dependent, _untouched, evidence = loaded
    before_entities = repo.entity_count()
    invalidate(repo, [version_of("v2")], author="pipeline")
    assert repo.entity_count() == before_entities
    assert repo.get_evidence(evidence.id) is not None
    assert repo.evidence_for(dependent.id)[0].id == evidence.id


def test_invalidate_leaves_independent_knowledge_untouched(loaded):
    repo, _first, _other, _dependent, untouched, _ev = loaded
    result = invalidate(repo, [version_of("v2")], author="pipeline")
    assert untouched.id.value not in result.entities
    assert repo.get_entity(untouched.id).confidence is Confidence.SUPPORTED


def test_invalidate_records_the_affected_evidence(loaded):
    repo, _first, _other, dependent, _untouched, evidence = loaded
    result = invalidate(repo, [version_of("v2")], author="pipeline")
    assert result.evidence == (evidence.id,)
    assert repo.invalidated(EVIDENCE_TARGET) == (evidence.id,)
    assert repo.invalidated(ENTITY_TARGET) == (dependent.id.value,)
    assert result.total == 2


def test_invalidate_registers_the_new_source_version(loaded):
    repo, first, *_ = loaded
    incoming = version_of("v2")
    invalidate(repo, [incoming], author="pipeline")
    hashes = {v.version_hash for v in repo.source_versions(first.source_id)}
    assert hashes == {"v1", "v2"}


def test_invalidate_creates_a_single_new_revision(loaded):
    repo, *_ = loaded
    before = repo.revision_count()
    result = invalidate(repo, [version_of("v2")], author="pipeline", summary="nova varredura")
    assert repo.revision_count() == before + 1
    assert repo.get_revision(result.revision_id).summary == "nova varredura"


def test_invalidate_is_idempotent_for_the_same_incoming_version(loaded):
    repo, _first, _other, dependent, *_ = loaded
    first_pass = invalidate(repo, [version_of("v2")], author="pipeline")
    second_pass = invalidate(repo, [version_of("v2")], author="pipeline")
    assert first_pass.entities == (dependent.id.value,)
    assert second_pass.entities == (dependent.id.value,)
    assert repo.invalidated(ENTITY_TARGET) == (dependent.id.value,)


def test_historical_entities_are_not_revalidated(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    version = version_of("v1")
    past = Entity.create(
        kind="capability",
        name="Fluxo antigo",
        epistemic=EpistemicStatus.HISTORICAL,
        confidence=Confidence.SUPPORTED,
        source_versions=(version.key,),
    )
    with repo.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version)
        revision.put_entity(past)
    result = invalidate(repo, [version_of("v2")], author="pipeline")
    assert result.entities == ()
    assert repo.get_entity(past.id).confidence is Confidence.SUPPORTED
    repo.close()


def test_invalidation_without_dependents_changes_nothing(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    result = invalidate(repo, [version_of("v1")], author="pipeline")
    assert result.obsolete_versions == ()
    assert result.total == 0
    assert repo.source_versions() != []
    repo.close()
