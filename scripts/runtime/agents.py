"""Contrato comum de agente: descritor, binding, sonda e registro (spec §10.4.1-§10.4.6).

Este módulo é a ÚNICA definição do contrato `describe/connect/probe/submit/poll/
cancel/close`. `runtime.executors` deixou de ser um registro paralelo: seu
`get_executor` é fachada fina sobre `default_registry()` (ver
`executors/__init__.py`), e os executores concretos (`LocalThreadExecutor`,
`ClaudeCliExecutor`) só chegam ao despacho ENVOLVIDOS por um adaptador
(`executors/agent_adapters.py`).

Invariantes em código (não em prosa):

* `AgentDescriptor.__post_init__` recusa descritor sem transporte válido,
  sem versão de adaptador, com passo de preparação não executável
  (`SetupStep` exige `argv` ou `manual_action`+`verify_argv`) ou com
  pré-requisito sem comando de verificação — é o que impede "configure o
  agente" virar instrução (§10.4.6).
* `verified_versions` vazio significa NÃO homologado; `to_dict()` publica
  `homologated: False`. Nenhum adaptador é registrado só por ter ID (§10.4.5).
* `AgentRegistry.register` não tem enumeração fechada: qualquer objeto que
  satisfaça o protocolo entra, e `load_extensions` registra adaptadores de
  terceiros por `modulo:fabrica` sem alterar o núcleo (§10.4.2 item 10).
* `AgentRegistry.connect` NUNCA cai em outro agente: transporte não oferecido
  ou handshake sem `connected=True` levantam erro (§10.4.2 item 5).
* `auto` é seleção POR CAPACIDADE VERIFICADA (§10.4.3): o adaptador que
  implementa `available_transports(config)` informa quais transportes estão
  realmente utilizáveis, e `resolve_transport` escolhe o primeiro DISPONÍVEL
  na ordem de preferência do descritor. Nenhum transporte disponível ⇒
  `TransportUnavailable` (um `HandshakeFailed`) com a instrução daquele
  agente; transporte explícito indisponível ⇒ o mesmo erro com o detalhe
  daquele transporte. Adaptador sem o método mantém o comportamento
  histórico (primeiro declarado) e a escolha fica marcada como
  `transport_selection="declared"` no binding e na sonda — nunca "verified".

Somente stdlib. Nenhum import de `wk`/`codescan`/`sbindex`; nenhuma rede.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

#: §10.4.1 — meios de troca entre controlador e adaptador. `auto` é seleção,
#: não um transporte próprio: `AgentRegistry.connect` resolve `auto` para um
#: transporte declarado pelo descritor.
TRANSPORTS = frozenset({"process", "session"})

#: Como o transporte do binding foi escolhido (§10.4.3).
#: `verified` — o adaptador informou disponibilidade real por
#: `available_transports(config)`; `declared` — adaptador sem esse método: vale
#: a ordem declarada no descritor (comportamento histórico), e a escolha NÃO
#: pode ser lida como capacidade verificada.
SELECTION_VERIFIED = "verified"
SELECTION_DECLARED = "declared"
TRANSPORT_SELECTIONS = frozenset({SELECTION_VERIFIED, SELECTION_DECLARED})

#: Origem da distribuição do adaptador — o que `agent list` mostra como
#: "distribuído com o núcleo" x "registrado por extensão" (§10.4.5).
DISTRIBUTIONS = frozenset({"bundled", "extension"})

#: Variável lida por `default_registry()` para registrar adaptadores externos.
EXTENSIONS_ENV = "WIKI_AI_AGENT_ADAPTERS"

_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Chaves nunca copiadas para saída pública (`to_public_dict`) — §10.3
#: "não transportar segredos".
_SECRET_HINT = re.compile(
    r"token|secret|password|passwd|credential|api[_-]?key|authorization|cookie|session[_-]?id",
    re.IGNORECASE,
)

#: Marca do valor suprimido. É visível de propósito: quem lê `agent status` ou
#: `agents.json` precisa saber que ALI havia um valor — suprimir em silêncio
#: transformaria "segredo removido" em "campo vazio".
REDACTED = "[REDACTED]"

#: Nomes de segredo escritos DENTRO de um valor livre. `_SECRET_HINT` só olha o
#: NOME da chave; o token que um adaptador de extensão escreve na própria frase
#: (`TransportAvailability.detail`, `outcome["detail"]`, `ProbeResult.
#: diagnostics`) passava inteiro por ele e chegava à saída pública e ao
#: `agents.json`. O valor inclui um esquema opcional (`Bearer x`) para que
#: `authorization: Bearer abc` some de uma vez, e não pela metade.
_SECRET_KV = re.compile(
    r"(?i)\b(token|secret|password|passwd|credential|api[_-]?key|authorization|cookie|"
    r"session[_-]?id)(\s*[=:]\s*)\"?(?:(?:bearer|basic|token)\s+)?[^\s\"',;&]+",
)
#: Credencial de cabeçalho solta, sem chave nomeada antes.
_SECRET_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}")
#: Chave de API no formato `sk-...` (padrão de vários hosts) sem nome de chave.
_SECRET_SK = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{3,}")


def redact_secrets(text: Any) -> Any:
    """Suprime o VALOR do segredo mantendo o resto da frase.

    A semântica da mensagem não muda: `api_key=abc123` vira
    `api_key=[REDACTED]` — o leitor continua sabendo QUAL campo estava
    envolvido, sem receber a credencial. Valor não-string volta intacto.
    """
    if not isinstance(text, str) or not text:
        return text
    out = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    out = _SECRET_SCHEME.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    return _SECRET_SK.sub(f"sk-{REDACTED}", out)


def _public_text(value: Any) -> str:
    """Texto livre pronto para saída pública/persistida (§10.3)."""
    return redact_secrets(str(value or ""))


def config_cache_key(config: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """Chave estável de um `config` para memoização DENTRO de uma operação.

    `config` carrega valores arbitrários (inclusive não-hasháveis) vindos do
    chamador: `repr` é o que dá uma chave determinística sem exigir que o valor
    seja hashável. A chave nunca sai do processo — só distingue "mesma
    configuração" de "outra" dentro de um `connect`/`probe`.
    """
    return tuple(sorted((str(k), repr(v)) for k, v in dict(config or {}).items()))


class _OperationState(threading.local):
    """Profundidade e memória da operação corrente, POR THREAD.

    Thread-local porque `connect`/`probe` concorrentes de threads diferentes
    são operações diferentes: partilhar a memória entre elas faria uma thread
    ler o handshake que a outra acabou de fazer com OUTRO `config`.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.data: dict[Any, Any] = {}


class OperationCache:
    """Memória de UMA operação: reentrante, esvaziada ao fechar a janela.

    Existe para o §10.4.3 não custar N handshakes: `connect` com `auto` chama
    `describe()`, `available_transports(config)` e o handshake do `connect`
    propriamente dito, e cada um deles disparava a verificação REAL do host
    (no `claude-code`, um `claude --version` com 5 s de timeout — até três
    subprocessos por `connect`). Dentro da janela o resultado é reaproveitado;
    ao sair (ou em `close()`) a memória some, então nada aqui é cache de longa
    duração: uma operação nova volta a verificar o host de verdade.
    """

    def __init__(self) -> None:
        self._state = _OperationState()

    @property
    def active(self) -> bool:
        return self._state.depth > 0

    @contextlib.contextmanager
    def scope(self):
        state = self._state
        state.depth += 1
        try:
            yield self
        finally:
            state.depth -= 1
            if state.depth <= 0:
                state.depth = 0
                state.data.clear()

    def get(self, key: Any, produce):
        """`produce()` roda no máximo UMA vez por chave dentro da janela."""
        state = self._state
        if state.depth <= 0:
            return produce()
        if key in state.data:
            return state.data[key]
        value = produce()
        state.data[key] = value
        return value

    def clear(self) -> None:
        self._state.data.clear()


def _adapter_scope(adapter: Any):
    """Janela de operação DO ADAPTADOR, quando ele oferece uma.

    Adaptador de terceiro não precisa implementar `operation_scope`: sem ela o
    comportamento é o histórico (cada chamada verifica o host de novo).
    """
    opener = getattr(adapter, "operation_scope", None)
    if not callable(opener):
        return contextlib.nullcontext()
    try:
        return opener()
    except Exception:  # pragma: no cover - defensivo: extensão mal comportada
        return contextlib.nullcontext()


class AgentContractError(ValueError):
    """Descritor/binding fora do contrato mínimo do §10.4.4."""


class AgentUnavailableError(RuntimeError):
    """Agente não registrado, transporte indisponível ou integração ausente."""


class HandshakeFailed(AgentUnavailableError):
    """`connect` não produziu binding conectado — §10.4.6: não registrar conexão
    antes do handshake."""


class TransportUnavailable(HandshakeFailed):
    """Nenhum transporte utilizável (ou o transporte pedido está indisponível).

    §10.4.3: "se nenhum transporte estiver disponível, o estado é bloqueado com
    instrução específica para aquele agente". Por isso o erro CARREGA os passos
    de preparação daquele agente (`setup_steps`) e o detalhe verificado de cada
    transporte — `auto` nunca vira tentativa de adivinhar argumentos, e quem
    trata o bloqueio tem o que executar.

    É um `HandshakeFailed` (logo, um `AgentUnavailableError`): quem já
    diferencia "engine desconhecida" de "despacho indisponível" continua
    classificando este caso como despacho indisponível.
    """

    def __init__(
        self,
        message: str,
        *,
        agent_id: str,
        transport: str = "auto",
        options: Sequence["TransportAvailability"] = (),
        setup_steps: Sequence["SetupStep"] = (),
    ) -> None:
        super().__init__(message)
        self.agent_id = agent_id
        self.transport = transport
        self.options: tuple["TransportAvailability", ...] = tuple(options)
        self.setup_steps: tuple["SetupStep", ...] = tuple(setup_steps)

    def next_actions(self) -> tuple[dict[str, Any], ...]:
        """Passos executáveis do agente bloqueado — cada um com `argv` real."""
        actions: list[dict[str, Any]] = []
        for step in self.setup_steps:
            actions.append(
                {
                    "actor": "operador",
                    "reason": f"{self.agent_id}: {step.description}",
                    "argv": list(step.argv or step.verify_argv),
                    "manual_action": step.manual_action,
                }
            )
        return tuple(actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "transport": self.transport,
            # A mensagem carrega o `detail` verificado de cada transporte —
            # inclusive o de um adaptador de extensão que escreveu token ali.
            "detail": _public_text(self),
            "transports": [o.to_dict() for o in self.options],
            "setup_steps": [s.to_dict() for s in self.setup_steps],
            "next_actions": [dict(a) for a in self.next_actions()],
        }


def _clean_public(value: Any, depth: int = 0) -> Any:
    """Cópia de `value` sem campos cujo NOME indique segredo.

    Filtrar por NOME não basta: o token que o adaptador de extensão escreve
    dentro de uma string livre (`"falhou com api_key=abc123"`) está sob uma
    chave inocente e passava inteiro. Toda folha string passa agora por
    `redact_secrets`.
    """
    if depth > 6:
        return None
    if isinstance(value, Mapping):
        return {
            str(k): _clean_public(v, depth + 1)
            for k, v in value.items()
            if not _SECRET_HINT.search(str(k))
        }
    if isinstance(value, (list, tuple)):
        return [_clean_public(v, depth + 1) for v in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


# --------------------------------------------------------------------------
# Capacidades negociáveis (§10.4.4, último parágrafo)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCapabilities:
    """Capacidade EFETIVAMENTE negociada — nunca uma promessa do fornecedor.

    `negotiate()` devolve os limites efetivos de um orçamento pedido: o mínimo
    entre o pedido e o declarado. Reduzir tamanho não remove obrigação: o
    resultado só mexe em limites (`max_bytes`, `max_tokens`, `concurrency`,
    `timeout_s`), nunca em `reading_needs`/`evidence` (§10.4.4).
    """

    #: `False` ⇒ adaptador conhecido, sem despacho real disponível agora.
    dispatch: bool = True
    max_context_bytes: int | None = None
    max_context_tokens: int | None = None
    max_concurrency: int | None = None
    max_duration_s: float | None = None
    supports_session: bool = False
    #: Cancelamento FÍSICO (matar processo/sessão). Quando `False`, só existe
    #: invalidação lógica de lease (§10.4.4).
    physical_cancellation: bool = False
    structured_output: bool = True
    #: Consegue LER evidência nova e aprofundar investigação numa continuação.
    deepening: bool = False
    telemetry: bool = False
    tools: tuple[str, ...] = ()
    reason: str = ""
    vendor: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatch": self.dispatch,
            "max_context_bytes": self.max_context_bytes,
            "max_context_tokens": self.max_context_tokens,
            "max_concurrency": self.max_concurrency,
            "max_duration_s": self.max_duration_s,
            "supports_session": self.supports_session,
            "physical_cancellation": self.physical_cancellation,
            "structured_output": self.structured_output,
            "deepening": self.deepening,
            "telemetry": self.telemetry,
            "tools": list(self.tools),
            "reason": _public_text(self.reason),
            "vendor": dict(self.vendor),
        }

    def to_public_dict(self) -> dict[str, Any]:
        """`to_dict()` SEM segredo dentro de `vendor`.

        `vendor` é a área extensível do fornecedor: é exatamente onde um
        adaptador de terceiro deposita `api_key`/`token`/`cookie` do host. A
        saída pública (`agent status`, campo `agent` do §10.3) e o registro
        persistido (`bindings.save_binding`) passam por aqui, então o segredo
        nem chega ao JSON impresso nem ao `agents.json` em disco.
        """
        data = self.to_dict()
        data["vendor"] = _clean_public(data["vendor"])
        return data

    def to_executor_capabilities(self) -> dict[str, Any]:
        """Forma consumida por `coordinator.run`/`plan_continuations`.

        Mantém as chaves históricas (`dispatch`, `concurrency`, `cancellation`,
        `tools`, `structured_output`, `telemetry`, `deepening`) para que a
        fachada `get_executor` e os chamadores existentes continuem lendo o
        mesmo vocabulário — UMA fonte, dois nomes de chave.
        """
        return {
            "dispatch": self.dispatch,
            "concurrency": self.max_concurrency,
            "max_concurrency": self.max_concurrency,
            "cancellation": "process-kill" if self.physical_cancellation else "best-effort",
            "tools": list(self.tools),
            "structured_output": self.structured_output,
            "telemetry": self.telemetry,
            "deepening": self.deepening,
            "supports_session": self.supports_session,
            "max_context_bytes": self.max_context_bytes,
            "max_context_tokens": self.max_context_tokens,
            "max_duration_s": self.max_duration_s,
            "reason": self.reason,
        }

    @staticmethod
    def _min(requested: Any, declared: Any) -> Any:
        if requested is None:
            return declared
        if declared is None:
            return requested
        return min(requested, declared)

    def negotiate(self, requested: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Limites efetivos = min(pedido, declarado). Só limites, nunca obrigações."""
        req = dict(requested or {})
        effective = {
            "max_bytes": self._min(req.get("max_bytes"), self.max_context_bytes),
            "max_tokens": self._min(req.get("max_tokens"), self.max_context_tokens),
            "max_concurrency": self._min(
                req.get("max_concurrency", req.get("concurrency")), self.max_concurrency
            ),
            "timeout_s": self._min(
                req.get("timeout_s", req.get("max_duration_s")), self.max_duration_s
            ),
        }
        preserved = {k: v for k, v in req.items() if k not in {
            "max_bytes", "max_tokens", "max_concurrency", "concurrency", "timeout_s",
            "max_duration_s",
        }}
        effective.update(preserved)
        return {k: v for k, v in effective.items() if v is not None}

    @classmethod
    def from_executor(cls, caps: Mapping[str, Any] | None) -> "AgentCapabilities":
        """Converte `AgentExecutor.capabilities()` no vocabulário do §10.4.1."""
        data = dict(caps or {})
        conc = data.get("max_concurrency", data.get("concurrency"))
        if isinstance(conc, bool) or not isinstance(conc, int) or conc <= 0:
            conc = None
        tools = data.get("tools") or ()
        return cls(
            dispatch=bool(data.get("dispatch", False)),
            max_context_bytes=data.get("max_context_bytes"),
            max_context_tokens=data.get("max_context_tokens"),
            max_concurrency=conc,
            max_duration_s=data.get("max_duration_s"),
            supports_session=bool(data.get("supports_session", False)),
            physical_cancellation=str(data.get("cancellation", "")) == "process-kill",
            structured_output=bool(data.get("structured_output", False)),
            deepening=bool(data.get("deepening", False)),
            telemetry=bool(data.get("telemetry", False)),
            tools=tuple(str(t) for t in tools),
            reason=str(data.get("reason") or ""),
        )


# --------------------------------------------------------------------------
# Preparação executável (§10.4.6)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Prerequisite:
    """Pré-requisito com COMANDO de verificação — nunca uma frase solta."""

    prerequisite_id: str
    description: str
    check_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.prerequisite_id.strip() or not self.description.strip():
            raise AgentContractError("pré-requisito sem id/descrição")
        if not self.check_argv:
            raise AgentContractError(
                f"pré-requisito {self.prerequisite_id!r} sem comando de verificação"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prerequisite_id": self.prerequisite_id,
            "description": self.description,
            "check_argv": list(self.check_argv),
        }


@dataclass(frozen=True)
class SetupStep:
    """Passo de `agent setup`: comando real OU ação manual com verificação.

    `__post_init__` recusa o passo puramente narrativo ("configure o agente"):
    ou existe `argv` executável, ou existe `manual_action` acompanhado de
    `verify_argv` — sempre há como o operador saber, por execução, que o passo
    terminou (§10.4.6).
    """

    step_id: str
    description: str
    argv: tuple[str, ...] = ()
    manual_action: str = ""
    verify_argv: tuple[str, ...] = ()
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.step_id.strip() or not self.description.strip():
            raise AgentContractError("passo de preparação sem id/descrição")
        if not self.argv and not (self.manual_action.strip() and self.verify_argv):
            raise AgentContractError(
                f"passo {self.step_id!r} não é executável: informe argv, ou "
                "manual_action + verify_argv"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "argv": list(self.argv),
            "manual_action": self.manual_action,
            "verify_argv": list(self.verify_argv),
            "optional": self.optional,
        }


# --------------------------------------------------------------------------
# Disponibilidade de transporte (§10.4.3)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportAvailability:
    """Um transporte declarado e o que a VERIFICAÇÃO do adaptador achou dele.

    `available=True` significa que o adaptador exercitou a condição real do
    §10.4.3 (o binário responde, no `process`; a ponte responde ao handshake,
    no `session`) — nunca "o descritor cita o transporte". `detail` é o motivo
    verificado, e é ele que sai no erro quando o transporte pedido não serve.
    """

    transport: str
    available: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if self.transport not in TRANSPORTS:
            raise AgentContractError(
                f"available_transports devolveu transporte desconhecido: {self.transport!r}"
            )

    def __iter__(self):
        """Desempacotável como a tupla `(transport, available, detail)`."""
        yield from (self.transport, self.available, self.detail)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "available": self.available,
            # `detail` é texto livre do adaptador: sai redigido (§10.3).
            "detail": _public_text(self.detail),
        }

    @classmethod
    def coerce(cls, raw: Any) -> "TransportAvailability":
        """Aceita a própria classe, `(transport, available, detail)` ou mapa.

        Adaptador de terceiro não precisa importar este módulo para responder:
        a tupla do contrato basta.
        """
        if isinstance(raw, TransportAvailability):
            return raw
        if isinstance(raw, Mapping):
            return cls(
                transport=str(raw.get("transport") or ""),
                available=bool(raw.get("available", False)),
                detail=str(raw.get("detail") or ""),
            )
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            detail = str(raw[2]) if len(raw) > 2 and raw[2] is not None else ""
            return cls(transport=str(raw[0]), available=bool(raw[1]), detail=detail)
        raise AgentContractError(
            "available_transports precisa devolver (transport, available, detail); "
            f"recebido: {type(raw).__name__}"
        )


@dataclass(frozen=True)
class TransportChoice:
    """Resultado da seleção: qual transporte, e sob qual regime de evidência."""

    transport: str
    selection: str = SELECTION_DECLARED
    detail: str = ""
    options: tuple[TransportAvailability, ...] = ()

    def __post_init__(self) -> None:
        if self.transport not in TRANSPORTS:
            raise AgentContractError(f"transporte inválido na seleção: {self.transport!r}")
        if self.selection not in TRANSPORT_SELECTIONS:
            raise AgentContractError(f"transport_selection inválido: {self.selection!r}")

    @property
    def verified(self) -> bool:
        return self.selection == SELECTION_VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "transport_selection": self.selection,
            "detail": _public_text(self.detail),
            "transports": [o.to_dict() for o in self.options],
        }


def _setup_instruction(descriptor: "AgentDescriptor") -> str:
    """Instrução ESPECÍFICA daquele agente, montada dos passos executáveis."""
    parts: list[str] = []
    for step in descriptor.setup_steps:
        if step.argv:
            parts.append(f"{step.step_id}: {step.description} [{' '.join(step.argv)}]")
        else:
            parts.append(
                f"{step.step_id}: {step.description} "
                f"[manual: {step.manual_action}; verificar: {' '.join(step.verify_argv)}]"
            )
    if not parts:
        return (
            f"o adaptador de {descriptor.agent_id!r} não declara passo de preparação; "
            "corrija a integração antes de conectar"
        )
    return "; ".join(parts)


# --------------------------------------------------------------------------
# Descritor, binding, sonda
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentDescriptor:
    """O que `agent list`/`agent setup` publicam sobre uma integração."""

    agent_id: str
    adapter_version: str
    transports: tuple[str, ...]
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    #: Versões de host homologadas. VAZIO ⇒ não homologado (§10.4.5).
    verified_versions: tuple[str, ...] = ()
    prerequisites: tuple[Prerequisite, ...] = ()
    setup_steps: tuple[SetupStep, ...] = ()
    distribution: str = "bundled"
    summary: str = ""
    vendor: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _AGENT_ID_RE.match(self.agent_id or ""):
            raise AgentContractError(f"agent_id inválido: {self.agent_id!r}")
        if not str(self.adapter_version).strip():
            raise AgentContractError(f"agente {self.agent_id!r} sem adapter_version")
        if not self.transports:
            raise AgentContractError(f"agente {self.agent_id!r} sem transporte declarado")
        unknown = set(self.transports) - TRANSPORTS
        if unknown:
            raise AgentContractError(
                f"agente {self.agent_id!r} declara transporte desconhecido: {sorted(unknown)}"
            )
        if self.distribution not in DISTRIBUTIONS:
            raise AgentContractError(f"distribution inválida: {self.distribution!r}")

    @property
    def homologated(self) -> bool:
        return bool(self.verified_versions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "adapter_version": self.adapter_version,
            "transports": list(self.transports),
            "capabilities": self.capabilities.to_dict(),
            "verified_versions": list(self.verified_versions),
            "homologated": self.homologated,
            "prerequisites": [p.to_dict() for p in self.prerequisites],
            "setup_steps": [s.to_dict() for s in self.setup_steps],
            "distribution": self.distribution,
            "summary": self.summary,
            "vendor": dict(self.vendor),
        }


@dataclass(frozen=True)
class AgentBinding:
    """Vínculo imutável recebido pela execução ao iniciar (§10.4.6)."""

    binding_id: str
    agent_id: str
    adapter_version: str
    transport: str
    connected: bool = False
    established_at: str = ""
    #: Versão informada pelo HOST no handshake (não a homologada).
    host_version: str | None = None
    #: Modelo informado pelo host. `None` ⇒ indisponível; nunca inferido do
    #: nome do host (§10.4.2 item 7).
    model: str | None = None
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    detail: str = ""
    vendor: Mapping[str, Any] = field(default_factory=dict)
    #: `verified` ⇒ o transporte foi escolhido por disponibilidade CONFERIDA
    #: (`available_transports`); `declared` ⇒ adaptador sem verificação, valeu
    #: a ordem do descritor. Fica no registro para que ninguém leia a ordem
    #: declarada como prova de capacidade (§10.4.3).
    transport_selection: str = SELECTION_DECLARED

    def __post_init__(self) -> None:
        if not self.binding_id.strip():
            raise AgentContractError("binding sem binding_id")
        if not _AGENT_ID_RE.match(self.agent_id or ""):
            raise AgentContractError(f"binding com agent_id inválido: {self.agent_id!r}")
        if self.transport not in TRANSPORTS:
            raise AgentContractError(f"binding com transporte inválido: {self.transport!r}")
        if self.transport_selection not in TRANSPORT_SELECTIONS:
            raise AgentContractError(
                f"binding com transport_selection inválido: {self.transport_selection!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "agent_id": self.agent_id,
            "adapter_version": self.adapter_version,
            "transport": self.transport,
            "transport_selection": self.transport_selection,
            "connected": self.connected,
            "established_at": self.established_at,
            "host_version": self.host_version,
            "model": self.model,
            "model_available": self.model is not None,
            "capabilities": self.capabilities.to_dict(),
            # `detail` vem do handshake do host e é gravado em `agents.json`.
            "detail": _public_text(self.detail),
            "vendor": dict(self.vendor),
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Binding sem segredo — em TODA profundidade que tem área de fornecedor.

        Antes desta correção só o `vendor` do topo era filtrado:
        `capabilities.vendor` (a mesma área extensível, um nível abaixo) saía
        íntegro em `agent status`/`doctor` e era gravado em `agents.json`. As
        duas áreas passam agora pelo mesmo `_clean_public`.
        """
        data = self.to_dict()
        data["vendor"] = _clean_public(data["vendor"])
        data["capabilities"] = self.capabilities.to_public_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentBinding":
        caps = data.get("capabilities")
        capabilities = AgentCapabilities(
            **{
                k: v
                for k, v in dict(caps or {}).items()
                if k in AgentCapabilities.__dataclass_fields__
            }
        ) if isinstance(caps, Mapping) else AgentCapabilities()
        if isinstance(caps, Mapping) and isinstance(caps.get("tools"), list):
            capabilities = replace(capabilities, tools=tuple(caps["tools"]))
        return cls(
            binding_id=str(data.get("binding_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            adapter_version=str(data.get("adapter_version") or ""),
            transport=str(data.get("transport") or ""),
            # Registro gravado antes desta correção não tem o campo: `declared`
            # é a leitura HONESTA dele (nada foi verificado na época).
            transport_selection=str(data.get("transport_selection") or SELECTION_DECLARED),
            connected=bool(data.get("connected", False)),
            established_at=str(data.get("established_at") or ""),
            host_version=data.get("host_version"),
            model=data.get("model"),
            capabilities=capabilities,
            detail=str(data.get("detail") or ""),
            vendor=dict(data.get("vendor") or {}),
        )


def new_binding_id(agent_id: str) -> str:
    return f"bind-{agent_id}-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class ProbeResult:
    """Sonda do §10.1 com os QUATRO eixos distintos exigidos.

    `integration_available` — existe adaptador e transporte utilizável;
    `agent_connected`       — handshake vivo com o host;
    `access_usable`         — a tarefa mínima foi aceita e executada;
    `contract_accepted`     — o resultado passou pela validação de envelope.
    """

    integration_available: bool = False
    agent_connected: bool = False
    access_usable: bool = False
    contract_accepted: bool = False
    detail: str = ""
    diagnostics: tuple[str, ...] = ()
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    next_actions: tuple[Mapping[str, Any], ...] = ()
    usage: Mapping[str, Any] | None = None
    #: Mesmo vocabulário do binding: `verified` só quando o adaptador
    #: verificou a disponibilidade dos transportes (§10.4.3).
    transport_selection: str = SELECTION_DECLARED
    #: Disponibilidade conferida por transporte (vazio no regime `declared`).
    transports: tuple[TransportAvailability, ...] = ()

    @property
    def ok(self) -> bool:
        return (
            self.integration_available
            and self.agent_connected
            and self.access_usable
            and self.contract_accepted
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "integration_available": self.integration_available,
            "agent_connected": self.agent_connected,
            "access_usable": self.access_usable,
            "contract_accepted": self.contract_accepted,
            "ok": self.ok,
            "detail": _public_text(self.detail),
            "diagnostics": [_public_text(d) for d in self.diagnostics],
            "capabilities": self.capabilities.to_dict(),
            "next_actions": [dict(a) for a in self.next_actions],
            "usage": dict(self.usage) if self.usage is not None else None,
            "transport_selection": self.transport_selection,
            "transports": [t.to_dict() for t in self.transports],
        }


# --------------------------------------------------------------------------
# Protocolo do adaptador (§10.4.4)
# --------------------------------------------------------------------------


@runtime_checkable
class AgentAdapter(Protocol):
    """API mínima: `describe`, `connect`, `probe`, `submit`, `poll`, `cancel`, `close`."""

    def describe(self) -> AgentDescriptor: ...

    def connect(self, transport: str, config: Mapping[str, Any]) -> AgentBinding: ...

    def probe(self, binding: AgentBinding) -> ProbeResult: ...

    def submit(self, binding: AgentBinding, envelope: Mapping[str, Any]) -> str: ...

    def poll(self, binding: AgentBinding, execution_id: str) -> Mapping[str, Any]: ...

    def cancel(self, binding: AgentBinding, execution_id: str) -> bool: ...

    def close(self, binding: AgentBinding) -> None: ...


@runtime_checkable
class TransportProbingAdapter(Protocol):
    """Extensão OPCIONAL do contrato: disponibilidade verificada por transporte.

    Quem implementa `available_transports(config)` participa da seleção
    `verified` do §10.4.3; quem não implementa continua válido como adaptador
    (a API mínima não muda) e cai no regime `declared`. Por isso este protocolo
    é separado de `AgentAdapter`: exigir o método lá invalidaria adaptadores
    já registrados por extensão.
    """

    def available_transports(
        self, config: Mapping[str, Any] | None = ...
    ) -> Sequence[Any]: ...


# --------------------------------------------------------------------------
# Registro extensível (§10.4.2 item 10)
# --------------------------------------------------------------------------


class AgentRegistry:
    """Registro SEM enumeração fechada.

    O núcleo não conhece uma lista de IDs válidos: `register()` é público e
    aceita qualquer objeto que satisfaça `AgentAdapter`. `default()` registra
    apenas os adaptadores com integração REAL neste repositório; qualquer
    outro agente entra por extensão (`load_extensions`).
    """

    def __init__(self, adapters: Sequence[AgentAdapter] = ()) -> None:
        self._adapters: dict[str, AgentAdapter] = {}
        #: Memória da operação corrente (§10.4.3): dentro de um `connect`/
        #: `probe` o descritor e a disponibilidade de transporte são lidos uma
        #: vez só, em vez de uma vez por chamada.
        self._operation_cache = OperationCache()
        for adapter in adapters:
            self.register(adapter)

    @contextlib.contextmanager
    def operation(self, agent_id: str):
        """Janela de UMA operação sobre `agent_id`: 1 handshake real.

        Abre a memória do registro E a do adaptador (quando ele oferece
        `operation_scope`), de modo que `describe`, `available_transports` e o
        handshake do `connect` compartilhem o MESMO resultado verificado. É
        reentrante: `connect` chamando `resolve_transport_choice` não reinicia
        a janela nem descarta o que já foi verificado.
        """
        try:
            adapter: Any = self._adapters.get(agent_id)
        except TypeError:  # pragma: no cover - agent_id não-hasheável
            adapter = None
        with self._operation_cache.scope():
            if adapter is None:
                yield
                return
            with _adapter_scope(adapter):
                yield

    # -- registro ----------------------------------------------------------

    def register(self, adapter: AgentAdapter, *, replace_existing: bool = False) -> AgentDescriptor:
        describe = getattr(adapter, "describe", None)
        if not callable(describe):
            raise AgentContractError(
                f"objeto sem describe() não é adaptador: {type(adapter).__name__}"
            )
        for name in ("connect", "probe", "submit", "poll", "cancel", "close"):
            if not callable(getattr(adapter, name, None)):
                raise AgentContractError(
                    f"adaptador {type(adapter).__name__} não implementa {name}()"
                )
        descriptor = describe()
        if not isinstance(descriptor, AgentDescriptor):
            raise AgentContractError("describe() precisa devolver AgentDescriptor")
        if descriptor.agent_id in self._adapters and not replace_existing:
            raise AgentContractError(f"adaptador já registrado: {descriptor.agent_id}")
        self._adapters[descriptor.agent_id] = adapter
        return descriptor

    def unregister(self, agent_id: str) -> bool:
        return self._adapters.pop(agent_id, None) is not None

    def __contains__(self, agent_id: object) -> bool:
        return str(agent_id) in self._adapters

    def agent_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def list(self) -> tuple[AgentDescriptor, ...]:
        return tuple(self._adapters[k].describe() for k in sorted(self._adapters))

    def describe(self, agent_id: str) -> AgentDescriptor:
        """Descritor do agente — UMA vez por operação aberta.

        `resolve_transport_choice` e `transport_options` precisam do descritor
        no mesmo `connect`; em adaptador cujo `describe()` sonda o host (o
        `claude-code` roda `claude --version` ali), repetir a chamada era
        repetir o subprocesso.
        """
        return self._operation_cache.get(
            ("describe", agent_id), lambda: self.adapter(agent_id).describe()
        )

    def adapter(self, agent_id: str) -> AgentAdapter:
        try:
            return self._adapters[agent_id]
        except KeyError:
            raise AgentUnavailableError(
                f"nenhum adaptador registrado para {agent_id!r} "
                f"(registrados: {list(self.agent_ids())}); registre uma extensão "
                f"em {EXTENSIONS_ENV} — o núcleo não escolhe outro agente por conta própria"
            ) from None

    # -- extensões ---------------------------------------------------------

    def load_extensions(
        self, specs: Iterable[str] | None = None, *, env_var: str = EXTENSIONS_ENV
    ) -> tuple[AgentDescriptor, ...]:
        """Registra adaptadores de terceiros por `pacote.modulo:fabrica`.

        A fábrica devolve um adaptador ou uma sequência deles. Falha de import
        vira `AgentContractError` com o spec problemático — nunca um registro
        silencioso pela metade.
        """
        raw = list(specs) if specs is not None else [
            s.strip() for s in os.environ.get(env_var, "").split(os.pathsep) if s.strip()
        ]
        registered: list[AgentDescriptor] = []
        for spec in raw:
            if ":" not in spec:
                raise AgentContractError(
                    f"extensão inválida {spec!r}: use 'pacote.modulo:fabrica'"
                )
            module_name, _, factory_name = spec.partition(":")
            try:
                module = importlib.import_module(module_name)
                factory = getattr(module, factory_name)
            except Exception as exc:
                raise AgentContractError(
                    f"extensão {spec!r} não carregou: {type(exc).__name__}: {exc}"
                ) from exc
            produced = factory()
            candidates = produced if isinstance(produced, (list, tuple)) else [produced]
            for adapter in candidates:
                registered.append(self.register(adapter))
        return tuple(registered)

    # -- ciclo de vida -----------------------------------------------------

    def transport_options(
        self, agent_id: str, config: Mapping[str, Any] | None = None
    ) -> tuple[TransportAvailability, ...] | None:
        """Disponibilidade VERIFICADA de cada transporte declarado.

        `None` ⇒ o adaptador não implementa `available_transports`: não há o
        que verificar e a seleção fica no regime `declared`. A ordem devolvida
        é sempre a do descritor (a preferência é de quem declara o agente, não
        de quem responde a sonda), e transporte declarado que o adaptador não
        reportou vira INDISPONÍVEL — omissão não é disponibilidade.
        """
        return self._operation_cache.get(
            ("transport_options", agent_id, config_cache_key(config)),
            lambda: self._transport_options_uncached(agent_id, config),
        )

    def _transport_options_uncached(
        self, agent_id: str, config: Mapping[str, Any] | None
    ) -> tuple[TransportAvailability, ...] | None:
        adapter = self.adapter(agent_id)
        probe_fn = getattr(adapter, "available_transports", None)
        if not callable(probe_fn):
            return None
        try:
            raw = probe_fn(dict(config or {}))
        except AgentUnavailableError:
            raise
        except Exception as exc:
            raise AgentContractError(
                f"available_transports() de {agent_id!r} falhou: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if raw is None:
            return None
        if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, (list, tuple, set)):
            raise AgentContractError(
                f"available_transports() de {agent_id!r} precisa devolver uma sequência "
                f"de (transport, available, detail); recebido: {type(raw).__name__}"
            )
        reported = {}
        for item in raw:
            option = TransportAvailability.coerce(item)
            reported[option.transport] = option
        descriptor = self.describe(agent_id)
        return tuple(
            reported.get(
                name,
                TransportAvailability(
                    transport=name,
                    available=False,
                    detail="adaptador não verificou este transporte",
                ),
            )
            for name in descriptor.transports
        )

    def resolve_transport_choice(
        self,
        agent_id: str,
        transport: str = "auto",
        config: Mapping[str, Any] | None = None,
    ) -> TransportChoice:
        """Seleção do §10.4.3: `auto` é capacidade verificada, não adivinhação.

        * Adaptador com `available_transports`: `auto` devolve o PRIMEIRO
          transporte disponível na ordem do descritor; nenhum disponível ⇒
          `TransportUnavailable` com os passos de preparação do agente.
        * Transporte explícito indisponível ⇒ `TransportUnavailable` com o
          detalhe verificado DAQUELE transporte (nunca cai em outro).
        * Adaptador sem o método: primeiro declarado, marcado `declared`.
        """
        with self.operation(agent_id):
            return self._resolve_transport_choice(agent_id, transport, config)

    def _resolve_transport_choice(
        self,
        agent_id: str,
        transport: str,
        config: Mapping[str, Any] | None,
    ) -> TransportChoice:
        descriptor = self.describe(agent_id)
        requested = "" if transport in ("", "auto", None) else str(transport)
        if requested and requested not in descriptor.transports:
            raise AgentUnavailableError(
                f"agente {agent_id!r} não oferece transporte {requested!r} "
                f"(disponíveis: {list(descriptor.transports)})"
            )
        options = self.transport_options(agent_id, config)
        if options is None:
            return TransportChoice(
                transport=requested or descriptor.transports[0],
                selection=SELECTION_DECLARED,
                detail=(
                    f"adaptador de {agent_id!r} não verifica transporte "
                    "(available_transports ausente): vale a ordem declarada"
                ),
            )
        by_name = {o.transport: o for o in options}
        if requested:
            option = by_name[requested]
            if not option.available:
                raise TransportUnavailable(
                    f"agente {agent_id!r}: transporte {requested!r} indisponível "
                    f"({option.detail or 'sem detalhe do adaptador'}); "
                    f"prepare com: {_setup_instruction(descriptor)}",
                    agent_id=agent_id,
                    transport=requested,
                    options=options,
                    setup_steps=descriptor.setup_steps,
                )
            return TransportChoice(
                transport=requested,
                selection=SELECTION_VERIFIED,
                detail=option.detail,
                options=options,
            )
        for option in options:
            if option.available:
                return TransportChoice(
                    transport=option.transport,
                    selection=SELECTION_VERIFIED,
                    detail=option.detail,
                    options=options,
                )
        situation = "; ".join(
            f"{o.transport}: {o.detail or 'indisponível'}" for o in options
        )
        raise TransportUnavailable(
            f"agente {agent_id!r} sem transporte disponível ({situation}); "
            f"prepare com: {_setup_instruction(descriptor)}",
            agent_id=agent_id,
            transport="auto",
            options=options,
            setup_steps=descriptor.setup_steps,
        )

    def resolve_transport(
        self,
        agent_id: str,
        transport: str = "auto",
        config: Mapping[str, Any] | None = None,
    ) -> str:
        """Nome do transporte escolhido (a decisão inteira está em
        `resolve_transport_choice`)."""
        return self.resolve_transport_choice(agent_id, transport, config).transport

    def connect(
        self,
        agent_id: str,
        transport: str = "auto",
        config: Mapping[str, Any] | None = None,
    ) -> AgentBinding:
        """Conecta SEM fallback: erro aqui nunca vira outro agente (§10.4.2 item 5).

        Tudo isto é UMA operação: a seleção de transporte e o handshake do
        adaptador compartilham a mesma verificação do host (§10.4.3), então um
        `connect` custa UM handshake real — não um por chamada de `describe`/
        `available_transports`/`connect`.
        """
        with self.operation(agent_id):
            return self._connect(agent_id, transport, config)

    def _connect(
        self,
        agent_id: str,
        transport: str,
        config: Mapping[str, Any] | None,
    ) -> AgentBinding:
        adapter = self.adapter(agent_id)
        settings = dict(config or {})
        # §10.4.3: a escolha vem ANTES do connect e olha capacidade verificada.
        # Sem transporte disponível não há tentativa nenhuma — `TransportUnavailable`
        # já traz a instrução daquele agente.
        choice = self.resolve_transport_choice(agent_id, transport, settings)
        chosen = choice.transport
        binding = adapter.connect(chosen, settings)
        if not isinstance(binding, AgentBinding):
            # Não há binding para fechar: o adaptador devolveu outra coisa.
            raise AgentContractError("connect() precisa devolver AgentBinding")
        # A partir daqui o adaptador JÁ construiu o executor deste binding.
        # Qualquer recusa precisa fechá-lo antes de propagar: sem isto, um
        # `connect` recusado deixava processo/pool vivo e sem dono (ninguém
        # tem o binding para chamar `close`).
        if binding.agent_id != agent_id or binding.transport != chosen:
            detail = (
                "binding divergente do agente/transporte solicitado: "
                f"{binding.agent_id!r}/{binding.transport!r} em vez de "
                f"{agent_id!r}/{chosen!r}"
            )
            self._close_quietly(adapter, binding)
            raise HandshakeFailed(f"handshake com {agent_id!r} recusado: {detail}")
        if not binding.connected:
            detail = binding.detail or "sem detalhe"
            self._close_quietly(adapter, binding)
            raise HandshakeFailed(f"handshake com {agent_id!r} não concluído: {detail}")
        # Quem selecionou o transporte é este registro: é ele que sabe se a
        # escolha foi verificada ou apenas declarada. O adaptador não pode
        # carimbar `verified` por conta própria.
        return replace(binding, transport_selection=choice.selection)

    @staticmethod
    def _close_quietly(adapter: Any, binding: AgentBinding) -> None:
        """Fecha o recurso do binding recusado sem mascarar a recusa original.

        `close()` é o único dono do subprocesso/pool criado por `connect()`;
        uma falha AO FECHAR não pode virar a exceção que o chamador vê, senão
        o motivo real da recusa (transporte/handshake) se perde.
        """
        closer = getattr(adapter, "close", None)
        if not callable(closer):
            return
        try:
            closer(binding)
        except Exception:  # pragma: no cover - defensivo
            pass

    def probe(self, binding: AgentBinding) -> ProbeResult:
        with self.operation(binding.agent_id):
            return self.adapter(binding.agent_id).probe(binding)

    def submit(self, binding: AgentBinding, envelope: Mapping[str, Any]) -> str:
        return self.adapter(binding.agent_id).submit(binding, envelope)

    def poll(self, binding: AgentBinding, execution_id: str) -> Mapping[str, Any]:
        return self.adapter(binding.agent_id).poll(binding, execution_id)

    def cancel(self, binding: AgentBinding, execution_id: str) -> bool:
        return self.adapter(binding.agent_id).cancel(binding, execution_id)

    def close(self, binding: AgentBinding) -> None:
        """Fecha o binding e DESCARTA o que foi memoizado sobre o host.

        Depois de `close` nada do que foi verificado continua valendo: a
        próxima operação verifica o host de novo, e nenhuma janela sobrevive
        ao fim da conexão que a motivou.
        """
        try:
            self.adapter(binding.agent_id).close(binding)
        finally:
            self._operation_cache.clear()

    # -- distribuição padrão ------------------------------------------------

    @classmethod
    def default(cls, *, extensions: bool = True, **kwargs: Any) -> "AgentRegistry":
        return default_registry(extensions=extensions, **kwargs)


def default_registry(
    *,
    extensions: bool = True,
    local_task_registry: Mapping[str, Any] | None = None,
    local_max_workers: int = 4,
    claude_binary: Any = "claude",
) -> AgentRegistry:
    """Registro com os adaptadores REAIS deste repositório.

    Somente dois: `local` (worker determinístico controlado, hoje
    `LocalThreadExecutor`) e `claude-code` (transporte `process` sobre
    `ClaudeCliExecutor`). `devin-cli`, `cursor`, `antigravity`, `windsurf` e
    `codex` NÃO são registrados: não existe integração real neste ambiente e
    o §10.4.5 proíbe declarar suporte por ID. Eles entram como extensão
    (`AgentRegistry.load_extensions` / `WIKI_AI_AGENT_ADAPTERS`).
    """
    from .executors.agent_adapters import ClaudeCodeAgentAdapter, LocalAgentAdapter

    registry = AgentRegistry()
    registry.register(
        LocalAgentAdapter(task_registry=local_task_registry, max_workers=local_max_workers)
    )
    registry.register(ClaudeCodeAgentAdapter(binary_path=claude_binary))
    if extensions:
        registry.load_extensions()
    return registry


__all__ = [
    "DISTRIBUTIONS",
    "EXTENSIONS_ENV",
    "SELECTION_DECLARED",
    "SELECTION_VERIFIED",
    "TRANSPORTS",
    "TRANSPORT_SELECTIONS",
    "AgentAdapter",
    "AgentBinding",
    "AgentCapabilities",
    "AgentContractError",
    "AgentDescriptor",
    "AgentRegistry",
    "AgentUnavailableError",
    "HandshakeFailed",
    "OperationCache",
    "Prerequisite",
    "ProbeResult",
    "SetupStep",
    "TransportAvailability",
    "TransportChoice",
    "TransportProbingAdapter",
    "TransportUnavailable",
    "REDACTED",
    "config_cache_key",
    "default_registry",
    "new_binding_id",
    "redact_secrets",
]
