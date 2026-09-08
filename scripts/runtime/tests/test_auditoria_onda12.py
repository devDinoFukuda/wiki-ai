"""Regressões da onda 12 — A1, M1, M3 e a corrida de `schema_version`.

Cada teste falha no código anterior à onda e passa depois. Não há mock de
comportamento: o defeito é reproduzido com o objeto real (store, cadeia,
`agents.json` em disco) e a asserção é sobre o efeito observável.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest

from runtime import bindings as B
from runtime import coordinator as C
from runtime import state as S
from runtime import tasks as T
from runtime.executors.local_thread import LocalThreadExecutor


def _need(target, motivo="obrigacao"):
    return {
        "kind": "code",
        "target": target,
        "motivo": motivo,
        "need_id": f"need:{target}",
    }


class _Engine(LocalThreadExecutor):
    """Worker local que declara `deepening=True`, para o laço poder continuar."""

    def capabilities(self):
        caps = dict(super().capabilities())
        caps["deepening"] = True
        return caps


def _task_callable(objective=None, references=None, schema=None, cancel_event=None):
    objective = dict(objective or {})
    return {"objective_id": objective.get("objective_id", "obj-1"), "state": "partial"}


# --------------------------------------------------------------------------
# A1 — o corpo da rodada de `run_chain` é blindado
# --------------------------------------------------------------------------


class RunChainBlindagemTest(unittest.TestCase):
    """Erro dentro da rodada vira PARADA canônica, não exceção para fora."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wk-onda12-chain-")
        self.store = T.TaskStore.open(os.path.join(self.root, "runtime.db"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self.store.close)
        self.inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        self.executor = _Engine(registry={"investigation": _task_callable}, max_workers=2)
        self.addCleanup(self.executor.shutdown)
        self.store.create_task(
            T.TaskKind.INVESTIGATION,
            {"objective_id": "obj-1", "kind": "investigation"},
            self.inputs,
        )
        self.store.refresh_states()

    def _ultima_tarefa(self):
        tarefas = sorted(self.store.all_tasks(), key=lambda t: (t.created_at, t.task_id))
        return tarefas[-1].task_id

    def _run(self, outcomes_fn, **kwargs):
        return C.run_chain(
            self.store,
            objective_ids=["obj-1"],
            input_versions=self.inputs,
            outcomes_fn=outcomes_fn,
            repo_id="repo-1",
            executor=self.executor,
            engine_capabilities={"deepening": True},
            **kwargs,
        )

    def _outcomes_com_lixo_na_rodada_2(self, lixo):
        """Rodada 0 progride de verdade; rodada 1 devolve item fora de contrato."""

        def outcomes_fn(round_no):
            if round_no == 0:
                return [{
                    "objective_id": "obj-1",
                    "task_id": self._ultima_tarefa(),
                    "state": "partial",
                    "leituras": [],
                    "unmet_needs": [_need("a.py")],
                    "contract_state": {"regra": {"content": "apuracao 0"}},
                }]
            return [lixo]

        return outcomes_fn

    def test_outcome_nao_mapping_para_a_cadeia_sem_perder_a_rodada_1(self):
        relatorio = self._run(self._outcomes_com_lixo_na_rodada_2("nao-sou-mapping"))

        # a) não explodiu: veio ChainReport.
        self.assertIsInstance(relatorio, C.ChainReport)
        # b) motivo canônico do vocabulário fechado do §7.3.
        self.assertEqual(relatorio.stop_reason, S.STOP_INTERRUPTED)
        self.assertIn(relatorio.stop_reason, S.STOP_REASONS)
        # c) `detail` e `diagnostics` nomeiam a causa.
        self.assertIn("str", relatorio.detail)
        self.assertTrue(
            any("rodada 1" in d for d in relatorio.diagnostics),
            f"diagnostics não nomeia a rodada abortada: {relatorio.diagnostics}",
        )
        # d) a rodada 0, que rodou inteira, continua no relatório.
        self.assertGreaterEqual(len(relatorio.progress_by_round), 1)
        self.assertEqual(relatorio.progress_by_round[0]["round"], 0)
        self.assertEqual(
            relatorio.state["obj-1"]["contract"]["regra"]["content"], "apuracao 0"
        )

    def test_parada_por_erro_interno_e_persistida_no_store(self):
        """`_finish()` roda: o disco fica com o motivo, então `resume` retoma."""
        self._run(self._outcomes_com_lixo_na_rodada_2(["ainda-pior"]))
        self.assertEqual(
            self.store.chain_status("obj-1")["stop_reason"], S.STOP_INTERRUPTED
        )
        estado = S.StateStore.of(self.store).load(
            "repo-1", "obj-1", T.input_versions_hash(self.inputs)
        )
        self.assertIsNotNone(estado, "estado consolidado não foi salvo")
        self.assertEqual(estado.stop_reason, S.STOP_INTERRUPTED)

    def test_falha_do_store_na_integracao_vira_parada_e_nao_excecao(self):
        """Erro do STORE dentro da rodada tem o mesmo destino do outcome ruim."""
        original = C.plan_continuations

        def _explode(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error simulado")

        C.plan_continuations = _explode
        self.addCleanup(setattr, C, "plan_continuations", original)

        def outcomes_fn(round_no):
            return [{
                "objective_id": "obj-1",
                "task_id": self._ultima_tarefa(),
                "state": "partial",
                "leituras": [],
                "unmet_needs": [_need("a.py")],
                "contract_state": {"regra": {"content": "apuracao 0"}},
            }]

        relatorio = self._run(outcomes_fn)
        self.assertEqual(relatorio.stop_reason, S.STOP_INTERRUPTED)
        self.assertIn("OperationalError", relatorio.detail)
        # A rodada foi publicada antes do erro e não sumiu do relatório.
        self.assertEqual(len(relatorio.progress_by_round), 1)
        self.assertEqual(
            self.store.chain_status("obj-1")["stop_reason"], S.STOP_INTERRUPTED
        )

    def test_keyboard_interrupt_continua_sendo_interrupted(self):
        def outcomes_fn(round_no):
            raise KeyboardInterrupt

        relatorio = self._run(outcomes_fn)
        self.assertEqual(relatorio.stop_reason, S.STOP_INTERRUPTED)
        self.assertIn("interrompido", relatorio.detail)


# --------------------------------------------------------------------------
# B1 — nenhum passo de `run()` deixa lease aberto ao falhar
# --------------------------------------------------------------------------


class RunNaoVazaLeaseTest(unittest.TestCase):
    """Envelope e heartbeat têm a mesma blindagem de submit/poll."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wk-onda12-lease-run-")
        self.bindings = B.BindingStore(self.root)
        self.store = T.TaskStore.open(os.path.join(self.root, "runtime.db"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self.store.close)
        from runtime.tests.test_agent_contract import FakeAgentAdapter

        self.adapter = FakeAgentAdapter(payload={"objective_id": "obj-1"})
        self.binding = self.adapter.connect("process", {})
        self.addCleanup(self.adapter.close, self.binding)
        C.plan_from_objectives(
            self.store,
            [{"objective_id": "obj-1"}],
            snapshot_id="snap-1",
            budget={"max_bytes": 200_000, "max_tokens": 50_000},
        )

    def _run(self):
        return C.run(
            self.store,
            adapter=self.adapter,
            binding=self.binding,
            bindings=self.bindings,
        )

    def _leases_ativos_no_runtime(self):
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM leases"
        ).fetchone()[0]

    def test_falha_ao_montar_envelope_fecha_o_lease(self):
        original = C.envelope_for_task

        def _explode(*args, **kwargs):
            raise TypeError("estado acumulado não serializável")

        C.envelope_for_task = _explode
        self.addCleanup(setattr, C, "envelope_for_task", original)

        report = self._run()  # não levanta: a falha vira diagnóstico
        self.assertEqual(report.accepted, 0)
        self.assertEqual(
            self.bindings.active_leases(), (), "lease do binding ficou aberto"
        )
        self.assertEqual(self._leases_ativos_no_runtime(), 0, "lease do runtime ficou aberto")
        self.assertTrue(
            any("envelope" in str(d.detail) for d in report.diagnostics),
            f"nenhum diagnóstico nomeia a montagem do envelope: {report.diagnostics}",
        )

    def test_falha_no_heartbeat_fecha_o_lease(self):
        # Poll devolve `running` para forçar o batimento; o store falha nele.
        self.adapter.poll = lambda binding, execution_id: {"state": "running"}
        original = self.store.heartbeat

        def _explode(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error simulado")

        self.store.heartbeat = _explode
        self.addCleanup(setattr, self.store, "heartbeat", original)

        report = self._run()
        self.assertEqual(
            self.bindings.active_leases(), (), "lease do binding ficou aberto"
        )
        self.assertEqual(self._leases_ativos_no_runtime(), 0, "lease do runtime ficou aberto")
        self.assertTrue(
            any("lease" in str(d.detail) for d in report.diagnostics),
            f"nenhum diagnóstico nomeia a renovação do lease: {report.diagnostics}",
        )


# --------------------------------------------------------------------------
# M1 — `record_chain_round` conta rodadas concorrentes sob BEGIN IMMEDIATE
# --------------------------------------------------------------------------


class RecordChainRoundConcorrenteTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wk-onda12-rounds-")
        self.path = os.path.join(self.root, "runtime.db")
        self.store = T.TaskStore.open(self.path)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self.store.close)

    def test_duas_threads_contam_duas_rodadas(self):
        """Leitura fora da transação fazia a segunda rodada sumir do contador."""
        partida = threading.Barrier(2)
        erros = []

        def rodada(indice):
            loja = T.TaskStore.open(self.path)
            try:
                partida.wait(timeout=10)
                loja.record_chain_round(
                    "obj-1",
                    package_hash=f"pkg-{indice}",
                    usage={"tokens": 10},
                )
            except BaseException as exc:  # noqa: BLE001 — reportado no assert
                erros.append(f"{exc.__class__.__name__}: {exc}")
            finally:
                loja.close()

        threads = [threading.Thread(target=rodada, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertEqual(erros, [], "corrida levantou exceção")
        status = self.store.chain_status("obj-1")
        self.assertEqual(status["rounds_used"], 2, "uma das rodadas foi perdida")
        # O consumo também é acumulativo, não sobrescrito.
        self.assertEqual(status["consumed"]["calls"], 2)
        self.assertEqual(status["consumed"]["tokens"], 20)

    def test_counts_round_false_nao_avanca_contador(self):
        self.store.record_chain_round("obj-2", counts_round=False, usage={"tokens": 3})
        status = self.store.chain_status("obj-2")
        self.assertEqual(status["rounds_used"], 0)
        self.assertEqual(status["consumed"]["tokens"], 3)


# --------------------------------------------------------------------------
# M3 — `vendor` do lease nunca chega a `agents.json`
# --------------------------------------------------------------------------


class LeaseSemSegredoTest(unittest.TestCase):
    SEGREDO = "x"
    SENTINELA = "sk-vazamento-onda12"

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wk-onda12-lease-")
        self.bindings = B.BindingStore(self.root)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _raw(self):
        with open(self.bindings.path, encoding="utf-8") as fh:
            return fh.read()

    def test_open_lease_nao_persiste_vendor(self):
        lease = self.bindings.open_lease(
            "lease-1",
            binding_id="bind-1",
            task_id="t-1",
            vendor={"api_key": self.SEGREDO, "token": self.SENTINELA},
        )
        # Em MEMÓRIA quem abriu o lease continua com o vendor.
        self.assertEqual(lease.vendor["api_key"], self.SEGREDO)

        raw = self._raw()
        self.assertNotIn(self.SENTINELA, raw)
        self.assertNotIn("api_key", raw)
        gravado = json.loads(raw)["leases"]["lease-1"]
        self.assertEqual(gravado["vendor"], {})
        # Identidade e vigência continuam gravadas: a redação não apaga o lease.
        self.assertEqual(gravado["binding_id"], "bind-1")
        self.assertEqual(gravado["task_id"], "t-1")
        self.assertEqual(gravado["state"], B.LEASE_ACTIVE)

    def test_fechar_lease_nao_reintroduz_vendor(self):
        self.bindings.open_lease(
            "lease-2", binding_id="bind-1", vendor={"api_key": self.SENTINELA}
        )
        self.assertTrue(self.bindings.close_lease("lease-2", reason="concluído"))
        raw = self._raw()
        self.assertNotIn(self.SENTINELA, raw)
        self.assertEqual(json.loads(raw)["leases"]["lease-2"]["vendor"], {})
        self.assertEqual(json.loads(raw)["leases"]["lease-2"]["state"], B.LEASE_CLOSED)

    def test_lease_relido_do_disco_continua_valido(self):
        self.bindings.open_lease(
            "lease-3", binding_id="bind-1", vendor={"api_key": self.SENTINELA}
        )
        outro = B.BindingStore(self.root)
        self.assertTrue(outro.lease_valid("lease-3"))
        self.assertEqual(outro.get_lease("lease-3").vendor, {})


# --------------------------------------------------------------------------
# FLAKE — abertura concorrente do mesmo banco não colide em `schema_version`
# --------------------------------------------------------------------------


class AberturaConcorrenteTest(unittest.TestCase):
    """`UNIQUE constraint failed: schema_version.version` na ABERTURA."""

    THREADS = 8
    REPETICOES = 20

    def _abrir_em_paralelo(self, abrir, path):
        partida = threading.Barrier(self.THREADS)
        erros = []

        def alvo():
            try:
                partida.wait(timeout=30)
                fechar = abrir(path)
                fechar()
            except BaseException as exc:  # noqa: BLE001 — reportado no assert
                erros.append(f"{exc.__class__.__name__}: {exc}")

        threads = [threading.Thread(target=alvo) for _ in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        return erros

    def test_runtime_db_em_8_threads_repetido_20x(self):
        def abrir(path):
            loja = T.TaskStore.open(path)
            return loja.close

        for volta in range(self.REPETICOES):
            root = tempfile.mkdtemp(prefix="wk-onda12-open-rt-")
            self.addCleanup(shutil.rmtree, root, ignore_errors=True)
            erros = self._abrir_em_paralelo(abrir, os.path.join(root, "runtime.db"))
            self.assertEqual(erros, [], f"volta {volta}: abertura concorrente falhou")

    def test_knowledge_db_em_8_threads_repetido_20x(self):
        from knowledge import repository as K

        def abrir(path):
            repo = K.Repository.open(path)
            return repo.close

        for volta in range(self.REPETICOES):
            root = tempfile.mkdtemp(prefix="wk-onda12-open-kn-")
            self.addCleanup(shutil.rmtree, root, ignore_errors=True)
            erros = self._abrir_em_paralelo(abrir, os.path.join(root, "knowledge.db"))
            self.assertEqual(erros, [], f"volta {volta}: abertura concorrente falhou")

    def test_apply_schema_e_idempotente_na_mesma_conexao(self):
        root = tempfile.mkdtemp(prefix="wk-onda12-idem-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = os.path.join(root, "runtime.db")
        loja = T.TaskStore.open(path)
        self.addCleanup(loja.close)
        self.assertEqual(T.apply_schema(loja.conn, "2026-01-01T00:00:00+00:00"),
                         T.SCHEMA_VERSION)
        linhas = loja.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        self.assertEqual(linhas, 1, "reaplicar o schema duplicou a versão")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
