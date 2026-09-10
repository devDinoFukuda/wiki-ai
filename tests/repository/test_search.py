from __future__ import annotations

from pathlib import Path

import pytest

from wiki_ai.repository.search import SearchPatternInvalid, search
from wiki_ai.repository.snapshot import SnapshotSpec, take_snapshot


def _write(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _snapshot(root: Path):
    _write(root, "src/alpha.py", "alpha = 1\nbeta = alpha + 1\n")
    _write(root, "src/beta.java", "int alpha = 2;\n")
    _write(root, "docs/notes.md", "alpha in prose\n")
    return take_snapshot(SnapshotSpec(root=root))


def test_search_finds_literal_matches(tmp_path: Path) -> None:
    result = search(_snapshot(tmp_path), "alpha")
    assert len(result.matches) == 4
    assert result.matches[0].path == "docs/notes.md"
    assert result.matches[0].line == 1
    assert result.matches[0].column == 1


def test_search_reports_line_and_column(tmp_path: Path) -> None:
    result = search(_snapshot(tmp_path), "beta", globs=("src/alpha.py",))
    assert [(m.line, m.column) for m in result.matches] == [(2, 1)]
    assert result.matches[0].excerpt == "beta = alpha + 1"


def test_search_treats_pattern_literally_unless_regex(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    assert search(snapshot, "alpha + 1").matches
    assert search(snapshot, "a.pha", regex=True).matches
    assert not search(snapshot, "a.pha").matches


def test_search_filters_by_extension(tmp_path: Path) -> None:
    result = search(_snapshot(tmp_path), "alpha", extensions=("java",))
    assert result.paths() == ("src/beta.java",)


def test_search_filters_by_glob(tmp_path: Path) -> None:
    result = search(_snapshot(tmp_path), "alpha", globs=("src/**",))
    assert result.paths() == ("src/alpha.py", "src/beta.java")


def test_search_honours_max_results(tmp_path: Path) -> None:
    result = search(_snapshot(tmp_path), "alpha", max_results=2)
    assert len(result.matches) == 2
    assert result.truncated is True


def test_search_skips_oversized_files(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    result = search(snapshot, "alpha", max_file_bytes=1)
    assert not result.matches
    assert len(result.skipped) == 3


def test_search_rejects_invalid_pattern(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with pytest.raises(SearchPatternInvalid):
        search(snapshot, "(", regex=True)
    with pytest.raises(SearchPatternInvalid):
        search(snapshot, "")
    with pytest.raises(SearchPatternInvalid):
        search(snapshot, "alpha", max_results=0)
