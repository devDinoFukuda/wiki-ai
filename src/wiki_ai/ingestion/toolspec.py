from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "DocumentToolError",
    "UnknownDocumentTool",
    "InvalidDocumentArguments",
    "DocumentToolSpec",
    "TOOL_OUTLINE",
    "TOOL_BLOCKS",
    "TOOL_READ",
    "TOOL_SEARCH",
    "TOOL_TABLE",
    "TOOL_GRAPH",
    "TOOL_HINTS",
    "TOOL_EVIDENCE_CAPTURE",
    "TOOL_NAMES",
    "MAX_BLOCKS_PER_PAGE",
    "MAX_SEARCH_MATCHES",
    "MAX_EXCERPT_CHARS",
    "build_specs",
    "validate_arguments",
]

TOOL_OUTLINE = "doc.outline"
TOOL_BLOCKS = "doc.blocks"
TOOL_READ = "doc.read"
TOOL_SEARCH = "doc.search"
TOOL_TABLE = "doc.table"
TOOL_GRAPH = "doc.graph"
TOOL_HINTS = "doc.hints"
TOOL_EVIDENCE_CAPTURE = "evidence.capture"

TOOL_NAMES: tuple[str, ...] = (
    TOOL_OUTLINE,
    TOOL_BLOCKS,
    TOOL_READ,
    TOOL_SEARCH,
    TOOL_TABLE,
    TOOL_GRAPH,
    TOOL_HINTS,
    TOOL_EVIDENCE_CAPTURE,
)

MAX_BLOCKS_PER_PAGE = 200
MAX_SEARCH_MATCHES = 200
MAX_EXCERPT_CHARS = 4000


class DocumentToolError(Exception):
    pass


class UnknownDocumentTool(DocumentToolError):
    pass


class InvalidDocumentArguments(DocumentToolError):
    pass


@dataclass(frozen=True)
class DocumentToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", dict(self.input_schema))
        object.__setattr__(self, "output_schema", dict(self.output_schema))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
        }


_TYPES: Mapping[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    checks = _TYPES.get(expected)
    if checks is None:
        return True
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, checks)


def _check(name: str, value: Any, schema: Mapping[str, Any]) -> None:
    declared = schema.get("type")
    expected: tuple[str, ...]
    if isinstance(declared, str):
        expected = (declared,)
    elif isinstance(declared, (list, tuple)):
        expected = tuple(str(item) for item in declared)
    else:
        expected = ()
    if expected and not any(_matches(value, item) for item in expected):
        raise InvalidDocumentArguments(
            f"argument {name} must be of type {'|'.join(expected)}"
        )
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and value not in enum:
        raise InvalidDocumentArguments(f"argument {name} must be one of {list(enum)}")
    minimum = schema.get("minimum")
    if minimum is not None and isinstance(value, (int, float)) and value < minimum:
        raise InvalidDocumentArguments(f"argument {name} must be >= {minimum}")
    maximum = schema.get("maximum")
    if maximum is not None and isinstance(value, (int, float)) and value > maximum:
        raise InvalidDocumentArguments(f"argument {name} must be <= {maximum}")
    items = schema.get("items")
    if "array" in expected and isinstance(value, (list, tuple)) and isinstance(items, Mapping):
        for index, item in enumerate(value):
            _check(f"{name}[{index}]", item, items)


def validate_arguments(
    spec: DocumentToolSpec, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise InvalidDocumentArguments("arguments must be a mapping")
    schema = spec.input_schema
    raw_properties = schema.get("properties")
    properties = raw_properties if isinstance(raw_properties, Mapping) else {}
    raw_required = schema.get("required")
    required = (
        tuple(str(item) for item in raw_required)
        if isinstance(raw_required, (list, tuple))
        else ()
    )
    for key in arguments:
        if key not in properties:
            raise InvalidDocumentArguments(f"unknown argument: {key}")
    for key in required:
        if key not in arguments or arguments[key] is None:
            raise InvalidDocumentArguments(f"missing required argument: {key}")
    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        property_schema = properties.get(key)
        if isinstance(property_schema, Mapping) and value is not None:
            _check(key, value, property_schema)
        cleaned[key] = value
    return cleaned


_STRING = {"type": "string"}
_INT = {"type": "integer", "minimum": 0}
_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}


def _object(properties: Mapping[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def build_specs() -> tuple[DocumentToolSpec, ...]:
    return (
        DocumentToolSpec(
            name=TOOL_OUTLINE,
            description=(
                "structure of the source: headings, worksheets, pages, speakers and "
                "diagram pages, each with the number of blocks it holds"
            ),
            input_schema=_object({}),
            output_schema=_object(
                {
                    "source_id": _STRING,
                    "kind": _STRING,
                    "version_hash": _STRING,
                    "entries": {"type": "array", "items": {"type": "object"}},
                    "block_kinds": {"type": "object"},
                    "gaps": {"type": "array", "items": {"type": "object"}},
                }
            ),
        ),
        DocumentToolSpec(
            name=TOOL_BLOCKS,
            description=(
                "paginated listing of blocks, filtered by block kind, section, "
                "worksheet, speaker or page"
            ),
            input_schema=_object(
                {
                    "kinds": _STRING_ARRAY,
                    "section": _STRING,
                    "worksheet": _STRING,
                    "speaker": _STRING,
                    "page": {"type": ["string", "integer"]},
                    "offset": _INT,
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_BLOCKS_PER_PAGE},
                }
            ),
            output_schema=_object(
                {
                    "blocks": {"type": "array", "items": {"type": "object"}},
                    "total": _INT,
                    "offset": _INT,
                    "truncated": {"type": "boolean"},
                }
            ),
        ),
        DocumentToolSpec(
            name=TOOL_READ,
            description=(
                "full text of one block by id, or of every block in an order range"
            ),
            input_schema=_object(
                {
                    "block_id": _STRING,
                    "order_start": _INT,
                    "order_end": _INT,
                }
            ),
            output_schema=_object(
                {
                    "blocks": {"type": "array", "items": {"type": "object"}},
                    "total": _INT,
                }
            ),
        ),
        DocumentToolSpec(
            name=TOOL_SEARCH,
            description="literal or regular-expression search over the text of the blocks",
            input_schema=_object(
                {
                    "pattern": _STRING,
                    "regex": {"type": "boolean"},
                    "ignore_case": {"type": "boolean"},
                    "kinds": _STRING_ARRAY,
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_MATCHES},
                },
                required=("pattern",),
            ),
            output_schema=_object(
                {
                    "matches": {"type": "array", "items": {"type": "object"}},
                    "total": _INT,
                    "truncated": {"type": "boolean"},
                }
            ),
        ),
        DocumentToolSpec(
            name=TOOL_TABLE,
            description=(
                "a table or a cell region rendered as rows by columns with its header, "
                "addressed by table block id or by worksheet and range"
            ),
            input_schema=_object(
                {
                    "block_id": _STRING,
                    "worksheet": _STRING,
                    "cell_range": _STRING,
                    "header_row": {"type": "boolean"},
                }
            ),
            output_schema=_object(
                {
                    "header": _STRING_ARRAY,
                    "rows": {"type": "array", "items": {"type": "array"}},
                    "block_ids": _STRING_ARRAY,
                    "locator": {"type": "object"},
                    "range": _STRING,
                }
            ),
        ),
        DocumentToolSpec(
            name=TOOL_GRAPH,
            description=(
                "diagram nodes and edges with their labels, optionally restricted to "
                "one page or to the neighbourhood of a node"
            ),
            input_schema=_object(
                {
                    "page": {"type": ["string", "integer"]},
                    "node": _STRING,
                    "depth": {"type": "integer", "minimum": 1, "maximum": 5},
                }
            ),
            output_schema=_object(
                {
                    "nodes": {"type": "array", "items": {"type": "object"}},
                    "edges": {"type": "array", "items": {"type": "object"}},
                    "pages": _STRING_ARRAY,
                }
            ),
        ),
        DocumentToolSpec(
            name=TOOL_HINTS,
            description=(
                "cheap non-authoritative candidates from the pre-classifier; a hint "
                "never raises confidence and never creates knowledge by itself"
            ),
            input_schema=_object(
                {
                    "candidate_kinds": _STRING_ARRAY,
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_MATCHES},
                }
            ),
            output_schema=_object(
                {
                    "hints": {"type": "array", "items": {"type": "object"}},
                    "total": _INT,
                    "authoritative": {"type": "boolean"},
                }
            ),
        ),
        DocumentToolSpec(
            name=TOOL_EVIDENCE_CAPTURE,
            description=(
                "capture one or more blocks as evidence with a typed locator for the "
                "kind of the source and the hash of the captured text"
            ),
            input_schema=_object(
                {"block_ids": _STRING_ARRAY},
                required=("block_ids",),
            ),
            output_schema=_object(
                {
                    "capture_id": _STRING,
                    "source_id": _STRING,
                    "version_hash": _STRING,
                    "locator": {"type": "object"},
                    "excerpt": _STRING,
                    "excerpt_hash": _STRING,
                }
            ),
        ),
    )
