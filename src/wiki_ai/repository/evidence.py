from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from wiki_ai.repository.reader import (
    DEFAULT_MAX_BYTES,
    PathOutsideSnapshot,
    ReaderError,
    read_range,
)
from wiki_ai.repository.snapshot import RepositorySnapshot, normalize_path

__all__ = [
    "EvidenceError",
    "EvidenceCapture",
    "capture",
    "verify",
    "excerpt_digest",
]


class EvidenceError(Exception):
    pass


@dataclass(frozen=True)
class EvidenceCapture:
    snapshot_id: str
    path: str
    file_sha256: str
    line_start: int
    line_end: int
    symbol: str | None
    excerpt_sha256: str
    excerpt: str

    def locator(self) -> str:
        span = f"{self.line_start}-{self.line_end}"
        if self.symbol:
            return f"{self.path}:{span}#{self.symbol}"
        return f"{self.path}:{span}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "path": self.path,
            "file_sha256": self.file_sha256,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "symbol": self.symbol,
            "excerpt_sha256": self.excerpt_sha256,
            "excerpt": self.excerpt,
            "locator": self.locator(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceCapture":
        required = (
            "snapshot_id",
            "path",
            "file_sha256",
            "line_start",
            "line_end",
            "excerpt_sha256",
            "excerpt",
        )
        for key in required:
            if key not in payload:
                raise EvidenceError(f"missing key: {key}")
        symbol = payload.get("symbol")
        return cls(
            snapshot_id=str(payload["snapshot_id"]),
            path=str(payload["path"]),
            file_sha256=str(payload["file_sha256"]),
            line_start=int(payload["line_start"]),
            line_end=int(payload["line_end"]),
            symbol=None if symbol is None else str(symbol),
            excerpt_sha256=str(payload["excerpt_sha256"]),
            excerpt=str(payload["excerpt"]),
        )


def excerpt_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def capture(
    snapshot: RepositorySnapshot,
    path: str,
    line_start: int,
    line_end: int,
    *,
    symbol: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> EvidenceCapture:
    relative = normalize_path(path)
    record = snapshot.file_map().get(relative)
    if record is None:
        raise PathOutsideSnapshot(
            f"path is not part of snapshot {snapshot.digest[:12]}: {path}"
        )
    text_range = read_range(
        snapshot, relative, line_start, line_end, max_bytes=max_bytes
    )
    return EvidenceCapture(
        snapshot_id=snapshot.digest,
        path=relative,
        file_sha256=record.sha256,
        line_start=text_range.start,
        line_end=text_range.end,
        symbol=symbol,
        excerpt_sha256=excerpt_digest(text_range.text),
        excerpt=text_range.text,
    )


def verify(snapshot: RepositorySnapshot, item: EvidenceCapture) -> bool:
    if item.snapshot_id != snapshot.digest:
        return False
    record = snapshot.file_map().get(item.path)
    if record is None or record.sha256 != item.file_sha256:
        return False
    if excerpt_digest(item.excerpt) != item.excerpt_sha256:
        return False
    try:
        fresh = read_range(snapshot, item.path, item.line_start, item.line_end)
    except ReaderError:
        return False
    return excerpt_digest(fresh.text) == item.excerpt_sha256
