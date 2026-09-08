"""Preferência de agente, binding e leases persistidos por store/repo (spec §10.4.6).

Arquivo único, versionado e com escrita atômica:
`<store_root>/agents.json` (`schema_version`, `store`, `repos`, `leases`).
Escrita atômica = arquivo temporário no MESMO diretório + `os.replace`, para
que uma interrupção nunca deixe o registro do agente pela metade (o §7.3 exige
retomada idempotente após interrupção de processo).

Regras do §10.4.6 que estão em código, não em prosa:

* `set_preference` registra a PREFERÊNCIA (`init --agent`); `save_binding`
  registra o BINDING e só aceita binding com `connected=True` — a seleção
  persistida muda depois do handshake, nunca antes.
* `resolve` devolve a ORIGEM (`repo` / `store` / `override` / `none`).
  Operação com repo usa o binding do repo ou, na ausência, o do store;
  operação sem repo usa o do store. Nada é deduzido de executável no PATH:
  este módulo não olha `PATH`, `shutil.which` nem nome de binário.
* `cancel_active` invalida os leases ativos do escopo (é o
  `agent connect --cancel-active`): decisão explícita de interromper
  trabalho, nunca passo automático da troca de binding.
* Trocar de binding NÃO mexe em orçamento nem em `input_revision` — este
  módulo não guarda nenhum dos dois; eles vivem em `runtime.tasks`.

Somente stdlib. Sem rede.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

from .agents import AgentBinding

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None

SCHEMA_VERSION = 1
STORE_FILENAME = "agents.json"

#: Escopo do store (operação sem `--repo`).
STORE_SCOPE = "__store__"

ORIGIN_REPO = "repo"
ORIGIN_STORE = "store"
ORIGIN_OVERRIDE = "override"
ORIGIN_NONE = "none"

LEASE_ACTIVE = "active"
LEASE_CLOSED = "closed"
LEASE_CANCELLED = "cancelled"

#: Sufixo do arquivo de trava entre PROCESSOS (`agents.json.lock`).
LOCK_SUFFIX = ".lock"

#: Espera máxima pela trava antes de desistir. Ultrapassá-la é `BindingError`
#: (com o caminho da trava), nunca gravar por cima do outro processo.
LOCK_TIMEOUT_S = 10.0
_LOCK_RETRY_S = 0.02


def normalize_repo(repo: str | None) -> str | None:
    """Chave canônica de um repositório. `None`/vazio continuam sendo o store.

    `str(repo)` cru fazia `C:\\Repo` e `c:\\repo` (o MESMO diretório no
    Windows) virarem dois escopos distintos: o operador conectava o agente por
    um caminho e a operação seguinte, digitada com outra caixa ou com barras
    invertidas, não encontrava binding nenhum. `normcase(abspath(...))` resolve
    caixa e separador pelo SO; a barra normal no fim mantém a MESMA forma que
    `wk.cli._repo_key` grava no perfil de análise, então os dois lados do
    sistema falam do mesmo repositório com a mesma string.
    """
    if repo in (None, ""):
        return None
    return os.path.normcase(os.path.abspath(str(repo))).replace("\\", "/")


@contextlib.contextmanager
def _file_lock(path: str, timeout_s: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Exclusão mútua ENTRE PROCESSOS para o ciclo ler-modificar-gravar.

    `os.replace` deixa o arquivo sempre íntegro, mas não impede o último a
    gravar de apagar o trabalho do outro: dois `wk` simultâneos liam o mesmo
    `agents.json`, cada um acrescentava o SEU lease e o segundo `replace`
    fazia o lease do primeiro desaparecer — lease perdido é resultado tardio
    aceito depois de `--cancel-active`. A trava é um arquivo vizinho
    (`agents.json.lock`) com `fcntl.flock` no POSIX e `msvcrt.locking` no
    Windows; sem nenhum dos dois, o `O_CREAT|O_EXCL` do próprio arquivo serve
    de trava.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    deadline = time.monotonic() + timeout_s
    handle = None
    exclusive = False
    while True:
        try:
            handle = open(path, "a+b")
        except OSError as exc:  # pragma: no cover - permissão/FS exótico
            raise BindingError(f"trava {path!r} não pôde ser aberta: {exc}") from exc
        try:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
                exclusive = True
            elif _msvcrt is not None:
                handle.seek(0)
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
                exclusive = True
            else:  # pragma: no cover - nem fcntl nem msvcrt
                exclusive = True
        except OSError:
            handle.close()
            handle = None
            if time.monotonic() >= deadline:
                raise BindingError(
                    f"trava {path!r} ocupada por outro processo há mais de {timeout_s}s; "
                    "nada foi gravado (o registro do outro processo permanece íntegro)"
                )
            time.sleep(_LOCK_RETRY_S)
            continue
        break
    try:
        yield
    finally:
        try:
            if exclusive and _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            elif exclusive and _msvcrt is not None:
                handle.seek(0)
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover - liberação best-effort
            pass
        handle.close()


def persistable_binding(binding: AgentBinding) -> dict[str, Any]:
    """Forma GRAVÁVEL de um binding: campos públicos, `vendor` fora.

    `binding.to_dict()` levava `vendor` inteiro para o disco — e `vendor` é a
    área livre do fornecedor, exatamente onde um adaptador guarda
    `api_key`/`token`/`cookie` do host. `agents.json` fica em texto puro no
    store, então isso era segredo persistido. Aqui vale a regra do §10.3: o
    registro guarda IDENTIDADE e CAPACIDADE (quem é o agente, que transporte,
    que versão, o que negociou), nunca credencial. Quem precisa do segredo é o
    host, que o tem por conta própria — o wiki-ai não o reemite.
    """
    data = binding.to_public_dict()
    data["vendor"] = {}
    capabilities = dict(data.get("capabilities") or {})
    capabilities["vendor"] = {}
    data["capabilities"] = capabilities
    return data


class BindingError(RuntimeError):
    """Operação recusada por violar o ciclo preferência → handshake → binding."""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now(),
        "store": {"preference": None, "binding": None},
        "repos": {},
        "leases": {},
    }


@dataclass(frozen=True)
class ResolvedBinding:
    """Seleção efetiva de agente, com a origem sempre explícita."""

    binding: AgentBinding | None
    origin: str
    agent_id: str | None
    detail: str = ""
    preference: str | None = None

    @property
    def connected(self) -> bool:
        return self.binding is not None and self.binding.connected

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_public_dict() if self.binding else None,
            "origin": self.origin,
            "agent_id": self.agent_id,
            "preference": self.preference,
            "connected": self.connected,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LeaseRecord:
    """Lease de execução vinculado a um binding e a um escopo."""

    lease_id: str
    binding_id: str
    scope: str
    state: str = LEASE_ACTIVE
    task_id: str = ""
    opened_at: str = ""
    closed_at: str | None = None
    reason: str = ""
    vendor: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "binding_id": self.binding_id,
            "scope": self.scope,
            "state": self.state,
            "task_id": self.task_id,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "reason": self.reason,
            "vendor": dict(self.vendor),
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Forma GRAVÁVEL do lease: tudo menos `vendor`.

        Mesma regra de `persistable_binding()` (§10.3): `agents.json` fica em
        texto puro no store e `vendor` é a área livre do fornecedor —
        exatamente onde um adaptador guarda `api_key`/`token`/`cookie`. O
        lease guarda IDENTIDADE e VIGÊNCIA (qual binding, qual tarefa, qual
        escopo, aberto/fechado quando e por quê), nunca credencial. Quem
        precisa do segredo é o host, que o tem por conta própria.
        """
        data = self.to_dict()
        data["vendor"] = {}
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LeaseRecord":
        return cls(
            lease_id=str(data.get("lease_id") or ""),
            binding_id=str(data.get("binding_id") or ""),
            scope=str(data.get("scope") or STORE_SCOPE),
            state=str(data.get("state") or LEASE_ACTIVE),
            task_id=str(data.get("task_id") or ""),
            opened_at=str(data.get("opened_at") or ""),
            closed_at=data.get("closed_at"),
            reason=str(data.get("reason") or ""),
            vendor=dict(data.get("vendor") or {}),
        )


class BindingStore:
    """Persistência de preferência/binding/leases sob `store_root`."""

    def __init__(self, store_root: str | os.PathLike[str], *, filename: str = STORE_FILENAME):
        self.store_root = str(store_root)
        self.path = os.path.join(self.store_root, filename)
        self.lock_path = self.path + LOCK_SUFFIX

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[dict[str, Any]]:
        """Ler-modificar-gravar sob trava entre processos. O `yield` é o estado.

        Todo caminho de escrita deste módulo passa por aqui: sem a trava, dois
        processos liam a MESMA versão e o segundo `os.replace` descartava a
        alteração do primeiro (last-writer-wins) — preferência, binding e
        lease se perdiam sem erro nenhum.
        """
        with _file_lock(self.lock_path):
            state = self._read()
            yield state
            self._write(state)

    # -- IO ----------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return _empty_state()
        except (json.JSONDecodeError, OSError) as exc:
            raise BindingError(
                f"registro de agentes ilegível em {self.path!r}: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(data, Mapping):
            raise BindingError(f"registro de agentes inválido em {self.path!r}")
        version = int(data.get("schema_version", 0))
        if version != SCHEMA_VERSION:
            raise BindingError(
                f"registro de agentes na versão {version}; este código suporta "
                f"{SCHEMA_VERSION} — migração é explícita"
            )
        state = _empty_state()
        state.update({k: v for k, v in data.items() if k in state})
        state["store"] = dict(data.get("store") or state["store"])
        state["repos"] = dict(data.get("repos") or {})
        state["leases"] = dict(data.get("leases") or {})
        return state

    def _write(self, state: Mapping[str, Any]) -> None:
        payload = dict(state)
        payload["schema_version"] = SCHEMA_VERSION
        payload["updated_at"] = _now()
        os.makedirs(self.store_root, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.store_root, prefix=".agents-", suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    # -- escopos ------------------------------------------------------------

    @staticmethod
    def _scope(repo: str | None) -> str:
        key = normalize_repo(repo)
        return STORE_SCOPE if key is None else key

    @staticmethod
    def _find_repo_key(state: Mapping[str, Any], key: str) -> str:
        """Chave já gravada que designa o MESMO repositório de `key`.

        Registros escritos antes da normalização guardam o caminho como o
        operador o digitou. Ler por equivalência (e não só por igualdade)
        evita que a correção apague binding e leases de quem já tinha o
        arquivo — a regravação usa a forma normalizada.
        """
        repos = state.get("repos") or {}
        if key in repos:
            return key
        for stored in repos:
            if normalize_repo(stored) == key:
                return str(stored)
        return key

    def _slot(self, state: Mapping[str, Any], repo: str | None) -> dict[str, Any]:
        key = normalize_repo(repo)
        if key is None:
            return dict(state.get("store") or {})
        stored = self._find_repo_key(state, key)
        return dict((state.get("repos") or {}).get(stored) or {})

    def _put_slot(self, state: dict[str, Any], repo: str | None, slot: Mapping[str, Any]) -> None:
        key = normalize_repo(repo)
        if key is None:
            state["store"] = dict(slot)
        else:
            repos = dict(state.get("repos") or {})
            legacy = self._find_repo_key(state, key)
            if legacy != key:
                repos.pop(legacy, None)
            repos[key] = dict(slot)
            state["repos"] = repos

    # -- preferência (init --agent) -----------------------------------------

    def set_preference(self, agent_id: str, repo: str | None = None) -> dict[str, Any]:
        """Registra a preferência declarada. NÃO significa conectado (§10.1)."""
        if not str(agent_id or "").strip():
            raise BindingError("preferência de agente vazia")
        with self._transaction() as state:
            slot = self._slot(state, repo)
            slot["preference"] = {"agent_id": str(agent_id), "declared_at": _now()}
            self._put_slot(state, repo, slot)
        return dict(slot["preference"])

    def get_preference(self, repo: str | None = None) -> dict[str, Any] | None:
        slot = self._slot(self._read(), repo)
        pref = slot.get("preference")
        return dict(pref) if isinstance(pref, Mapping) else None

    # -- binding (agent connect) --------------------------------------------

    def save_binding(self, binding: AgentBinding, repo: str | None = None) -> AgentBinding:
        """Persiste o binding APÓS handshake válido (§10.4.6).

        Binding não conectado é recusado: é exatamente o "não registra conexão
        antes do handshake". A preferência do escopo passa a ser o agente do
        binding — a seleção persistida muda aqui, não em `connect` que falhou.
        """
        if not isinstance(binding, AgentBinding):
            raise BindingError("save_binding exige AgentBinding")
        if not binding.connected:
            raise BindingError(
                f"binding de {binding.agent_id!r} não está conectado; "
                "a seleção persistida só muda após handshake válido"
            )
        with self._transaction() as state:
            slot = self._slot(state, repo)
            # NUNCA `binding.to_dict()`: `vendor` (do binding e das
            # capacidades) é a área do fornecedor, e este arquivo é texto puro.
            slot["binding"] = persistable_binding(binding)
            slot["preference"] = {"agent_id": binding.agent_id, "declared_at": _now()}
            self._put_slot(state, repo, slot)
        return binding

    def get_binding(self, repo: str | None = None) -> AgentBinding | None:
        slot = self._slot(self._read(), repo)
        raw = slot.get("binding")
        return AgentBinding.from_dict(raw) if isinstance(raw, Mapping) else None

    def clear_binding(self, repo: str | None = None) -> bool:
        with self._transaction() as state:
            slot = self._slot(state, repo)
            had = slot.get("binding") is not None
            slot["binding"] = None
            self._put_slot(state, repo, slot)
        return had

    # -- resolução ----------------------------------------------------------

    def resolve(self, repo: str | None = None, *, override_agent_id: str | None = None) -> ResolvedBinding:
        """Binding efetivo + origem. Nunca deduz agente por executável no PATH."""
        state = self._read()
        repo_slot = self._slot(state, repo) if repo not in (None, "") else {}
        store_slot = self._slot(state, None)

        def _binding(slot: Mapping[str, Any]) -> AgentBinding | None:
            raw = slot.get("binding")
            return AgentBinding.from_dict(raw) if isinstance(raw, Mapping) else None

        def _pref(slot: Mapping[str, Any]) -> str | None:
            pref = slot.get("preference")
            return str(pref.get("agent_id")) if isinstance(pref, Mapping) and pref.get("agent_id") else None

        if override_agent_id:
            for slot in (repo_slot, store_slot):
                candidate = _binding(slot)
                if candidate is not None and candidate.agent_id == override_agent_id:
                    return ResolvedBinding(
                        candidate, ORIGIN_OVERRIDE, override_agent_id,
                        "override explícito do operador", _pref(slot),
                    )
            return ResolvedBinding(
                None, ORIGIN_OVERRIDE, override_agent_id,
                f"override {override_agent_id!r} sem binding conectado: execute agent connect",
                _pref(repo_slot) or _pref(store_slot),
            )

        for slot, origin in ((repo_slot, ORIGIN_REPO), (store_slot, ORIGIN_STORE)):
            candidate = _binding(slot)
            if candidate is not None:
                return ResolvedBinding(candidate, origin, candidate.agent_id, "", _pref(slot))
            pref = _pref(slot)
            if pref:
                return ResolvedBinding(
                    None, origin, pref,
                    f"preferência {pref!r} registrada sem binding: execute agent connect",
                    pref,
                )
        return ResolvedBinding(
            None, ORIGIN_NONE, None,
            "nenhuma preferência ou binding registrado; selecione com init --agent",
            None,
        )

    # -- leases -------------------------------------------------------------

    def open_lease(
        self,
        lease_id: str | None = None,
        *,
        binding_id: str,
        repo: str | None = None,
        task_id: str = "",
        vendor: Mapping[str, Any] | None = None,
    ) -> LeaseRecord:
        record = LeaseRecord(
            lease_id=str(lease_id or f"lease-{uuid.uuid4().hex[:16]}"),
            binding_id=str(binding_id),
            scope=self._scope(repo),
            state=LEASE_ACTIVE,
            task_id=str(task_id),
            opened_at=_now(),
            vendor=dict(vendor or {}),
        )
        with self._transaction() as state:
            leases = dict(state.get("leases") or {})
            # M3: NUNCA `record.to_dict()` — levaria `vendor` (credencial do
            # host) para `agents.json` em texto puro. O `vendor` fica só no
            # `LeaseRecord` devolvido, em memória, para quem abriu o lease.
            leases[record.lease_id] = record.to_public_dict()
            state["leases"] = leases
        return record

    def get_lease(self, lease_id: str) -> LeaseRecord | None:
        raw = (self._read().get("leases") or {}).get(str(lease_id))
        return LeaseRecord.from_dict(raw) if isinstance(raw, Mapping) else None

    def lease_valid(self, lease_id: str) -> bool:
        """`True` só para lease conhecido e ativo — invalidado ou desconhecido é `False`."""
        record = self.get_lease(lease_id)
        return record is not None and record.state == LEASE_ACTIVE

    def close_lease(self, lease_id: str, *, reason: str = "concluído") -> bool:
        return self._finish_lease(lease_id, LEASE_CLOSED, reason)

    def _finish_lease(self, lease_id: str, state_value: str, reason: str) -> bool:
        with _file_lock(self.lock_path):
            state = self._read()
            leases = dict(state.get("leases") or {})
            raw = leases.get(str(lease_id))
            if not isinstance(raw, Mapping) or raw.get("state") != LEASE_ACTIVE:
                return False
            record = LeaseRecord.from_dict(raw)
            leases[str(lease_id)] = LeaseRecord(
                lease_id=record.lease_id,
                binding_id=record.binding_id,
                scope=record.scope,
                state=state_value,
                task_id=record.task_id,
                opened_at=record.opened_at,
                closed_at=_now(),
                reason=reason,
                vendor=record.vendor,
            ).to_public_dict()  # M3: fechar o lease não reintroduz `vendor`
            state["leases"] = leases
            self._write(state)
        return True

    def active_leases(self, repo: str | None = None) -> tuple[LeaseRecord, ...]:
        """Leases ativos do escopo. Sem repo, os do store."""
        scope = self._scope(repo)
        records = [
            LeaseRecord.from_dict(raw)
            for raw in (self._read().get("leases") or {}).values()
            if isinstance(raw, Mapping)
        ]
        return tuple(
            sorted(
                (r for r in records if r.state == LEASE_ACTIVE and r.scope == scope),
                key=lambda r: (r.opened_at, r.lease_id),
            )
        )

    def cancel_active(self, repo: str | None = None, *, reason: str = "agent connect --cancel-active") -> int:
        """Invalida os leases ativos do escopo. Devolve quantos foram invalidados.

        Depois disto, `lease_valid` é `False` e
        `envelopes.validate_result(..., lease_valid=False)` recusa qualquer
        resultado tardio daquelas execuções.
        """
        cancelled = 0
        for record in self.active_leases(repo):
            if self._finish_lease(record.lease_id, LEASE_CANCELLED, reason):
                cancelled += 1
        return cancelled

    # -- saída pública (campo JSON `agent` do §10.3) ------------------------

    def to_public_dict(self, repo: str | None = None) -> dict[str, Any]:
        """Campo `agent`: binding, agente, transporte, versão do adaptador,
        capacidades e situação da conexão. Sem segredos."""
        resolved = self.resolve(repo)
        binding = resolved.binding
        return {
            "binding_id": binding.binding_id if binding else None,
            "agent_id": resolved.agent_id,
            "transport": binding.transport if binding else None,
            # §10.4.3: quem lê o campo `agent` precisa distinguir transporte
            # ESCOLHIDO por capacidade verificada de transporte apenas
            # declarado pelo descritor — senão a ordem da lista passa por prova.
            "transport_selection": binding.transport_selection if binding else None,
            "adapter_version": binding.adapter_version if binding else None,
            "host_version": binding.host_version if binding else None,
            "model": binding.model if binding else None,
            "model_available": bool(binding.model) if binding else False,
            "capabilities": binding.capabilities.to_public_dict() if binding else None,
            "connection_status": "connected" if resolved.connected else (
                "selected" if resolved.agent_id else "unselected"
            ),
            "selection_origin": resolved.origin,
            "preference": resolved.preference,
            "detail": resolved.detail,
            "active_leases": [r.to_dict() for r in self.active_leases(repo)],
        }


__all__ = [
    "LEASE_ACTIVE",
    "LEASE_CANCELLED",
    "LEASE_CLOSED",
    "ORIGIN_NONE",
    "ORIGIN_OVERRIDE",
    "ORIGIN_REPO",
    "ORIGIN_STORE",
    "SCHEMA_VERSION",
    "STORE_FILENAME",
    "STORE_SCOPE",
    "LOCK_SUFFIX",
    "LOCK_TIMEOUT_S",
    "BindingError",
    "BindingStore",
    "LeaseRecord",
    "ResolvedBinding",
    "normalize_repo",
    "persistable_binding",
]
