from __future__ import annotations

import hashlib
import json
import os
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "SnapshotError",
    "SnapshotSpecInvalid",
    "SnapshotPayloadInvalid",
    "SnapshotSpec",
    "FileRecord",
    "RepositorySnapshot",
    "normalize_path",
    "matches_any",
    "take_snapshot",
    "SnapshotDiff",
    "diff",
]


class SnapshotError(Exception):
    pass


class SnapshotSpecInvalid(SnapshotError):
    pass


class SnapshotPayloadInvalid(SnapshotError):
    pass


_GLOB_CHARS = ("*", "?", "[")
_SKIPPED_DIRECTORIES = frozenset({".git"})
_HASH_CHUNK = 65536
_GIT_TIMEOUT_SECONDS = 30


def normalize_path(raw: str) -> str:
    return unicodedata.normalize("NFC", (raw or "").replace("\\", "/")).strip("/")


def _is_glob(pattern: str) -> bool:
    return any(char in pattern for char in _GLOB_CHARS)


def matches_any(path: str, patterns: Sequence[str]) -> bool:
    for raw in patterns:
        pattern = normalize_path(raw)
        if not pattern:
            continue
        if _is_glob(pattern):
            if fnmatchcase(path, pattern):
                return True
            if pattern.endswith("/**") and (
                path == pattern[:-3] or path.startswith(pattern[:-3] + "/")
            ):
                return True
        elif path == pattern or path.startswith(pattern + "/"):
            return True
    return False


def _clean_patterns(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({normalize_path(v) for v in values if normalize_path(v)}))


@dataclass(frozen=True)
class SnapshotSpec:
    root: Path
    roots: tuple[str, ...] = ()
    includes: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "roots", _clean_patterns(self.roots))
        object.__setattr__(self, "includes", _clean_patterns(self.includes))
        object.__setattr__(self, "excludes", _clean_patterns(self.excludes))

    def admits(self, path: str) -> bool:
        if self.roots and not matches_any(path, self.roots):
            return False
        if self.includes and not matches_any(path, self.includes):
            return False
        if self.excludes and matches_any(path, self.excludes):
            return False
        return True


@dataclass(frozen=True)
class FileRecord:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RepositorySnapshot:
    root: str
    git_head: str | None
    files: tuple[FileRecord, ...]
    digest: str
    taken_at: str

    def file_map(self) -> Mapping[str, FileRecord]:
        return {record.path: record for record in self.files}

    def contains(self, path: str) -> bool:
        return normalize_path(path) in self.file_map()

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "git_head": self.git_head,
            "digest": self.digest,
            "taken_at": self.taken_at,
            "files": [
                {"path": record.path, "size": record.size, "sha256": record.sha256}
                for record in self.files
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RepositorySnapshot":
        for key in ("root", "digest", "taken_at", "files"):
            if key not in payload:
                raise SnapshotPayloadInvalid(f"missing key: {key}")
        raw_files = payload["files"]
        if not isinstance(raw_files, (list, tuple)):
            raise SnapshotPayloadInvalid("files must be a sequence")
        records: list[FileRecord] = []
        for item in raw_files:
            if not isinstance(item, Mapping):
                raise SnapshotPayloadInvalid("file entry must be a mapping")
            for key in ("path", "size", "sha256"):
                if key not in item:
                    raise SnapshotPayloadInvalid(f"file entry missing key: {key}")
            records.append(
                FileRecord(
                    path=str(item["path"]),
                    size=int(item["size"]),
                    sha256=str(item["sha256"]),
                )
            )
        return cls(
            root=str(payload["root"]),
            git_head=payload.get("git_head"),
            files=tuple(records),
            digest=str(payload["digest"]),
            taken_at=str(payload["taken_at"]),
        )

    @classmethod
    def from_json(cls, raw: str) -> "RepositorySnapshot":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SnapshotPayloadInvalid(str(exc)) from exc
        if not isinstance(payload, Mapping):
            raise SnapshotPayloadInvalid("payload must be a mapping")
        return cls.from_dict(payload)


def read_git_head(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
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
    head = completed.stdout.strip()
    return head or None


def _hash_file(full_path: Path) -> tuple[int, str] | None:
    digest = hashlib.sha256()
    size = 0
    try:
        with open(full_path, "rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return None
    return size, digest.hexdigest()


def compute_digest(records: Iterable[FileRecord]) -> str:
    pieces = sorted((record.path, record.sha256) for record in records)
    blob = json.dumps(pieces, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _walk(spec: SnapshotSpec) -> list[FileRecord]:
    root = spec.root
    records: list[FileRecord] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIPPED_DIRECTORIES)
        for filename in sorted(filenames):
            full_path = Path(dirpath) / filename
            relative = normalize_path(os.path.relpath(full_path, root))
            if not spec.admits(relative):
                continue
            hashed = _hash_file(full_path)
            if hashed is None:
                continue
            size, sha256 = hashed
            records.append(FileRecord(path=relative, size=size, sha256=sha256))
    records.sort(key=lambda record: record.path)
    return records


def take_snapshot(spec: SnapshotSpec) -> RepositorySnapshot:
    if not spec.root.is_dir():
        raise SnapshotSpecInvalid(f"root is not an existing directory: {spec.root}")
    root = spec.root.resolve()
    resolved = SnapshotSpec(
        root=root,
        roots=spec.roots,
        includes=spec.includes,
        excludes=spec.excludes,
    )
    records = _walk(resolved)
    return RepositorySnapshot(
        root=str(root),
        git_head=read_git_head(root),
        files=tuple(records),
        digest=compute_digest(records),
        taken_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


@dataclass(frozen=True)
class SnapshotDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": list(self.changed),
        }


def diff(before: RepositorySnapshot, after: RepositorySnapshot) -> SnapshotDiff:
    before_map = before.file_map()
    after_map = after.file_map()
    before_paths = set(before_map)
    after_paths = set(after_map)
    return SnapshotDiff(
        added=tuple(sorted(after_paths - before_paths)),
        removed=tuple(sorted(before_paths - after_paths)),
        changed=tuple(
            sorted(
                path
                for path in before_paths & after_paths
                if before_map[path].sha256 != after_map[path].sha256
            )
        ),
    )
