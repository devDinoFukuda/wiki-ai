from __future__ import annotations

from pathlib import Path

from wiki_ai.repository.snapshot import RepositorySnapshot, SnapshotSpec, take_snapshot
from wiki_ai.repository.store import SnapshotStore

from tests.repository import fixtures_repos

__all__ = [
    "store_for",
    "materialize",
    "snapshot_of",
    "java_repo",
    "python_repo",
    "mainframe_repo",
    "mixed_repo",
    "write",
]

write = fixtures_repos.write


def store_for(root: Path) -> SnapshotStore:
    return SnapshotStore(Path(root).parent / f"{Path(root).name}-store")


def materialize(root: Path) -> RepositorySnapshot:
    return take_snapshot(SnapshotSpec(root=root), store_for(root))


def snapshot_of(root: Path) -> RepositorySnapshot:
    return materialize(root)


def java_repo(root: Path) -> RepositorySnapshot:
    fixtures_repos.java_repo(root)
    return materialize(root)


def python_repo(root: Path) -> RepositorySnapshot:
    fixtures_repos.python_repo(root)
    return materialize(root)


def mainframe_repo(root: Path) -> RepositorySnapshot:
    fixtures_repos.mainframe_repo(root)
    return materialize(root)


def mixed_repo(root: Path) -> RepositorySnapshot:
    fixtures_repos.mixed_repo(root)
    return materialize(root)
