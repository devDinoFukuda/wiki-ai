from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from wiki_ai.repository.snapshot import (
    RepositorySnapshot,
    SnapshotSpec,
    SnapshotSpecInvalid,
    SnapshotPayloadInvalid,
    diff,
    matches_any,
    normalize_path,
    take_snapshot,
)


def _write(root: Path, relative: str, content: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _sample(root: Path) -> None:
    _write(root, "src/main.py", "value = 1\n")
    _write(root, "src/util/helper.py", "value = 2\n")
    _write(root, "docs/guide.md", "title\n")


def test_take_snapshot_lists_files_with_hashes(tmp_path: Path) -> None:
    _sample(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path))
    paths = [record.path for record in snapshot.files]
    assert paths == ["docs/guide.md", "src/main.py", "src/util/helper.py"]
    assert all(len(record.sha256) == 64 for record in snapshot.files)
    assert all(record.size > 0 for record in snapshot.files)
    assert len(snapshot.digest) == 64


def test_snapshot_is_immutable(tmp_path: Path) -> None:
    _sample(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path))
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.digest = "0" * 64
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.files[0].path = "other.py"

    twin_root = tmp_path.parent / "twin"
    twin_root.mkdir()
    _sample(twin_root)
    twin = take_snapshot(SnapshotSpec(root=twin_root))
    assert twin.digest == snapshot.digest


def test_digest_changes_when_content_changes(tmp_path: Path) -> None:
    _sample(tmp_path)
    before = take_snapshot(SnapshotSpec(root=tmp_path))
    _write(tmp_path, "src/main.py", "value = 99\n")
    after = take_snapshot(SnapshotSpec(root=tmp_path))
    assert before.digest != after.digest


def test_snapshot_roundtrips_through_json(tmp_path: Path) -> None:
    _sample(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path))
    payload = json.loads(snapshot.to_json())
    restored = RepositorySnapshot.from_dict(payload)
    assert restored == snapshot
    assert RepositorySnapshot.from_json(snapshot.to_json()) == snapshot


def test_from_dict_rejects_incomplete_payload() -> None:
    with pytest.raises(SnapshotPayloadInvalid):
        RepositorySnapshot.from_dict({"root": "/x", "digest": "a", "taken_at": "t"})


def test_spec_includes_and_excludes(tmp_path: Path) -> None:
    _sample(tmp_path)
    _write(tmp_path, "node_modules/pkg/index.js", "x\n")
    included = take_snapshot(SnapshotSpec(root=tmp_path, includes=("src",)))
    assert [record.path for record in included.files] == [
        "src/main.py",
        "src/util/helper.py",
    ]
    excluded = take_snapshot(SnapshotSpec(root=tmp_path, excludes=("node_modules",)))
    assert not any("node_modules" in record.path for record in excluded.files)


def test_spec_roots_limit_the_scan(tmp_path: Path) -> None:
    _sample(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path, roots=("docs",)))
    assert [record.path for record in snapshot.files] == ["docs/guide.md"]


def test_spec_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(SnapshotSpecInvalid):
        take_snapshot(SnapshotSpec(root=tmp_path / "absent"))


def test_git_directory_is_never_captured(tmp_path: Path) -> None:
    _sample(tmp_path)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path))
    assert not any(record.path.startswith(".git/") for record in snapshot.files)


def test_git_head_is_none_without_repository(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "x\n")
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path))
    assert snapshot.git_head is None or len(snapshot.git_head) == 40


def test_diff_reports_added_removed_and_changed(tmp_path: Path) -> None:
    _sample(tmp_path)
    before = take_snapshot(SnapshotSpec(root=tmp_path))
    (tmp_path / "docs" / "guide.md").unlink()
    _write(tmp_path, "src/main.py", "value = 3\n")
    _write(tmp_path, "src/new.py", "value = 4\n")
    after = take_snapshot(SnapshotSpec(root=tmp_path))
    result = diff(before, after)
    assert result.added == ("src/new.py",)
    assert result.removed == ("docs/guide.md",)
    assert result.changed == ("src/main.py",)
    assert not result.is_empty()


def test_pattern_matching_supports_prefix_and_glob() -> None:
    assert matches_any("src/a/b.py", ("src",))
    assert matches_any("src/a/b.py", ("src/**",))
    assert matches_any("src/a/b.py", ("src/*.py",))
    assert not matches_any("srcx/a.py", ("src",))
    assert normalize_path("\\a\\b\\") == "a/b"
