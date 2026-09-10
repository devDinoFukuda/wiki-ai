from __future__ import annotations

import dataclasses

import pytest

from wiki_ai.knowledge import (
    Confidence,
    Entity,
    EntityId,
    KnowledgeState,
    InvalidKind,
    PayloadInvalid,
    Relation,
    Revision,
    SourceVersion,
)
from wiki_ai.knowledge.model import validate_kind


def test_state_and_confidence_are_separate_axes():
    state = {status.value for status in KnowledgeState}
    confidence = {level.value for level in Confidence}
    assert state == {"implemented", "declared", "proposed", "historical"}
    assert confidence == {"supported", "inferred", "unresolved", "contradicted"}
    assert state.isdisjoint(confidence)


def test_entity_carries_both_axes_independently():
    entity = Entity.create(
        kind="capability",
        name="Emissão de boleto",
        state=KnowledgeState.PROPOSED,
        confidence=Confidence.INFERRED,
    )
    assert entity.state is KnowledgeState.PROPOSED
    assert entity.confidence is Confidence.INFERRED
    moved = entity.with_confidence(Confidence.UNRESOLVED)
    assert moved.state is KnowledgeState.PROPOSED
    assert moved.confidence is Confidence.UNRESOLVED
    assert entity.confidence is Confidence.INFERRED


@pytest.mark.parametrize(
    "kind", ["capability", "business_rule", "data_entity", "flow", "http_endpoint2"]
)
def test_snake_case_kind_accepted(kind):
    assert validate_kind(kind) == kind


@pytest.mark.parametrize(
    "kind", ["BusinessRule", "business rule", "business-rule", "_flow", "flow_", "", "2flow"]
)
def test_kind_outside_snake_case_rejected(kind):
    with pytest.raises(InvalidKind):
        validate_kind(kind)


def test_entity_kind_validated_on_construction():
    with pytest.raises(InvalidKind):
        Entity.create(kind="BusinessRule", name="Regra")


def test_relation_kind_validated_on_construction():
    left = EntityId.derive("module", "svc/a")
    right = EntityId.derive("module", "svc/b")
    with pytest.raises(InvalidKind):
        Relation.create(kind="Calls", source_id=left, target_id=right)


def test_entity_id_is_deterministic_and_kind_scoped():
    first = EntityId.derive("module", "svc/a")
    again = EntityId.derive("module", "svc/a")
    other_kind = EntityId.derive("capability", "svc/a")
    assert first == again
    assert first != other_kind
    assert str(first) == first.value


def test_entity_attributes_are_immutable():
    entity = Entity.create(kind="module", name="Serviço", attributes={"lang": "java"})
    assert entity.attributes["lang"] == "java"
    with pytest.raises(TypeError):
        entity.attributes["lang"] = "python"
    with pytest.raises(dataclasses.FrozenInstanceError):
        entity.name = "outro"


def test_entity_name_required():
    with pytest.raises(PayloadInvalid):
        Entity.create(kind="module", name="   ")


def test_relation_id_is_deterministic():
    left = EntityId.derive("module", "svc/a")
    right = EntityId.derive("module", "svc/b")
    first = Relation.create("calls", left, right)
    again = Relation.create("calls", left, right)
    reversed_relation = Relation.create("calls", right, left)
    assert first.id == again.id
    assert first.id != reversed_relation.id


def test_source_version_key_is_deterministic():
    version = SourceVersion("src_repo", "abc123", "/repo", "2026-01-01T00:00:00+00:00")
    same = SourceVersion("src_repo", "abc123", "/repo", "2026-02-02T00:00:00+00:00")
    other = SourceVersion("src_repo", "def456", "/repo", "2026-01-01T00:00:00+00:00")
    assert version.key == same.key
    assert version.key != other.key


def test_source_version_requires_hash():
    with pytest.raises(PayloadInvalid):
        SourceVersion("src_repo", "", "/repo", "2026-01-01T00:00:00+00:00")


def test_revision_requires_author():
    with pytest.raises(PayloadInvalid):
        Revision(id="rev_1", parent_id=None, author="", created_at="2026-01-01", summary="")
