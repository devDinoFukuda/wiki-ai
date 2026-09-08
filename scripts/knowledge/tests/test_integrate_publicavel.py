"""A acumulação (§7.1/§7.2) não pode esvaziar o plano de publicação.

Guarda de regressão para a suspeita levantada na revisão da onda 12: que
`integrate(accumulated_state=...)` + `collect_results(latest_only=False)` +
`_seed_with_accumulated` pudessem semear o contrato de um jeito que rebaixasse
o `analysis_state`/a suficiência das unidades a ponto de
`publishing.planner.plan` não produzir documento algum ("plano não produziu
nenhum documento publicável").

O que este teste fixa, com o pipeline REAL (snapshot -> extração -> integrate
-> planner, sem `wk/cli.py` no caminho):

- a cadeia A+B de UM objetivo, dobrada sobre o estado acumulado, grava uma
  revisão cujo plano tem `documents > 0` — a acumulação SOMA conteúdo, e
  conteúdo somado nunca reduz o que é publicável;
- o plano da cadeia A+B tem pelo menos tantos documentos quanto o plano de A
  sozinha: se um dia a semeadura passar a sobrescrever/limpar campo apurado, a
  contagem cai aqui, no módulo que causa o problema, e não três camadas acima
  na mensagem genérica da CLI.

`publishing.planner` entra apenas como LEITOR (nada de `scripts/publishing/**`
é editado por causa deste teste): é o consumidor real do que `integrate`
grava, e é ele quem decide o que é publicável.
"""

import os
import shutil
import tempfile
import unittest

from analysis.extractors.base import SourceFile
from analysis.extractors.registry import default_registry
from analysis.investigation import CONTRACT_FIELDS, CONTRACT_LABELS, ContractField
from analysis.snapshot import capture
from knowledge import integrate as I
from knowledge.repository import Repository
from publishing import planner
from runtime import tasks as rt_tasks

NAMESPACE = "code/repro-publicavel"

CODIGO = '''\
def aprovar(total):
    """Aprova o pedido quando o total respeita o limite."""
    if total > 1000:
        return {"status": 409, "erro": "limite excedido"}
    return {"status": 200}
'''


def _extraction_of(snapshot):
    registry = default_registry()
    files = []
    for entry in snapshot.files:
        with open(os.path.join(snapshot.repo, entry.path), "r", encoding="utf-8") as fh:
            files.append(SourceFile(path=entry.path, content=fh.read()))
    return registry.extract_all(files)


def _objective_dict():
    return {
        "objective_id": "obj-1",
        "kind": "capability",
        "capability_id": "cap-1",
        "name": "Aprovacao de pedido",
        "contract": {
            n: ContractField(name=n, label=CONTRACT_LABELS[n]).to_dict() for n in CONTRACT_FIELDS
        },
        "reading_needs": [],
        "state": "partial",
    }


class PlanoPublicavelAposAcumulacaoTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="integra-publicavel-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.repo_dir = os.path.join(self.tmpdir, "repo")
        os.makedirs(self.repo_dir, exist_ok=True)
        with open(os.path.join(self.repo_dir, "app.py"), "w", encoding="utf-8") as fh:
            fh.write(CODIGO)
        self.snapshot = capture(self.repo_dir)
        self.extraction = _extraction_of(self.snapshot)
        self.versions = {"snapshot_id": self.snapshot.snapshot_id, "source_version_ids": []}

    def _result(self, task_id, contract, created_at):
        return I.AcceptedResult(
            task_id=task_id,
            kind="investigation",
            objective_id="obj-1",
            capability_id="cap-1",
            execution_id=f"exec-{task_id}",
            input_versions=self.versions,
            input_versions_hash=rt_tasks.input_versions_hash(self.versions),
            output={
                "objective_id": "obj-1",
                "capability_id": "cap-1",
                "contract": contract,
            },
            objective_payload={"objective_id": "obj-1", "capability_id": "cap-1"},
            asserted_by="llm:worker",
            created_at=created_at,
        )

    def _plan_of(self, nome_db, results, accumulated_state=None):
        """integrate(...) real -> planner.plan(...) real; devolve o plano."""
        repo = Repository.open(os.path.join(self.tmpdir, nome_db))
        try:
            report = I.integrate(
                repo,
                None,
                self.snapshot,
                self.extraction,
                NAMESPACE,
                objectives=[_objective_dict()],
                results=results,
                accumulated_state=accumulated_state,
            )
            self.assertTrue(
                report.revisao,
                f"integração não gravou revisão; descartados={report.descartados!r}",
            )
            return planner.plan(repo, report.revisao, namespace=NAMESPACE), report
        finally:
            repo.close()

    def _rodada_a(self):
        return self._result(
            "t-a",
            {
                "decisoes": {
                    "status": "filled",
                    "content": "Pedido com total acima de 1000 é recusado com 409.",
                    "evidence_refs": [{"path": "app.py", "lines": "3-4"}],
                },
            },
            "2026-09-07T00:00:00Z",
        )

    def _rodada_b(self):
        return self._result(
            "t-b",
            {
                "falhas": {
                    "status": "filled",
                    "content": "Total acima do limite devolve erro 'limite excedido'.",
                    "evidence_refs": [{"path": "app.py", "lines": "4"}],
                },
            },
            "2026-09-07T01:00:00Z",
        )

    # -- regressão ---------------------------------------------------------

    def test_cadeia_acumulada_produz_documento_publicavel(self):
        plano, _ = self._plan_of("k-ab.db", [self._rodada_a(), self._rodada_b()])
        self.assertGreater(
            len(plano.documents),
            0,
            "cadeia A+B integrada não produziu documento publicável; "
            f"skipped={[ (s.target_id, s.reason) for s in plano.skipped ]!r}",
        )

    def test_acumular_nunca_reduz_o_publicavel_de_uma_rodada_so(self):
        plano_a, _ = self._plan_of("k-a.db", [self._rodada_a()])
        plano_ab, _ = self._plan_of("k-ab2.db", [self._rodada_a(), self._rodada_b()])
        self.assertGreaterEqual(
            len(plano_ab.documents),
            len(plano_a.documents),
            "dobrar a rodada B sobre a A reduziu o que é publicável — a acumulação "
            "está apagando o que a rodada anterior apurou",
        )

    def test_estado_acumulado_externo_tambem_mantem_o_plano_publicavel(self):
        """`accumulated_state` vindo de fora (o `contract` consolidado que
        `runtime.state` carrega entre invocações) é SEMEADO no objetivo; se a
        semeadura corrompesse o contrato, o plano ficaria vazio aqui."""
        estado = {
            "obj-1": {
                "contract": {
                    "decisoes": {
                        "status": "filled",
                        "content": "Pedido acima de 1000 é recusado (apurado em rodada anterior).",
                    }
                }
            }
        }
        plano, report = self._plan_of("k-seed.db", [self._rodada_b()], accumulated_state=estado)
        self.assertGreater(
            len(plano.documents),
            0,
            "com estado acumulado externo o plano ficou sem documento publicável; "
            f"skipped={[ (s.target_id, s.reason) for s in plano.skipped ]!r}",
        )
        # o contrato consolidado no outcome carrega os DOIS campos (o semeado
        # e o da rodada corrente) — é ele que a continuação envia ao worker.
        contratos = {
            campo
            for objetivo in report.objetivos
            for campo in (objetivo.contract_state or {})
        }
        self.assertIn("decisoes", contratos)
        self.assertIn("falhas", contratos)


if __name__ == "__main__":
    unittest.main()
