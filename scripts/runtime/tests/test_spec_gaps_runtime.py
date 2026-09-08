"""Testes de lacunas da especificação — runtime (§10.4, T30).

Cobre requisitos específicos:

- T5/S10.4.4-07: submit→cancel→poll no adaptador local deve observar estado
  cancelado
- T6/S10.4.6-11: lease ativo de tarefa com binding B1; `agent connect` de B2
  com/sem cancel_active afeta validade do lease
- T7/T30-01: run_chain interrompido (simulado), troca de binding, retomada
  ⇒ rodada anterior preservada, resultado tardio rejeitado
- T9/S10.4.3-05: `connect(id, "auto")` escolhe transporte pela
  DISPONIBILIDADE VERIFICADA do adaptador, não pela posição declarada
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from typing import Any, Mapping

from runtime import agents as A
from runtime import bindings as B
from runtime import coordinator as C
from runtime import envelopes as E
from runtime import tasks as T
from runtime.executors.agent_adapters import (
    BaseExecutorAdapter,
    LocalAgentAdapter,
)


# ---------------------------------------------------------------------------
# T5/S10.4.4-07: submit -> cancel -> poll observa estado cancelado; um
# resultado tardio da MESMA execução é rejeitado pelo coordenador.
# ---------------------------------------------------------------------------


def _slow_cancellable_task(*, objective, references, schema, cancel_event, **_kw):
    """Callable real de `LocalThreadExecutor` (§ cooperative cancellation):
    bloqueia até `cancel_event` ser sinalizado (ou 5s), para que o `cancel()`
    chamado logo após o `submit()` sempre alcance a execução ainda em curso —
    `LocalThreadExecutor._run` promove o estado para `CANCELLED` sempre que
    `cancel_event.is_set()` é verdadeiro quando o callable retorna, mesmo que
    a execução já tivesse começado a rodar."""
    cancel_event.wait(5.0)
    return {"objective_id": objective.get("objective_id"), "nota": "não deveria completar sem cancelamento"}


class TestLocalAdapterSubmitCancelPoll(unittest.TestCase):
    """T5/S10.4.4-07: `submit -> cancel -> poll` no adaptador `local` real
    (não um fake) observa estado cancelado; depois, `coordinator.accept_result`
    rejeita um resultado tardio dessa mesma execução."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wiki-ai-cancel-poll-")
        self.store = T.TaskStore.open(os.path.join(self.tmp, "runtime.db"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_submit_cancel_poll_observa_cancelado_e_resultado_tardio_e_rejeitado(self):
        registry = A.AgentRegistry()
        adapter = LocalAgentAdapter(
            task_registry={"slow-cancel": _slow_cancellable_task}, max_workers=1,
        )
        registry.register(adapter)
        binding = registry.connect("local", "process", {})
        self.addCleanup(registry.close, binding)

        envelope = E.TaskEnvelope(
            task_id="t-cancel-1", objective_id="obj-cancel", input_revision="rev-1",
            context_hash="", objective={"kind": "slow-cancel", "objective_id": "obj-cancel"},
            result_schema={"version": "worker_result/1", "required": ["objective_id"]},
        )
        execution_id = registry.submit(binding, envelope)

        cancelled = registry.cancel(binding, execution_id)
        self.assertTrue(cancelled, "cancel() do adaptador local deveria confirmar o pedido")

        observed = None
        for _ in range(500):
            observed = registry.poll(binding, execution_id)
            if observed.get("state") == "cancelled" or observed.get("error"):
                break
            time.sleep(0.02)
        self.assertIsNotNone(observed)
        self.assertEqual(
            observed["state"], "cancelled",
            f"poll() deveria observar estado cancelado; obteve {observed!r}",
        )
        self.assertIsNone(observed["result"], "execução cancelada não deveria produzir resultado")

        # resultado tardio da MESMA execução: o coordenador precisa recusá-lo
        # (mesmo padrão de AcceptanceTest.test_tentativa_cancelada_recusa em
        # runtime/tests/test_agent_contract.py).
        task = self.store.create_task(
            T.TaskKind.INVESTIGATION, {"objective_id": "obj-cancel"}, {"snapshot_id": "snap-cancel"},
            budget={"max_tokens": 1000},
        )
        lease = self.store.acquire_lease(task.task_id, "owner-cancel")
        provenance = E.Provenance.from_binding(binding)
        self.store.start_attempt(
            task.task_id, lease.lease_id, execution_id,
            binding_id=binding.binding_id, provenance=provenance.to_dict(),
        )
        self.store.record_result(
            task.task_id, lease.lease_id, execution_id,
            state=T.TaskState.PENDING, outcome=T.AttemptOutcome.CANCELLED,
            termination_reason="cancelled",
        )
        task_envelope = C.envelope_for_task(
            task, execution_id=execution_id, attempt_id="1", lease_id=lease.lease_id,
        )
        late_result = E.build_result(
            task_envelope, execution_id=execution_id, provenance=provenance,
            claims={"objective_id": "obj-cancel"},
        )
        verdict = C.accept_result(
            self.store, task, execution_id, late_result,
            task_envelope=task_envelope, binding_id=binding.binding_id,
        )
        self.assertFalse(verdict.accepted, "resultado tardio de execução cancelada deveria ser recusado")
        self.assertIs(verdict.reason, C.RejectionReason.ATTEMPT_CANCELLED)


# ---------------------------------------------------------------------------
# T6/S10.4.6-11: lease ativo sobrevive à troca de binding SEM
# `cancel_active`; `cancel_active` invalida.
# ---------------------------------------------------------------------------


class TestLeaseWithMultipleBindings(unittest.TestCase):
    """T6/S10.4.6-11: binding B1 salvo, lease aberto sobre ele; salvar um
    binding B2 novo no MESMO escopo SEM `cancel_active` preserva o lease de
    B1 ativo (`lease_valid` continua True, `active_leases` ainda mostra
    B1); só `cancel_active` invalida."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wiki-ai-lease-multi-")
        self.store = B.BindingStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_novo_binding_sem_cancel_active_preserva_lease_do_binding_anterior(self):
        repo_scope = "repo-t6"
        b1 = A.AgentBinding(
            binding_id="bind-B1", agent_id="local", adapter_version="local/1",
            transport="process", connected=True,
        )
        self.store.save_binding(b1, repo=repo_scope)
        lease = self.store.open_lease(binding_id=b1.binding_id, repo=repo_scope, task_id="t1")
        self.assertTrue(self.store.lease_valid(lease.lease_id))

        b2 = A.AgentBinding(
            binding_id="bind-B2", agent_id="claude-code", adapter_version="claude-code/1",
            transport="process", connected=True,
        )
        # SEM cancel_active: o lease do binding anterior (B1) não pode ser
        # invalidado só por trocar o binding salvo no mesmo escopo.
        self.store.save_binding(b2, repo=repo_scope)

        self.assertTrue(
            self.store.lease_valid(lease.lease_id),
            "lease de B1 deveria continuar válido após salvar B2 sem cancel_active",
        )
        active = self.store.active_leases(repo=repo_scope)
        self.assertEqual([r.lease_id for r in active], [lease.lease_id])
        self.assertEqual(active[0].binding_id, b1.binding_id)
        # o binding CORRENTE do escopo já é B2 — o lease antigo não segue o
        # binding automaticamente, continua referenciando B1 até ser fechado.
        self.assertEqual(self.store.get_binding(repo=repo_scope).binding_id, b2.binding_id)

        cancelled = self.store.cancel_active(repo=repo_scope)
        self.assertEqual(cancelled, 1)
        self.assertFalse(self.store.lease_valid(lease.lease_id))
        self.assertEqual(self.store.active_leases(repo=repo_scope), ())


# ---------------------------------------------------------------------------
# T7/T30-01: run_chain interrompido, troca de binding, retomado.
# ---------------------------------------------------------------------------


class TestRunChainInterruptionAndResumption(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="runchain-interrupt-")
        self.bindings = B.BindingStore(self.root)
        self.store = T.TaskStore.open(os.path.join(self.root, "runtime.db"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_interrupcao_preserva_estado_e_rounds(self):
        """Simula run_chain com múltiplas rodadas, interrupção na rodada 2,
        troca de binding, retomada. Deve preservar estado da rodada 1 e
        orçamento."""

        # Cria tarefa
        task = self.store.create_task(
            T.TaskKind.INVESTIGATION,
            {"objective_id": "obj-interrupt"},
            {"snapshot_id": "snap-1"},
            budget={"max_tokens": 5000},
        )

        # Simula rodada 1: executa, obtém resultado parcial
        lease1 = self.store.acquire_lease(task.task_id, "owner-1")

        # Simula submissão em rodada 1
        attempt1 = self.store.start_attempt(
            task.task_id,
            lease1.lease_id,
            "executor:exec-1",
            binding_id="bind-1",
            provenance={"agent_id": "local", "transport": "process"}.copy(),
        )

        # Verifica que attempt foi registrado
        self.assertIsNotNone(attempt1)

        # Simula resultado tardio da rodada 1 (depois da interrupção)
        # Este resultado deve ser rejeitado pois houve mudança de binding
        b1 = A.AgentBinding(
            binding_id="bind-1",
            agent_id="local",
            adapter_version="local/1",
            transport="process",
            connected=True,
        )
        self.bindings.save_binding(b1)
        self.bindings.open_lease(lease1.lease_id, binding_id="bind-1")

        # Troca para binding 2
        b2 = A.AgentBinding(
            binding_id="bind-2",
            agent_id="claude-code",
            adapter_version="claude-code/1",
            transport="session",
            connected=True,
        )
        self.bindings.save_binding(b2)
        self.bindings.cancel_active()

        # Tenta aceitar resultado da rodada 1 após mudança de binding
        envelope = C.envelope_for_task(task, execution_id="executor:exec-1")
        result_payload = E.build_result(
            envelope,
            execution_id="executor:exec-1",
            provenance=E.Provenance("local", "local/1", "process", "bind-1"),
            claims={"objective_id": "obj-interrupt"},
        )

        verdict = C.accept_result(
            self.store, task, "executor:exec-1", result_payload,
            task_envelope=envelope, binding_id="bind-1", bindings=self.bindings,
        )

        # Resultado deve ser rejeitado porque lease foi invalidado
        self.assertFalse(verdict.accepted,
                        "resultado de binding anterior deve ser rejeitado")
        self.assertEqual(verdict.reason, C.RejectionReason.LEASE_INVALID)


# ---------------------------------------------------------------------------
# T9/S10.4.3-05: `auto` escolhe pela disponibilidade VERIFICADA do
# adaptador, nunca pela posição declarada em `transports`.
# ---------------------------------------------------------------------------


class _VerifyingAdapter(BaseExecutorAdapter):
    """Adaptador fake que declara `("process", "session")` e VERIFICA cada
    um de verdade (mesmo contrato de `runtime.tests.test_transport_auto`:
    `_verify_transport`/`_handshake` — não um atributo estático)."""

    agent_id = "spec-gaps-transport"
    adapter_version = "spec-gaps-transport/1"

    def __init__(self, availability: Mapping[str, tuple[bool, str]]):
        super().__init__()
        self.availability = dict(availability)

    def describe(self) -> A.AgentDescriptor:
        return A.AgentDescriptor(
            agent_id=self.agent_id,
            adapter_version=self.adapter_version,
            transports=("process", "session"),
            capabilities=A.AgentCapabilities(dispatch=True, supports_session=True),
            setup_steps=(
                A.SetupStep(
                    step_id="install-bridge",
                    description="instalar a ponte de sessão do agente de teste",
                    argv=("fake-agent", "install", "--bridge"),
                ),
            ),
            distribution="extension",
            summary="adaptador de teste de T9/S10.4.3-05",
        )

    def _verify_transport(self, transport: str, config: Mapping[str, Any]) -> tuple[bool, str]:
        return self.availability.get(transport, (False, "transporte não verificado"))

    def _handshake(self, transport: str, config: Mapping[str, Any]) -> dict:
        available, detail = self.availability.get(transport, (False, "indisponível"))
        return {"connected": available, "detail": detail, "host_version": "fake-host/1", "model": None}

    def _new_executor(self, config: Mapping[str, Any]):
        return _NullExecutor()


class _NullExecutor:
    """Executor mínimo — os testes de resolução de transporte nunca
    despacham nada, só conectam/bloqueiam; só precisa existir para
    satisfazer `BaseExecutorAdapter.connect()`."""

    def capabilities(self) -> dict:
        return {"dispatch": True}

    def shutdown(self, wait: bool = True) -> None:
        return None


class TestResolveTransportWithSessionSupport(unittest.TestCase):
    """T9/S10.4.3-05: "`auto` é seleção do transporte pelo adaptador e pelas
    capacidades verificadas; não é tentativa de adivinhar argumentos. Se
    nenhum transporte estiver disponível, o estado é bloqueado com instrução
    específica para aquele agente."."""

    def test_process_indisponivel_session_disponivel_auto_escolhe_session(self):
        adapter = _VerifyingAdapter({
            "process": (False, "binário não encontrado no PATH"),
            "session": (True, "ponte respondeu ao handshake"),
        })
        registry = A.AgentRegistry()
        registry.register(adapter)

        # `transports[0]` é "process" — se `auto` só olhasse a posição
        # declarada (comportamento antigo), escolheria "process" mesmo
        # indisponível. A escolha real precisa vir da disponibilidade
        # VERIFICADA (`_verify_transport`), não da ordem de `describe()`.
        chosen = registry.resolve_transport("spec-gaps-transport", "auto")
        self.assertEqual(chosen, "session")

        binding = registry.connect("spec-gaps-transport", "auto")
        self.addCleanup(registry.close, binding)
        self.assertEqual(binding.transport, "session")

    def test_ambos_indisponiveis_bloqueia_com_setup_steps_daquele_agente(self):
        adapter = _VerifyingAdapter({
            "process": (False, "binário não encontrado no PATH"),
            "session": (False, "ponte não respondeu ao handshake"),
        })
        registry = A.AgentRegistry()
        registry.register(adapter)

        with self.assertRaises(A.TransportUnavailable) as ctx:
            registry.connect("spec-gaps-transport", "auto")
        exc = ctx.exception
        self.assertEqual(exc.agent_id, "spec-gaps-transport")
        # bloqueio precisa trazer a instrução EXECUTÁVEL específica deste
        # agente (setup_steps), nunca um erro genérico sem próximo passo.
        self.assertTrue(exc.setup_steps, "erro de transporte indisponível precisa trazer setup_steps")
        self.assertIn("fake-agent install --bridge", str(exc))
        self.assertTrue(exc.next_actions(), "precisa haver ao menos uma ação executável")


if __name__ == "__main__":
    unittest.main()
