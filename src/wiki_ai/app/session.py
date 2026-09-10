from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from wiki_ai.agent.session import AgentProvider
from wiki_ai.agent.registry import ProviderRegistry, ProviderUnavailable
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.repository.snapshot import RepositorySnapshot

__all__ = [
    "SessionError",
    "SnapshotNotStored",
    "FORMAT_VERSION",
    "STATE_DIR_NAME",
    "SUBDIRECTORIES",
    "FORMAT_FILE",
    "DATABASE_FILE",
    "LATEST_FILE",
    "HOME_VARIABLE",
    "OUTDATED_STORE_MARKERS",
    "detect_outdated_store",
    "repository_identity",
    "state_dir_for",
    "Session",
]

FORMAT_VERSION = 1
STATE_DIR_NAME = ".wiki-ai"
SUBDIRECTORIES = ("snapshots", "evidence", "publications")
FORMAT_FILE = "format.json"
DATABASE_FILE = "state.db"
LATEST_FILE = "latest.json"
HOME_VARIABLE = "WIKI_AI_HOME"
OUTDATED_STORE_MARKERS = (".codescan", "raw", "wiki", "wiki-docx", "agent-outputs")

_GIT_TIMEOUT_SECONDS = 30
_IDENTITY_LENGTH = 32


class SessionError(Exception):
    pass


class SnapshotNotStored(SessionError):
    pass


def detect_outdated_store(repo: Path) -> tuple[str, ...]:
    root = Path(repo)
    return tuple(name for name in OUTDATED_STORE_MARKERS if (root / name).is_dir())


def _git_value(root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_IDENTITY_LENGTH]


def repository_identity(repo: Path) -> str:
    root = Path(repo).resolve()
    remote = _git_value(root, "config", "--get", "remote.origin.url")
    if remote:
        return "repo_" + _digest(remote.rstrip("/").lower())
    toplevel = _git_value(root, "rev-parse", "--show-toplevel")
    if toplevel:
        return "repo_" + _digest(Path(toplevel).resolve().as_posix().lower())
    return "repo_" + _digest(root.as_posix().lower())


def state_dir_for(repo: Path, identity: str, home: str | None = None) -> Path:
    configured = os.environ.get(HOME_VARIABLE) if home is None else home
    if configured and configured.strip():
        return Path(configured).expanduser().resolve() / identity
    return Path(repo).resolve() / STATE_DIR_NAME


@dataclass(frozen=True)
class Session:
    repo: Path
    state_dir: Path
    identity: str

    @classmethod
    def open(cls, repo: Path, home: str | None = None) -> "Session":
        root = Path(repo)
        if not root.is_dir():
            raise SessionError(f"repository root is not a directory: {root}")
        resolved = root.resolve()
        identity = repository_identity(resolved)
        session = cls(
            repo=resolved,
            state_dir=state_dir_for(resolved, identity, home),
            identity=identity,
        )
        session.prepare()
        return session

    @property
    def namespace(self) -> str:
        return self.identity

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

    @property
    def latest_path(self) -> Path:
        return self.snapshots_dir / LATEST_FILE

    def prepare(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for name in SUBDIRECTORIES:
            (self.state_dir / name).mkdir(parents=True, exist_ok=True)
        if not self.format_path.is_file():
            self.write_format()

    def write_format(self) -> None:
        payload = {"format_version": FORMAT_VERSION, "repository_identity": self.identity}
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

    def _write_atomic(self, target: Path, text: str) -> None:
        temporary = target.with_name(target.name + ".partial")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)

    def save_snapshot(self, snapshot: RepositorySnapshot) -> Path:
        target = self.snapshot_path(snapshot.digest)
        self._write_atomic(target, snapshot.to_json() + "\n")
        self.set_current_snapshot(snapshot)
        return target

    def set_current_snapshot(self, snapshot: RepositorySnapshot) -> None:
        payload = {
            "digest": snapshot.digest,
            "taken_at": snapshot.taken_at,
            "git_head": snapshot.git_head,
        }
        self._write_atomic(
            self.latest_path, json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"
        )

    def current_snapshot_id(self) -> str | None:
        try:
            payload = json.loads(self.latest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        digest = payload.get("digest")
        return str(digest) if digest else None

    def current_snapshot(self) -> RepositorySnapshot | None:
        digest = self.current_snapshot_id()
        if digest is None:
            return None
        try:
            return self.load_snapshot(digest)
        except SnapshotNotStored:
            return None

    def load_snapshot(self, digest: str) -> RepositorySnapshot:
        target = self.snapshot_path(digest)
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise SnapshotNotStored(f"{target}: {exc}") from exc
        return RepositorySnapshot.from_json(raw)

    def stored_snapshots(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                path.stem
                for path in self.snapshots_dir.glob("*.json")
                if path.name != LATEST_FILE
            )
        )

    def open_knowledge(self) -> KnowledgeRepository:
        return KnowledgeRepository.open(str(self.database_path))

    def resolve_provider(
        self, registry: ProviderRegistry, preference: str | None = None
    ) -> AgentProvider | None:
        try:
            return registry.resolve(preference)
        except ProviderUnavailable:
            return None
