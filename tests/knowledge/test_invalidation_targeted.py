from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    CodeLocator,
    Confidence,
    Entity,
    EpistemicStatus,
    KnowledgeRepository,
    Relation,
    SourceVersion,
    make_evidence,
)
from wiki_ai.knowledge.gaps import GAP_KIND
from wiki_ai.knowledge.invalidation import (
    ENTITY_TARGET,
    EVIDENCE_TARGET,
    RELATION_TARGET,
    apply_targeted,
    entities_of_evidence,
    relations_of_entities,
    surviving_evidence,
)
from wiki_ai.knowledge.repository import DATABASE_FILENAME

CAPTURED = "2026-01-01T00:00:00+00:00"
NAMESPACE = "src_repo"


def version_of(version_hash: str) -> SourceVersion:
    return SourceVersion(NAMESPACE, version_hash, "/repo", CAPTURED)


def evidence_of(version: SourceVersion, path: str, symbol: str):
    locator = CodeLocator(path=path, line_start=1, line_end=10, symbol=symbol)
    return make_evidence(
        version.source_id, version.version_hash, locator, f"code of {symbol}", CAPTURED
    )


def rule(name: str, version: SourceVersion) -> Entity:
    return Entity.create(
        kind="business_rule",
        name=name,
        attributes={
            "statement": name,
            "conditions": ["always"],
            "effects": ["happens"],
        },
        epistemic=EpistemicStatus.IMPLEMENTED,
        confidence=Confidence.SUPPORTED,
        source_versions=(version.key,),
    )


@pytest.fixture()
def loaded(tmp_path):
    repository = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    previous = version_of("v1")
    changed = rule("reference is saved", previous)
    intact = rule("payload is emitted", previous)
    changed_evidence = evidence_of(previous, "src/Service.java", "place")
    intact_evidence = evidence_of(previous, "src/Producer.java", "emit")
    with repository.begin_revision("investigation", "baseline") as revision:
        revision.put_source_version(previous)
        revision.put_entity(changed)
        revision.put_entity(intact)
        revision.put_evidence(changed_evidence, [changed.id])
        revision.put_evidence(intact_evidence, [intact.id])
        revision.put_relation(
            Relation.create(
                "contradicts", changed.id, intact.id, confidence=Confidence.SUPPORTED
            )
        )
    yield repository, previous, changed, intact, changed_evidence, intact_evidence
    repository.close()


def test_entities_of_evidence_finds_only_the_holders(loaded):
    repository, _prev, changed, _intact, changed_evidence, _other = loaded
    assert entities_of_evidence(repository, [changed_evidence.id]) == (changed.id.value,)


def test_entities_of_evidence_is_empty_for_unknown_ids(loaded):
    repository, *_ = loaded
    assert entities_of_evidence(repository, ["evd_absent"]) == ()


def test_relations_of_entities_reaches_both_directions(loaded):
    repository, _prev, changed, intact, *_ = loaded
    from_source = relations_of_entities(repository, [changed.id.value])
    from_target = relations_of_entities(repository, [intact.id.value])
    assert from_source == from_target
    assert len(from_source) == 1


def test_surviving_evidence_reports_what_is_left(loaded):
    repository, _prev, changed, _intact, changed_evidence, _other = loaded
    assert surviving_evidence(repository, changed.id.value, [changed_evidence.id]) == ()
    assert surviving_evidence(repository, changed.id.value, []) == (changed_evidence.id,)


def applied_update(repository, previous, changed, changed_evidence, historical=()):
    return apply_targeted(
        repository=repository,
        incoming=version_of("v2"),
        obsolete_key=previous.key,
        evidence_ids=(changed_evidence.id,),
        entity_ids=(changed.id.value,),
        relation_ids=relations_of_entities(repository, [changed.id.value]),
        historical_ids=historical,
        carried_entities=(),
        carried_evidence=(),
        gap_questions={changed.id.value: "behavior may have changed in src/Service.java"},
        author="update",
        summary="targeted update",
    )


def test_apply_targeted_demotes_only_the_named_entity(loaded):
    repository, previous, changed, intact, changed_evidence, _other = loaded
    applied_update(repository, previous, changed, changed_evidence)
    assert repository.get_entity(changed.id).confidence is Confidence.UNRESOLVED
    assert repository.get_entity(intact.id).confidence is Confidence.SUPPORTED


def test_apply_targeted_preserves_the_epistemic_axis(loaded):
    repository, previous, changed, _intact, changed_evidence, _other = loaded
    applied_update(repository, previous, changed, changed_evidence)
    assert repository.get_entity(changed.id).epistemic is EpistemicStatus.IMPLEMENTED


def test_apply_targeted_marks_historical_when_asked(loaded):
    repository, previous, changed, _intact, changed_evidence, _other = loaded
    applied_update(
        repository, previous, changed, changed_evidence, historical=(changed.id.value,)
    )
    stored = repository.get_entity(changed.id)
    assert stored.epistemic is EpistemicStatus.HISTORICAL
    assert stored.confidence is Confidence.UNRESOLVED


def test_apply_targeted_deletes_nothing(loaded):
    repository, previous, changed, _intact, changed_evidence, _other = loaded
    before = repository.entity_count()
    applied_update(repository, previous, changed, changed_evidence)
    assert repository.entity_count() >= before
    assert repository.get_evidence(changed_evidence.id) is not None


def test_apply_targeted_records_every_target_kind(loaded):
    repository, previous, changed, _intact, changed_evidence, _other = loaded
    applied = applied_update(repository, previous, changed, changed_evidence)
    assert repository.invalidated(ENTITY_TARGET) == (changed.id.value,)
    assert repository.invalidated(EVIDENCE_TARGET) == (changed_evidence.id,)
    assert repository.invalidated(RELATION_TARGET) == applied.relations


def test_apply_targeted_demotes_the_attached_relation(loaded):
    repository, previous, changed, _intact, changed_evidence, _other = loaded
    applied = applied_update(repository, previous, changed, changed_evidence)
    for relation_id in applied.relations:
        assert repository.get_relation(relation_id).confidence is Confidence.UNRESOLVED


def test_apply_targeted_opens_the_gap_about_the_entity(loaded):
    repository, previous, changed, _intact, changed_evidence, _other = loaded
    applied = applied_update(repository, previous, changed, changed_evidence)
    assert applied.gaps == ("behavior may have changed in src/Service.java",)
    names = [gap.name for gap in repository.find_entities(GAP_KIND)]
    assert "behavior may have changed in src/Service.java" in names


def test_apply_targeted_writes_one_revision_authored_by_update(loaded):
    repository, previous, changed, _intact, changed_evidence, _other = loaded
    before = repository.revision_count()
    applied = applied_update(repository, previous, changed, changed_evidence)
    assert repository.revision_count() == before + 1
    assert repository.get_revision(applied.revision_id).author == "update"


def test_apply_targeted_registers_the_incoming_version(loaded):
    repository, previous, changed, _intact, changed_evidence, _other = loaded
    applied_update(repository, previous, changed, changed_evidence)
    hashes = {item.version_hash for item in repository.source_versions(NAMESPACE)}
    assert hashes == {"v1", "v2"}


def test_apply_targeted_is_idempotent(loaded):
    repository, previous, changed, _intact, changed_evidence, _other = loaded
    first = applied_update(repository, previous, changed, changed_evidence)
    counts = (repository.entity_count(), repository.relation_count())
    second = applied_update(repository, previous, changed, changed_evidence)
    assert second.entities == first.entities
    assert set(first.relations) <= set(second.relations)
    assert (repository.entity_count(), repository.relation_count()) == counts


def test_carried_over_targets_are_recorded_without_losing_supported(loaded):
    repository, previous, _changed, intact, _changed_evidence, intact_evidence = loaded
    applied = apply_targeted(
        repository=repository,
        incoming=version_of("v2"),
        obsolete_key=previous.key,
        evidence_ids=(),
        entity_ids=(),
        relation_ids=(),
        historical_ids=(),
        carried_entities=(intact.id.value,),
        carried_evidence=(intact_evidence.id,),
        gap_questions={},
        author="update",
        summary="carry over",
    )
    assert applied.carried_over_entities == (intact.id.value,)
    assert repository.get_entity(intact.id).confidence is Confidence.SUPPORTED
    assert repository.invalidated(ENTITY_TARGET) == (intact.id.value,)
