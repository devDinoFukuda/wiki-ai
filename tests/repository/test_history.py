from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.repository.fixtures_repos import python_repo, snapshot_of, write
from wiki_ai.repository.history import history


def _git_available() -> bool:
    return shutil.which("git") is not None


def _run(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _git_repo(root: Path) -> None:
    _run(root, "init", "--initial-branch=main")
    _run(root, "config", "user.email", "harness@example.invalid")
    _run(root, "config", "user.name", "Harness")
    _run(root, "config", "commit.gpgsign", "false")


def test_history_without_git_reports_reason(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    report = history(snapshot)
    assert report.available is False
    assert report.reason == "not_a_git_repository"
    assert report.commits == ()
    assert report.blame == ()


def test_history_rejects_path_outside_snapshot(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    report = history(snapshot, "../outside.py")
    assert report.available is False
    assert report.reason == "path_not_in_snapshot"


def test_history_rejects_absent_path(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    assert history(snapshot, "billing/absent.py").reason == "path_not_in_snapshot"


@pytest.mark.skipif(not _git_available(), reason="git executable is unavailable")
def test_history_reads_commits_blame_and_renames(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    write(tmp_path, "billing/service.py", "VALUE = 1\n")
    _run(tmp_path, "add", ".")
    _run(tmp_path, "commit", "-m", "first commit")
    _run(tmp_path, "mv", "billing/service.py", "billing/invoice.py")
    _run(tmp_path, "commit", "-m", "rename module")
    write(tmp_path, "billing/invoice.py", "VALUE = 2\nEXTRA = 3\n")
    _run(tmp_path, "add", ".")
    _run(tmp_path, "commit", "-m", "extend module")
    snapshot = snapshot_of(tmp_path)
    report = history(snapshot, "billing/invoice.py")
    assert report.available is True
    assert report.reason is None
    assert report.head == snapshot.git_head
    assert [item.subject for item in report.commits][0] == "extend module"
    assert all(item.author == "Harness" for item in report.commits)
    assert {item.line for item in report.blame} == {1, 2}
    assert any(
        item.old_path == "billing/service.py" and item.new_path == "billing/invoice.py"
        for item in report.renames
    )


@pytest.mark.skipif(not _git_available(), reason="git executable is unavailable")
def test_history_limit_bounds_commits(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    for index in range(4):
        write(tmp_path, "app.py", f"VALUE = {index}\n")
        _run(tmp_path, "add", ".")
        _run(tmp_path, "commit", "-m", f"commit {index}")
    snapshot = snapshot_of(tmp_path)
    assert len(history(snapshot, limit=2).commits) == 2
    assert history(snapshot, limit=0).reason == "limit_must_be_positive"
