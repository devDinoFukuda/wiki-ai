from __future__ import annotations

from dataclasses import dataclass

from wiki_ai.repository.snapshot import (
    BlobUnavailable,
    RepositorySnapshot,
    normalize_path,
)

__all__ = [
    "ReaderError",
    "PathOutsideSnapshot",
    "RangeInvalid",
    "FileUnreadable",
    "TextRange",
    "DEFAULT_MAX_BYTES",
    "BINARY_PROBE_BYTES",
    "snapshot_bytes",
    "snapshot_text",
    "read_range",
]

DEFAULT_MAX_BYTES = 262144
BINARY_PROBE_BYTES = 8192


class ReaderError(Exception):
    pass


class PathOutsideSnapshot(ReaderError):
    pass


class RangeInvalid(ReaderError):
    pass


class FileUnreadable(ReaderError):
    pass


@dataclass(frozen=True)
class TextRange:
    path: str
    start: int
    end: int
    text: str
    truncated: bool
    total_lines: int


def snapshot_bytes(snapshot: RepositorySnapshot, path: str) -> bytes:
    relative = _admitted_path(snapshot, path)
    try:
        return snapshot.read_bytes(relative)
    except BlobUnavailable as exc:
        raise FileUnreadable(f"{relative}: {exc}") from exc


def snapshot_text(
    snapshot: RepositorySnapshot, path: str, max_file_bytes: int
) -> str | None:
    try:
        raw = snapshot_bytes(snapshot, path)
    except (PathOutsideSnapshot, FileUnreadable):
        return None
    if len(raw) > max_file_bytes or b"\x00" in raw[:BINARY_PROBE_BYTES]:
        return None
    return raw.decode("utf-8", errors="replace")


def _admitted_path(snapshot: RepositorySnapshot, path: str) -> str:
    relative = normalize_path(path)
    if not relative:
        raise PathOutsideSnapshot("empty path is not part of the snapshot")
    if relative not in snapshot.file_map():
        raise PathOutsideSnapshot(
            f"path is not part of snapshot {snapshot.digest[:12]}: {relative}"
        )
    return relative


def read_range(
    snapshot: RepositorySnapshot,
    path: str,
    start: int = 1,
    end: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TextRange:
    relative = _admitted_path(snapshot, path)
    if max_bytes <= 0:
        raise RangeInvalid(f"max_bytes must be positive: {max_bytes}")
    raw = snapshot_bytes(snapshot, relative)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    total = len(lines)
    last = total if end is None else end
    if start < 1 or last < start:
        raise RangeInvalid(f"invalid line range for {relative}: {start}..{last}")
    if last > total:
        raise RangeInvalid(
            f"line range {start}..{last} exceeds {relative} ({total} lines)"
        )
    selected = "".join(lines[start - 1 : last])
    encoded = selected.encode("utf-8")
    truncated = len(encoded) > max_bytes
    if truncated:
        selected = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return TextRange(
        path=relative,
        start=start,
        end=last,
        text=selected,
        truncated=truncated,
        total_lines=total,
    )
