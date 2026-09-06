"""`LocalThreadExecutor`: adapter de referência para tarefas determinísticas (plano §7.3).

Não integra nenhuma engine de LLM. Executa callables Python REGISTRADOS
explicitamente (whitelist por `kind`) em um `ThreadPoolExecutor` com
concorrência limitada — serve de executor real para testes do coordenador e
para tarefas mecânicas (ex.: verificação, agregação) que não precisam de um
agente externo.

Cancelamento é `"best-effort"` de propósito: `ThreadPoolExecutor` não mata
uma thread em execução. `cancel()` seta um `threading.Event`; o callable
registrado é responsável por checá-lo cooperativamente e retornar cedo. Uma
tarefa que nunca olha o evento roda até o fim — isso é declarado em
`capabilities()`, nunca prometido como cancelamento forçado.

Somente stdlib. Nenhum import de `wk`/`codescan`/`sbindex`.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Mapping, Optional, Sequence

from .base import BaseExecutor, ExecutionRecord, ExecutionState

# Assinatura exigida de um callable registrado: recebe o pacote da tarefa e
# um `threading.Event` de cancelamento cooperativo; devolve o `output`
# estruturado (idealmente um Mapping, já que `result()` documenta output
# como objeto).
LocalTaskCallable = Callable[..., Any]


class LocalThreadExecutor(BaseExecutor):
    """Executor real (não simulado) sobre `ThreadPoolExecutor`.

    `registry` mapeia `kind -> callable`. `submit(...)` espera que
    `objective` seja um `Mapping` com a chave `"kind"` presente em
    `registry` — outros formatos são rejeitados com `ValueError` antes de
    qualquer trabalho ser agendado (falha imediata, sem `execution_id`
    emitido para um objetivo que não pode rodar).
    """

    def __init__(self, registry: Mapping[str, LocalTaskCallable], max_workers: int = 4):
        super().__init__(prefix="local")
        if max_workers < 1:
            raise ValueError("max_workers deve ser >= 1")
        self._registry: dict[str, LocalTaskCallable] = dict(registry)
        self._max_workers = max_workers
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="local-exec")
        self._futures: dict[str, Future] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    # -- capabilities: só o que foi de fato sondado --------------------------

    def capabilities(self) -> dict:
        return {
            "dispatch": True,
            "concurrency": self._max_workers,
            "cancellation": "best-effort",
            "tools": sorted(self._registry.keys()),
            "structured_output": True,
            "telemetry": True,
        }

    # -- submit ----------------------------------------------------------------

    def submit(
        self,
        task_id: str,
        objective: Any,
        references: Optional[Sequence[Any]] = None,
        schema: Optional[Mapping[str, Any]] = None,
        policy: Optional[Mapping[str, Any]] = None,
    ) -> str:
        if not isinstance(objective, Mapping) or "kind" not in objective:
            raise ValueError(
                "objective de LocalThreadExecutor precisa ser Mapping com chave 'kind'"
            )
        kind = objective["kind"]
        if kind not in self._registry:
            raise ValueError(f"kind não registrado em LocalThreadExecutor: {kind!r}")

        validated_policy = self._validate_policy(policy)
        execution_id = self._new_execution_id()  # emitido ANTES de agendar (F07)
        cancel_event = threading.Event()
        record = ExecutionRecord(
            execution_id=execution_id,
            task_id=task_id,
            objective=objective,
            references=references,
            schema=schema,
            policy=validated_policy,
        )
        self._register(record)
        self._cancel_events[execution_id] = cancel_event

        future = self._pool.submit(
            self._run, execution_id, kind, objective, references, schema, cancel_event
        )
        self._futures[execution_id] = future
        return execution_id

    def _run(
        self,
        execution_id: str,
        kind: str,
        objective: Any,
        references: Optional[Sequence[Any]],
        schema: Optional[Mapping[str, Any]],
        cancel_event: threading.Event,
    ) -> None:
        with self._lock:
            record = self._records[execution_id]
            if cancel_event.is_set():
                record.state = ExecutionState.CANCELLED
                record.heartbeat_at = time.time()
                return
            record.state = ExecutionState.RUNNING
            record.heartbeat_at = time.time()

        callable_ = self._registry[kind]
        try:
            output = callable_(
                objective=objective,
                references=references,
                schema=schema,
                cancel_event=cancel_event,
            )
        except Exception as exc:  # falha do callable vira estado, não crash da pool
            with self._lock:
                record = self._records[execution_id]
                record.state = ExecutionState.FAILED
                record.error = f"{type(exc).__name__}: {exc}"
                record.heartbeat_at = time.time()
            return

        with self._lock:
            record = self._records[execution_id]
            if cancel_event.is_set():
                record.state = ExecutionState.CANCELLED
            else:
                record.state = ExecutionState.DONE
                record.output = output
            record.heartbeat_at = time.time()

    # -- status/result: herdados de BaseExecutor (F07 via _get_owned) --------

    # -- cancel ------------------------------------------------------------------

    def cancel(self, execution_id: str) -> bool:
        record = self._get_owned(execution_id)  # levanta UnknownExecutionError se alheio
        event = self._cancel_events.get(execution_id)
        if event is None:
            return False
        with self._lock:
            if record.state in ExecutionState.TERMINAL:
                return False
            event.set()
            if record.state == ExecutionState.QUEUED:
                # Ainda não começou a rodar: podemos marcar cancelado direto.
                record.state = ExecutionState.CANCELLED
                record.heartbeat_at = time.time()
        # Se já está RUNNING, o callable precisa observar `cancel_event`
        # cooperativamente; o estado final é decidido em `_run` (best-effort).
        return True

    def shutdown(self, wait: bool = True) -> None:
        """Encerra o pool. Não faz parte do protocolo `AgentExecutor`; uso em testes/CLI."""
        self._pool.shutdown(wait=wait)
