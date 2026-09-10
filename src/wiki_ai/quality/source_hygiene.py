from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator

from wiki_ai.quality import vocabulary

__all__ = [
    "HygieneKind",
    "HygieneViolation",
    "PYTHON_SUFFIX",
    "SLASH_COMMENT_SUFFIXES",
    "MARKUP_COMMENT_SUFFIXES",
    "MARKER_WORDS",
    "DIRECTIVE_TOKENS",
    "scan",
]


class HygieneKind(Enum):
    COMMENT = "comment"
    DOCSTRING = "docstring"
    MARKER = "marker"
    DIRECTIVE = "directive"
    UNPARSABLE = "unparsable"


@dataclass(frozen=True)
class HygieneViolation:
    path: str
    line: int
    kind: HygieneKind
    detail: str


PYTHON_SUFFIX = ".py"
SLASH_COMMENT_SUFFIXES = (".js", ".ts", ".java", ".go", ".c", ".cs")
MARKUP_COMMENT_SUFFIXES = (".html", ".xml")
MARKER_WORDS = vocabulary.terms("markers")
DIRECTIVE_TOKENS = vocabulary.terms("directives")

_MARKER_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(word) for word in MARKER_WORDS) + r")\b"
)

_SCANNED_SUFFIXES = (PYTHON_SUFFIX,) + SLASH_COMMENT_SUFFIXES + MARKUP_COMMENT_SUFFIXES
_DOCSTRING_HOLDERS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
_QUOTES = ("'", '"')


def _iter_files(paths: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for entry in paths:
        candidate = Path(entry)
        if candidate.is_dir():
            for found in sorted(candidate.rglob("*")):
                if found.is_file() and found.suffix.lower() in _SCANNED_SUFFIXES:
                    if found not in seen:
                        seen.add(found)
                        yield found
        elif candidate.is_file() and candidate.suffix.lower() in _SCANNED_SUFFIXES:
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def _markers(path: str, line_number: int, text: str) -> list[HygieneViolation]:
    found: list[HygieneViolation] = []
    for match in _MARKER_PATTERN.finditer(text):
        found.append(
            HygieneViolation(path, line_number, HygieneKind.MARKER, match.group(1))
        )
    return found


def _directives(path: str, line_number: int, text: str) -> list[HygieneViolation]:
    found: list[HygieneViolation] = []
    lowered = text.lower()
    for token in DIRECTIVE_TOKENS:
        if token in lowered:
            found.append(
                HygieneViolation(path, line_number, HygieneKind.DIRECTIVE, token)
            )
    return found


def _markers_and_directives(path: str, line_number: int, text: str) -> list[HygieneViolation]:
    return _markers(path, line_number, text) + _directives(path, line_number, text)


def _scan_python(path: str, source: str) -> list[HygieneViolation]:
    found: list[HygieneViolation] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        return [HygieneViolation(path, 1, HygieneKind.UNPARSABLE, str(exc))]
    for token in tokens:
        if token.type == tokenize.COMMENT:
            found.append(
                HygieneViolation(
                    path, token.start[0], HygieneKind.COMMENT, token.string.strip()
                )
            )
            found.extend(_markers_and_directives(path, token.start[0], token.string))
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        found.append(HygieneViolation(path, 1, HygieneKind.UNPARSABLE, str(exc)))
        return found
    holders: list[ast.AST] = [tree]
    holders.extend(node for node in ast.walk(tree) if isinstance(node, _DOCSTRING_HOLDERS))
    for holder in holders:
        body = getattr(holder, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.append(
                HygieneViolation(
                    path,
                    first.lineno,
                    HygieneKind.DOCSTRING,
                    type(holder).__name__,
                )
            )
    for number, line in enumerate(source.splitlines(), start=1):
        found.extend(_markers(path, number, line))
    return _deduplicate(found)


def _slash_comment_positions(line: str) -> list[int]:
    positions: list[int] = []
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in _QUOTES:
            quote = char
            index += 1
            continue
        if line.startswith("//", index) or line.startswith("/*", index):
            positions.append(index)
            return positions
        index += 1
    return positions


def _scan_slash_comments(path: str, source: str) -> list[HygieneViolation]:
    found: list[HygieneViolation] = []
    in_block = False
    for number, line in enumerate(source.splitlines(), start=1):
        if in_block:
            found.append(HygieneViolation(path, number, HygieneKind.COMMENT, line.strip()))
            found.extend(_markers_and_directives(path, number, line))
            if "*/" in line:
                in_block = False
            continue
        positions = _slash_comment_positions(line)
        if positions:
            start = positions[0]
            found.append(
                HygieneViolation(path, number, HygieneKind.COMMENT, line[start:].strip())
            )
            found.extend(_markers_and_directives(path, number, line[start:]))
            if line.startswith("/*", start) and "*/" not in line[start + 2 :]:
                in_block = True
    return _deduplicate(found)


def _scan_markup_comments(path: str, source: str) -> list[HygieneViolation]:
    found: list[HygieneViolation] = []
    in_block = False
    for number, line in enumerate(source.splitlines(), start=1):
        if in_block:
            found.append(HygieneViolation(path, number, HygieneKind.COMMENT, line.strip()))
            found.extend(_markers_and_directives(path, number, line))
            if "-->" in line:
                in_block = False
            continue
        start = line.find("<!--")
        if start >= 0:
            found.append(
                HygieneViolation(path, number, HygieneKind.COMMENT, line[start:].strip())
            )
            found.extend(_markers_and_directives(path, number, line[start:]))
            if "-->" not in line[start + 4 :]:
                in_block = True
    return _deduplicate(found)


def _deduplicate(violations: Iterable[HygieneViolation]) -> list[HygieneViolation]:
    unique: dict[tuple[str, int, HygieneKind, str], HygieneViolation] = {}
    for violation in violations:
        key = (violation.path, violation.line, violation.kind, violation.detail)
        unique.setdefault(key, violation)
    return sorted(unique.values(), key=lambda v: (v.path, v.line, v.kind.value, v.detail))


def scan(paths: Iterable[Path]) -> list[HygieneViolation]:
    violations: list[HygieneViolation] = []
    for file_path in _iter_files(paths):
        display = file_path.as_posix()
        try:
            source = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            violations.append(HygieneViolation(display, 1, HygieneKind.UNPARSABLE, str(exc)))
            continue
        suffix = file_path.suffix.lower()
        if suffix == PYTHON_SUFFIX:
            violations.extend(_scan_python(display, source))
        elif suffix in SLASH_COMMENT_SUFFIXES:
            violations.extend(_scan_slash_comments(display, source))
        elif suffix in MARKUP_COMMENT_SUFFIXES:
            violations.extend(_scan_markup_comments(display, source))
    return violations
