"""Testes para tasks.py (alvo A) com assinaturas reais."""

import os
import sqlite3
import tempfile
import time
import unittest

from runtime import tasks as T


class TaskStoreTest(unittest.TestCase):
    """Testes para TaskStore com API real."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "runtime.db")
        self.store = T.TaskStore.open(self.db_path)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_task_basic(self):
        """Criar tarefa básica."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={"goal": "test"},
            input_versions={"v": "1"},
        )

        self.assertEqual(task.state, T.TaskState.PENDING)

    def test_deduplicates_by_effect_identity(self):
        """Deduplica por effect_identity (UNIQUE)."""
        task1 = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={"goal": "test"},
            input_versions={"v": "1"},
        )

        task2 = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={"goal": "test"},
            input_versions={"v": "1"},
        )

        self.assertEqual(task1.task_id, task2.task_id)

    def test_schema_version_mismatch(self):
        """SchemaVersionMismatch em versão divergente."""
        self.store.close()
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version, applied_at, note) VALUES (999, '2026-01-01T00:00:00', 'bad')")
        conn.commit()
        conn.close()

        with self.assertRaises(T.SchemaVersionMismatch):
            T.TaskStore.open(self.db_path)

    def test_lease_two_concurrent_one_fails(self):
        """2 aquisições concorrentes: 1 falha LeaseHeld."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={},
            input_versions={},
        )

        lease1 = self.store.acquire_lease(task.task_id, owner="w1")
        self.assertIsNotNone(lease1)

        with self.assertRaises(T.LeaseHeld):
            self.store.acquire_lease(task.task_id, owner="w2")

    def test_lease_expired_reacquired(self):
        """Lease expirado pode ser readquirido."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={},
            input_versions={},
        )

        lease1 = self.store.acquire_lease(task.task_id, owner="w1", ttl_seconds=0.1)
        time.sleep(0.2)

        lease2 = self.store.acquire_lease(task.task_id, owner="w2")
        self.assertNotEqual(lease1.lease_id, lease2.lease_id)

    def test_record_result_transaction(self):
        """record_result em transação."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={},
            input_versions={},
        )

        lease = self.store.acquire_lease(task.task_id, owner="w")

        updated = self.store.record_result(
            task_id=task.task_id,
            lease_id=lease.lease_id,
            execution_id="exec_1",
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result={"done": True},
        )

        self.assertEqual(updated.state, T.TaskState.DONE)

    def test_reuse_result(self):
        """Reutiliza resultado de tarefa DONE."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={"goal": "compute"},
            input_versions={"v": "1"},
        )

        lease = self.store.acquire_lease(task.task_id, owner="w")
        output = {"result": "computed"}

        self.store.record_result(
            task_id=task.task_id,
            lease_id=lease.lease_id,
            execution_id="exec_1",
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result=output,
        )

        reused = self.store.reuse_result(task.effect_identity)
        self.assertEqual(reused, output)

    def test_register_effect(self):
        """register_effect registra com task válida."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={},
            input_versions={},
        )

        effect_id = T.new_id("effect")
        self.store.register_effect(effect_id=effect_id, task_id=task.task_id)

        effects = self.store.pending_effects()
        self.assertEqual(len(effects), 1)

    def test_pending_effects_survive_reopen(self):
        """pending_effects sobrevive reabertura."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={},
            input_versions={},
        )

        effect_id = T.new_id("effect")
        self.store.register_effect(effect_id=effect_id, task_id=task.task_id)

        self.store.close()
        self.store = T.TaskStore.open(self.db_path)

        effects = self.store.pending_effects()
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]["effect_id"], effect_id)

    def test_dependency_chain_execution(self):
        """Cadeia de dependências: A → B → C."""
        task_a = self.store.create_task(
            kind=T.TaskKind.DISCOVERY,
            objective={},
            input_versions={},
        )

        task_b = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={},
            input_versions={},
            depends_on=(task_a.task_id,),
        )

        # Completar A
        lease_a = self.store.acquire_lease(task_a.task_id, owner="w")
        self.store.record_result(
            task_id=task_a.task_id,
            lease_id=lease_a.lease_id,
            execution_id="exec_1",
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result={},
        )

        # B agora completa
        lease_b = self.store.acquire_lease(task_b.task_id, owner="w")
        self.store.record_result(
            task_id=task_b.task_id,
            lease_id=lease_b.lease_id,
            execution_id="exec_2",
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result={},
        )

        # Verificar que ambas estão done
        task_a_done = self.store.get(task_a.task_id)
        task_b_done = self.store.get(task_b.task_id)
        self.assertEqual(task_a_done.state, T.TaskState.DONE)
        self.assertEqual(task_b_done.state, T.TaskState.DONE)


if __name__ == "__main__":
    unittest.main()
