from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from wiki_ai import __version__
from wiki_ai.app.session import detect_outdated_store
from wiki_ai.repository.inventory import Inventory, build_inventory
from wiki_ai.repository.snapshot import (
    RepositorySnapshot,
    SnapshotSpec,
    take_snapshot,
)

__all__ = [
    "ApiError",
    "RepositoryNotFound",
    "OutdatedStore",
    "InspectReport",
    "DEFAULT_EXCLUDES",
    "version",
    "inspect",
]

DEFAULT_EXCLUDES = (".wiki-ai",)


class ApiError(Exception):
    pass


class RepositoryNotFound(ApiError):
    pass


class OutdatedStore(ApiError):
    def __init__(self, markers: tuple[str, ...]) -> None:
        super().__init__(", ".join(markers))
        self.markers = markers


@dataclass(frozen=True)
class InspectReport:
    root: str
    git_head: str | None
    snapshot_digest: str
    total_files: int
    analyzable_files: int
    by_classification: tuple[tuple[str, int], ...]
    by_language: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "git_head": self.git_head,
            "snapshot_digest": self.snapshot_digest,
            "total_files": self.total_files,
            "analyzable_files": self.analyzable_files,
            "by_classification": dict(self.by_classification),
            "by_language": dict(self.by_language),
        }


def version() -> str:
    return __version__


def _pairs(counts: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(counts.items()))


def _report(snapshot: RepositorySnapshot, inventory: Inventory) -> InspectReport:
    return InspectReport(
        root=snapshot.root,
        git_head=snapshot.git_head,
        snapshot_digest=snapshot.digest,
        total_files=len(inventory.entries),
        analyzable_files=len(inventory.analyzable()),
        by_classification=_pairs(inventory.by_classification()),
        by_language=_pairs(inventory.by_language()),
    )


def inspect(repo: Path) -> InspectReport:
    root = Path(repo)
    if not root.is_dir():
        raise RepositoryNotFound(f"repository root is not a directory: {root}")
    markers = detect_outdated_store(root)
    if markers:
        raise OutdatedStore(markers)
    snapshot = take_snapshot(SnapshotSpec(root=root, excludes=DEFAULT_EXCLUDES))
    return _report(snapshot, build_inventory(snapshot))
