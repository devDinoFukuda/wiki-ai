"""Checkpoint entre sessões.

Sem isto, analisar repo grande é impossível: a sessão morre no meio do estágio
2 e você recomeça do zero. O estado vive em disco, não na conversa.

O work dir fica FORA do repositório analisado. O legado é read-only — nada é
escrito lá dentro, nem um arquivo de estado.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time

_URL_RE = re.compile(r"^(https?|git|ssh)://", re.I)


def is_url(repo: str) -> bool:
    """True se `repo` é um repositório remoto (a clonar), não um caminho local."""
    return bool(_URL_RE.match(repo)) or repo.startswith("git@") or repo.endswith(".git")


def url_name(url: str) -> str:
    """Nome legível derivado da URL: último segmento sem `.git`."""
    seg = url.rstrip("/").split("/")[-1]
    if seg.endswith(".git"):
        seg = seg[:-4]
    return seg or "repo"

# Ordem espelha o Discovery do Reversa: surface≈Scout, modules≈Archaeologist,
# rules≈Detective, architecture≈Architect, specs≈Writer. evidence/synth/verify
# fecham o funil para o corpus do wiki-ai.
STAGES = (
    "surface", "modules", "rules",
    "architecture", "specs",
    "evidence", "synth", "verify",
)

STATUSES = ("pending", "in_progress", "done", "blocked", "failed", "degraded")
ITEM_PROBLEM_STATUSES = ("blocked", "failed", "degraded")
ITEM_STAGES_REQUIRE_FINALIZE = ("modules", "specs")


def workdir(store: str, repo: str) -> str:
    """Work dir derivado do repo: <store>/.codescan/<nome>-<hash>.

    Chave estável: URL usa a própria string; caminho local usa o absoluto.
    Assim o mesmo repo (URL ou path) resolve sempre para o mesmo workdir.
    """
    if is_url(repo):
        key, name = repo, url_name(repo)
    else:
        key = os.path.abspath(repo)
        name = os.path.basename(key.rstrip("/\\")) or "repo"
    h = hashlib.sha1(key.encode()).hexdigest()[:8]
    return os.path.join(store, ".codescan", f"{name}-{h}")


def _path(wd: str) -> str:
    return os.path.join(wd, "state.json")


@contextlib.contextmanager
def _lock(wd: str, timeout: float = 30.0, poll: float = 0.05):
    """Trava entre processos para read-modify-write do estado.

    Agentes paralelos marcam `done` de módulos diferentes ao mesmo tempo. Sem
    trava, dois `load()->save()` concorrentes se sobrescrevem e um `done` some —
    o pior tipo de bug num pipeline com checkpoint. `os.mkdir` é atômico em
    Windows e POSIX; quem cria o diretório detém a trava.
    """
    os.makedirs(wd, exist_ok=True)
    lockdir = os.path.join(wd, ".state.lock")
    deadline = time.time() + timeout
    while True:
        try:
            os.mkdir(lockdir)
            break
        except (FileExistsError, PermissionError):
            if time.time() >= deadline:
                # Trava órfã de sessão morta: força depois do timeout.
                with contextlib.suppress(OSError):
                    os.rmdir(lockdir)
                    continue
                raise TimeoutError(f"não obtive a trava do estado em {timeout}s")
            time.sleep(poll)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(lockdir)


def load(wd: str) -> dict | None:
    p = _path(wd)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def init(wd: str, repo: str, topic: str | None) -> dict:
    os.makedirs(wd, exist_ok=True)
    st = {
        "repo": os.path.abspath(repo),
        "topic": topic,
        "workdir": wd,
        "started_at": _now(),
        "updated_at": _now(),
        "stages": {
            s: {"status": "pending", "done": [], "pending": [], "artifact": None}
            for s in STAGES
        },
    }
    save(wd, st)
    return st


def save(wd: str, st: dict) -> None:
    os.makedirs(wd, exist_ok=True)
    st["updated_at"] = _now()
    tmp = _path(wd) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _path(wd))  # atômico: sessão morta não corrompe o estado


def _ensure_status(status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status inválido: {status}")


def _derive_item_stage_status(s: dict, stage: str | None = None) -> str:
    if s.get("failed"):
        return "failed"
    if s.get("blocked"):
        return "blocked"
    if s.get("degraded"):
        return "degraded"
    if s.get("pending"):
        return "in_progress"
    if s.get("done"):
        if stage in ITEM_STAGES_REQUIRE_FINALIZE and not s.get("finalized"):
            return "in_progress"
        return "done"
    return "pending"


def mark(wd: str, stage: str, status: str, artifact: str | None = None) -> dict:
    _ensure_status(status)
    with _lock(wd):
        st = load(wd) or {}
        s = st.setdefault("stages", {}).setdefault(stage, {})
        if status == "done":
            blockers = [
                k for k in ("pending", "blocked", "failed", "degraded")
                if s.get(k)
            ]
            if blockers:
                raise ValueError(
                    "não pode marcar done com itens não resolvidos: "
                    + ", ".join(blockers)
                )
        s["status"] = status
        s["at"] = _now()
        if status == "done":
            s["finalized"] = True
        if artifact:
            s["artifact"] = artifact
        save(wd, st)
        return st


def mark_item(wd: str, stage: str, item: str, done: bool = True) -> dict:
    """Marca um item concluído/pendente. Concorrência-segura: agentes paralelos
    marcam módulos diferentes sem se sobrescrever (o read-modify-write inteiro
    roda sob trava)."""
    with _lock(wd):
        st = load(wd) or {}
        s = st.setdefault("stages", {}).setdefault(stage, {"done": [], "pending": []})
        d = set(s.get("done") or [])
        p = set(s.get("pending") or [])
        problem = {k: set(s.get(k) or []) for k in ITEM_PROBLEM_STATUSES}
        if done:
            d.add(item)
            p.discard(item)
            for items in problem.values():
                items.discard(item)
        else:
            p.add(item)
            d.discard(item)
            for items in problem.values():
                items.discard(item)
        s["done"], s["pending"] = sorted(d), sorted(p)
        for status, items in problem.items():
            s[status] = sorted(items)
        s["items_complete"] = bool(d and not p and not any(problem.values()))
        if not s["items_complete"]:
            s.pop("finalized", None)
        s["status"] = _derive_item_stage_status(s, stage)
        save(wd, st)
        return st


def mark_item_status(
    wd: str,
    stage: str,
    item: str,
    status: str,
    artifact: str | None = None,
    reason: str | None = None,
) -> dict:
    _ensure_status(status)
    if status in ("done", "pending"):
        return mark_item(wd, stage, item, done=(status == "done"))
    if status == "in_progress":
        raise ValueError("status in_progress não é status de item; use pending")
    with _lock(wd):
        st = load(wd) or {}
        s = st.setdefault("stages", {}).setdefault(stage, {"done": [], "pending": []})
        for key in ("done", "pending", *ITEM_PROBLEM_STATUSES):
            items = set(s.get(key) or [])
            if key == status:
                items.add(item)
            else:
                items.discard(item)
            s[key] = sorted(items)
        if artifact:
            artifacts = s.setdefault("artifacts", {})
            artifacts[item] = artifact
        if reason:
            reasons = s.setdefault("reasons", {})
            reasons[item] = reason
        s["items_complete"] = bool(s.get("done") and not s.get("pending") and not any(s.get(k) for k in ITEM_PROBLEM_STATUSES))
        if not s["items_complete"]:
            s.pop("finalized", None)
        s["status"] = _derive_item_stage_status(s, stage)
        save(wd, st)
        return st


def record_stage_error(
    wd: str,
    stage: str,
    error: str,
    artifact=None,
    blockers: list | None = None,
    command: str = "done",
    exit_code: int = 2,
) -> dict:
    """Persiste o último erro acionável de `done` sem mudar o status do stage."""
    with _lock(wd):
        st = load(wd) or {}
        s = st.setdefault("stages", {}).setdefault(stage, {})
        s["last_error"] = {
            "command": command,
            "stage": stage,
            "exit_code": exit_code,
            "error": error,
            "at": _now(),
        }
        s["last_artifact"] = artifact
        if blockers:
            s["last_blockers"] = blockers
        else:
            s.pop("last_blockers", None)
        save(wd, st)
        return st


def clear_stage_error(wd: str, stage: str) -> dict:
    with _lock(wd):
        st = load(wd) or {}
        s = st.setdefault("stages", {}).setdefault(stage, {})
        for key in ("last_error", "last_error_at", "last_artifact", "last_blockers"):
            s.pop(key, None)
        save(wd, st)
        return st


def set_pending(wd: str, stage: str, items: list[str]) -> dict:
    with _lock(wd):
        st = load(wd) or {}
        s = st.setdefault("stages", {}).setdefault(stage, {})
        done = set(s.get("done") or [])
        s["pending"] = sorted(set(items) - done)
        s["done"] = sorted(done)
        for status in ITEM_PROBLEM_STATUSES:
            problem = set(s.get(status) or [])
            problem -= set(items)
            s[status] = sorted(problem)
        s["items_complete"] = bool(done and not s.get("pending") and not any(s.get(k) for k in ITEM_PROBLEM_STATUSES))
        if not s["items_complete"]:
            s.pop("finalized", None)
        s["status"] = _derive_item_stage_status(s, stage)
        save(wd, st)
        return st


def next_stage(st: dict) -> str | None:
    for s in STAGES:
        if (st.get("stages", {}).get(s, {}).get("status")) != "done":
            return s
    return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(path: str) -> str:
    """sha256 hex dos bytes em disco — base da proveniência inforjável do merge."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
