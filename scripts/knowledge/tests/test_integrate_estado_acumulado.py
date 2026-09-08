"""Integração a partir do ESTADO ACUMULADO, não da última tarefa (§7.1, T06).

Antes: `integrate()` coletava com `latest_only=True` e montava o objetivo a
partir do índice PRISTINO do plano. Consequência — a rodada A preenchia o campo
`decisoes`, a rodada B (que só falava de `decisoes`) chegava, e a integração
gravava um objetivo em que `decisoes` estava vazia de novo. Progresso reconstruído
"a partir da última tarefa" é exatamente o que o §7.1 proíbe.

Agora a cadeia inteira do objetivo é dobrada em ordem cronológica sobre o estado
acumulado, e o `contract_state` consolidado sai no outcome — que é o que a
rodada seguinte manda ao worker (§7.2, "contrato acumulado com conteúdo").
"""

import os
import shutil
import tempfile
import unittest

from analysis.extractors.base import SourceFile
from analysis.extractors.registry import default_registry
from analysis.snapshot import capture
from knowledge import integrate as I
from knowledge.repository import Repository
from runtime import tasks as rt_tasks

CODIGO = '''\
def aprovar(total):
    if total > 1000:
        return {"status": 409}
    return {"status": 200}
'''


def _extraction_of(snapshot):
    registry = default_registry()
    files = []
    for entry in snapshot.files:
        with open(os.path.join(snapshot.repo, entry.path), "r", encoding="utf-8") as fh:
            files.append(SourceFile(path=entry.path, content=fh.read()))
    return registry.extract_all(files)


def _objective_dict(objective_id="obj-1", capability_id="cap-1"):
    from analysis.investigation import CONTRACT_FIELDS, CONTRACT_LABELS, ContractField

    return {
        "objective_id": objective_id,
        "kind": "capability",
        "capability_id": capability_id,
        "name": "Aprovacao",
        "contract": {
            n: ContractField(name=n, label=CONTRACT_LABELS[n]).to_dict() for n in CONTRACT_FIELDS
        },
        "reading_needs": [],
        "state": "partial",
    }


class EstadoAcumuladoTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="estado-acum-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.repo_dir = os.path.join(self.tmpdir, "repo")
        os.makedirs(self.repo_dir, exist_ok=True)
        with open(os.path.join(self.repo_dir, "app.py"), "w", encoding="utf-8") as fh:
            fh.write(CODIGO)
        self.snapshot = capture(self.repo_dir)
        self.extraction = _extraction_of(self.snapshot)
        self.versions = {"snapshot_id": self.snapshot.snapshot_id, "source_version_ids": []}

    def _result(self, task_id, output, created_at):
        return I.AcceptedResult(
            task_id=task_id,
            kind="investigation",
            objective_id="obj-1",
            capability_id="cap-1",
            execution_id=f"exec-{task_id}",
            input_versions=self.versions,
            input_versions_hash=rt_tasks.input_versions_hash(self.versions),
            output=output,
            objective_payload={"objective_id": "obj-1", "capability_id": "cap-1"},
            asserted_by="llm:worker",
            created_at=created_at,
        )

    def _integrate(self, results, accumulated_state=None):
        repo = Repository.open(os.path.join(self.tmpdir, f"k-{len(results)}-{id(results)}.db"))
        try:
            return I.integrate(
                repo,
                None,
                self.snapshot,
                self.extraction,
                "code/repro",
                objectives=[_objective_dict()],
                results=results,
                accumulated_state=accumulated_state,
            ).to_dict()
        finally:
            repo.close()

    # -- T06 ---------------------------------------------------------------

    def test_t06_rodada_a_depois_de_b_produz_a_mais_b(self):
        a = self._result(
            "t-a",
            {
                "objective_id": "obj-1",
                "capability_id": "cap-1",
                "contract": {"decisoes": {"status": "filled", "content": "RN-900 limite de 1000"}},
            },
            "2026-09-07T00:00:00Z",
        )
        b = self._result(
            "t-b",
            {
                "objective_id": "obj-1",
                "capability_id": "cap-1",
                "contract": {"falhas": {"status": "filled", "content": "bloqueia pedido grande"}},
            },
            "2026-09-07T01:00:00Z",
        )
        relatorio = self._integrate([a, b])
        objetivo = relatorio["objetivos"][0]
        contrato = objetivo["contract_state"]
        self.assertIn("decisoes", contrato, "a rodada A não pode sumir quando B chega")
        self.assertIn("falhas", contrato)
        self.assertEqual(contrato["decisoes"]["content"], "RN-900 limite de 1000")
        self.assertEqual(contrato["falhas"]["content"], "bloqueia pedido grande")
        # A cadeia inteira fica rastreável.
        self.assertEqual(objetivo["chain_task_ids"], ["t-a", "t-b"])
        self.assertEqual(objetivo["task_id"], "t-b", "quem assina é a rodada corrente")

    def test_ordem_da_cadeia_e_cronologica_nao_de_varredura(self):
        a = self._result(
            "t-zzz",
            {"objective_id": "obj-1", "capability_id": "cap-1",
             "contract": {"decisoes": {"status": "filled", "content": "primeira"}}},
            "2026-09-07T00:00:00Z",
        )
        b = self._result(
            "t-aaa",
            {"objective_id": "obj-1", "capability_id": "cap-1",
             "contract": {"decisoes": {"status": "filled", "content": "segunda"}}},
            "2026-09-07T02:00:00Z",
        )
        agrupado = I.results_by_objective([b, a])
        self.assertEqual([r.task_id for r in agrupado["obj-1"]], ["t-zzz", "t-aaa"])
        contrato = self._integrate([b, a])["objetivos"][0]["contract_state"]
        self.assertEqual(contrato["decisoes"]["content"], "segunda", "a rodada mais nova prevalece")

    # -- estado consolidado externo ----------------------------------------

    def test_estado_acumulado_semeia_o_contrato_do_indice(self):
        """O que o `runtime.state` já apurou entra mesmo sem estar no resultado."""
        b = self._result(
            "t-b",
            {"objective_id": "obj-1", "capability_id": "cap-1",
             "contract": {"falhas": {"status": "filled", "content": "so o impacto"}}},
            "2026-09-07T01:00:00Z",
        )
        relatorio = self._integrate(
            [b],
            accumulated_state={
                "obj-1": {"contract": {"decisoes": {"content": "apurada numa rodada anterior"}}}
            },
        )
        contrato = relatorio["objetivos"][0]["contract_state"]
        self.assertEqual(contrato["decisoes"]["content"], "apurada numa rodada anterior")
        self.assertEqual(contrato["falhas"]["content"], "so o impacto")

    def test_estado_acumulado_nao_sobrescreve_conteudo_do_resultado(self):
        b = self._result(
            "t-b",
            {"objective_id": "obj-1", "capability_id": "cap-1",
             "contract": {"decisoes": {"status": "filled", "content": "valor da rodada corrente"}}},
            "2026-09-07T01:00:00Z",
        )
        relatorio = self._integrate(
            [b], accumulated_state={"obj-1": {"contract": {"decisoes": {"content": "valor antigo"}}}}
        )
        self.assertEqual(
            relatorio["objetivos"][0]["contract_state"]["decisoes"]["content"],
            "valor da rodada corrente",
        )

    def test_contrato_vazio_nao_viaja_no_outcome(self):
        b = self._result(
            "t-b",
            {"objective_id": "obj-1", "capability_id": "cap-1",
             "contract": {"decisoes": {"status": "filled", "content": "conteudo"}}},
            "2026-09-07T01:00:00Z",
        )
        contrato = self._integrate([b])["objetivos"][0]["contract_state"]
        self.assertIn("decisoes", contrato)
        for nome, campo in contrato.items():
            self.assertTrue(str(campo.get("content") or "").strip(), nome)


class CollectResultsChainTest(unittest.TestCase):
    """`collect_results` deixa de ser a fonte de verdade "só o mais recente"."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="collect-chain-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = rt_tasks.TaskStore.open(os.path.join(self.root, "runtime.db"))
        self.addCleanup(self.store.close)
        self.versions = {"snapshot_id": "snap-1", "source_version_ids": []}

    def _tarefa_concluida(self, marca, conteudo):
        task = self.store.create_task(
            rt_tasks.TaskKind.INVESTIGATION,
            {"objective_id": "obj-1", "marca": marca},
            self.versions,
        )
        lease = self.store.acquire_lease(task.task_id, "owner")
        self.store.start_attempt(task.task_id, lease.lease_id, f"exec-{marca}")
        self.store.record_result(
            task.task_id,
            lease.lease_id,
            f"exec-{marca}",
            state=rt_tasks.TaskState.DONE,
            outcome=rt_tasks.AttemptOutcome.SUCCEEDED,
            result={"objective_id": "obj-1", "contract": {"decisoes": {"content": conteudo}}},
            termination_reason="completed",
        )
        return task.task_id

    def test_cadeia_inteira_disponivel_com_latest_only_false(self):
        self._tarefa_concluida("a", "primeira")
        self._tarefa_concluida("b", "segunda")

        somente_ultimo = I.collect_results(self.store, ["obj-1"])
        self.assertEqual(len(somente_ultimo), 1, "latest_only continua sendo inspeção")

        cadeia = I.collect_results(self.store, ["obj-1"], latest_only=False)
        self.assertEqual(len(cadeia), 2, "a integração precisa ver a cadeia inteira")
        agrupado = I.results_by_objective(cadeia)
        self.assertEqual(len(agrupado["obj-1"]), 2)


if __name__ == "__main__":
    unittest.main()
