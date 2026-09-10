from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from wiki_ai.quality import vocabulary

__all__ = [
    "SymbolKind",
    "DeadSymbol",
    "PACKAGE_NAME",
    "PUBLIC_API_KEY",
    "ALLOW_KEY",
    "public_api_prefixes",
    "allowances",
    "definitions_of",
    "references_of",
    "check",
]

PACKAGE_NAME = "wiki_ai"
PUBLIC_API_KEY = "public_api"
ALLOW_KEY = "dead_code_allow"

_INIT_STEM = "__init__"
_ALL = "__all__"
_GETATTR = "getattr"
_WILDCARD_SUFFIX = ".*"
_ENUM_BASES = frozenset({"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"})
_PROTOCOL_BASES = frozenset({"Protocol"})
_DATA_DECORATORS = frozenset({"dataclass"})
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


class SymbolKind(Enum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    CONSTANT = "constant"
    ENUM_MEMBER = "enum_member"


@dataclass(frozen=True)
class DeadSymbol:
    path: str
    line: int
    name: str
    kind: SymbolKind

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind.value} {self.name}"


@dataclass(frozen=True)
class Definition:
    module: str
    path: str
    line: int
    name: str
    kind: SymbolKind
    owner: str | None
    start: int
    end: int


def _package_root(src_root: Path) -> Path:
    candidate = src_root / PACKAGE_NAME
    return candidate if candidate.is_dir() else src_root


def _module_name(package_root: Path, path: Path) -> str:
    relative = path.relative_to(package_root).with_suffix("")
    parts = [PACKAGE_NAME, *relative.parts]
    if parts[-1] == _INIT_STEM:
        parts = parts[:-1]
    return ".".join(parts)


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _span(node: ast.AST) -> tuple[int, int]:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", None)
    return start, end if end is not None else start


def _named(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _named(node.value)
    if isinstance(node, ast.Call):
        return _named(node.func)
    return None


def _base_names(node: ast.ClassDef) -> frozenset[str]:
    return frozenset(
        name for name in (_named(base) for base in node.bases) if name is not None
    )


def _decorator_names(node: ast.AST) -> frozenset[str]:
    items = getattr(node, "decorator_list", [])
    return frozenset(
        name for name in (_named(item) for item in items) if name is not None
    )


def _module_level_names(node: ast.AST) -> Iterator[tuple[str, int]]:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                yield target.id, target.lineno
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        yield node.target.id, node.target.lineno


def _class_members(
    node: ast.ClassDef, module: str, relative: str, owner: str
) -> Iterator[Definition]:
    bases = _base_names(node)
    is_enum = bool(bases & _ENUM_BASES)
    is_protocol = bool(bases & _PROTOCOL_BASES)
    is_data = bool(_decorator_names(node) & _DATA_DECORATORS)
    for child in node.body:
        start, end = _span(child)
        if isinstance(child, _FUNCTION_NODES):
            if is_protocol or _is_dunder(child.name):
                continue
            yield Definition(
                module, relative, child.lineno, child.name, SymbolKind.METHOD, owner, start, end
            )
            continue
        if isinstance(child, ast.ClassDef):
            yield Definition(
                module, relative, child.lineno, child.name, SymbolKind.CLASS, owner, start, end
            )
            yield from _class_members(child, module, relative, child.name)
            continue
        if is_data or is_protocol:
            continue
        if isinstance(child, ast.AnnAssign) and child.value is None:
            continue
        for name, lineno in _module_level_names(child):
            if _is_dunder(name) or name == "_":
                continue
            kind = SymbolKind.ENUM_MEMBER if is_enum else SymbolKind.CONSTANT
            yield Definition(module, relative, lineno, name, kind, owner, start, end)


def definitions_of(package_root: Path, path: Path) -> tuple[Definition, ...]:
    tree = _parse(path)
    if tree is None:
        return ()
    module = _module_name(package_root, path)
    relative = path.relative_to(package_root.parent).as_posix()
    found: list[Definition] = []
    for node in tree.body:
        start, end = _span(node)
        if isinstance(node, _FUNCTION_NODES):
            if _is_dunder(node.name):
                continue
            found.append(
                Definition(
                    module, relative, node.lineno, node.name, SymbolKind.FUNCTION, None, start, end
                )
            )
        elif isinstance(node, ast.ClassDef):
            found.append(
                Definition(
                    module, relative, node.lineno, node.name, SymbolKind.CLASS, None, start, end
                )
            )
            found.extend(_class_members(node, module, relative, node.name))
        else:
            for name, lineno in _module_level_names(node):
                if _is_dunder(name) or name == "_":
                    continue
                found.append(
                    Definition(
                        module, relative, lineno, name, SymbolKind.CONSTANT, None, start, end
                    )
                )
    return tuple(found)


def _all_entries(tree: ast.Module) -> Iterator[tuple[str, int]]:
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(item, ast.Name) and item.id == _ALL for item in targets):
            continue
        value = getattr(node, "value", None)
        if not isinstance(value, (ast.List, ast.Tuple)):
            continue
        for element in value.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                yield element.value, element.lineno


def references_of(tree: ast.Module) -> tuple[tuple[str, int], ...]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            found.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            found.append((node.attr, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.rsplit(".", 1)[-1], node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == _GETATTR and len(node.args) > 1:
                literal = node.args[1]
                if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                    found.append((literal.value, node.lineno))
    found.extend(_all_entries(tree))
    return tuple(found)


@dataclass(frozen=True)
class _ClassShape:
    methods: frozenset[str]
    bases: frozenset[str]
    is_enum: bool


def _class_shapes(package_root: Path) -> Mapping[str, _ClassShape]:
    shapes: dict[str, tuple[set[str], set[str], bool]] = {}
    for path in sorted(package_root.rglob("*.py")):
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            methods = {
                child.name for child in node.body if isinstance(child, _FUNCTION_NODES)
            }
            bases = set(_base_names(node))
            entry = shapes.setdefault(node.name, (set(), set(), False))
            entry[0].update(methods)
            entry[1].update(bases)
            shapes[node.name] = (
                entry[0],
                entry[1],
                entry[2] or bool(bases & _ENUM_BASES),
            )
    return {
        name: _ClassShape(frozenset(methods), frozenset(bases), is_enum)
        for name, (methods, bases, is_enum) in shapes.items()
    }


def _overridden_methods(shapes: Mapping[str, _ClassShape]) -> frozenset[str]:
    overridden: set[str] = set()
    for shape in shapes.values():
        for base in shape.bases:
            if base in _PROTOCOL_BASES or base in _ENUM_BASES:
                continue
            inherited = shapes.get(base)
            if inherited is None:
                overridden |= shape.methods
                continue
            overridden |= shape.methods & inherited.methods
    return frozenset(overridden)


def _enum_classes_built_by_value(
    package_root: Path, shapes: Mapping[str, _ClassShape]
) -> frozenset[str]:
    enums = {name for name, shape in shapes.items() if shape.is_enum}
    built: set[str] = set()
    for path in sorted(package_root.rglob("*.py")):
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _named(node.func)
            if name in enums and node.args:
                built.add(name)
    return frozenset(built)


def public_api_prefixes() -> tuple[str, ...]:
    return vocabulary.terms(PUBLIC_API_KEY)


def allowances() -> Mapping[str, str]:
    payload = vocabulary.load()
    entries = payload.get(ALLOW_KEY, ())
    resolved: dict[str, str] = {}
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Iterable):
        return resolved
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        symbol = entry.get("symbol")
        reason = entry.get("reason")
        if isinstance(symbol, str) and isinstance(reason, str) and reason:
            resolved[symbol] = reason
    return resolved


def _matches_public_api(module: str, prefixes: Iterable[str]) -> bool:
    for prefix in prefixes:
        if prefix.endswith(_WILDCARD_SUFFIX):
            stem = prefix[: -len(_WILDCARD_SUFFIX)]
            if module == stem or module.startswith(stem + "."):
                return True
        elif module == prefix:
            return True
    return False


def _external_uses(
    definition: Definition, occurrences: Mapping[str, tuple[tuple[str, int], ...]]
) -> bool:
    for path, entries in occurrences.items():
        for name, line in entries:
            if name != definition.name:
                continue
            if path == definition.path and definition.start <= line <= definition.end:
                continue
            return True
    return False


def check(src_root: Path, tests_root: Path | None = None) -> tuple[DeadSymbol, ...]:
    package_root = _package_root(Path(src_root))
    shapes = _class_shapes(package_root)
    overridden = _overridden_methods(shapes)
    by_value = _enum_classes_built_by_value(package_root, shapes)
    prefixes = public_api_prefixes()
    exceptions = allowances()
    definitions: list[Definition] = []
    production: dict[str, tuple[tuple[str, int], ...]] = {}
    for path in sorted(package_root.rglob("*.py")):
        tree = _parse(path)
        if tree is None:
            continue
        relative = path.relative_to(package_root.parent).as_posix()
        production[relative] = references_of(tree)
        definitions.extend(definitions_of(package_root, path))
    test_names: set[str] = set()
    if tests_root is not None and Path(tests_root).is_dir():
        for path in sorted(Path(tests_root).rglob("*.py")):
            tree = _parse(path)
            if tree is None:
                continue
            test_names |= {name for name, _ in references_of(tree)}
    dead: list[DeadSymbol] = []
    for definition in definitions:
        if f"{definition.module}:{definition.name}" in exceptions:
            continue
        if definition.kind is SymbolKind.METHOD and definition.name in overridden:
            continue
        if definition.kind is SymbolKind.ENUM_MEMBER and definition.owner in by_value:
            continue
        if _external_uses(definition, production):
            continue
        if definition.name in test_names and _matches_public_api(
            definition.module, prefixes
        ):
            continue
        dead.append(
            DeadSymbol(
                definition.path, definition.line, definition.name, definition.kind
            )
        )
    return tuple(sorted(dead, key=lambda item: (item.path, item.line, item.name)))
