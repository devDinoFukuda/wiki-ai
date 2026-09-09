"""ENTREGA (defeito reproduzido pelo orquestrador): repo Go sintético
(`cmd/api/main.go`, `internal/orders/orders.go`, `go.mod`) analisado com
`wk analyze --mode structural` devolvia `operation_status=succeeded`,
`knowledge_status=complete`, `chain.objective_ids=[]`, `pending=[]`, exit 0 —
"completo" falso: nenhuma linguagem tinha extrator e o modelo nunca foi
consultado.

Dono exclusivo desta suíte: `scripts/wk/cli.py`
(`_apply_cobertura_status`/`_cobertura_extracao_dict`/`_objetivos_discovery_info`
e a cobertura de extração em `doctor --repo`/`status --repo`) — nada em
`scripts/analysis/**`/`scripts/knowledge/**`/`scripts/runtime/**` foi editado
para este arquivo passar.

Cobre:
1. repo Go sintético (`--mode structural`) -> NUNCA `succeeded`/`complete`
   vazio; `partial`/exit 3 com `pending` tipado `sem_objetivos` OU
   `descoberta_requer_agente` (o segundo é o que `analysis.investigation`
   produz hoje, via `ObjectiveKind.DISCOVERY` — o teste aceita qualquer um
   dos dois, nunca `succeeded`).
2. repo sem NENHUM arquivo de código (só `.md`) -> `noop`/`not_applicable`,
   `pending` tipado `sem_codigo`.
3. repo Python (compat, item 5) -> nenhum pending novo (`sem_objetivos`/
   `sem_codigo`/`descoberta_requer_agente`) aparece quando há capacidade real.
4. `doctor --repo`/`status --repo` mostram `cobertura_extracao` com
   linguagem sem extrator + contagem.
5. `wk profile set --discovery-max-files N` / `wk profile show` aceitam e
   exibem `discovery_max_files_per_objective`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

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
    "func main() {\n"
    "\tsvc := orders.NewService()\n"
    "\t_ = svc\n"
    "}\n"
)
GO_ORDERS = (
    "package orders\n\n"
    "type Order struct {\n"
    "\tID string\n"
    "}\n\n"
    "type Service struct{}\n\n"
    "func NewService() *Service {\n"
    "\treturn &Service{}\n"
    "}\n"
)
GO_MOD = "module example.com/api\n\ngo 1.21\n"

PY_APP = (
    "def add(a, b):\n"
    "    return a + b\n"
)

#: pending que NUNCA pode coexistir com `succeeded`/`complete` — evidência
#: do defeito reproduzido corrigida (item 1 da entrega).
_TIPOS_COBERTURA_INCOMPLETA = {"sem_objetivos", "descoberta_requer_agente"}


class _RepoStoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-cobertura-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")

    def _analyze(self, repo, *extra):
        return _run_cli([
            "analyze", "--repo", repo, "--store", self.store,
            "--mode", "structural", "--json", *extra,
        ])


# ---------------------------------------------------------------------------
# 1) repo Go sintético — defeito reproduzido
# ---------------------------------------------------------------------------


class RepoGoSemExtratorTests(_RepoStoreCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo-go")
        _write(os.path.join(self.repo, "go.mod"), GO_MOD)
        _write(os.path.join(self.repo, "cmd", "api", "main.go"), GO_MAIN)
        _write(os.path.join(self.repo, "internal", "orders", "orders.go"), GO_ORDERS)

    def test_nunca_succeeded_complete_vazio(self):
        code, out, err = self._analyze(self.repo)
        self.assertEqual(err, "", err)
        payload = json.loads(out)

        # O NÚCLEO do defeito: nunca `succeeded`/`complete` com o repo Go
        # inteiro sem cobertura estrutural nenhuma.
        self.assertNotEqual(payload["operation_status"], "succeeded", payload)
        self.assertNotEqual(payload["knowledge_status"], "complete", payload)
        self.assertEqual(payload["operation_status"], "partial", payload)
        self.assertEqual(payload["knowledge_status"], "partial", payload)
        self.assertEqual(code, 3, payload)
        self.assertEqual(code, cli._exit_code_common(payload["operation_status"]))

        tipos = {p["tipo"] for p in payload["pending"]}
        self.assertTrue(
            tipos & _TIPOS_COBERTURA_INCOMPLETA,
            f"esperava sem_objetivos OU descoberta_requer_agente em {tipos}",
        )
        # `pending` nunca fica vazio quando o resultado não é `succeeded`.
        self.assertTrue(payload["pending"], payload)
        self.assertTrue(payload["next_actions"], payload)

    def test_cobertura_extracao_lista_go_sem_extrator(self):
        code, out, err = self._analyze(self.repo)
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        detail = payload["summary"]["detail"]
        cobertura = detail["cobertura_extracao"]
        self.assertIn("go", cobertura["linguagens_sem_extrator"])
        self.assertEqual(cobertura["linguagens_sem_extrator"]["go"], 3)
        self.assertEqual(cobertura["arquivos_totais"], 3)
        # § entrega item 1: `arquivos_sem_extrator` (chave literal) também
        # presente no topo de `summary.detail`.
        self.assertIn("go", detail["arquivos_sem_extrator"])

    def test_cobertura_canonica_de_plan_accounting_sem_arquivo_perdido(self):
        """`summary.detail.cobertura` = `analysis.investigation.plan_accounting(
        ..., extraction=, inventory=)` (fonte canônica, SEMPRE calculada —
        reforço pós-entrega de `analysis`): `files_uncovered == 0` (nenhum
        arquivo de código do repo Go ficou sem NENHUMA obrigação de leitura;
        `assert_plan_accounted` teria levantado `PlanAccountingError`/bloqueio
        interno senão), e `files_without_extractor` bate com
        `arquivos_sem_extrator`."""
        code, out, err = self._analyze(self.repo)
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        detail = payload["summary"]["detail"]
        cobertura = detail["cobertura"]
        self.assertEqual(cobertura["files_uncovered"], 0, cobertura)
        self.assertEqual(cobertura["files_without_extractor"], {"go": 3})
        self.assertEqual(cobertura["files_without_extractor"], detail["arquivos_sem_extrator"])
        self.assertGreaterEqual(cobertura["discovery_objectives"], 1)
        self.assertGreaterEqual(cobertura["discovery_files"], 1)

    def test_objetivos_descoberta_quando_presentes(self):
        """Quando `analysis.investigation` gera objetivos `discovery` (o caso
        real hoje), `summary.detail.objetivos_descoberta` os lista com
        id/módulo/arquivos/linguagens — nunca um dict vazio junto de um
        `pending` `descoberta_requer_agente`."""
        code, out, err = self._analyze(self.repo)
        payload = json.loads(out)
        detail = payload["summary"]["detail"]
        tipos = {p["tipo"] for p in payload["pending"]}
        if "descoberta_requer_agente" in tipos:
            descobertas = detail["objetivos_descoberta"]
            self.assertTrue(descobertas)
            for d in descobertas:
                self.assertTrue(d["objective_id"])
                self.assertGreater(d["arquivos"], 0)
                self.assertIn("go", d["linguagens"])

    def test_doctor_repo_mostra_cobertura_extracao(self):
        code, out, err = _run_cli([
            "doctor", "--repo", self.repo, "--store", self.store, "--json",
        ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        cobertura = payload["summary"]["detail"]["cobertura_extracao"]
        self.assertTrue(cobertura["disponivel"], cobertura)
        self.assertIn("go", cobertura["linguagens_sem_extrator"])
        self.assertEqual(cobertura["linguagens_sem_extrator"]["go"], 3)

    def test_status_repo_mostra_cobertura_extracao(self):
        code, out, err = self._analyze(self.repo)
        self.assertEqual(err, "", err)
        code, out, err = _run_cli([
            "status", "--repo", self.repo, "--store", self.store, "--json",
        ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "succeeded")
        cobertura = payload["summary"]["detail"]["cobertura_extracao"]
        self.assertTrue(cobertura["disponivel"], cobertura)
        self.assertIn("go", cobertura["linguagens_sem_extrator"])


# ---------------------------------------------------------------------------
# 2) repo sem NENHUM arquivo de código
# ---------------------------------------------------------------------------


class RepoSemCodigoTests(_RepoStoreCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo-doc")
        _write(os.path.join(self.repo, "README.md"), "# Só documentação\n\nNada de código aqui.\n")

    def test_noop_not_applicable_com_pending_explicando(self):
        code, out, err = self._analyze(self.repo)
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "noop", payload)
        self.assertEqual(payload["knowledge_status"], "not_applicable", payload)
        self.assertEqual(code, 0)
        tipos = [p["tipo"] for p in payload["pending"]]
        self.assertIn("sem_codigo", tipos)

    def test_repo_totalmente_vazio_tambem_noop(self):
        vazio = os.path.join(self.tmp, "repo-vazio")
        os.makedirs(vazio, exist_ok=True)
        code, out, err = self._analyze(vazio)
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertNotEqual(payload["operation_status"], "succeeded", payload)
        self.assertNotEqual(payload["knowledge_status"], "complete", payload)


# ---------------------------------------------------------------------------
# 3) repo Python — compat (entrega item 5)
# ---------------------------------------------------------------------------


class RepoPythonCompatTests(_RepoStoreCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo-py")
        _write(os.path.join(self.repo, "src", "app.py"), PY_APP)

    def test_sem_pending_de_cobertura_incompleta(self):
        code, out, err = self._analyze(self.repo)
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        tipos = {p["tipo"] for p in payload["pending"]}
        self.assertFalse(tipos & ({"sem_objetivos", "sem_codigo"}), payload["pending"])
        # Python tem extrator dedicado: nenhuma linguagem sem adaptador.
        cobertura = payload["summary"]["detail"]["cobertura_extracao"]
        self.assertEqual(cobertura["linguagens_sem_extrator"], {})
        self.assertEqual(payload["summary"]["detail"]["objetivos_descoberta"], [])
        # `cobertura` canônica (`plan_accounting`) também fecha: nenhum
        # arquivo Python sem obrigação de leitura.
        self.assertEqual(payload["summary"]["detail"]["cobertura"]["files_uncovered"], 0)
        self.assertEqual(payload["summary"]["detail"]["cobertura"]["files_without_extractor"], {})


class PlanAccountingErrorTests(_RepoStoreCase):
    """`PlanAccountingError` (bug de integridade do plano, ex.: arquivo de
    código sem NENHUMA obrigação de leitura) -> `_internal_error_blocked`
    (`tipo=falha_interna`), nunca `_precondition_blocked` — é falha do
    PIPELINE, não do operador/perfil."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo-plan-bug")
        _write(os.path.join(self.repo, "src", "app.py"), PY_APP)

    def test_plan_accounting_error_vira_falha_interna(self):
        from unittest import mock

        from analysis.investigation import PlanAccountingError

        with mock.patch.object(
            cli, "_analyze_pipeline", side_effect=PlanAccountingError("bug-simulado-no-plano"),
        ):
            code, out, err = self._analyze(self.repo)
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "blocked", payload)
        self.assertEqual(code, 2)
        pend = payload["pending"][0]
        self.assertEqual(pend["tipo"], "falha_interna")
        self.assertIn("bug-simulado-no-plano", pend["causa"])
        self.assertIn("PlanAccountingError", pend["causa"])


# ---------------------------------------------------------------------------
# 4) `wk profile set --discovery-max-files` / `wk profile show`
# ---------------------------------------------------------------------------


class ProfileDiscoveryMaxFilesTests(_RepoStoreCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo-profile")
        os.makedirs(self.repo, exist_ok=True)

    def test_set_grava_e_show_exibe(self):
        code, out, err = _run_cli([
            "profile", "set", "--repo", self.repo, "--store", self.store,
            "--discovery-max-files", "7", "--json",
        ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "succeeded")
        self.assertEqual(
            payload["summary"]["detail"]["perfil_gravado"]["discovery_max_files_per_objective"], 7,
        )

        code, out, err = _run_cli([
            "profile", "show", "--repo", self.repo, "--store", self.store, "--json",
        ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        resolvido = payload["summary"]["detail"]["resolvido"]
        self.assertEqual(resolvido["overrides"]["discovery_max_files_per_objective"], 7)
        camada_store = payload["summary"]["detail"]["camadas"]["store"]
        self.assertEqual(camada_store["discovery_max_files_per_objective"], 7)

    def test_flag_chega_integra_no_namespace_do_analyze(self):
        """A mesma flag também existe em `wk analyze` (camada 'cli' de
        `_add_analysis_profile_flags`) — só o parser, sem rodar o pipeline
        pesado (evita duplicar todo o resto do fluxo de `analyze` aqui)."""
        parser = cli._build_parser()
        ns = parser.parse_args([
            "analyze", "--repo", self.repo, "--store", self.store,
            "--discovery-max-files", "3",
        ])
        self.assertEqual(ns.discovery_max_files_per_objective, 3)


# ---------------------------------------------------------------------------
# 5) repo sintético COBOL+JCL+SQL (mainframe) — requisito reforçado: nenhuma
#    rigidez de linguagem pode impedir a análise (evidência só de código
#    executável: `analysis.inventory`/`analysis.extractors.registry` já
#    reconhecem `.cbl`/`.jcl`/`.sql` como linguagem, mesmo sem extrator para
#    COBOL/JCL — `_cobertura_extracao_dict` é agnóstico de linguagem, não
#    precisa de NENHUM caso especial neste módulo).
# ---------------------------------------------------------------------------

COBOL_SRC = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. CUSTMAST.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01 WS-CUSTOMER-ID PIC 9(6).\n"
    "       PROCEDURE DIVISION.\n"
    "       MAIN-PARA.\n"
    "           DISPLAY \"CUSTOMER MASTER\".\n"
    "           STOP RUN.\n"
)
JCL_SRC = (
    "//RUNCUST  JOB (ACCT),'RUN CUSTOMER',CLASS=A\n"
    "//STEP1    EXEC PGM=CUSTMAST\n"
    "//SYSOUT   DD SYSOUT=*\n"
)
SQL_SRC = "CREATE TABLE customer (\n    id INT PRIMARY KEY,\n    name VARCHAR(100)\n);\n"


class RepoMainframeCobolJclSqlTests(_RepoStoreCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo-mainframe")
        _write(os.path.join(self.repo, "src", "CUSTMAST.cbl"), COBOL_SRC)
        _write(os.path.join(self.repo, "jcl", "RUNCUST.jcl"), JCL_SRC)
        _write(os.path.join(self.repo, "sql", "schema.sql"), SQL_SRC)

    def test_nunca_succeeded_complete_vazio(self):
        code, out, err = self._analyze(self.repo)
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertNotEqual(payload["operation_status"], "succeeded", payload)
        self.assertNotEqual(payload["knowledge_status"], "complete", payload)
        tipos = {p["tipo"] for p in payload["pending"]}
        self.assertTrue(tipos & _TIPOS_COBERTURA_INCOMPLETA, payload["pending"])

    def test_cobertura_extracao_cobol_jcl_sem_extrator_sql_nao(self):
        code, out, err = self._analyze(self.repo)
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        cobertura = payload["summary"]["detail"]["cobertura_extracao"]
        sem_extrator = cobertura["linguagens_sem_extrator"]
        # COBOL/JCL: nenhum adaptador dedicado ainda -> aparecem aqui.
        self.assertTrue(
            {"cobol", "jcl"} & set(sem_extrator),
            f"esperava cobol/jcl em {sem_extrator}",
        )
        # nenhum arquivo é silenciosamente omitido: total bate com o repo.
        self.assertEqual(cobertura["arquivos_totais"], 3)


# ---------------------------------------------------------------------------
# 6) `--mode deep` sem agente conectado: `blocked` com `next_actions` de
#    `wk agent connect` (reinforço item 4 — comportamento pré-existente,
#    confirmado aqui por teste dedicado desta suíte).
# ---------------------------------------------------------------------------


class ModeDeepSemAgenteTests(_RepoStoreCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo-deep")
        _write(os.path.join(self.repo, "src", "app.py"), PY_APP)

    def test_deep_sem_binding_bloqueia_com_agent_connect(self):
        code, out, err = _run_cli([
            "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "deep", "--json",
        ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "blocked", payload)
        self.assertEqual(code, 2)
        argvs = [na.get("argv") for na in payload["next_actions"] if na.get("argv")]
        self.assertTrue(
            any("agent" in argv for argv in argvs), payload["next_actions"],
        )


if __name__ == "__main__":
    unittest.main()
