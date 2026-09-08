"""`coordinator.run_chain`: o laço automático do §7.3 numa única invocação.

O operador não roda `resume` por rodada. Estes testes exercitam o laço com um
executor determinístico em processo (nenhuma rede, nenhum agente externo) e
verificam o que o §7.3 exige de cada motivo de parada.
"""

import os
import shutil
import tempfile
import unittest

from runtime import coordinator as C
from runtime import state as S
from runtime import tasks as T
from runtime.executors.local_thread import LocalThreadExecutor


def _need(target, motivo="obrigacao"):
    return {"kind": "code", "target": target, "motivo": motivo, "need_id": f"need:{target}"}


class _Engine(LocalThreadExecutor):
    """Worker local que declara `deepening=True` (aprofunda leitura).

    O `LocalThreadExecutor` puro declara `deepening=False` e por isso
    `plan_continuations` recusa criar continuação para ele — correto em
    produção, e inútil para exercitar o LAÇO. Aqui a capacidade é declarada de
    propósito: o que está sob teste é o laço, não a política de capacidade.
    """

    def capabilities(self):
        caps = dict(super().capabilities())
        caps["deepening"] = True
        return caps


def _task_callable(objective=None, references=None, schema=None, cancel_event=None):
    objective = dict(objective or {})
    return {"objective_id": objective.get("objective_id", "obj-1"), "state": "partial"}


class RunChainTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wk-chain-loop-")
        self.path = os.path.join(self.root, "runtime.db")
        self.store = T.TaskStore.open(self.path)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self.store.close)
        self.inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        self.executor = _Engine(registry={"investigation": _task_callable}, max_workers=2)
        self.addCleanup(self.executor.shutdown)
        self.task = self.store.create_task(
            T.TaskKind.INVESTIGATION,
            {"objective_id": "obj-1", "kind": "investigation"},
            self.inputs,
        )
        self.store.refresh_states()
        self._passo = 0

    def _outcomes_factory(self, roteiro):
        """`outcomes_fn` roteirizado, com passo GLOBAL entre invocações.

        O passo não pode reiniciar a cada `run_chain`: o §7.3 fala de cadeia,
        e uma segunda invocação que reapresentasse o resultado da rodada 1
        seria, corretamente, recusada por falta de progresso — mascarando o
        que estes testes querem medir (o teto de rodadas).
        """
        self.rodadas_pedidas = []

        def outcomes_fn(round_no):
            self.rodadas_pedidas.append(round_no)
            item = roteiro[min(self._passo, len(roteiro) - 1)]
            self._passo += 1
            return [dict(item, task_id=self._ultima_tarefa())]

        return outcomes_fn

    def _outcomes_sempre_progride(self):
        """Cada rodada fecha uma obrigação nova e abre a seguinte: progresso real."""

        def outcomes_fn(round_no):
            k = self._passo
            self._passo += 1
            return [{
                "objective_id": "obj-1",
                "task_id": self._ultima_tarefa(),
                "state": "partial",
                "leituras": (
                    [{"need_id": f"need:f{k - 1}.py", "satisfeita": True,
                      "evidencia": [f"f{k - 1}.py:1-1"]}] if k else []
                ),
                "unmet_needs": [_need(f"f{k}.py")],
                "contract_state": {"regra": {"content": f"apuracao {k}"}},
            }]

        return outcomes_fn

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

    # -- conclusão ---------------------------------------------------------

    def test_para_em_completed_quando_nao_ha_mais_parcial(self):
        relatorio = self._run(
            self._outcomes_factory([
                {"objective_id": "obj-1", "state": "partial",
                 "unmet_needs": [_need("a.py")],
                 "leituras": [],
                 "contract_state": {"regra": {"content": "primeira apuracao"}}},
                {"objective_id": "obj-1", "state": "complete",
                 "leituras": [{"need_id": "need:a.py", "satisfeita": True,
                               "evidencia": ["a.py:1-10"]}]},
            ])
        )
        self.assertEqual(relatorio.stop_reason, S.STOP_COMPLETED)
        self.assertGreaterEqual(relatorio.rounds, 1)
        estado = relatorio.state["obj-1"]
        self.assertEqual(estado["stop_reason"], "completed")
        self.assertIn("need:a.py", estado["satisfied_readings"])
        self.assertEqual(estado["contract"]["regra"]["content"], "primeira apuracao")

    def test_t06_estado_do_laco_acumula_a_mais_b(self):
        relatorio = self._run(
            self._outcomes_factory([
                {"objective_id": "obj-1", "state": "partial",
                 "unmet_needs": [_need("a.py"), _need("b.py")],
                 "contract_state": {"regra": {"content": "A"}},
                 "leituras": [{"need_id": "need:a.py", "satisfeita": True,
                               "evidencia": ["a.py:1-10"]}]},
                {"objective_id": "obj-1", "state": "complete",
                 "contract_state": {"impacto": {"content": "B"}},
                 "leituras": [{"need_id": "need:b.py", "satisfeita": True,
                               "evidencia": ["b.py:1-10"]}]},
            ])
        )
        estado = relatorio.state["obj-1"]
        self.assertEqual(estado["contract"]["regra"]["content"], "A")
        self.assertEqual(estado["contract"]["impacto"]["content"], "B")
        self.assertEqual(
            set(estado["satisfied_readings"]), {"need:a.py", "need:b.py"},
            "leitura fechada na rodada A continua fechada depois de B",
        )

    # -- parada sem progresso ---------------------------------------------

    def test_para_em_no_progress_sem_repetir_pacote(self):
        """Rodadas que não movem nada param a cadeia — e não recriam tarefa."""
        roteiro = [{
            "objective_id": "obj-1", "state": "partial",
            "unmet_needs": [_need("a.py")],
            "leituras": [],
        }]
        relatorio = self._run(self._outcomes_factory(roteiro))
        self.assertEqual(relatorio.stop_reason, S.STOP_NO_PROGRESS)
        self.assertIn("a.py", relatorio.detail)
        # Tarefa-base + no máximo UMA continuação: o pacote não foi repetido.
        self.assertLessEqual(len(self.store.all_tasks()), 2)

    # -- teto de rodadas entre invocações ----------------------------------

    def test_max_rounds_e_teto_total_entre_invocacoes_sucessivas(self):
        primeira = self._run(self._outcomes_sempre_progride(), max_rounds=2)
        self.assertEqual(primeira.stop_reason, S.STOP_BUDGET_EXHAUSTED)
        usado = self.store.chain_status("obj-1")["rounds_used"]
        self.assertEqual(usado, 2)

        # SEGUNDA invocação: nenhum crédito novo, mesmo teto.
        segunda = self._run(self._outcomes_sempre_progride(), max_rounds=2)
        self.assertEqual(segunda.stop_reason, S.STOP_BUDGET_EXHAUSTED)
        self.assertEqual(segunda.rounds, 0, "invocação nova não ganha rodada")
        self.assertEqual(self.store.chain_status("obj-1")["rounds_used"], 2)

        # Ampliar de 2 para 3 libera exatamente mais uma.
        terceira = self._run(self._outcomes_sempre_progride(), max_rounds=3)
        self.assertEqual(self.store.chain_status("obj-1")["rounds_used"], 3)
        self.assertEqual(terceira.stop_reason, S.STOP_BUDGET_EXHAUSTED)

    # -- executor indisponível --------------------------------------------

    def test_executor_indisponivel_preserva_progresso_e_nao_conta_rodada(self):
        class _SemDespacho:
            """Engine conhecida, SEM despacho disponível agora (§10.4.4).

            Implementa o protocolo inteiro de propósito: o que derruba o
            despacho tem de ser a capacidade declarada (`dispatch: False`), e
            não a ausência de um método — senão o teste provaria outra coisa.
            """

            prefix = "sem-despacho"

            def capabilities(self):
                return {"dispatch": False, "reason": "CLI não encontrada no PATH"}

            def submit(self, *a, **k):  # pragma: no cover - nunca chamado
                raise AssertionError("não deve despachar")

            def status(self, execution_id):  # pragma: no cover
                raise AssertionError("não deve consultar")

            def result(self, execution_id):  # pragma: no cover
                raise AssertionError("não deve coletar")

            def cancel(self, execution_id):  # pragma: no cover
                return False

        antes = self.store.chain_status("obj-1")["rounds_used"]
        relatorio = C.run_chain(
            self.store,
            objective_ids=["obj-1"],
            input_versions=self.inputs,
            outcomes_fn=lambda n: [],
            repo_id="repo-1",
            executor=_SemDespacho(),
        )
        self.assertEqual(relatorio.stop_reason, S.STOP_EXECUTOR_UNAVAILABLE)
        self.assertEqual(relatorio.rounds, 0)
        self.assertIn("CLI não encontrada no PATH", relatorio.detail)
        self.assertEqual(self.store.chain_status("obj-1")["rounds_used"], antes)
        self.assertEqual(
            self.store.chain_status("obj-1")["stop_reason"], "executor_unavailable"
        )

    # -- orçamento ---------------------------------------------------------

    def test_orcamento_esgotado_preserva_estado(self):
        self.store.set_chain_limits("obj-1", budget={"max_tokens": 1})
        self.store.record_chain_round("obj-1", usage={"tokens": 5}, counts_round=False)
        relatorio = self._run(
            self._outcomes_factory([
                {"objective_id": "obj-1", "state": "partial",
                 "unmet_needs": [_need("a.py")],
                 "leituras": [{"need_id": "need:a.py", "satisfeita": True,
                               "evidencia": ["a.py:1-1"]}]},
            ])
        )
        self.assertEqual(relatorio.stop_reason, S.STOP_BUDGET_EXHAUSTED)
        self.assertIn("orçamento", relatorio.detail)
        # Estado preservado: a leitura fechada continua fechada.
        self.assertIn("need:a.py", relatorio.state["obj-1"]["satisfied_readings"])

    # -- retomada ----------------------------------------------------------

    def test_retomada_idempotente_nao_reaplica_resultado(self):
        roteiro = [{
            "objective_id": "obj-1", "state": "partial",
            "result_hash": "fixo",
            "unmet_needs": [_need("a.py")],
            "leituras": [{"need_id": "need:a.py", "satisfeita": True,
                          "evidencia": ["a.py:1-1"]}],
        }]
        primeira = self._run(self._outcomes_factory(roteiro))
        tentativas = primeira.state["obj-1"]["attempts"]

        # "Reinício do processo": store novo sobre o MESMO arquivo.
        outro = T.TaskStore.open(self.path)
        self.addCleanup(outro.close)
        estados = S.StateStore.of(outro)
        estado = estados.load("repo-1", "obj-1", T.input_versions_hash(self.inputs))
        self.assertIsNotNone(estado)
        self.assertEqual(estado.attempts, tentativas)

        _, progresso = estados.apply(estado, roteiro[0])
        self.assertTrue(progresso.duplicate, "mesmo result_hash não pode reaplicar")
        self.assertEqual(
            estados.load("repo-1", "obj-1", T.input_versions_hash(self.inputs)).attempts,
            tentativas,
        )

    # -- callback ----------------------------------------------------------

    def test_on_round_recebe_cada_rodada(self):
        vistas = []
        self._run(
            self._outcomes_factory([
                {"objective_id": "obj-1", "state": "partial", "unmet_needs": [_need("a.py")]},
            ]),
            on_round=vistas.append,
        )
        self.assertTrue(vistas)
        self.assertIsInstance(vistas[0], C.RoundReport)
        self.assertEqual(vistas[0].round_no, 0)
        self.assertIn("progress", vistas[0].to_dict())


if __name__ == "__main__":
    unittest.main()
