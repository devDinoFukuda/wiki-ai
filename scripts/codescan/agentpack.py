"""Agent-pack compacto para subagentes.

O pacote entrega evidência operacional ranqueada, não cópia do repositório.
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass
from typing import Iterable


DEFAULT_MAX_BYTES = 45_000
DEFAULT_MAX_FILES_PER_MODULE = 4
DEFAULT_MAX_LINES_PER_FILE = 40
MAX_FILE_BYTES = 250_000

SOURCE_EXTENSIONS = {
    ".java", ".kt", ".kts", ".cs", ".py", ".js", ".jsx", ".ts", ".tsx",
    ".go", ".rs", ".rb", ".php", ".scala", ".sql", ".yaml", ".yml", ".json",
    ".xml", ".properties", ".gradle", ".toml",
}

SKIP_DIRS = {".git", "target", "build", "dist", "node_modules", "__pycache__", ".idea", ".vscode"}

ROLE_WEIGHTS = (
    (re.compile(r"(controller|resource|endpoint|handler|route)", re.I), 110),
    (re.compile(r"(service|usecase|use_case|application|facade)", re.I), 105),
    (re.compile(r"(domain|model|entity|aggregate|policy|rule|validator)", re.I), 95),
    (re.compile(r"(repository|gateway|client|adapter|producer|consumer|publisher|listener)", re.I), 90),
    (re.compile(r"(config|configuration|properties|yaml|yml|xml)", re.I), 70),
    (re.compile(r"(exception|error|failure)", re.I), 65),
    (re.compile(r"(test|spec)", re.I), 25),
)

# Material de entrada por estágio (sdd-contract §1.3): lido do proprio workdir
# (artefatos ja gravados por merge-agent-output), nunca do repo. Ordem das
# tuplas define a ordem de leitura no pack.
STAGE_INPUT_GLOBS: dict[str, tuple[str, ...]] = {
    "rules": ("modules/*.md",),
    "architecture": (
        "modules/*.md", "sdd/domain.md", "sdd/state-machines.md", "sdd/permissions.md",
    ),
    "specs": ("modules/*.md", "sdd/architecture.md", "sdd/domain.md"),
    "synth": ("sdd/*.md",),
}

SIGNAL_PATTERNS = (
    re.compile(r"@\w+"),
    re.compile(r"\b(public|private|protected|class|interface|enum|record|fun|def|function)\b"),
    re.compile(r"\b(if|else|switch|case|when|for|while|try|catch|throw|throws|return)\b"),
    re.compile(r"\b(save|persist|insert|update|delete|publish|send|emit|consume|request|post|get|put|patch)\b", re.I),
    re.compile(r"\b(http|kafka|sqs|sns|dynamo|redis|sql|jdbc|jpa|mongo|cache)\b", re.I),
)


@dataclass(frozen=True)
class PackLimits:
    max_bytes: int = DEFAULT_MAX_BYTES
    max_files_per_module: int = DEFAULT_MAX_FILES_PER_MODULE
    max_lines_per_file: int = DEFAULT_MAX_LINES_PER_FILE


def compact_json_size(obj: object) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def dump_compact_json(path: str, obj: object) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def estimate_module_bytes(module: dict, limits: PackLimits | None = None) -> int:
    limits = limits or PackLimits()
    files = min(int(module.get("files") or 1), limits.max_files_per_module)
    lines = min(max(int(module.get("loc") or 1), 1), limits.max_lines_per_file * files)
    return 380 + files * 190 + lines * 56


def choose_batch_count(modules: list[dict], limits: PackLimits | None = None, max_agents: int = 8) -> int:
    if not modules:
        return 1
    limits = limits or PackLimits()
    total = sum(estimate_module_bytes(m, limits) for m in modules)
    by_bytes = max(1, -(-total // limits.max_bytes))
    return max(1, min(max_agents, len(modules), by_bytes))


def _safe_join(repo: str, rel: str) -> str | None:
    repo_abs = os.path.abspath(repo)
    full = os.path.abspath(os.path.join(repo_abs, rel))
    if full == repo_abs or full.startswith(repo_abs + os.sep):
        return full
    return None


def _role_score(rel: str) -> int:
    score = 0
    for pattern, weight in ROLE_WEIGHTS:
        if pattern.search(rel):
            score = max(score, weight)
    return score


def _signal_score(lines: Iterable[str]) -> int:
    score = 0
    for line in lines:
        for pattern in SIGNAL_PATTERNS:
            if pattern.search(line):
                score += 8
    return min(score, 160)


def _read_lines(full: str) -> list[str]:
    try:
        with open(full, encoding="utf-8-sig", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _candidate_files(repo: str, module_path: str) -> list[tuple[int, str, list[str]]]:
    repo_abs = os.path.abspath(repo)
    root = _safe_join(repo_abs, module_path)
    if root is None or not os.path.isdir(root):
        return []
    candidates: list[tuple[int, str, list[str]]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SOURCE_EXTENSIONS:
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                continue
            rel = os.path.relpath(full, repo_abs).replace("\\", "/")
            lines = _read_lines(full)
            if not lines:
                continue
            score = _role_score(rel) + _signal_score(lines[:220]) + min(len(lines), 220) // 25
            candidates.append((score, rel, lines))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates


def _select_line_numbers(lines: list[str], max_lines: int) -> list[int]:
    selected: list[int] = []
    for idx, line in enumerate(lines, start=1):
        if any(pattern.search(line) for pattern in SIGNAL_PATTERNS):
            selected.append(idx)
        if len(selected) >= max_lines:
            return selected
    if not selected:
        selected = list(range(1, min(len(lines), max_lines) + 1))
    return selected[:max_lines]


def _excerpt(rel: str, lines: list[str], max_lines: int) -> dict:
    nums = _select_line_numbers(lines, max_lines)
    return {
        "path": rel,
        "total_lines": len(lines),
        "included_lines": nums,
        "signals": [{"l": n, "t": lines[n - 1]} for n in nums],
    }


def build_agent_pack(
    *,
    repo: str,
    repo_label: str,
    stage: str,
    batch: int,
    total_batches: int,
    modules: list[dict],
    limits: PackLimits | None = None,
) -> dict:
    limits = limits or PackLimits()
    packed_modules: list[dict] = []
    dropped: list[dict] = []
    pack = {
        "schema": "wiki-ai.agent-pack.v2",
        "stage": stage,
        "batch": batch,
        "total_batches": total_batches,
        "repo": repo_label,
        "budget": {
            "max_bytes": limits.max_bytes,
            "max_files_per_module": limits.max_files_per_module,
            "max_lines_per_file": limits.max_lines_per_file,
            "format": "compact-json",
        },
        "rules": [
            "PT-BR tecnico; sem prosa metodologica; sem eco de comando/log/diff/codigo.",
            "Nao copie o repo para store/.codescan; use somente evidencias deste pack.",
            "Confirmacao exige citacao path:linha presente em signals.",
            "Evidencia insuficiente deve virar FAILED MODULE com leitura adicional pontual.",
        ],
        "modules": packed_modules,
        "dropped": dropped,
    }
    for mod in modules:
        excerpts = []
        for _score, rel, lines in _candidate_files(repo, mod["path"])[: limits.max_files_per_module]:
            excerpts.append(_excerpt(rel, lines, limits.max_lines_per_file))
        item = {
            "path": mod["path"],
            "loc": mod.get("loc", 0),
            "files": mod.get("files", 0),
            "languages": mod.get("languages", []),
            "evidence": excerpts,
        }
        candidate = {**pack, "modules": packed_modules + [item]}
        if compact_json_size(candidate) <= limits.max_bytes or not packed_modules:
            packed_modules.append(item)
        else:
            dropped.append({"path": mod["path"], "reason": "budget"})
    pack["metrics"] = {
        "bytes": compact_json_size(pack),
        "modules": len(packed_modules),
        "files": sum(len(m["evidence"]) for m in packed_modules),
        "dropped": len(dropped),
    }
    while compact_json_size(pack) > limits.max_bytes and packed_modules:
        largest = max(packed_modules, key=lambda m: sum(len(e.get("signals", [])) for e in m.get("evidence", [])))
        changed = False
        for ev in largest.get("evidence", []):
            signals = ev.get("signals", [])
            if len(signals) > 8:
                del signals[8:]
                ev["included_lines"] = ev.get("included_lines", [])[:8]
                changed = True
                break
        if not changed:
            break
        pack["metrics"]["bytes"] = compact_json_size(pack)
    pack["metrics"]["bytes"] = compact_json_size(pack)
    return pack


def _stage_input_files(wd: str, stage: str) -> list[str]:
    """Resolve o material de entrada do estagio (sdd-contract §1.3), lido do
    proprio workdir (artefatos ja gravados por merge-agent-output) — nunca do
    repo. Ordem estavel: segue STAGE_INPUT_GLOBS, glob ordenado por nome."""
    patterns = STAGE_INPUT_GLOBS.get(stage, ())
    seen: set[str] = set()
    files: list[str] = []
    for pattern in patterns:
        for full in sorted(glob.glob(os.path.join(wd, pattern))):
            if not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, wd).replace("\\", "/")
            if rel in seen:
                continue
            seen.add(rel)
            files.append(rel)
    return files


def build_stage_pack(
    *,
    wd: str,
    repo_label: str,
    stage: str,
    batch: int,
    total_batches: int,
    items: list[str],
    limits: PackLimits | None = None,
) -> dict:
    """Pack v2 determinístico com o material de entrada de um estágio
    (`rules`/`architecture`/`specs`/`synth`), lido dos artefatos já gravados
    no workdir (`modules/*.md`, `sdd/*.md`) — nunca do repo (`sdd-contract`
    §1.3). Mesmo schema/formato de dump de `build_agent_pack`, adaptado para
    fontes documentais em vez de módulos de código."""
    limits = limits or PackLimits()
    packed_sources: list[dict] = []
    dropped: list[dict] = []
    pack = {
        "schema": "wiki-ai.agent-pack.v2",
        "stage": stage,
        "batch": batch,
        "total_batches": total_batches,
        "repo": repo_label,
        "items": list(items),
        "budget": {
            "max_bytes": limits.max_bytes,
            "max_lines_per_file": limits.max_lines_per_file,
            "format": "compact-json",
        },
        "rules": [
            "PT-BR tecnico; sem prosa metodologica; sem eco de comando/log/diff/codigo.",
            "Nao copie o repo para store/.codescan; use somente evidencias deste pack.",
            "Confirmacao exige citacao path:linha presente nas sources deste pack.",
            "Evidencia insuficiente deve virar FAILED <TIPO> com leitura adicional pontual.",
        ],
        "sources": packed_sources,
        "dropped": dropped,
    }
    for rel in _stage_input_files(wd, stage):
        full = os.path.join(wd, *rel.split("/"))
        lines = _read_lines(full)
        if not lines:
            continue
        included = lines[: limits.max_lines_per_file]
        item = {
            "path": rel,
            "total_lines": len(lines),
            "included_lines": len(included),
            "content": "\n".join(included),
        }
        candidate = {**pack, "sources": packed_sources + [item]}
        if compact_json_size(candidate) <= limits.max_bytes or not packed_sources:
            packed_sources.append(item)
        else:
            dropped.append({"path": rel, "reason": "budget"})
    pack["metrics"] = {
        "bytes": compact_json_size(pack),
        "sources": len(packed_sources),
        "files": len(packed_sources),
        "dropped": len(dropped),
    }
    while compact_json_size(pack) > limits.max_bytes and packed_sources:
        largest = max(packed_sources, key=lambda s: len(s.get("content", "")))
        content_lines = largest.get("content", "").split("\n")
        if len(content_lines) > 8:
            largest["content"] = "\n".join(content_lines[:8])
            largest["included_lines"] = 8
        else:
            break
        pack["metrics"]["bytes"] = compact_json_size(pack)
    pack["metrics"]["bytes"] = compact_json_size(pack)
    return pack
