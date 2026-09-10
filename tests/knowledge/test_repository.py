from __future__ import annotations

import sqlite3
import threading

import pytest

from wiki_ai.knowledge import (
    CodeContent,
    CodeLocator,
    Confidence,
    DocumentLocator,
    Entity,
    EntityId,
    KnowledgeState,
    FormatVersionMismatch,
    KnowledgeRepository,
    Relation,
    RelationCycle,
    RevisionClosed,
    SourceVersion,
    UnknownReference,
    UnsupportedEvidence,
    make_evidence,
)
from wiki_ai.knowledge.errors import (
    InvalidRelationPair,
    MissingRequiredAttribute,
    UnknownKind,
)
from wiki_ai.knowledge.repository import (
    DATABASE_FILENAME,
    FORMAT_VERSION,
    FORMAT_VERSION_KEY,
)

CAPTURED = "2026-01-01T00:00:00+00:00"


def database_path(tmp_path):
    return str(tmp_path / DATABASE_FILENAME)


@pytest.fixture()
def repository(tmp_path):
    repo = KnowledgeRepository.open(database_path(tmp_path))
    yield repo
    repo.close()


def a_source_version(version_hash="v1", source_id="src_repo"):
    return SourceVersion(
        source_id=source_id,
        version_hash=version_hash,
        locator_root="/repo",
        captured_at=CAPTURED,
    )


def an_entity(name="Pedido", kind="capability", **kwargs):
    return Entity.create(kind=kind, name=name, **kwargs)


def code_evidence(version, path="src/order.py", content=CodeContent.EXECUTABLE):
    locator = CodeLocator(path=path, line_start=1, line_end=20, content=content)
    return make_evidence(
        version.source_id, version.version_hash, locator, "codigo", CAPTURED
    )


def test_storage_kernel_initializes(tmp_path):
    path = database_path(tmp_path)
    with KnowledgeRepository.open(path) as repo:
        assert repo.format_version == FORMAT_VERSION
        assert repo.entity_count() == 0
        assert repo.relation_count() == 0
        assert repo.revision_count() == 0
        assert repo.head_revision_id() is None
        tables = {
            row[0]
            for row in repo.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "meta",
        "revisions",
        "source_versions",
        "entities",
        "entity_source_versions",
        "relations",
        "evidence",
        "evidence_links",
        "invalidations",
    } <= tables


def test_storage_kernel_reopens_existing_database(tmp_path):
    path = database_path(tmp_path)
    with KnowledgeRepository.open(path) as repo:
        with repo.begin_revision("pipeline", "primeira") as revision:
            revision.put_entity(an_entity())
    with KnowledgeRepository.open(path) as reopened:
        assert reopened.entity_count() == 1
        assert reopened.format_version == FORMAT_VERSION


def test_open_with_other_format_version_raises_typed_error(tmp_path):
    path = database_path(tmp_path)
    KnowledgeRepository.open(path).close()
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE meta SET value='99' WHERE key=?", (FORMAT_VERSION_KEY,)
    )
    conn.commit()
    conn.close()
    with pytest.raises(FormatVersionMismatch):
        KnowledgeRepository.open(path)


def test_revision_is_atomic(tmp_path):
    path = database_path(tmp_path)
    with KnowledgeRepository.open(path) as repo:
        entity = an_entity()
        with pytest.raises(RuntimeError):
            with repo.begin_revision("pipeline", "meia gravação") as revision:
                revision.put_source_version(a_source_version())
                revision.put_entity(entity)
                raise RuntimeError("falha no meio da revisão")
        assert repo.get_entity(entity.id) is None
        assert repo.entity_count() == 0
        assert repo.revision_count() == 0
        assert repo.source_versions() == []
    with KnowledgeRepository.open(path) as reopened:
        assert reopened.entity_count() == 0
        assert reopened.revision_count() == 0


def test_revision_commits_everything_together(repository):
    version = a_source_version()
    left = an_entity(name="Checkout", kind="module")
    right = an_entity(name="Cobrança", kind="module")
    evidence = code_evidence(version)
    with repository.begin_revision("pipeline", "carga inicial") as revision:
        revision.put_source_version(version)
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(Relation.create("calls", left.id, right.id))
        revision.put_evidence(evidence, [left.id])
        revision_id = revision.revision_id
    assert repository.entity_count() == 2
    assert repository.relation_count() == 1
    assert repository.evidence_for(left.id)[0].id == evidence.id
    assert repository.get_revision(revision_id).author == "pipeline"
    assert repository.head_revision_id() == revision_id


def test_revision_chains_to_parent(repository):
    with repository.begin_revision("pipeline", "um") as first:
        first.put_entity(an_entity(name="A", kind="module"))
        first_id = first.revision_id
    with repository.begin_revision("pipeline", "dois") as second:
        second.put_entity(an_entity(name="B", kind="module"))
        second_id = second.revision_id
    assert repository.get_revision(second_id).parent_id == first_id
    assert repository.get_revision(first_id).parent_id is None


def test_writing_after_revision_closed_raises(repository):
    with repository.begin_revision("pipeline", "fechada") as revision:
        revision.put_entity(an_entity())
    with pytest.raises(RevisionClosed):
        revision.put_entity(an_entity(name="Depois"))


def test_same_entity_written_twice_in_one_revision_does_not_duplicate(repository):
    entity = an_entity(name="Pedido", kind="capability")
    with repository.begin_revision("pipeline", "idempotente") as revision:
        revision.put_entity(entity)
        revision.put_entity(entity)
    assert repository.entity_count() == 1
    assert repository.get_entity(entity.id) == entity


def test_reingestion_of_same_entity_across_revisions_does_not_duplicate(repository):
    entity = an_entity(name="Pedido", kind="capability")
    for _ in range(3):
        with repository.begin_revision("pipeline", "reingestão") as revision:
            revision.put_entity(entity)
    assert repository.entity_count() == 1


def test_same_relation_written_twice_does_not_duplicate(repository):
    left = an_entity(name="A", kind="module")
    right = an_entity(name="B", kind="module")
    relation = Relation.create("calls", left.id, right.id)
    with repository.begin_revision("pipeline", "relação") as revision:
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(relation)
        revision.put_relation(relation)
    assert repository.relation_count() == 1


def test_same_evidence_written_twice_does_not_duplicate(repository):
    version = a_source_version()
    entity = an_entity(name="A", kind="module")
    evidence = code_evidence(version)
    with repository.begin_revision("pipeline", "evidência") as revision:
        revision.put_source_version(version)
        revision.put_entity(entity)
        revision.put_evidence(evidence, [entity.id])
        revision.put_evidence(evidence, [entity.id])
    assert len(repository.evidence_for(entity.id)) == 1


def test_entity_roundtrips_with_attributes_and_source_versions(repository):
    version = a_source_version()
    entity = an_entity(
        name="Pedido",
        kind="capability",
        attributes={"owner": "squad-a", "critical": True},
        state=KnowledgeState.DECLARED,
        confidence=Confidence.INFERRED,
        source_versions=(version.key,),
    )
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version)
        revision.put_entity(entity)
    stored = repository.get_entity(entity.id)
    assert stored == entity
    assert dict(stored.attributes) == {"owner": "squad-a", "critical": True}
    assert stored.source_versions == (version.key,)


def test_entity_update_overwrites_in_place(repository):
    version = a_source_version()
    entity = an_entity(name="Pedido", kind="capability", source_versions=(version.key,))
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_source_version(version)
        revision.put_entity(entity)
    with repository.begin_revision("pipeline", "revisão") as revision:
        revision.put_entity(entity.with_confidence(Confidence.SUPPORTED))
    stored = repository.get_entity(entity.id)
    assert repository.entity_count() == 1
    assert stored.confidence is Confidence.SUPPORTED
    assert stored.source_versions == (version.key,)


def test_find_entities_filters_by_kind(repository):
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_entity(an_entity(name="A", kind="module"))
        revision.put_entity(an_entity(name="B", kind="module"))
        revision.put_entity(an_entity(name="C", kind="business_rule", attributes={"statement": "s", "conditions": ["c"], "effects": ["e"]}))
    assert len(repository.find_entities()) == 3
    assert len(repository.find_entities(kind="module")) == 2
    assert [e.name for e in repository.find_entities(kind="business_rule")] == ["C"]


def test_get_entity_of_unknown_id_returns_none(repository):
    assert repository.get_entity(EntityId("ent_inexistente")) is None


def test_entity_citing_unknown_source_version_rejected(repository):
    with pytest.raises(UnknownReference):
        with repository.begin_revision("pipeline", "sem fonte") as revision:
            revision.put_entity(an_entity(source_versions=("srv_fantasma",)))
    assert repository.entity_count() == 0


def test_evidence_citing_unknown_source_version_rejected(repository):
    version = a_source_version()
    with pytest.raises(UnknownReference):
        with repository.begin_revision("pipeline", "sem fonte") as revision:
            revision.put_evidence(code_evidence(version))


def test_evidence_linked_to_unknown_entity_rejected(repository):
    version = a_source_version()
    with pytest.raises(UnknownReference):
        with repository.begin_revision("pipeline", "sem alvo") as revision:
            revision.put_source_version(version)
            revision.put_evidence(code_evidence(version), [EntityId("ent_fantasma")])


def test_evidence_for_returns_locator_intact(repository):
    version = a_source_version()
    entity = an_entity(name="A", kind="module")
    locator = CodeLocator(path="src/a.py", line_start=3, line_end=9, symbol="run")
    evidence = make_evidence(version.source_id, version.version_hash, locator, "x", CAPTURED)
    with repository.begin_revision("pipeline", "evidência") as revision:
        revision.put_source_version(version)
        revision.put_entity(entity)
        revision.put_evidence(evidence, [entity.id])
    stored = repository.evidence_for(entity.id)[0]
    assert stored == evidence
    assert stored.locator == locator


def test_implemented_and_supported_requires_executable_evidence(repository):
    version = a_source_version()
    entity = an_entity(
        name="Pedido",
        kind="capability",
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.SUPPORTED,
    )
    prose = make_evidence(
        version.source_id, version.version_hash, DocumentLocator(block_id="b1"), "texto", CAPTURED
    )
    with pytest.raises(UnsupportedEvidence):
        with repository.begin_revision("pipeline", "sem código") as revision:
            revision.put_source_version(version)
            revision.put_entity(entity)
            revision.put_evidence(prose, [entity.id])
    assert repository.entity_count() == 0


def test_implemented_and_supported_accepted_with_executable_evidence(repository):
    version = a_source_version()
    entity = an_entity(
        name="Pedido",
        kind="capability",
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.SUPPORTED,
    )
    with repository.begin_revision("pipeline", "com código") as revision:
        revision.put_source_version(version)
        revision.put_entity(entity)
        revision.put_evidence(code_evidence(version), [entity.id])
    assert repository.get_entity(entity.id).confidence is Confidence.SUPPORTED


def test_declared_entity_does_not_require_executable_evidence(repository):
    entity = an_entity(
        name="Requisito",
        kind="requirement",
        attributes={"statement": "requisito"},
        state=KnowledgeState.DECLARED,
        confidence=Confidence.INFERRED,
    )
    with repository.begin_revision("pipeline", "declarado") as revision:
        revision.put_entity(entity)
    assert repository.get_entity(entity.id).state is KnowledgeState.DECLARED


def test_relation_to_missing_entity_rejected(repository):
    left = an_entity(name="A", kind="module")
    ghost = EntityId("ent_fantasma")
    with pytest.raises(UnknownReference):
        with repository.begin_revision("pipeline", "alvo ausente") as revision:
            revision.put_entity(left)
            revision.put_relation(Relation.create("calls", left.id, ghost))
    assert repository.relation_count() == 0


def test_supersedes_cycle_rejected(repository):
    left = an_entity(name="A", kind="module")
    right = an_entity(name="B", kind="module")
    with repository.begin_revision("pipeline", "substituição") as revision:
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(Relation.create("supersedes", left.id, right.id))
    with pytest.raises(RelationCycle):
        with repository.begin_revision("pipeline", "ciclo") as revision:
            revision.put_relation(Relation.create("supersedes", right.id, left.id))
    assert repository.relation_count() == 1


def test_concurrent_open_of_same_database_is_safe(tmp_path):
    path = database_path(tmp_path)
    failures: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            repo = KnowledgeRepository.open(path)
            assert repo.format_version == FORMAT_VERSION
            repo.close()
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []


def test_entity_kind_outside_taxonomy_is_rejected(repository):
    with pytest.raises(UnknownKind):
        with repository.begin_revision("pipeline", "kind livre") as revision:
            revision.put_entity(Entity.create(kind="component", name="Servico"))
    assert repository.entity_count() == 0


def test_entity_missing_required_attribute_is_rejected(repository):
    with pytest.raises(MissingRequiredAttribute):
        with repository.begin_revision("pipeline", "sem atributos") as revision:
            revision.put_entity(Entity.create(kind="business_rule", name="Regra"))
    assert repository.entity_count() == 0


def test_entity_with_required_attributes_is_accepted(repository):
    entity = Entity.create(
        kind="business_rule",
        name="Regra",
        attributes={"statement": "s", "conditions": ["c"], "effects": ["e"]},
    )
    with repository.begin_revision("pipeline", "com atributos") as revision:
        revision.put_entity(entity)
    assert repository.get_entity(entity.id).name == "Regra"


def test_relation_kind_outside_taxonomy_is_rejected(repository):
    left = an_entity(name="A", kind="module")
    right = an_entity(name="B", kind="module")
    with pytest.raises(UnknownKind):
        with repository.begin_revision("pipeline", "relacao livre") as revision:
            revision.put_entity(left)
            revision.put_entity(right)
            revision.put_relation(Relation.create("mentions", left.id, right.id))
    assert repository.relation_count() == 0


def test_relation_with_invalid_pair_is_rejected(repository):
    capability = an_entity(name="Pedido", kind="capability")
    state = an_entity(name="Ativo", kind="state")
    with pytest.raises(InvalidRelationPair):
        with repository.begin_revision("pipeline", "par invalido") as revision:
            revision.put_entity(capability)
            revision.put_entity(state)
            revision.put_relation(
                Relation.create("transitions_to", capability.id, state.id)
            )
    assert repository.relation_count() == 0


def test_relation_confidence_roundtrips(repository):
    left = an_entity(name="A", kind="capability")
    right = an_entity(name="B", kind="capability")
    relation = Relation.create("calls", left.id, right.id)
    with repository.begin_revision("pipeline", "relacao") as revision:
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(relation)
    stored = repository.get_relation(relation.id)
    assert stored == relation
    assert stored.confidence is Confidence.UNRESOLVED


def test_supported_relation_without_evidence_rolls_back(repository):
    version = a_source_version()
    left = an_entity(name="A", kind="capability")
    right = an_entity(name="B", kind="capability")
    with pytest.raises(UnsupportedEvidence):
        with repository.begin_revision("pipeline", "sem lastro") as revision:
            revision.put_source_version(version)
            revision.put_entity(left)
            revision.put_entity(right)
            revision.put_relation(
                Relation.create(
                    "calls", left.id, right.id, confidence=Confidence.SUPPORTED
                )
            )
    assert repository.relation_count() == 0


def test_supported_relation_with_own_evidence_is_accepted(repository):
    version = a_source_version()
    left = an_entity(name="A", kind="capability")
    right = an_entity(name="B", kind="capability")
    relation = Relation.create(
        "calls", left.id, right.id, confidence=Confidence.SUPPORTED
    )
    with repository.begin_revision("pipeline", "com lastro") as revision:
        revision.put_source_version(version)
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(relation)
        revision.put_evidence(code_evidence(version), relation_ids=[relation.id])
    assert repository.get_relation(relation.id).confidence is Confidence.SUPPORTED
    assert len(repository.evidence_for_relation(relation.id)) == 1


def test_supported_endpoints_do_not_support_the_relation_between_them(repository):
    version = a_source_version()
    left = an_entity(
        name="A",
        kind="capability",
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.SUPPORTED,
    )
    right = an_entity(
        name="B",
        kind="capability",
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.SUPPORTED,
    )
    relation = Relation.create(
        "calls", left.id, right.id, confidence=Confidence.SUPPORTED
    )
    with pytest.raises(UnsupportedEvidence):
        with repository.begin_revision("pipeline", "extremos supported") as revision:
            revision.put_source_version(version)
            revision.put_entity(left)
            revision.put_entity(right)
            revision.put_evidence(code_evidence(version, "src/a.py"), [left.id])
            revision.put_evidence(code_evidence(version, "src/b.py"), [right.id])
            revision.put_relation(relation)
    assert repository.relation_count() == 0


def test_supported_relation_is_accepted_with_its_own_evidence(repository):
    version = a_source_version()
    left = an_entity(
        name="A",
        kind="capability",
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.SUPPORTED,
    )
    right = an_entity(
        name="B",
        kind="capability",
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.SUPPORTED,
    )
    relation = Relation.create(
        "calls", left.id, right.id, confidence=Confidence.SUPPORTED
    )
    with repository.begin_revision("pipeline", "aresta com lastro") as revision:
        revision.put_source_version(version)
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_evidence(code_evidence(version, "src/a.py"), [left.id])
        revision.put_evidence(code_evidence(version, "src/b.py"), [right.id])
        revision.put_relation(relation)
        revision.put_evidence(
            code_evidence(version, "src/edge.py"), relation_ids=(relation.id,)
        )
    assert repository.get_relation(relation.id).confidence is Confidence.SUPPORTED


def test_evidence_linked_to_unknown_relation_rejected(repository):
    version = a_source_version()
    with pytest.raises(UnknownReference):
        with repository.begin_revision("pipeline", "relacao ausente") as revision:
            revision.put_source_version(version)
            revision.put_evidence(code_evidence(version), relation_ids=["rel_fantasma"])


def test_find_relations_filters_by_kind(repository):
    left = an_entity(name="A", kind="capability")
    right = an_entity(name="B", kind="capability")
    with repository.begin_revision("pipeline", "relacoes") as revision:
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(Relation.create("calls", left.id, right.id))
        revision.put_relation(Relation.create("depends_on", left.id, right.id))
    assert len(repository.find_relations()) == 2
    assert [r.kind for r in repository.find_relations("calls")] == ["calls"]


def test_get_relation_of_unknown_id_returns_none(repository):
    assert repository.get_relation("rel_fantasma") is None


def test_all_evidence_keys_lists_stored_evidence(repository):
    version = a_source_version()
    entity = an_entity(name="A", kind="capability")
    evidence = code_evidence(version)
    with repository.begin_revision("pipeline", "evidencia") as revision:
        revision.put_source_version(version)
        revision.put_entity(entity)
        revision.put_evidence(evidence, [entity.id])
    assert repository.all_evidence_keys() == ((evidence.id, version.key),)
