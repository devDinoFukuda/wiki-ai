from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    CodeLocator,
    Confidence,
    Entity,
    KnowledgeRepository,
    KnowledgeState,
    Relation,
    SourceVersion,
    make_evidence,
)
from wiki_ai.knowledge.gate import KnowledgeRule, check
from wiki_ai.knowledge.invalidation import (
    apply_targeted,
    relations_of_entities,
    relations_of_evidence,
)
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import DATABASE_FILENAME

CAPTURED = "2026-01-01T00:00:00+00:00"
LATER = "2026-02-01T00:00:00+00:00"
NAMESPACE = "src_repo"


def version_of(version_hash: str) -> SourceVersion:
    return SourceVersion(NAMESPACE, version_hash, "/repo", CAPTURED)


def evidence_of(version: SourceVersion, path: str, symbol: str):
    locator = CodeLocator(path=path, line_start=1, line_end=10, symbol=symbol)
    return make_evidence(
        version.source_id, version.version_hash, locator, f"code of {symbol}", CAPTURED
    )


def capability(name: str, version: SourceVersion) -> Entity:
    return Entity.create(
        kind="capability",
        name=name,
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.SUPPORTED,
        source_versions=(version.key,),
    )


class Graph:
    def __init__(self, repository, version, source, target, relation, relation_evidence, entity_evidence):
        self.repository = repository
        self.version = version
        self.source = source
        self.target = target
        self.relation = relation
        self.relation_evidence = relation_evidence
        self.entity_evidence = entity_evidence


@pytest.fixture()
def graph(tmp_path):
    repository = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    version = version_of("v1")
    source = capability("place order", version)
    target = capability("store order", version)
    relation_evidence = evidence_of(version, "src/Service.java", "place")
    entity_evidence = evidence_of(version, "src/Storage.java", "store")
    with repository.begin_revision("investigation", "baseline") as revision:
        revision.put_source_version(version)
        revision.put_entity(source)
        revision.put_entity(target)
        revision.put_evidence(entity_evidence, [source.id, target.id])
        relation = Relation.create(
            "calls", source.id, target.id, confidence=Confidence.SUPPORTED
        )
        revision.put_relation(relation)
        revision.put_evidence(relation_evidence, relation_ids=(relation.id,))
    yield Graph(
        repository, version, source, target, relation, relation_evidence, entity_evidence
    )
    repository.close()


def test_stored_evidence_starts_active(graph):
    stored = graph.repository.get_evidence(graph.relation_evidence.id)

    assert stored.invalidated_at == ""
    assert stored.active is True


def test_invalidate_evidence_stamps_the_moment(graph):
    with graph.repository.begin_revision("update", "invalidate") as revision:
        revision.invalidate_evidence([graph.relation_evidence.id], LATER)
        revision.put_relation(graph.relation.with_confidence(Confidence.UNRESOLVED))

    stored = graph.repository.get_evidence(graph.relation_evidence.id, active_only=False)
    assert stored.invalidated_at == LATER
    assert stored.active is False


def test_invalidated_evidence_disappears_from_the_active_view(graph):
    with graph.repository.begin_revision("update", "invalidate") as revision:
        revision.invalidate_evidence([graph.entity_evidence.id], LATER)
        revision.put_entity(graph.source.with_confidence(Confidence.UNRESOLVED))
        revision.put_entity(graph.target.with_confidence(Confidence.UNRESOLVED))

    assert graph.repository.evidence_for(graph.source.id) == []
    assert graph.repository.get_evidence(graph.entity_evidence.id) is None


def test_physical_evidence_survives_invalidation(graph):
    with graph.repository.begin_revision("update", "invalidate") as revision:
        revision.invalidate_evidence([graph.entity_evidence.id], LATER)
        revision.put_entity(graph.source.with_confidence(Confidence.UNRESOLVED))
        revision.put_entity(graph.target.with_confidence(Confidence.UNRESOLVED))

    physical = graph.repository.evidence_for(graph.source.id, active_only=False)
    assert [item.id for item in physical] == [graph.entity_evidence.id]
    assert graph.repository.get_evidence(
        graph.entity_evidence.id, active_only=False
    ) is not None


def test_invalidating_unknown_evidence_is_refused(graph):
    from wiki_ai.knowledge import UnknownReference

    with pytest.raises(UnknownReference):
        with graph.repository.begin_revision("update", "invalidate") as revision:
            revision.invalidate_evidence(["evd_absent"], LATER)


def test_relation_evidence_active_view_follows_invalidation(graph):
    with graph.repository.begin_revision("update", "invalidate") as revision:
        revision.invalidate_evidence([graph.relation_evidence.id], LATER)
        revision.put_relation(graph.relation.with_confidence(Confidence.UNRESOLVED))

    assert graph.repository.evidence_for_relation(graph.relation.id) == []
    physical = graph.repository.evidence_for_relation(graph.relation.id, active_only=False)
    assert [item.id for item in physical] == [graph.relation_evidence.id]


def test_supported_relation_with_invalidated_evidence_is_flagged_by_the_gate(graph):
    with graph.repository.begin_revision("update", "invalidate") as revision:
        revision.invalidate_evidence([graph.relation_evidence.id], LATER)
        revision.put_relation(graph.relation.with_confidence(Confidence.UNRESOLVED))
    graph.repository.conn.execute(
        "UPDATE relations SET confidence='supported' WHERE relation_id=?",
        (graph.relation.id,),
    )

    found = check(graph.repository, {NAMESPACE: "v1"})

    rules = {violation.rule for violation in found}
    assert KnowledgeRule.SUPPORTED_WITHOUT_ACTIVE_EVIDENCE in rules
    flagged = [
        violation
        for violation in found
        if violation.rule is KnowledgeRule.SUPPORTED_WITHOUT_ACTIVE_EVIDENCE
    ]
    assert flagged[0].target_id == graph.relation.id


def test_supported_entity_with_invalidated_evidence_is_flagged_by_the_gate(graph):
    with graph.repository.begin_revision("update", "invalidate") as revision:
        revision.invalidate_evidence([graph.entity_evidence.id], LATER)
        revision.put_entity(graph.source.with_confidence(Confidence.UNRESOLVED))
        revision.put_entity(graph.target.with_confidence(Confidence.UNRESOLVED))
    graph.repository.conn.execute(
        "UPDATE entities SET confidence='supported' WHERE entity_id=?",
        (graph.source.id.value,),
    )

    found = check(graph.repository, {NAMESPACE: "v1"})

    flagged = [
        violation
        for violation in found
        if violation.rule is KnowledgeRule.SUPPORTED_WITHOUT_ACTIVE_EVIDENCE
    ]
    assert [violation.target_id for violation in flagged] == [graph.source.id.value]


def test_healthy_graph_has_no_active_evidence_violation(graph):
    found = check(graph.repository, {NAMESPACE: "v1"})

    assert found == ()


def test_query_evidence_reports_only_active_items(graph):
    with graph.repository.begin_revision("update", "invalidate") as revision:
        revision.invalidate_evidence([graph.entity_evidence.id], LATER)
        revision.put_entity(graph.source.with_confidence(Confidence.UNRESOLVED))
        revision.put_entity(graph.target.with_confidence(Confidence.UNRESOLVED))

    query = KnowledgeQuery(graph.repository)

    assert query.evidence_of(graph.source.id) == ()


def test_relations_of_evidence_finds_the_relation_link(graph):
    assert relations_of_evidence(graph.repository, [graph.relation_evidence.id]) == (
        graph.relation.id,
    )


def test_relations_of_evidence_ignores_entity_only_evidence(graph):
    assert relations_of_evidence(graph.repository, [graph.entity_evidence.id]) == ()


def test_relations_of_evidence_is_empty_for_unknown_ids(graph):
    assert relations_of_evidence(graph.repository, ["evd_absent"]) == ()


def test_relation_evidence_alone_demotes_the_relation(graph):
    applied = apply_targeted(
        repository=graph.repository,
        incoming=version_of("v2"),
        obsolete_key=graph.version.key,
        evidence_ids=(graph.relation_evidence.id,),
        entity_ids=(),
        relation_ids=(),
        historical_ids=(),
        carried_entities=(),
        carried_evidence=(),
        gap_questions={},
        author="update",
        summary="relation evidence changed",
    )

    assert applied.relation_ids == (graph.relation.id,)
    assert graph.repository.get_relation(graph.relation.id).confidence is (
        Confidence.UNRESOLVED
    )


def test_relation_evidence_alone_leaves_the_entities_intact(graph):
    apply_targeted(
        repository=graph.repository,
        incoming=version_of("v2"),
        obsolete_key=graph.version.key,
        evidence_ids=(graph.relation_evidence.id,),
        entity_ids=(),
        relation_ids=(),
        historical_ids=(),
        carried_entities=(),
        carried_evidence=(),
        gap_questions={},
        author="update",
        summary="relation evidence changed",
    )

    assert graph.repository.get_entity(graph.source.id).confidence is Confidence.SUPPORTED
    assert graph.repository.get_entity(graph.target.id).confidence is Confidence.SUPPORTED
    assert graph.repository.evidence_for(graph.source.id) != []


def test_relation_ids_unions_entity_and_evidence_reach(graph):
    applied = apply_targeted(
        repository=graph.repository,
        incoming=version_of("v2"),
        obsolete_key=graph.version.key,
        evidence_ids=(graph.relation_evidence.id,),
        entity_ids=(graph.source.id.value,),
        relation_ids=relations_of_entities(graph.repository, [graph.source.id.value]),
        historical_ids=(),
        carried_entities=(),
        carried_evidence=(),
        gap_questions={},
        author="update",
        summary="both reaches",
    )

    assert applied.relation_ids == (graph.relation.id,)
    assert applied.relations == applied.relation_ids


def test_relation_kept_by_older_evidence_becomes_inferred(graph):
    repository = graph.repository
    older = version_of("v0")
    older_evidence = evidence_of(older, "src/Older.java", "place")
    with repository.begin_revision("investigation", "older support") as revision:
        revision.put_source_version(older)
        revision.put_evidence(older_evidence, relation_ids=(graph.relation.id,))

    applied = apply_targeted(
        repository=repository,
        incoming=version_of("v2"),
        obsolete_key=graph.version.key,
        evidence_ids=(graph.relation_evidence.id,),
        entity_ids=(),
        relation_ids=(),
        historical_ids=(),
        carried_entities=(),
        carried_evidence=(),
        gap_questions={},
        author="update",
        summary="only stale evidence survives",
    )

    assert applied.relation_ids == (graph.relation.id,)
    assert repository.get_relation(graph.relation.id).confidence is Confidence.INFERRED


def test_gate_is_quiet_after_a_targeted_update(graph):
    apply_targeted(
        repository=graph.repository,
        incoming=version_of("v2"),
        obsolete_key=graph.version.key,
        evidence_ids=(graph.relation_evidence.id,),
        entity_ids=(),
        relation_ids=(),
        historical_ids=(),
        carried_entities=(),
        carried_evidence=(),
        gap_questions={},
        author="update",
        summary="relation evidence changed",
    )

    flagged = [
        violation
        for violation in check(graph.repository)
        if violation.rule is KnowledgeRule.SUPPORTED_WITHOUT_ACTIVE_EVIDENCE
    ]
    assert flagged == []
