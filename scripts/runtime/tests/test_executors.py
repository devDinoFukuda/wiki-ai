"""Testes para executors/ (alvo E) com assinaturas reais."""

import time
import unittest

from runtime.executors.base import PolicyRejectedError, UnknownExecutionError
from runtime.executors.local_thread import LocalThreadExecutor


class LocalThreadExecutorTest(unittest.TestCase):
    """Testes para LocalThreadExecutor."""

    def setUp(self):
        self.registry = {
            "simple": lambda obj, ce: {"echo": obj.get("msg", "")},
            "slow": lambda obj, ce: (time.sleep(0.05), {"done": True})[1],
        }
        self.executor = LocalThreadExecutor(registry=self.registry, max_workers=3)

    def tearDown(self):
        self.executor.shutdown(wait=True)

    def test_capabilities(self):
        """capabilities() retorna dispatch=True."""
        caps = self.executor.capabilities()
        self.assertTrue(caps.get("dispatch"))
        self.assertEqual(caps.get("concurrency"), 3)

    def test_submit_returns_execution_id(self):
        """submit() retorna execution_id."""
        exec_id = self.executor.submit(
            task_id="t1",
            objective={"kind": "simple", "msg": "hello"},
            references=[],
            schema={},
        )

        self.assertTrue(exec_id.startswith("local"))

    def test_three_concurrent_executions(self):
        """3 execuções concorrentes em paralelo."""
        exec_ids = []
        start = time.time()

        for i in range(3):
            exec_id = self.executor.submit(
                task_id=f"t{i}",
                objective={"kind": "slow"},
                references=[],
                schema={},
            )
            exec_ids.append(exec_id)

        for exec_id in exec_ids:
            result = self.executor.result(exec_id)
            self.assertIsNotNone(result)

        elapsed = time.time() - start
        self.assertLess(elapsed, 0.3, "Não rodou em paralelo")

    def test_unknown_execution_id_raises_error(self):
        """result() com id desconhecido → UnknownExecutionError."""
        fake_id = "local_unknown_9999"

        with self.assertRaises(UnknownExecutionError):
            self.executor.result(fake_id)

    def test_policy_write_paths_rejected(self):
        """policy com write_paths → PolicyRejectedError."""
        policy = {"write_paths": ["/some/path"]}

        with self.assertRaises(PolicyRejectedError):
            self.executor.submit(
                task_id="t1",
                objective={"kind": "simple"},
                references=[],
                schema={},
                policy=policy,
            )

    def test_submit_status_result_flow(self):
        """Fluxo: submit → status → result."""
        exec_id = self.executor.submit(
            task_id="t1",
            objective={"kind": "simple", "msg": "test"},
            references=[],
            schema={},
        )

        status = self.executor.status(exec_id)
        self.assertIn("state", status)

        result = self.executor.result(exec_id)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
