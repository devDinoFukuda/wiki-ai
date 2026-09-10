from __future__ import annotations

from pathlib import Path

import pytest

from wiki_ai.repository.reader import (
    PathOutsideSnapshot,
    RangeInvalid,
    read_range,
)
from tests.repository.fixtures_repos import store_for
from wiki_ai.repository.snapshot import SnapshotSpec, take_snapshot

_CONTENT = "one\ntwo\nthree\nfour\n"


def _snapshot(root: Path):
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "file.py").write_bytes(_CONTENT.encode("utf-8"))
    return take_snapshot(SnapshotSpec(root=root), store_for(root))


def test_read_range_returns_selected_lines(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    result = read_range(snapshot, "src/file.py", 2, 3)
    assert result.text == "two\nthree\n"
    assert result.start == 2
    assert result.end == 3
    assert result.total_lines == 4
    assert result.truncated is False


def test_read_range_defaults_to_whole_file(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    result = read_range(snapshot, "src/file.py")
    assert result.text == _CONTENT


def test_read_range_rejects_path_outside_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    (tmp_path / "secret.txt").write_text("hidden\n", encoding="utf-8")
    with pytest.raises(PathOutsideSnapshot):
        read_range(snapshot, "secret.txt")


def test_read_range_rejects_traversal(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with pytest.raises(PathOutsideSnapshot):
        read_range(snapshot, "../outside.py")


def test_read_range_rejects_invalid_ranges(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with pytest.raises(RangeInvalid):
        read_range(snapshot, "src/file.py", 0, 2)
    with pytest.raises(RangeInvalid):
        read_range(snapshot, "src/file.py", 3, 2)
    with pytest.raises(RangeInvalid):
        read_range(snapshot, "src/file.py", 1, 99)


def test_read_range_truncates_at_max_bytes(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    result = read_range(snapshot, "src/file.py", 1, 4, max_bytes=5)
    assert result.truncated is True
    assert len(result.text.encode("utf-8")) <= 5
