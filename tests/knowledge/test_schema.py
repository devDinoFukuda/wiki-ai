from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    CodeLocator,
    Confidence,
    Entity,
    EpistemicStatus,
    PayloadInvalid,
    Relation,
    SourceVersion,
    make_evidence,
)
from wiki_ai.knowledge.schema import SCHEMAS, schema_for, validate

CAPTURED = "2026-01-01T00:00:00+00:00"


def entity_payload(**overrides):
    entity = Entity.create(
        kind="capability",
        name="Pedido",
        attributes={"owner": "squad-a"},
        epistemic=EpistemicStatus.DECLARED,
        confidence=Confidence.INFERRED,
    )
    payload = {
        "id": entity.id.value,
        "kind": entity.kind,
        "name": entity.name,
        "attributes": dict(entity.attributes),
        "epistemic": entity.epistemic.value,
        "confidence": entity.confidence.value,
        "source_versions": list(entity.source_versions),
    }
    payload.update(overrides)
    return payload


def relation_payload(**overrides):
    left = Entity.create(kind="module", name="A")
    right = Entity.create(kind="module", name="B")
    relation = Relation.create("calls", left.id, right.id)
    payload = {
        "id": relation.id,
        "kind": relation.kind,
        "source_id": relation.source_id.value,
        "target_id": relation.target_id.value,
        "attributes": {},
    }
    payload.update(overrides)
    return payload


def evidence_payload(**overrides):
    locator = CodeLocator(path="src/a.py", line_start=1, line_end=4, symbol="run")
    evidence = make_evidence("src_repo", "v1", locator, "codigo", CAPTURED)
    payload = {
        "id": evidence.id,
        "source_id": evidence.source_id,
        "version_hash": evidence.version_hash,
        "locator": evidence.locator.to_dict(),
        "excerpt_hash": evidence.excerpt_hash,
        "captured_at": evidence.captured_at,
    }
    payload.update(overrides)
    return payload


def source_version_payload(**overrides):
    version = SourceVersion("src_repo", "v1", "/repo", CAPTURED)
    payload = {
        "source_id": version.source_id,
        "version_hash": version.version_hash,
        "locator_root": version.locator_root,
        "captured_at": version.captured_at,
    }
    payload.update(overrides)
    return payload


def test_every_kernel_type_has_a_schema():
    assert set(SCHEMAS) == {"entity", "relation", "evidence", "source_version"}
    for name in SCHEMAS:
        assert schema_for(name)["type"] == "object"


def test_unknown_schema_name_rejected():
    with pytest.raises(PayloadInvalid):
        schema_for("fact")


def test_valid_entity_payload_accepted():
    assert validate("entity", entity_payload())["kind"] == "capability"


def test_entity_payload_missing_field_rejected():
    payload = entity_payload()
    del payload["confidence"]
    with pytest.raises(PayloadInvalid):
        validate("entity", payload)


def test_entity_payload_with_unknown_field_rejected():
    with pytest.raises(PayloadInvalid):
        validate("entity", entity_payload(nature="implemented"))


def test_entity_payload_with_kind_outside_snake_case_rejected():
    with pytest.raises(PayloadInvalid):
        validate("entity", entity_payload(kind="Capability"))


def test_entity_payload_mixing_the_two_axes_rejected():
    with pytest.raises(PayloadInvalid):
        validate("entity", entity_payload(epistemic="supported"))
    with pytest.raises(PayloadInvalid):
        validate("entity", entity_payload(confidence="implemented"))


def test_entity_payload_with_wrong_type_rejected():
    with pytest.raises(PayloadInvalid):
        validate("entity", entity_payload(attributes=["owner"]))
    with pytest.raises(PayloadInvalid):
        validate("entity", entity_payload(name=17))


def test_entity_payload_with_empty_name_rejected():
    with pytest.raises(PayloadInvalid):
        validate("entity", entity_payload(name="   "))


def test_entity_source_versions_items_validated():
    with pytest.raises(PayloadInvalid):
        validate("entity", entity_payload(source_versions=[""]))


def test_valid_relation_payload_accepted():
    assert validate("relation", relation_payload())["kind"] == "calls"


def test_relation_payload_missing_target_rejected():
    payload = relation_payload()
    del payload["target_id"]
    with pytest.raises(PayloadInvalid):
        validate("relation", payload)


def test_valid_evidence_payload_accepted():
    assert validate("evidence", evidence_payload())["source_id"] == "src_repo"


def test_evidence_payload_without_version_rejected():
    payload = evidence_payload()
    del payload["version_hash"]
    with pytest.raises(PayloadInvalid):
        validate("evidence", payload)


def test_evidence_payload_with_unknown_locator_kind_rejected():
    with pytest.raises(PayloadInvalid):
        validate("evidence", evidence_payload(locator={"kind": "telepathy"}))


def test_valid_source_version_payload_accepted():
    assert validate("source_version", source_version_payload())["version_hash"] == "v1"


def test_source_version_payload_without_locator_root_rejected():
    payload = source_version_payload()
    del payload["locator_root"]
    with pytest.raises(PayloadInvalid):
        validate("source_version", payload)


def test_boolean_is_not_accepted_where_string_is_required():
    with pytest.raises(PayloadInvalid):
        validate("source_version", source_version_payload(version_hash=True))
