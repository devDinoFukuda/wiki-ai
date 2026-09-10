from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from wiki_ai.repository import schemas
from wiki_ai.repository.config import find_config
from wiki_ai.repository.dependencies import DependencyScope, detect_dependencies
from wiki_ai.repository.evidence import capture as capture_evidence
from wiki_ai.repository.inventory import Classification, build_inventory
from wiki_ai.repository.reader import ReaderError, read_range
from wiki_ai.repository.references import ReferenceError, find_references
from wiki_ai.repository.search import SearchError, search
from wiki_ai.repository.snapshot import RepositorySnapshot, matches_any, normalize_path
from wiki_ai.repository.symbols import SymbolKind, find_symbols
from wiki_ai.repository.tests_discovery import related_tests
from wiki_ai.repository.history import history as read_history
from wiki_ai.repository.toolspec import (
    InvalidToolArguments,
    ToolError,
    ToolSpec,
    UnknownTool,
    validate_arguments,
)

__all__ = [
    "ToolError",
    "UnknownTool",
    "InvalidToolArguments",
    "ToolSpec",
    "ToolLimits",
    "ScopeFocus",
    "HarnessStats",
    "RepositoryHarness",
    "SCOPE_FOCUS",
    "SCOPE_REPOSITORY",
    "PATH_OUTSIDE_FOCUS",
]

SCOPE_FOCUS = "focus"
SCOPE_REPOSITORY = "repository"
PATH_OUTSIDE_FOCUS = "path_outside_focus"


@dataclass(frozen=True)
class ToolLimits:
    max_results: int = 200
    max_results_ceiling: int = 2000
    max_bytes: int = 262144
    max_bytes_ceiling: int = 4194304

    def results(self, requested: Any) -> int:
        if requested is None:
            return self.max_results
        return min(int(requested), self.max_results_ceiling)

    def budget(self, requested: Any) -> int:
        if requested is None:
            return self.max_bytes
        return min(int(requested), self.max_bytes_ceiling)


@dataclass(frozen=True)
class ScopeFocus:
    paths: tuple[str, ...] = ()
    allow_outside: bool = True

    def __post_init__(self) -> None:
        cleaned: list[str] = []
        for raw in self.paths:
            item = normalize_path(str(raw))
            if item and item not in cleaned:
                cleaned.append(item)
        object.__setattr__(self, "paths", tuple(cleaned))

    @property
    def is_empty(self) -> bool:
        return not self.paths

    def contains(self, path: str) -> bool:
        if self.is_empty:
            return True
        return matches_any(path, self.paths)

    def to_dict(self) -> dict[str, Any]:
        return {"paths": list(self.paths), "allow_outside": self.allow_outside}


@dataclass(frozen=True)
class HarnessStats:
    focus_paths: tuple[str, ...] = ()
    outside_focus_reads: int = 0
    files_read: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "focus_paths": list(self.focus_paths),
            "outside_focus_reads": self.outside_focus_reads,
            "files_read": list(self.files_read),
        }


class RepositoryHarness:
    def __init__(
        self,
        snapshot: RepositorySnapshot,
        limits: ToolLimits | None = None,
        focus: ScopeFocus | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._limits = limits or ToolLimits()
        self._focus = focus if focus is not None and not focus.is_empty else None
        self._outside_reads = 0
        self._files_read: list[str] = []
        self._specs = schemas.build_specs(
            self._limits.max_results_ceiling, self._limits.max_bytes_ceiling
        )
        self._by_name: Mapping[str, ToolSpec] = {spec.name: spec for spec in self._specs}
        self._handlers: Mapping[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            schemas.TOOL_INVENTORY: self._inventory,
            schemas.TOOL_SEARCH: self._search,
            schemas.TOOL_READ: self._read,
            schemas.TOOL_SYMBOL: self._symbol,
            schemas.TOOL_REFERENCES: self._references,
            schemas.TOOL_DEPENDENCIES: self._dependencies,
            schemas.TOOL_TESTS: self._tests,
            schemas.TOOL_CONFIG: self._config,
            schemas.TOOL_HISTORY: self._history,
            schemas.TOOL_EVIDENCE_CAPTURE: self._capture,
        }

    @property
    def snapshot(self) -> RepositorySnapshot:
        return self._snapshot

    @property
    def limits(self) -> ToolLimits:
        return self._limits

    @property
    def focus(self) -> ScopeFocus | None:
        return self._focus

    def stats(self) -> HarnessStats:
        return HarnessStats(
            focus_paths=self._focus.paths if self._focus is not None else (),
            outside_focus_reads=self._outside_reads,
            files_read=tuple(self._files_read),
        )

    def specs(self) -> tuple[ToolSpec, ...]:
        return self._specs

    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs)

    def spec_for(self, name: str) -> ToolSpec:
        spec = self._by_name.get(name)
        if spec is None:
            raise UnknownTool(f"unknown tool: {name}")
        return spec

    def invoke(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        spec = self.spec_for(name)
        cleaned = validate_arguments(spec, arguments or {})
        handler = self._handlers[name]
        try:
            return handler(cleaned)
        except (ReaderError, SearchError, ReferenceError) as exc:
            raise InvalidToolArguments(f"{name}: {exc}") from exc

    def _admitted_path(self, raw: Any) -> str:
        relative = normalize_path(str(raw))
        if not relative or relative not in self._snapshot.file_map():
            raise InvalidToolArguments(f"path is not part of the snapshot: {raw}")
        return relative

    def _followed_path(self, raw: Any) -> str:
        relative = self._admitted_path(raw)
        self._account(relative)
        return relative

    def _account(self, relative: str) -> None:
        if relative not in self._files_read:
            self._files_read.append(relative)
        if self._focus is None or self._focus.contains(relative):
            return
        if not self._focus.allow_outside:
            raise InvalidToolArguments(PATH_OUTSIDE_FOCUS)
        self._outside_reads += 1

    def _restricted(self, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        if self._focus is None:
            return ()
        if str(arguments.get("scope", SCOPE_FOCUS)) == SCOPE_REPOSITORY:
            return ()
        return self._focus.paths

    def _inside(self, paths: Sequence[str], path: str) -> bool:
        return not paths or matches_any(path, paths)

    def _inventory(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        inventory = build_inventory(self._snapshot)
        limit = self._limits.results(arguments.get("max_results"))
        wanted_classification = arguments.get("classification")
        wanted_language = arguments.get("language_hint")
        prefix = arguments.get("path_prefix")
        include_ignored = bool(arguments.get("include_ignored", False))
        if wanted_classification is not None:
            valid = {item.value for item in Classification}
            if wanted_classification not in valid:
                raise InvalidToolArguments(
                    f"classification must be one of {sorted(valid)}"
                )
        normalized_prefix = normalize_path(prefix) if prefix else None
        restricted = self._restricted(arguments)
        selected = []
        for entry in inventory.entries:
            if not self._inside(restricted, entry.path):
                continue
            if not include_ignored and (entry.ignored or entry.generated):
                continue
            if wanted_classification and entry.classification.value != wanted_classification:
                continue
            if wanted_language and entry.language_hint != wanted_language:
                continue
            if normalized_prefix and not (
                entry.path == normalized_prefix
                or entry.path.startswith(normalized_prefix + "/")
            ):
                continue
            selected.append(entry)
        return {
            "snapshot_id": self._snapshot.digest,
            "entries": [entry.to_dict() for entry in selected[:limit]],
            "total": len(selected),
            "truncated": len(selected) > limit,
            "by_classification": dict(inventory.by_classification()),
            "by_language": dict(inventory.by_language()),
        }

    def _search(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        restricted = self._restricted(arguments)
        requested_globs = tuple(str(item) for item in arguments.get("globs", ()))
        result = search(
            self._snapshot,
            str(arguments["pattern"]),
            regex=bool(arguments.get("regex", False)),
            ignore_case=bool(arguments.get("ignore_case", False)),
            globs=requested_globs or restricted,
            extensions=tuple(str(item) for item in arguments.get("extensions", ())),
            max_results=self._limits.results(arguments.get("max_results")),
            max_file_bytes=self._limits.budget(arguments.get("max_file_bytes")),
        )
        matches = tuple(
            match for match in result.matches if self._inside(restricted, match.path)
        )
        return {
            "snapshot_id": self._snapshot.digest,
            "matches": [
                {
                    "path": match.path,
                    "line": match.line,
                    "column": match.column,
                    "excerpt": match.excerpt,
                }
                for match in matches
            ],
            "paths": list(dict.fromkeys(match.path for match in matches)),
            "truncated": result.truncated,
            "skipped": list(result.skipped),
        }

    def _read(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        path = self._followed_path(arguments["path"])
        start = int(arguments.get("start", 1))
        end = arguments.get("end")
        text_range = read_range(
            self._snapshot,
            path,
            start,
            int(end) if end is not None else None,
            max_bytes=self._limits.budget(arguments.get("max_bytes")),
        )
        return {
            "snapshot_id": self._snapshot.digest,
            "path": text_range.path,
            "start": text_range.start,
            "end": text_range.end,
            "text": text_range.text,
            "truncated": text_range.truncated,
            "total_lines": text_range.total_lines,
        }

    def _symbol(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        raw_path = arguments.get("path")
        restricted = self._restricted(arguments)
        path = self._admitted_path(raw_path) if raw_path is not None else None
        if path is not None and not self._inside(restricted, path):
            raise InvalidToolArguments(PATH_OUTSIDE_FOCUS)
        raw_kinds = arguments.get("kinds", ())
        valid = {item.value: item for item in SymbolKind}
        kinds = []
        for item in raw_kinds:
            resolved = valid.get(str(item))
            if resolved is None:
                raise InvalidToolArguments(f"kinds must be one of {sorted(valid)}")
            kinds.append(resolved)
        found = tuple(
            item
            for item in find_symbols(
                self._snapshot,
                path,
                arguments.get("name"),
                kinds=tuple(kinds),
                max_results=self._limits.results(arguments.get("max_results")),
            )
            if self._inside(restricted, item.path)
        )
        return {
            "snapshot_id": self._snapshot.digest,
            "symbols": [item.to_dict() for item in found],
            "total": len(found),
        }

    def _references(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        origin = arguments.get("origin_path")
        origin_path = self._followed_path(origin) if origin is not None else None
        found = find_references(
            self._snapshot,
            str(arguments["symbol_name"]),
            exclude_definition=bool(arguments.get("exclude_definition", True)),
            origin_path=origin_path,
            limit=self._limits.results(arguments.get("limit")),
        )
        if self._focus is not None and not self._focus.allow_outside:
            found = tuple(item for item in found if self._focus.contains(item.path))
        for path in dict.fromkeys(item.path for item in found):
            self._account(path)
        return {
            "snapshot_id": self._snapshot.digest,
            "references": [item.to_dict() for item in found],
            "total": len(found),
        }

    def _dependencies(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        report = detect_dependencies(self._snapshot)
        limit = self._limits.results(arguments.get("max_results"))
        prefix = arguments.get("path_prefix")
        normalized_prefix = normalize_path(prefix) if prefix else None
        restricted = self._restricted(arguments)
        scope = arguments.get("import_scope")
        if scope is not None and scope not in {item.value for item in DependencyScope}:
            raise InvalidToolArguments(
                "import_scope must be one of "
                f"{sorted(item.value for item in DependencyScope)}"
            )

        def keep(path: str) -> bool:
            if not self._inside(restricted, path):
                return False
            if normalized_prefix is None:
                return True
            return path == normalized_prefix or path.startswith(normalized_prefix + "/")

        imports = [
            item
            for item in report.imports
            if keep(item.path) and (scope is None or item.scope.value == scope)
        ]
        return {
            "snapshot_id": self._snapshot.digest,
            "imports": [item.to_dict() for item in imports[:limit]],
            "manifests": [
                item.to_dict() for item in report.manifests if keep(item.path)
            ][:limit],
            "endpoints": [
                item.to_dict() for item in report.endpoints if keep(item.path)
            ][:limit],
            "messaging": [
                item.to_dict() for item in report.messaging if keep(item.path)
            ][:limit],
            "hosts": list(report.hosts()),
        }

    def _tests(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        subject = str(arguments["path_or_symbol"])
        if "/" in subject or subject.startswith("."):
            subject = self._admitted_path(subject)
        restricted = self._restricted(arguments)
        found = tuple(
            item
            for item in related_tests(
                self._snapshot,
                subject,
                limit=self._limits.results(arguments.get("limit")),
            )
            if self._inside(restricted, item.path)
        )
        return {
            "snapshot_id": self._snapshot.digest,
            "tests": [item.to_dict() for item in found],
            "total": len(found),
        }

    def _config(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        restricted = self._restricted(arguments)
        found = tuple(
            item
            for item in find_config(
                self._snapshot,
                str(arguments["key_or_usage"]),
                limit=self._limits.results(arguments.get("limit")),
            )
            if self._inside(restricted, item.path)
        )
        return {
            "snapshot_id": self._snapshot.digest,
            "hits": [item.to_dict() for item in found],
            "total": len(found),
        }

    def _history(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        raw_path = arguments.get("path")
        path = self._admitted_path(raw_path) if raw_path is not None else None
        report = read_history(
            self._snapshot,
            path,
            limit=self._limits.results(arguments.get("limit")),
        )
        payload = report.to_dict()
        payload["snapshot_id"] = self._snapshot.digest
        return payload

    def _capture(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        path = self._followed_path(arguments["path"])
        symbol = arguments.get("symbol")
        item = capture_evidence(
            self._snapshot,
            path,
            int(arguments["line_start"]),
            int(arguments["line_end"]),
            symbol=str(symbol) if symbol is not None else None,
            max_bytes=self._limits.budget(arguments.get("max_bytes")),
        )
        return item.to_dict()
