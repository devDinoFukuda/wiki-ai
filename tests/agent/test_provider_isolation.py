from __future__ import annotations

import ast
from pathlib import Path

from wiki_ai.quality import architecture, source_hygiene, vocabulary

AGENT_ROOT = Path(architecture.__file__).resolve().parents[1] / "agent"
SRC_ROOT = AGENT_ROOT.parents[1]
ADAPTER_DIR = AGENT_ROOT / "providers"
PROVIDER_NAMES = tuple(term.lower() for term in vocabulary.terms("provider_names"))


def _agent_violations(found) -> list:
    return [item for item in found if "agent" in item.path.replace("\\", "/")]


def test_the_agent_area_passes_the_architecture_gate() -> None:
    assert _agent_violations(architecture.check(SRC_ROOT)) == []


def test_the_agent_area_passes_the_hygiene_gate() -> None:
    assert source_hygiene.scan([AGENT_ROOT]) == []


def _names_in(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8").lower()
    return {name for name in PROVIDER_NAMES if name in text}


def test_no_provider_name_appears_outside_the_adapter_package() -> None:
    offenders: dict[str, set[str]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if ADAPTER_DIR in path.parents:
            continue
        found = _names_in(path)
        if found:
            offenders[path.as_posix()] = found
    assert offenders == {}


def test_the_gate_reports_no_provider_name_outside_the_adapter() -> None:
    rule = architecture.ArchitectureRule.PROVIDER_NAME_OUTSIDE_ADAPTER
    found = [v for v in architecture.check(SRC_ROOT) if v.rule is rule]
    assert found == []


def test_the_adapter_package_is_where_provider_names_live() -> None:
    found: set[str] = set()
    for path in sorted(ADAPTER_DIR.rglob("*.py")):
        found |= _names_in(path)
    assert {"claude", "codex"} <= found


def test_the_core_never_imports_a_concrete_adapter() -> None:
    adapter = vocabulary.text("provider_adapter_package")
    for path in sorted(AGENT_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(adapter + "."), path
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(adapter + "."), path
