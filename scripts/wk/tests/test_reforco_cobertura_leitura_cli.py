"""Reforço pós-entrega (analysis/runtime/knowledge já landed durante a
tarefa): item 1 (`cmd_resume` — `PlanAccountingError` dedicado + `_apply_
cobertura_status`/`cobertura_leitura` + `cobertura` persistida), item 2
(cobertura de LEITURA: `summary.detail.cobertura_leitura` em analyze/update/
resume/status, `knowledge_status=complete` só quando `files_unread == []` E
todo objetivo `complete`), item 3 (`status` mostra `max_rounds`/`rounds_left`
como o runtime devolve — `None` quando ilimitado, nunca 3 imposto), item 4
(`summary.detail.cobertura` em `cmd_status` a partir do accounting
PERSISTIDO, sem recalcular o pipeline) e o ajuste do `next_action` de
`budget_exhausted` (`None` de `max_rounds` != `0`: só sugere `--max-rounds`
quando há teto NUMÉRICO).

Dono exclusivo: `scripts/wk/cli.py` (`_cobertura_leitura_from_report`,
`_target_looks_like_path`, `_apply_leitura_status`, `_pending_cobertura_
incompleta`, `_chain_next_action` ramo `budget_exhausted`, e a fiação em
`cmd_analyze`/`cmd_update`/`cmd_resume`/`cmd_status`) — nada em
`scripts/analysis/**`/`scripts/knowledge/**`/`scripts/runtime/**` editado.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from wk import cli


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


GO_MAIN = (
    "package main\n\n"
    'import "example.com/api/internal/orders"\n\n'
    "func main() {\n\tsvc := orders.NewService()\n\t_ = svc\n}\n"
)
GO_ORDERS = (
    "package orders\n\ntype Order struct {\n\tID string\n}\n\n"
    "type Service struct{}\n\nfunc NewService() *Service {\n\treturn &Service{}\n}\n"
)
GO_MOD = "module example.com/api\n\ngo 1.21\n"
PY_APP = "def add(a, b):\n    return a + b\n"


# ---------------------------------------------------------------------------
# Unidade: `_cobertura_leitura_from_report`
# ---------------------------------------------------------------------------


class CoberturaLeituraFromReportTests(unittest.TestCase):
    def test_arquivo_satisfeito_conta_como_lido(self):
        report = {
            "objetivos": [{
                "objective_id": "o1", "state": "complete",
                "leituras_satisfeitas": [
                    {"need_id": "n1", "target": "cmd/api/main.go:1-11", "satisfeita": True},
                ],
                "reading_needs": [],
            }],
        }
        cob = cli._cobertura_leitura_from_report(report, None)
        self.assertEqual(cob["files_total"], 1)
        self.assertEqual(cob["files_read"], 1)
        self.assertEqual(cob["files_unread"], [])
        self.assertTrue(cob["objetivos_completos"])

    def test_leitura_nao_satisfeita_conta_como_nao_lida(self):
        report = {
            "objetivos": [{
                "objective_id": "o1", "state": "partial",
                "leituras_satisfeitas": [
                    {"need_id": "n1", "target": "cmd/api/main.go:1-11", "satisfeita": False,
                     "motivo": "citação não resolveu"},
                ],
                "reading_needs": [],
            }],
        }
        cob = cli._cobertura_leitura_from_report(report, None)
        self.assertEqual(cob["files_total"], 1)
        self.assertEqual(cob["files_read"], 0)
        self.assertEqual(cob["files_unread"], ["cmd/api/main.go"])
        self.assertFalse(cob["objetivos_completos"])

    def test_reading_need_aberto_de_kind_range_conta_como_nao_lido(self):
        report = {
            "objetivos": [{
                "objective_id": "o1", "state": "partial",
                "leituras_satisfeitas": [],
                "reading_needs": [
                    {"need_id": "n2", "kind": "range", "target": "internal/orders/orders.go:1-18"},
                ],
            }],
        }
        cob = cli._cobertura_leitura_from_report(report, None)
        self.assertEqual(cob["files_total"], 1)
        self.assertEqual(cob["files_unread"], ["internal/orders/orders.go"])

    def test_reading_need_symbol_nunca_vira_arquivo_nao_lido_inventado(self):
        """GAP EXECUTÁVEL: `kind=symbol` (qualname, ex. `app.add`) não tem
        caminho de arquivo em `leituras_satisfeitas`/`reading_needs` — conta
        em `simbolos_sem_mapeamento_de_arquivo`, NUNCA em `files_unread`."""
        report = {
            "objetivos": [{
                "objective_id": "o1", "state": "partial",
                "leituras_satisfeitas": [],
                "reading_needs": [
                    {"need_id": "n1", "kind": "symbol", "target": "app.add"},
                    {"need_id": "n2", "kind": "symbol", "target": "app.subtract"},
                ],
            }],
        }
        cob = cli._cobertura_leitura_from_report(report, None)
        self.assertEqual(cob["files_total"], 0)
        self.assertEqual(cob["files_unread"], [])
        self.assertEqual(cob["simbolos_sem_mapeamento_de_arquivo"], 2)

    def test_evidence_refs_conta_como_lido(self):
        report = {
            "objetivos": [{
                "objective_id": "o1", "state": "complete",
                "leituras_satisfeitas": [],
                "reading_needs": [],
                "evidence_refs": [
                    {"path": "src/app.py", "line_start": 1, "line_end": 2, "content_kind": "code"},
                ],
            }],
        }
        cob = cli._cobertura_leitura_from_report(report, None)
        self.assertEqual(cob["files_total"], 1)
        self.assertEqual(cob["files_read"], 1)

    def test_filtro_por_objective_ids_ignora_objetivo_de_outro_repo(self):
        report = {
            "objetivos": [
                {"objective_id": "meu", "state": "complete",
                 "leituras_satisfeitas": [{"need_id": "n", "target": "a.go:1-1", "satisfeita": True}],
                 "reading_needs": []},
                {"objective_id": "de-outro-repo", "state": "partial",
                 "leituras_satisfeitas": [{"need_id": "n", "target": "b.go:1-1", "satisfeita": False}],
                 "reading_needs": []},
            ],
        }
        cob = cli._cobertura_leitura_from_report(report, {"meu"})
        self.assertEqual(cob["objetivos_no_escopo"], 1)
        self.assertEqual(cob["files_total"], 1)
        self.assertTrue(cob["objetivos_completos"])

    def test_sem_objetivo_no_escopo_nao_e_completo_por_vacuidade(self):
        cob = cli._cobertura_leitura_from_report({"objetivos": []}, {"meu"})
        self.assertEqual(cob["objetivos_no_escopo"], 0)
        self.assertFalse(cob["objetivos_completos"])

    def test_rejeitados_sao_expostos_com_objective_id(self):
        report = {
            "objetivos": [{
                "objective_id": "o1", "state": "partial",
                "leituras_satisfeitas": [], "reading_needs": [],
                "rejeitados": [{"claim_id": "c1", "campo": "sucesso",
                                 "tipo": "evidencia_nao_resolvida", "motivo": "linha não bate"}],
            }],
        }
        cob = cli._cobertura_leitura_from_report(report, None)
        self.assertEqual(cob["claims_rejeitados_total"], 1)
        self.assertEqual(cob["claims_rejeitados"][0]["objective_id"], "o1")
        self.assertEqual(cob["claims_rejeitados"][0]["tipo"], "evidencia_nao_resolvida")


# ---------------------------------------------------------------------------
# Unidade: `_apply_leitura_status`
# ---------------------------------------------------------------------------


class ApplyLeituraStatusTests(unittest.TestCase):
    def test_cobertura_completa_nao_mexe_no_envelope(self):
        cob = {
            "files_total": 1, "files_read": 1, "files_unread": [], "files_unread_total": 0,
            "objetivos_no_escopo": 1, "objetivos_completos": True,
        }
        op, kn, pending, na = cli._apply_leitura_status(
            "succeeded", "complete", [], [],
            command="analyze", store_root="S", repo_abs="R", cobertura_leitura=cob,
        )
        self.assertEqual((op, kn, pending, na), ("succeeded", "complete", [], []))

    def test_arquivo_nao_lido_rebaixa_succeeded_para_partial(self):
        cob = {
            "files_total": 2, "files_read": 1, "files_unread": ["b.go"], "files_unread_total": 1,
            "objetivos_no_escopo": 1, "objetivos_completos": True,
        }
        op, kn, pending, na = cli._apply_leitura_status(
            "succeeded", "complete", [], [],
            command="analyze", store_root="S", repo_abs="R", cobertura_leitura=cob,
        )
        self.assertEqual(op, "partial")
        self.assertEqual(kn, "partial")
        self.assertEqual(pending[0]["tipo"], "cobertura_incompleta")
        self.assertIn("b.go", pending[0]["causa"])
        self.assertTrue(na)
        self.assertEqual(na[0]["argv"][:2], ["wk", "resume"])

    def test_objetivo_nao_completo_tambem_rebaixa_mesmo_sem_arquivo_nao_lido(self):
        cob = {
            "files_total": 0, "files_read": 0, "files_unread": [], "files_unread_total": 0,
            "objetivos_no_escopo": 1, "objetivos_completos": False,
        }
        op, kn, pending, na = cli._apply_leitura_status(
            "succeeded", "complete", [], [],
            command="analyze", store_root="S", repo_abs="R", cobertura_leitura=cob,
        )
        self.assertEqual(op, "partial")
        self.assertEqual(kn, "partial")

    def test_sem_objetivo_no_escopo_nao_mexe_em_nada(self):
        cob = {"objetivos_no_escopo": 0, "objetivos_completos": False}
        op, kn, pending, na = cli._apply_leitura_status(
            "partial", "partial", [{"tipo": "sem_objetivos"}], [],
            command="analyze", store_root="S", repo_abs="R", cobertura_leitura=cob,
        )
        self.assertEqual((op, kn), ("partial", "partial"))
        self.assertEqual(pending, [{"tipo": "sem_objetivos"}])

    def test_ja_blocked_nao_regride_para_partial(self):
        cob = {
            "files_total": 1, "files_read": 0, "files_unread": ["a.go"], "files_unread_total": 1,
            "objetivos_no_escopo": 1, "objetivos_completos": False,
        }
        op, kn, pending, na = cli._apply_leitura_status(
            "blocked", "partial", [], [],
            command="analyze", store_root="S", repo_abs="R", cobertura_leitura=cob,
        )
        self.assertEqual(op, "blocked")


# ---------------------------------------------------------------------------
# `_chain_next_action` — `budget_exhausted` com `max_rounds=None` (sem teto)
# ---------------------------------------------------------------------------


class BudgetExhaustedSemTetoTests(unittest.TestCase):
    def test_sem_teto_numerico_nao_sugere_max_rounds(self):
        na = cli._chain_next_action(
            command="resume", store_root="S", repo_abs="R",
            stop_reason="budget_exhausted", detail="orçamento do pacote",
            chain_status_by_objective={
                "o1": {"max_rounds": None, "rounds_used": 3},
            },
            agent_id="local",
        )
        self.assertIsNotNone(na)
        self.assertNotIn("--max-rounds", na["argv"] or [])
        self.assertEqual(na["argv"], ["wk", "resume", "--store", "S", "--repo", "R"])

    def test_com_teto_numerico_ainda_sugere_max_rounds_maior(self):
        na = cli._chain_next_action(
            command="resume", store_root="S", repo_abs="R",
            stop_reason="budget_exhausted", detail="orçamento",
            chain_status_by_objective={
                "o1": {"max_rounds": 3, "rounds_used": 3},
            },
            agent_id="local",
        )
        self.assertIn("--max-rounds", na["argv"])
        self.assertEqual(na["argv"][-1], "6")

    def test_mistura_de_objetivos_com_e_sem_teto_usa_o_maior_numerico(self):
        na = cli._chain_next_action(
            command="resume", store_root="S", repo_abs="R",
            stop_reason="budget_exhausted", detail="orçamento",
            chain_status_by_objective={
                "o1": {"max_rounds": None, "rounds_used": 1},
                "o2": {"max_rounds": 2, "rounds_used": 2},
            },
            agent_id="local",
        )
        self.assertIn("--max-rounds", na["argv"])
        self.assertEqual(na["argv"][-1], "5")


# ---------------------------------------------------------------------------
# Integração: repo Go — analyze/status/resume nunca succeeded/complete;
# `cobertura`/`cobertura_leitura` presentes e consistentes entre os 3.
# ---------------------------------------------------------------------------


class RepoGoReforcoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-reforco-go-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo-go")
        _write(os.path.join(self.repo, "go.mod"), GO_MOD)
        _write(os.path.join(self.repo, "cmd", "api", "main.go"), GO_MAIN)
        _write(os.path.join(self.repo, "internal", "orders", "orders.go"), GO_ORDERS)

    def _analyze(self):
        return _run_cli([
            "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "structural", "--json",
        ])

    def test_analyze_status_resume_nunca_succeeded_complete(self):
        code, out, err = self._analyze()
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertNotEqual(payload["operation_status"], "succeeded", payload)
        self.assertIn("cobertura_leitura", payload["summary"]["detail"])
        self.assertIn("cobertura", payload["summary"]["detail"])

        code, out, err = _run_cli(["status", "--repo", self.repo, "--store", self.store, "--json"])
        self.assertEqual(err, "", err)
        payload_status = json.loads(out)
        self.assertEqual(payload_status["operation_status"], "succeeded", payload_status)
        detail_status = payload_status["summary"]["detail"]
        self.assertIn("cobertura_leitura", detail_status)
        # § reforço item 4: `cobertura` de `status` == a persistida por
        # `analyze` (mesmo dict, sem recalcular o pipeline).
        self.assertEqual(detail_status["cobertura"], payload["summary"]["detail"]["cobertura"])

        code, out, err = _run_cli([
            "resume", "--repo", self.repo, "--store", self.store, "--mode", "structural", "--json",
        ])
        self.assertEqual(err, "", err)
        payload_resume = json.loads(out)
        self.assertNotEqual(payload_resume["operation_status"], "succeeded", payload_resume)
        self.assertNotEqual(payload_resume["knowledge_status"], "complete", payload_resume)
        self.assertIn("cobertura_leitura", payload_resume["summary"]["detail"])
        self.assertIn("cobertura", payload_resume["summary"]["detail"])

    def test_status_rounds_left_none_quando_ilimitado(self):
        """Repo com pelo menos 1 objetivo integrado (Python, que dispara
        dispatch local de verdade) — `chain_status` real, não dict vazio —
        `max_rounds`/`rounds_left` vêm `None` do runtime (sem teto por
        padrão), `status` NUNCA impõe 3."""
        py_repo = os.path.join(self.tmp, "repo-py")
        _write(os.path.join(py_repo, "src", "app.py"), PY_APP)
        store_py = os.path.join(self.tmp, "store-py")
        code, out, err = _run_cli([
            "analyze", "--repo", py_repo, "--store", store_py, "--mode", "structural", "--json",
        ])
        self.assertEqual(err, "", err)
        code, out, err = _run_cli(["status", "--repo", py_repo, "--store", store_py, "--json"])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        continuacao = payload["summary"]["detail"]["continuacao"]
        self.assertIsNone(continuacao["max_rounds"])
        self.assertTrue(continuacao["chain_status"], continuacao)
        for oid, st in continuacao["chain_status"].items():
            self.assertIsNone(st["max_rounds"], st)
            self.assertIsNone(st["rounds_left"], st)


# ---------------------------------------------------------------------------
# `cmd_resume`: `PlanAccountingError` -> `_internal_error_blocked` dedicado
# ---------------------------------------------------------------------------


class ResumePlanAccountingErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-resume-plan-bug-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo")
        _write(os.path.join(self.repo, "src", "app.py"), PY_APP)
        code, out, err = _run_cli([
            "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "structural", "--json",
        ])
        self.assertEqual(err, "", err)

    def test_plan_accounting_error_no_resume_vira_falha_interna(self):
        from analysis.investigation import PlanAccountingError

        with mock.patch.object(
            cli, "_analyze_pipeline", side_effect=PlanAccountingError("bug-simulado-resume"),
        ):
            code, out, err = _run_cli([
                "resume", "--repo", self.repo, "--store", self.store,
                "--mode", "structural", "--json",
            ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "blocked", payload)
        self.assertEqual(code, 2)
        pend = payload["pending"][0]
        self.assertEqual(pend["tipo"], "falha_interna")
        self.assertIn("bug-simulado-resume", pend["causa"])


if __name__ == "__main__":
    unittest.main()
