"""Testes permanentes de audit finding BLOQUEANTE #2 runtime (Onda11-T2a, 3a auditoria).

Contrato:
- create_continuation_tasks grava objetivo RICO (needs com evidence, contract_state,
  parent_result, capability_context) sem mudar effect_identity/idempotencia.
- context.build_package(objetivo continuation=True) monta partes REAIS a partir de
  needs[].evidence; contract_state/parent_result entram no payload (contam no teto
  F05); need sem localizador vira diagnostico (unavailable_reason), nunca 0 parts
  silencioso.
- LocalThreadExecutor/ClaudeCliExecutor declaram capabilities()["deepening"].
- coordinator.plan_continuations(engine_capabilities=...) recusa sem criar tarefa
  nem avancar rodada quando deepening e False.
"""

import os
import tempfile
import unittest

from runtime import context as CTX
from runtime import coordinator as C
from runtime import tasks as T
from runtime.executors.local_thread import LocalThreadExecutor


def _fake_resolver(path, start, end):
    return {
        "snippet": f"# codigo real {path}:{start}-{end}\ndef f():\n    return {start}\n",
        "locator": {"path": path, "commit": "deadbeef"},
    }


class ContinuationRichObjectiveTest(unittest.TestCase):
    """create_continuation_tasks grava needs com evidence + contract_state + parent_result."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onda11t2a-")
        self.db_path = os.path.join(self.tmpdir, "runtime.db")
        self.store = T.TaskStore.open(self.db_path)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_parent(self):
        objective = {"objective_id": "obj-1", "task_kind": "investigation"}
        inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        task = self.store.create_task(T.TaskKind.INVESTIGATION, objective, inputs)
        lease = self.store.acquire_lease(task.task_id, "owner-1")
        self.store.start_attempt(task.task_id, lease.lease_id, "exec-1")
        parent_result = {
            "objective_id": "obj-1",
            "state": "partial",
            "facts": [{"id": "f1"}],
            "contract": {
                "identidade": {
                    "status": "filled",
                    "content": "x",
                    "evidence_refs": [
                        {"path": "scripts/runtime/tasks.py", "line_start": 1, "line_end": 5}
                    ],
                }
            },
        }
        self.store.record_result(
            task.task_id, lease.lease_id, "exec-1",
            state=T.TaskState.DONE, outcome=T.AttemptOutcome.SUCCEEDED,
            result=parent_result, termination_reason="completed",
        )
        return task, inputs, parent_result

    def test_needs_with_evidence_produce_real_parts(self):
        """4 needs evidenciados -> build_package produz >=1 part com codigo real por need."""
        parent, inputs, parent_result = self._make_parent()
        needs = [
            {
                "kind": "code",
                "target": f"scripts/runtime/tasks.py#need{i}",
                "motivo": f"confirmar comportamento {i}",
                "evidence": [
                    {"path": "scripts/runtime/tasks.py", "line_start": 10 + i, "line_end": 12 + i}
                ],
            }
            for i in range(4)
        ]
        task_ids = T.create_continuation_tasks(
            self.store,
            parent_task_id=parent.task_id,
            objective_id="obj-1",
            needs=needs,
            input_versions=inputs,
            round_no=1,
            contract_state={"identidade": {"status": "filled"}},
            parent_result=parent_result,
            capability_context={"symbols": ["s1"], "modules": ["scripts.runtime.tasks"]},
        )
        self.assertEqual(len(task_ids), 1)
        cont_task = self.store.get(task_ids[0])

        # objetivo gravado carrega os campos novos
        self.assertEqual(cont_task.objective["contract_state"], {"identidade": {"status": "filled"}})
        self.assertIn("filled_fields", cont_task.objective["parent_result"])
        self.assertTrue(cont_task.objective["parent_result"]["filled_fields"])
        self.assertTrue(cont_task.objective["parent_result"]["evidence_refs"])
        self.assertEqual(cont_task.objective["capability_context"]["symbols"], ["s1"])

        budget = CTX.Budget(max_bytes=200_000, max_tokens=100_000)
        package = CTX.build_package(cont_task.objective, budget, _fake_resolver)

        # NUNCA 0 parts quando needs tem evidencia resolvivel
        self.assertGreaterEqual(len(package.parts), 4)
        for part in package.parts:
            self.assertIn("def f()", part.snippet, "part deve conter codigo real")

        # contract_state/parent_result presentes no payload (refs[0] = envelope)
        envelope = package.refs[0]
        self.assertEqual(envelope["contract_state"], {"identidade": {"status": "filled"}})
        self.assertTrue(envelope["parent_result"]["filled_fields"])

    def test_need_without_locator_becomes_diagnostic_not_silence(self):
        """need sem evidence -> ref com unavailable_reason, pacote nao fica silencioso."""
        parent, inputs, parent_result = self._make_parent()
        needs = [
            {"kind": "code", "target": "sem_localizador", "motivo": "sem evidence"},
        ]
        task_ids = T.create_continuation_tasks(
            self.store,
            parent_task_id=parent.task_id,
            objective_id="obj-1",
            needs=needs,
            input_versions=inputs,
            round_no=1,
        )
        cont_task = self.store.get(task_ids[0])
        budget = CTX.Budget(max_bytes=200_000, max_tokens=100_000)
        package = CTX.build_package(cont_task.objective, budget, _fake_resolver)

        self.assertEqual(len(package.parts), 0)
        diag_refs = [r for r in package.refs if r.get("unavailable_reason")]
        self.assertEqual(len(diag_refs), 1)
        self.assertIn("sem trecho citavel", diag_refs[0]["unavailable_reason"])

    def test_extra_content_never_changes_effect_identity(self):
        """contract_state/parent_result diferentes NAO duplicam a tarefa (§7.2)."""
        parent, inputs, parent_result = self._make_parent()
        needs = [
            {
                "kind": "code",
                "target": "t1",
                "motivo": "m1",
                "evidence": [{"path": "f.py", "line_start": 1, "line_end": 2}],
            }
        ]
        first = T.create_continuation_tasks(
            self.store,
            parent_task_id=parent.task_id,
            objective_id="obj-1",
            needs=needs,
            input_versions=inputs,
            round_no=1,
            contract_state={"a": 1},
            parent_result={"x": 1},
        )
        second = T.create_continuation_tasks(
            self.store,
            parent_task_id=parent.task_id,
            objective_id="obj-1",
            needs=needs,
            input_versions=inputs,
            round_no=1,
            contract_state={"a": "COMPLETAMENTE DIFERENTE"},
            parent_result={"x": "outro valor"},
            capability_context={"entry_keys": ["novo"]},
        )
        self.assertEqual(first, second, "conteudo extra nao pode mudar effect_identity")
        self.assertEqual(len(self.store.all_tasks()), 2)  # mae + 1 continuacao (nao duplicada)

    def test_start_line_end_line_alias_normalized(self):
        """`start_line`/`end_line` (nome alternativo) e normalizado para `line_start`/`line_end`."""
        parent, inputs, _ = self._make_parent()
        needs = [
            {
                "kind": "code",
                "target": "t1",
                "motivo": "m1",
                "evidence": [{"path": "f.py", "start_line": 3, "end_line": 4}],
            }
        ]
        task_ids = T.create_continuation_tasks(
            self.store,
            parent_task_id=parent.task_id,
            objective_id="obj-1",
            needs=needs,
            input_versions=inputs,
            round_no=1,
        )
        cont_task = self.store.get(task_ids[0])
        ev = cont_task.objective["needs"][0]["evidence"][0]
        self.assertEqual(ev["line_start"], 3)
        self.assertEqual(ev["line_end"], 4)

        budget = CTX.Budget(max_bytes=200_000, max_tokens=100_000)
        package = CTX.build_package(cont_task.objective, budget, _fake_resolver)
        self.assertEqual(len(package.parts), 1)


class EngineDeepeningCapabilityTest(unittest.TestCase):
    """capabilities()["deepening"] e coordinator.plan_continuations(engine_capabilities=)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onda11t2a-")
        self.db_path = os.path.join(self.tmpdir, "runtime.db")
        self.store = T.TaskStore.open(self.db_path)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_local_thread_executor_deepening_false(self):
        executor = LocalThreadExecutor(registry={"investigation": lambda **kw: {}})
        caps = executor.capabilities()
        self.assertIn("deepening", caps)
        self.assertFalse(caps["deepening"])

    def test_plan_continuations_refuses_without_deepening_and_round_not_consumed(self):
        objective = {"objective_id": "obj-1", "task_kind": "investigation"}
        inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        task = self.store.create_task(T.TaskKind.INVESTIGATION, objective, inputs)
        lease = self.store.acquire_lease(task.task_id, "owner-1")
        self.store.start_attempt(task.task_id, lease.lease_id, "exec-1")

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

        rounds_before = T.continuation_rounds(self.store, "obj-1")
        total_tasks_before = len(self.store.all_tasks())

        result = C.plan_continuations(
            self.store,
            outcomes,
            input_versions=inputs,
            engine_capabilities={"dispatch": True, "deepening": False},
        )

        self.assertEqual(result["criadas"], [])
        self.assertEqual(len(result["recusadas"]), 1)
        self.assertIn("deepening", result["recusadas"][0]["motivo"])
        self.assertEqual(T.continuation_rounds(self.store, "obj-1"), rounds_before)
        self.assertEqual(len(self.store.all_tasks()), total_tasks_before)

    def test_plan_continuations_without_engine_capabilities_keeps_old_behavior(self):
        """engine_capabilities=None (default) preserva comportamento anterior."""
        objective = {"objective_id": "obj-1", "task_kind": "investigation"}
        inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        task = self.store.create_task(T.TaskKind.INVESTIGATION, objective, inputs)
        lease = self.store.acquire_lease(task.task_id, "owner-1")
        self.store.start_attempt(task.task_id, lease.lease_id, "exec-1")

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
        result = C.plan_continuations(self.store, outcomes, input_versions=inputs)
        self.assertEqual(len(result["criadas"]), 1)

    def test_plan_continuations_accepts_when_deepening_true(self):
        objective = {"objective_id": "obj-1", "task_kind": "investigation"}
        inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        task = self.store.create_task(T.TaskKind.INVESTIGATION, objective, inputs)
        lease = self.store.acquire_lease(task.task_id, "owner-1")
        self.store.start_attempt(task.task_id, lease.lease_id, "exec-1")

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
        result = C.plan_continuations(
            self.store,
            outcomes,
            input_versions=inputs,
            engine_capabilities={"dispatch": True, "deepening": True},
        )
        self.assertEqual(len(result["criadas"]), 1)


if __name__ == "__main__":
    unittest.main()
