from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    Confidence,
    Entity,
    EntityId,
    EpistemicStatus,
    KnowledgeRepository,
    Relation,
    SourceVersion,
    invalidate,
)
from wiki_ai.knowledge.gate import KnowledgeRule, check
from wiki_ai.knowledge.repository import DATABASE_FILENAME

from .graph_fixture import (
    CAPTURED,
    CODE_HASH,
    CODE_SOURCE,
    DOC_HASH,
    DOC_SOURCE,
    build,
    code_evidence,
    code_version,
)


@pytest.fixture()
def graph(tmp_path):
    fixture = build(tmp_path)
    yield fixture
    fixture.repository.close()


def current(code=CODE_HASH, doc=DOC_HASH):
    return {CODE_SOURCE: code, DOC_SOURCE: doc}


def rules(violations):
    return sorted({violation.rule for violation in violations}, key=lambda r: r.value)


def test_healthy_graph_has_no_violations(graph):
    assert check(graph.repository, current()) == ()


def test_gate_without_current_versions_skips_hash_checks(graph):
    assert check(graph.repository) == ()


def test_supported_entity_without_evidence_is_a_violation(graph):
    repo = graph.repository
    entity = Entity.create(
        kind="table",
        name="orphan",
        attributes={"schema": "public"},
        epistemic=EpistemicStatus.DECLARED,
        confidence=Confidence.SUPPORTED,
    )
    with repo.begin_revision("pipeline", "sem evidencia") as revision:
        revision.put_entity(entity)
    found = check(repo, current())
    assert rules(found) == [KnowledgeRule.SUPPORTED_WITHOUT_EVIDENCE]
    assert found[0].target_id == entity.id.value
    assert found[0].target_kind == "entity"


def test_supported_relation_without_evidence_is_a_violation(graph):
    repo = graph.repository
    relation = Relation.create(
        "calls", graph.id("capability"), graph.id("integration")
    )
    with repo.begin_revision("pipeline", "relacao") as revision:
        revision.put_relation(relation)
    repo.conn.execute(
        "UPDATE relations SET confidence='supported' WHERE relation_id=?",
        (relation.id,),
    )
    found = check(repo, current())
    assert rules(found) == [KnowledgeRule.SUPPORTED_WITHOUT_EVIDENCE]
    assert found[0].target_kind == "relation"
    assert found[0].target_id == relation.id


def test_supported_relation_with_supported_endpoints_passes(graph):
    repo = graph.repository
    relation = Relation.create(
        "calls", graph.id("capability"), graph.id("integration")
    )
    with repo.begin_revision("pipeline", "relacao") as revision:
        revision.put_relation(relation)
        revision.put_evidence(
            code_evidence(graph.code_version, "src/integration.py"),
            [graph.id("integration")],
        )
    repo.conn.execute(
        "UPDATE relations SET confidence='supported' WHERE relation_id=?",
        (relation.id,),
    )
    repo.conn.execute("UPDATE entities SET confidence='supported' WHERE entity_id IN (?,?)",
                      (graph.id("capability").value, graph.id("integration").value))
    assert check(repo, current()) == ()


def test_evidence_pointing_to_missing_source_version_is_a_violation(graph):
    repo = graph.repository
    ghost = "srv_fantasma"
    repo.conn.execute(
        "UPDATE evidence SET source_version_key=? WHERE evidence_id="
        "(SELECT evidence_id FROM evidence ORDER BY evidence_id LIMIT 1)",
        (ghost,),
    )
    found = check(repo, current())
    assert KnowledgeRule.EVIDENCE_DOES_NOT_RESOLVE in rules(found)
    assert any(ghost in violation.detail for violation in found)


def test_diverging_source_hash_is_a_violation(graph):
    found = check(graph.repository, current(code="outrohash"))
    assert KnowledgeRule.SOURCE_HASH_DIVERGES in rules(found)
    diverged = [
        v for v in found if v.rule is KnowledgeRule.SOURCE_HASH_DIVERGES
    ]
    assert diverged[0].target_kind == "source_version"
    assert "outrohash" in diverged[0].detail


def test_obsolete_version_without_invalidation_is_a_violation(graph):
    found = check(graph.repository, current(code="outrohash"))
    obsolete = [
        v for v in found if v.rule is KnowledgeRule.OBSOLETE_WITHOUT_INVALIDATION
    ]
    assert obsolete
    assert {v.target_kind for v in obsolete} <= {"entity", "evidence"}


def test_invalidation_clears_the_obsolete_violation(graph):
    repo = graph.repository
    newer = SourceVersion(
        source_id=CODE_SOURCE,
        version_hash="outrohash",
        locator_root="/repo",
        captured_at=CAPTURED,
    )
    invalidate(repo, [newer], author="pipeline", summary="nova versao")
    found = check(repo, {CODE_SOURCE: "outrohash", DOC_SOURCE: DOC_HASH})
    assert [
        v for v in found if v.rule is KnowledgeRule.OBSOLETE_WITHOUT_INVALIDATION
    ] == []


def test_violations_are_sorted_deterministically(graph):
    first = check(graph.repository, current(code="a"))
    second = check(graph.repository, current(code="a"))
    assert first == second


def test_gate_on_empty_repository_is_clean(tmp_path):
    with KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME)) as repo:
        assert check(repo, {}) == ()


def test_unknown_source_in_current_versions_is_ignored(graph):
    assert check(graph.repository, {**current(), "src_outro": "x"}) == ()


def test_gate_reports_every_rule_together(graph):
    repo = graph.repository
    entity = Entity.create(
        kind="table",
        name="orphan",
        attributes={"schema": "public"},
        confidence=Confidence.SUPPORTED,
    )
    with repo.begin_revision("pipeline", "sujeira") as revision:
        revision.put_entity(entity)
    repo.conn.execute(
        "UPDATE evidence SET source_version_key='srv_fantasma' WHERE evidence_id="
        "(SELECT evidence_id FROM evidence ORDER BY evidence_id LIMIT 1)"
    )
    found = check(repo, current(code="outrohash"))
    assert set(rules(found)) == {
        KnowledgeRule.EVIDENCE_DOES_NOT_RESOLVE,
        KnowledgeRule.OBSOLETE_WITHOUT_INVALIDATION,
        KnowledgeRule.SOURCE_HASH_DIVERGES,
        KnowledgeRule.SUPPORTED_WITHOUT_EVIDENCE,
    }


def test_gate_ignores_entities_without_supported_confidence(graph):
    repo = graph.repository
    with repo.begin_revision("pipeline", "inferido") as revision:
        revision.put_entity(
            Entity.create(
                kind="table",
                name="inferida",
                attributes={"schema": "public"},
                confidence=Confidence.INFERRED,
            )
        )
    assert check(repo, current()) == ()


def test_gate_detects_supported_entity_created_by_id(graph):
    repo = graph.repository
    repo.conn.execute(
        "UPDATE entities SET confidence='supported' WHERE entity_id=?",
        (graph.id("table").value,),
    )
    found = check(repo, current())
    assert [v.target_id for v in found] == [graph.id("table").value]
    assert repo.get_entity(EntityId(graph.id("table").value)) is not None
