from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from wiki_ai.repository.inventory import Classification, build_inventory, language_hint_for
from wiki_ai.repository.search import DEFAULT_MAX_FILE_BYTES, search
from wiki_ai.repository.snapshot import RepositorySnapshot, normalize_path
from wiki_ai.repository.symbols import SymbolInfo, find_symbols

__all__ = [
    "ReferenceError",
    "SymbolNameInvalid",
    "Reference",
    "DEFAULT_MAX_REFERENCES",
    "find_references",
]

DEFAULT_MAX_REFERENCES = 200

_NAME_PATTERN = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$\-.]*$")


class ReferenceError(Exception):
    pass


class SymbolNameInvalid(ReferenceError):
    pass


@dataclass(frozen=True)
class Reference:
    path: str
    line: int
    column: int
    excerpt: str
    language_hint: str
    is_test: bool
    is_definition: bool
    rank: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "excerpt": self.excerpt,
            "language_hint": self.language_hint,
            "is_test": self.is_test,
            "is_definition": self.is_definition,
            "rank": self.rank,
        }


def _directory_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _definition_lines(
    snapshot: RepositorySnapshot, symbol_name: str, max_results: int
) -> frozenset[tuple[str, int]]:
    definitions: Iterable[SymbolInfo] = find_symbols(
        snapshot, None, symbol_name, max_results=max_results
    )
    return frozenset((item.path, item.line_start) for item in definitions)


def _test_paths(snapshot: RepositorySnapshot) -> frozenset[str]:
    inventory = build_inventory(snapshot)
    return frozenset(
        entry.path
        for entry in inventory.entries
        if entry.classification is Classification.TEST
    )


def _rank_for(
    path: str,
    language: str,
    origin_directory: str | None,
    origin_language: str | None,
    is_test: bool,
) -> int:
    score = 0
    if origin_directory is not None and _directory_of(path) == origin_directory:
        score -= 4
    if origin_language is not None and language == origin_language:
        score -= 2
    if is_test:
        score += 1
    return score


def find_references(
    snapshot: RepositorySnapshot,
    symbol_name: str,
    *,
    exclude_definition: bool = True,
    origin_path: str | None = None,
    limit: int = DEFAULT_MAX_REFERENCES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> tuple[Reference, ...]:
    name = (symbol_name or "").strip()
    if not name or _NAME_PATTERN.match(name) is None:
        raise SymbolNameInvalid(f"symbol name is not a usable identifier: {symbol_name}")
    if limit <= 0:
        raise SymbolNameInvalid(f"limit must be positive: {limit}")
    pattern = r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])"
    result = search(
        snapshot,
        pattern,
        regex=True,
        max_results=max(limit * 4, limit),
        max_file_bytes=max_file_bytes,
    )
    definitions = _definition_lines(snapshot, name, max(limit * 4, limit))
    tests = _test_paths(snapshot)
    origin = normalize_path(origin_path) if origin_path else None
    origin_directory = _directory_of(origin) if origin else None
    origin_language = language_hint_for(origin) if origin else None
    if origin_directory is None and definitions:
        first = sorted(definitions)[0][0]
        origin_directory = _directory_of(first)
        origin_language = language_hint_for(first)
    collected: list[Reference] = []
    for match in result.matches:
        is_definition = (match.path, match.line) in definitions
        if exclude_definition and is_definition:
            continue
        language = language_hint_for(match.path)
        is_test = match.path in tests
        collected.append(
            Reference(
                path=match.path,
                line=match.line,
                column=match.column,
                excerpt=match.excerpt,
                language_hint=language,
                is_test=is_test,
                is_definition=is_definition,
                rank=_rank_for(
                    match.path, language, origin_directory, origin_language, is_test
                ),
            )
        )
    collected.sort(key=lambda item: (item.rank, item.path, item.line, item.column))
    return tuple(collected[:limit])
