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


# --- BUG B3 (causa-raiz): canonicalização de identificador de item ----------
#
# Antes desta correção, `mark_item`/`mark_item_status`/`set_pending`
# comparavam itens por igualdade EXATA de string nos sets
# done/pending/blocked/failed/degraded. No Windows, um item cujo caminho tem
# diretórios aninhados chega com '\\' (nativo do SO, via
# `os.path.relpath`/`surface.py`) por uma via e com '/' (normalizado, via
# `sdd.redo_stage`/`agentmerge._normalize_item`) por outra — a mesma entidade
# lógica virava DUAS entradas no mesmo set. A defesa vivia inteira em
# `cli.py` (`_reconcile_stage_items`/`_resolve_stored_item`, chamadas antes/
# depois de cada mutação); qualquer chamador que grave em state.json sem
# passar por esse wrapper (ex.: `sdd.redo_stage`, que chama `mark_item`
# direto) reabria o bug. Estas três funções abaixo replicam, NESTA camada
# (a mais baixa: quem grava em disco), a mesma semântica de resolução já
# estabelecida por `agentmerge._state_item_from_stage`/`cli._resolve_stored_
# item` — para que a garantia de "sem duplicata" não dependa de nenhum
# chamador específico fazer a coisa certa antes de chamar `state.py`.
def _canon_item(value) -> str:
    """Forma canônica (só para COMPARAÇÃO/dedup) de um identificador de
    item: separador '/', sem barras duplicadas nem de borda. Aceita entrada
    com '\\' (path colado do Windows) ou '/' indistintamente. NÃO é
    necessariamente a grafia que fica persistida — ver `_resolve_item`."""
    text = str(value).strip().replace("\\", "/")
    return re.sub(r"/+", "/", text).strip("/")


_ITEM_LIST_KEYS = ("done", "pending", *ITEM_PROBLEM_STATUSES)
_ITEM_LIST_PRECEDENCE = ("pending", *ITEM_PROBLEM_STATUSES, "done")


def _resolve_item(s: dict, item) -> str | None:
    """Resolve `item` (aceita '\\' ou '/') para a grafia JÁ EXISTENTE em
    qualquer lista de item do stage (done/pending/blocked/failed/degraded),
    comparando pela forma canônica. Devolve `None` se nenhuma grafia prévia
    casar — o chamador decide o default para item genuinamente novo (ver
    nota de `set_pending` sobre por que esse default não pode ser '/' às
    cegas para o stage `modules`)."""
    wanted = _canon_item(item)
    if not wanted:
        return None
    for key in _ITEM_LIST_KEYS:
        for candidate in s.get(key) or []:
            if _canon_item(candidate) == wanted:
                return str(candidate)
    return None


def _reconcile_item_lists(s: dict) -> None:
    """Colapsa, EM MEMÓRIA, grafias divergentes ('\\'/'/', ou duplicadas
    dentro da mesma lista) do MESMO item lógico nas listas
    done/pending/blocked/failed/degraded de `s` (mutação in-place).

    Migração de estado legado (decisão): um `state.json` gravado antes desta
    correção (grafia '\\' pura, ou mista com '/') é normalizado AQUI, em
    memória, no início de toda operação de escrita (`mark_item`/
    `mark_item_status`/`set_pending` chamam isto antes de mutar) — nunca por
    um passo de migração isolado que reescreveria o arquivo em uma leitura
    (`load()` continua sendo leitura pura, sem side effect em disco). Isso
    evita tocar o disco em comandos somente-leitura (`state`, `next`,
    `audit`) e só persiste a forma canônica na escrita que já ia acontecer de
    qualquer forma — mesma decisão já tomada por `cli._reconcile_stage_items`
    (fora do meu escopo de edição), replicada aqui para não depender
    exclusivamente desse wrapper.

    Resolução por precedência quando a MESMA forma canônica aparece em mais
    de uma lista: pending > blocked > failed > degraded > done — um item
    ainda pendente ou com problema não pode ficar registrado como `done` ao
    mesmo tempo. Dentro da MESMA lista, colapsa grafias duplicadas do mesmo
    item para uma única entrada (a primeira grafia encontrada)."""
    by_canon: dict[str, dict[str, list[str]]] = {}
    for key in _ITEM_LIST_KEYS:
        for raw in s.get(key) or []:
            canon = _canon_item(raw)
            if not canon:
                continue
            by_canon.setdefault(canon, {}).setdefault(key, []).append(str(raw))

    new_lists = {key: list(dict.fromkeys(s.get(key) or [])) for key in _ITEM_LIST_KEYS}
    artifacts = s.get("artifacts") if isinstance(s.get("artifacts"), dict) else None
    reasons = s.get("reasons") if isinstance(s.get("reasons"), dict) else None

    for _canon, occurrences in by_canon.items():
        all_raw = [raw for raws in occurrences.values() for raw in raws]
        if len(occurrences) == 1 and len(set(all_raw)) == 1:
            continue  # já canônico e único: nada a fazer
        winning_key = next(k for k in _ITEM_LIST_PRECEDENCE if k in occurrences)
        winning_raw = occurrences[winning_key][0]
        for key, raws in occurrences.items():
            for raw in set(raws):
                if key == winning_key and raw == winning_raw:
                    continue
                if raw in new_lists[key]:
                    new_lists[key].remove(raw)
                for mapping in (artifacts, reasons):
                    if mapping and raw in mapping:
                        mapping.setdefault(winning_raw, mapping.pop(raw))
        if winning_raw not in new_lists[winning_key]:
            new_lists[winning_key].append(winning_raw)

    for key in _ITEM_LIST_KEYS:
        s[key] = new_lists[key]


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
    roda sob trava).

    BUG B3 (causa-raiz): `item` é resolvido contra a grafia JÁ EXISTENTE nos
    sets do stage (`_resolve_item`, aceita '\\' ou '/') ANTES de mutar — sem
    isso, um chamador que sempre normaliza para '/' antes de chamar esta
    função (ex.: `sdd.redo_stage`) criava uma segunda entrada ao lado de um
    `done`/`pending` já gravado com '\\'. Sem grafia prévia (item
    genuinamente novo), a forma persistida é a canônica '/' — este é o único
    dos três pontos de escrita (`mark_item`/`mark_item_status`/`set_pending`)
    onde isso é seguro por padrão: os únicos chamadores que passam item novo
    para cá (`cli.cmd_done`/`cmd_problem_status`, já resolvidos por
    `cli._resolve_stored_item`, e `sdd.redo_stage`) já usam/aceitam a forma
    canônica. `set_pending` é diferente — ver nota lá."""
    with _lock(wd):
        st = load(wd) or {}
        s = st.setdefault("stages", {}).setdefault(stage, {"done": [], "pending": []})
        _reconcile_item_lists(s)
        resolved = _resolve_item(s, item)
        if resolved is None:
            resolved = _canon_item(item)
        d = set(s.get("done") or [])
        p = set(s.get("pending") or [])
        problem = {k: set(s.get(k) or []) for k in ITEM_PROBLEM_STATUSES}
        if done:
            d.add(resolved)
            p.discard(resolved)
            for items in problem.values():
                items.discard(resolved)
        else:
            p.add(resolved)
            d.discard(resolved)
            for items in problem.values():
                items.discard(resolved)
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
        _reconcile_item_lists(s)
        # BUG B3 (causa-raiz): mesma resolução contra grafia existente que
        # `mark_item` aplica — ver docstring lá. `artifacts`/`reasons` também
        # passam a ser chaveados pela grafia RESOLVIDA (consistente entre
        # chamadas), não pela entrada crua de cada chamada individual.
        resolved = _resolve_item(s, item)
        if resolved is None:
            resolved = _canon_item(item)
        for key in ("done", "pending", *ITEM_PROBLEM_STATUSES):
            items = set(s.get(key) or [])
            if key == status:
                items.add(resolved)
            else:
                items.discard(resolved)
            s[key] = sorted(items)
        if artifact:
            artifacts = s.setdefault("artifacts", {})
            artifacts[resolved] = artifact
        if reason:
            reasons = s.setdefault("reasons", {})
            reasons[resolved] = reason
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
    """Substitui a lista `pending` do stage (menos o que já está `done`).

    BUG B3 (causa-raiz): itens de `items` são resolvidos contra a grafia já
    existente em qualquer lista do stage (`_resolve_item`), e deduplicados
    entre si pela forma canônica antes de gravar — sem isso, duas grafias do
    MESMO item ('a\\b' e 'a/b') na mesma chamada viravam duas entradas.

    DECISÃO (desvio deliberado de "grava sempre '/'"): um item genuinamente
    NOVO (sem grafia prévia em nenhuma lista) é persistido com a grafia
    CRUA recebida, não forçada para '/'. Motivo: `cli.cmd_plan` é o único
    chamador que povoa `pending` do stage `modules` pela primeira vez, e
    passa `Module.path` de surface.json — nativo do SO ('\\' no Windows para
    diretório aninhado). Esse valor precisa sobreviver intacto porque
    `agentmerge._state_item_from_stage` (fora do meu escopo) resolve o item
    do bloco MODULE do subagente contra a grafia já gravada em `pending` e
    reaproveita ESSA grafia em `done` — forçar '/' aqui faria a primeira
    gravação de `pending` divergir do path nativo de `Module.path`, o que
    não quebra a comparação (que já é canônica em `cli._plan_groups`), mas
    muda a grafia que acaba persistida em `done` após o merge, quebrando a
    suíte existente (`test_fix_lote_a.py::RedoPathSeparatorTest`) sem
    corrigir bug nenhum. `mark_item`/`mark_item_status` não têm essa
    restrição (ver suas docstrings) porque nenhum chamador deles depende de
    preservar grafia nativa na primeira gravação."""
    with _lock(wd):
        st = load(wd) or {}
        s = st.setdefault("stages", {}).setdefault(stage, {})
        _reconcile_item_lists(s)
        done = set(s.get("done") or [])
        done_canon = {_canon_item(x) for x in done}

        resolved_items: list[str] = []
        seen_canon: set[str] = set()
        for raw in items:
            canon = _canon_item(raw)
            if not canon or canon in seen_canon:
                continue
            seen_canon.add(canon)
            matched = _resolve_item(s, raw)
            resolved_items.append(matched if matched is not None else raw)

        s["pending"] = sorted(x for x in resolved_items if _canon_item(x) not in done_canon)
        s["done"] = sorted(done)
        for status in ITEM_PROBLEM_STATUSES:
            problem = {x for x in (s.get(status) or []) if _canon_item(x) not in seen_canon}
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
