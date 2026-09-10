from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from wiki_ai.repository.inventory import language_hint_for
from wiki_ai.repository.reader import resolve_within
from wiki_ai.repository.snapshot import RepositorySnapshot, normalize_path

__all__ = [
    "SymbolKind",
    "Confidence",
    "SymbolInfo",
    "DEFAULT_MAX_SYMBOLS",
    "DEFAULT_MAX_FILE_BYTES",
    "find_symbols",
]

DEFAULT_MAX_SYMBOLS = 500
DEFAULT_MAX_FILE_BYTES = 1048576


class SymbolKind(Enum):
    MODULE = "module"
    CLASS = "class"
    INTERFACE = "interface"
    FUNCTION = "function"
    METHOD = "method"
    PROGRAM = "program"
    SECTION = "section"
    PARAGRAPH = "paragraph"
    STEP = "step"
    TABLE = "table"
    PROCEDURE = "procedure"
    VARIABLE = "variable"
    CALL = "call"
    UNKNOWN = "unknown"


class Confidence(Enum):
    PARSED = "parsed"
    HEURISTIC = "heuristic"


@dataclass(frozen=True)
class SymbolInfo:
    path: str
    name: str
    kind: SymbolKind
    line_start: int
    line_end: int | None
    language_hint: str
    confidence: Confidence

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "name": self.name,
            "kind": self.kind.value,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "language_hint": self.language_hint,
            "confidence": self.confidence.value,
        }


@dataclass(frozen=True)
class _Rule:
    expression: re.Pattern[str]
    kind: SymbolKind


def _rule(pattern: str, kind: SymbolKind, flags: int = 0) -> _Rule:
    return _Rule(expression=re.compile(pattern, flags), kind=kind)


_IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
_COBOL_IDENT = r"[A-Za-z0-9][A-Za-z0-9_-]*"

_PYTHON_RULES = (
    _rule(rf"^\s*class\s+(?P<name>{_IDENT})", SymbolKind.CLASS),
    _rule(rf"^\s*(?:async\s+)?def\s+(?P<name>{_IDENT})", SymbolKind.FUNCTION),
)

_JVM_RULES = (
    _rule(
        rf"^\s*(?:[\w@\[\]<>,\s]*?)\binterface\s+(?P<name>{_IDENT})",
        SymbolKind.INTERFACE,
    ),
    _rule(
        rf"^\s*(?:[\w@\[\]<>,\s]*?)\b(?:class|enum|record|object|struct)\s+(?P<name>{_IDENT})",
        SymbolKind.CLASS,
    ),
    _rule(rf"^\s*(?:\w[\w<>\[\].,\s]*\s+)?fun\s+(?P<name>{_IDENT})\s*\(", SymbolKind.METHOD),
    _rule(
        rf"^\s+(?:public|private|protected|internal|static|final|abstract|synchronized|override|virtual|async|native)"
        rf"(?:\s+\w[\w<>\[\].,?\s]*)*\s+(?P<name>{_IDENT})\s*\(",
        SymbolKind.METHOD,
    ),
)

_JS_RULES = (
    _rule(rf"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>{_IDENT})", SymbolKind.CLASS),
    _rule(rf"^\s*(?:export\s+)?interface\s+(?P<name>{_IDENT})", SymbolKind.INTERFACE),
    _rule(rf"^\s*(?:export\s+)?type\s+(?P<name>{_IDENT})\s*=", SymbolKind.INTERFACE),
    _rule(
        rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>{_IDENT})",
        SymbolKind.FUNCTION,
    ),
    _rule(
        rf"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>{_IDENT})\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\(|function|<)",
        SymbolKind.FUNCTION,
    ),
    _rule(rf"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>{_IDENT})\s*(?::[^=]+)?=", SymbolKind.VARIABLE),
)

_GO_RULES = (
    _rule(rf"^\s*func\s*(?:\([^)]*\)\s*)?(?P<name>{_IDENT})\s*[\(\[]", SymbolKind.FUNCTION),
    _rule(rf"^\s*type\s+(?P<name>{_IDENT})\s+(?:struct|interface)", SymbolKind.CLASS),
)

_COBOL_RULES = (
    _rule(rf"PROGRAM-ID\s*\.\s*(?P<name>{_COBOL_IDENT})", SymbolKind.PROGRAM, re.IGNORECASE),
    _rule(rf"^\s*(?P<name>{_COBOL_IDENT})\s+SECTION\s*\.", SymbolKind.SECTION, re.IGNORECASE),
    _rule(rf"\bPERFORM\s+(?P<name>{_COBOL_IDENT})", SymbolKind.CALL, re.IGNORECASE),
    _rule(rf"\bCALL\s+['\"](?P<name>[^'\"]+)['\"]", SymbolKind.CALL, re.IGNORECASE),
    _rule(rf"\bCOPY\s+(?P<name>{_COBOL_IDENT})", SymbolKind.CALL, re.IGNORECASE),
)

_JCL_RULES = (
    _rule(rf"^//(?P<name>{_COBOL_IDENT})\s+EXEC\b", SymbolKind.STEP, re.IGNORECASE),
    _rule(rf"^//(?P<name>{_COBOL_IDENT})\s+JOB\b", SymbolKind.PROGRAM, re.IGNORECASE),
    _rule(rf"^//(?P<name>{_COBOL_IDENT})\s+DD\b", SymbolKind.VARIABLE, re.IGNORECASE),
)

_SQL_RULES = (
    _rule(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:GLOBAL\s+TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[\w.\"\[\]]+)",
        SymbolKind.TABLE,
        re.IGNORECASE,
    ),
    _rule(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:VIEW|MATERIALIZED\s+VIEW)\s+(?P<name>[\w.\"\[\]]+)",
        SymbolKind.TABLE,
        re.IGNORECASE,
    ),
    _rule(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:PROCEDURE|PROC)\s+(?P<name>[\w.\"\[\]]+)",
        SymbolKind.PROCEDURE,
        re.IGNORECASE,
    ),
    _rule(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?P<name>[\w.\"\[\]]+)",
        SymbolKind.FUNCTION,
        re.IGNORECASE,
    ),
    _rule(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?P<name>[\w.\"\[\]]+)", SymbolKind.VARIABLE, re.IGNORECASE),
)

_RUBY_RULES = (
    _rule(rf"^\s*class\s+(?P<name>{_IDENT})", SymbolKind.CLASS),
    _rule(rf"^\s*module\s+(?P<name>{_IDENT})", SymbolKind.MODULE),
    _rule(rf"^\s*def\s+(?:self\.)?(?P<name>[A-Za-z_][A-Za-z0-9_]*[?!=]?)", SymbolKind.METHOD),
)

_PHP_RULES = (
    _rule(rf"^\s*(?:abstract\s+|final\s+)?class\s+(?P<name>{_IDENT})", SymbolKind.CLASS),
    _rule(rf"^\s*interface\s+(?P<name>{_IDENT})", SymbolKind.INTERFACE),
    _rule(
        rf"^\s*(?:public\s+|private\s+|protected\s+|static\s+|abstract\s+|final\s+)*function\s+&?(?P<name>{_IDENT})",
        SymbolKind.FUNCTION,
    ),
)

_C_RULES = (
    _rule(rf"^\s*(?:typedef\s+)?(?:struct|union|enum)\s+(?P<name>{_IDENT})", SymbolKind.CLASS),
    _rule(
        rf"^[A-Za-z_][\w\s\*&:<>,\[\]]*?\b(?P<name>{_IDENT})\s*\([^;]*\)\s*(?:const\s*)?\{{",
        SymbolKind.FUNCTION,
    ),
)

_RUST_RULES = (
    _rule(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(?P<name>{_IDENT})", SymbolKind.FUNCTION),
    _rule(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(?P<name>{_IDENT})", SymbolKind.CLASS),
)

_SHELL_RULES = (
    _rule(rf"^\s*(?:function\s+)?(?P<name>{_IDENT})\s*\(\s*\)\s*\{{", SymbolKind.FUNCTION),
)

_RULES_BY_LANGUAGE: Mapping[str, tuple[_Rule, ...]] = {
    "python": _PYTHON_RULES,
    "java": _JVM_RULES,
    "kotlin": _JVM_RULES,
    "csharp": _JVM_RULES,
    "scala": _JVM_RULES,
    "groovy": _JVM_RULES,
    "javascript": _JS_RULES,
    "typescript": _JS_RULES,
    "vue": _JS_RULES,
    "svelte": _JS_RULES,
    "go": _GO_RULES,
    "cobol": _COBOL_RULES,
    "jcl": _JCL_RULES,
    "sql": _SQL_RULES,
    "ruby": _RUBY_RULES,
    "php": _PHP_RULES,
    "c": _C_RULES,
    "cpp": _C_RULES,
    "objc": _C_RULES,
    "rust": _RUST_RULES,
    "shell": _SHELL_RULES,
}

_PARSED_LANGUAGES = frozenset({"python"})

_UNIVERSAL = re.compile(
    rf"^\s*(?:[\w@\[\]<>,\.\*&:]+\s+)*?(?P<name>{_IDENT})\s*(?:\(|\{{|=)"
)

_NOISE_NAMES = frozenset(
    {
        "if", "else", "elif", "for", "while", "switch", "case", "return", "with",
        "try", "catch", "except", "finally", "do", "in", "and", "or", "not",
        "import", "from", "print", "new", "delete", "throw", "raise", "assert",
        "when", "match", "select", "insert", "update", "where", "values", "set",
        "using", "package", "namespace", "public", "private", "protected", "static",
        "end", "begin", "then", "elsif", "unless", "yield", "await", "typeof",
    }
)

_COMMENT_PREFIXES = ("//", "#", "--", "*", "/*", "'", ";", "<!--")


def _language_of(path: str) -> str:
    return language_hint_for(path)


def _is_comment(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith(_COMMENT_PREFIXES)


def _clean_sql_name(raw: str) -> str:
    return raw.strip().strip('"').strip("[]").strip("`")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _block_end(lines: Sequence[str], index: int, language: str) -> int | None:
    if language not in {"python", "ruby"}:
        return None
    base = _indent_of(lines[index])
    for offset in range(index + 1, len(lines)):
        candidate = lines[offset]
        if not candidate.strip():
            continue
        if _indent_of(candidate) <= base:
            return offset
    return len(lines)


def _rules_for(language: str) -> tuple[_Rule, ...]:
    return _RULES_BY_LANGUAGE.get(language, ())


def _confidence_for(language: str) -> Confidence:
    return Confidence.PARSED if language in _PARSED_LANGUAGES else Confidence.HEURISTIC


def _extract(path: str, text: str, language: str) -> list[SymbolInfo]:
    lines = text.splitlines()
    rules = _rules_for(language)
    confidence = _confidence_for(language)
    found: list[SymbolInfo] = []
    seen: set[tuple[str, int]] = set()
    for index, line in enumerate(lines):
        number = index + 1
        if _is_comment(line) and language != "jcl":
            continue
        matched = False
        for rule in rules:
            match = rule.expression.search(line)
            if match is None:
                continue
            name = match.group("name")
            if language == "sql":
                name = _clean_sql_name(name)
            if not name or name.lower() in _NOISE_NAMES:
                continue
            key = (name, number)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                SymbolInfo(
                    path=path,
                    name=name,
                    kind=rule.kind,
                    line_start=number,
                    line_end=_block_end(lines, index, language),
                    language_hint=language,
                    confidence=confidence,
                )
            )
            matched = True
        if matched or rules:
            continue
        universal = _UNIVERSAL.match(line)
        if universal is None:
            continue
        name = universal.group("name")
        if name.lower() in _NOISE_NAMES:
            continue
        key = (name, number)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            SymbolInfo(
                path=path,
                name=name,
                kind=SymbolKind.UNKNOWN,
                line_start=number,
                line_end=None,
                language_hint=language,
                confidence=Confidence.HEURISTIC,
            )
        )
    return found


def _candidate_paths(snapshot: RepositorySnapshot, path: str | None) -> list[str]:
    if path is None:
        return [record.path for record in snapshot.files]
    wanted = normalize_path(path)
    if wanted in snapshot.file_map():
        return [wanted]
    return [
        record.path
        for record in snapshot.files
        if record.path == wanted or record.path.startswith(wanted + "/")
    ]


def _read_text(root: Path, path: str, max_file_bytes: int) -> str | None:
    try:
        full_path = resolve_within(root, path)
        raw = full_path.read_bytes()
    except (OSError, ValueError):
        return None
    if len(raw) > max_file_bytes or b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")


def find_symbols(
    snapshot: RepositorySnapshot,
    path: str | None = None,
    name: str | None = None,
    *,
    kinds: Iterable[SymbolKind] = (),
    max_results: int = DEFAULT_MAX_SYMBOLS,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> tuple[SymbolInfo, ...]:
    if max_results <= 0:
        return ()
    wanted_kinds = frozenset(kinds)
    root = Path(snapshot.root)
    collected: list[SymbolInfo] = []
    for candidate in _candidate_paths(snapshot, path):
        text = _read_text(root, candidate, max_file_bytes)
        if text is None:
            continue
        language = _language_of(candidate)
        for symbol in _extract(candidate, text, language):
            if name is not None and symbol.name != name:
                continue
            if wanted_kinds and symbol.kind not in wanted_kinds:
                continue
            collected.append(symbol)
            if len(collected) >= max_results:
                return tuple(collected)
    return tuple(collected)
