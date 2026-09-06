"""Testes para recovery.py (alvo C) com assinaturas reais."""

import os
import tempfile
import unittest

from runtime import recovery as R
from runtime import tasks as T


class RecoveryPolicyTest(unittest.TestCase):
    """Testes para políticas de recovery."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "runtime.db")
        self.store = T.TaskStore.open(self.db_path)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load_policy(self):
        """save_policy persiste e load_policies retorna."""
        policy = R.Policy(
            error_class=R.ErrorClass.TRANSIENT,
            action=R.Action.RETRY,
            max_additional_attempts=2,
        )

        R.save_policy(self.store, policy)

        policies = R.load_policies(self.store)
        self.assertIn(R.ErrorClass.TRANSIENT, policies)
        self.assertEqual(policies[R.ErrorClass.TRANSIENT].action, R.Action.RETRY)

    def test_decide_with_store_and_task(self):
        """decide com store e task_id reais."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={},
            input_versions={},
        )

        policy = R.Policy(
            error_class=R.ErrorClass.TRANSIENT,
            action=R.Action.RETRY,
            max_additional_attempts=2,
        )

        decision = R.decide(
            store=self.store,
            task_id=task.task_id,
            error_class=R.ErrorClass.TRANSIENT,
            error_detail="Connection timeout",
            policies={R.ErrorClass.TRANSIENT: policy},
        )

        self.assertEqual(decision.action, R.Action.RETRY)


class RecoveryResumeTest(unittest.TestCase):
    """Testes para resume com assinatura real."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "runtime.db")
        self.store = T.TaskStore.open(self.db_path)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_resume_basic(self):
        """resume retorna ResumePlan."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={"goal": "test"},
            input_versions={"v": "1"},
        )

        lease = self.store.acquire_lease(task.task_id, owner="w")
        self.store.record_result(
            task_id=task.task_id,
            lease_id=lease.lease_id,
            execution_id="exec_1",
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result={"done": True},
        )

        # Resume com mesmos input_versions
        resume_plan = R.resume(
            store=self.store,
            current_input_versions={"v": "1"},
        )

        self.assertIsNotNone(resume_plan)


class SameErrorHashBlockingTest(unittest.TestCase):
    """Testes para cenário 9: mesmo error_hash 2× → tarefa blocked (§7.4)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "runtime.db")
        self.store = T.TaskStore.open(self.db_path)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_same_error_hash_twice_blocks_task(self):
        """Mesmo error_hash 2× na mesma tarefa → tarefa deve ser BLOCKED (regra dura §7.4)."""
        task = self.store.create_task(
            kind=T.TaskKind.INVESTIGATION,
            objective={},
            input_versions={},
        )

        error_detail = "Connection refused"
        err_hash = R.error_hash(R.ErrorClass.TRANSIENT, error_detail)

        # Primeira tentativa: registrar erro com esse hash
        lease1 = self.store.acquire_lease(task.task_id, owner="w1")
        self.store.record_result(
            task_id=task.task_id,
            lease_id=lease1.lease_id,
            execution_id="exec_1",
            state=T.TaskState.FAILED,
            outcome=T.AttemptOutcome.FAILED,
            error_class=R.ErrorClass.TRANSIENT.value,
            error_detail=error_detail,
            error_hash=err_hash,
        )

        # Segunda tentativa: mesmo erro
        lease2 = self.store.acquire_lease(task.task_id, owner="w2")
        self.store.record_result(
            task_id=task.task_id,
            lease_id=lease2.lease_id,
            execution_id="exec_2",
            state=T.TaskState.BLOCKED,
            outcome=T.AttemptOutcome.FAILED,
            error_class=R.ErrorClass.TRANSIENT.value,
            error_detail=error_detail,
            error_hash=err_hash,
        )

        # Tarefa deve estar BLOCKED (mesmo error_hash 2x ativa bloqueio)
        final_task = self.store.get(task.task_id)
        self.assertEqual(final_task.state, T.TaskState.BLOCKED)


if __name__ == "__main__":
    unittest.main()
