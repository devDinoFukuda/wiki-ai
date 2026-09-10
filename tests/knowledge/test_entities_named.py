from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    Confidence,
    Entity,
    KnowledgeRepository,
    SourceVersion,
    canonical_name,
    contextual_key,
)
from wiki_ai.knowledge.repository import DATABASE_FILENAME, NAMESPACE_ATTRIBUTE

CAPTURED = "2026-01-01T00:00:00+00:00"
NAMESPACE = "repository"


@pytest.fixture()
def repository(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    yield repo
    repo.close()


def a_source_version() -> SourceVersion:
    return SourceVersion(
        source_id="src_repo",
        version_hash="v1",
        locator_root="/repo",
        captured_at=CAPTURED,
    )


def scoped(
    name: str,
    kind: str = "capability",
    namespace: str = NAMESPACE,
    owner: str | None = None,
    owner_id=None,
) -> Entity:
    return Entity.create(
        kind=kind,
        name=name,
        stable_key=contextual_key(namespace, kind, owner, name),
        attributes={NAMESPACE_ATTRIBUTE: namespace},
        owner_id=owner_id,
    )


def test_canonical_name_folds_accents_case_and_separators():
    assert canonical_name("Emissão de Nota") == "emissao_de_nota"
    assert canonical_name("OrderRepository") == "orderrepository"
    assert canonical_name("  order-repository  ") == "order_repository"
    assert canonical_name("") == ""


def test_entities_named_finds_candidates_by_canonical_name(repository):
    entity = scoped("Emissão de Nota")
    with repository.begin_revision("pipeline", "seed") as revision:
        revision.put_source_version(a_source_version())
        revision.put_entity(entity)

    found = repository.entities_named(NAMESPACE, "emissao de nota")

    assert tuple(item.id for item in found) == (entity.id,)


def test_entities_named_returns_every_homonym(repository):
    first = scoped("Pedido", owner="vendas")
    second = scoped("Pedido", owner="compras")
    with repository.begin_revision("pipeline", "seed") as revision:
        revision.put_source_version(a_source_version())
        revision.put_entity(first)
        revision.put_entity(second)

    found = repository.entities_named(NAMESPACE, "Pedido")

    assert {item.id for item in found} == {first.id, second.id}


def test_entities_named_filters_by_kind(repository):
    capability = scoped("Pedido", kind="capability")
    flow = scoped("Pedido", kind="flow")
    with repository.begin_revision("pipeline", "seed") as revision:
        revision.put_source_version(a_source_version())
        revision.put_entity(capability)
        revision.put_entity(flow)

    found = repository.entities_named(NAMESPACE, "Pedido", kind="flow")

    assert tuple(item.id for item in found) == (flow.id,)


def test_entities_named_filters_by_owner(repository):
    owner = scoped("Vendas", kind="module")
    owned = scoped("Pedido", owner="vendas", owner_id=owner.id)
    orphan = scoped("Pedido", owner="compras")
    with repository.begin_revision("pipeline", "seed") as revision:
        revision.put_source_version(a_source_version())
        revision.put_entity(owner)
        revision.put_entity(owned)
        revision.put_entity(orphan)

    found = repository.entities_named(NAMESPACE, "Pedido", owner_id=owner.id)

    assert tuple(item.id for item in found) == (owned.id,)


def test_entities_named_ignores_other_namespaces(repository):
    here = scoped("Pedido", namespace=NAMESPACE)
    elsewhere = scoped("Pedido", namespace="transcripts")
    with repository.begin_revision("pipeline", "seed") as revision:
        revision.put_source_version(a_source_version())
        revision.put_entity(here)
        revision.put_entity(elsewhere)

    found = repository.entities_named(NAMESPACE, "Pedido")

    assert tuple(item.id for item in found) == (here.id,)


def test_entities_named_includes_entities_without_declared_namespace(repository):
    ambient = Entity.create(kind="capability", name="Pedido")
    with repository.begin_revision("pipeline", "seed") as revision:
        revision.put_source_version(a_source_version())
        revision.put_entity(ambient)

    assert tuple(
        item.id for item in repository.entities_named(NAMESPACE, "Pedido")
    ) == (ambient.id,)


def test_entities_named_is_empty_for_unknown_names(repository):
    assert repository.entities_named(NAMESPACE, "Inexistente") == ()


def test_entities_named_rejects_names_without_alphanumerics(repository):
    assert repository.entities_named(NAMESPACE, "   ") == ()


def test_entities_named_tracks_renames(repository):
    entity = scoped("Pedido")
    with repository.begin_revision("pipeline", "seed") as revision:
        revision.put_source_version(a_source_version())
        revision.put_entity(entity)
    renamed = Entity(
        id=entity.id,
        kind=entity.kind,
        name="Pedido",
        attributes={NAMESPACE_ATTRIBUTE: NAMESPACE},
        state=entity.state,
        confidence=Confidence.INFERRED,
    )
    with repository.begin_revision("pipeline", "update") as revision:
        revision.put_entity(renamed)

    found = repository.entities_named(NAMESPACE, "Pedido")

    assert len(found) == 1
    assert found[0].confidence is Confidence.INFERRED
