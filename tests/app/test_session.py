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
    assert payload == {"format_version": FORMAT_VERSION}
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
