"""Deterministic low-noise contract for agent and chat outputs."""

from __future__ import annotations

import re
from collections import Counter


FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("command_echo", re.compile(r"\bRan command\b|\bRunning command\b", re.I)),
    ("edit_echo", re.compile(r"\bEdited\b|\bWrite\b|\bWrote\b", re.I)),
    ("diff_echo", re.compile(r"^diff --git\b|^@@\b|^\+\+\+\s|^---\s", re.M)),
    ("written_artifact_echo", re.compile(r"conte[uú]do do arquivo|artifact content|arquivo rec[eé]m-escrito|newly written artifact", re.I)),
)

RECEIPT_LINES = ("ARQUIVO:", "BLOCOS:", "BYTES:")
RECEIPT_CONTRACT_FLAGS = (
    "no_command_echo",
    "no_tool_output_echo",
    "no_diff_echo",
    "no_written_artifact_echo",
)
MAX_RECEIPT_STATUS_LINES = 3
MAX_RECEIPT_ERROR_LINES = 8

STACKTRACE_RE = re.compile(r"Traceback \(most recent call last\):|^\s+at .+\(.+:\d+\)", re.M)
LOG_LINE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}|"
    r"\[[A-Z]+\]|"
    r"(DEBUG|INFO|WARN|WARNING|ERROR|TRACE)\b|"
    r"[A-Za-z0-9_.-]+Exception:)",
    re.M,
)


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _is_long_log(text: str, *, min_lines: int = 20) -> bool:
    lines = [line for line in _lines(text) if line.strip()]
    if len(lines) < min_lines:
        return False
    hits = sum(1 for line in lines if LOG_LINE_RE.search(line))
    return hits >= max(8, len(lines) // 2)


def _is_long_stacktrace(text: str, *, max_lines: int = 12) -> bool:
    if not STACKTRACE_RE.search(text):
        return False
    return len([line for line in _lines(text) if line.strip()]) > max_lines


def _validate(text: str, *, agent: bool) -> list[str]:
    errors: list[str] = []
    for code, pattern in FORBIDDEN_PATTERNS:
        if pattern.search(text):
            errors.append(code)
    if _is_long_log(text):
        errors.append("long_log_echo")
    if _is_long_stacktrace(text):
        errors.append("long_stacktrace_echo")
    if agent and len([line for line in _lines(text) if line.strip()]) > 220:
        errors.append("agent_output_too_long")
    return sorted(set(errors))


def validate_chat_update(text: str) -> list[str]:
    """Return violation codes for chat/status output."""

    return _validate(text, agent=False)


def validate_agent_output(text: str) -> list[str]:
    """Return violation codes for subagent mergeable output."""

    return _validate(text, agent=True)


def receipt_contract() -> dict:
    """Contrato do recibo de 3 linhas devolvido pelo subagente no `run-stage`.

    O subagente grava o artefato ele mesmo no caminho `output` do manifesto e
    devolve apenas este recibo — nunca o conteúdo do artefato, comando, log de
    ferramenta ou diff. Isso evita que o mesmo conteúdo pague contexto duas
    vezes (uma na resposta do subagente, outra quando o orquestrador grava).
    """

    return {
        "subagent_writes_output": True,
        "receipt_format": list(RECEIPT_LINES),
        "flags": list(RECEIPT_CONTRACT_FLAGS),
        "max_status_lines": MAX_RECEIPT_STATUS_LINES,
        "max_error_lines": MAX_RECEIPT_ERROR_LINES,
    }


def validate_receipt(text: str) -> list[str]:
    """Valida o recibo de 3 linhas (ARQUIVO/BLOCOS/BYTES) do subagente.

    Reaproveita `validate_agent_output` para as regras de ruído (eco de
    comando, diff, artefato escrito etc.) e soma a checagem de forma/tamanho
    específica do recibo.
    """

    errors = _validate(text, agent=False)
    lines = [line for line in _lines(text) if line.strip()]
    if len(lines) > MAX_RECEIPT_STATUS_LINES:
        errors.append("receipt_too_long")
    if not any(line.startswith("ARQUIVO:") for line in lines):
        errors.append("receipt_missing_arquivo")
    if not any(line.startswith("BLOCOS:") for line in lines):
        errors.append("receipt_missing_blocos")
    if not any(line.startswith("BYTES:") for line in lines):
        errors.append("receipt_missing_bytes")
    return sorted(set(errors))


def _stacktrace_summary(text: str) -> str:
    lines = [line.strip() for line in _lines(text) if line.strip()]
    if not lines:
        return ""
    exception = next((line for line in reversed(lines) if "Exception" in line or "Error" in line), lines[-1])
    return f"STACKTRACE: {exception}"


def summarize_command_result(stdout: str, stderr: str, max_lines: int = 8) -> str:
    """Summarize command output without echoing long logs, diffs, or traces."""

    text = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
    if not text:
        return ""
    if _is_long_stacktrace(text):
        return _stacktrace_summary(text)
    redacted = redact_tool_noise(text)
    lines = [line for line in _lines(redacted) if line.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    files = Counter()
    for line in lines:
        match = re.search(r"([A-Za-z]:)?[/\\][^:\n]+|[\w./\\-]+\.[A-Za-z0-9_]+", line)
        if match:
            files[match.group(0)] += 1
    if files:
        summary = [f"{path}: {count}" for path, count in files.most_common(max_lines)]
        return "\n".join(summary)
    return "\n".join(lines[: max_lines - 1] + [f"... {len(lines) - max_lines + 1} linhas omitidas"])


def redact_tool_noise(text: str) -> str:
    """Remove deterministic tool chatter and compress noisy blocks."""

    kept: list[str] = []
    for line in _lines(text):
        if any(pattern.search(line) for _, pattern in FORBIDDEN_PATTERNS):
            continue
        if line.startswith(("+", "-")) and not line.startswith(("- ", "+ ")):
            continue
        kept.append(line)
    redacted = "\n".join(kept).strip()
    if _is_long_stacktrace(redacted):
        return _stacktrace_summary(redacted)
    if _is_long_log(redacted):
        lines = [line for line in _lines(redacted) if line.strip()]
        levels = Counter()
        for line in lines:
            match = re.search(r"\b(DEBUG|INFO|WARN|WARNING|ERROR|TRACE)\b", line)
            levels[match.group(1) if match else "LOG"] += 1
        return "\n".join(f"{level}: {count}" for level, count in sorted(levels.items()))
    return redacted
