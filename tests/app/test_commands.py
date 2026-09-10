from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from wiki_ai import __version__
from wiki_ai.agent.protocol import AgentCapabilities
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.agent.session import AgentRun, BudgetUsage, RunStatus
from wiki_ai.app import api
from wiki_ai.app.commands import COMMANDS, EXIT_BLOCKED, EXIT_ERROR, EXIT_OK, main
from wiki_ai.app.session import (
    CODESCAN_DIRECTORY,
    COMPANION_DIRECTORIES,
    DOCX_DIRECTORY,
)


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


def _blocked_payload() -> dict:
    return {
        "status": "blocked",
        "reason": "outdated_store",
        "action": "run a new analysis",
    }


def test_inspect_is_blocked_by_a_codescan_store(tmp_path: Path) -> None:
    _mixed_repo(tmp_path)
    directory = tmp_path / CODESCAN_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "state.db").write_bytes(b"")
    code, payload = _run("inspect", str(tmp_path))
    assert code == EXIT_BLOCKED
    assert payload == _blocked_payload()


def test_inspect_is_blocked_by_a_docx_store(tmp_path: Path) -> None:
    _mixed_repo(tmp_path)
    (tmp_path / DOCX_DIRECTORY).mkdir(parents=True, exist_ok=True)
    for name in COMPANION_DIRECTORIES:
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    code, payload = _run("inspect", str(tmp_path))
    assert code == EXIT_BLOCKED
    assert payload == _blocked_payload()


def test_inspect_accepts_a_repository_with_raw_or_wiki_directories(
    tmp_path: Path,
) -> None:
    _mixed_repo(tmp_path)
    for name in COMPANION_DIRECTORIES:
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    code, payload = _run("inspect", str(tmp_path))
    assert code == EXIT_OK
    assert payload["status"] == "ok"


def test_inspect_reports_missing_repository(tmp_path: Path) -> None:
    code, payload = _run("inspect", str(tmp_path / "absent"))
    assert code == EXIT_ERROR
    assert payload["status"] == "error"
    assert payload["reason"] == "inspect_failed"


def test_unknown_command_is_reported_as_json() -> None:
    code, payload = _run("promote")
    assert code == EXIT_ERROR
    assert payload["reason"] == "invalid_arguments"
    assert payload["commands"] == list(COMMANDS)


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


class _CliProvider:
    def connect(self) -> None:
        return None

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(tools=("repo.read",))

    def run(self, session: Any) -> AgentRun:
        return AgentRun(
            status=RunStatus.COMPLETED,
            findings=(),
            transcript=(),
            usage=BudgetUsage(tool_calls=0, tokens=0, seconds=0.0),
        )

    def cancel(self) -> None:
        return None


def _cli_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register("probe", _CliProvider)
    return registry


def _run_with(registry: ProviderRegistry, *argv: str) -> tuple[int, dict]:
    stream = io.StringIO()
    code = main(list(argv), stream=stream, registry=registry)
    return code, json.loads(stream.getvalue())


def test_provider_show_reports_no_preference_by_default(tmp_path: Path) -> None:
    _mixed_repo(tmp_path)
    code, payload = _run_with(_cli_registry(), "provider", "show", "--repo", str(tmp_path))
    assert code == EXIT_OK
    assert payload["status"] == "ok"
    assert payload["provider"] == ""
    assert payload["registered"] == ["probe"]
    assert payload["command"] == "provider"


def test_provider_set_then_show_round_trips(tmp_path: Path) -> None:
    _mixed_repo(tmp_path)
    registry = _cli_registry()
    code, payload = _run_with(
        registry, "provider", "set", "probe", "--repo", str(tmp_path)
    )
    assert code == EXIT_OK
    assert payload["provider"] == "probe"
    code, shown = _run_with(registry, "provider", "show", "--repo", str(tmp_path))
    assert code == EXIT_OK
    assert shown["provider"] == "probe"
    assert shown["source"] == "preferences"


def test_provider_set_rejects_an_unknown_name(tmp_path: Path) -> None:
    _mixed_repo(tmp_path)
    code, payload = _run_with(
        _cli_registry(), "provider", "set", "absent", "--repo", str(tmp_path)
    )
    assert code == EXIT_ERROR
    assert payload["status"] == "error"
    assert payload["reason"] == "unknown_provider"
    assert payload["registered"] == ["probe"]


def test_provider_requires_an_action() -> None:
    code, payload = _run("provider")
    assert code == EXIT_ERROR
    assert payload["reason"] == "invalid_arguments"


def test_the_stored_preference_reaches_analyze(tmp_path: Path) -> None:
    _mixed_repo(tmp_path)
    registry = _cli_registry()
    _run_with(registry, "provider", "set", "probe", "--repo", str(tmp_path))
    code, payload = _run_with(registry, "analyze", str(tmp_path))
    assert payload["provider"] == "probe"
    assert code in (EXIT_OK, EXIT_BLOCKED)
