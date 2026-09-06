"""Protocolo `AgentExecutor` e regras transversais dos adapters de execução (plano §7.3, F07).

Este módulo define o contrato que `runtime.coordinator` assume (ver
`runtime/__init__.py`, seção "Contratos dos vizinhos"):

    capabilities() -> dict
    submit(task_id, objective, references, schema, policy) -> str   # execution_id
    status(execution_id) -> {"state": str, "heartbeat": str|None}
    result(execution_id) -> {"execution_id": str, "output": dict|None}
    cancel(execution_id) -> bool

Três invariantes de F07/§13.1 ficam em código executável aqui, não em prosa:

1. **`execution_id` é sempre emitido pelo executor, nunca aceito de fora.**
   `BaseExecutor._new_execution_id` gera `"<prefixo>:<uuid4>"` ANTES de
   qualquer trabalho começar, e `_get_owned` só reconhece um id presente no
   registro interno (`_records`) deste próprio executor. Um id com o prefixo
   certo mas nunca devolvido por `submit` (ou devolvido por outro executor)
   não está em `_records` e é rejeitado do mesmo jeito que um id qualquer —
   a "identidade alegada" de um id bem formado não é atestado.
2. **`result()`/`status()`/`cancel()` de execução desconhecida levantam
   `UnknownExecutionError`.** Nunca retornam `None` nem um dicionário vazio
   silencioso — o chamador precisa de uma falha material para decidir.
3. **`policy` nunca autoriza escrita arbitrária.** `validate_policy` rejeita
   qualquer campo que pareça um destino de escrita/publicação (`write_paths`
   e afins) antes que o payload chegue a um executor — o worker recebe
   apenas objetivo/referências/schema/política, e nunca escreve na base
   canônica nem no destino de publicação (§7.3, §13.1: payload não muda
   orçamento/ferramentas/permissões).

Somente stdlib. Nenhum import de `wk`/`codescan`/`sbindex`.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable


# --------------------------------------------------------------------------
# Estado e exceções
# --------------------------------------------------------------------------


class ExecutionState:
    """Valores fechados de `state` (strings simples: o consumidor faz `== `/JSON)."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    ALL = frozenset({QUEUED, RUNNING, DONE, FAILED, CANCELLED, UNKNOWN})
    TERMINAL = frozenset({DONE, FAILED, CANCELLED})


class UnknownExecutionError(LookupError):
    """`execution_id` não emitido (ou não emitido por ESTE executor) — F07.

    Nunca levantada a partir de heurística sobre o formato do id: só a
    ausência do id no registro interno do executor decide.
    """


class PolicyRejectedError(ValueError):
    """`policy` contém campo de escrita/publicação proibido (§13.1)."""


class ExecutionNotReadyError(RuntimeError):
    """`result()` chamado antes de a execução atingir um estado terminal."""


class ExecutorUnavailableError(RuntimeError):
    """`submit()` chamado sem despacho real disponível (`capabilities().dispatch is False`)."""


# --------------------------------------------------------------------------
# Validação de política (§13.1)
# --------------------------------------------------------------------------

# Campos cujo NOME sozinho já denuncia um destino de escrita/publicação.
_FORBIDDEN_POLICY_KEYS = frozenset(
    {
        "write_path",
        "write_paths",
        "writeto",
        "write_to",
        "output_path",
        "output_paths",
        "publish_path",
        "publish_paths",
        "publish_target",
        "publish_to",
        "destination_path",
        "destination_paths",
        "canonical_path",
        "canonical_paths",
        "target_path",
        "target_paths",
        "save_path",
        "save_paths",
        "save_to",
    }
)

# "E afins": heurística por padrão, para pegar variações não listadas acima
# (ex.: "outputPath", "publishDir", "dest_path") sem precisar enumerar tudo.
_FORBIDDEN_KEY_PATTERN = re.compile(
    r"(write|publish|save|destination|canonical|target|output)[_-]?(path|dir|paths|dirs|to)"
    r"|(path|dir)[_-]?(write|publish|save)",
    re.IGNORECASE,
)

_MAX_POLICY_DEPTH = 6


def _is_forbidden_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _FORBIDDEN_POLICY_KEYS:
        return True
    return bool(_FORBIDDEN_KEY_PATTERN.search(normalized))


def _scan_for_forbidden_keys(value: Any, depth: int, offending: list[str]) -> None:
    if depth > _MAX_POLICY_DEPTH:
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _is_forbidden_key(key):
                offending.append(str(key))
            _scan_for_forbidden_keys(nested, depth + 1, offending)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _scan_for_forbidden_keys(item, depth + 1, offending)


def validate_policy(policy: Optional[Mapping[str, Any]]) -> dict:
    """Valida e normaliza `policy` para um `dict` puro.

    Rejeita (levanta `PolicyRejectedError`) qualquer campo — em qualquer
    profundidade dentro de mapeamentos aninhados — cujo nome indique um
    destino de escrita/publicação. `policy` legitima orçamento, ferramentas
    permitidas e limites de tempo; nunca um caminho de saída (§13.1: payload
    não muda orçamento/ferramentas/permissões).
    """
    if policy is None:
        return {}
    if not isinstance(policy, Mapping):
        raise PolicyRejectedError(f"policy deve ser Mapping, recebeu {type(policy).__name__}")
    offending: list[str] = []
    _scan_for_forbidden_keys(policy, 0, offending)
    if offending:
        raise PolicyRejectedError(
            "policy contém campo(s) de escrita/publicação proibido(s): "
            + ", ".join(sorted(set(offending)))
        )
    return dict(policy)


# --------------------------------------------------------------------------
# Registro interno (não exposto ao consumidor como está — §7.3)
# --------------------------------------------------------------------------


@dataclass
class ExecutionRecord:
    """Recibo interno de uma execução. Consumidor só vê `status()`/`result()`."""

    execution_id: str
    task_id: str
    objective: Any
    references: Optional[Sequence[Any]]
    schema: Optional[Mapping[str, Any]]
    policy: Mapping[str, Any]
    state: str = ExecutionState.QUEUED
    created_at: float = field(default_factory=time.time)
    heartbeat_at: float = field(default_factory=time.time)
    output: Optional[Mapping[str, Any]] = None
    error: Optional[str] = None
    attempts: int = 0


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Protocolo público
# --------------------------------------------------------------------------


@runtime_checkable
class AgentExecutor(Protocol):
    """Estrutura assumida por `runtime.coordinator` (ver `runtime/__init__.py`)."""

    def capabilities(self) -> dict: ...

    def submit(
        self,
        task_id: str,
        objective: Any,
        references: Optional[Sequence[Any]] = None,
        schema: Optional[Mapping[str, Any]] = None,
        policy: Optional[Mapping[str, Any]] = None,
    ) -> str: ...

    def status(self, execution_id: str) -> dict: ...

    def result(self, execution_id: str) -> dict: ...

    def cancel(self, execution_id: str) -> bool: ...


class BaseExecutor(ABC):
    """Base comum: id prefixado, registro interno, validação de política.

    Subclasses implementam `capabilities`/`submit`/`_status_record`/`_result_record`/`cancel`
    (via os métodos abstratos abaixo) e usam `_new_execution_id`/`_register`/`_get_owned`
    para respeitar F07 sem duplicar a lógica de identidade.
    """

    def __init__(self, prefix: str):
        if not prefix or ":" in prefix or "/" in prefix or "\\" in prefix:
            raise ValueError(f"prefixo de executor inválido: {prefix!r}")
        self._prefix = prefix
        self._lock = threading.RLock()
        self._records: dict[str, ExecutionRecord] = {}

    @property
    def prefix(self) -> str:
        return self._prefix

    # -- identidade (F07) ---------------------------------------------------

    def _new_execution_id(self) -> str:
        return f"{self._prefix}:{uuid.uuid4()}"

    def _register(self, record: ExecutionRecord) -> None:
        with self._lock:
            self._records[record.execution_id] = record

    def _get_owned(self, execution_id: str) -> ExecutionRecord:
        """Só reconhece um id presente no registro deste executor (F07)."""
        with self._lock:
            record = self._records.get(execution_id)
        if record is None:
            raise UnknownExecutionError(
                f"execution_id não emitido por este executor ({self._prefix}): {execution_id!r}"
            )
        return record

    def _touch(self, execution_id: str) -> None:
        with self._lock:
            record = self._records.get(execution_id)
            if record is not None:
                record.heartbeat_at = time.time()

    # -- formatação da saída pública -----------------------------------------

    @staticmethod
    def _status_dict(record: ExecutionRecord) -> dict:
        return {"state": record.state, "heartbeat": _iso(record.heartbeat_at)}

    @staticmethod
    def _result_dict(record: ExecutionRecord) -> dict:
        if record.state not in ExecutionState.TERMINAL:
            raise ExecutionNotReadyError(
                f"execução {record.execution_id!r} ainda não terminou (state={record.state!r})"
            )
        out = {"execution_id": record.execution_id, "output": record.output}
        if record.error is not None:
            out["error"] = record.error
        return out

    # -- política -------------------------------------------------------------

    @staticmethod
    def _validate_policy(policy: Optional[Mapping[str, Any]]) -> dict:
        return validate_policy(policy)

    # -- protocolo (implementado pelas subclasses) -----------------------------

    @abstractmethod
    def capabilities(self) -> dict: ...

    @abstractmethod
    def submit(
        self,
        task_id: str,
        objective: Any,
        references: Optional[Sequence[Any]] = None,
        schema: Optional[Mapping[str, Any]] = None,
        policy: Optional[Mapping[str, Any]] = None,
    ) -> str: ...

    def status(self, execution_id: str) -> dict:
        return self._status_dict(self._get_owned(execution_id))

    def result(self, execution_id: str) -> dict:
        return self._result_dict(self._get_owned(execution_id))

    @abstractmethod
    def cancel(self, execution_id: str) -> bool: ...
