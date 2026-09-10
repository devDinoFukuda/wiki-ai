from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    Entity,
    EntityId,
    InvalidKind,
    KnowledgeRepository,
    Relation,
    RelationCycle,
    UnknownReference,
)
from wiki_ai.knowledge.relations import (
    entity_exists,
    neighbor_ids,
    neighborhood,
    path_exists,
    validate_relation,
)
from wiki_ai.knowledge.repository import DATABASE_FILENAME


@pytest.fixture()
def graph(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    nodes = {
        name: Entity.create(kind="component", name=name) for name in ("A", "B", "C", "D")
    }
    with repo.begin_revision("pipeline", "grafo") as revision:
        for node in nodes.values():
            revision.put_entity(node)
        revision.put_relation(Relation.create("calls", nodes["A"].id, nodes["B"].id))
        revision.put_relation(Relation.create("calls", nodes["B"].id, nodes["C"].id))
        revision.put_relation(Relation.create("reads", nodes["A"].id, nodes["C"].id))
        revision.put_relation(Relation.create("belongs_to", nodes["D"].id, nodes["A"].id))
    yield repo, nodes
    repo.close()


def test_validate_relation_accepts_existing_endpoints(graph):
    repo, nodes = graph
    validate_relation(repo.conn, Relation.create("calls", nodes["C"].id, nodes["D"].id))


def test_validate_relation_rejects_unknown_source(graph):
    repo, nodes = graph
    relation = Relation.create("calls", EntityId("ent_fantasma"), nodes["B"].id)
    with pytest.raises(UnknownReference):
        validate_relation(repo.conn, relation)


def test_validate_relation_rejects_unknown_target(graph):
    repo, nodes = graph
    relation = Relation.create("calls", nodes["A"].id, EntityId("ent_fantasma"))
    with pytest.raises(UnknownReference):
        validate_relation(repo.conn, relation)


def test_relation_kind_must_be_snake_case(graph):
    repo, nodes = graph
    with pytest.raises(InvalidKind):
        Relation.create("Calls", nodes["A"].id, nodes["B"].id)


def test_entity_exists_reflects_storage(graph):
    repo, nodes = graph
    assert entity_exists(repo.conn, nodes["A"].id) is True
    assert entity_exists(repo.conn, EntityId("ent_fantasma")) is False


def test_neighborhood_out_only(graph):
    repo, nodes = graph
    kinds = sorted(r.kind for r in neighborhood(repo.conn, nodes["A"].id, "out"))
    assert kinds == ["calls", "reads"]


def test_neighborhood_in_only(graph):
    repo, nodes = graph
    found = neighborhood(repo.conn, nodes["A"].id, "in")
    assert [r.kind for r in found] == ["belongs_to"]


def test_neighborhood_both_directions(graph):
    repo, nodes = graph
    assert len(neighborhood(repo.conn, nodes["A"].id, "both")) == 3


def test_neighborhood_filters_by_kind(graph):
    repo, nodes = graph
    found = neighborhood(repo.conn, nodes["A"].id, "both", kinds=["calls"])
    assert [r.kind for r in found] == ["calls"]


def test_neighborhood_rejects_unknown_direction(graph):
    repo, nodes = graph
    with pytest.raises(UnknownReference):
        neighborhood(repo.conn, nodes["A"].id, "sideways")


def test_neighbor_ids_returns_the_other_end(graph):
    repo, nodes = graph
    assert neighbor_ids(repo.conn, nodes["A"].id, "out", ["calls"]) == (nodes["B"].id.value,)


def test_relations_of_delegates_to_neighborhood(graph):
    repo, nodes = graph
    assert len(repo.relations_of(nodes["A"].id)) == 3
    assert len(repo.relations_of(nodes["A"].id, "out", ["reads"])) == 1


def test_relation_attributes_survive_roundtrip(graph):
    repo, nodes = graph
    relation = Relation.create(
        "calls", nodes["C"].id, nodes["D"].id, attributes={"protocol": "http", "count": 3}
    )
    with repo.begin_revision("pipeline", "atributos") as revision:
        revision.put_relation(relation)
    stored = repo.relations_of(nodes["C"].id, "out", ["calls"])[0]
    assert stored == relation
    assert dict(stored.attributes) == {"protocol": "http", "count": 3}


def test_path_exists_follows_transitive_edges(graph):
    repo, nodes = graph
    assert path_exists(repo.conn, "calls", nodes["A"].id, nodes["C"].id) is True
    assert path_exists(repo.conn, "reads", nodes["A"].id, nodes["D"].id) is False


def test_self_referencing_supersedes_rejected(graph):
    repo, nodes = graph
    relation = Relation.create("supersedes", nodes["A"].id, nodes["A"].id)
    with pytest.raises(RelationCycle):
        validate_relation(repo.conn, relation)


def test_transitive_supersedes_cycle_rejected(graph):
    repo, nodes = graph
    with repo.begin_revision("pipeline", "cadeia") as revision:
        revision.put_relation(Relation.create("supersedes", nodes["A"].id, nodes["B"].id))
        revision.put_relation(Relation.create("supersedes", nodes["B"].id, nodes["C"].id))
    with pytest.raises(RelationCycle):
        validate_relation(
            repo.conn, Relation.create("supersedes", nodes["C"].id, nodes["A"].id)
        )


def test_cycle_allowed_for_ordinary_kinds(graph):
    repo, nodes = graph
    with repo.begin_revision("pipeline", "ciclo de chamada") as revision:
        revision.put_relation(Relation.create("calls", nodes["C"].id, nodes["A"].id))
    assert len(repo.relations_of(nodes["C"].id, "out", ["calls"])) == 1
