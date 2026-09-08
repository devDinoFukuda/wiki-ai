"""Adaptadores de agente sobre os executores reais (spec §10.4.3-§10.4.5).

Aqui os executores concretos deixam de ser uma rota própria e passam a ser o
MECANISMO INTERNO de um adaptador:

| Agente        | ID público    | Transporte | Mecanismo                     |
|---------------|---------------|------------|-------------------------------|
| worker local  | `local`       | `process`  | `LocalThreadExecutor`         |
| Claude Code   | `claude-code` | `process`  | `ClaudeCliExecutor` (CLI)     |

Nenhum outro agente é registrado por padrão: `devin-cli`, `cursor`,
`antigravity`, `windsurf` e `codex` não têm integração real neste ambiente, e
o §10.4.5 proíbe declarar suporte só porque o ID existe. Eles entram por
extensão (`AgentRegistry.load_extensions`), com o mesmo contrato.

`probe()` do adaptador base executa a tarefa mínima PELO MESMO caminho do
despacho real (`submit` → `poll` → `envelopes.validate_result`), como o §10.1
exige, e devolve os quatro eixos distintos (integração disponível / agente
conectado / acesso utilizável / contrato de resposta aceito).

Somente stdlib. Nenhuma rede além do processo do próprio agente.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Callable, Mapping

from .. import agents as A
from .. import envelopes as E
from .base import ExecutionState
from .claude_cli import ClaudeCliExecutor, _as_command, _minimal_env
from .local_thread import LocalThreadExecutor

#: Vocabulário do `state` devolvido por `AgentExecutor.status()`.
_TERMINAL_OK = frozenset({ExecutionState.DONE, "succeeded", "success", "completed", "complete"})
_TERMINAL_BAD = frozenset(
    {ExecutionState.FAILED, ExecutionState.CANCELLED, "error", "timeout", "canceled", "aborted"}
)

_PROBE_KIND = "__wiki_ai_probe__"
_PROBE_TIMEOUT_S = 30.0

#: Sentinela de "argumento não informado" — `None` é um valor legítimo de
#: `binary_path` (e precisa produzir "binary_path vazio", não o default).
_UNSET = object()


def _probe_callable(objective=None, references=None, schema=None, cancel_event=None):
    """Tarefa mínima da sonda: nenhum código do usuário é lido ou executado."""
    return {
        "objective_id": "probe",
        "notes": "probe: contrato de resposta exercitado pelo mesmo despacho",
    }


class BaseExecutorAdapter:
    """Base dos adaptadores que despacham por um `AgentExecutor`.

    Implementa `submit/poll/cancel/close/probe` uma única vez; a subclasse
    fornece `describe()`, `_handshake()` e `_new_executor()`.
    """

    #: Sobrescrito pela subclasse.
    agent_id = ""
    adapter_version = "1"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        #: Memória da operação corrente (§10.4.3). `connect` com `auto` chama
        #: `describe()`, `available_transports(config)` e o handshake do
        #: `connect`: sem esta janela, cada um repetia a verificação REAL do
        #: host. Ela vive só enquanto a operação está aberta — não é cache de
        #: disponibilidade, é a mesma resposta lida uma vez.
        self._operation_cache = A.OperationCache()
        self._executors: dict[str, Any] = {}
        #: envelope EM VOO por `execution_id`. Removido assim que a execucao
        #: chega a estado terminal (aceita, recusada, cancelada ou falha): o
        #: dicionario e buffer de despacho, nunca historico. A idempotencia de
        #: "mesmo resultado duas vezes" NAO depende dele - ela vem de
        #: `attempts.result_hash`, persistido em `runtime.db` por
        #: `TaskStore.record_result` e conferido em `coordinator.accept_result`.
        self._envelopes: dict[str, E.TaskEnvelope] = {}
        self._bindings: dict[str, A.AgentBinding] = {}
        #: bindings ja fechados: usar o executor depois de `close()` e recusa
        #: explicita de contrato, nunca `RuntimeError` opaco de pool morto.
        self._closed: set[str] = set()

    # -- contrato: describe/connect (subclasse) -----------------------------

    def describe(self) -> A.AgentDescriptor:  # pragma: no cover - abstrato
        raise NotImplementedError

    def _handshake(self, transport: str, config: Mapping[str, Any]) -> dict[str, Any]:
        """Verificação REAL da integração.

        Devolve `{"connected": bool, "detail": str, "host_version": str|None,
        "model": str|None}`. `model=None` significa indisponível — nunca é
        inferido do nome do host.
        """
        raise NotImplementedError  # pragma: no cover - abstrato

    def _new_executor(self, config: Mapping[str, Any]) -> Any:  # pragma: no cover - abstrato
        raise NotImplementedError

    def new_executor(self, **config: Any) -> Any:
        """Constrói o executor deste adaptador — usada pela fachada `get_executor`."""
        return self._new_executor(dict(config))

    # -- janela de operação: 1 handshake real por connect/probe -------------

    def operation_scope(self):
        """Janela reentrante em que o handshake real acontece UMA vez.

        `AgentRegistry.operation` abre esta janela antes de resolver o
        transporte, de modo que a seleção do §10.4.3 e o `connect` que a segue
        leiam a MESMA verificação do host. Abrir a janela por conta própria
        (`connect`/`probe`/`available_transports` fazem isso) garante o mesmo
        quando o adaptador é usado sem passar pelo registro.
        """
        return self._operation_cache.scope()

    def _handshake_once(self, transport: str, config: Mapping[str, Any]) -> dict[str, Any]:
        """`_handshake` memoizado por `(transporte, config)` DENTRO da janela.

        Fora de uma janela aberta o comportamento é o histórico: verifica
        sempre. Exceção não é memoizada — o próximo chamador tenta de novo.
        """
        key = ("handshake", transport, A.config_cache_key(config))
        outcome = self._operation_cache.get(
            key, lambda: dict(self._handshake(transport, config) or {})
        )
        return dict(outcome)

    # -- disponibilidade verificada por transporte (§10.4.3) ----------------

    def available_transports(
        self, config: Mapping[str, Any] | None = None
    ) -> tuple[A.TransportAvailability, ...]:
        """Quais transportes DECLARADOS estão de fato utilizáveis agora.

        É o que faz `auto` ser seleção por capacidade verificada em vez de
        "primeiro da lista": cada transporte é exercitado por
        `_verify_transport` (por padrão, o próprio handshake — o binário que
        responde no `process`, a ponte que responde no `session`), sem
        construir executor nem despachar tarefa.
        """
        settings = dict(config or {})
        with self.operation_scope():
            return tuple(
                A.TransportAvailability(transport, available, detail)
                for transport, (available, detail) in (
                    (t, self._verify_transport(t, settings))
                    for t in self.describe().transports
                )
            )

    def _verify_transport(self, transport: str, config: Mapping[str, Any]) -> tuple[bool, str]:
        """Verificação de UM transporte: `(disponível, detalhe)`.

        O handshake é a verificação real do §10.4.3 para os dois transportes;
        um adaptador cuja verificação seja mais barata (ou diferente por
        transporte) sobrescreve este método. Exceção do host vira
        indisponibilidade COM detalhe — nunca sobe crua para a seleção.
        """
        try:
            outcome = self._handshake_once(transport, config) or {}
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return bool(outcome.get("connected")), str(outcome.get("detail") or "")

    # -- connect ------------------------------------------------------------

    def connect(self, transport: str, config: Mapping[str, Any]) -> A.AgentBinding:
        with self.operation_scope():
            return self._connect(transport, dict(config or {}))

    def _connect(self, transport: str, config: Mapping[str, Any]) -> A.AgentBinding:
        descriptor = self.describe()
        if transport not in descriptor.transports:
            raise A.AgentUnavailableError(
                f"agente {descriptor.agent_id!r} não oferece transporte {transport!r}"
            )
        outcome = self._handshake_once(transport, dict(config or {}))
        if not outcome.get("connected"):
            raise A.HandshakeFailed(
                f"{descriptor.agent_id}: {outcome.get('detail') or 'handshake sem resposta'}"
            )
        binding = A.AgentBinding(
            binding_id=A.new_binding_id(descriptor.agent_id),
            agent_id=descriptor.agent_id,
            adapter_version=descriptor.adapter_version,
            transport=transport,
            connected=True,
            established_at=_now(),
            host_version=outcome.get("host_version"),
            model=outcome.get("model"),
            capabilities=descriptor.capabilities,
            detail=str(outcome.get("detail") or ""),
            vendor=dict(outcome.get("vendor") or {}),
        )
        executor = self._new_executor(dict(config or {}))
        with self._lock:
            self._bindings[binding.binding_id] = binding
            self._executors[binding.binding_id] = executor
            self._closed.discard(binding.binding_id)
        return binding

    def executor_for(self, binding: A.AgentBinding) -> Any:
        with self._lock:
            executor = self._executors.get(binding.binding_id)
            closed = binding.binding_id in self._closed
        if executor is None:
            if closed:
                raise A.AgentUnavailableError(
                    f"binding {binding.binding_id!r} já foi fechado neste adaptador "
                    "(close() encerrou o executor; reconecte antes de despachar)"
                )
            raise A.AgentUnavailableError(
                f"binding {binding.binding_id!r} não está conectado neste adaptador "
                "(conecte antes de despachar; nenhuma conexão é assumida)"
            )
        return executor

    # -- submit/poll/cancel/close -------------------------------------------

    def submit(self, binding: A.AgentBinding, envelope: Mapping[str, Any]) -> str:
        env = envelope if isinstance(envelope, E.TaskEnvelope) else E.validate_task(envelope)
        executor = self.executor_for(binding)
        # A política de execução viaja na área declarada `vendor` do envelope;
        # os limites efetivos são o mínimo entre o pedido e o que o agente
        # declarou suportar (§10.4.4). Nada de obrigação/evidência é removido
        # aqui — só limites são clampados.
        negotiated = binding.capabilities.negotiate(dict(env.vendor.get("policy") or {}))
        execution_id = executor.submit(
            env.task_id,
            dict(env.objective),
            list(env.evidence),
            dict(env.result_schema),
            negotiated,
        )
        with self._lock:
            self._envelopes[execution_id] = env.with_execution_id(execution_id)
        return execution_id

    def poll(self, binding: A.AgentBinding, execution_id: str) -> dict[str, Any]:
        executor = self.executor_for(binding)
        observed = executor.status(execution_id) or {}
        state = str(observed.get("state", "running")).lower()
        answer: dict[str, Any] = {
            "state": state,
            "heartbeat": observed.get("heartbeat"),
            "result": None,
            "error": None,
        }
        if state in _TERMINAL_BAD:
            answer["error"] = str(observed.get("detail") or self._error_detail(executor, execution_id) or state)
            with self._lock:
                self._envelopes.pop(execution_id, None)
            return answer
        if state not in _TERMINAL_OK:
            return answer
        raw = executor.result(execution_id)
        with self._lock:
            env = self._envelopes.pop(execution_id, None)
        if env is None:
            answer["error"] = (
                f"execução {execution_id!r} não foi submetida por este adaptador"
            )
            answer["state"] = ExecutionState.FAILED
            return answer
        provenance = E.Provenance.from_binding(binding)
        answer["result"] = self._normalize_result(env, raw, provenance, execution_id)
        answer["error"] = (raw or {}).get("error") if isinstance(raw, Mapping) else None
        return answer

    def _normalize_result(
        self,
        env: E.TaskEnvelope,
        raw: Any,
        provenance: E.Provenance,
        execution_id: str,
    ) -> dict[str, Any]:
        """Saída do executor → envelope de resultado fechado.

        Identidades vêm do envelope emitido pelo controlador (F07), nunca do
        payload do worker. Saída textual passa por `adapt_agent_text` — e por
        isso texto não parseável vira diagnóstico, nunca `done` vazio.
        """
        payload = raw.get("output") if isinstance(raw, Mapping) else raw
        payload = self._extract_payload(payload)
        if isinstance(payload, Mapping):
            return E.build_result(
                env,
                execution_id=execution_id,
                provenance=provenance,
                claims=dict(payload),
                usage=raw.get("usage") if isinstance(raw, Mapping) else None,
            )
        return E.adapt_agent_text(payload, env, provenance, execution_id=execution_id)

    def _extract_payload(self, payload: Any) -> Any:
        """Gancho de transformação específica do host (não do significado)."""
        return payload

    @staticmethod
    def _error_detail(executor: Any, execution_id: str) -> str:
        try:
            envelope = executor.result(execution_id)
        except Exception:
            return ""
        if not isinstance(envelope, Mapping):
            return ""
        error = envelope.get("error")
        return "" if error is None else str(error)

    def cancel(self, binding: A.AgentBinding, execution_id: str) -> bool:
        executor = self.executor_for(binding)
        try:
            return bool(executor.cancel(execution_id))
        finally:
            with self._lock:
                self._envelopes.pop(execution_id, None)

    def close(self, binding: A.AgentBinding) -> None:
        """Encerra o executor do binding. IDEMPOTENTE e seguro sob concorrência.

        A remoção do mapa acontece sob o lock; o `shutdown` (que pode bloquear
        esperando processo/thread) acontece FORA dele — segurar o lock durante
        um `wait()` congelaria `submit`/`poll` de qualquer outro binding deste
        mesmo adaptador. Depois disto `executor_for` recusa o binding com
        `AgentUnavailableError`, em vez de deixar um pool já encerrado levantar
        `RuntimeError` no meio do despacho.
        """
        with self._lock:
            executor = self._executors.pop(binding.binding_id, None)
            self._bindings.pop(binding.binding_id, None)
            self._closed.add(binding.binding_id)
        # Nada do que foi verificado sobre o host continua valendo depois de
        # fechar: a próxima operação sonda de novo.
        self._operation_cache.clear()
        if executor is None:
            return  # já fechado: no-op
        shutdown = getattr(executor, "shutdown", None)
        if callable(shutdown):
            shutdown(wait=True)

    # -- probe (§10.1: mesmo mecanismo do despacho real) --------------------

    def _probe_envelope(self) -> E.TaskEnvelope:
        return E.TaskEnvelope(
            task_id=f"probe-{uuid.uuid4().hex[:12]}",
            objective_id="probe",
            input_revision="probe",
            context_hash="",
            objective=self._probe_objective(),
            result_schema={"version": "worker_result/1", "required": ["objective_id"]},
            budget={"max_tokens": 512},
            lease_id="probe",
            attempt_id="probe",
            vendor={"policy": {"timeout_s": _PROBE_TIMEOUT_S}},
        )

    def _probe_objective(self) -> dict[str, Any]:
        return {"kind": _PROBE_KIND, "objective_id": "probe"}

    def probe(
        self, binding: A.AgentBinding, *, timeout_s: float = _PROBE_TIMEOUT_S
    ) -> A.ProbeResult:
        """Quatro eixos do §10.1 + o REGIME da seleção de transporte (§10.4.3).

        `transport_selection` sai da sonda junto com os eixos para que a saída
        pública nunca deixe "primeiro transporte declarado" passar por
        "transporte verificado".
        """
        with self.operation_scope():
            result = self._probe_axes(binding, timeout_s=timeout_s)
            selection, options = self._transport_evidence(binding)
        return replace(result, transport_selection=selection, transports=options)

    def _transport_evidence(
        self, binding: A.AgentBinding
    ) -> tuple[str, tuple[A.TransportAvailability, ...]]:
        """Regime da seleção + disponibilidade conferida agora (quando existe)."""
        probe_fn = getattr(self, "available_transports", None)
        if not callable(probe_fn):
            return A.SELECTION_DECLARED, ()
        try:
            options = tuple(A.TransportAvailability.coerce(o) for o in probe_fn({}))
        except Exception:
            options = ()
        return binding.transport_selection or A.SELECTION_DECLARED, options

    def _probe_axes(
        self, binding: A.AgentBinding, *, timeout_s: float = _PROBE_TIMEOUT_S
    ) -> A.ProbeResult:
        descriptor = self.describe()
        integration = bool(descriptor.transports) and descriptor.capabilities.dispatch
        diagnostics: list[str] = []
        if not binding.connected:
            return A.ProbeResult(
                integration_available=integration,
                detail="binding não conectado: execute agent connect antes da sonda",
                capabilities=descriptor.capabilities,
                next_actions=(
                    {
                        "actor": "operador",
                        "reason": "conexão ausente",
                        "argv": ["wk", "agent", "connect", "--agent", descriptor.agent_id],
                    },
                ),
            )
        env = self._probe_envelope()
        try:
            execution_id = self.submit(binding, env)
        except Exception as exc:
            return A.ProbeResult(
                integration_available=integration,
                agent_connected=True,
                detail=f"submissão da tarefa mínima falhou: {type(exc).__name__}: {exc}",
                diagnostics=(str(exc),),
                capabilities=descriptor.capabilities,
            )
        deadline = time.time() + timeout_s
        observed: dict[str, Any] = {}
        while time.time() < deadline:
            observed = self.poll(binding, execution_id)
            if observed.get("result") is not None or observed.get("error"):
                break
            if str(observed.get("state")) in _TERMINAL_OK | _TERMINAL_BAD:
                break
            time.sleep(0.01)
        else:
            self.cancel(binding, execution_id)
            return A.ProbeResult(
                integration_available=integration,
                agent_connected=True,
                detail=f"tarefa mínima não terminou em {timeout_s}s",
                diagnostics=("timeout na sonda",),
                capabilities=descriptor.capabilities,
            )
        candidate = observed.get("result")
        if candidate is None:
            return A.ProbeResult(
                integration_available=integration,
                agent_connected=True,
                detail=f"acesso não utilizável: {observed.get('error') or observed.get('state')}",
                diagnostics=(str(observed.get("error") or ""),),
                capabilities=descriptor.capabilities,
            )
        # `poll` ja descartou o envelope em voo (buffer, nunca historico); a
        # sonda conhece o envelope que ela mesma montou.
        submitted = env.with_execution_id(execution_id)
        try:
            result = E.validate_result(candidate, submitted, binding_id=binding.binding_id)
        except E.EnvelopeError as exc:
            return A.ProbeResult(
                integration_available=integration,
                agent_connected=True,
                access_usable=True,
                detail=f"contrato de resposta recusado ({exc.reason}): {exc.detail}",
                diagnostics=(exc.reason,),
                capabilities=descriptor.capabilities,
            )
        return A.ProbeResult(
            integration_available=integration,
            agent_connected=True,
            access_usable=True,
            contract_accepted=result.execution_status in E.CONTENTFUL_STATUSES,
            detail=f"tarefa mínima aceita ({result.execution_status})",
            diagnostics=tuple(str(d.get("message", "")) for d in result.diagnostics),
            capabilities=descriptor.capabilities,
            usage=result.usage,
        )


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------
# `local` — worker determinístico controlado
# --------------------------------------------------------------------------


class LocalAgentAdapter(BaseExecutorAdapter):
    """Agente `local`: callables Python registrados, em `ThreadPoolExecutor`.

    É homologado porque o host é este próprio repositório (`verified_versions`
    aponta o módulo e a versão do Python em execução). Não lê código novo:
    `deepening=False` — quem planeja continuação recusa antes de gastar rodada.
    """

    agent_id = "local"
    adapter_version = "local/1"

    def __init__(
        self,
        task_registry: Mapping[str, Callable[..., Any]] | None = None,
        max_workers: int = 4,
    ) -> None:
        super().__init__()
        self._task_registry = dict(task_registry or {})
        self._max_workers = max_workers

    def describe(self) -> A.AgentDescriptor:
        return A.AgentDescriptor(
            agent_id=self.agent_id,
            adapter_version=self.adapter_version,
            transports=("process",),
            capabilities=A.AgentCapabilities(
                dispatch=True,
                max_concurrency=self._max_workers,
                supports_session=False,
                physical_cancellation=False,
                structured_output=True,
                deepening=False,
                telemetry=True,
                tools=tuple(sorted(self._task_registry)),
            ),
            verified_versions=(
                "runtime.executors.local_thread/1",
                f"cpython-{platform.python_version()}",
            ),
            prerequisites=(
                A.Prerequisite(
                    prerequisite_id="python",
                    description="interpretador Python que roda este pacote",
                    check_argv=("python", "--version"),
                ),
            ),
            setup_steps=(
                A.SetupStep(
                    step_id="verify",
                    description="verificar que o worker local responde à tarefa mínima",
                    argv=("wk", "doctor", "--probe-agent"),
                ),
            ),
            distribution="bundled",
            summary="worker determinístico do próprio wiki-ai (sem LLM, sem rede)",
        )

    def _handshake(self, transport: str, config: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "connected": True,
            "detail": "worker local em processo",
            "host_version": f"cpython-{platform.python_version()}",
            # Worker determinístico: não existe modelo. Indisponível, não inferido.
            "model": None,
        }

    def _new_executor(self, config: Mapping[str, Any]) -> LocalThreadExecutor:
        registry = dict(config.get("registry") or self._task_registry)
        registry.setdefault(_PROBE_KIND, _probe_callable)
        workers = int(config.get("max_workers") or self._max_workers)
        return LocalThreadExecutor(registry=registry, max_workers=workers)


# --------------------------------------------------------------------------
# `claude-code` — transporte por processo sobre a CLI oficial
# --------------------------------------------------------------------------


class ClaudeCodeAgentAdapter(BaseExecutorAdapter):
    """Agente `claude-code` (§10.4.5), transporte `process` sobre `ClaudeCliExecutor`.

    `verified_versions` é VAZIO de propósito: nenhuma versão do host foi
    homologada neste repositório. `connect()` grava a versão REAL informada
    por `claude --version` no binding (`host_version`) — o que foi observado,
    não o que se supõe. `model` fica `None` (a CLI não expõe o modelo no
    handshake), registrado como indisponível.
    """

    agent_id = "claude-code"
    adapter_version = "claude-code-process/1"

    def __init__(self, binary_path: Any = "claude", timeout_s: float = 60.0) -> None:
        super().__init__()
        self._binary_path = binary_path
        self._timeout_s = timeout_s

    def _detect(self, binary_path: Any = _UNSET) -> tuple[bool, str, str | None]:
        """Sonda o binário INFORMADO (ou o do adaptador). Sem efeito colateral.

        `binary_path` é argumento — e não `self._binary_path` trocado
        temporariamente — porque dois `connect()` concorrentes com binários
        diferentes se sobrescreviam: um deles sondava o binário do outro e
        gravava no binding o `host_version` do host errado.
        """
        with self._lock:
            resolved = self._binary_path if binary_path is _UNSET else binary_path
        command = _as_command(resolved)
        if not command:
            return False, "binary_path vazio", None
        if shutil.which(command[0]) is None:
            return False, f"binário não encontrado no PATH: {command[0]!r}", None
        try:
            proc = subprocess.run(
                [*command, "--version"],
                cwd=tempfile.gettempdir(),
                env=_minimal_env(),
                capture_output=True,
                text=True,
                timeout=5.0,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"falha ao sondar binário: {type(exc).__name__}: {exc}", None
        if proc.returncode != 0:
            return False, f"'{command[0]} --version' saiu com {proc.returncode}", None
        return True, "handshake por processo verificado", (proc.stdout or "").strip() or None

    def _detect_once(self, binary_path: Any = _UNSET) -> tuple[bool, str, str | None]:
        """`_detect` memoizado por binário DENTRO da janela de operação.

        `describe()` e `_handshake()` sondam o MESMO binário no mesmo
        `connect`: sem esta memória, um `connect` com `auto` custava três
        `claude --version` (5 s de timeout cada) para produzir a MESMA
        resposta. A chave é o comando resolvido, então dois `connect`
        concorrentes com binários diferentes continuam sondando cada um o seu.
        """
        with self._lock:
            resolved = self._binary_path if binary_path is _UNSET else binary_path
        try:
            key: Any = ("detect", tuple(_as_command(resolved)))
        except TypeError:
            # `binary_path` inválido (p.ex. `None`): a chave não precisa ser
            # bonita, precisa ser estável — e `_detect` devolve o motivo.
            key = ("detect", repr(resolved))
        return self._operation_cache.get(key, lambda: self._detect(binary_path))

    def describe(self) -> A.AgentDescriptor:
        available, detail, _ = self._detect_once()
        return A.AgentDescriptor(
            agent_id=self.agent_id,
            adapter_version=self.adapter_version,
            transports=("process",),
            capabilities=A.AgentCapabilities(
                dispatch=available,
                max_concurrency=None,
                supports_session=False,
                physical_cancellation=True,
                structured_output=True,
                deepening=available,
                telemetry=False,
                reason="" if available else detail,
            ),
            # Sem homologação local: nenhuma versão do host foi verificada aqui.
            verified_versions=(),
            prerequisites=(
                A.Prerequisite(
                    prerequisite_id="npm",
                    description="npm disponível para instalar a CLI",
                    check_argv=("npm", "--version"),
                ),
                A.Prerequisite(
                    prerequisite_id="claude-code-cli",
                    description="CLI do Claude Code no PATH",
                    check_argv=("claude", "--version"),
                ),
            ),
            setup_steps=(
                A.SetupStep(
                    step_id="install",
                    description="instalar a CLI do Claude Code",
                    argv=("npm", "install", "-g", "@anthropic-ai/claude-code"),
                ),
                A.SetupStep(
                    step_id="authenticate",
                    description="autenticar a CLI no host (login interativo do provedor)",
                    manual_action=(
                        "executar `claude` no terminal e concluir o login do provedor; "
                        "a autenticação é do host, não do wiki-ai"
                    ),
                    verify_argv=("claude", "-p", "ping", "--output-format", "json"),
                ),
                A.SetupStep(
                    step_id="verify",
                    description="verificar o despacho pela sonda do fluxo real",
                    argv=("wk", "doctor", "--probe-agent"),
                ),
            ),
            distribution="bundled",
            summary="Claude Code por processo (`claude -p ... --output-format json`)",
        )

    def _handshake(self, transport: str, config: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            binary = config.get("binary_path", self._binary_path)
        available, detail, host_version = self._detect_once(binary)
        return {
            "connected": available,
            "detail": detail,
            "host_version": host_version,
            # A CLI não expõe o modelo no handshake: indisponível (§10.4.2 item 7).
            "model": None,
        }

    def _new_executor(self, config: Mapping[str, Any]) -> ClaudeCliExecutor:
        return ClaudeCliExecutor(
            binary_path=config.get("binary_path", self._binary_path),
            timeout_s=float(config.get("timeout_s", self._timeout_s)),
            workdir_root=config.get("workdir_root"),
        )

    def _probe_objective(self) -> dict[str, Any]:
        return {
            "kind": _PROBE_KIND,
            "objective_id": "probe",
            "instruction": (
                "Responda SOMENTE com o JSON {\"objective_id\": \"probe\"}. "
                "Não leia arquivos nem execute comandos."
            ),
        }

    def _extract_payload(self, payload: Any) -> Any:
        """A CLI devolve `{"type":"result","result":"<texto>"}` — o texto é a resposta.

        Transformar o envelope do host é papel do adaptador; o SIGNIFICADO da
        afirmação não muda: o texto extraído segue para `adapt_agent_text`, que
        recusa virar `done` vazio quando não é parseável.
        """
        if isinstance(payload, Mapping) and isinstance(payload.get("result"), str):
            return payload["result"]
        return payload


# --------------------------------------------------------------------------
# Executor já construído → adaptador (ponte de compatibilidade)
# --------------------------------------------------------------------------


class LegacyExecutorAdapter(BaseExecutorAdapter):
    """Envolve um `AgentExecutor` JÁ CONSTRUÍDO no contrato de adaptador.

    É o que permite `coordinator.run(store, executor=...)` (a chamada da CLI
    atual e dos testes) continuar existindo sem uma segunda rota de despacho:
    o executor recebido vira adaptador aqui, e o laço só conhece adaptadores.
    """

    adapter_version = "legacy-executor/1"

    def __init__(self, executor: Any, *, agent_id: str | None = None) -> None:
        super().__init__()
        self._executor = executor
        from . import resolve_agent_id

        raw_id = agent_id or str(getattr(executor, "prefix", "") or "legacy")
        # Nome histórico de executor -> ID público do agente pelo alias único
        # (`AGENT_ID_ALIASES`); nenhuma tabela paralela de nomes aqui.
        self.agent_id = resolve_agent_id(raw_id)

    def describe(self) -> A.AgentDescriptor:
        caps_fn = getattr(self._executor, "capabilities", None)
        caps = A.AgentCapabilities.from_executor(caps_fn() if callable(caps_fn) else {})
        return A.AgentDescriptor(
            agent_id=self.agent_id,
            adapter_version=self.adapter_version,
            transports=("process",),
            capabilities=caps,
            verified_versions=(),
            setup_steps=(
                A.SetupStep(
                    step_id="verify",
                    description="verificar o despacho pela sonda do fluxo real",
                    argv=("wk", "doctor", "--probe-agent"),
                ),
            ),
            distribution="bundled",
            summary=f"executor {type(self._executor).__name__} adaptado ao contrato comum",
        )

    def _handshake(self, transport: str, config: Mapping[str, Any]) -> dict[str, Any]:
        caps_fn = getattr(self._executor, "capabilities", None)
        caps = dict(caps_fn()) if callable(caps_fn) else {}
        return {
            "connected": bool(caps.get("dispatch", True)),
            "detail": str(caps.get("reason") or "executor já construído"),
            "host_version": None,
            "model": None,
        }

    def _new_executor(self, config: Mapping[str, Any]) -> Any:
        return self._executor

    def close(self, binding: A.AgentBinding) -> None:
        """Não encerra o executor recebido: quem o construiu é dono do ciclo de vida."""
        with self._lock:
            self._executors.pop(binding.binding_id, None)
            self._bindings.pop(binding.binding_id, None)

    def bind(self, transport: str = "process") -> A.AgentBinding:
        """Binding para um executor já pronto (sem reconstruir nada)."""
        return self.connect(transport, {})


__all__ = [
    "BaseExecutorAdapter",
    "ClaudeCodeAgentAdapter",
    "LegacyExecutorAdapter",
    "LocalAgentAdapter",
]
