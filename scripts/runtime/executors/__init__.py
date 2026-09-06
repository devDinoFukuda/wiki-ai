"""Adapters de execução (`AgentExecutor`) — plano §7.3, F07.

| Módulo            | Responsabilidade                                                        |
|-------------------|--------------------------------------------------------------------------|
| `base.py`         | Protocolo `AgentExecutor`, `ExecutionState`, exceções, `validate_policy` |
| `local_thread.py` | `LocalThreadExecutor` — callables registrados em `ThreadPoolExecutor`    |
| `claude_cli.py`   | `ClaudeCliExecutor` — engine LLM via CLI `claude` headless               |

`get_executor(name, **cfg)` é a factory usada por quem monta o coordenador
(`runtime.coordinator`, vizinho — não lido/importado por este pacote).

Somente stdlib. Nenhum import de `wk`/`codescan`/`sbindex`.
"""

from __future__ import annotations

from typing import Any

from .base import (
    AgentExecutor,
    BaseExecutor,
    ExecutionRecord,
    ExecutionState,
    ExecutorUnavailableError,
    ExecutionNotReadyError,
    PolicyRejectedError,
    UnknownExecutionError,
    validate_policy,
)
from .claude_cli import ClaudeCliExecutor
from .local_thread import LocalThreadExecutor

__all__ = [
    "AgentExecutor",
    "BaseExecutor",
    "ExecutionRecord",
    "ExecutionState",
    "ExecutorUnavailableError",
    "ExecutionNotReadyError",
    "PolicyRejectedError",
    "UnknownExecutionError",
    "validate_policy",
    "ClaudeCliExecutor",
    "LocalThreadExecutor",
    "get_executor",
]

_REGISTRY = {
    "local": LocalThreadExecutor,
    "claude-cli": ClaudeCliExecutor,
}


def get_executor(name: str, **cfg: Any) -> AgentExecutor:
    """Factory: `get_executor("local", registry=..., max_workers=4)` etc.

    Levanta `ValueError` para `name` desconhecido — nunca devolve um
    executor "genérico" silencioso para um nome não mapeado.
    """
    try:
        cls = _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"executor desconhecido: {name!r} (disponíveis: {sorted(_REGISTRY)})"
        ) from exc
    return cls(**cfg)
