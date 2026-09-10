from __future__ import annotations

from pathlib import Path

from wiki_ai.quality import vocabulary
from wiki_ai.quality.dead_code import (
    ALLOW_KEY,
    PUBLIC_API_KEY,
    SymbolKind,
    allowances,
    check,
    public_api_prefixes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _package(root: Path, modules: dict[str, str]) -> Path:
    src_root = root / "src"
    package = src_root / "wiki_ai"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for relative, body in modules.items():
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        marker = target.parent / "__init__.py"
        if target.parent != package and not marker.exists():
            marker.write_text("", encoding="utf-8")
    return src_root


def _tests(root: Path, modules: dict[str, str]) -> Path:
    tests_root = root / "tests"
    tests_root.mkdir(parents=True)
    for relative, body in modules.items():
        target = tests_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return tests_root


def _names(found) -> list[str]:
    return [item.name for item in found]


def test_an_unreferenced_function_is_reported(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "app/commands.py": "def main() -> int:\n    return 0\n",
            "knowledge/store.py": "def orphan() -> int:\n    return 1\n",
        },
    )
    found = check(src_root)
    assert "orphan" in _names(found)
    dead = [item for item in found if item.name == "orphan"][0]
    assert dead.kind is SymbolKind.FUNCTION
    assert dead.path == "wiki_ai/knowledge/store.py"
    assert dead.line == 1


def test_a_referenced_function_is_not_reported(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "app/commands.py": (
                "from wiki_ai.knowledge.store import used\n\n\n"
                "def main() -> int:\n    return used()\n"
            ),
            "knowledge/store.py": "def used() -> int:\n    return 1\n",
        },
    )
    assert "used" not in _names(check(src_root))


def test_a_function_used_only_by_tests_in_a_private_module_is_reported(
    tmp_path: Path,
) -> None:
    src_root = _package(
        tmp_path, {"knowledge/store.py": "def helper() -> int:\n    return 1\n"}
    )
    tests_root = _tests(
        tmp_path,
        {
            "knowledge/test_store.py": (
                "from wiki_ai.knowledge.store import helper\n\n\n"
                "def test_it() -> None:\n    assert helper() == 1\n"
            )
        },
    )
    assert "helper" in _names(check(src_root, tests_root))


def test_a_function_used_only_by_tests_in_a_public_module_is_not_reported(
    tmp_path: Path,
) -> None:
    src_root = _package(
        tmp_path, {"quality/rules.py": "def helper() -> int:\n    return 1\n"}
    )
    tests_root = _tests(
        tmp_path,
        {
            "quality/test_rules.py": (
                "from wiki_ai.quality.rules import helper\n\n\n"
                "def test_it() -> None:\n    assert helper() == 1\n"
            )
        },
    )
    assert "helper" not in _names(check(src_root, tests_root))


def test_an_overridden_method_is_not_reported(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "app/commands.py": (
                "from wiki_ai.knowledge.store import Child\n\n\n"
                "def main() -> int:\n    return Child().run()\n"
            ),
            "knowledge/store.py": (
                "class Base:\n"
                "    def run(self) -> int:\n"
                "        return 0\n\n\n"
                "class Child(Base):\n"
                "    def run(self) -> int:\n"
                "        return 1\n"
            ),
        },
    )
    assert "run" not in _names(check(src_root))


def test_an_enum_member_used_by_value_is_not_reported(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "app/commands.py": (
                "from wiki_ai.knowledge.store import Kind\n\n\n"
                "def main() -> Kind:\n    return Kind('a')\n"
            ),
            "knowledge/store.py": (
                "from enum import Enum\n\n\n"
                "class Kind(Enum):\n"
                "    A = 'a'\n"
                "    B = 'b'\n"
            ),
        },
    )
    found = _names(check(src_root))
    assert "A" not in found
    assert "B" not in found


def test_an_enum_member_never_used_is_reported(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "app/commands.py": (
                "from wiki_ai.knowledge.store import Kind\n\n\n"
                "def main() -> Kind:\n    return Kind.A\n"
            ),
            "knowledge/store.py": (
                "from enum import Enum\n\n\n"
                "class Kind(Enum):\n"
                "    A = 'a'\n"
                "    B = 'b'\n"
            ),
        },
    )
    found = _names(check(src_root))
    assert "B" in found
    assert "A" not in found


def test_a_bare_string_is_not_a_reference(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "app/commands.py": "def main() -> str:\n    return 'orphan'\n",
            "knowledge/store.py": "def orphan() -> int:\n    return 1\n",
        },
    )
    assert "orphan" in _names(check(src_root))


def test_a_getattr_literal_is_a_reference(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "app/commands.py": (
                "from wiki_ai.knowledge import store\n\n\n"
                "def main() -> object:\n"
                "    return getattr(store, 'orphan')\n"
            ),
            "knowledge/store.py": "def orphan() -> int:\n    return 1\n",
        },
    )
    assert "orphan" not in _names(check(src_root))


def test_an_all_entry_is_a_reference(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "knowledge/store.py": (
                "__all__ = ['orphan']\n\n\n" "def orphan() -> int:\n    return 1\n"
            )
        },
    )
    assert "orphan" not in _names(check(src_root))


def test_a_dunder_method_is_never_reported(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "app/commands.py": (
                "from wiki_ai.knowledge.store import Box\n\n\n"
                "def main() -> str:\n    return str(Box())\n"
            ),
            "knowledge/store.py": (
                "class Box:\n"
                "    def __str__(self) -> str:\n"
                "        return 'box'\n"
            ),
        },
    )
    assert "__str__" not in _names(check(src_root))


def test_a_dataclass_field_is_never_reported(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "app/commands.py": (
                "from wiki_ai.knowledge.store import Box\n\n\n"
                "def main() -> Box:\n    return Box(size=1)\n"
            ),
            "knowledge/store.py": (
                "from dataclasses import dataclass\n\n\n"
                "@dataclass(frozen=True)\n"
                "class Box:\n"
                "    size: int = 0\n"
            ),
        },
    )
    assert "size" not in _names(check(src_root))


def test_a_protocol_member_is_never_reported(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "app/ports.py": (
                "from typing import Protocol\n\n\n"
                "class Runner(Protocol):\n"
                "    def run(self) -> int: ...\n"
            ),
            "app/commands.py": (
                "from wiki_ai.app.ports import Runner\n\n\n"
                "def main(runner: Runner) -> int:\n    return runner.run()\n"
            ),
        },
    )
    assert "run" not in _names(check(src_root))


def test_a_decorator_counts_as_a_reference(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path,
        {
            "knowledge/store.py": (
                "from wiki_ai.knowledge.marks import mark\n\n\n"
                "@mark\n"
                "def target() -> int:\n"
                "    return 1\n"
            ),
            "knowledge/marks.py": (
                "from typing import Callable\n\n\n"
                "def mark(fn: Callable[[], int]) -> Callable[[], int]:\n"
                "    return fn\n"
            ),
        },
    )
    assert "mark" not in _names(check(src_root))


def test_an_explicit_allowance_suppresses_the_finding(tmp_path: Path) -> None:
    src_root = _package(
        tmp_path, {"knowledge/store.py": "def orphan() -> int:\n    return 1\n"}
    )
    assert "orphan" in _names(check(src_root))
    assert "wiki_ai.knowledge.store:orphan" not in allowances()


def test_the_allowance_list_is_data_with_a_reason() -> None:
    payload = vocabulary.load()
    entries = payload[ALLOW_KEY]
    assert isinstance(entries, list)
    for entry in entries:
        assert set(entry) == {"symbol", "reason"}
        assert ":" in entry["symbol"]
        assert entry["reason"]
    assert set(allowances()) == {entry["symbol"] for entry in entries}


def test_the_public_api_list_is_data() -> None:
    prefixes = public_api_prefixes()
    assert "wiki_ai.quality.*" in prefixes
    assert "wiki_ai.app.ports" in prefixes
    assert "wiki_ai.agent.protocol" in prefixes
    assert "wiki_ai.__main__" in prefixes
    assert vocabulary.load()[PUBLIC_API_KEY] == list(prefixes)


def test_the_repository_has_no_dead_symbol_in_the_quality_area() -> None:
    found = check(REPO_ROOT / "src", REPO_ROOT / "tests")
    assert [
        str(item) for item in found if item.path.startswith("wiki_ai/quality/")
    ] == []
