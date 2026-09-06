"""Testes de integração (alvo F): plan → TaskStore → run → persistência."""

import os
import tempfile
import unittest

from runtime import coordinator as C
from runtime import tasks as T
from runtime.executors.local_thread import LocalThreadExecutor


class IntegrationTest(unittest.TestCase):
    """Integração: planejar → executar → persistir."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "runtime.db")
        self.store = T.TaskStore.open(self.db_path)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_plan_and_execute_with_local_executor(self):
        """plan_from_objectives → run(LocalThreadExecutor)."""
        objectives = [
            {"goal": "analyze", "objective_id": "obj1"},
        ]

        plan = C.plan_from_objectives(
            store=self.store,
            objectives=objectives,
            snapshot_id="snap_123",
        )

        self.assertGreater(len(plan), 0)

        # Executor com tarefas locais
        registry = {
            "investigation": lambda obj, ce: {"status": "done"},
        }
        executor = LocalThreadExecutor(registry=registry, max_workers=1)

        try:
            # Executar com coordinator.run
            report = C.run(
                store=self.store,
                executor=executor,
            )

            self.assertIsNotNone(report)
        finally:
            executor.shutdown(wait=True)

    def test_reopen_db_reuses_results(self):
        """Reinício: novo TaskStore no mesmo db reutiliza resultados."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={"goal": "test"},
            input_versions={"v": "1"},
        )

        lease = self.store.acquire_lease(task.task_id, owner="w")
        output = {"computed": True}

        self.store.record_result(
            task_id=task.task_id,
            lease_id=lease.lease_id,
            execution_id="exec_1",
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result=output,
        )

        self.store.close()

        # Reabrir
        store2 = T.TaskStore.open(self.db_path)

        identity = task.effect_identity
        reused = store2.reuse_result(identity)
        self.assertEqual(reused, output)

        store2.close()

    def test_persisted_results_survive_restart(self):
        """Resultado persistido sobrevive reinício."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={"goal": "test"},
            input_versions={},
        )

        lease = self.store.acquire_lease(task.task_id, owner="w")
        result_output = {"status": "success", "data": [1, 2, 3]}

        self.store.record_result(
            task_id=task.task_id,
            lease_id=lease.lease_id,
            execution_id="exec_1",
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result=result_output,
        )

        task_before = self.store.get(task.task_id)
        self.assertEqual(task_before.state, T.TaskState.DONE)
        self.assertEqual(task_before.result, result_output)

        self.store.close()

        # Reabrir
        store2 = T.TaskStore.open(self.db_path)

        task_after = store2.get(task.task_id)
        self.assertEqual(task_after.state, T.TaskState.DONE)
        self.assertEqual(task_after.result, result_output)

        store2.close()

    def test_dependency_chain_execution(self):
        """Cadeia: A → B → C."""
        task_a = self.store.create_task(
            kind=T.TaskKind.DISCOVERY,
            objective={"step": "a"},
            input_versions={},
        )

        task_b = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={"step": "b"},
            input_versions={},
            depends_on=(task_a.task_id,),
        )

        task_c = self.store.create_task(
            kind=T.TaskKind.VERIFICATION,
            objective={"step": "c"},
            input_versions={},
            depends_on=(task_b.task_id,),
        )

        # Completar A
        lease_a = self.store.acquire_lease(task_a.task_id, owner="w")
        self.store.record_result(
            task_id=task_a.task_id,
            lease_id=lease_a.lease_id,
            execution_id="exec_a",
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result={"a": "done"},
        )

        # B agora está ready
        self.store.refresh_states()
        ready = self.store.ready_tasks()
        ready_ids = [t.task_id for t in ready]
        self.assertIn(task_b.task_id, ready_ids)

        # Completar B
        lease_b = self.store.acquire_lease(task_b.task_id, owner="w")
        self.store.record_result(
            task_id=task_b.task_id,
            lease_id=lease_b.lease_id,
            execution_id="exec_b",
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result={"b": "done"},
        )

        # C agora está ready
        self.store.refresh_states()
        ready = self.store.ready_tasks()
        ready_ids = [t.task_id for t in ready]
        self.assertIn(task_c.task_id, ready_ids)


if __name__ == "__main__":
    unittest.main()
