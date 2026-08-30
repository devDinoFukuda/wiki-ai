"""Deterministic low-noise contract for agent and chat outputs."""

from __future__ import annotations

import os
import re
from collections import Counter


FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("command_echo", re.compile(r"\bRan command\b|\bRunning command\b", re.I)),
    # F-19: âncoras em início de linha (com indentação opcional) para que
    # "edit_echo" só case eco literal de ferramenta ("Edited x", "Write file",
    # "Wrote x.py"), não a palavra em prosa técnica no meio de uma frase (ex.:
    # "O metodo write() grava..."). `(?!\()` evita casar "Write(" de uma
    # chamada de função citada no início de uma linha de código.
    ("edit_echo", re.compile(r"^\s*(Edited|Wrote|Writing)\b|^\s*Write\b(?!\()", re.I | re.M)),
    # F-05: exige contexto real de diff (prefixo a/ b/, /dev/null, ou um nome
    # de arquivo com extensão; hunk header com dígitos) em vez de `\s` solto
    # (que casa a própria quebra de linha e faz qualquer "---"/"+++" isolado
    # — hr do markdown, delimitador de frontmatter — disparar). O lookahead
    # negativo em `---` impede casar o marcador legítimo do formato SPEC
    # (`--- requirements.md ---`, ver SPEC_FILE_RE em agentmerge.py), que
    # sempre fecha com um `---` final.
    ("diff_echo", re.compile(
        r"^diff --git a/\S+ b/\S+"
        r"|^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@"
        r"|^\+\+\+ (?:a/\S+|b/\S+|/dev/null|\S+\.\w+)"
        r"|^--- (?!.*\s---\s*$)(?:a/\S+|b/\S+|/dev/null|\S+\.\w+)",
        re.M,
    )),
    ("written_artifact_echo", re.compile(r"conte[uú]do do arquivo|artifact content|arquivo rec[eé]m-escrito|newly written artifact", re.I)),
)

# F-19(a): conteúdo entre crases simples e dentro de fences ``` é mascarado
# (preservando quebras de linha, para não afetar contagem/numeração) antes de
# aplicar FORBIDDEN_PATTERNS — prosa técnica que cita "write()", "Edited" ou
# um diff de exemplo dentro de um bloco de código não deve ser tratada como
# eco real de ferramenta.
_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _mask_span(match: re.Match[str]) -> str:
    return "".join(ch if ch == "\n" else " " for ch in match.group(0))


def _mask_code_spans(text: str) -> str:
    """Substitui conteúdo entre crases/fences por espaços, preservando `\\n`."""

    return _INLINE_CODE_RE.sub(_mask_span, _FENCE_RE.sub(_mask_span, text))

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
    masked = _mask_code_spans(text)
    for code, pattern in FORBIDDEN_PATTERNS:
        if pattern.search(masked):
            errors.append(code)
    if _is_long_log(text):
        errors.append("long_log_echo")
    if _is_long_stacktrace(text):
        errors.append("long_stacktrace_echo")
    if agent and len([line for line in _lines(text) if line.strip()]) > _agent_output_max_lines():
        errors.append("agent_output_too_long")
    return sorted(set(errors))


def validate_chat_update(text: str) -> list[str]:
    """Return violation codes for chat/status output."""

    return _validate(text, agent=False)


def validate_agent_output(text: str) -> list[str]:
    """Return violation codes for subagent mergeable output."""

    return _validate(text, agent=True)


MAX_VIOLATION_SNIPPET_LEN = 120
# F-32: limite de linhas do output de subagente era hard-coded (aqui e em
# `_validate`, duplicado como literal `220`). Agora configurável via env
# `WK_AGENT_OUTPUT_MAX_LINES` — `AGENT_OUTPUT_MAX_LINES` permanece como o
# valor padrão (API pública inalterada); `_agent_output_max_lines()` é o
# valor efetivo em vigor (lido a cada chamada, para refletir a env atual sem
# exigir reimportar o módulo — importante em testes que fazem
# `os.environ[...] = ...` no meio da suíte).
AGENT_OUTPUT_MAX_LINES = 220
AGENT_OUTPUT_MAX_LINES_ENV_VAR = "WK_AGENT_OUTPUT_MAX_LINES"
MIN_AGENT_OUTPUT_MAX_LINES = 50
LONG_STACKTRACE_MAX_LINES = 12


def _agent_output_max_lines() -> int:
    """Limite efetivo de linhas para `agent_output_too_long`.

    Lê `WK_AGENT_OUTPUT_MAX_LINES` do ambiente a cada chamada; um valor
    ausente, não-inteiro, ou abaixo de `MIN_AGENT_OUTPUT_MAX_LINES` (50) cai
    de volta para `AGENT_OUTPUT_MAX_LINES` (220) — nunca lança exceção nem
    aceita um limite baixo o bastante para rejeitar recibos legítimos.
    """

    raw = os.environ.get(AGENT_OUTPUT_MAX_LINES_ENV_VAR)
    if raw is None or not raw.strip():
        return AGENT_OUTPUT_MAX_LINES
    try:
        value = int(raw.strip())
    except ValueError:
        return AGENT_OUTPUT_MAX_LINES
    if value < MIN_AGENT_OUTPUT_MAX_LINES:
        return AGENT_OUTPUT_MAX_LINES
    return value


def _truncate_snippet(text: str, limit: int = MAX_VIOLATION_SNIPPET_LEN) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1].rstrip() + "…"


def detect_violations(text: str, *, agent: bool) -> list[dict]:
    """Detecta violações do contrato de ruído com diagnóstico acionável.

    Ao contrário de `_validate`/`validate_agent_output` (que só devolvem o
    nome da regra deduplicado), isto reporta CADA linha ofensiva — um log
    ecoado de centenas de linhas com "Ran command"/"Edited" repetidos gera
    uma violação por ocorrência, não uma só. Cada item traz `regra`, `linha`
    (1-indexed; `None` quando a violação é agregada, sem uma única linha
    ofensiva, como `agent_output_too_long`), `trecho` (texto truncado a
    ~120 chars, ou a medição "N linhas, limite M" para violações agregadas)
    e `padrao` (o regex, ou descrição da regra, que casou). Existe para que
    `agentmerge._reject_noise` consiga apontar exatamente o que rejeitar em
    vez de só o código da regra — ver FIX 1 do lote D: sem isso, o operador
    reescrevia a saída inteira por tentativa e erro porque não sabia qual
    linha (ou quanto excesso de linhas) disparou a rejeição. O chamador deve
    truncar a lista (ex.: ~10 primeiras) e reportar o total à parte.
    """

    violations: list[dict] = []
    masked_lines = _lines(_mask_code_spans(text))
    for lineno, (line, masked_line) in enumerate(zip(_lines(text), masked_lines), start=1):
        if not line.strip():
            continue
        for code, pattern in FORBIDDEN_PATTERNS:
            if pattern.search(masked_line):
                violations.append({
                    "regra": code,
                    "linha": lineno,
                    "trecho": _truncate_snippet(line),
                    "padrao": pattern.pattern,
                })
    nonblank = len([line for line in _lines(text) if line.strip()])
    if _is_long_log(text):
        violations.append({
            "regra": "long_log_echo",
            "linha": None,
            "trecho": f"{nonblank} linhas, padrão de log detectado em metade ou mais",
            "padrao": LOG_LINE_RE.pattern,
        })
    if _is_long_stacktrace(text):
        violations.append({
            "regra": "long_stacktrace_echo",
            "linha": None,
            "trecho": f"{nonblank} linhas, limite {LONG_STACKTRACE_MAX_LINES}",
            "padrao": STACKTRACE_RE.pattern,
        })
    if agent:
        effective_limit = _agent_output_max_lines()
        if nonblank > effective_limit:
            violations.append({
                "regra": "agent_output_too_long",
                "linha": None,
                "trecho": (
                    f"{nonblank} linhas, limite {effective_limit} "
                    f"(env {AGENT_OUTPUT_MAX_LINES_ENV_VAR})"
                ),
                "padrao": f"linhas não vazias > {effective_limit}",
            })
    # Linhas ofensivas em ordem de leitura primeiro (mais úteis truncadas nas
    # ~10 primeiras); agregadas (sem linha única) por último, por nome de regra.
    violations.sort(
        key=lambda v: (0, v["linha"], v["regra"]) if v["linha"] is not None else (1, 0, v["regra"])
    )
    return violations


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
    masked_lines = _lines(_mask_code_spans(text))
    for line, masked_line in zip(_lines(text), masked_lines):
        if any(pattern.search(masked_line) for _, pattern in FORBIDDEN_PATTERNS):
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
