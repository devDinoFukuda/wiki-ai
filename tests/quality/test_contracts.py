from __future__ import annotations

import pytest

from wiki_ai.quality.contracts import (
    ContractError,
    ContractRule,
    SchemaInvalid,
    assert_valid,
    validate,
)

SCHEMA = {
    "type": "object",
    "required": ["name", "kind"],
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": ["source", "test"]},
        "size": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "nested": {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "number"}},
        },
    },
}


def test_valid_payload_has_no_violations() -> None:
    payload = {
        "name": "a.py",
        "kind": "source",
        "size": 12,
        "tags": ["x"],
        "nested": {"value": 1.5},
    }
    assert validate(SCHEMA, payload) == []


def test_missing_required_property_is_reported() -> None:
    violations = validate(SCHEMA, {"name": "a.py"})
    assert [v.rule for v in violations] == [ContractRule.REQUIRED]
    assert violations[0].pointer == "/kind"


def test_wrong_type_is_reported() -> None:
    violations = validate(SCHEMA, {"name": 1, "kind": "source"})
    assert violations[0].rule is ContractRule.TYPE
    assert violations[0].pointer == "/name"


def test_enum_mismatch_is_reported() -> None:
    violations = validate(SCHEMA, {"name": "a", "kind": "other"})
    assert violations[0].rule is ContractRule.ENUM


def test_additional_properties_are_rejected() -> None:
    violations = validate(SCHEMA, {"name": "a", "kind": "test", "extra": 1})
    assert violations[0].rule is ContractRule.ADDITIONAL_PROPERTIES
    assert violations[0].pointer == "/extra"


def test_item_violations_carry_index_pointer() -> None:
    violations = validate(SCHEMA, {"name": "a", "kind": "test", "tags": ["x", 2]})
    assert violations[0].pointer == "/tags/1"


def test_nested_object_is_validated() -> None:
    violations = validate(SCHEMA, {"name": "a", "kind": "test", "nested": {}})
    assert violations[0].pointer == "/nested/value"


def test_boolean_is_not_an_integer() -> None:
    violations = validate({"type": "integer"}, True)
    assert violations[0].rule is ContractRule.TYPE


def test_union_types_are_supported() -> None:
    schema = {"type": ["string", "null"]}
    assert validate(schema, None) == []
    assert validate(schema, "x") == []
    assert validate(schema, 1)


def test_unknown_type_name_is_a_schema_error() -> None:
    with pytest.raises(SchemaInvalid):
        validate({"type": "date"}, "x")


def test_assert_valid_raises_on_violation() -> None:
    with pytest.raises(ContractError):
        assert_valid(SCHEMA, {"name": "a"})
    assert_valid(SCHEMA, {"name": "a", "kind": "test"}) is None
