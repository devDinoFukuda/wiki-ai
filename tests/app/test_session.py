from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiki_ai.app.session import (
    DATABASE_FILE,
    FORMAT_VERSION,
    STATE_DIR_NAME,
    SUBDIRECTORIES,
    Session,
    SessionError,
    SnapshotNotStored,
    detect_outdated_store,
)
from wiki_ai.repository.snapshot import SnapshotSpec, take_snapshot


def test_session_creates_the_state_layout(tmp_path: Path) -> None:
    session = Session.open(tmp_path)
    assert session.state_dir == tmp_path / STATE_DIR_NAME
    for name in SUBDIRECTORIES:
        assert (session.state_dir / name).is_dir()
    assert set(SUBDIRECTORIES) == {"snapshots", "evidence", "publications"}


def test_session_writes_the_format_version(tmp_path: Path) -> None:
    session = Session.open(tmp_path)
    payload = json.loads(session.format_path.read_text(encoding="utf-8"))
    assert payload["format_version"] == FORMAT_VERSION
    assert payload["repository_identity"] == session.identity
    assert session.format_version() == 1


def test_session_reserves_the_database_without_creating_it(tmp_path: Path) -> None:
    session = Session.open(tmp_path)
    assert session.database_path == session.state_dir / DATABASE_FILE
    assert not session.database_path.exists()


def test_session_is_idempotent(tmp_path: Path) -> None:
    first = Session.open(tmp_path)
    first.format_path.write_text('{"format_version": 1}\n', encoding="utf-8")
    second = Session.open(tmp_path)
    assert first == second
    assert second.format_version() == 1


def test_session_rejects_missing_repository(tmp_path: Path) -> None:
    with pytest.raises(SessionError):
        Session.open(tmp_path / "absent")


def test_session_stores_and_reloads_snapshots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    snapshot = take_snapshot(SnapshotSpec(root=repo, excludes=(STATE_DIR_NAME,)))
    session = Session.open(repo)
    stored = session.save_snapshot(snapshot)
    assert stored.name == f"{snapshot.digest}.json"
    assert session.stored_snapshots() == (snapshot.digest,)
    assert session.load_snapshot(snapshot.digest) == snapshot


def test_session_reports_unknown_snapshot(tmp_path: Path) -> None:
    session = Session.open(tmp_path)
    with pytest.raises(SnapshotNotStored):
        session.load_snapshot("0" * 64)


def test_detect_outdated_store_lists_present_markers(tmp_path: Path) -> None:
    assert detect_outdated_store(tmp_path) == ()
    (tmp_path / ".codescan").mkdir()
    (tmp_path / "wiki-docx").mkdir()
    assert detect_outdated_store(tmp_path) == (".codescan", "wiki-docx")


def test_repository_identity_is_stable_and_prefixed(tmp_path: Path) -> None:
    from wiki_ai.app.session import repository_identity

    first = repository_identity(tmp_path)
    second = repository_identity(tmp_path)
    assert first == second
    assert first.startswith("repo_")
    assert len(first) == len("repo_") + 32


def test_repository_identity_differs_between_unrelated_paths(tmp_path: Path) -> None:
    from wiki_ai.app.session import repository_identity

    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    assert repository_identity(left) != repository_identity(right)


def test_repository_identity_follows_the_git_toplevel(tmp_path: Path) -> None:
    import subprocess

    from wiki_ai.app.session import repository_identity

    repo = tmp_path / "repo"
    (repo / "nested" / "deep").mkdir(parents=True)
    completed = subprocess.run(
        ["git", "init", str(repo)], capture_output=True, text=True
    )
    if completed.returncode != 0:
        pytest.skip("git is unavailable")
    assert repository_identity(repo / "nested" / "deep") == repository_identity(repo)


def test_session_namespace_matches_the_repository_identity(tmp_path: Path) -> None:
    session = Session.open(tmp_path)
    assert session.namespace == session.identity


def test_explicit_home_relocates_the_state_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    session = Session.open(repo, home=str(home))
    assert session.state_dir == home.resolve() / session.identity
    assert session.state_dir.is_dir()
    assert not (repo / STATE_DIR_NAME).exists()


def test_saving_a_snapshot_updates_the_latest_pointer(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    snapshot = take_snapshot(SnapshotSpec(root=repo, excludes=(STATE_DIR_NAME,)))
    session = Session.open(repo)
    session.save_snapshot(snapshot)
    assert session.latest_path.is_file()
    assert session.current_snapshot_id() == snapshot.digest
    assert session.current_snapshot() == snapshot
    assert session.stored_snapshots() == (snapshot.digest,)


def test_current_snapshot_is_absent_before_any_analysis(tmp_path: Path) -> None:
    session = Session.open(tmp_path)
    assert session.current_snapshot_id() is None
    assert session.current_snapshot() is None


def test_current_snapshot_survives_a_truncated_pointer(tmp_path: Path) -> None:
    session = Session.open(tmp_path)
    session.latest_path.write_text("{ not json", encoding="utf-8")
    assert session.current_snapshot_id() is None


def test_current_snapshot_is_absent_when_the_file_was_removed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    snapshot = take_snapshot(SnapshotSpec(root=repo, excludes=(STATE_DIR_NAME,)))
    session = Session.open(repo)
    session.save_snapshot(snapshot)
    session.snapshot_path(snapshot.digest).unlink()
    assert session.current_snapshot() is None


def test_open_knowledge_creates_the_database(tmp_path: Path) -> None:
    session = Session.open(tmp_path)
    with session.open_knowledge() as knowledge:
        assert knowledge.entity_count() == 0
    assert session.database_path.is_file()


def test_resolve_provider_returns_none_without_providers(tmp_path: Path) -> None:
    from wiki_ai.agent.registry import ProviderRegistry

    session = Session.open(tmp_path)
    assert session.resolve_provider(ProviderRegistry()) is None
