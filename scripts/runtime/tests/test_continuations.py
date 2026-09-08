"""Testes permanentes de audit finding #3 runtime (Onda10-C).

Contrato: ResultSchema aceita reading_needs/reading_satisfied mas rejeita campos de controle.
plan_continuations cria continuacoes para partial com needs, idempotente.
"""

import os
import tempfile
import unittest

from runtime import coordinator as C
from runtime import tasks as T


class ContinuationsTest(unittest.TestCase):
    """Testes para continuacoes (Onda10-C, achado #3)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onda10c-")
        self.db_path = os.path.join(self.tmpdir, "runtime.db")
        self.store = T.TaskStore.open(self.db_path)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_result_schema_accepts_reading_needs_and_satisfied(self):
        """ResultSchema aceita reading_satisfied -> accept_result NAO rejeita."""
        objective = {"objective_id": "obj-1", "task_kind": "investigation"}
        inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        task = self.store.create_task(T.TaskKind.INVESTIGATION, objective, inputs)
        lease = self.store.acquire_lease(task.task_id, "owner-1")
        execution_id = "exec-1"
        self.store.start_attempt(task.task_id, lease.lease_id, execution_id)

        envelope = {
            "execution_id": execution_id,
            "output": {
                "objective_id": "obj-1",
                "state": "partial",
                "facts": [],
                "reading_needs": [
                    {"kind": "code", "target": "scripts/runtime/tasks.py", "motivo": "confirmar assinatura"}
                ],
                "reading_satisfied": [
                    {"target": "scripts/runtime/coordinator.py", "evidence_refs": ["ref-1"]}
                ],
            },
        }
        verdict = C.accept_result(self.store, task, execution_id, envelope)

        # Aceite: accept_result nao deve rejeitar
        self.assertTrue(
            verdict.accepted,
            f"accept_result deve aceitar reading_satisfied; detail: {verdict.detail}"
        )
        self.assertIn(
            "reading_satisfied",
            verdict.output,
            "output deve conter reading_satisfied"
        )

    def test_result_schema_rejects_control_fields(self):
        """ResultSchema ainda deve rejeitar campos de controle (budget)."""
        objective = {"objective_id": "obj-ctrl"}
        inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        task = self.store.create_task(T.TaskKind.INVESTIGATION, objective, inputs)
        lease = self.store.acquire_lease(task.task_id, "owner-1")
        execution_id = "exec-ctrl"
        self.store.start_attempt(task.task_id, lease.lease_id, execution_id)

        bad_envelope = {
            "execution_id": execution_id,
            "output": {"objective_id": "obj-ctrl", "budget": {"tokens": 999999}},
        }
        verdict = C.accept_result(self.store, task, execution_id, bad_envelope)

        # Aceite: deve rejeitar campo de controle
        self.assertFalse(
            verdict.accepted,
            "accept_result deve rejeitar budget (campo de controle)"
        )
        self.assertEqual(
            verdict.reason,
            C.RejectionReason.CONTROL_FIELD,
            f"reason deve ser CONTROL_FIELD, got {verdict.reason}"
        )

    def test_plan_continuations_creates_for_partial_with_needs(self):
        """plan_continuations cria tarefas para partial com unmet_needs."""
        objective = {"objective_id": "obj-1", "task_kind": "investigation"}
        inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        task = self.store.create_task(T.TaskKind.INVESTIGATION, objective, inputs)
        lease = self.store.acquire_lease(task.task_id, "owner-1")
        execution_id = "exec-1"
        self.store.start_attempt(task.task_id, lease.lease_id, execution_id)

        needs = [
            {"kind": "code", "target": f"file_{i}.py", "motivo": f"obrigacao_{i}"}
            for i in range(4)
        ]
        outcomes = [
            {
                "objective_id": "obj-1",
                "task_id": task.task_id,
                "state": "partial",
                "unmet_needs": needs,
            }
        ]
        result1 = C.plan_continuations(self.store, outcomes, input_versions=inputs)

        # Aceite: deve criar uma tarefa
        self.assertEqual(
            len(result1["criadas"]),
            1,
            f"1a chamada deve criar 1 tarefa, got {len(result1['criadas'])}"
        )
        self.assertEqual(
            len(result1["recusadas"]),
            0,
            "Nao deve haver recusadas na primeira chamada"
        )

        # Verify continuation task exists
        cont_task_id = result1["criadas"][0]
        cont_task = self.store.get(cont_task_id)
        self.assertIsNotNone(
            cont_task,
            "Tarefa criada deve existir no store"
        )

    def test_plan_continuations_is_idempotent(self):
        """Chamar plan_continuations repetidamente nao duplica tarefas."""
        objective = {"objective_id": "obj-1", "task_kind": "investigation"}
        inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        task = self.store.create_task(T.TaskKind.INVESTIGATION, objective, inputs)
        lease = self.store.acquire_lease(task.task_id, "owner-1")
        execution_id = "exec-1"
        self.store.start_attempt(task.task_id, lease.lease_id, execution_id)

        needs = [
            {"kind": "code", "target": f"file_{i}.py", "motivo": f"obrigacao_{i}"}
            for i in range(4)
        ]
        outcomes = [
            {
                "objective_id": "obj-1",
                "task_id": task.task_id,
                "state": "partial",
                "unmet_needs": needs,
            }
        ]

        # Primera llamada
        result1 = C.plan_continuations(self.store, outcomes, input_versions=inputs)
        criadas_1 = result1["criadas"][0]

        # Segunda chamada (repeticion) com o MESMO pacote de necessidades.
        result2 = C.plan_continuations(self.store, outcomes, input_versions=inputs)

        # Aceite (§7.3): repetir o mesmo pacote nao cria nada E e recusado com
        # motivo canonico `no_progress` — antes a chamada repetida devolvia a
        # mesma tarefa (dedupe por effect_identity) sem dizer que a cadeia nao
        # tinha avancado. Nao duplicar continua valendo (total_tasks == 2).
        self.assertEqual(
            result2["criadas"],
            [],
            "Pacote identico ao ja despachado nao pode gerar nova continuacao"
        )
        self.assertEqual(len(result2["recusadas"]), 1)
        self.assertEqual(result2["recusadas"][0]["stop_reason"], "no_progress")
        self.assertIn("idêntico ao já despachado", result2["recusadas"][0]["motivo"])
        self.assertIn(criadas_1, result1["criadas"])
        total_tasks = len(self.store.all_tasks())
        # Original + 1 continuation = 2 total
        self.assertEqual(
            total_tasks,
            2,
            f"Total de tarefas deve ser 2, got {total_tasks}"
        )

    def test_plan_continuations_refuses_when_round_exceeds_max(self):
        """plan_continuations recusa quando round > max_rounds."""
        objective = {"objective_id": "obj-1", "task_kind": "investigation"}
        inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        task = self.store.create_task(T.TaskKind.INVESTIGATION, objective, inputs)
        lease = self.store.acquire_lease(task.task_id, "owner-1")
        execution_id = "exec-1"
        self.store.start_attempt(task.task_id, lease.lease_id, execution_id)

        needs = [
            {"kind": "code", "target": f"file_{i}.py", "motivo": f"obrigacao_{i}"}
            for i in range(4)
        ]
        outcomes = [
            {
                "objective_id": "obj-1",
                "task_id": task.task_id,
                "state": "partial",
                "unmet_needs": needs,
            }
        ]

        # Primeira chamada
        result1 = C.plan_continuations(self.store, outcomes, input_versions=inputs)
        cont_task_id = result1["criadas"][0]

        # Simular tentativa de continuacao
        cont_task = self.store.get(cont_task_id)
        lease3 = self.store.acquire_lease(cont_task.task_id, "owner-1")
        self.store.start_attempt(cont_task.task_id, lease3.lease_id, "exec-cont-1")
        self.store.record_result(
            cont_task.task_id,
            lease3.lease_id,
            "exec-cont-1",
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result={"objective_id": "obj-1", "state": "partial"},
            termination_reason="completed",
        )

        outcomes_round2 = [
            {
                "objective_id": "obj-1",
                "task_id": cont_task.task_id,
                "state": "partial",
                "unmet_needs": needs,
            }
        ]

        # Configurar max_rounds = 1 (para que round 2 seja recusada)
        T.set_max_continuation_rounds(self.store, 1)
        result3 = C.plan_continuations(self.store, outcomes_round2, input_versions=inputs)

        # Aceite: deve recusar round 2
        self.assertEqual(
            len(result3["criadas"]),
            0,
            "Round 2 com max_rounds=1 deve recusar criacao"
        )
        self.assertGreater(
            len(result3["recusadas"]),
            0,
            "Deve haver recusadas"
        )
        self.assertIn(
            "excede",
            result3["recusadas"][0]["motivo"],
            "Motivo deve mencionar excesso de rounds"
        )

    def test_plan_continuations_refuses_empty_needs(self):
        """plan_continuations recusa partial com unmet_needs vazio."""
        objective = {"objective_id": "obj-1", "task_kind": "investigation"}
        inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        task = self.store.create_task(T.TaskKind.INVESTIGATION, objective, inputs)

        result = C.plan_continuations(
            self.store,
            [
                {
                    "objective_id": "obj-1",
                    "task_id": task.task_id,
                    "state": "partial",
                    "unmet_needs": [],
                }
            ],
            input_versions=inputs,
        )

        # Aceite: deve recusar
        self.assertEqual(
            len(result["criadas"]),
            0,
            "partial com unmet_needs vazio deve ser recusada"
        )
        self.assertGreater(
            len(result["recusadas"]),
            0,
            "Deve haver recusadas"
        )

    def test_plan_continuations_ignores_complete_and_blocked(self):
        """complete/blocked nunca geram continuacao."""
        objective1 = {"objective_id": "obj-2", "task_kind": "investigation"}
        objective2 = {"objective_id": "obj-3", "task_kind": "investigation"}
        inputs = {"snapshot_id": "snap-1", "source_version_ids": []}

        task1 = self.store.create_task(T.TaskKind.INVESTIGATION, objective1, inputs)
        task2 = self.store.create_task(T.TaskKind.INVESTIGATION, objective2, inputs)

        needs = [
            {"kind": "code", "target": f"file_{i}.py", "motivo": f"obrigacao_{i}"}
            for i in range(4)
        ]

        result = C.plan_continuations(
            self.store,
            [
                {
                    "objective_id": "obj-2",
                    "task_id": task1.task_id,
                    "state": "complete",
                    "unmet_needs": needs,
                },
                {
                    "objective_id": "obj-3",
                    "task_id": task2.task_id,
                    "state": "blocked",
                    "unmet_needs": needs,
                },
            ],
            input_versions=inputs,
        )

        # Aceite: nao deve criar nem recusar (estados complete/blocked sao ignorados)
        self.assertEqual(
            result,
            {"criadas": [], "recusadas": []},
            "complete/blocked deve retornar resultado vazio"
        )


if __name__ == "__main__":
    unittest.main()
