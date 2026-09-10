from __future__ import annotations

import re

__all__ = ["invocation_targets"]

_CALLEE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*[.:>-]{1,2}\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")

_TARGET_NAME = r"[A-Za-z0-9_$#@]+(?:-[A-Za-z0-9_$#@]+)*"

_STATEMENT_CALL = re.compile(
    rf"\b(?:call|perform|invoke|xctl|chain|gosub)\s+"
    rf"(?:'([^']{{1,64}})'|\"([^\"]{{1,64}})\"|({_TARGET_NAME}))",
    re.IGNORECASE,
)

_CICS_TRANSFER = re.compile(
    rf"\b(?:link|xctl|start)\s+(?:program|transid|trans)\s*\(\s*"
    rf"'?({_TARGET_NAME})'?\s*\)",
    re.IGNORECASE,
)

_JCL_STEP = re.compile(
    rf"\bexec\s+(?:pgm|proc|program)\s*=\s*({_TARGET_NAME})", re.IGNORECASE
)

_JCL_BARE_PROC = re.compile(
    rf"^\s*//{_TARGET_NAME}\s+exec\s+({_TARGET_NAME})\s*(?:,|$)",
    re.IGNORECASE | re.MULTILINE,
)

_DEFINITION_KEYWORDS: frozenset[str] = frozenset(
    {
        "def",
        "func",
        "function",
        "fun",
        "fn",
        "sub",
        "procedure",
        "proc",
        "method",
        "class",
        "struct",
        "interface",
        "record",
        "enum",
        "module",
        "package",
        "namespace",
        "program",
        "section",
        "paragraph",
        "declare",
        "define",
        "public",
        "private",
        "protected",
        "static",
        "final",
        "abstract",
        "override",
        "async",
        "void",
        "return",
        "new",
        "typedef",
        "template",
        "operator",
    }
)

_CONTROL_KEYWORDS: frozenset[str] = frozenset(
    {
        "if",
        "elif",
        "else",
        "elseif",
        "while",
        "for",
        "foreach",
        "switch",
        "case",
        "when",
        "match",
        "catch",
        "except",
        "with",
        "using",
        "do",
        "try",
        "until",
        "unless",
        "loop",
        "and",
        "or",
        "not",
        "in",
        "is",
        "print",
        "sizeof",
        "typeof",
        "assert",
        "raise",
        "throw",
        "yield",
        "await",
        "lambda",
        "select",
        "from",
        "where",
        "values",
        "set",
        "end",
        "then",
        "begin",
        "exec",
        "call",
        "perform",
    }
)

_DIRECT_CALL = re.compile(
    rf"(?:^|[^\w.:>-])({_TARGET_NAME})\s*\(", re.MULTILINE
)

_PRECEDING_WORD = re.compile(rf"({_TARGET_NAME})\s*$")

_SIGNATURE_TAIL = re.compile(r"^[^()\n]{0,200}\)\s*(?:\{|:\s*(?:\n|$))")

def _callee_name(raw: str) -> str:
    name = (raw or "").strip().lower().strip("-_")
    if len(name) < 2 or name.isdigit():
        return ""
    if name in _CONTROL_KEYWORDS or name in _DEFINITION_KEYWORDS:
        return ""
    return name


def _word_before(text: str, position: int) -> str:
    match = _PRECEDING_WORD.search(text[:position])
    return match.group(1).lower() if match else ""


def invocation_targets(text: str) -> tuple[str, ...]:
    found: list[str] = []

    def _add(raw: str) -> None:
        name = _callee_name(raw)
        if name and name not in found:
            found.append(name)

    for match in _CALLEE.finditer(text):
        _add(match.group(1))
    for match in _STATEMENT_CALL.finditer(text):
        _add(match.group(1) or match.group(2) or match.group(3) or "")
    for match in _CICS_TRANSFER.finditer(text):
        _add(match.group(1))
    for match in _JCL_STEP.finditer(text):
        _add(match.group(1))
    for match in _JCL_BARE_PROC.finditer(text):
        _add(match.group(1))
    for match in _DIRECT_CALL.finditer(text):
        if _word_before(text, match.start(1)) in _DEFINITION_KEYWORDS:
            continue
        if _SIGNATURE_TAIL.match(text[match.end() :]):
            continue
        _add(match.group(1))
    return tuple(found)
