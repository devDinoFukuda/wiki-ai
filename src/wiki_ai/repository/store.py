from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from wiki_ai.repository.snapshot import (
    FileRecord,
    RepositorySnapshot,
    SnapshotError,
    compute_digest,
)

__all__ = [
    "StoreError",
    "BlobMissing",
    "ManifestMissing",
    "ManifestInvalid",
    "BLOBS_DIRECTORY",
    "MANIFEST_NAME",
    "SnapshotStore",
]

BLOBS_DIRECTORY = "blobs"
MANIFEST_NAME = "manifest.json"

_HASH_CHUNK = 65536


class StoreError(SnapshotError):
    pass


class BlobMissing(StoreError):
    pass


class ManifestMissing(StoreError):
    pass


class ManifestInvalid(StoreError):
    pass


def _digest_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _blob_path(self, sha256: str) -> Path:
        return self._root / BLOBS_DIRECTORY / sha256[:2] / sha256

    def _manifest_path(self, digest: str) -> Path:
        return self._root / digest / MANIFEST_NAME

    def has(self, sha256: str) -> bool:
        return self._blob_path(sha256).is_file()

    def put(self, payload: bytes) -> str:
        sha256 = _digest_of(payload)
        target = self._blob_path(sha256)
        if target.is_file():
            return sha256
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_name(f"{sha256}.{os.getpid()}.pending")
        pending.write_bytes(payload)
        try:
            os.replace(pending, target)
        except OSError:
            pending.unlink(missing_ok=True)
            if not target.is_file():
                raise
        return sha256

    def open(self, sha256: str) -> bytes:
        target = self._blob_path(sha256)
        try:
            payload = target.read_bytes()
        except OSError as exc:
            raise BlobMissing(f"blob is not in the store: {sha256}") from exc
        if _digest_of(payload) != sha256:
            raise BlobMissing(f"blob content does not match its digest: {sha256}")
        return payload

    def put_file(self, full_path: Path) -> tuple[int, str]:
        running = hashlib.sha256()
        size = 0
        chunks: list[bytes] = []
        with open(full_path, "rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK)
                if not chunk:
                    break
                running.update(chunk)
                size += len(chunk)
                chunks.append(chunk)
        sha256 = running.hexdigest()
        if not self.has(sha256):
            self.put(b"".join(chunks))
        return size, sha256

    def materialize(
        self, snapshot: RepositorySnapshot, source_root: Path
    ) -> RepositorySnapshot:
        base = Path(source_root).resolve()
        records: list[FileRecord] = []
        for record in snapshot.files:
            full_path = base / record.path
            try:
                size, sha256 = self.put_file(full_path)
            except OSError as exc:
                raise BlobMissing(f"{record.path}: {exc}") from exc
            records.append(FileRecord(path=record.path, size=size, sha256=sha256))
        stored = tuple(records)
        digest = compute_digest(stored)
        self._write_manifest(digest, snapshot, stored)
        return RepositorySnapshot(
            root=snapshot.root,
            git_head=snapshot.git_head,
            files=stored,
            digest=digest,
            taken_at=snapshot.taken_at,
            store_root=str(self._root),
        )

    def _write_manifest(
        self,
        digest: str,
        snapshot: RepositorySnapshot,
        records: tuple[FileRecord, ...],
    ) -> None:
        target = self._manifest_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "root": snapshot.root,
            "git_head": snapshot.git_head,
            "digest": digest,
            "taken_at": snapshot.taken_at,
            "files": [
                {"path": record.path, "size": record.size, "sha256": record.sha256}
                for record in records
            ],
        }
        target.write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def load(self, digest: str) -> RepositorySnapshot:
        target = self._manifest_path(digest)
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestMissing(f"no manifest for snapshot: {digest}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManifestInvalid(f"{digest}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ManifestInvalid(f"{digest}: manifest must be a mapping")
        snapshot = RepositorySnapshot.from_dict(_with_store(payload, self._root))
        if snapshot.digest != digest:
            raise ManifestInvalid(
                f"manifest digest does not match its location: {snapshot.digest}"
            )
        return snapshot


def _with_store(payload: Mapping[str, Any], store_root: Path) -> dict[str, Any]:
    resolved = dict(payload)
    resolved["store_root"] = str(store_root)
    return resolved
