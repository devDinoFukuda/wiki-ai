"""Estado consolidado, invariante de leituras e cadeia automática (§7.1-§7.3).

Cobre T06-T10 do requisito e as regressões da auditoria sobre `runtime`:

| Teste                                            | O que prova                        |
|--------------------------------------------------|------------------------------------|
| `test_t06_*`                                     | A depois B => A+B no estado         |
| `test_leitura_satisfeita_nao_reabre_*`           | invariante §7.1                     |
| `test_invalidate_*`                              | reabertura SÓ com causa persistida  |
| `test_no_progress_*`                             | para e não repete pacote idêntico   |
| `test_max_rounds_e_teto_de_cadeia_*`             | teto TOTAL entre invocações         |
| `test_recusa_de_executor_nao_conta_rodada`       | §7.3 "não contar recusa de executor"|
| `test_orcamento_persistido_*`                    | orçamento com consumo persistido    |
| `test_retomada_idempotente_*`                    | result_hash não reaplica            |
"""

import os
import shutil
import tempfile
import unittest

from runtime import coordinator as C
from runtime import state as S
from runtime import tasks as T


def _need(target, motivo="obrigacao", kind="code"):
    return {"kind": kind, "target": target, "motivo": motivo, "need_id": f"need:{target}"}


def _evidence(path, start=1, end=10):
    return {"path": path, "line_start": start, "line_end": end}


class ConsolidatedStateTest(unittest.TestCase):
    """Álgebra pura do estado: `apply_result` como DELTA (§7.1)."""

    def setUp(self):
        self.state = S.new_state("repo-1", "obj-1", "rev-1")

    def test_t06_contrato_acumula_entre_rodadas(self):
        """A rodada B não apaga o que a rodada A apurou: o estado tem A+B."""
        depois_a, progresso_a = S.apply_result(
            self.state,
            {
                "result_hash": "hash-a",
                "contract": {"regra": {"content": "A apurou a regra", "evidence_refs": [_evidence("a.py")]}},
            },
            round_no=1,
        )
        self.assertEqual(progresso_a.contract_fields_filled, ("regra",))

        # A rodada B fala SÓ do impacto — nem menciona `regra`.
        depois_b, progresso_b = S.apply_result(
            depois_a,
            {
                "result_hash": "hash-b",
                "contract": {"impacto": {"content": "B apurou o impacto", "evidence_refs": [_evidence("b.py")]}},
            },
            round_no=2,
        )
        conteudo = depois_b.contract_content()
        self.assertEqual(conteudo["regra"], "A apurou a regra")
        self.assertEqual(conteudo["impacto"], "B apurou o impacto")
        self.assertEqual(progresso_b.contract_fields_filled, ("impacto",))
        # Evidência das DUAS rodadas continua aceita.
        self.assertEqual(len(depois_b.accepted_evidence), 2)

    def test_t06_contrato_vazio_nao_apaga_conteudo_apurado(self):
        depois_a, _ = S.apply_result(
            self.state, {"result_hash": "a", "contract": {"regra": {"content": "conteudo real"}}}
        )
        depois_b, progresso = S.apply_result(
            depois_a, {"result_hash": "b", "contract": {"regra": {"content": ""}}}
        )
        self.assertEqual(depois_b.contract_content()["regra"], "conteudo real")
        self.assertFalse(progresso.has_progress)

    def test_leitura_satisfeita_nao_reabre_por_omissao(self):
        """Invariante §7.1: leituras_satisfeitas(n) ⊆ leituras_satisfeitas(n+1)."""
        aberto, _ = S.apply_result(
            self.state, {"result_hash": "r0", "unmet_needs": [_need("a.py"), _need("b.py")]}
        )
        self.assertEqual(aberto.open_ids(), {"need:a.py", "need:b.py"})

        fechou_a, progresso = S.apply_result(
            aberto,
            {
                "result_hash": "r1",
                "leituras": [{"need_id": "need:a.py", "satisfeita": True, "evidencia": ["a.py:1-10"]}],
                "unmet_needs": [_need("b.py")],
            },
            round_no=1,
        )
        self.assertEqual(fechou_a.satisfied_ids(), {"need:a.py"})
        self.assertEqual(progresso.obligations_closed, ("need:a.py",))
        self.assertTrue(progresso.has_progress)

        # Rodada n+1 NÃO restata A — e A continua satisfeita.
        rodada2, _ = S.apply_result(
            fechou_a,
            {"result_hash": "r2", "unmet_needs": [_need("b.py")]},
            round_no=2,
        )
        self.assertIn("need:a.py", rodada2.satisfied_ids())
        self.assertNotIn("need:a.py", rodada2.open_ids())

    def test_leitura_satisfeita_nao_reabre_por_reaparecer_como_pendente(self):
        """Reaparecer na lista de pendências NÃO reabre: só a obrigação mudar reabre."""
        fechou, _ = S.apply_result(
            self.state,
            {
                "result_hash": "r1",
                "leituras": [{"need_id": "need:a.py", "target": "a.py", "satisfeita": True,
                              "evidencia": ["a.py:1-10"]}],
                "unmet_needs": [],
            },
        )
        # MESMA obrigação (mesmo kind/target/motivo) devolvida como pendente.
        depois, progresso = S.apply_result(
            fechou, {"result_hash": "r2", "unmet_needs": [_need("a.py")]}
        )
        self.assertIn("need:a.py", depois.satisfied_ids())
        self.assertEqual(progresso.readings_reopened, ())

    def test_obrigacao_alterada_reabre_com_causa_persistida(self):
        fechou, _ = S.apply_result(
            self.state,
            {
                "result_hash": "r1",
                "leituras": [{"need_id": "need:a.py", "target": "a.py", "satisfeita": True,
                              "evidencia": ["a.py:1-10"]}],
                "unmet_needs": [_need("a.py")],
            },
        )
        self.assertIn("need:a.py", fechou.satisfied_ids())
        # A MESMA need_id com OUTRO motivo é outra obrigação.
        depois, progresso = S.apply_result(
            fechou, {"result_hash": "r2", "unmet_needs": [_need("a.py", motivo="outra obrigacao")]}
        )
        self.assertNotIn("need:a.py", depois.satisfied_ids())
        self.assertIn("need:a.py", depois.open_ids())
        self.assertEqual(progresso.readings_reopened, ("need:a.py",))
        self.assertEqual(depois.invalidations[-1]["cause"], S.CAUSE_OBLIGATION_CHANGED)

    def test_invalidate_exige_causa_e_persiste(self):
        fechou, _ = S.apply_result(
            self.state,
            {
                "result_hash": "r1",
                "leituras": [{"need_id": "need:a.py", "satisfeita": True, "evidencia": ["a.py:1-10"]}],
            },
        )
        with self.assertRaises(S.StateError):
            S.invalidate(fechou, "need:a.py", "")

        reaberto = S.invalidate(
            fechou, "need:a.py", S.CAUSE_EVIDENCE_CHANGED, detail="arquivo mudou", round_no=3
        )
        self.assertNotIn("need:a.py", reaberto.satisfied_ids())
        self.assertIn("need:a.py", reaberto.open_ids())
        causa = reaberto.invalidations[-1]
        self.assertEqual(causa["cause"], S.CAUSE_EVIDENCE_CHANGED)
        self.assertEqual(causa["detail"], "arquivo mudou")
        self.assertEqual(causa["round"], 3)

    def test_invalidate_de_leitura_nunca_satisfeita_e_noop(self):
        self.assertEqual(S.invalidate(self.state, "need:x", S.CAUSE_OPERATOR), self.state)

    def test_progresso_exige_obrigacao_evidencia_ou_conflito(self):
        """Texto novo sem citação nova não é progresso (§7.3)."""
        base, _ = S.apply_result(
            self.state, {"result_hash": "a", "contract": {"regra": {"content": "x", "evidence_refs": [_evidence("a.py")]}}}
        )
        # Mesma citação, texto reescrito: nada moveu.
        _, progresso = S.apply_result(
            base,
            {"result_hash": "b", "contract": {"regra": {"content": "x reescrito", "evidence_refs": [_evidence("a.py")]}}},
        )
        self.assertFalse(progresso.has_progress)

    def test_conflito_resolvido_conta_como_progresso(self):
        com_conflito, _ = S.apply_result(
            self.state, {"result_hash": "a", "conflicts": [{"conflict_id": "c1", "resolved": False}]}
        )
        _, progresso = S.apply_result(
            com_conflito, {"result_hash": "b", "conflicts": [{"conflict_id": "c1", "resolved": True}]}
        )
        self.assertEqual(progresso.conflicts_resolved, ("c1",))
        self.assertTrue(progresso.has_progress)

    def test_resultado_recusado_entra_com_motivo_e_nao_progride(self):
        depois, progresso = S.apply_result(
            self.state, {"result_hash": "r", "rejected": True, "rejection_reason": "schema_invalid"}
        )
        self.assertEqual(len(depois.rejected_results), 1)
        self.assertEqual(depois.rejected_results[0]["motivo"], "schema_invalid")
        self.assertFalse(progresso.has_progress)
        self.assertEqual(depois.attempts, 1)

    def test_retomada_idempotente_mesmo_result_hash(self):
        uma, progresso1 = S.apply_result(
            self.state,
            {"result_hash": "identico", "leituras": [{"need_id": "n1", "satisfeita": True,
                                                      "evidencia": ["a.py:1-2"]}]},
        )
        duas, progresso2 = S.apply_result(uma, {"result_hash": "identico", "leituras": []})
        self.assertTrue(progresso1.has_progress)
        self.assertTrue(progresso2.duplicate)
        self.assertFalse(progresso2.has_progress)
        self.assertEqual(duas.attempts, uma.attempts)
        self.assertEqual(duas.to_dict(), uma.to_dict())

    def test_stop_reason_fora_do_vocabulario_e_recusado(self):
        with self.assertRaises(S.StateError):
            S.with_stop_reason(self.state, "parou_por_algum_motivo")
        self.assertEqual(
            S.with_stop_reason(self.state, S.STOP_NO_PROGRESS).stop_reason, "no_progress"
        )

    def test_pacote_identico_tem_o_mesmo_hash_em_qualquer_ordem(self):
        a = S.needs_package_hash([_need("a.py"), _need("b.py")])
        b = S.needs_package_hash([_need("b.py"), _need("a.py")])
        self.assertEqual(a, b)
        self.assertNotEqual(a, S.needs_package_hash([_need("c.py")]))


class StateStorePersistenceTest(unittest.TestCase):
    """O estado sobrevive ao processo — §7.1 "não reconstruir da última tarefa"."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wk-state-")
        self.path = os.path.join(self.root, "runtime.db")
        self.store = T.TaskStore.open(self.path)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self.store.close)

    def test_estado_persiste_por_repo_objetivo_e_revisao(self):
        states = S.StateStore.of(self.store)
        base = states.load_or_new("repo-1", "obj-1", "rev-1")
        depois, _ = S.apply_result(base, {"result_hash": "a", "contract": {"regra": {"content": "R"}}})
        states.save(depois, now=T.utc_now())

        # Outro "processo": conexão nova sobre o MESMO arquivo.
        outro = T.TaskStore.open(self.path)
        self.addCleanup(outro.close)
        lido = S.StateStore.of(outro).load("repo-1", "obj-1", "rev-1")
        self.assertIsNotNone(lido)
        self.assertEqual(lido.contract_content()["regra"], "R")
        # Chave diferente => estado diferente (nada vaza entre revisões).
        self.assertIsNone(S.StateStore.of(outro).load("repo-1", "obj-1", "rev-2"))
        self.assertIsNone(S.StateStore.of(outro).load("repo-2", "obj-1", "rev-1"))


class ChainLimitsTest(unittest.TestCase):
    """`max_rounds` é teto TOTAL da cadeia; orçamento é persistido (§7.3)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wk-chain-")
        self.store = T.TaskStore.open(os.path.join(self.root, "runtime.db"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self.store.close)

    def test_set_chain_limits_nao_zera_rodadas_consumidas(self):
        self.store.set_chain_limits("obj-1", max_rounds=3)
        for _ in range(3):
            self.store.record_chain_round("obj-1")
        status = self.store.chain_status("obj-1")
        self.assertEqual(status["rounds_used"], 3)
        self.assertEqual(status["rounds_left"], 0)

        # Ampliar de 3 para 6 depois de consumir 3 permite MAIS 3 — não 6.
        ampliado = self.store.set_chain_limits("obj-1", max_rounds=6)
        self.assertEqual(ampliado["rounds_used"], 3)
        self.assertEqual(ampliado["rounds_left"], 3)

    def test_orcamento_persistido_com_consumo(self):
        self.store.set_chain_limits("obj-1", budget={"max_tokens": 100})
        self.store.record_chain_round("obj-1", usage={"tokens": 60})
        self.store.record_chain_round("obj-1", usage={"tokens": 60})
        status = self.store.chain_status("obj-1")
        self.assertEqual(status["consumed"]["tokens"], 120)
        self.assertEqual(status["consumed"]["calls"], 2)
        esgotado, detalhe = S.budget_exhausted(status["budget"], status["consumed"])
        self.assertTrue(esgotado)
        self.assertIn("max_tokens", detalhe)

    def test_recusa_de_executor_nao_conta_rodada(self):
        self.store.set_chain_limits("obj-1", max_rounds=3)
        self.store.record_chain_round("obj-1", usage={"tokens": 5}, counts_round=False)
        status = self.store.chain_status("obj-1")
        self.assertEqual(status["rounds_used"], 0, "recusa de executor não consome rodada")
        self.assertEqual(status["consumed"]["tokens"], 5)

    def test_stop_reason_persistido_e_vocabulario_fechado(self):
        self.store.set_chain_stop("obj-1", S.STOP_BUDGET_EXHAUSTED)
        self.assertEqual(self.store.chain_status("obj-1")["stop_reason"], "budget_exhausted")
        with self.assertRaises(T.RuntimeStoreError):
            self.store.set_chain_stop("obj-1", "sei_la")


class PlanContinuationsGuardTest(unittest.TestCase):
    """`plan_continuations` recusa continuar sem progresso material (§7.3)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wk-plan-")
        self.store = T.TaskStore.open(os.path.join(self.root, "runtime.db"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self.store.close)
        self.inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        self.task = self.store.create_task(
            T.TaskKind.INVESTIGATION, {"objective_id": "obj-1"}, self.inputs
        )

    def _partial(self, task_id, needs):
        return {
            "objective_id": "obj-1",
            "task_id": task_id,
            "state": "partial",
            "unmet_needs": needs,
        }

    def test_pacote_identico_e_recusado_com_no_progress(self):
        needs = [_need("a.py"), _need("b.py")]
        primeiro = C.plan_continuations(
            self.store, [self._partial(self.task.task_id, needs)], input_versions=self.inputs
        )
        self.assertEqual(len(primeiro["criadas"]), 1)

        segundo = C.plan_continuations(
            self.store, [self._partial(self.task.task_id, needs)], input_versions=self.inputs
        )
        self.assertEqual(segundo["criadas"], [])
        self.assertEqual(segundo["recusadas"][0]["stop_reason"], S.STOP_NO_PROGRESS)
        self.assertIn("idêntico ao já despachado", segundo["recusadas"][0]["motivo"])
        # E o diagnóstico nomeia obrigação e alvo (§7.3).
        self.assertIn("a.py", segundo["recusadas"][0]["motivo"])

    def test_rodada_anterior_sem_progresso_para_a_cadeia(self):
        needs_1 = [_need("a.py")]
        C.plan_continuations(
            self.store, [self._partial(self.task.task_id, needs_1)], input_versions=self.inputs
        )
        # Pacote DIFERENTE, mas a rodada anterior não moveu nada.
        sem_progresso = S.Progress().to_dict()
        recusa = C.plan_continuations(
            self.store,
            [self._partial(self.task.task_id, [_need("c.py")])],
            input_versions=self.inputs,
            progress={"obj-1": sem_progresso},
        )
        self.assertEqual(recusa["criadas"], [])
        self.assertEqual(recusa["recusadas"][0]["stop_reason"], S.STOP_NO_PROGRESS)
        self.assertIn("c.py", recusa["recusadas"][0]["motivo"])
        self.assertEqual(self.store.chain_status("obj-1")["stop_reason"], S.STOP_NO_PROGRESS)

    def test_com_progresso_a_cadeia_continua(self):
        C.plan_continuations(
            self.store, [self._partial(self.task.task_id, [_need("a.py")])], input_versions=self.inputs
        )
        progresso = S.Progress(obligations_closed=("need:a.py",)).to_dict()
        segue = C.plan_continuations(
            self.store,
            [self._partial(self.task.task_id, [_need("c.py")])],
            input_versions=self.inputs,
            progress={"obj-1": progresso},
        )
        self.assertEqual(len(segue["criadas"]), 1, segue["recusadas"])

    def test_teto_de_cadeia_vale_entre_invocacoes(self):
        """Nova invocação NÃO recebe crédito novo de rodadas (§7.3)."""
        self.store.set_chain_limits("obj-1", max_rounds=1)
        primeira = C.plan_continuations(
            self.store, [self._partial(self.task.task_id, [_need("a.py")])], input_versions=self.inputs
        )
        self.assertEqual(len(primeira["criadas"]), 1)
        self.assertEqual(self.store.chain_status("obj-1")["rounds_used"], 1)

        # "Nova invocação": outra tarefa-base, pacote novo, progresso real.
        outra = self.store.create_task(
            T.TaskKind.INVESTIGATION, {"objective_id": "obj-1", "n": 2}, self.inputs
        )
        segunda = C.plan_continuations(
            self.store,
            [self._partial(outra.task_id, [_need("z.py")])],
            input_versions=self.inputs,
            progress={"obj-1": S.Progress(obligations_closed=("need:a.py",)).to_dict()},
        )
        self.assertEqual(segunda["criadas"], [])
        self.assertEqual(segunda["recusadas"][0]["stop_reason"], S.STOP_BUDGET_EXHAUSTED)
        self.assertIn("teto total", segunda["recusadas"][0]["motivo"])

        # Ampliar o teto libera exatamente mais uma rodada.
        self.store.set_chain_limits("obj-1", max_rounds=2)
        terceira = C.plan_continuations(
            self.store,
            [self._partial(outra.task_id, [_need("z.py")])],
            input_versions=self.inputs,
            progress={"obj-1": S.Progress(obligations_closed=("need:a.py",)).to_dict()},
        )
        self.assertEqual(len(terceira["criadas"]), 1, terceira["recusadas"])

    def test_orcamento_esgotado_recusa_com_motivo_canonico(self):
        self.store.set_chain_limits("obj-1", max_rounds=5, budget={"max_tokens": 10})
        self.store.record_chain_round("obj-1", usage={"tokens": 10}, counts_round=False)
        recusa = C.plan_continuations(
            self.store, [self._partial(self.task.task_id, [_need("a.py")])], input_versions=self.inputs
        )
        self.assertEqual(recusa["criadas"], [])
        self.assertEqual(recusa["recusadas"][0]["stop_reason"], S.STOP_BUDGET_EXHAUSTED)
        self.assertIn("orçamento", recusa["recusadas"][0]["motivo"])

    def test_chain_guard_desligado_preserva_o_planejamento_puro(self):
        needs = [_need("a.py")]
        C.plan_continuations(
            self.store, [self._partial(self.task.task_id, needs)], input_versions=self.inputs
        )
        de_novo = C.plan_continuations(
            self.store,
            [self._partial(self.task.task_id, needs)],
            input_versions=self.inputs,
            chain_guard=False,
        )
        # Sem a guarda, o dedupe por effect_identity devolve a MESMA tarefa.
        self.assertEqual(len(de_novo["criadas"]), 1)
        self.assertEqual(len(self.store.all_tasks()), 2)


if __name__ == "__main__":
    unittest.main()
