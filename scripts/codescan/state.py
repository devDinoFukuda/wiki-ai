"""Checkpoint entre sessões.

Sem isto, analisar repo grande é impossível: a sessão morre no meio do estágio
2 e você recomeça do zero. O estado vive em disco, não na conversa.

O work dir fica FORA do repositório analisado. O legado é read-only — nada é
escrito lá dentro, nem um arquivo de estado.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import shutil
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

# F01 (W0): `evidence`/`verify` não têm itens (done/pending) nem gate de
# auditoria (sdd.CRITICAL_STAGES) — antes desta correção, `done <stage>`
# genérico (cli.cmd_done) não tinha NENHUM blocker próprio para os dois, e
# `st.mark(wd, stage, "done", <qualquer coisa>)` fechava o estágio inteiro
# sem que nenhuma execução real (`evidence`/`verify --artifact`) tivesse
# rodado. Estágios aqui só podem ser fechados por `done` quando existe um
# registro de verificação (`record_verification`) cujo hash ainda bate com o
# conteúdo atual do artefato em disco (`verification_ok`) — ver cli.cmd_done.
VERIFIED_STAGES = ("evidence", "verify")


def workdir(store: str, repo: str) -> str:
    """Work dir derivado do repo: <store>/.codescan/<nome>-<hash>.

    Chave estável: URL usa a própria string; caminho local usa o absoluto.
    Assim o mesmo repo (URL ou path) resolve sempre para o mesmo workdir.

    BUG F-27: no Windows (e em qualquer FS case-insensitive), `c:/x` e
    `C:/X` são o MESMO diretório no disco, mas `os.path.abspath` preserva a
    grafia recebida — sem normalizar, o sha1 da chave produzia dois hashes
    diferentes para o mesmo repo, e portanto dois workdirs (dois
    state.json) para o mesmo checkpoint, dependendo só de como o usuário
    digitou `--repo` numa chamada e noutra. `os.path.normcase` resolve isso:
    em Windows baixa para minúsculas e troca '/' por '\\'; em POSIX é
    no-op (mantém case-sensitive, correto lá). URLs NÃO passam por
    normcase — a chave permanece a string crua da URL (URLs são
    case-sensitive por natureza; normalizar poderia colidir hosts/paths
    legitimamente distintos).

    NOTA DE MIGRAÇÃO: workdirs já criados ANTES desta correção com uma
    grafia de case divergente da que será usada daqui pra frente (ex.:
    repo passado como "C:\\Code\\Repo" numa sessão antiga e "c:\\code\\repo"
    depois) ficam ÓRFÃOS — o novo hash não bate com o antigo, então o
    checkpoint antigo não é mais encontrado automaticamente. Não há
    reaproveitamento automático aqui (fora do escopo desta correção,
    que é só `state.py`); quem tiver um workdir órfão precisa localizá-lo
    manualmente em `<store>/.codescan/` (pelo prefixo `<nome>-`) e apagá-lo
    ou migrar o `state.json` para o novo diretório à mão.
    """
    if is_url(repo):
        key, name = repo, url_name(repo)
    else:
        key = os.path.normcase(os.path.abspath(repo))
        name = os.path.basename(key.rstrip("/\\")) or "repo"
    h = hashlib.sha1(key.encode()).hexdigest()[:8]
    return os.path.join(store, ".codescan", f"{name}-{h}")


def _path(wd: str) -> str:
    return os.path.join(wd, "state.json")


def _pid_alive(pid: int) -> bool:
    """True se ainda existe um processo vivo com este pid.

    `os.kill(pid, 0)` (o truque POSIX de "sinal 0 só verifica") não é
    confiável em Windows: `os.kill` lá não implementa sinal 0 de verificação
    de existência da forma esperada. Em vez disso, tentamos abrir um handle
    para o processo via `OpenProcess` (API do Windows, por ctypes) com o
    direito mínimo (`PROCESS_QUERY_LIMITED_INFORMATION`) — se abrir, o
    processo existe; se falhar (ERROR_INVALID_PARAMETER/ACCESS_DENIED por
    pid inexistente), tratamos como morto. Em POSIX, `os.kill(pid, 0)`
    funciona como documentado: ESRCH = não existe, EPERM = existe mas sem
    permissão (ainda vivo)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_owner_path(lockdir: str) -> str:
    return os.path.join(lockdir, "owner.json")


def _write_lock_owner(lockdir: str) -> None:
    owner = {"pid": os.getpid(), "created_at": time.time()}
    with contextlib.suppress(OSError):
        with open(_lock_owner_path(lockdir), "w", encoding="utf-8") as f:
            json.dump(owner, f)


def _read_lock_owner(lockdir: str) -> dict | None:
    try:
        with open(_lock_owner_path(lockdir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


@contextlib.contextmanager
def _lock(wd: str, timeout: float = 30.0, poll: float = 0.05):
    """Trava entre processos para read-modify-write do estado.

    Agentes paralelos marcam `done` de módulos diferentes ao mesmo tempo. Sem
    trava, dois `load()->save()` concorrentes se sobrescrevem e um `done` some —
    o pior tipo de bug num pipeline com checkpoint. `os.mkdir` é atômico em
    Windows e POSIX; quem cria o diretório detém a trava.

    BUG F-09 (CRÍTICO, roubo de trava viva): antes desta correção, o
    critério para quebrar uma trava travada era só "passou `timeout`" — sem
    checar se quem a detém ainda está vivo. Processo B, depois de 30s,
    apagava a trava do processo A AINDA EM EXECUÇÃO (ex.: A só estava
    demorando — repo grande, disco lento — não morto) e criava a sua
    própria. Pior: quando o `finally` de A eventualmente rodava, ele
    apagava a trava de B (que A nunca soube que existia) — dois processos
    escrevendo `state.json` concorrentemente SEM proteção nenhuma, exatamente
    o cenário que esta trava existe para prevenir.

    Correção: cada dono grava `owner.json` (pid + created_at) DENTRO do
    diretório da trava. Ao encontrar a trava ocupada e o timeout local
    esgotado, só quebramos se o pid do dono já não existir mais (via
    `_pid_alive` — órfã de sessão morta de verdade) — nunca só por idade.
    Se o dono ainda está vivo, levantamos `TimeoutError` com erro claro em
    vez de roubar. No `finally`, só removemos a trava se `owner.json` ainda
    apontar para o NOSSO próprio pid — se por qualquer razão outro processo
    já a quebrou e recriou entretanto, não mexemos nela (evita o mesmo
    "apaga a trava do outro" na ponta de saída)."""
    os.makedirs(wd, exist_ok=True)
    lockdir = os.path.join(wd, ".state.lock")
    deadline = time.time() + timeout
    while True:
        try:
            os.mkdir(lockdir)
            break
        except (FileExistsError, PermissionError):
            if time.time() >= deadline:
                # Só lemos/decidimos sobre `owner.json` aqui, ao esgotar o
                # timeout — NÃO a cada iteração do poll. Ler o arquivo do
                # dono em todo tick (a cada `poll` segundos, com N processos/
                # threads concorrentes) é I/O extra que compete com o
                # detentor real da trava por disco/GIL; sob concorrência alta
                # (ex.: dezenas de agentes paralelos marcando itens) isso
                # sozinho já atrasava o detentor o bastante para o timeout
                # disparar por inanição, não por trava realmente presa.
                owner = _read_lock_owner(lockdir)
                owner_pid = owner.get("pid") if owner else None
                owner_dead = owner_pid is None or not _pid_alive(int(owner_pid))
                if not owner_dead:
                    raise TimeoutError(
                        f"não obtive a trava do estado em {timeout}s "
                        f"(detida pelo processo vivo pid={owner_pid} — não "
                        "roubada; se ele de fato travou, encerre-o e apague "
                        f"{lockdir!r} manualmente)"
                    )
                # Trava órfã de sessão morta de verdade (dono não existe
                # mais, ou owner.json sumiu/corrompeu junto com o crash):
                # só agora é seguro quebrar.
                with contextlib.suppress(OSError):
                    os.remove(_lock_owner_path(lockdir))
                with contextlib.suppress(OSError):
                    os.rmdir(lockdir)
                continue
            time.sleep(poll)
    _write_lock_owner(lockdir)
    try:
        yield
    finally:
        owner = _read_lock_owner(lockdir)
        if owner is not None and owner.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                os.remove(_lock_owner_path(lockdir))
            with contextlib.suppress(OSError):
                os.rmdir(lockdir)


class StateCorruptError(Exception):
    """state.json existe mas está ilegível/corrompido (JSON inválido, I/O,
    encoding, etc.) — ver a mensagem para o caminho e a sugestão de
    restaurar `state.json.bak` (gravado por `save()` a cada escrita bem-
    sucedida)."""


def load(wd: str) -> dict | None:
    """Lê o estado do work dir.

    `None` só significa "arquivo não existe" — checkpoint genuinamente
    ainda não iniciado. Isso é distinto de "arquivo existe mas não dá pra
    ler": nesse segundo caso levantamos `StateCorruptError` em vez de
    devolver None.

    BUG F-10 (causa-raiz): antes desta correção, qualquer exceção de
    leitura/parse era engolida e virava None — indistinguível de "sem
    estado". Os mutadores deste módulo fazem `load(wd) or {}` para tratar
    "sem estado" como "começa vazio"; com o engolimento, um state.json
    CORROMPIDO (mas com progresso real gravado) também virava `{}`, e a
    escrita seguinte (`save()`) sobrescrevia o arquivo corrompido com esse
    estado em branco — perda total e silenciosa do checkpoint, sem
    qualquer erro reportado ao usuário. Levantando aqui, a exceção
    atravessa o `load(wd) or {}` dos mutadores sem ser mascarada (`or`
    nunca avalia o lado direito quando o esquerdo levanta) e chega ao
    chamador — que agora precisa decidir explicitamente (ex.: restaurar o
    `.bak`) em vez de perder dado sem saber."""
    p = _path(wd)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        bak = p + ".bak"
        if os.path.exists(bak):
            hint = f"backup disponível em {bak!r} — se íntegro, restaure copiando-o por cima de {p!r}"
        else:
            hint = f"nenhum backup encontrado em {bak!r}"
        raise StateCorruptError(
            f"estado corrompido/ilegível em {p!r}: {e}. {hint}."
        ) from e


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
    """Grava o estado em disco (via tmp + os.replace atômico).

    BUG F-10 (mitigação complementar): antes de sobrescrever, copia o
    state.json ANTERIOR (se existir) para state.json.bak. Na sequência
    normal load()->mutação->save() usada por todo mutador deste módulo, o
    arquivo que está prestes a ser substituído já foi lido com sucesso por
    `load()` nesta mesma operação (ou nunca existiu) — ou seja, o que vira
    `.bak` aqui é sempre o último estado ÍNTEGRO conhecido, nunca lixo.
    Isso dá uma via de recuperação manual (ver `StateCorruptError`) se um
    `state.json` futuro corromper por qualquer motivo externo (escrita
    parcial fora deste módulo, disco cheio, etc.) — sem isto, uma
    corrupção do arquivo principal não deixaria nenhuma cópia boa para
    trás."""
    os.makedirs(wd, exist_ok=True)
    st["updated_at"] = _now()
    target = _path(wd)
    if os.path.exists(target):
        with contextlib.suppress(OSError):
            shutil.copyfile(target, target + ".bak")
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)  # atômico: sessão morta não corrompe o estado


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


def _safe_sha256(path: str | None) -> str | None:
    """`sha256_file` sem levantar: `None` se `path` é vazio, sumiu, ou não dá
    pra ler. Usado nas checagens de integridade abaixo, onde "não dá pra
    confirmar o hash" e "hash não bate" têm o mesmo efeito prático (artefato
    verificado não pode mais ser considerado válido)."""
    if not path:
        return None
    try:
        return sha256_file(path)
    except OSError:
        return None


# --- F01 (W0): vínculo de verificação para `evidence`/`verify` -------------
#
# `record_verification` é chamada SÓ pela execução real (`cli.cmd_evidence`/
# `cli.cmd_verify`, logo depois de gerar/validar o artefato) — nunca por
# `done`/`problem-status`. `verification_ok` é o gate que `cli.cmd_done`
# consulta antes de fechar `evidence`; `revalidate_verified_stages` é a
# auto-cura: reaberta a cada invocação do CLI (ver `cli.main`), ela derruba
# de volta para `failed` qualquer estágio cujo artefato verificado mudou de
# conteúdo (ou sumiu) desde o último `done` — sem isso, editar
# `sdd/confirmed.md` depois do `verify` deixaria `stages.verify.status`
# congelado em "done" para sempre, mentindo sobre o que foi de fato checado.
def record_verification(
    wd: str, stage: str, key: str, path: str, scope: dict | None = None
) -> dict:
    """Grava, sob trava, o hash sha256 + escopo do artefato que embasou uma
    execução real de `stage` (`evidence` ou `verify`). `key` identifica o
    artefato dentro do estágio (a mesma chave usada em
    `stages.verify.artifacts` para `verify`; fixa para `evidence`, que tem um
    único artefato por vez). Levanta `OSError` se `path` não existir/não der
    pra ler — quem chama já deve ter confirmado que o artefato foi escrito."""
    digest = sha256_file(path)
    with _lock(wd):
        st = load(wd) or {}
        s = st.setdefault("stages", {}).setdefault(stage, {})
        verified = s.setdefault("verified", {})
        verified[key] = {
            "path": os.path.abspath(path),
            "sha256": digest,
            "scope": scope,
            "at": _now(),
        }
        s.pop("invalidated", None)
        save(wd, st)
        return st


def verification_ok(wd: str, stage: str) -> tuple[bool, str | None]:
    """`(True, None)` só se `stage` tem ao menos um artefato verificado
    (`record_verification`) e todo hash gravado ainda bate com o conteúdo
    ATUAL em disco. Base do gate de `done <stage>` para `VERIFIED_STAGES`:
    sem isto, `done evidence` bastava para fechar o estágio sem que
    `evidence` tivesse rodado de verdade."""
    st = load(wd) or {}
    s = (st.get("stages") or {}).get(stage) or {}
    verified = s.get("verified")
    if not isinstance(verified, dict) or not verified:
        return False, f"nenhuma verificação registrada para '{stage}': execute o comando real antes de done"
    stale = sorted(
        key
        for key, entry in verified.items()
        if not isinstance(entry, dict)
        or _safe_sha256(entry.get("path")) != entry.get("sha256")
    )
    if stale:
        return False, "artefato(s) alterado(s)/ausente(s) desde a verificação: " + ", ".join(stale)
    return True, None


def revalidate_verified_stages(wd: str) -> dict | None:
    """Auto-cura: derruba `status` de `evidence`/`verify` de "done" para
    "failed" quando o artefato que embasou aquele `done` mudou de conteúdo
    (ou sumiu) desde então. Chamada no início de toda invocação do
    CLI (ver `cli.main`), ANTES de qualquer subcomando rodar — assim a
    invalidação por edição de artefato aparece na primeira chamada seguinte
    ao codescan, sem exigir um comando dedicado. Retorno best-effort: `None`
    se não há state.json ou nada a revalidar; nunca levanta (chamador não
    deve deixar uma falha aqui derrubar o comando real que o usuário pediu)."""
    if not os.path.isfile(_path(wd)):
        return None
    try:
        with _lock(wd):
            st = load(wd)
            if not st:
                return None
            changed = False
            for stage in VERIFIED_STAGES:
                s = (st.get("stages") or {}).get(stage)
                if not s or s.get("status") != "done":
                    continue
                verified = s.get("verified")
                if not isinstance(verified, dict) or not verified:
                    continue
                stale = sorted(
                    key
                    for key, entry in verified.items()
                    if not isinstance(entry, dict)
                    or _safe_sha256(entry.get("path")) != entry.get("sha256")
                )
                if not stale:
                    continue
                # "failed", não "in_progress": `wk/cli.py` (fora do meu
                # escopo de edição) só bloqueia promote/compile/docx quando
                # `stages.verify.status == "failed"` (ver
                # `_failed_verify_workdirs` lá) — "in_progress" passaria
                # batido por aquele portão, como se o estágio nunca tivesse
                # sido tocado. "failed" é também semanticamente correto: o
                # resultado que tinha passado não vale mais para o conteúdo
                # atual do artefato.
                s["status"] = "failed"
                s["invalidated"] = {
                    "artefatos": stale,
                    "motivo": "conteúdo alterado ou artefato ausente desde a verificação anterior",
                    "at": _now(),
                }
                artifacts = s.get("artifacts")
                if isinstance(artifacts, dict):
                    for key in stale:
                        if artifacts.get(key) == "done":
                            artifacts[key] = "stale"
                changed = True
            if changed:
                save(wd, st)
            return st
    except (OSError, StateCorruptError):
        # best-effort: um state.json corrompido/ilocável não deve impedir o
        # comando real de rodar — quem de fato precisa do estado (cmd_state,
        # cmd_done, ...) vai chamar `load()` de novo e reportar o erro certo.
        return None
