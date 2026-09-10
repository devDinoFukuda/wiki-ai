from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    Entity,
    GraphPolicy,
    KnowledgeQuery,
    KnowledgeRepository,
)
from wiki_ai.knowledge.gaps import open_gap
from wiki_ai.knowledge.model import Confidence, KnowledgeState, Relation
from wiki_ai.knowledge.repository import DATABASE_FILENAME, FORMAT_VERSION
from wiki_ai.knowledge.taxonomy import RelationKind

NAMESPACE = "acme"


@pytest.fixture()
def repository(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    yield repo
    repo.close()


def capability(name: str) -> Entity:
    return Entity.owned(
        NAMESPACE,
        "capability",
        name,
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.INFERRED,
    )


def test_format_version_of_a_fresh_repository_is_four(repository):
    assert FORMAT_VERSION == "4"
    assert repository.format_version == "4"


def test_provenance_of_an_empty_repository_reports_no_head(repository):
    report = KnowledgeQuery(repository).provenance()
    assert report.format_version == FORMAT_VERSION
    assert report.revision_count == 0
    assert report.head_revision_id == ""
    assert report.head_author == ""


def test_provenance_follows_the_head_revision(repository):
    with repository.begin_revision("pipeline", "primeira") as revision:
        revision.put_entity(capability("Renewal"))
    with repository.begin_revision("auditor", "segunda") as revision:
        revision.put_entity(capability("Billing"))
    report = KnowledgeQuery(repository).provenance()
    assert report.revision_count == 2
    assert report.head_author == "auditor"
    assert report.head_revision_id == repository.head_revision_id()
    assert report.head_created_at


def test_neighbor_ids_returns_the_other_end_of_each_relation(repository):
    left = capability("Renewal")
    right = capability("Billing")
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(
            Relation.create(RelationKind.DEPENDS_ON.value, left.id, right.id)
        )
    query = KnowledgeQuery(repository, GraphPolicy.ALL)
    assert query.neighbor_ids(left.id, RelationKind.DEPENDS_ON, "out") == (
        right.id.value,
    )
    assert query.neighbor_ids(right.id, RelationKind.DEPENDS_ON, "out") == ()
    assert query.neighbor_ids(right.id) == (left.id.value,)


def test_neighbor_ids_of_an_unresolved_relation_are_empty_by_default(repository):
    left = capability("Renewal")
    right = capability("Billing")
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_entity(left)
        revision.put_entity(right)
        revision.put_relation(
            Relation.create(RelationKind.DEPENDS_ON.value, left.id, right.id)
        )
    query = KnowledgeQuery(repository)

    assert query.policy is GraphPolicy.SUPPORTED_ONLY
    assert query.neighbor_ids(left.id, RelationKind.DEPENDS_ON, "out") == ()
    assert query.neighbors(left.id) == ()


def test_capability_profile_reports_the_gaps_that_affect_it(repository):
    renewal = capability("Renewal")
    with repository.begin_revision("pipeline", "carga") as revision:
        revision.put_entity(renewal)
        open_gap(revision, "qual o prazo de carencia?", about=renewal.id)
    profile = KnowledgeQuery(repository).capability_profile(renewal.id)
    assert profile is not None
    assert [gap.attributes["question"] for gap in profile.gaps] == [
        "qual o prazo de carencia?"
    ]
