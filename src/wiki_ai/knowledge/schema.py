from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .errors import PayloadInvalid
from .evidence import LOCATOR_TYPES
from .model import KIND_PATTERN, Confidence, EpistemicStatus

TYPE_NAMES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "object": (dict,),
    "array": (list, tuple),
    "boolean": (bool,),
}

KIND_SCHEMA: dict[str, Any] = {"type": "string", "pattern": KIND_PATTERN.pattern}
IDENTIFIER_SCHEMA: dict[str, Any] = {"type": "string", "min_length": 1}

ENTITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "kind", "name", "epistemic", "confidence"],
    "additional_properties": False,
    "properties": {
        "id": IDENTIFIER_SCHEMA,
        "kind": KIND_SCHEMA,
        "name": {"type": "string", "min_length": 1},
        "attributes": {"type": "object"},
        "epistemic": {"type": "string", "enum": [s.value for s in EpistemicStatus]},
        "confidence": {"type": "string", "enum": [c.value for c in Confidence]},
        "source_versions": {"type": "array", "items": IDENTIFIER_SCHEMA},
    },
}

RELATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "kind", "source_id", "target_id"],
    "additional_properties": False,
    "properties": {
        "id": IDENTIFIER_SCHEMA,
        "kind": KIND_SCHEMA,
        "source_id": IDENTIFIER_SCHEMA,
        "target_id": IDENTIFIER_SCHEMA,
        "attributes": {"type": "object"},
    },
}

LOCATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["kind"],
    "additional_properties": True,
    "properties": {"kind": {"type": "string", "enum": sorted(LOCATOR_TYPES)}},
}

EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "source_id", "version_hash", "locator", "excerpt_hash", "captured_at"],
    "additional_properties": False,
    "properties": {
        "id": IDENTIFIER_SCHEMA,
        "source_id": IDENTIFIER_SCHEMA,
        "version_hash": IDENTIFIER_SCHEMA,
        "locator": LOCATOR_SCHEMA,
        "excerpt_hash": IDENTIFIER_SCHEMA,
        "captured_at": {"type": "string", "min_length": 1},
    },
}

SOURCE_VERSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["source_id", "version_hash", "locator_root", "captured_at"],
    "additional_properties": False,
    "properties": {
        "source_id": IDENTIFIER_SCHEMA,
        "version_hash": IDENTIFIER_SCHEMA,
        "locator_root": {"type": "string", "min_length": 1},
        "captured_at": {"type": "string", "min_length": 1},
    },
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "entity": ENTITY_SCHEMA,
    "relation": RELATION_SCHEMA,
    "evidence": EVIDENCE_SCHEMA,
    "source_version": SOURCE_VERSION_SCHEMA,
}


def schema_for(name: str) -> dict[str, Any]:
    schema = SCHEMAS.get(name)
    if schema is None:
        raise PayloadInvalid(
            f"schema {name!r} desconhecido; conhecidos: {', '.join(sorted(SCHEMAS))}"
        )
    return schema


def validate(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return validate_against(schema_for(name), payload, name)


def validate_against(
    schema: Mapping[str, Any], payload: Any, path: str
) -> dict[str, Any]:
    _check_value(schema, payload, path)
    return dict(payload)


def _check_value(schema: Mapping[str, Any], value: Any, path: str) -> None:
    _check_type(schema, value, path)
    if "enum" in schema and value not in schema["enum"]:
        raise PayloadInvalid(
            f"{path}: valor {value!r} fora do conjunto {sorted(schema['enum'])}"
        )
    if isinstance(value, str):
        _check_string(schema, value, path)
    if isinstance(value, dict):
        _check_object(schema, value, path)
    if isinstance(value, (list, tuple)) and "items" in schema:
        for index, item in enumerate(value):
            _check_value(schema["items"], item, f"{path}[{index}]")


def _check_type(schema: Mapping[str, Any], value: Any, path: str) -> None:
    expected = schema.get("type")
    if expected is None:
        return
    allowed = TYPE_NAMES.get(str(expected))
    if allowed is None:
        raise PayloadInvalid(f"{path}: tipo de schema desconhecido {expected!r}")
    if expected != "boolean" and isinstance(value, bool):
        raise PayloadInvalid(f"{path}: esperado {expected}, recebido boolean")
    if not isinstance(value, allowed):
        raise PayloadInvalid(
            f"{path}: esperado {expected}, recebido {type(value).__name__}"
        )


def _check_string(schema: Mapping[str, Any], value: str, path: str) -> None:
    minimum = int(schema.get("min_length", 0))
    if len(value.strip()) < minimum:
        raise PayloadInvalid(f"{path}: texto exige ao menos {minimum} caractere(s)")
    pattern = schema.get("pattern")
    if pattern is not None and not re.fullmatch(str(pattern), value):
        raise PayloadInvalid(f"{path}: {value!r} não casa com {pattern}")


def _check_object(schema: Mapping[str, Any], value: Mapping[str, Any], path: str) -> None:
    required: Sequence[str] = schema.get("required", ())
    missing = [name for name in required if name not in value]
    if missing:
        raise PayloadInvalid(f"{path}: campos obrigatórios ausentes: {', '.join(missing)}")
    properties: Mapping[str, Any] = schema.get("properties", {})
    if not schema.get("additional_properties", True):
        unknown = sorted(name for name in value if name not in properties)
        if unknown:
            raise PayloadInvalid(f"{path}: campos não previstos: {', '.join(unknown)}")
    for name, sub_schema in properties.items():
        if name in value:
            _check_value(sub_schema, value[name], f"{path}.{name}")
