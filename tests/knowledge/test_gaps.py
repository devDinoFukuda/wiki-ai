from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    Entity,
    EntityId,
    GraphPolicy,
    KnowledgeQuery,
    KnowledgeRepository,
)
from wiki_ai.knowledge.errors import GapNotFound, MissingRequiredAttribute
from wiki_ai.knowledge.gaps import (
    GAP_KIND,
    blocking_gaps,
    close_gap,
    gaps_about,
    is_blocking,
    is_open,
    make_gap,
    open_gap,
    open_gaps,
    questions,
)
from wiki_ai.knowledge.repository import DATABASE_FILENAME
from wiki_ai.knowledge.taxonomy import RelationKind

from .graph_fixture import build


@pytest.fixture()
def graph(tmp_path):
    fixture = build(tmp_path)
    yield fixture
    fixture.repository.close()


@pytest.fixture()
def empty(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / "vazio" / DATABASE_FILENAME))
    yield repo
    repo.close()


def test_make_gap_produces_a_gap_entity():
    gap = make_gap("Qual a janela?", blocking=True)
    assert gap.kind == GAP_KIND
    assert gap.attributes["question"] == "Qual a janela?"
    assert gap.attributes["blocking"] is True
    assert gap.attributes["status"] == "open"


def test_open_gap_persists_and_links_to_the_subject(graph):
    repo = graph.repository
    with repo.begin_revision("pipeline", "duvida") as revision:
        gap_id = open_gap(
            revision, "Qual o SLA?", about=graph.id("integration"), blocking=True
        )
    stored = repo.get_entity(gap_id)
    assert stored is not None
    assert stored.kind == GAP_KIND
    linked = repo.relations_of(
        gap_id, "out", (RelationKind.AFFECTS.value,), policy=GraphPolicy.ALL
    )
    assert [relation.target_id for relation in linked] == [graph.id("integration")]


def test_open_gap_without_subject_creates_a_global_gap(empty):
    with empty.begin_revision("pipeline", "global") as revision:
        gap_id = open_gap(revision, "Quem aprova mudancas?")
    assert empty.relations_of(gap_id, "out") == []
    assert is_open(empty.get_entity(gap_id))


def test_open_gap_is_idempotent_for_the_same_question(graph):
    repo = graph.repository
    before = repo.entity_count()
    for _ in range(3):
        with repo.begin_revision("pipeline", "repetida") as revision:
            open_gap(revision, "Qual o SLA?", about=graph.id("integration"))
    assert repo.entity_count() == before + 1


def test_same_question_about_different_subjects_are_distinct_gaps(graph):
    repo = graph.repository
    with repo.begin_revision("pipeline", "duas") as revision:
        first = open_gap(revision, "Qual o SLA?", about=graph.id("integration"))
        second = open_gap(revision, "Qual o SLA?", about=graph.id("capability"))
    assert first != second


def test_close_gap_marks_resolution_and_clears_blocking(graph):
    repo = graph.repository
    with repo.begin_revision("pipeline", "abre") as revision:
        gap_id = open_gap(
            revision, "Qual o SLA?", about=graph.id("integration"), blocking=True
        )
    with repo.begin_revision("pipeline", "fecha") as revision:
        close_gap(revision, repo, gap_id, "SLA de 5 segundos confirmado")
    stored = repo.get_entity(gap_id)
    assert stored.attributes["status"] == "closed"
    assert stored.attributes["resolution"] == "SLA de 5 segundos confirmado"
    assert stored.attributes["blocking"] is False
    assert is_open(stored) is False


def test_close_gap_does_not_create_a_new_entity(graph):
    repo = graph.repository
    with repo.begin_revision("pipeline", "abre") as revision:
        gap_id = open_gap(revision, "Qual o SLA?", about=graph.id("integration"))
    before = repo.entity_count()
    with repo.begin_revision("pipeline", "fecha") as revision:
        close_gap(revision, repo, gap_id, "resolvido")
    assert repo.entity_count() == before


def test_close_unknown_gap_raises(graph):
    repo = graph.repository
    with pytest.raises(GapNotFound):
        with repo.begin_revision("pipeline", "fantasma") as revision:
            close_gap(revision, repo, EntityId("ent_fantasma"), "x")


def test_close_gap_on_entity_of_another_kind_raises(graph):
    repo = graph.repository
    with pytest.raises(GapNotFound):
        with repo.begin_revision("pipeline", "alvo errado") as revision:
            close_gap(revision, repo, graph.id("capability"), "x")


def test_open_gaps_excludes_closed_ones(graph):
    repo = graph.repository
    assert len(open_gaps(repo)) == 2
    with repo.begin_revision("pipeline", "fecha") as revision:
        close_gap(revision, repo, graph.id("gap_soft"), "resolvido")
    assert questions(open_gaps(repo)) == ("Qual a janela de renovacao?",)


def test_blocking_gaps_filters_by_the_blocking_flag(graph):
    assert questions(blocking_gaps(graph.repository)) == (
        "Qual a janela de renovacao?",
    )


def test_closed_gap_is_no_longer_blocking(graph):
    repo = graph.repository
    with repo.begin_revision("pipeline", "fecha") as revision:
        close_gap(revision, repo, graph.id("gap"), "janela e de 30 dias")
    assert blocking_gaps(repo) == ()
    assert is_blocking(repo.get_entity(graph.id("gap"))) is False


def test_gaps_about_returns_only_gaps(graph):
    found = gaps_about(graph.repository, graph.id("capability"))
    assert questions(found) == ("Qual a janela de renovacao?", "Quem e o dono?")


def test_gaps_about_entity_without_gaps_is_empty(graph):
    assert gaps_about(graph.repository, graph.id("table")) == ()


def test_gap_entity_without_question_is_rejected(empty):
    with pytest.raises(MissingRequiredAttribute):
        with empty.begin_revision("pipeline", "invalida") as revision:
            revision.put_entity(
                Entity.create(kind="gap", name="sem pergunta", attributes={"blocking": True})
            )


def test_query_gaps_agrees_with_gaps_module(graph):
    query = KnowledgeQuery(graph.repository)
    assert sorted(g.name for g in query.gaps(blocking_only=True)) == sorted(
        g.name for g in blocking_gaps(graph.repository)
    )


def test_query_gaps_excludes_closed_gaps(graph):
    repo = graph.repository
    query = KnowledgeQuery(repo)
    with repo.begin_revision("pipeline", "fecha") as revision:
        close_gap(revision, repo, graph.id("gap"), "resolvido")
    assert [g.name for g in query.gaps()] == ["Quem e o dono?"]
    assert query.gaps(blocking_only=True) == ()
