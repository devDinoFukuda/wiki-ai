"""Executores reais e sua ponte para o contrato de agente (spec §10.4.3-§10.4.5).

| Módulo              | Responsabilidade                                                     |
|---------------------|----------------------------------------------------------------------|
| `base.py`           | Protocolo `AgentExecutor`, `ExecutionState`, exceções, `validate_policy` |
| `local_thread.py`   | `LocalThreadExecutor` — callables registrados em `ThreadPoolExecutor` |
| `claude_cli.py`     | `ClaudeCliExecutor` — CLI `claude` headless                          |
| `agent_adapters.py` | Adaptadores (`local`, `claude-code`, legado) sobre esses executores  |

**Rota única.** O antigo `_REGISTRY = {"local": ..., "claude-cli": ...}` era um
segundo registro, concorrente com `runtime.agents.AgentRegistry`. Ele não
existe mais: `get_executor(name, **cfg)` é uma FACHADA FINA que resolve o nome
no registro de agentes e delega a construção ao adaptador
(`BaseExecutorAdapter.new_executor`). Não há tabela de classes aqui — o
registro é extensível e a construção tem uma implementação só.

`AGENT_ID_ALIASES` mantém os nomes históricos aceitos por
`wk.cli._build_executor` (`"claude-cli"` → `claude-code`) enquanto a CLI não
migra para `agent connect`. Alias é tradução de nome, não uma segunda rota.

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
    "AGENT_ID_ALIASES",
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
    "resolve_agent_id",
]

#: Nomes históricos de executor → ID público de agente (§10.4.5).
AGENT_ID_ALIASES = {
    "claude-cli": "claude-code",
    "claude_cli": "claude-code",
}


def resolve_agent_id(name: str) -> str:
    """Traduz o nome histórico de executor para o ID público do agente."""
    return AGENT_ID_ALIASES.get(name, name)


def get_executor(name: str, **cfg: Any) -> AgentExecutor:
    """Fachada: constrói o executor do agente `name` pelo registro de adaptadores.

    `get_executor("local", registry=..., max_workers=4)` continua funcionando
    para `wk.cli._build_executor`, mas quem constrói é o `LocalAgentAdapter`.
    Nome desconhecido levanta `ValueError` listando os agentes REGISTRADOS —
    nunca devolve um executor genérico silencioso.
    """
    from ..agents import AgentUnavailableError, default_registry

    registry = default_registry()
    agent_id = resolve_agent_id(name)
    try:
        adapter = registry.adapter(agent_id)
    except AgentUnavailableError as exc:
        raise ValueError(
            f"executor desconhecido: {name!r} (agentes registrados: "
            f"{list(registry.agent_ids())})"
        ) from exc
    factory = getattr(adapter, "new_executor", None)
    if not callable(factory):
        raise ValueError(
            f"adaptador de {agent_id!r} não expõe executor construível; "
            "use agents.AgentRegistry.connect() para despachar por ele"
        )
    return factory(**cfg)
