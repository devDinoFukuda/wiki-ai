from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

__all__ = [
    "ContractError",
    "SchemaInvalid",
    "ContractRule",
    "ContractViolation",
    "validate",
    "assert_valid",
]


class ContractError(Exception):
    pass


class SchemaInvalid(ContractError):
    pass


class ContractRule(Enum):
    TYPE = "type"
    REQUIRED = "required"
    ENUM = "enum"
    ADDITIONAL_PROPERTIES = "additionalProperties"


@dataclass(frozen=True)
class ContractViolation:
    pointer: str
    rule: ContractRule
    detail: str


_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, Mapping),
    "array": lambda value: isinstance(value, (list, tuple)),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _expected_types(schema: Mapping[str, Any], pointer: str) -> tuple[str, ...]:
    declared = schema.get("type")
    if declared is None:
        return ()
    names = (declared,) if isinstance(declared, str) else tuple(declared)
    for name in names:
        if name not in _TYPE_CHECKS:
            raise SchemaInvalid(f"{pointer}: unknown type {name}")
    return names


def _child_pointer(pointer: str, key: str | int) -> str:
    return f"{pointer}/{key}" if pointer else f"/{key}"


def _check_type(
    schema: Mapping[str, Any], payload: Any, pointer: str
) -> list[ContractViolation]:
    names = _expected_types(schema, pointer)
    if not names:
        return []
    if any(_TYPE_CHECKS[name](payload) for name in names):
        return []
    return [
        ContractViolation(
            pointer or "/",
            ContractRule.TYPE,
            f"expected {'|'.join(names)}, got {type(payload).__name__}",
        )
    ]


def _check_enum(
    schema: Mapping[str, Any], payload: Any, pointer: str
) -> list[ContractViolation]:
    if "enum" not in schema:
        return []
    allowed = schema["enum"]
    if not isinstance(allowed, (list, tuple)):
        raise SchemaInvalid(f"{pointer}: enum must be a sequence")
    if payload in allowed:
        return []
    return [
        ContractViolation(
            pointer or "/",
            ContractRule.ENUM,
            f"value {payload!r} is not one of {list(allowed)!r}",
        )
    ]


def _check_object(
    schema: Mapping[str, Any], payload: Mapping[str, Any], pointer: str
) -> list[ContractViolation]:
    violations: list[ContractViolation] = []
    required = schema.get("required", ())
    if not isinstance(required, (list, tuple)):
        raise SchemaInvalid(f"{pointer}: required must be a sequence")
    for name in required:
        if name not in payload:
            violations.append(
                ContractViolation(
                    _child_pointer(pointer, str(name)),
                    ContractRule.REQUIRED,
                    "required property is missing",
                )
            )
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise SchemaInvalid(f"{pointer}: properties must be a mapping")
    for name, subschema in properties.items():
        if name in payload:
            violations.extend(
                _validate(subschema, payload[name], _child_pointer(pointer, str(name)))
            )
    if schema.get("additionalProperties") is False:
        for name in payload:
            if name not in properties:
                violations.append(
                    ContractViolation(
                        _child_pointer(pointer, str(name)),
                        ContractRule.ADDITIONAL_PROPERTIES,
                        "property is not declared by the schema",
                    )
                )
    return violations


def _check_array(
    schema: Mapping[str, Any], payload: Sequence[Any], pointer: str
) -> list[ContractViolation]:
    subschema = schema.get("items")
    if subschema is None:
        return []
    if not isinstance(subschema, Mapping):
        raise SchemaInvalid(f"{pointer}: items must be a mapping")
    violations: list[ContractViolation] = []
    for index, item in enumerate(payload):
        violations.extend(_validate(subschema, item, _child_pointer(pointer, index)))
    return violations


def _validate(schema: Any, payload: Any, pointer: str) -> list[ContractViolation]:
    if not isinstance(schema, Mapping):
        raise SchemaInvalid(f"{pointer or '/'}: schema must be a mapping")
    violations = _check_type(schema, payload, pointer)
    if violations:
        return violations
    violations.extend(_check_enum(schema, payload, pointer))
    if isinstance(payload, Mapping):
        violations.extend(_check_object(schema, payload, pointer))
    elif isinstance(payload, (list, tuple)) and not isinstance(payload, (str, bytes)):
        violations.extend(_check_array(schema, payload, pointer))
    return violations


def validate(schema: Mapping[str, Any], payload: Any) -> list[ContractViolation]:
    return _validate(schema, payload, "")


def assert_valid(schema: Mapping[str, Any], payload: Any) -> None:
    violations = validate(schema, payload)
    if violations:
        summary = "; ".join(f"{v.pointer} {v.rule.value}: {v.detail}" for v in violations)
        raise ContractError(summary)
