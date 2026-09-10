from __future__ import annotations

from pathlib import Path

from wiki_ai.quality import vocabulary
from wiki_ai.quality.architecture import EXTERNAL_TOKENS, ArchitectureRule, check

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


def _violations(rule: ArchitectureRule, src_root: Path = SRC_ROOT, repo_root: Path = REPO_ROOT):
    return [item for item in check(src_root, repo_root) if item.rule is rule]


def _format(violations) -> str:
    return "\n".join(f"{item.path}: {item.detail}" for item in violations)


def _make_package(root: Path, modules: dict[str, str]) -> Path:
    src_root = root / "src"
    package = src_root / "wiki_ai"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0"\n', encoding="utf-8")
    for relative, body in modules.items():
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        marker = target.parent / "__init__.py"
        if not marker.exists() and target.parent != package:
            marker.write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[project.scripts]\nwiki-ai = "wiki_ai.app.commands:main"\n',
        encoding="utf-8",
    )
    return src_root


def test_import_graph_has_no_cycle() -> None:
    found = _violations(ArchitectureRule.IMPORT_CYCLE)
    assert not found, _format(found)


def test_only_one_application_entrypoint() -> None:
    found = _violations(ArchitectureRule.MULTIPLE_ENTRYPOINTS)
    assert not found, _format(found)


def test_no_removed_packages() -> None:
    found = _violations(ArchitectureRule.REMOVED_PACKAGE)
    assert not found, _format(found)


def test_no_removed_commands() -> None:
    found = _violations(ArchitectureRule.REMOVED_COMMAND)
    assert not found, _format(found)


def test_no_forbidden_import_direction() -> None:
    found = _violations(ArchitectureRule.FORBIDDEN_DIRECTION)
    assert not found, _format(found)


def test_no_concrete_provider_in_core() -> None:
    found = _violations(ArchitectureRule.CONCRETE_PROVIDER_IN_CORE)
    assert not found, _format(found)


def test_no_banned_terms() -> None:
    found = _violations(ArchitectureRule.BANNED_TERM)
    assert not found, _format(found)


def test_no_provider_names_outside_adapter() -> None:
    found = _violations(ArchitectureRule.PROVIDER_NAME_OUTSIDE_ADAPTER)
    assert not found, _format(found)


def test_no_oversized_production_file() -> None:
    found = _violations(ArchitectureRule.FILE_TOO_LONG)
    assert not found, _format(found)


def test_no_unparsable_production_file() -> None:
    found = _violations(ArchitectureRule.UNPARSABLE)
    assert not found, _format(found)


def test_check_detects_import_cycle(tmp_path: Path) -> None:
    src_root = _make_package(
        tmp_path,
        {
            "repository/a.py": "from wiki_ai.repository import b\n\nvalue = b\n",
            "repository/b.py": "from wiki_ai.repository import a\n\nvalue = a\n",
        },
    )
    found = _violations(ArchitectureRule.IMPORT_CYCLE, src_root, tmp_path)
    assert found


def test_check_detects_forbidden_direction(tmp_path: Path) -> None:
    src_root = _make_package(
        tmp_path,
        {
            "knowledge/model.py": "from wiki_ai.publishing import render\n\nvalue = render\n",
            "publishing/render.py": "value = 1\n",
        },
    )
    found = _violations(ArchitectureRule.FORBIDDEN_DIRECTION, src_root, tmp_path)
    assert found


def test_check_detects_import_of_application_layer(tmp_path: Path) -> None:
    src_root = _make_package(
        tmp_path,
        {
            "repository/reader.py": "from wiki_ai.app import api\n\nvalue = api\n",
            "app/api.py": "value = 1\n",
        },
    )
    found = _violations(ArchitectureRule.FORBIDDEN_DIRECTION, src_root, tmp_path)
    assert found


def test_check_detects_concrete_provider_import(tmp_path: Path) -> None:
    src_root = _make_package(
        tmp_path,
        {
            "investigation/run.py": "from wiki_ai.agent.providers import shell\n\nvalue = shell\n",
            "agent/providers/shell.py": "value = 1\n",
        },
    )
    found = _violations(ArchitectureRule.CONCRETE_PROVIDER_IN_CORE, src_root, tmp_path)
    assert found


def test_check_detects_banned_term(tmp_path: Path) -> None:
    term = vocabulary.terms("banned_terms")[0]
    src_root = _make_package(tmp_path, {"knowledge/model.py": f"{term}_flag = True\n"})
    found = _violations(ArchitectureRule.BANNED_TERM, src_root, tmp_path)
    assert found


def test_external_ooxml_tokens_are_allowed(tmp_path: Path) -> None:
    lines = "".join(f'element_{index} = "{token}"\n' for index, token in enumerate(sorted(EXTERNAL_TOKENS)))
    src_root = _make_package(tmp_path, {"publishing/parts.py": lines})
    found = _violations(ArchitectureRule.BANNED_TERM, src_root, tmp_path)
    assert not found, _format(found)


def test_identifier_named_compat_is_rejected(tmp_path: Path) -> None:
    identifier = next(token for token in sorted(EXTERNAL_TOKENS) if token.isidentifier())
    src_root = _make_package(tmp_path, {"publishing/parts.py": f"{identifier} = True\n"})
    found = _violations(ArchitectureRule.BANNED_TERM, src_root, tmp_path)
    assert found


def test_check_detects_provider_name_outside_adapter(tmp_path: Path) -> None:
    name = vocabulary.terms("provider_names")[0]
    src_root = _make_package(
        tmp_path,
        {
            "knowledge/model.py": f'engine = "{name}"\n',
            "agent/providers/shell.py": f'engine = "{name}"\n',
        },
    )
    found = _violations(ArchitectureRule.PROVIDER_NAME_OUTSIDE_ADAPTER, src_root, tmp_path)
    assert [item.path for item in found] == ["wiki_ai/knowledge/model.py"]


def test_check_detects_oversized_file(tmp_path: Path) -> None:
    limit = vocabulary.number("max_production_file_lines")
    body = "value = 1\n" * (limit + 1)
    src_root = _make_package(tmp_path, {"knowledge/model.py": body})
    found = _violations(ArchitectureRule.FILE_TOO_LONG, src_root, tmp_path)
    assert found


def test_check_detects_removed_command(tmp_path: Path) -> None:
    command = vocabulary.terms("removed_commands")[0]
    body = (
        "import argparse\n\n\n"
        "def build() -> argparse.ArgumentParser:\n"
        "    parser = argparse.ArgumentParser()\n"
        "    subparsers = parser.add_subparsers()\n"
        f'    subparsers.add_parser("{command}")\n'
        "    return parser\n"
    )
    src_root = _make_package(tmp_path, {"app/commands.py": body})
    found = _violations(ArchitectureRule.REMOVED_COMMAND, src_root, tmp_path)
    assert found


def test_check_detects_removed_package(tmp_path: Path) -> None:
    src_root = _make_package(tmp_path, {"knowledge/model.py": "value = 1\n"})
    (tmp_path / vocabulary.terms("removed_packages")[0]).mkdir(parents=True, exist_ok=True)
    found = _violations(ArchitectureRule.REMOVED_PACKAGE, src_root, tmp_path)
    assert found


def test_check_detects_extra_entrypoint(tmp_path: Path) -> None:
    src_root = _make_package(
        tmp_path,
        {
            "knowledge/model.py": "value = 1\n",
            "knowledge/__main__.py": "value = 1\n",
        },
    )
    found = _violations(ArchitectureRule.MULTIPLE_ENTRYPOINTS, src_root, tmp_path)
    assert found
