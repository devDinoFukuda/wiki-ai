from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from wiki_ai.publishing.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestInvalid,
    hash_file,
    normalize_relative_path,
)

__all__ = [
    "STAGING_DIRNAME",
    "RELEASES_DIRNAME",
    "CURRENT_POINTER",
    "ReleaseError",
    "PublicationExists",
    "PublicationNotFound",
    "ArtifactMissing",
    "HashMismatch",
    "PointerBusy",
    "PublicationIdInvalid",
    "StagedRelease",
    "Publication",
    "staging_dir",
    "release_dir",
    "read_manifest",
    "stage",
    "promote",
    "rollback",
    "current",
    "list_publications",
]

STAGING_DIRNAME = "staging"
RELEASES_DIRNAME = "releases"
CURRENT_POINTER = "current"
POINTER_LOCK_FILENAME = ".current.lock"
POINTER_LOCK_TIMEOUT_S = 10.0

_PUBLICATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROCESS_LOCK = threading.Lock()


class ReleaseError(RuntimeError):
    pass


class PublicationExists(ReleaseError):
    def __init__(self, publication_id: str, directory: Path) -> None:
        self.publication_id = publication_id
        self.directory = directory
        super().__init__(
            f"publication {publication_id!r} already exists at {directory}; "
            "released packages are never overwritten"
        )


class PublicationNotFound(ReleaseError):
    def __init__(self, publication_id: str, directory: Path) -> None:
        self.publication_id = publication_id
        self.directory = directory
        super().__init__(f"publication {publication_id!r} not found at {directory}")


class ArtifactMissing(ReleaseError):
    def __init__(self, relative_path: str, directory: Path) -> None:
        self.relative_path = relative_path
        self.directory = directory
        super().__init__(f"artifact {relative_path!r} is absent from {directory}")


class HashMismatch(ReleaseError):
    def __init__(self, relative_path: str, expected: str, found: str) -> None:
        self.relative_path = relative_path
        self.expected = expected
        self.found = found
        super().__init__(
            f"artifact {relative_path!r} hashes to {found} but the manifest declares {expected}"
        )


class PointerBusy(ReleaseError):
    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        super().__init__(
            f"could not acquire the publication pointer lock at {path} within {timeout}s"
        )


class PublicationIdInvalid(ReleaseError):
    def __init__(self, publication_id: str) -> None:
        self.publication_id = publication_id
        super().__init__(
            f"publication_id {publication_id!r} is not a single safe path segment"
        )


@dataclass(frozen=True)
class StagedRelease:
    root: Path
    directory: Path
    manifest: Manifest

    @property
    def publication_id(self) -> str:
        return self.manifest.publication_id


@dataclass(frozen=True)
class Publication:
    root: Path
    directory: Path
    manifest: Manifest

    @property
    def publication_id(self) -> str:
        return self.manifest.publication_id


def _root(publications_root: Path | str) -> Path:
    return Path(publications_root).absolute()


def _checked_id(publication_id: str) -> str:
    if not _PUBLICATION_ID_RE.match(str(publication_id)):
        raise PublicationIdInvalid(str(publication_id))
    return str(publication_id)


def staging_dir(publications_root: Path | str, publication_id: str) -> Path:
    return _root(publications_root) / STAGING_DIRNAME / _checked_id(publication_id)


def release_dir(publications_root: Path | str, publication_id: str) -> Path:
    return _root(publications_root) / RELEASES_DIRNAME / _checked_id(publication_id)


def read_manifest(directory: Path | str) -> Manifest:
    path = Path(directory) / MANIFEST_FILENAME
    if not path.is_file():
        raise ArtifactMissing(MANIFEST_FILENAME, Path(directory))
    try:
        return Manifest.from_json(path.read_text(encoding="utf-8"))
    except ManifestInvalid as exc:
        raise ReleaseError(f"manifest unreadable at {path}: {exc}") from exc


def _lock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def _pointer_lock(
    root: Path, timeout: float = POINTER_LOCK_TIMEOUT_S
) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    path = root / POINTER_LOCK_FILENAME
    acquired_process_lock = _PROCESS_LOCK.acquire(timeout=timeout)
    if not acquired_process_lock:
        raise PointerBusy(path, timeout)
    handle = open(path, "a+b")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _lock_handle(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise PointerBusy(path, timeout)
                time.sleep(0.02)
        try:
            yield
        finally:
            _unlock_handle(handle)
    finally:
        handle.close()
        _PROCESS_LOCK.release()


def _write_pointer(root: Path, publication_id: str) -> None:
    with _pointer_lock(root):
        descriptor, temporary = tempfile.mkstemp(
            dir=root, prefix=".current-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(publication_id + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, root / CURRENT_POINTER)
        except BaseException:
            if os.path.exists(temporary):
                os.remove(temporary)
            raise


def _verify_artifacts(directory: Path, manifest: Manifest) -> None:
    for artifact in manifest.artifacts:
        path = directory / artifact.relative_path
        if not path.is_file():
            if artifact.required:
                raise ArtifactMissing(artifact.relative_path, directory)
            continue
        found = hash_file(path)
        if found != artifact.sha256:
            raise HashMismatch(artifact.relative_path, artifact.sha256, found)


def stage(
    publications_root: Path | str,
    manifest: Manifest,
    files: Mapping[str, bytes],
) -> StagedRelease:
    root = _root(publications_root)
    publication_id = _checked_id(manifest.publication_id)
    target = root / RELEASES_DIRNAME / publication_id
    if target.exists():
        raise PublicationExists(publication_id, target)

    directory = root / STAGING_DIRNAME / publication_id
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)

    for raw_path, data in files.items():
        relative = normalize_relative_path(raw_path)
        destination = directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    manifest_path = directory / MANIFEST_FILENAME
    manifest_path.write_text(manifest.to_json() + "\n", encoding="utf-8", newline="\n")
    return StagedRelease(root=root, directory=directory, manifest=manifest)


def promote(staged: StagedRelease) -> Publication:
    root = staged.root
    publication_id = _checked_id(staged.publication_id)
    releases = root / RELEASES_DIRNAME
    target = releases / publication_id
    if target.exists():
        raise PublicationExists(publication_id, target)
    if not staged.directory.is_dir():
        raise ArtifactMissing(STAGING_DIRNAME, staged.directory)

    _verify_artifacts(staged.directory, staged.manifest)

    releases.mkdir(parents=True, exist_ok=True)
    os.replace(staged.directory, target)
    try:
        _write_pointer(root, publication_id)
    except BaseException:
        os.replace(target, staged.directory)
        raise
    return Publication(root=root, directory=target, manifest=staged.manifest)


def rollback(publications_root: Path | str, to_publication_id: str) -> Publication:
    root = _root(publications_root)
    publication_id = _checked_id(to_publication_id)
    target = root / RELEASES_DIRNAME / publication_id
    if not target.is_dir():
        raise PublicationNotFound(publication_id, target)
    manifest = read_manifest(target)
    _write_pointer(root, publication_id)
    return Publication(root=root, directory=target, manifest=manifest)


def current(publications_root: Path | str) -> Publication | None:
    root = _root(publications_root)
    pointer = root / CURRENT_POINTER
    if not pointer.is_file():
        return None
    publication_id = pointer.read_text(encoding="utf-8").strip()
    if not publication_id:
        return None
    target = root / RELEASES_DIRNAME / _checked_id(publication_id)
    if not target.is_dir():
        raise PublicationNotFound(publication_id, target)
    return Publication(
        root=root, directory=target, manifest=read_manifest(target)
    )


def list_publications(publications_root: Path | str) -> tuple[str, ...]:
    releases = _root(publications_root) / RELEASES_DIRNAME
    if not releases.is_dir():
        return ()
    return tuple(sorted(entry.name for entry in releases.iterdir() if entry.is_dir()))
