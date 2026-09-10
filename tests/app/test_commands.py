from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from wiki_ai import __version__
from wiki_ai.app import api
from wiki_ai.app.commands import EXIT_BLOCKED, EXIT_ERROR, EXIT_OK, main
from wiki_ai.app.session import OUTDATED_STORE_MARKERS


def _run(*argv: str) -> tuple[int, dict]:
    stream = io.StringIO()
    code = main(list(argv), stream=stream)
    return code, json.loads(stream.getvalue())


def _mixed_repo(root: Path) -> Path:
    files = {
        "src/app.py": "value = 1\n",
        "src/Service.java": "class Service {}\n",
        "cobol/PROG.CBL": "IDENTIFICATION DIVISION.\n",
        "jcl/JOB.JCL": "//JOB1 JOB\n",
        "db/schema.sql": "select 1;\n",
        "tests/test_app.py": "value = 1\n",
        "README.md": "title\n",
    }
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def test_version_command_returns_the_package_version() -> None:
    code, payload = _run("version")
    assert code == EXIT_OK
    assert payload["status"] == "ok"
    assert payload["version"] == __version__


def test_inspect_reports_counts_for_a_mixed_repository(tmp_path: Path) -> None:
    _mixed_repo(tmp_path)
    code, payload = _run("inspect", str(tmp_path))
    assert code == EXIT_OK
    assert payload["status"] == "ok"
    assert payload["total_files"] == 7
    assert payload["by_language"]["cobol"] == 1
    assert payload["by_language"]["jcl"] == 1
    assert payload["by_language"]["sql"] == 1
    assert payload["by_language"]["java"] == 1
    assert payload["by_language"]["python"] == 2
    assert payload["by_classification"]["source"] == 5
    assert payload["by_classification"]["test"] == 1
    assert payload["by_classification"]["document"] == 1
    assert len(payload["snapshot_digest"]) == 64


@pytest.mark.parametrize("marker", OUTDATED_STORE_MARKERS)
def test_inspect_is_blocked_by_an_outdated_store(tmp_path: Path, marker: str) -> None:
    _mixed_repo(tmp_path)
    (tmp_path / marker).mkdir(parents=True, exist_ok=True)
    code, payload = _run("inspect", str(tmp_path))
    assert code == EXIT_BLOCKED
    assert payload == {
        "status": "blocked",
        "reason": "outdated_store",
        "action": "run a new analysis",
    }


def test_inspect_reports_missing_repository(tmp_path: Path) -> None:
    code, payload = _run("inspect", str(tmp_path / "absent"))
    assert code == EXIT_ERROR
    assert payload["status"] == "error"
    assert payload["reason"] == "inspect_failed"


def test_unknown_command_is_reported_as_json() -> None:
    code, payload = _run("promote")
    assert code == EXIT_ERROR
    assert payload["reason"] == "invalid_arguments"
    assert payload["commands"] == ["version", "inspect"]


def test_missing_command_is_reported_as_json() -> None:
    code, payload = _run()
    assert code == EXIT_ERROR
    assert payload["reason"] == "invalid_arguments"


def test_api_inspect_is_pure(tmp_path: Path) -> None:
    _mixed_repo(tmp_path)
    first = api.inspect(tmp_path)
    second = api.inspect(tmp_path)
    assert first.snapshot_digest == second.snapshot_digest
    assert not (tmp_path / ".wiki-ai").exists()


def test_api_version_matches_package() -> None:
    assert api.version() == __version__
