from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = "wiki_ai"
ENTRYPOINTS: tuple[str, ...] = (
    "wiki_ai.app.commands",
    "wiki_ai.agent.providers",
    "wiki_ai.__main__",
)
PUBLIC_API: tuple[str, ...] = (
    "wiki_ai.quality",
    "wiki_ai.quality.architecture",
    "wiki_ai.quality.contracts",
    "wiki_ai.quality.source_hygiene",
    "wiki_ai.quality.vocabulary",
)


def source_root() -> Path:
    return Path(__file__).resolve().parents[2] / "src"


def modules_of(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(root.rglob("*.py")):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found[".".join(parts)] = path
    return found


def _anchor(name: str, path: Path, level: int) -> str:
    package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
    parts = package.split(".")
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    return ".".join(parts)


def _prefixes(text: object, found: set[str]) -> None:
    if not isinstance(text, str) or not text.startswith(PACKAGE):
        return
    parts = text.split(".")
    for size in range(1, len(parts) + 1):
        found.add(".".join(parts[:size]))


def references(name: str, path: Path, modules: dict[str, Path]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _prefixes(alias.name, found)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = _anchor(name, path, node.level)
                base = f"{anchor}.{base}" if base else anchor
            _prefixes(base, found)
            for alias in node.names:
                _prefixes(f"{base}.{alias.name}" if base else alias.name, found)
        elif isinstance(node, ast.Constant):
            _prefixes(node.value, found)
    return {item for item in found if item in modules}


def reachable(root: Path, entrypoints: tuple[str, ...] = ENTRYPOINTS) -> set[str]:
    modules = modules_of(root)
    seen: set[str] = set()
    frontier = list(entrypoints)
    while frontier:
        current = frontier.pop()
        if current in seen or current not in modules:
            continue
        seen.add(current)
        frontier.extend(references(current, modules[current], modules))
    return seen


def unreachable(root: Path) -> tuple[str, ...]:
    modules = set(modules_of(root))
    return tuple(sorted(modules - reachable(root) - set(PUBLIC_API)))
