from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.repository.fixtures_repos import python_repo, write
from wiki_ai.repository.harness import InvalidToolArguments, RepositoryHarness
from wiki_ai.repository.snapshot import SnapshotSpec, take_snapshot

_PATH_ARGUMENTS = {
    "repo.read": lambda value: {"path": value},
    "repo.symbol": lambda value: {"path": value},
    "repo.references": lambda value: {"symbol_name": "InvoiceService", "origin_path": value},
    "repo.history": lambda value: {"path": value},
    "evidence.capture": lambda value: {"path": value, "line_start": 1, "line_end": 1},
}

_TRAVERSALS = (
    "../secret.txt",
    "../../secret.txt",
    "billing/../../secret.txt",
    "./../secret.txt",
    "..\\secret.txt",
    "/etc/passwd",
    "C:/Windows/win.ini",
)

_FORBIDDEN_MODULES = frozenset({"os", "shutil", "socket", "urllib", "http", "ftplib", "shlex"})
_PRODUCTION_FILES = (
    "symbols.py",
    "references.py",
    "dependencies.py",
    "tests_discovery.py",
    "config.py",
    "evidence.py",
    "harness.py",
    "schemas.py",
    "toolspec.py",
)


def _harness(tmp_path: Path) -> RepositoryHarness:
    repository = tmp_path / "repository"
    repository.mkdir()
    write(tmp_path, "secret.txt", "top secret value\n")
    return RepositoryHarness(python_repo(repository))


@pytest.mark.parametrize("tool", sorted(_PATH_ARGUMENTS))
def test_path_traversal_is_rejected_on_every_path_tool(tmp_path: Path, tool: str) -> None:
    harness = _harness(tmp_path)
    builder = _PATH_ARGUMENTS[tool]
    for candidate in _TRAVERSALS:
        with pytest.raises(InvalidToolArguments):
            harness.invoke(tool, builder(candidate))


@pytest.mark.parametrize("tool", sorted(_PATH_ARGUMENTS))
def test_paths_outside_the_snapshot_are_rejected(tmp_path: Path, tool: str) -> None:
    harness = _harness(tmp_path)
    builder = _PATH_ARGUMENTS[tool]
    for candidate in ("billing/absent.py", "unmapped/file.py", ""):
        with pytest.raises(InvalidToolArguments):
            harness.invoke(tool, builder(candidate))


def test_search_globs_cannot_escape_the_snapshot(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    payload = harness.invoke("repo.search", {"pattern": "top secret", "globs": ["../**"]})
    assert payload["matches"] == []
    every = harness.invoke("repo.search", {"pattern": "top secret"})
    assert every["matches"] == []


def test_excluded_files_stay_invisible_to_every_tool(tmp_path: Path) -> None:
    write(tmp_path, "app/main.py", "SECRET = 'visible'\n")
    write(tmp_path, "app/private.py", "SECRET = 'hidden'\n")
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path, excludes=("app/private.py",)))
    harness = RepositoryHarness(snapshot)
    paths = {entry["path"] for entry in harness.invoke("repo.inventory", {})["entries"]}
    assert paths == {"app/main.py"}
    assert harness.invoke("repo.search", {"pattern": "hidden"})["matches"] == []
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.read", {"path": "app/private.py"})


def test_tools_never_write_to_the_repository(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    before = take_snapshot(SnapshotSpec(root=Path(harness.snapshot.root)))
    harness.invoke("repo.inventory", {})
    harness.invoke("repo.search", {"pattern": "Invoice"})
    harness.invoke("repo.read", {"path": "billing/service.py"})
    harness.invoke("repo.symbol", {})
    harness.invoke("repo.references", {"symbol_name": "InvoiceService"})
    harness.invoke("repo.dependencies", {})
    harness.invoke("repo.tests", {"path_or_symbol": "InvoiceService"})
    harness.invoke("repo.config", {"key_or_usage": "INVOICE_TOPIC"})
    harness.invoke("repo.history", {})
    harness.invoke("evidence.capture", {"path": "billing/service.py", "line_start": 1, "line_end": 1})
    after = take_snapshot(SnapshotSpec(root=Path(harness.snapshot.root)))
    assert before.digest == after.digest


def _module_source(name: str) -> ast.Module:
    path = Path(__file__).resolve().parents[2] / "src" / "wiki_ai" / "repository" / name
    return ast.parse(path.read_text(encoding="utf-8"))


def test_production_modules_import_no_shell_or_network_capability() -> None:
    for name in _PRODUCTION_FILES:
        for node in ast.walk(_module_source(name)):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not (roots & _FORBIDDEN_MODULES), f"{name} imports {roots}"


def test_only_history_uses_subprocess_and_with_fixed_arguments() -> None:
    for name in _PRODUCTION_FILES:
        source = _module_source(name)
        modules = {
            alias.name
            for node in ast.walk(source)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "subprocess" not in modules
    tree = _module_source("history.py")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "run"):
            continue
        assert isinstance(function.value, ast.Name)
        assert function.value.id == "subprocess"
        argument = node.args[0]
        assert isinstance(argument, ast.List)
        assert isinstance(argument.elts[0], ast.Constant)
        assert argument.elts[0].value == "git"
        keywords = {keyword.arg for keyword in node.keywords}
        assert "shell" not in keywords
        assert "timeout" in keywords
