from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from wiki_ai.repository.snapshot import RepositorySnapshot

__all__ = [
    "SessionError",
    "SnapshotNotStored",
    "FORMAT_VERSION",
    "STATE_DIR_NAME",
    "SUBDIRECTORIES",
    "FORMAT_FILE",
    "DATABASE_FILE",
    "OUTDATED_STORE_MARKERS",
    "detect_outdated_store",
    "Session",
]

FORMAT_VERSION = 1
STATE_DIR_NAME = ".wiki-ai"
SUBDIRECTORIES = ("snapshots", "evidence", "publications")
FORMAT_FILE = "format.json"
DATABASE_FILE = "state.db"
OUTDATED_STORE_MARKERS = (".codescan", "raw", "wiki", "wiki-docx", "agent-outputs")


class SessionError(Exception):
    pass


class SnapshotNotStored(SessionError):
    pass


def detect_outdated_store(repo: Path) -> tuple[str, ...]:
    root = Path(repo)
    return tuple(name for name in OUTDATED_STORE_MARKERS if (root / name).is_dir())


@dataclass(frozen=True)
class Session:
    repo: Path
    state_dir: Path

    @classmethod
    def open(cls, repo: Path) -> "Session":
        root = Path(repo)
        if not root.is_dir():
            raise SessionError(f"repository root is not a directory: {root}")
        session = cls(repo=root, state_dir=root / STATE_DIR_NAME)
        session.prepare()
        return session

    @property
    def snapshots_dir(self) -> Path:
        return self.state_dir / SUBDIRECTORIES[0]

    @property
    def evidence_dir(self) -> Path:
        return self.state_dir / SUBDIRECTORIES[1]

    @property
    def publications_dir(self) -> Path:
        return self.state_dir / SUBDIRECTORIES[2]

    @property
    def database_path(self) -> Path:
        return self.state_dir / DATABASE_FILE

    @property
    def format_path(self) -> Path:
        return self.state_dir / FORMAT_FILE

    def prepare(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for name in SUBDIRECTORIES:
            (self.state_dir / name).mkdir(parents=True, exist_ok=True)
        if not self.format_path.is_file():
            self.write_format()

    def write_format(self) -> None:
        payload = {"format_version": FORMAT_VERSION}
        self.format_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def read_format(self) -> Mapping[str, Any]:
        try:
            payload = json.loads(self.format_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"{self.format_path}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise SessionError(f"{self.format_path}: payload must be a mapping")
        return payload

    def format_version(self) -> int:
        return int(self.read_format().get("format_version", 0))

    def snapshot_path(self, digest: str) -> Path:
        return self.snapshots_dir / f"{digest}.json"

    def save_snapshot(self, snapshot: RepositorySnapshot) -> Path:
        target = self.snapshot_path(snapshot.digest)
        target.write_text(snapshot.to_json() + "\n", encoding="utf-8")
        return target

    def load_snapshot(self, digest: str) -> RepositorySnapshot:
        target = self.snapshot_path(digest)
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise SnapshotNotStored(f"{target}: {exc}") from exc
        return RepositorySnapshot.from_json(raw)

    def stored_snapshots(self) -> tuple[str, ...]:
        return tuple(sorted(path.stem for path in self.snapshots_dir.glob("*.json")))
