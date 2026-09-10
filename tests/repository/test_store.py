from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.repository.fixtures_repos import store_for
from wiki_ai.repository.snapshot import (
    BlobUnavailable,
    RepositorySnapshot,
    SnapshotNotMaterialized,
    SnapshotSpec,
    observe,
    take_snapshot,
)
from wiki_ai.repository.store import (
    BLOBS_DIRECTORY,
    MANIFEST_NAME,
    BlobMissing,
    ManifestInvalid,
    ManifestMissing,
    SnapshotStore,
)


ALPHA = b"value = 1\n"
BETA = b"value = 2\n"
CHANGED = b"value = 999\n"
SHARED = b"shared = 1\n"


def _write_bytes(root: Path, relative: str, payload: bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _repo(root: Path) -> None:
    _write_bytes(root, "src/alpha.py", ALPHA)
    _write_bytes(root, "src/beta.py", BETA)


def test_put_and_open_round_trip(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "store")
    payload = b"content bytes\n"
    sha256 = store.put(payload)
    assert sha256 == hashlib.sha256(payload).hexdigest()
    assert store.has(sha256) is True
    assert store.open(sha256) == payload


def test_blob_layout_is_content_addressed(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "store")
    sha256 = store.put(b"x\n")
    expected = tmp_path / "store" / BLOBS_DIRECTORY / sha256[:2] / sha256
    assert expected.is_file()
    assert expected.read_bytes() == b"x\n"


def test_open_rejects_unknown_blob(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "store")
    with pytest.raises(BlobMissing):
        store.open("0" * 64)


def test_open_rejects_blob_whose_bytes_do_not_match_the_digest(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "store")
    sha256 = store.put(b"honest\n")
    blob = tmp_path / "store" / BLOBS_DIRECTORY / sha256[:2] / sha256
    blob.write_bytes(b"forged\n")
    with pytest.raises(BlobMissing):
        store.open(sha256)


def test_take_snapshot_without_store_is_not_materialized(tmp_path: Path) -> None:
    _repo(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path))
    assert snapshot.materialized is False
    with pytest.raises(SnapshotNotMaterialized):
        snapshot.read_bytes("src/alpha.py")


def test_take_snapshot_with_store_materializes_every_file(tmp_path: Path) -> None:
    _repo(tmp_path)
    store = store_for(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path), store)
    assert snapshot.materialized is True
    assert snapshot.read_bytes("src/alpha.py") == b"value = 1\n"
    assert snapshot.read_bytes("src/beta.py") == b"value = 2\n"
    for record in snapshot.files:
        assert store.has(record.sha256) is True


def test_read_bytes_rejects_a_path_outside_the_snapshot(tmp_path: Path) -> None:
    _repo(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path), store_for(tmp_path))
    with pytest.raises(BlobUnavailable):
        snapshot.read_bytes("src/absent.py")


def test_snapshot_content_survives_working_tree_changes(tmp_path: Path) -> None:
    _repo(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path), store_for(tmp_path))
    _write_bytes(tmp_path, "src/alpha.py", CHANGED)
    assert snapshot.read_bytes("src/alpha.py") == b"value = 1\n"


def test_manifest_is_written_per_digest(tmp_path: Path) -> None:
    _repo(tmp_path)
    store = store_for(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path), store)
    manifest = store.root / snapshot.digest / MANIFEST_NAME
    assert manifest.is_file()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["digest"] == snapshot.digest
    entries = {item["path"]: item for item in payload["files"]}
    assert set(entries) == {"src/alpha.py", "src/beta.py"}
    assert entries["src/alpha.py"]["size"] == len(b"value = 1\n")
    assert len(entries["src/alpha.py"]["sha256"]) == 64


def test_load_reconstructs_an_identical_snapshot(tmp_path: Path) -> None:
    _repo(tmp_path)
    store = store_for(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path), store)
    _write_bytes(tmp_path, "src/alpha.py", CHANGED)
    restored = SnapshotStore(store.root).load(snapshot.digest)
    assert restored.digest == snapshot.digest
    assert restored.files == snapshot.files
    assert restored.materialized is True
    assert restored.read_bytes("src/alpha.py") == b"value = 1\n"


def test_load_rejects_a_missing_or_corrupt_manifest(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "store")
    with pytest.raises(ManifestMissing):
        store.load("a" * 64)
    target = store.root / ("b" * 64) / MANIFEST_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestInvalid):
        store.load("b" * 64)


def test_load_rejects_a_manifest_whose_digest_was_relocated(tmp_path: Path) -> None:
    _repo(tmp_path)
    store = store_for(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path), store)
    moved = store.root / ("c" * 64)
    moved.mkdir(parents=True, exist_ok=True)
    source = store.root / snapshot.digest / MANIFEST_NAME
    (moved / MANIFEST_NAME).write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ManifestInvalid):
        store.load("c" * 64)


def test_identical_bytes_are_stored_once_across_snapshots(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_bytes(first, "a.py", SHARED)
    _write_bytes(second, "nested/b.py", SHARED)
    store = SnapshotStore(tmp_path / "store")
    left = take_snapshot(SnapshotSpec(root=first), store)
    right = take_snapshot(SnapshotSpec(root=second), store)
    assert left.digest != right.digest
    shared = left.files[0].sha256
    assert right.files[0].sha256 == shared
    blobs = [
        item
        for item in (store.root / BLOBS_DIRECTORY).rglob("*")
        if item.is_file()
    ]
    assert [item.name for item in blobs] == [shared]


def test_materialize_reports_a_file_that_vanished(tmp_path: Path) -> None:
    _repo(tmp_path)
    store = store_for(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path))
    (tmp_path / "src" / "alpha.py").unlink()
    with pytest.raises(BlobMissing):
        store.materialize(snapshot, tmp_path)


def test_observe_reads_without_materializing(tmp_path: Path) -> None:
    _repo(tmp_path)
    store = SnapshotStore(tmp_path.parent / "observe-store")
    observation = observe(SnapshotSpec(root=tmp_path))
    assert len(observation.digest) == 64
    assert {record.path for record in observation.files} == {
        "src/alpha.py",
        "src/beta.py",
    }
    assert not (store.root / BLOBS_DIRECTORY).exists()


def test_observe_digest_matches_a_materialized_snapshot(tmp_path: Path) -> None:
    _repo(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path), store_for(tmp_path))
    assert observe(SnapshotSpec(root=tmp_path)).digest == snapshot.digest


def test_observe_detects_the_new_digest_after_a_change(tmp_path: Path) -> None:
    _repo(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path), store_for(tmp_path))
    _write_bytes(tmp_path, "src/alpha.py", CHANGED)
    later = observe(SnapshotSpec(root=tmp_path))
    assert later.digest != snapshot.digest
    assert later.file_map()["src/alpha.py"].sha256 != (
        snapshot.file_map()["src/alpha.py"].sha256
    )
    assert later.to_dict()["digest"] == later.digest


def test_snapshot_dict_round_trip_keeps_the_store(tmp_path: Path) -> None:
    _repo(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path), store_for(tmp_path))
    restored = RepositorySnapshot.from_json(snapshot.to_json())
    assert restored == snapshot
    assert restored.read_bytes("src/beta.py") == b"value = 2\n"


def test_empty_file_is_readable_from_the_snapshot(tmp_path: Path) -> None:
    _write_bytes(tmp_path, "empty.txt", b"")
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path), store_for(tmp_path))
    assert snapshot.read_bytes("empty.txt") == b""
