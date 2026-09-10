from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "ToolError",
    "UnknownTool",
    "InvalidToolArguments",
    "ToolSpec",
    "validate_arguments",
]


class ToolError(Exception):
    pass


class UnknownTool(ToolError):
    pass


class InvalidToolArguments(ToolError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
        }


_TYPE_CHECKS: Mapping[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    checks = _TYPE_CHECKS.get(expected)
    if checks is None:
        return True
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, checks)


def _expected_types(schema: Mapping[str, Any]) -> tuple[str, ...]:
    declared = schema.get("type")
    if declared is None:
        return ()
    if isinstance(declared, str):
        return (declared,)
    if isinstance(declared, (list, tuple)):
        return tuple(str(item) for item in declared)
    return ()


def _check_value(name: str, value: Any, schema: Mapping[str, Any]) -> None:
    expected = _expected_types(schema)
    if expected and not any(_type_matches(value, item) for item in expected):
        raise InvalidToolArguments(
            f"argument {name} must be of type {'|'.join(expected)}"
        )
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and value not in enum:
        raise InvalidToolArguments(f"argument {name} must be one of {list(enum)}")
    minimum = schema.get("minimum")
    if minimum is not None and isinstance(value, (int, float)) and value < minimum:
        raise InvalidToolArguments(f"argument {name} must be >= {minimum}")
    maximum = schema.get("maximum")
    if maximum is not None and isinstance(value, (int, float)) and value > maximum:
        raise InvalidToolArguments(f"argument {name} must be <= {maximum}")
    if "array" in expected and isinstance(value, (list, tuple)):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _check_value(f"{name}[{index}]", item, items)


def validate_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise InvalidToolArguments("arguments must be a mapping")
    schema = spec.input_schema
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    required = schema.get("required")
    required_names: Sequence[str] = (
        tuple(str(item) for item in required) if isinstance(required, (list, tuple)) else ()
    )
    additional = schema.get("additionalProperties", False)
    for key in arguments:
        if key not in properties and additional is False:
            raise InvalidToolArguments(f"unknown argument: {key}")
    for key in required_names:
        if key not in arguments or arguments[key] is None:
            raise InvalidToolArguments(f"missing required argument: {key}")
    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        property_schema = properties.get(key)
        if isinstance(property_schema, Mapping) and value is not None:
            _check_value(key, value, property_schema)
        cleaned[key] = value
    return cleaned
