from __future__ import annotations

from typing import Any, Mapping, Sequence

from wiki_ai.repository.toolspec import ToolSpec

__all__ = [
    "TOOL_INVENTORY",
    "TOOL_SEARCH",
    "TOOL_READ",
    "TOOL_SYMBOL",
    "TOOL_REFERENCES",
    "TOOL_DEPENDENCIES",
    "TOOL_TESTS",
    "TOOL_CONFIG",
    "TOOL_HISTORY",
    "TOOL_EVIDENCE_CAPTURE",
    "TOOL_NAMES",
    "build_specs",
]

TOOL_INVENTORY = "repo.inventory"
TOOL_SEARCH = "repo.search"
TOOL_READ = "repo.read"
TOOL_SYMBOL = "repo.symbol"
TOOL_REFERENCES = "repo.references"
TOOL_DEPENDENCIES = "repo.dependencies"
TOOL_TESTS = "repo.tests"
TOOL_CONFIG = "repo.config"
TOOL_HISTORY = "repo.history"
TOOL_EVIDENCE_CAPTURE = "evidence.capture"

TOOL_NAMES: tuple[str, ...] = (
    TOOL_INVENTORY,
    TOOL_SEARCH,
    TOOL_READ,
    TOOL_SYMBOL,
    TOOL_REFERENCES,
    TOOL_DEPENDENCIES,
    TOOL_TESTS,
    TOOL_CONFIG,
    TOOL_HISTORY,
    TOOL_EVIDENCE_CAPTURE,
)

_STRING = {"type": "string"}
_SCOPE = {"type": "string", "enum": ["focus", "repository"]}
_BOOLEAN = {"type": "boolean"}
_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}


def _integer(minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _object(
    properties: Mapping[str, Any], required: Sequence[str] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _array_of(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": _object(properties)}


_ENTRY_PROPERTIES = {
    "path": _STRING,
    "size": {"type": "integer"},
    "language_hint": _STRING,
    "classification": _STRING,
    "generated": _BOOLEAN,
    "ignored": _BOOLEAN,
    "hash": _STRING,
}

_MATCH_PROPERTIES = {
    "path": _STRING,
    "line": {"type": "integer"},
    "column": {"type": "integer"},
    "excerpt": _STRING,
}

_SYMBOL_PROPERTIES = {
    "path": _STRING,
    "name": _STRING,
    "kind": _STRING,
    "line_start": {"type": "integer"},
    "line_end": {"type": ["integer", "null"]},
    "language_hint": _STRING,
    "confidence": _STRING,
}

_REFERENCE_PROPERTIES = {
    "path": _STRING,
    "line": {"type": "integer"},
    "column": {"type": "integer"},
    "excerpt": _STRING,
    "language_hint": _STRING,
    "is_test": _BOOLEAN,
    "is_definition": _BOOLEAN,
    "rank": {"type": "integer"},
}

_IMPORT_PROPERTIES = {
    "path": _STRING,
    "line": {"type": "integer"},
    "target": _STRING,
    "scope": _STRING,
    "language_hint": _STRING,
    "mechanism": _STRING,
}

_MANIFEST_PROPERTIES = {
    "path": _STRING,
    "line": {"type": "integer"},
    "manifest": _STRING,
    "name": _STRING,
    "version": {"type": ["string", "null"]},
}

_ENDPOINT_PROPERTIES = {
    "path": _STRING,
    "line": {"type": "integer"},
    "url": _STRING,
    "host": _STRING,
    "scheme": _STRING,
}

_MESSAGING_PROPERTIES = {
    "path": _STRING,
    "line": {"type": "integer"},
    "technology": _STRING,
    "excerpt": _STRING,
}

_TEST_PROPERTIES = {
    "path": _STRING,
    "reason": _STRING,
    "line": {"type": ["integer", "null"]},
    "subject": _STRING,
    "rank": {"type": "integer"},
}

_CONFIG_PROPERTIES = {
    "path": _STRING,
    "line": {"type": "integer"},
    "column": {"type": "integer"},
    "key": _STRING,
    "value": {"type": ["string", "null"]},
    "kind": _STRING,
    "mechanism": _STRING,
    "language_hint": _STRING,
    "excerpt": _STRING,
}

_COMMIT_PROPERTIES = {
    "sha": _STRING,
    "author": _STRING,
    "date": _STRING,
    "subject": _STRING,
    "paths": _STRING_ARRAY,
}

_BLAME_PROPERTIES = {
    "path": _STRING,
    "line": {"type": "integer"},
    "sha": _STRING,
    "author": _STRING,
    "date": _STRING,
}

_RENAME_PROPERTIES = {"sha": _STRING, "old_path": _STRING, "new_path": _STRING}


def build_specs(max_results_ceiling: int, max_bytes_ceiling: int) -> tuple[ToolSpec, ...]:
    results = _integer(1, max_results_ceiling)
    byte_budget = _integer(1, max_bytes_ceiling)
    return (
        ToolSpec(
            name=TOOL_INVENTORY,
            description="List snapshot files with size, language hint, classification, ignored and generated flags, and content hash. Restricted to the declared focus paths unless scope is repository.",
            input_schema=_object(
                {
                    "classification": _STRING,
                    "language_hint": _STRING,
                    "path_prefix": _STRING,
                    "include_ignored": _BOOLEAN,
                    "scope": _SCOPE,
                    "max_results": results,
                }
            ),
            output_schema=_object(
                {
                    "snapshot_id": _STRING,
                    "entries": _array_of(_ENTRY_PROPERTIES),
                    "total": {"type": "integer"},
                    "truncated": _BOOLEAN,
                    "by_classification": {"type": "object"},
                    "by_language": {"type": "object"},
                },
                ("snapshot_id", "entries", "total", "truncated"),
            ),
        ),
        ToolSpec(
            name=TOOL_SEARCH,
            description="Search snapshot content by literal text or controlled regular expression, filtered by glob or extension. Restricted to the declared focus paths unless scope is repository.",
            input_schema=_object(
                {
                    "pattern": _STRING,
                    "regex": _BOOLEAN,
                    "ignore_case": _BOOLEAN,
                    "globs": _STRING_ARRAY,
                    "extensions": _STRING_ARRAY,
                    "scope": _SCOPE,
                    "max_results": results,
                    "max_file_bytes": byte_budget,
                },
                ("pattern",),
            ),
            output_schema=_object(
                {
                    "snapshot_id": _STRING,
                    "matches": _array_of(_MATCH_PROPERTIES),
                    "paths": _STRING_ARRAY,
                    "truncated": _BOOLEAN,
                    "skipped": _STRING_ARRAY,
                },
                ("snapshot_id", "matches", "truncated"),
            ),
        ),
        ToolSpec(
            name=TOOL_READ,
            description="Read a line range of one snapshot file under an explicit byte budget.",
            input_schema=_object(
                {
                    "path": _STRING,
                    "start": {"type": "integer", "minimum": 1},
                    "end": {"type": "integer", "minimum": 1},
                    "max_bytes": byte_budget,
                },
                ("path",),
            ),
            output_schema=_object(
                {
                    "snapshot_id": _STRING,
                    "path": _STRING,
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                    "text": _STRING,
                    "truncated": _BOOLEAN,
                    "total_lines": {"type": "integer"},
                },
                ("snapshot_id", "path", "start", "end", "text", "total_lines"),
            ),
        ),
        ToolSpec(
            name=TOOL_SYMBOL,
            description="Return definition-like symbols found by language heuristics with a universal fallback, never failing on an unknown language. Restricted to the declared focus paths unless scope is repository.",
            input_schema=_object(
                {
                    "path": _STRING,
                    "name": _STRING,
                    "kinds": _STRING_ARRAY,
                    "scope": _SCOPE,
                    "max_results": results,
                }
            ),
            output_schema=_object(
                {
                    "snapshot_id": _STRING,
                    "symbols": _array_of(_SYMBOL_PROPERTIES),
                    "total": {"type": "integer"},
                },
                ("snapshot_id", "symbols", "total"),
            ),
        ),
        ToolSpec(
            name=TOOL_REFERENCES,
            description="Find textual usages of a symbol with word-boundary fallback, ranked by directory and language proximity, flagging test files.",
            input_schema=_object(
                {
                    "symbol_name": _STRING,
                    "exclude_definition": _BOOLEAN,
                    "origin_path": _STRING,
                    "limit": results,
                },
                ("symbol_name",),
            ),
            output_schema=_object(
                {
                    "snapshot_id": _STRING,
                    "references": _array_of(_REFERENCE_PROPERTIES),
                    "total": {"type": "integer"},
                },
                ("snapshot_id", "references", "total"),
            ),
        ),
        ToolSpec(
            name=TOOL_DEPENDENCIES,
            description="Report imports classified as internal or external through import_scope, manifest packages, literal http endpoints and messaging hints, each with path and line. Restricted to the declared focus paths unless scope is repository.",
            input_schema=_object(
                {
                    "path_prefix": _STRING,
                    "import_scope": _STRING,
                    "scope": _SCOPE,
                    "max_results": results,
                }
            ),
            output_schema=_object(
                {
                    "snapshot_id": _STRING,
                    "imports": _array_of(_IMPORT_PROPERTIES),
                    "manifests": _array_of(_MANIFEST_PROPERTIES),
                    "endpoints": _array_of(_ENDPOINT_PROPERTIES),
                    "messaging": _array_of(_MESSAGING_PROPERTIES),
                    "hosts": _STRING_ARRAY,
                },
                ("snapshot_id", "imports", "manifests", "endpoints", "messaging"),
            ),
        ),
        ToolSpec(
            name=TOOL_TESTS,
            description="Discover test files related to a path or symbol by mirrored naming and by references inside files classified as test. Restricted to the declared focus paths unless scope is repository.",
            input_schema=_object(
                {"path_or_symbol": _STRING, "scope": _SCOPE, "limit": results},
                ("path_or_symbol",),
            ),
            output_schema=_object(
                {
                    "snapshot_id": _STRING,
                    "tests": _array_of(_TEST_PROPERTIES),
                    "total": {"type": "integer"},
                },
                ("snapshot_id", "tests", "total"),
            ),
        ),
        ToolSpec(
            name=TOOL_CONFIG,
            description="Find configuration declarations in env, properties, yaml, json, toml, ini and xml files plus code usages that read them. Restricted to the declared focus paths unless scope is repository.",
            input_schema=_object(
                {"key_or_usage": _STRING, "scope": _SCOPE, "limit": results},
                ("key_or_usage",),
            ),
            output_schema=_object(
                {
                    "snapshot_id": _STRING,
                    "hits": _array_of(_CONFIG_PROPERTIES),
                    "total": {"type": "integer"},
                },
                ("snapshot_id", "hits", "total"),
            ),
        ),
        ToolSpec(
            name=TOOL_HISTORY,
            description="Read git commits, blame and renames for the snapshot or one path; reports availability instead of raising when git is absent.",
            input_schema=_object({"path": _STRING, "limit": results}),
            output_schema=_object(
                {
                    "snapshot_id": _STRING,
                    "available": _BOOLEAN,
                    "reason": {"type": ["string", "null"]},
                    "head": {"type": ["string", "null"]},
                    "path": {"type": ["string", "null"]},
                    "commits": _array_of(_COMMIT_PROPERTIES),
                    "blame": _array_of(_BLAME_PROPERTIES),
                    "renames": _array_of(_RENAME_PROPERTIES),
                },
                ("snapshot_id", "available"),
            ),
        ),
        ToolSpec(
            name=TOOL_EVIDENCE_CAPTURE,
            description="Turn an already performed read into a verifiable capture carrying snapshot id, file hash, line range, symbol and excerpt hash.",
            input_schema=_object(
                {
                    "path": _STRING,
                    "line_start": {"type": "integer", "minimum": 1},
                    "line_end": {"type": "integer", "minimum": 1},
                    "symbol": _STRING,
                    "max_bytes": byte_budget,
                },
                ("path", "line_start", "line_end"),
            ),
            output_schema=_object(
                {
                    "snapshot_id": _STRING,
                    "path": _STRING,
                    "file_sha256": _STRING,
                    "line_start": {"type": "integer"},
                    "line_end": {"type": "integer"},
                    "symbol": {"type": ["string", "null"]},
                    "excerpt_sha256": _STRING,
                    "excerpt": _STRING,
                    "locator": _STRING,
                },
                (
                    "snapshot_id",
                    "path",
                    "file_sha256",
                    "line_start",
                    "line_end",
                    "excerpt_sha256",
                    "excerpt",
                ),
            ),
        ),
    )
