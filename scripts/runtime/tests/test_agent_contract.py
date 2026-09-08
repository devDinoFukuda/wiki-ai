"""Suíte de contrato do adaptador de agente (spec §10.4.4/§10.4.5/§10.4.6).

`AgentAdapterContract` é a base reutilizável do T32: qualquer adaptador novo
(distribuído ou de extensão) passa a ser homologável herdando esta classe e
implementando `make_adapter()`. `FakeAgentAdapter` é a implementação de
referência que prova que a suíte é satisfazível sem nenhum agente externo,
sem rede e sem código do usuário.
"""

import os
import tempfile
import unittest
from typing import Any, Mapping

from runtime import agents as A
from runtime import coordinator as C
from runtime import envelopes as E
from runtime import tasks as T
from runtime.executors import get_executor, resolve_agent_id
from runtime.executors.agent_adapters import (
    BaseExecutorAdapter,
    ClaudeCodeAgentAdapter,
    LegacyExecutorAdapter,
    LocalAgentAdapter,
)
from runtime.executors.base import ExecutionState


# --------------------------------------------------------------------------
# Adaptador "fake" de referência (base do T32)
# --------------------------------------------------------------------------


class _FakeExecutor:
    """Executor síncrono em memória com o protocolo `AgentExecutor`."""

    def __init__(self, payload: Any = None, fail: bool = False):
        self._payload = payload if payload is not None else {"objective_id": "probe"}
        self._fail = fail
        self._records: dict[str, dict] = {}
        self._n = 0

    def capabilities(self) -> dict:
        return {
            "dispatch": True,
            "concurrency": 2,
            "cancellation": "process-kill",
            "structured_output": True,
            "deepening": True,
            "tools": [],
        }

    def submit(self, task_id, objective, references=None, schema=None, policy=None) -> str:
        self._n += 1
        execution_id = f"fake:{task_id}:{self._n}"
        self._records[execution_id] = {
            "state": ExecutionState.FAILED if self._fail else ExecutionState.DONE,
            "task_id": task_id,
            "objective": objective,
            "references": references,
            "schema": schema,
            "policy": policy,
        }
        return execution_id

    def status(self, execution_id) -> dict:
        return {"state": self._records[execution_id]["state"], "heartbeat": None}

    def result(self, execution_id) -> dict:
        record = self._records[execution_id]
        if record["state"] == ExecutionState.FAILED:
            return {"execution_id": execution_id, "output": None, "error": "falha simulada"}
        return {"execution_id": execution_id, "output": self._payload}

    def cancel(self, execution_id) -> bool:
        self._records[execution_id]["state"] = ExecutionState.CANCELLED
        return True


class FakeAgentAdapter(BaseExecutorAdapter):
    """Adaptador de referência: passa a suíte de contrato sem agente externo."""

    agent_id = "fake-agent"
    adapter_version = "fake/1"

    def __init__(self, *, payload: Any = None, fail_handshake: bool = False, model=None):
        super().__init__()
        self._payload = payload
        self._fail_handshake = fail_handshake
        self._model = model

    def describe(self) -> A.AgentDescriptor:
        return A.AgentDescriptor(
            agent_id=self.agent_id,
            adapter_version=self.adapter_version,
            transports=("process", "session"),
            capabilities=A.AgentCapabilities(
                dispatch=True,
                max_concurrency=2,
                max_context_tokens=1000,
                supports_session=True,
                physical_cancellation=True,
                deepening=True,
            ),
            verified_versions=("fake-host/1",),
            prerequisites=(
                A.Prerequisite(
                    prerequisite_id="python",
                    description="interpretador do próprio teste",
                    check_argv=("python", "--version"),
                ),
            ),
            setup_steps=(
                A.SetupStep(
                    step_id="verify",
                    description="sonda pelo fluxo real",
                    argv=("wk", "doctor", "--probe-agent"),
                ),
            ),
            distribution="extension",
            summary="adaptador de referência da suíte de contrato",
        )

    def _handshake(self, transport: str, config: Mapping[str, Any]) -> dict:
        if self._fail_handshake:
            return {"connected": False, "detail": "handshake recusado no teste"}
        return {
            "connected": True,
            "detail": "ponte de teste",
            "host_version": "fake-host/1",
            "model": self._model,
        }

    def _new_executor(self, config: Mapping[str, Any]):
        return _FakeExecutor(payload=self._payload)


def make_extension_adapter():
    """Fábrica usada por `AgentRegistry.load_extensions` no teste de extensão."""
    return FakeAgentAdapter()


# --------------------------------------------------------------------------
# Suíte de contrato reutilizável (T32)
# --------------------------------------------------------------------------


class AgentAdapterContract:
    """Herde com `unittest.TestCase` e implemente `make_adapter()`."""

    def make_adapter(self):  # pragma: no cover - implementado pela subclasse
        raise NotImplementedError

    def connect_config(self) -> dict:
        return {}

    def test_describe_declara_contrato_minimo(self):
        descriptor = self.make_adapter().describe()
        self.assertIsInstance(descriptor, A.AgentDescriptor)
        self.assertTrue(descriptor.transports)
        self.assertTrue(set(descriptor.transports) <= A.TRANSPORTS)
        self.assertTrue(descriptor.adapter_version)

    def test_setup_steps_sao_executaveis(self):
        for step in self.make_adapter().describe().setup_steps:
            self.assertTrue(
                step.argv or (step.manual_action and step.verify_argv),
                f"passo {step.step_id} não é executável",
            )

    def test_api_minima_presente(self):
        adapter = self.make_adapter()
        for name in ("describe", "connect", "probe", "submit", "poll", "cancel", "close"):
            self.assertTrue(callable(getattr(adapter, name, None)), name)

    def test_conexao_produz_binding_com_proveniencia(self):
        adapter = self.make_adapter()
        transport = adapter.describe().transports[0]
        binding = adapter.connect(transport, self.connect_config())
        self.addCleanup(adapter.close, binding)
        self.assertTrue(binding.connected)
        provenance = E.Provenance.from_binding(binding)
        self.assertEqual(provenance.agent_id, binding.agent_id)
        self.assertEqual(provenance.binding_id, binding.binding_id)

    def test_despacho_produz_resultado_validavel(self):
        adapter = self.make_adapter()
        transport = adapter.describe().transports[0]
        binding = adapter.connect(transport, self.connect_config())
        self.addCleanup(adapter.close, binding)
        envelope = adapter._probe_envelope()
        execution_id = adapter.submit(binding, envelope)
        observed = _drain(adapter, binding, execution_id)
        self.assertIsNotNone(observed.get("result"), observed)
        validated = E.validate_result(
            observed["result"],
            envelope.with_execution_id(execution_id),
            binding_id=binding.binding_id,
        )
        self.assertIn(validated.execution_status, E.EXECUTION_STATUSES)

    def test_probe_diferencia_os_quatro_eixos(self):
        adapter = self.make_adapter()
        transport = adapter.describe().transports[0]
        binding = adapter.connect(transport, self.connect_config())
        self.addCleanup(adapter.close, binding)
        probe = adapter.probe(binding)
        self.assertTrue(probe.integration_available)
        self.assertTrue(probe.agent_connected)
        self.assertTrue(probe.access_usable)
        self.assertTrue(probe.contract_accepted, probe.detail)
        self.assertTrue(probe.ok)
        self.assertEqual(
            set(probe.to_dict())
            & {"integration_available", "agent_connected", "access_usable", "contract_accepted"},
            {"integration_available", "agent_connected", "access_usable", "contract_accepted"},
        )


def _drain(adapter, binding, execution_id, limit: int = 500) -> dict:
    for _ in range(limit):
        observed = adapter.poll(binding, execution_id)
        if observed.get("result") is not None or observed.get("error"):
            return observed
        if str(observed.get("state")) in {"done", "failed", "cancelled"}:
            return observed
    return {}


class FakeAdapterContractTest(AgentAdapterContract, unittest.TestCase):
    def make_adapter(self):
        return FakeAgentAdapter()


class LocalAdapterContractTest(AgentAdapterContract, unittest.TestCase):
    """O agente `local` distribuído passa exatamente a mesma suíte."""

    def make_adapter(self):
        return LocalAgentAdapter(max_workers=2)


# --------------------------------------------------------------------------
# Registro extensível, sem enumeração fechada (§10.4.2 item 10, §10.4.5)
# --------------------------------------------------------------------------


class RegistryTest(unittest.TestCase):
    def test_default_registry_so_tem_adaptadores_reais(self):
        registry = A.default_registry(extensions=False)
        self.assertEqual(set(registry.agent_ids()), {"local", "claude-code"})
        for missing in ("devin-cli", "cursor", "antigravity", "windsurf", "codex"):
            self.assertNotIn(missing, registry)

    def test_registro_aceita_adaptador_novo_sem_tocar_no_nucleo(self):
        registry = A.default_registry(extensions=False)
        descriptor = registry.register(FakeAgentAdapter())
        self.assertEqual(descriptor.agent_id, "fake-agent")
        self.assertIn("fake-agent", registry)
        self.assertIn("fake-agent", [d.agent_id for d in registry.list()])

    def test_registro_duplicado_e_recusado(self):
        registry = A.AgentRegistry([FakeAgentAdapter()])
        with self.assertRaises(A.AgentContractError):
            registry.register(FakeAgentAdapter())

    def test_objeto_sem_api_minima_nao_e_adaptador(self):
        class Incompleto:
            def describe(self):
                return FakeAgentAdapter().describe()

        with self.assertRaises(A.AgentContractError):
            A.AgentRegistry().register(Incompleto())

    def test_extensao_por_modulo_fabrica(self):
        registry = A.AgentRegistry()
        registry.load_extensions(["runtime.tests.test_agent_contract:make_extension_adapter"])
        self.assertIn("fake-agent", registry)
        self.assertEqual(registry.describe("fake-agent").distribution, "extension")

    def test_extensao_por_variavel_de_ambiente(self):
        previous = os.environ.get(A.EXTENSIONS_ENV)
        os.environ[A.EXTENSIONS_ENV] = (
            "runtime.tests.test_agent_contract:make_extension_adapter"
        )
        try:
            registry = A.default_registry()
            self.assertIn("fake-agent", registry)
        finally:
            if previous is None:
                os.environ.pop(A.EXTENSIONS_ENV, None)
            else:
                os.environ[A.EXTENSIONS_ENV] = previous

    def test_extensao_invalida_falha_explicitamente(self):
        with self.assertRaises(A.AgentContractError):
            A.AgentRegistry().load_extensions(["sem_dois_pontos"])

    def test_agente_desconhecido_nao_cai_em_outro(self):
        registry = A.default_registry(extensions=False)
        with self.assertRaises(A.AgentUnavailableError) as ctx:
            registry.adapter("devin-cli")
        self.assertIn("devin-cli", str(ctx.exception))

    def test_transporte_nao_oferecido_e_recusado(self):
        registry = A.AgentRegistry([LocalAgentAdapter()])
        with self.assertRaises(A.AgentUnavailableError):
            registry.connect("local", "session")

    def test_conexao_so_apos_handshake(self):
        registry = A.AgentRegistry([FakeAgentAdapter(fail_handshake=True)])
        with self.assertRaises(A.HandshakeFailed):
            registry.connect("fake-agent", "process")

    def test_descritor_sem_versao_homologada_nao_e_homologado(self):
        descriptor = ClaudeCodeAgentAdapter(binary_path="claude").describe()
        self.assertEqual(descriptor.verified_versions, ())
        self.assertFalse(descriptor.homologated)
        self.assertEqual(descriptor.to_dict()["homologated"], False)
        self.assertIn("process", descriptor.transports)
        self.assertNotIn("session", descriptor.transports)

    def test_passo_de_preparacao_narrativo_e_recusado(self):
        with self.assertRaises(A.AgentContractError):
            A.SetupStep(step_id="x", description="configure o agente")

    def test_prerequisito_sem_verificacao_e_recusado(self):
        with self.assertRaises(A.AgentContractError):
            A.Prerequisite(prerequisite_id="x", description="instale algo", check_argv=())

    def test_capacidades_negociadas_sao_o_minimo(self):
        caps = A.AgentCapabilities(max_context_tokens=1000, max_concurrency=2)
        effective = caps.negotiate({"max_tokens": 5000, "max_concurrency": 8, "timeout_s": 30})
        self.assertEqual(effective["max_tokens"], 1000)
        self.assertEqual(effective["max_concurrency"], 2)
        self.assertEqual(effective["timeout_s"], 30)


class ExecutorFacadeTest(unittest.TestCase):
    """`get_executor` é fachada do registro — não um segundo registro."""

    def test_local_vem_do_adaptador(self):
        executor = get_executor("local", registry={"x": lambda **kw: {}}, max_workers=2)
        self.addCleanup(executor.shutdown, True)
        self.assertEqual(executor.capabilities()["concurrency"], 2)

    def test_alias_historico_aponta_para_id_publico(self):
        self.assertEqual(resolve_agent_id("claude-cli"), "claude-code")

    def test_nome_desconhecido_falha_sem_fallback(self):
        with self.assertRaises(ValueError):
            get_executor("cursor")


# --------------------------------------------------------------------------
# Aceitação de resultado: recusas do §10.4.4
# --------------------------------------------------------------------------


class AcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="accept-")
        self.store = T.TaskStore.open(os.path.join(self.tmp, "runtime.db"))
        self.task = self.store.create_task(
            T.TaskKind.INVESTIGATION,
            {"objective_id": "obj-1"},
            {"snapshot_id": "snap-1"},
            budget={"max_tokens": 4242},
        )
        self.lease = self.store.acquire_lease(self.task.task_id, "owner")
        self.binding = A.AgentBinding(
            binding_id="bind-A",
            agent_id="fake-agent",
            adapter_version="fake/1",
            transport="process",
            connected=True,
        )
        self.provenance = E.Provenance.from_binding(self.binding)
        self.execution_id = "fake:exec-1"
        self.store.start_attempt(
            self.task.task_id,
            self.lease.lease_id,
            self.execution_id,
            binding_id=self.binding.binding_id,
            provenance=self.provenance.to_dict(),
        )
        self.envelope = C.envelope_for_task(
            self.task,
            execution_id=self.execution_id,
            attempt_id="1",
            lease_id=self.lease.lease_id,
            context_hash="ctx-1",
        )

    def tearDown(self):
        self.store.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _result(self, **overrides):
        data = E.build_result(
            self.envelope,
            execution_id=self.execution_id,
            provenance=self.provenance,
            claims={"objective_id": "obj-1", "facts": []},
        )
        data.update(overrides)
        return data

    def _accept(self, envelope, **kwargs):
        kwargs.setdefault("task_envelope", self.envelope)
        kwargs.setdefault("binding_id", self.binding.binding_id)
        return C.accept_result(
            self.store, self.task, self.execution_id, envelope, **kwargs
        )

    def test_resultado_valido_e_aceito(self):
        verdict = self._accept(self._result())
        self.assertTrue(verdict.accepted, verdict.detail)
        self.assertEqual(verdict.output["objective_id"], "obj-1")
        self.assertFalse(verdict.duplicate)

    def test_lease_invalidado_recusa(self):
        verdict = self._accept(self._result(), lease_valid=False)
        self.assertFalse(verdict.accepted)
        self.assertIs(verdict.reason, C.RejectionReason.LEASE_INVALID)

    def test_tentativa_cancelada_recusa(self):
        self.store.record_result(
            self.task.task_id,
            self.lease.lease_id,
            self.execution_id,
            state=T.TaskState.PENDING,
            outcome=T.AttemptOutcome.CANCELLED,
            termination_reason="cancelled",
        )
        verdict = self._accept(self._result())
        self.assertFalse(verdict.accepted)
        self.assertIs(verdict.reason, C.RejectionReason.ATTEMPT_CANCELLED)

    def test_protocolo_divergente_recusa(self):
        verdict = self._accept(self._result(protocol_version="outro/9"))
        self.assertFalse(verdict.accepted)
        self.assertIs(verdict.reason, C.RejectionReason.PROTOCOL_DIVERGENCE)

    def test_contexto_divergente_recusa(self):
        verdict = self._accept(self._result(context_hash="ctx-outro"))
        self.assertFalse(verdict.accepted)
        self.assertIs(verdict.reason, C.RejectionReason.CONTEXT_INVALID)

    def test_binding_anterior_recusa_resultado_tardio(self):
        antigo = E.Provenance("fake-agent", "fake/1", "process", "bind-ANTIGO")
        tardio = E.build_result(
            self.envelope,
            execution_id=self.execution_id,
            provenance=antigo,
            claims={"objective_id": "obj-1"},
        )
        verdict = self._accept(tardio, binding_id="bind-B")
        self.assertFalse(verdict.accepted)
        self.assertIs(verdict.reason, C.RejectionReason.BINDING_DIVERGENCE)

    def test_revisao_de_input_divergente_recusa(self):
        verdict = self._accept(
            {"execution_id": self.execution_id, "output": {"objective_id": "obj-1"},
             "input_versions": {"snapshot_id": "outro"}},
        )
        self.assertFalse(verdict.accepted)
        self.assertIs(verdict.reason, C.RejectionReason.INPUT_DIVERGENCE)

    def test_execucao_desconhecida_recusa(self):
        verdict = C.accept_result(
            self.store, self.task, "fake:nao-emitido", self._result()
        )
        self.assertFalse(verdict.accepted)
        self.assertIs(verdict.reason, C.RejectionReason.UNKNOWN_EXECUTION)

    def test_done_vazio_e_recusado_como_nao_parseavel(self):
        verdict = self._accept(self._result(claims={}, execution_status=E.DONE))
        self.assertFalse(verdict.accepted)
        self.assertIs(verdict.reason, C.RejectionReason.UNPARSEABLE)

    def test_campo_nao_declarado_em_schema_fechado_recusa(self):
        data = self._result()
        data["campo_novo"] = 1
        verdict = self._accept(data)
        self.assertFalse(verdict.accepted)
        self.assertIs(verdict.reason, C.RejectionReason.SCHEMA_INVALID)

    def test_duplicata_e_idempotente(self):
        envelope = self._result()
        first = self._accept(envelope)
        self.assertTrue(first.accepted)
        self.store.record_result(
            self.task.task_id,
            self.lease.lease_id,
            self.execution_id,
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result=first.output,
            termination_reason="completed",
            result_hash=first.result_hash,
        )
        task = self.store.get(self.task.task_id)
        second = C.accept_result(
            self.store,
            task,
            self.execution_id,
            envelope,
            task_envelope=self.envelope,
            binding_id=self.binding.binding_id,
        )
        self.assertTrue(second.accepted)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.output, first.output)
        self.assertEqual(len(self.store.attempts(self.task.task_id)), 1)

    def test_proveniencia_por_tentativa_com_modelo_indisponivel(self):
        gravada = self.store.provenance_for_execution(self.execution_id)
        self.assertEqual(gravada["agent_id"], "fake-agent")
        self.assertEqual(gravada["binding_id"], "bind-A")
        self.assertIsNone(gravada["model"])
        self.assertFalse(gravada["model_available"])
        historico = self.store.execution_provenance(self.task.task_id)
        self.assertEqual(historico[0]["binding_id"], "bind-A")

    def test_troca_de_binding_preserva_orcamento_e_revisao(self):
        antes = self.store.get(self.task.task_id)
        novo_binding = A.AgentBinding(
            binding_id="bind-B",
            agent_id="local",
            adapter_version="local/1",
            transport="process",
            connected=True,
        )
        envelope_b = C.envelope_for_task(
            antes, execution_id="local:exec-9", attempt_id="2", lease_id=self.lease.lease_id
        )
        self.assertEqual(envelope_b.budget, {"max_tokens": 4242})
        self.assertEqual(envelope_b.input_revision, self.envelope.input_revision)
        depois = self.store.get(self.task.task_id)
        self.assertEqual(depois.budget, antes.budget)
        self.assertEqual(depois.input_versions_hash, antes.input_versions_hash)
        self.assertEqual(
            E.Provenance.from_binding(novo_binding).binding_id, "bind-B"
        )


class TextAdaptationTest(unittest.TestCase):
    """Texto do host: adaptado ou diagnosticado — nunca `done` vazio."""

    def setUp(self):
        self.envelope = E.TaskEnvelope(
            task_id="t1",
            objective_id="obj",
            input_revision="rev",
            context_hash="",
            objective={},
            execution_id="e1",
        )
        self.provenance = E.Provenance("fake-agent", "fake/1", "process", "bind-A")

    def test_json_valido_vira_claims(self):
        data = E.adapt_agent_text(
            '{"objective_id": "obj"}', self.envelope, self.provenance, execution_id="e1"
        )
        self.assertEqual(data["execution_status"], E.DONE)
        self.assertEqual(data["claims"]["objective_id"], "obj")

    def test_texto_nao_parseavel_vira_diagnostico(self):
        data = E.adapt_agent_text(
            "desculpe, não consegui", self.envelope, self.provenance, execution_id="e1"
        )
        self.assertEqual(data["execution_status"], E.DIAGNOSTIC)
        self.assertEqual(data["claims"], {})
        self.assertTrue(data["diagnostics"])
        with self.assertRaises(E.EnvelopeError) as ctx:
            E.validate_result(
                {**data, "execution_status": E.DONE}, self.envelope, binding_id="bind-A"
            )
        self.assertEqual(ctx.exception.reason, E.R_UNPARSEABLE)

    def test_modelo_ausente_e_indisponibilidade_registrada(self):
        provenance = E.Provenance("claude-code", "claude-code-process/1", "process", "bind-C")
        self.assertIsNone(provenance.model)
        self.assertFalse(provenance.to_dict()["model_available"])


class LegacyAdapterTest(unittest.TestCase):
    """A ponte de compatibilidade usa a MESMA rota: envelope + validação."""

    def test_executor_pronto_vira_adaptador(self):
        adapter = LegacyExecutorAdapter(_FakeExecutor(payload={"objective_id": "obj"}))
        binding = adapter.bind()
        self.addCleanup(adapter.close, binding)
        self.assertTrue(binding.connected)
        envelope = E.TaskEnvelope(
            task_id="t1", objective_id="obj", input_revision="rev", context_hash="",
            objective={"kind": "x"},
        )
        execution_id = adapter.submit(binding, envelope)
        observed = _drain(adapter, binding, execution_id)
        validated = E.validate_result(
            observed["result"],
            envelope.with_execution_id(execution_id),
            binding_id=binding.binding_id,
        )
        self.assertEqual(validated.claims["objective_id"], "obj")
        self.assertEqual(validated.agent_provenance.binding_id, binding.binding_id)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# Despacho real: uma rota só (envelope → adaptador → validate_result)
# --------------------------------------------------------------------------


class DispatchRouteTest(unittest.TestCase):
    """`coordinator.run` só fala com adaptador+binding — nunca com executor cru."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-")
        self.store = T.TaskStore.open(os.path.join(self.tmp, "runtime.db"))

    def tearDown(self):
        self.store.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plan(self):
        return C.plan_from_objectives(
            self.store,
            [{"objective_id": "obj-1", "kind": "__wiki_ai_probe__"}],
            snapshot_id="snap-1",
            budget={"max_bytes": 200_000, "max_tokens": 50_000},
        )

    def test_run_por_binding_grava_proveniencia_da_tentativa(self):
        adapter = FakeAgentAdapter(payload={"objective_id": "obj-1", "facts": []})
        binding = adapter.connect("process", {})
        self.addCleanup(adapter.close, binding)
        tasks = self._plan()
        report = C.run(self.store, adapter=adapter, binding=binding)
        self.assertEqual(report.accepted, 1, report.rejections)
        task = self.store.get(tasks[0].task_id)
        self.assertIs(task.state, T.TaskState.DONE)
        historico = self.store.execution_provenance(task.task_id)
        self.assertEqual(historico[0]["binding_id"], binding.binding_id)
        self.assertEqual(historico[0]["agent_provenance"]["agent_id"], "fake-agent")
        self.assertIsNone(historico[0]["agent_provenance"]["model"])
        self.assertFalse(historico[0]["agent_provenance"]["model_available"])
        self.assertTrue(historico[0]["result_hash"])

    def test_run_sem_binding_conectado_e_bloqueio_explicito(self):
        adapter = FakeAgentAdapter()
        desconectado = A.AgentBinding(
            binding_id="bind-x",
            agent_id="fake-agent",
            adapter_version="fake/1",
            transport="process",
            connected=False,
        )
        with self.assertRaises(C.DispatchUnavailable):
            C.run(self.store, adapter=adapter, binding=desconectado)

    def test_run_sem_executor_e_sem_binding_nao_escolhe_agente(self):
        with self.assertRaises(C.DispatchUnavailable):
            C.run(self.store)

    def test_run_por_executor_legado_usa_a_mesma_rota(self):
        tasks = self._plan()
        executor = _FakeExecutor(payload={"objective_id": "obj-1"})
        report = C.run(self.store, executor=executor)
        self.assertEqual(report.accepted, 1, report.rejections)
        historico = self.store.execution_provenance(tasks[0].task_id)
        self.assertEqual(historico[0]["agent_provenance"]["adapter_version"], "legacy-executor/1")
        self.assertTrue(historico[0]["binding_id"].startswith("bind-legacy"))

    def test_envelope_submetido_tem_os_campos_do_contrato(self):
        adapter = FakeAgentAdapter(payload={"objective_id": "obj-1"})
        binding = adapter.connect("process", {})
        self.addCleanup(adapter.close, binding)
        self._plan()
        C.run(self.store, adapter=adapter, binding=binding)
        executor = adapter.executor_for(binding)
        (record,) = list(executor._records.values())
        self.assertIn("objective_id", record["objective"])
        self.assertEqual(record["schema"]["version"], "worker_result/1")


class SchemaCompatibilityTest(unittest.TestCase):
    """Banco criado antes da proveniência ganha as colunas aditivas ao abrir."""

    def test_colunas_aditivas_sao_criadas_em_banco_legado(self):
        import sqlite3

        tmp = tempfile.mkdtemp(prefix="legacydb-")
        path = os.path.join(tmp, "runtime.db")
        legacy = sqlite3.connect(path)
        legacy.executescript(
            """
            CREATE TABLE attempts (
                task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, execution_id TEXT,
                started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT NOT NULL DEFAULT 'running',
                error_class TEXT NOT NULL DEFAULT '', error_detail TEXT NOT NULL DEFAULT '',
                error_hash TEXT NOT NULL DEFAULT '', PRIMARY KEY (task_id, attempt_no)
            );
            """
        )
        legacy.commit()
        legacy.close()
        store = T.TaskStore.open(path)
        self.addCleanup(store.close)
        colunas = {
            row[1] for row in store.conn.execute("PRAGMA table_info(attempts)").fetchall()
        }
        self.assertTrue({"binding_id", "lease_id", "provenance_json", "result_hash"} <= colunas)
        self.assertEqual(T.SCHEMA_VERSION, 1)
