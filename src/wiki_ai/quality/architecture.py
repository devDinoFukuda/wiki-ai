from __future__ import annotations

import ast
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from wiki_ai.quality import vocabulary

__all__ = [
    "ArchitectureRule",
    "ArchitectureViolation",
    "ModuleFile",
    "PACKAGE_NAME",
    "EXTERNAL_TOKENS",
    "check",
]

PACKAGE_NAME = "wiki_ai"
_WILDCARD = "*"
_INIT_STEM = "__init__"
_ENTRYPOINT_STEM = "__main__"
_SUBPARSER_FACTORY = "add_parser"

EXTERNAL_TOKENS = frozenset(vocabulary.terms("external_tokens"))


class ArchitectureRule(Enum):
    FILE_TOO_LONG = "file_too_long"
    IMPORT_CYCLE = "import_cycle"
    FORBIDDEN_DIRECTION = "forbidden_direction"
    CONCRETE_PROVIDER_IN_CORE = "concrete_provider_in_core"
    BANNED_TERM = "banned_term"
    PROVIDER_NAME_OUTSIDE_ADAPTER = "provider_name_outside_adapter"
    REMOVED_PACKAGE = "removed_package"
    REMOVED_COMMAND = "removed_command"
    MULTIPLE_ENTRYPOINTS = "multiple_entrypoints"
    UNPARSABLE = "unparsable"


@dataclass(frozen=True)
class ArchitectureViolation:
    rule: ArchitectureRule
    path: str
    detail: str


@dataclass(frozen=True)
class ModuleFile:
    module: str
    path: Path
    relative: str
    area: str | None
    lines: int
    is_package: bool


def _package_root(src_root: Path) -> Path:
    candidate = src_root / PACKAGE_NAME
    return candidate if candidate.is_dir() else src_root


def _areas(package_root: Path) -> frozenset[str]:
    return frozenset(
        entry.name
        for entry in package_root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    )


def _module_name(package_root: Path, path: Path) -> str:
    relative = path.relative_to(package_root).with_suffix("")
    parts = [PACKAGE_NAME, *relative.parts]
    if parts[-1] == _INIT_STEM:
        parts = parts[:-1]
    return ".".join(parts)


def _source_area(package_root: Path, path: Path, areas: frozenset[str]) -> str | None:
    parts = path.relative_to(package_root).parts
    if len(parts) < 2:
        if path.stem == _ENTRYPOINT_STEM:
            return vocabulary.text("application_area")
        return None
    return parts[0] if parts[0] in areas else None


def _target_area(module: str, areas: frozenset[str]) -> str | None:
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != PACKAGE_NAME:
        return None
    return parts[1] if parts[1] in areas else None


def _python_files(package_root: Path) -> Iterator[Path]:
    yield from sorted(package_root.rglob("*.py"))


def collect_modules(src_root: Path) -> tuple[list[ModuleFile], dict[str, ast.Module], list[ArchitectureViolation]]:
    package_root = _package_root(src_root)
    areas = _areas(package_root)
    modules: list[ModuleFile] = []
    trees: dict[str, ast.Module] = {}
    problems: list[ArchitectureViolation] = []
    for path in _python_files(package_root):
        relative = path.relative_to(package_root.parent).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(ArchitectureViolation(ArchitectureRule.UNPARSABLE, relative, str(exc)))
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            problems.append(ArchitectureViolation(ArchitectureRule.UNPARSABLE, relative, str(exc)))
            continue
        module = _module_name(package_root, path)
        modules.append(
            ModuleFile(
                module=module,
                path=path,
                relative=relative,
                area=_source_area(package_root, path, areas),
                lines=len(source.splitlines()),
                is_package=path.stem == _INIT_STEM,
            )
        )
        trees[module] = tree
    return modules, trees, problems


def _imported_modules(module: str, tree: ast.Module, is_package: bool) -> set[str]:
    parts = module.split(".")
    package = parts if is_package else parts[:-1]
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = ".".join(package[: len(package) - node.level + 1])
            if not node.module:
                for alias in node.names:
                    targets.add(f"{base}.{alias.name}")
                continue
            prefix = f"{base}.{node.module}"
        else:
            prefix = node.module or ""
        if not prefix:
            continue
        targets.add(prefix)
        for alias in node.names:
            targets.add(f"{prefix}.{alias.name}")
    return {target for target in targets if target}


def _internal_graph(
    modules: Iterable[ModuleFile], trees: Mapping[str, ast.Module]
) -> dict[str, set[str]]:
    known = {item.module for item in modules}
    graph: dict[str, set[str]] = {name: set() for name in known}
    for item in modules:
        for target in _imported_modules(item.module, trees[item.module], item.is_package):
            resolved = target
            while resolved and resolved not in known:
                resolved = resolved.rpartition(".")[0]
            if resolved and resolved != item.module:
                graph[item.module].add(resolved)
    return graph


def _strongly_connected(graph: Mapping[str, set[str]]) -> list[tuple[str, ...]]:
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    components: list[tuple[str, ...]] = []
    counter = 0
    for start in sorted(graph):
        if start in index_of:
            continue
        index_of[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack[start] = True
        work: list[tuple[str, Iterator[str]]] = [(start, iter(sorted(graph[start])))]
        while work:
            node, children = work[-1]
            descended = False
            for child in children:
                if child not in graph:
                    continue
                if child not in index_of:
                    index_of[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack[child] = True
                    work.append((child, iter(sorted(graph[child]))))
                    descended = True
                    break
                if on_stack.get(child):
                    low[node] = min(low[node], index_of[child])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component: list[str] = []
                while True:
                    top = stack.pop()
                    on_stack[top] = False
                    component.append(top)
                    if top == node:
                        break
                components.append(tuple(sorted(component)))
    return components


def _check_file_length(modules: Iterable[ModuleFile]) -> list[ArchitectureViolation]:
    limit = vocabulary.number("max_production_file_lines")
    return [
        ArchitectureViolation(
            ArchitectureRule.FILE_TOO_LONG,
            item.relative,
            f"{item.lines} lines exceed the limit of {limit}",
        )
        for item in modules
        if item.lines > limit
    ]


def _check_cycles(graph: Mapping[str, set[str]]) -> list[ArchitectureViolation]:
    return [
        ArchitectureViolation(
            ArchitectureRule.IMPORT_CYCLE,
            component[0].replace(".", "/") + ".py",
            " -> ".join(component),
        )
        for component in _strongly_connected(graph)
        if len(component) > 1
    ]


def _check_direction(
    modules: Iterable[ModuleFile], graph: Mapping[str, set[str]], areas: frozenset[str]
) -> list[ArchitectureViolation]:
    layers = vocabulary.mapping("layers")
    application = vocabulary.text("application_area")
    violations: list[ArchitectureViolation] = []
    for item in modules:
        source_area = item.area
        for target in sorted(graph[item.module]):
            target_area = _target_area(target, areas)
            if target_area is None or target_area == source_area:
                continue
            if target_area == application and source_area != application:
                violations.append(
                    ArchitectureViolation(
                        ArchitectureRule.FORBIDDEN_DIRECTION,
                        item.relative,
                        f"{source_area} imports {target}",
                    )
                )
                continue
            if source_area is None:
                continue
            allowed = layers.get(source_area, ())
            if _WILDCARD in allowed or target_area in allowed:
                continue
            violations.append(
                ArchitectureViolation(
                    ArchitectureRule.FORBIDDEN_DIRECTION,
                    item.relative,
                    f"{source_area} imports {target}",
                )
            )
    return violations


def _check_provider_isolation(
    modules: Iterable[ModuleFile], graph: Mapping[str, set[str]]
) -> list[ArchitectureViolation]:
    adapter = vocabulary.text("provider_adapter_package")
    violations: list[ArchitectureViolation] = []
    for item in modules:
        if item.module == adapter or item.module.startswith(adapter + "."):
            continue
        for target in sorted(graph[item.module]):
            if target.startswith(adapter + "."):
                violations.append(
                    ArchitectureViolation(
                        ArchitectureRule.CONCRETE_PROVIDER_IN_CORE,
                        item.relative,
                        f"{item.module} imports {target}",
                    )
                )
    return violations


def _identifiers_and_strings(tree: ast.Module) -> Iterator[tuple[int, str, bool]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.lineno, node.id, False
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr, False
        elif isinstance(node, ast.arg):
            yield node.lineno, node.arg, False
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.lineno, node.name, False
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value, True
        elif isinstance(node, ast.alias):
            yield 0, node.name, False


def _without_external_tokens(value: str) -> str:
    remainder = value
    for token in sorted(EXTERNAL_TOKENS, key=len, reverse=True):
        remainder = remainder.replace(token.lower(), " ")
    return remainder


def _check_terms(
    modules: Iterable[ModuleFile], trees: Mapping[str, ast.Module]
) -> list[ArchitectureViolation]:
    banned = tuple(term.lower() for term in vocabulary.terms("banned_terms"))
    providers = tuple(term.lower() for term in vocabulary.terms("provider_names"))
    adapter = vocabulary.text("provider_adapter_package")
    violations: list[ArchitectureViolation] = []
    for item in modules:
        inside_adapter = item.module == adapter or item.module.startswith(adapter + ".")
        for lineno, value, is_string in _identifiers_and_strings(trees[item.module]):
            lowered = value.lower()
            searchable = _without_external_tokens(lowered) if is_string else lowered
            for term in banned:
                if term in searchable:
                    violations.append(
                        ArchitectureViolation(
                            ArchitectureRule.BANNED_TERM,
                            item.relative,
                            f"line {lineno}: {term}",
                        )
                    )
            if inside_adapter:
                continue
            for name in providers:
                if name in lowered:
                    violations.append(
                        ArchitectureViolation(
                            ArchitectureRule.PROVIDER_NAME_OUTSIDE_ADAPTER,
                            item.relative,
                            f"line {lineno}: {name}",
                        )
                    )
    return violations


def _check_removed_packages(repo_root: Path) -> list[ArchitectureViolation]:
    violations: list[ArchitectureViolation] = []
    for relative in vocabulary.terms("removed_packages"):
        candidate = repo_root / relative
        if candidate.exists():
            violations.append(
                ArchitectureViolation(
                    ArchitectureRule.REMOVED_PACKAGE,
                    relative,
                    "removed package is still present in the repository",
                )
            )
    return violations


def _registered_commands(
    modules: Iterable[ModuleFile], trees: Mapping[str, ast.Module]
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for item in modules:
        for node in ast.walk(trees[item.module]):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != _SUBPARSER_FACTORY:
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    found.append((item.relative, value))
    return found


def _console_scripts(repo_root: Path) -> tuple[dict[str, str], str | None]:
    manifest = repo_root / "pyproject.toml"
    if not manifest.is_file():
        return {}, f"{manifest.name} not found at {repo_root}"
    try:
        payload = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, str(exc)
    project = payload.get("project", {})
    scripts = project.get("scripts", {}) if isinstance(project, Mapping) else {}
    if not isinstance(scripts, Mapping):
        return {}, "project.scripts must be a table"
    return {str(k): str(v) for k, v in scripts.items()}, None


def _check_removed_commands(
    repo_root: Path, modules: Iterable[ModuleFile], trees: Mapping[str, ast.Module]
) -> list[ArchitectureViolation]:
    removed = frozenset(vocabulary.terms("removed_commands"))
    violations: list[ArchitectureViolation] = []
    for relative, name in _registered_commands(modules, trees):
        if name in removed:
            violations.append(
                ArchitectureViolation(
                    ArchitectureRule.REMOVED_COMMAND,
                    relative,
                    f"removed command is registered: {name}",
                )
            )
    scripts, failure = _console_scripts(repo_root)
    if failure is None:
        for name in sorted(scripts):
            if name in removed:
                violations.append(
                    ArchitectureViolation(
                        ArchitectureRule.REMOVED_COMMAND,
                        "pyproject.toml",
                        f"removed command is registered: {name}",
                    )
                )
    return violations


def _check_entrypoints(repo_root: Path, src_root: Path) -> list[ArchitectureViolation]:
    package_root = _package_root(src_root)
    violations: list[ArchitectureViolation] = []
    allowed = frozenset(vocabulary.terms("allowed_console_scripts"))
    scripts, failure = _console_scripts(repo_root)
    if failure is not None:
        violations.append(
            ArchitectureViolation(
                ArchitectureRule.MULTIPLE_ENTRYPOINTS, "pyproject.toml", failure
            )
        )
    else:
        for name in sorted(scripts):
            if name not in allowed:
                violations.append(
                    ArchitectureViolation(
                        ArchitectureRule.MULTIPLE_ENTRYPOINTS,
                        "pyproject.toml",
                        f"unexpected console script: {name}",
                    )
                )
        if len(scripts) > 1:
            violations.append(
                ArchitectureViolation(
                    ArchitectureRule.MULTIPLE_ENTRYPOINTS,
                    "pyproject.toml",
                    f"{len(scripts)} console scripts declared",
                )
            )
    expected = package_root / f"{_ENTRYPOINT_STEM}.py"
    for path in sorted(package_root.rglob(f"{_ENTRYPOINT_STEM}.py")):
        if path != expected:
            violations.append(
                ArchitectureViolation(
                    ArchitectureRule.MULTIPLE_ENTRYPOINTS,
                    path.relative_to(package_root.parent).as_posix(),
                    "additional application entrypoint module",
                )
            )
    return violations


def check(src_root: Path, repo_root: Path | None = None) -> list[ArchitectureViolation]:
    src_root = Path(src_root)
    resolved_repo_root = Path(repo_root) if repo_root is not None else src_root.parent
    modules, trees, violations = collect_modules(src_root)
    package_root = _package_root(src_root)
    areas = _areas(package_root)
    graph = _internal_graph(modules, trees)
    violations.extend(_check_file_length(modules))
    violations.extend(_check_cycles(graph))
    violations.extend(_check_direction(modules, graph, areas))
    violations.extend(_check_provider_isolation(modules, graph))
    violations.extend(_check_terms(modules, trees))
    violations.extend(_check_removed_packages(resolved_repo_root))
    violations.extend(_check_removed_commands(resolved_repo_root, modules, trees))
    violations.extend(_check_entrypoints(resolved_repo_root, src_root))
    return sorted(violations, key=lambda v: (v.rule.value, v.path, v.detail))
