"""Preferência, binding e leases por store/repo (spec §10.4.6)."""

import json
import os
import shutil
import tempfile
import unittest

from runtime import agents as A
from runtime import bindings as B
from runtime import coordinator as C
from runtime import envelopes as E
from runtime import tasks as T


def _binding(agent_id="local", binding_id="bind-1", connected=True, model=None):
    return A.AgentBinding(
        binding_id=binding_id,
        agent_id=agent_id,
        adapter_version="local/1",
        transport="process",
        connected=connected,
        host_version="cpython-3",
        model=model,
        capabilities=A.AgentCapabilities(max_concurrency=2),
    )


class BindingStoreTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bindstore-")
        self.store = B.BindingStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- preferência x binding ---------------------------------------------

    def test_preferencia_nao_significa_conectado(self):
        self.store.set_preference("claude-code")
        resolved = self.store.resolve()
        self.assertEqual(resolved.agent_id, "claude-code")
        self.assertIsNone(resolved.binding)
        self.assertFalse(resolved.connected)
        self.assertEqual(resolved.origin, B.ORIGIN_STORE)

    def test_binding_desconectado_nao_persiste(self):
        with self.assertRaises(B.BindingError):
            self.store.save_binding(_binding(connected=False))
        self.assertIsNone(self.store.get_binding())

    def test_selecao_muda_apos_handshake_valido(self):
        self.store.set_preference("claude-code")
        self.store.save_binding(_binding(agent_id="local"))
        self.assertEqual(self.store.get_preference()["agent_id"], "local")
        self.assertEqual(self.store.resolve().agent_id, "local")

    # -- resolução ----------------------------------------------------------

    def test_repo_usa_binding_do_repo(self):
        self.store.save_binding(_binding(binding_id="bind-store"))
        self.store.save_binding(_binding(binding_id="bind-repo"), repo="repo-1")
        resolved = self.store.resolve(repo="repo-1")
        self.assertEqual(resolved.origin, B.ORIGIN_REPO)
        self.assertEqual(resolved.binding.binding_id, "bind-repo")

    def test_repo_sem_binding_cai_no_store(self):
        self.store.save_binding(_binding(binding_id="bind-store"))
        resolved = self.store.resolve(repo="repo-sem-config")
        self.assertEqual(resolved.origin, B.ORIGIN_STORE)
        self.assertEqual(resolved.binding.binding_id, "bind-store")

    def test_override_explicito_tem_origem_propria(self):
        self.store.save_binding(_binding(binding_id="bind-store", agent_id="local"))
        resolved = self.store.resolve(override_agent_id="local")
        self.assertEqual(resolved.origin, B.ORIGIN_OVERRIDE)
        self.assertEqual(resolved.binding.binding_id, "bind-store")

    def test_override_sem_binding_nao_inventa_conexao(self):
        resolved = self.store.resolve(override_agent_id="claude-code")
        self.assertEqual(resolved.origin, B.ORIGIN_OVERRIDE)
        self.assertIsNone(resolved.binding)
        self.assertIn("agent connect", resolved.detail)

    def test_sem_configuracao_nao_deduz_agente(self):
        resolved = self.store.resolve()
        self.assertEqual(resolved.origin, B.ORIGIN_NONE)
        self.assertIsNone(resolved.agent_id)
        self.assertIsNone(resolved.binding)

    # -- persistência -------------------------------------------------------

    def test_arquivo_versionado_e_relido(self):
        self.store.save_binding(_binding())
        with open(os.path.join(self.root, B.STORE_FILENAME), encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["schema_version"], B.SCHEMA_VERSION)
        outro = B.BindingStore(self.root)
        self.assertEqual(outro.resolve().binding.binding_id, "bind-1")

    def test_versao_divergente_e_recusada(self):
        with open(os.path.join(self.root, B.STORE_FILENAME), "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 99}, handle)
        with self.assertRaises(B.BindingError):
            B.BindingStore(self.root).resolve()

    def test_escrita_nao_deixa_temporario(self):
        self.store.save_binding(_binding())
        restos = [n for n in os.listdir(self.root) if n.endswith(".tmp")]
        self.assertEqual(restos, [])

    # -- leases -------------------------------------------------------------

    def test_cancel_active_invalida_leases_do_escopo(self):
        self.store.open_lease("lease-store", binding_id="bind-1")
        self.store.open_lease("lease-repo", binding_id="bind-1", repo="repo-1")
        self.assertTrue(self.store.lease_valid("lease-store"))
        self.assertEqual(len(self.store.active_leases()), 1)
        self.assertEqual(self.store.cancel_active(), 1)
        self.assertFalse(self.store.lease_valid("lease-store"))
        self.assertTrue(self.store.lease_valid("lease-repo"))

    def test_lease_desconhecido_nao_e_valido(self):
        self.assertFalse(self.store.lease_valid("nunca-aberto"))

    def test_campo_publico_agent_nao_traz_segredo(self):
        binding = A.AgentBinding(
            binding_id="bind-1",
            agent_id="local",
            adapter_version="local/1",
            transport="process",
            connected=True,
            vendor={"api_key": "SEGREDO", "endpoint": "local"},
        )
        self.store.save_binding(binding)
        publico = self.store.to_public_dict()
        self.assertEqual(publico["connection_status"], "connected")
        self.assertEqual(publico["selection_origin"], B.ORIGIN_STORE)
        self.assertEqual(publico["agent_id"], "local")
        self.assertEqual(publico["transport"], "process")
        self.assertEqual(publico["adapter_version"], "local/1")
        self.assertIsNotNone(publico["capabilities"])
        self.assertFalse(publico["model_available"])
        self.assertNotIn("SEGREDO", json.dumps(publico))


class BindingSwitchTest(unittest.TestCase):
    """Troca de binding: orçamento e revisão intactos; resultado tardio recusado."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bindswitch-")
        self.bindings = B.BindingStore(self.root)
        self.store = T.TaskStore.open(os.path.join(self.root, "runtime.db"))
        self.task = self.store.create_task(
            T.TaskKind.INVESTIGATION,
            {"objective_id": "obj-1"},
            {"snapshot_id": "snap-1"},
            budget={"max_tokens": 777},
        )

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_troca_preserva_orcamento_e_input_revision(self):
        antes = self.store.get(self.task.task_id)
        self.bindings.save_binding(_binding(binding_id="bind-A"))
        self.bindings.save_binding(_binding(binding_id="bind-B", agent_id="claude-code"))
        depois = self.store.get(self.task.task_id)
        self.assertEqual(depois.budget, antes.budget)
        self.assertEqual(depois.input_versions_hash, antes.input_versions_hash)
        self.assertEqual(self.bindings.resolve().binding.binding_id, "bind-B")

    def test_resultado_tardio_apos_cancel_active_e_recusado(self):
        lease = self.store.acquire_lease(self.task.task_id, "owner")
        self.bindings.save_binding(_binding(binding_id="bind-A"))
        self.bindings.open_lease(lease.lease_id, binding_id="bind-A")
        provenance = E.Provenance("local", "local/1", "process", "bind-A")
        self.store.start_attempt(
            self.task.task_id,
            lease.lease_id,
            "local:exec-1",
            binding_id="bind-A",
            provenance=provenance.to_dict(),
        )
        envelope = C.envelope_for_task(
            self.task, execution_id="local:exec-1", attempt_id="1", lease_id=lease.lease_id
        )
        payload = E.build_result(
            envelope,
            execution_id="local:exec-1",
            provenance=provenance,
            claims={"objective_id": "obj-1"},
        )

        self.assertEqual(self.bindings.cancel_active(), 1)
        verdict = C.accept_result(
            self.store,
            self.store.get(self.task.task_id),
            "local:exec-1",
            payload,
            task_envelope=envelope,
            binding_id="bind-A",
            bindings=self.bindings,
        )
        self.assertFalse(verdict.accepted)
        self.assertIs(verdict.reason, C.RejectionReason.LEASE_INVALID)


class LocalAdapterBindingTest(unittest.TestCase):
    """Binding real do agente `local`: handshake, persistência e sonda."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bindlocal-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_connect_persiste_binding_e_sonda_passa(self):
        registry = A.default_registry(extensions=False)
        binding = registry.connect("local", "auto")
        self.addCleanup(registry.close, binding)
        store = B.BindingStore(self.root)
        store.save_binding(binding, repo="repo-1")
        resolved = store.resolve(repo="repo-1")
        self.assertEqual(resolved.origin, B.ORIGIN_REPO)
        self.assertEqual(resolved.binding.agent_id, "local")
        self.assertIsNone(resolved.binding.model)
        probe = registry.probe(binding)
        self.assertTrue(probe.ok, probe.detail)


if __name__ == "__main__":
    unittest.main()


class RunRegistersBindingLeaseTest(unittest.TestCase):
    """`run` abre e fecha o lease do binding quando recebe um `BindingStore`."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="runlease-")
        self.bindings = B.BindingStore(self.root)
        self.store = T.TaskStore.open(os.path.join(self.root, "runtime.db"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_lease_do_binding_e_registrado_e_encerrado(self):
        from runtime.tests.test_agent_contract import FakeAgentAdapter

        adapter = FakeAgentAdapter(payload={"objective_id": "obj-1"})
        binding = adapter.connect("process", {})
        self.addCleanup(adapter.close, binding)
        C.plan_from_objectives(
            self.store,
            [{"objective_id": "obj-1"}],
            snapshot_id="snap-1",
            budget={"max_bytes": 200_000, "max_tokens": 50_000},
        )
        report = C.run(
            self.store, adapter=adapter, binding=binding, bindings=self.bindings
        )
        self.assertEqual(report.accepted, 1, report.rejections)
        self.assertEqual(self.bindings.active_leases(), ())
        with open(os.path.join(self.root, B.STORE_FILENAME), encoding="utf-8") as handle:
            registrados = json.load(handle)["leases"]
        self.assertEqual(len(registrados), 1)
        (registro,) = registrados.values()
        self.assertEqual(registro["binding_id"], binding.binding_id)
        self.assertEqual(registro["state"], B.LEASE_CLOSED)
