from __future__ import annotations

import hashlib
import sqlite3

import pytest

from wiki_ai.knowledge import (
    CodeContent,
    CodeLocator,
    Confidence,
    Entity,
    EntityId,
    KnowledgeState,
    KnowledgeRepository,
    PayloadInvalid,
    Relation,
    SourceVersion,
    make_evidence,
)
from wiki_ai.knowledge.evidence import excerpt_digest
from wiki_ai.knowledge.gate import KnowledgeRule, check
from wiki_ai.knowledge.model import Evidence
from wiki_ai.knowledge.repository import DATABASE_FILENAME, FORMAT_VERSION

CAPTURED = "2026-01-01T00:00:00+00:00"
SOURCE = "src_repo"
VERSION = "codehash1"
EXCERPT = "def renew(order):\n    return order.active"



def a_version():
    return SourceVersion(
        source_id=SOURCE,
        version_hash=VERSION,
        locator_root="/repo",
        captured_at=CAPTURED,
    )


def a_locator(path="src/renewal.py"):
    return CodeLocator(
        path=path, line_start=1, line_end=20, content=CodeContent.EXECUTABLE
    )


def an_evidence(path="src/renewal.py", excerpt=EXCERPT):
    return make_evidence(SOURCE, VERSION, a_locator(path), excerpt, CAPTURED)


def a_table(confidence=Confidence.SUPPORTED, name="renewals"):
    return Entity.create(
        kind="table",
        name=name,
        attributes={"schema": "public"},
        state=KnowledgeState.DECLARED,
        confidence=confidence,
    )


@pytest.fixture()
def repository(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    yield repo
    repo.close()


def test_excerpt_digest_is_plain_sha256_of_utf8():
    assert excerpt_digest(EXCERPT) == hashlib.sha256(
        EXCERPT.encode("utf-8")
    ).hexdigest()


def test_make_evidence_carries_the_excerpt_and_its_hash():
    evidence = an_evidence()
    assert evidence.excerpt == EXCERPT
    assert evidence.excerpt_hash == excerpt_digest(EXCERPT)


def test_evidence_defaults_to_empty_excerpt():
    evidence = Evidence(
        id="ev1",
        source_id=SOURCE,
        version_hash=VERSION,
        locator=a_locator(),
        excerpt_hash=excerpt_digest(EXCERPT),
        captured_at=CAPTURED,
    )
    assert evidence.excerpt == ""


def test_excerpt_that_disagrees_with_the_hash_is_rejected():
    with pytest.raises(PayloadInvalid) as error:
        Evidence(
            id="ev1",
            source_id=SOURCE,
            version_hash=VERSION,
            locator=a_locator(),
            excerpt_hash=excerpt_digest("outro trecho"),
            captured_at=CAPTURED,
            excerpt=EXCERPT,
        )
    assert str(error.value) == "Evidence.excerpt não corresponde a excerpt_hash"


def test_evidence_dict_round_trip_preserves_the_excerpt():
    evidence = an_evidence()
    payload = evidence.to_dict()
    assert payload["excerpt"] == EXCERPT
    restored = Evidence.from_dict(payload, evidence.locator)
    assert restored == evidence


def test_evidence_from_dict_without_excerpt_yields_empty():
    payload = an_evidence().to_dict()
    del payload["excerpt"]
    restored = Evidence.from_dict(payload, a_locator())
    assert restored.excerpt == ""
    assert restored.excerpt_hash == excerpt_digest(EXCERPT)


def test_repository_round_trips_the_excerpt(repository):
    entity = a_table()
    evidence = an_evidence()
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(a_version())
        revision.put_entity(entity)
        revision.put_evidence(evidence, [entity.id])
    stored = repository.evidence_for(entity.id)
    assert [item.excerpt for item in stored] == [EXCERPT]
    assert repository.get_evidence(evidence.id).excerpt == EXCERPT


def test_repository_round_trips_the_excerpt_of_relation_evidence(repository):
    left = a_table(name="renewals")
    right = a_table(name="orders")
    relation = Relation.create("depends_on", left.id, right.id)
    evidence = an_evidence(path="src/link.py")
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(a_version())
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(relation)
        revision.put_evidence(evidence, [left.id], relation_ids=[relation.id])
    stored = repository.evidence_for_relation(relation.id)
    assert [item.excerpt for item in stored] == [EXCERPT]


def test_reingesting_evidence_updates_the_stored_excerpt(repository):
    entity = a_table()
    with repository.begin_revision("pipeline", "primeira") as revision:
        revision.put_source_version(a_version())
        revision.put_entity(entity)
        revision.put_evidence(an_evidence(), [entity.id])
    repository.conn.execute("UPDATE evidence SET excerpt=''")
    with repository.begin_revision("pipeline", "segunda") as revision:
        revision.put_evidence(an_evidence(), [entity.id])
    assert repository.evidence_for(entity.id)[0].excerpt == EXCERPT


def test_database_without_the_excerpt_column_opens_and_reads_empty(tmp_path):
    path = str(tmp_path / DATABASE_FILENAME)
    entity = a_table()
    evidence = an_evidence()
    with KnowledgeRepository.open(path) as repo:
        with repo.begin_revision("pipeline", "carga") as revision:
            revision.put_source_version(a_version())
            revision.put_entity(entity)
            revision.put_evidence(evidence, [entity.id])
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("ALTER TABLE evidence DROP COLUMN excerpt")
    assert "excerpt" not in {
        str(row[1]) for row in conn.execute("PRAGMA table_info(evidence)").fetchall()
    }
    conn.close()

    with KnowledgeRepository.open(path) as repo:
        assert repo.format_version == FORMAT_VERSION
        stored = repo.evidence_for(entity.id)
        assert [item.excerpt for item in stored] == [""]
        assert stored[0].excerpt_hash == evidence.excerpt_hash
        assert "excerpt" in {
            str(row[1])
            for row in repo.conn.execute("PRAGMA table_info(evidence)").fetchall()
        }


def test_gate_flags_supported_entity_evidence_without_excerpt(repository):
    entity = a_table()
    evidence = an_evidence()
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(a_version())
        revision.put_entity(entity)
        revision.put_evidence(evidence, [entity.id])
    assert check(repository) == ()
    repository.conn.execute("UPDATE evidence SET excerpt=''")
    found = check(repository)
    assert [item.rule for item in found] == [KnowledgeRule.EVIDENCE_WITHOUT_EXCERPT]
    assert found[0].target_kind == "evidence"
    assert found[0].target_id == evidence.id


def test_gate_flags_supported_relation_evidence_without_excerpt(repository):
    left = a_table(confidence=Confidence.INFERRED, name="renewals")
    right = a_table(confidence=Confidence.INFERRED, name="orders")
    relation = Relation.create("depends_on", left.id, right.id)
    evidence = an_evidence(path="src/link.py")
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(a_version())
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(relation)
        revision.put_evidence(evidence, [left.id], relation_ids=[relation.id])
    repository.conn.execute(
        "UPDATE relations SET confidence='supported' WHERE relation_id=?",
        (relation.id,),
    )
    repository.conn.execute("UPDATE evidence SET excerpt=''")
    found = check(repository)
    assert [item.rule for item in found] == [KnowledgeRule.EVIDENCE_WITHOUT_EXCERPT]
    assert found[0].target_id == evidence.id


def test_gate_accepts_inferred_entity_evidence_without_excerpt(repository):
    entity = a_table(confidence=Confidence.INFERRED)
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(a_version())
        revision.put_entity(entity)
        revision.put_evidence(an_evidence(), [entity.id])
    repository.conn.execute("UPDATE evidence SET excerpt=''")
    assert check(repository) == ()


def test_gate_reports_shared_evidence_once(repository):
    left = a_table(name="renewals")
    right = a_table(name="orders")
    evidence = an_evidence()
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(a_version())
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_evidence(evidence, [left.id, right.id])
    repository.conn.execute("UPDATE evidence SET excerpt=''")
    found = check(repository)
    assert [item.target_id for item in found] == [evidence.id]


def test_gate_ignores_orphan_evidence_without_excerpt(repository):
    entity = a_table(confidence=Confidence.INFERRED)
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(a_version())
        revision.put_entity(entity)
        revision.put_evidence(an_evidence(), [entity.id])
    repository.conn.execute("UPDATE evidence SET excerpt=''")
    repository.conn.execute("DELETE FROM evidence_links")
    assert check(repository) == ()
    assert repository.get_entity(EntityId(entity.id.value)) is not None
