from __future__ import annotations

import pytest

from wiki_ai.knowledge import KnowledgeRepository, Relation
from wiki_ai.knowledge.repository import DATABASE_FILENAME

from .graph_fixture import (
    EDGES,
    catalog,
    code_evidence,
    code_version,
    doc_evidence,
    doc_version,
)


def apply_snapshot(repository, summary):
    nodes = catalog()
    code = code_version()
    docs = doc_version()
    with repository.begin_revision("pipeline", summary) as revision:
        revision.put_source_version(code)
        revision.put_source_version(docs)
        for node in nodes.values():
            revision.put_entity(node)
        for kind, source, target in EDGES:
            revision.put_relation(
                Relation.create(kind, nodes[source].id, nodes[target].id)
            )
        revision.put_evidence(
            code_evidence(code, "src/renewal.py", "renew"), [nodes["capability"].id]
        )
        revision.put_evidence(
            code_evidence(code, "src/rules.py", "eligible"), [nodes["rule"].id]
        )
        revision.put_evidence(
            doc_evidence(docs, "b-requirement"), [nodes["requirement"].id]
        )
    return nodes


def counts(repository):
    conn = repository.conn
    return {
        "entities": repository.entity_count(),
        "relations": repository.relation_count(),
        "source_versions": len(repository.source_versions()),
        "evidence": int(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]),
        "evidence_links": int(
            conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0]
        ),
        "entity_source_versions": int(
            conn.execute("SELECT COUNT(*) FROM entity_source_versions").fetchone()[0]
        ),
    }


@pytest.fixture()
def repository(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    yield repo
    repo.close()


def test_reapplying_the_same_snapshot_does_not_duplicate_anything(repository):
    apply_snapshot(repository, "primeira aplicacao")
    first = counts(repository)
    for index in range(3):
        apply_snapshot(repository, f"reaplicacao {index}")
    assert counts(repository) == first


def test_reapplication_creates_a_revision_per_run_but_no_new_knowledge(repository):
    apply_snapshot(repository, "primeira")
    before = counts(repository)
    apply_snapshot(repository, "segunda")
    assert repository.revision_count() == 2
    assert counts(repository) == before


def test_entity_identity_survives_reapplication(repository):
    nodes = apply_snapshot(repository, "primeira")
    apply_snapshot(repository, "segunda")
    for key, node in nodes.items():
        stored = repository.get_entity(node.id)
        assert stored is not None, key
        assert stored.id == node.id
        assert stored.kind == node.kind
        assert stored.name == node.name


def test_relation_identity_survives_reapplication(repository):
    nodes = apply_snapshot(repository, "primeira")
    ids_before = {relation.id for relation in repository.find_relations()}
    apply_snapshot(repository, "segunda")
    assert {relation.id for relation in repository.find_relations()} == ids_before
    assert len(ids_before) == len(EDGES)
    assert nodes


def test_evidence_identity_survives_reapplication(repository):
    apply_snapshot(repository, "primeira")
    before = repository.all_evidence_keys()
    apply_snapshot(repository, "segunda")
    assert repository.all_evidence_keys() == before


def test_evidence_links_are_not_duplicated(repository):
    nodes = apply_snapshot(repository, "primeira")
    apply_snapshot(repository, "segunda")
    apply_snapshot(repository, "terceira")
    assert len(repository.evidence_for(nodes["capability"].id)) == 1
    assert len(repository.evidence_for(nodes["rule"].id)) == 1


def test_source_versions_are_not_duplicated(repository):
    apply_snapshot(repository, "primeira")
    apply_snapshot(repository, "segunda")
    versions = repository.source_versions()
    assert len(versions) == 2
    assert len({version.key for version in versions}) == 2


def test_reapplication_across_reopen_does_not_duplicate(tmp_path):
    path = str(tmp_path / DATABASE_FILENAME)
    with KnowledgeRepository.open(path) as repo:
        apply_snapshot(repo, "primeira")
        before = counts(repo)
    with KnowledgeRepository.open(path) as reopened:
        apply_snapshot(reopened, "segunda")
        assert counts(reopened) == before
