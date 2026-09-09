"""Captura de conteúdo analisado: snapshot determinístico, resolução de evidência e diff (plano §6.1 item 4, F02).

`codescan/surface.py::drift_report` (baseline) já sabia detectar sujeira do
worktree (`content_state`), mas continuava ancorado no HEAD: dois snapshots do
MESMO conteúdo, tirados de HEADs diferentes, comparavam como diferentes; e o
oposto — HEAD igual com worktree sujo diferente — só virava `warning`, nunca
identidade nova de verdade. Este módulo substitui essa base por identidade
puramente de CONTEÚDO:

    snapshot_id = sha256({(path, blob_hash) admitido no escopo})

`blob_hash` é o sha256 do conteúdo em disco de cada arquivo (não o sha1 do
objeto git) — assim o mesmo conjunto de bytes produz o mesmo `snapshot_id`
esteja o repositório sujo, limpo, em outro HEAD ou sem git nenhum. `head` fica
registrado à parte, como proveniência, nunca como parte da identidade.

Três garantias exigidas pelo plano, todas verificáveis nos testes:

1. Arquivo staged, unstaged OU não rastreado (respeitando `.gitignore`) entra
   no snapshot — `capture()` nunca se limita a `git show HEAD`.
2. Falha de `git` vira aviso explícito em `Snapshot.warnings` — nunca
   "sem mudança" silencioso (`diff()` propaga os avisos dos dois lados).
3. `resolve_evidence()` verifica que o arquivo em disco ainda bate com o hash
   registrado no snapshot antes de citar um trecho: citar conteúdo que já
   mudou sob o snapshot é erro (`SnapshotStale`), não evidência silenciosa.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import subprocess
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from knowledge.evidence import snippet_hash as _snippet_hash, validate_locator
from knowledge.models import LocatorInvalid, SourceKind

from .profile import matches_any as _matches_any

__all__ = [
    "FileState",
    "FileEntry",
    "Snapshot",
    "SnapshotError",
    "PathNotInSnapshot",
    "SnapshotStale",
    "EvidenceRangeInvalid",
    "capture",
    "resolve_evidence",
    "diff",
]


# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------


class SnapshotError(Exception):
    """Base dos erros deste módulo."""


class PathNotInSnapshot(SnapshotError):
    """`path` não está no conjunto de arquivos admitidos pelo snapshot."""


class SnapshotStale(SnapshotError):
    """Conteúdo em disco mudou desde a captura do snapshot: não é mais citável."""


class EvidenceRangeInvalid(SnapshotError):
    """Intervalo de linhas fora dos limites reais do arquivo citado."""


# --------------------------------------------------------------------------
# Modelo
# --------------------------------------------------------------------------


class FileState(str, enum.Enum):
    """Estado do arquivo no momento da captura, relativo ao HEAD e ao índice."""

    TRACKED_CLEAN = "tracked_clean"
    STAGED = "staged"
    UNSTAGED = "unstaged"
    UNTRACKED = "untracked"
    DELETED = "deleted"


@dataclass(frozen=True)
class FileEntry:
    """Um arquivo admitido no snapshot.

    `size`/`sha256` são `None` só para `state=DELETED`: não há conteúdo em
    disco para ler, mas o path continua registrado — é o que faz uma deleção
    pura mudar o `snapshot_id` em vez de simplesmente desaparecer sem rastro.
    """

    path: str
    size: int | None
    sha256: str | None
    state: FileState


@dataclass(frozen=True)
class Snapshot:
    """Captura imutável do conteúdo analisado de um repositório."""

    repo: str
    snapshot_id: str
    head: str | None
    git_available: bool
    files: tuple[FileEntry, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    #: Quantos caminhos ADMITIDOS pelo escopo foram removidos por `exclude`.
    #: Não entra em `snapshot_id` (identidade é conteúdo); existe para que a
    #: exclusão do perfil seja contada e auditável, nunca silenciosa (§6.1.3).
    excluded_by_profile: int = 0
    exclude_patterns: tuple[str, ...] = field(default_factory=tuple)

    def file_map(self) -> dict[str, FileEntry]:
        return {f.path: f for f in self.files}


# --------------------------------------------------------------------------
# Utilidades de caminho (independentes de codescan/surface.py)
# --------------------------------------------------------------------------


def _normalize_path(path: str) -> str:
    """Caminho relativo, separador '/', forma NFC — chave estável entre SOs."""
    return unicodedata.normalize("NFC", (path or "").replace("\\", "/")).strip("/")


def _unquote_git_path(raw: str) -> str:
    """Desfaz a citação C-style do git para caminho não-ASCII.

    Com `core.quotePath` padrão, um caminho acentuado sai entre aspas com
    escapes octais (`"servi\\303\\247o.py"`). Já forçamos
    `-c core.quotePath=false` nas chamadas; isto é a rede de segurança para
    git antigo ou config herdada que ignore a flag.
    """
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        body = raw[1:-1]
        try:
            return body.encode("latin-1").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return body
    return raw


def _within_scope(path: str, scopes: Sequence[str] | None) -> bool:
    """`True` se `path` é admitido pelo escopo.

    Escopo vazio/`None` admite tudo. Cada padrão é PREFIXO (`path == s` ou
    `path` começando por `s + "/"`, semântica histórica) quando não tem
    metacaractere, e GLOB `fnmatch` quando tem — a mesma função que
    `inventory` usa, para que include/exclude não divirjam entre os módulos.
    """
    if not scopes:
        return True
    return _matches_any(path, scopes)


def _normalize_scope(scope: Iterable[str] | None) -> tuple[str, ...] | None:
    if scope is None:
        return None
    return tuple(sorted({_normalize_path(s) for s in scope if (s or "").strip()}))


# --------------------------------------------------------------------------
# Git (reimplementado aqui; NÃO importa codescan/surface.py)
# --------------------------------------------------------------------------


def _run_git(repo: str, *args: str) -> str | None:
    """`git` com `.strip()` do stdout inteiro. Só para saídas sem formato de
    coluna fixa (rev-parse, ls-files). `None` = comando indisponível/falhou."""
    try:
        r = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _run_git_raw(repo: str, *args: str) -> str | None:
    """`git` SEM `.strip()` do stdout. `git status --porcelain` usa os 2
    primeiros bytes de CADA linha como código de estado (ex.: `' M arq.py'`);
    tirar o strip do stdout inteiro corta esse byte na primeira linha e
    desalinha o parser de coluna fixa. `None` = comando indisponível/falhou."""
    try:
        r = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _is_git_repo(repo: str) -> bool:
    return _run_git(repo, "rev-parse", "--is-inside-work-tree") == "true"


def _head(repo: str) -> str | None:
    """`None` quando não há commit algum ainda (repo git recém-criado)."""
    return _run_git(repo, "rev-parse", "HEAD")


def _tracked_files(repo: str) -> tuple[list[str], str | None]:
    out = _run_git_raw(repo, "-c", "core.quotePath=false", "ls-files")
    if out is None:
        return [], "`git ls-files` falhou: lista de arquivos rastreados indisponível"
    files = [
        _normalize_path(_unquote_git_path(ln))
        for ln in out.splitlines()
        if ln.strip()
    ]
    return files, None


def _status_entries(repo: str) -> tuple[dict[str, FileState], str | None]:
    """Mapa `path -> estado` a partir de `git status --porcelain`.

    Cobre staged, unstaged, não-rastreado e deletado num único comando
    (`--untracked-files=all` evita que um diretório novo inteiro vire uma
    linha só). Rename ('R  old -> new'): o lado antigo entra como DELETED, o
    novo herda o estado normal — é assim que uma renomeação "aparece" como
    deleção+adição em `diff()`, sem precisar de uma categoria própria.
    """
    out = _run_git_raw(repo, "-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all")
    if out is None:
        return {}, "`git status` falhou: staged/unstaged/untracked não verificável"
    result: dict[str, FileState] = {}
    for raw in out.splitlines():
        if len(raw) <= 3:
            continue
        code = raw[:2]
        body = raw[3:]
        if code == "??":
            p = _normalize_path(_unquote_git_path(body.strip()))
            if p:
                result[p] = FileState.UNTRACKED
            continue
        if code == "!!":
            continue  # ignorado explicitamente por git; fora do escopo por definição
        parts = body.split(" -> ")
        if len(parts) == 2:
            old_p = _normalize_path(_unquote_git_path(parts[0].strip()))
            new_p = _normalize_path(_unquote_git_path(parts[1].strip()))
            if old_p:
                result[old_p] = FileState.DELETED
            if new_p:
                result[new_p] = _state_from_code(code)
            continue
        p = _normalize_path(_unquote_git_path(body.strip()))
        if p:
            result[p] = _state_from_code(code)
    return result, None


def _state_from_code(code: str) -> FileState:
    index_c, tree_c = code[0], code[1]
    if index_c == "D" or tree_c == "D":
        return FileState.DELETED
    if tree_c != " ":
        return FileState.UNSTAGED
    if index_c != " ":
        return FileState.STAGED
    return FileState.TRACKED_CLEAN


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------


def _file_sha256(full_path: str) -> tuple[int, str] | None:
    """`(tamanho, sha256)` do arquivo, lido em blocos. `None` se ilegível."""
    try:
        h = hashlib.sha256()
        size = 0
        with open(full_path, "rb") as f:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
        return size, h.hexdigest()
    except OSError:
        return None


_DELETED_MARKER = "\x00deleted"


def _compute_snapshot_id(entries: Iterable[FileEntry]) -> str:
    """sha256 do conjunto `{(path, blob_hash)}` — pura identidade de conteúdo,
    nunca do HEAD. Ver docstring do módulo."""
    pieces = sorted(
        (e.path, e.sha256 if e.sha256 is not None else _DELETED_MARKER)
        for e in entries
    )
    blob = json.dumps(pieces, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Captura
# --------------------------------------------------------------------------


def capture(
    repo_path: str,
    scope: Iterable[str] | None = None,
    *,
    exclude: Iterable[str] | None = None,
) -> Snapshot:
    """Captura o conteúdo analisado de `repo_path`.

    `scope` (= `include` do perfil): padrões de caminho relativos, separador
    `/`. Cada padrão é PREFIXO quando não tem metacaractere (`path == prefixo`
    ou `path` começando por `prefixo + "/"`, comportamento histórico) e GLOB
    `fnmatch` quando tem (`src/**/*.py`). `None` admite o repositório inteiro.
    Filtrar por escopo não é "exclusão silenciosa" (§6.1 item 3): é o próprio
    contrato de `scope`, decidido por quem chama.

    `exclude`: mesmos padrões, aplicados DEPOIS de `scope` — exclusão vence
    inclusão. Quantos caminhos foram removidos por aqui fica em
    `Snapshot.excluded_by_profile` (e em `warnings`, quando > 0), porque
    exclusão contada é auditável e exclusão silenciosa não é.

    Com git: inclui todo arquivo rastreado no HEAD (limpo ou não) mais todo
    não-rastreado admitido pelo `.gitignore` — cobre exatamente "HEAD +
    staged + unstaged + untracked no escopo" (F02). Sem git: cai para
    varredura de diretório + hash, sem noção de staged/unstaged (registrado
    como limitação em `warnings`).
    """
    repo = os.path.abspath(repo_path)
    scopes = _normalize_scope(scope)
    excludes = _normalize_scope(exclude) or ()
    excluded_count = 0
    warnings: list[str] = []

    def _admitted(path: str) -> bool:
        """Escopo admite E exclusão não remove. Cada remoção é contada."""
        nonlocal excluded_count
        if not _within_scope(path, scopes):
            return False
        if excludes and _matches_any(path, excludes):
            excluded_count += 1
            return False
        return True

    if not os.path.isdir(repo):
        raise SnapshotError(f"repo_path não é um diretório existente: {repo_path!r}")

    git_available = _is_git_repo(repo)
    head: str | None = None
    entries: list[FileEntry] = []

    if git_available:
        head = _head(repo)
        if head is None:
            warnings.append("repositório git sem nenhum commit: HEAD indisponível")

        tracked, tracked_warn = _tracked_files(repo)
        if tracked_warn:
            warnings.append(tracked_warn)
        status, status_warn = _status_entries(repo)
        if status_warn:
            warnings.append(status_warn)

        candidate_paths = {p for p in sorted(tracked) if _admitted(p)}
        candidate_paths |= {p for p in sorted(status) if _admitted(p) and p not in candidate_paths}

        for p in sorted(candidate_paths):
            state = status.get(p, FileState.TRACKED_CLEAN)
            if state is FileState.DELETED:
                entries.append(FileEntry(path=p, size=None, sha256=None, state=state))
                continue
            full = os.path.join(repo, *p.split("/"))
            hashed = _file_sha256(full)
            if hashed is None:
                warnings.append(f"{p!r} não pôde ser lido do disco: tratado como deletado")
                entries.append(FileEntry(path=p, size=None, sha256=None, state=FileState.DELETED))
                continue
            size, sha = hashed
            entries.append(FileEntry(path=p, size=size, sha256=sha, state=state))
    else:
        warnings.append(
            "sem git neste diretório: snapshot por varredura + hash, sem staged/unstaged/HEAD"
        )
        for dirpath, dirnames, filenames in os.walk(repo):
            if ".git" in dirnames:
                dirnames.remove(".git")
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = _normalize_path(os.path.relpath(full, repo))
                if not _admitted(rel):
                    continue
                hashed = _file_sha256(full)
                if hashed is None:
                    warnings.append(f"{rel!r} não pôde ser lido do disco: omitido do snapshot")
                    continue
                size, sha = hashed
                entries.append(FileEntry(path=rel, size=size, sha256=sha, state=FileState.UNTRACKED))

    entries.sort(key=lambda e: e.path)
    if excluded_count:
        warnings.append(
            f"{excluded_count} caminho(s) admitido(s) pelo escopo foram removidos por "
            f"exclude={list(excludes)!r} (perfil de análise): a exclusão é contada em "
            "Snapshot.excluded_by_profile, não silenciosa"
        )
    snapshot_id = _compute_snapshot_id(entries)
    return Snapshot(
        repo=repo,
        snapshot_id=snapshot_id,
        head=head,
        git_available=git_available,
        files=tuple(entries),
        warnings=tuple(warnings),
        excluded_by_profile=excluded_count,
        exclude_patterns=tuple(excludes),
    )


# --------------------------------------------------------------------------
# Evidência
# --------------------------------------------------------------------------


def resolve_evidence(snapshot: Snapshot, path: str, start: int, end: int) -> dict:
    """Localizador de código (formato `knowledge.evidence`) + o trecho exato.

    `commit` do localizador carrega o `snapshot_id` de CONTEÚDO — não o hash
    do HEAD — porque é isso que faz a citação detectar qualquer alteração do
    conteúdo usado (F02), inclusive dirty worktree sem commit novo.

    Antes de citar, confere que o arquivo em disco ainda bate com o hash
    registrado no snapshot: citar um trecho que já mudou por baixo do
    snapshot seria evidência silenciosamente errada (`SnapshotStale`).

    Retorna `{"locator": <dict validado por knowledge.evidence>, "snippet": <texto>}`.
    """
    norm = _normalize_path(path)
    entry = snapshot.file_map().get(norm)
    if entry is None:
        raise PathNotInSnapshot(
            f"{norm!r} não está no snapshot {snapshot.snapshot_id} (escopo ou path incorretos)"
        )
    if entry.state is FileState.DELETED or entry.sha256 is None:
        raise PathNotInSnapshot(
            f"{norm!r} está deletado no snapshot {snapshot.snapshot_id}: sem conteúdo para citar"
        )

    full = os.path.join(snapshot.repo, *norm.split("/"))
    try:
        with open(full, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise SnapshotStale(f"{norm!r} não pôde ser lido do disco após a captura: {exc}") from exc

    current_hash = hashlib.sha256(raw).hexdigest()
    if current_hash != entry.sha256:
        raise SnapshotStale(
            f"{norm!r} mudou desde a captura do snapshot {snapshot.snapshot_id} "
            f"(registrado {entry.sha256[:12]}, atual {current_hash[:12]}): "
            "evidência não pode citar conteúdo diferente do capturado"
        )

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        raise EvidenceRangeInvalid(f"intervalo de código inválido: {start}..{end}")
    if end > len(lines):
        raise EvidenceRangeInvalid(
            f"intervalo {start}..{end} excede o arquivo ({len(lines)} linhas): {norm!r}"
        )

    snippet = "".join(lines[start - 1:end])
    locator = {
        "repo": snapshot.repo,
        "commit": snapshot.snapshot_id,
        "path": norm,
        "start_line": start,
        "end_line": end,
        "snippet_hash": _snippet_hash(snippet),
    }
    try:
        validated = validate_locator(SourceKind.CODE, locator)
    except LocatorInvalid:
        raise
    return {"locator": validated, "snippet": snippet}


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------


def diff(snap_a: Snapshot, snap_b: Snapshot) -> dict:
    """Compara dois snapshots por conteúdo.

    `added`/`removed`/`changed`: listas de paths ordenadas. Um `path`
    presente e não-deletado só num dos lados entra em `added`/`removed` — é
    assim que uma deleção (entra como `FileState.DELETED`, não desaparece do
    snapshot) e uma renomeação (par deleção-do-antigo + adição-do-novo) ficam
    representadas sem categoria extra. `changed`: mesmo path nos dois lados,
    `sha256` diferente.

    `warnings`: união dos avisos de captura dos dois snapshots. Nunca
    interpretar `added=removed=changed=[]` como "sem mudança" quando
    `warnings` não está vazio — pode significar "não verificável" (regra
    geral do plano: erro de git nunca vira silêncio).
    """
    a_map, b_map = snap_a.file_map(), snap_b.file_map()
    a_present = {p for p, e in a_map.items() if e.state is not FileState.DELETED}
    b_present = {p for p, e in b_map.items() if e.state is not FileState.DELETED}

    added = sorted(b_present - a_present)
    removed = sorted(a_present - b_present)
    changed = sorted(
        p for p in (a_present & b_present) if a_map[p].sha256 != b_map[p].sha256
    )
    warnings = tuple(snap_a.warnings) + tuple(snap_b.warnings)
    return {"added": added, "removed": removed, "changed": changed, "warnings": warnings}
