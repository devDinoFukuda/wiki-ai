from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from wiki_ai.repository.inventory import Classification, build_inventory
from wiki_ai.repository.references import find_references
from wiki_ai.repository.references import SymbolNameInvalid
from wiki_ai.repository.snapshot import RepositorySnapshot, normalize_path

__all__ = [
    "MatchReason",
    "RelatedTest",
    "DEFAULT_MAX_TESTS",
    "related_tests",
]

DEFAULT_MAX_TESTS = 100


class MatchReason(Enum):
    MIRRORED_NAME = "mirrored_name"
    SAME_DIRECTORY = "same_directory"
    SYMBOL_REFERENCE = "symbol_reference"


@dataclass(frozen=True)
class RelatedTest:
    path: str
    reason: MatchReason
    line: int | None
    subject: str
    rank: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "reason": self.reason.value,
            "line": self.line,
            "subject": self.subject,
            "rank": self.rank,
        }


_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

_RANKS = {
    MatchReason.MIRRORED_NAME: 0,
    MatchReason.SYMBOL_REFERENCE: 1,
    MatchReason.SAME_DIRECTORY: 2,
}


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _directory(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _snake(value: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return spaced.replace("-", "_").lower()


def _mirrored_candidates(subject: str) -> frozenset[str]:
    base = _stem(subject) if "/" in subject or "." in subject else subject
    snake = _snake(base)
    lowered = base.lower()
    return frozenset(
        {
            f"test_{snake}",
            f"test{lowered}",
            f"{snake}_test",
            f"{lowered}test",
            f"{lowered}tests",
            f"{base}Test".lower(),
            f"{lowered}.spec",
            f"{lowered}.test",
            f"{snake}_spec",
            f"{lowered}spec",
        }
    )


def _looks_like_path(snapshot: RepositorySnapshot, value: str) -> bool:
    normalized = normalize_path(value)
    return bool(normalized) and normalized in snapshot.file_map()


def related_tests(
    snapshot: RepositorySnapshot,
    path_or_symbol: str,
    *,
    limit: int = DEFAULT_MAX_TESTS,
) -> tuple[RelatedTest, ...]:
    subject = (path_or_symbol or "").strip()
    if not subject or limit <= 0:
        return ()
    inventory = build_inventory(snapshot)
    test_paths = tuple(
        entry.path
        for entry in inventory.entries
        if entry.classification is Classification.TEST
    )
    is_path = _looks_like_path(snapshot, subject)
    normalized = normalize_path(subject) if is_path else subject
    candidates = _mirrored_candidates(normalized)
    source_directory = _directory(normalized) if is_path else None
    collected: dict[tuple[str, MatchReason], RelatedTest] = {}

    for candidate_path in test_paths:
        stem = _stem(candidate_path).lower()
        stripped = stem.rsplit(".", 1)[0] if "." in stem else stem
        if stem in candidates or stripped in candidates:
            key = (candidate_path, MatchReason.MIRRORED_NAME)
            collected[key] = RelatedTest(
                path=candidate_path,
                reason=MatchReason.MIRRORED_NAME,
                line=None,
                subject=normalized,
                rank=_RANKS[MatchReason.MIRRORED_NAME],
            )

    if is_path:
        base_name = normalized.rsplit("/", 1)[-1]
        for candidate_path in test_paths:
            if source_directory and _directory(candidate_path) == source_directory:
                key = (candidate_path, MatchReason.SAME_DIRECTORY)
                collected.setdefault(
                    key,
                    RelatedTest(
                        path=candidate_path,
                        reason=MatchReason.SAME_DIRECTORY,
                        line=None,
                        subject=normalized,
                        rank=_RANKS[MatchReason.SAME_DIRECTORY],
                    ),
                )
        symbol_like = _stem(base_name)
    else:
        symbol_like = normalized

    if _IDENTIFIER.match(symbol_like) is not None:
        try:
            references = find_references(
                snapshot,
                symbol_like,
                exclude_definition=True,
                origin_path=normalized if is_path else None,
                limit=max(limit * 4, limit),
            )
        except SymbolNameInvalid:
            references = ()
        for reference in references:
            if not reference.is_test:
                continue
            key = (reference.path, MatchReason.SYMBOL_REFERENCE)
            if key in collected:
                continue
            collected[key] = RelatedTest(
                path=reference.path,
                reason=MatchReason.SYMBOL_REFERENCE,
                line=reference.line,
                subject=symbol_like,
                rank=_RANKS[MatchReason.SYMBOL_REFERENCE],
            )

    ordered = sorted(
        collected.values(), key=lambda item: (item.rank, item.path, item.line or 0)
    )
    seen_paths: set[str] = set()
    unique: list[RelatedTest] = []
    for item in ordered:
        if item.path in seen_paths:
            continue
        seen_paths.add(item.path)
        unique.append(item)
    return tuple(unique[:limit])
