from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path

import pytest

from wiki_ai.app.commands import (
    COMMANDS,
    EXIT_BLOCKED,
    EXIT_OK,
    build_parser,
    main,
)

FORBIDDEN_OPTIONS = ("--store", "--engine", "--binding", "--provider", "--objective-id")
PRODUCT_COMMANDS = ("analyze", "ingest", "ask", "publish", "status")


def _subparsers() -> dict[str, argparse.ArgumentParser]:
    parser = build_parser()
    for action in parser._subparsers._group_actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("the parser declares no subcommands")


def _option_strings(parser: argparse.ArgumentParser) -> tuple[str, ...]:
    found: list[str] = []
    for action in parser._actions:
        found.extend(action.option_strings)
    return tuple(found)


def _run(*argv: str) -> tuple[int, dict]:
    stream = io.StringIO()
    code = main(list(argv), stream=stream)
    return code, json.loads(stream.getvalue())


def _repo(root: Path) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    return root


def test_the_product_commands_are_exactly_the_declared_ones() -> None:
    registered = _subparsers()
    assert tuple(sorted(registered)) == tuple(sorted(COMMANDS))
    for name in PRODUCT_COMMANDS:
        assert name in registered


@pytest.mark.parametrize("name", COMMANDS)
def test_each_command_has_exactly_one_entry(name: str) -> None:
    assert COMMANDS.count(name) == 1
    assert list(_subparsers()).count(name) == 1


@pytest.mark.parametrize("name", COMMANDS)
def test_no_command_accepts_store_engine_or_binding(name: str) -> None:
    options = _option_strings(_subparsers()[name])
    for forbidden in FORBIDDEN_OPTIONS:
        assert forbidden not in options


@pytest.mark.parametrize("name", ("ingest", "ask", "publish", "status"))
def test_repo_option_defaults_to_the_working_directory(name: str, tmp_path: Path) -> None:
    parser = build_parser()
    extra = {"ingest": ["x.md"], "ask": ["question"]}.get(name, [])
    parsed = parser.parse_args([name, *extra])
    assert Path(parsed.repo).resolve() == Path.cwd().resolve()


def test_analyze_repo_argument_defaults_to_the_working_directory() -> None:
    parsed = build_parser().parse_args(["analyze"])
    assert Path(parsed.repo).resolve() == Path.cwd().resolve()


def test_analyze_without_a_provider_prints_the_documented_json(tmp_path: Path) -> None:
    _repo(tmp_path)
    code, payload = _run("analyze", str(tmp_path))
    assert code == EXIT_BLOCKED
    assert payload["status"] == "blocked"
    assert payload["reason"] == "agent_provider_unavailable"
    assert payload["action"] == "configure a supported provider"
    assert payload["command"] == "analyze"


def test_status_of_a_new_repository_exits_zero(tmp_path: Path) -> None:
    _repo(tmp_path)
    code, payload = _run("status", "--repo", str(tmp_path))
    assert code == EXIT_OK
    assert payload["status"] == "ok"
    assert payload["snapshot_digest"] is None
    assert payload["provider_available"] is False


def test_ask_on_empty_knowledge_exits_two(tmp_path: Path) -> None:
    _repo(tmp_path)
    code, payload = _run("ask", "how does renewal work", "--repo", str(tmp_path))
    assert code == EXIT_BLOCKED
    assert payload["reason"] == "knowledge_empty"
    assert payload["action"] == "run analyze or ingest first"


def test_publish_on_empty_knowledge_exits_two(tmp_path: Path) -> None:
    _repo(tmp_path)
    code, payload = _run("publish", "--repo", str(tmp_path))
    assert code == EXIT_BLOCKED
    assert payload["reason"] == "nothing_to_publish"


def test_ingest_exits_zero_and_registers_the_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    source = tmp_path / "notes.md"
    source.write_text("# renewal\n", encoding="utf-8")
    code, payload = _run("ingest", str(source), "--repo", str(repo))
    assert code == EXIT_OK
    assert payload["status"] == "ok"
    assert payload["registered"] is True
    assert payload["kind"] == "markdown"


def test_ingest_of_a_missing_source_exits_one(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    code, payload = _run("ingest", str(tmp_path / "absent.md"), "--repo", str(repo))
    assert code == 1
    assert payload["status"] == "error"
    assert payload["reason"] == "ingest_failed"


def test_state_layout_is_created_by_the_first_command(tmp_path: Path) -> None:
    _repo(tmp_path)
    _run("analyze", str(tmp_path))
    state = tmp_path / ".wiki-ai"
    assert (state / "format.json").is_file()
    assert (state / "snapshots" / "latest.json").is_file()
    assert (state / "evidence").is_dir()
    assert (state / "publications").is_dir()


def test_blocked_payloads_never_leak_internal_vocabulary(tmp_path: Path) -> None:
    _repo(tmp_path)
    forbidden = ("store", "lease", "binding", "envelope", "objective_id", "revision")
    for argv in (
        ("analyze", str(tmp_path)),
        ("ask", "q", "--repo", str(tmp_path)),
        ("publish", "--repo", str(tmp_path)),
    ):
        _, payload = _run(*argv)
        text = json.dumps(payload).lower()
        for word in forbidden:
            assert word not in text


def test_global_home_relocates_the_state_directory(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    home = tmp_path / "home"
    previous = os.environ.get("WIKI_AI_HOME")
    os.environ["WIKI_AI_HOME"] = str(home)
    try:
        code, payload = _run("status", "--repo", str(repo))
    finally:
        if previous is None:
            os.environ.pop("WIKI_AI_HOME", None)
        else:
            os.environ["WIKI_AI_HOME"] = previous
    assert code == EXIT_OK
    assert payload["status"] == "ok"
    assert not (repo / ".wiki-ai").exists()
    assert any(child.is_dir() for child in home.iterdir())
