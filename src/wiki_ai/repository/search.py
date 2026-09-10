from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from wiki_ai.repository.reader import (
    BINARY_PROBE_BYTES,
    FileUnreadable,
    PathOutsideSnapshot,
    snapshot_bytes,
)
from wiki_ai.repository.snapshot import (
    RepositorySnapshot,
    matches_any,
    normalize_path,
)

__all__ = [
    "SearchError",
    "SearchPatternInvalid",
    "SearchQuery",
    "SearchMatch",
    "SearchResult",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_LINE_CHARS",
    "search",
]

DEFAULT_MAX_RESULTS = 200
DEFAULT_MAX_FILE_BYTES = 1048576
DEFAULT_MAX_LINE_CHARS = 400


class SearchError(Exception):
    pass


class SearchPatternInvalid(SearchError):
    pass


@dataclass(frozen=True)
class SearchQuery:
    pattern: str
    regex: bool = False
    ignore_case: bool = False
    globs: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    max_results: int = DEFAULT_MAX_RESULTS
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "globs", tuple(self.globs))
        object.__setattr__(
            self,
            "extensions",
            tuple(sorted({e.lower() if e.startswith(".") else "." + e.lower() for e in self.extensions})),
        )

    def compile(self) -> re.Pattern[str]:
        if not self.pattern:
            raise SearchPatternInvalid("pattern must not be empty")
        expression = self.pattern if self.regex else re.escape(self.pattern)
        flags = re.IGNORECASE if self.ignore_case else 0
        try:
            return re.compile(expression, flags)
        except re.error as exc:
            raise SearchPatternInvalid(f"{self.pattern}: {exc}") from exc


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line: int
    column: int
    excerpt: str


@dataclass(frozen=True)
class SearchResult:
    query: SearchQuery
    matches: tuple[SearchMatch, ...]
    truncated: bool
    skipped: tuple[str, ...]

    def paths(self) -> tuple[str, ...]:
        seen: list[str] = []
        for match in self.matches:
            if match.path not in seen:
                seen.append(match.path)
        return tuple(seen)


def _selected_paths(snapshot: RepositorySnapshot, query: SearchQuery) -> list[str]:
    selected: list[str] = []
    for record in snapshot.files:
        path = record.path
        if query.globs and not matches_any(path, query.globs):
            continue
        if query.extensions:
            name = path.rsplit("/", 1)[-1].lower()
            if not name.endswith(query.extensions):
                continue
        selected.append(path)
    return selected


def _excerpt(line: str) -> str:
    stripped = line.rstrip("\r\n")
    if len(stripped) > DEFAULT_MAX_LINE_CHARS:
        return stripped[:DEFAULT_MAX_LINE_CHARS]
    return stripped


def _scan_file(
    snapshot: RepositorySnapshot,
    path: str,
    expression: re.Pattern[str],
    query: SearchQuery,
    remaining: int,
) -> tuple[list[SearchMatch], str | None]:
    try:
        raw = snapshot_bytes(snapshot, path)
    except (PathOutsideSnapshot, FileUnreadable) as exc:
        return [], f"{path}: {exc}"
    if len(raw) > query.max_file_bytes:
        return [], f"{path}: exceeds max_file_bytes ({len(raw)})"
    if b"\x00" in raw[:BINARY_PROBE_BYTES]:
        return [], f"{path}: binary content"
    text = raw.decode("utf-8", errors="replace")
    found: list[SearchMatch] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in expression.finditer(line):
            found.append(
                SearchMatch(
                    path=path,
                    line=number,
                    column=match.start() + 1,
                    excerpt=_excerpt(line),
                )
            )
            if len(found) >= remaining:
                return found, None
    return found, None


def search(
    snapshot: RepositorySnapshot,
    pattern: str,
    *,
    regex: bool = False,
    ignore_case: bool = False,
    globs: Iterable[str] = (),
    extensions: Iterable[str] = (),
    max_results: int = DEFAULT_MAX_RESULTS,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> SearchResult:
    query = SearchQuery(
        pattern=pattern,
        regex=regex,
        ignore_case=ignore_case,
        globs=tuple(normalize_path(g) for g in globs),
        extensions=tuple(extensions),
        max_results=max_results,
        max_file_bytes=max_file_bytes,
    )
    if query.max_results <= 0:
        raise SearchPatternInvalid(f"max_results must be positive: {max_results}")
    expression = query.compile()
    matches: list[SearchMatch] = []
    skipped: list[str] = []
    truncated = False
    for path in _selected_paths(snapshot, query):
        remaining = query.max_results - len(matches)
        if remaining <= 0:
            truncated = True
            break
        found, skip_reason = _scan_file(snapshot, path, expression, query, remaining)
        if skip_reason is not None:
            skipped.append(skip_reason)
            continue
        matches.extend(found)
        if len(matches) >= query.max_results:
            truncated = True
            break
    return SearchResult(
        query=query,
        matches=tuple(matches[: query.max_results]),
        truncated=truncated,
        skipped=tuple(skipped),
    )
