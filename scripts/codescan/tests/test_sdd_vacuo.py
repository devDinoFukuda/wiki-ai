"""S16: elimina a aprovação vácua de `audit`/`audit --strict` (falso positivo).

Defeito verificado em store real: `audit --strict` num workdir onde apenas
`surface` está `done` (nenhum estágio de conteúdo concluído) avaliava ZERO
estágios e retornava `{"status": "pass", "score": 100}` — a agregação usava
`min(<scores>, default=100)`, então ausência total de evidência produzia nota
máxima. "Nada foi verificado" ficava indistinguível de "tudo passou".

Este arquivo cobre:
1. workdir com só `surface` done -> `audit --strict` não retorna `pass`, não
   retorna score 100, e sai != 0 (corpo completo e `--quiet`).
2. um estágio com artefatos reais e válidos -> continua `pass` com score
   real (a correção não pode virar falso negativo).
3. um estágio sem nenhum artefato elegível -> não contribui score 100 para a
   agregação, mesmo quando outro estágio no mesmo escopo é um `pass` real.
4. a saída (corpo completo e `--quiet`) sempre expõe quantos estágios foram
   avaliados e quantos artefatos foram verificados.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from codescan import sdd as sdd_mod
from codescan import state as st_mod
from codescan.cli import main


def _run(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _module_java(name: str) -> str:
    return "\n".join(
        [
            f"package app.{name};",
            f"class {name.capitalize()}Service {{",
            "  boolean run(String id) {",
            "    if (id == null) return false;",
            "    return true;",
            "  }",
            "}",
        ]
    )


def _passing_stage_report(stage: str, score: int = 96) -> dict:
    """Resultado sintético de `_audit_stage` como um estágio real e válido
    produziria: status pass, score real, >=1 artefato de fato verificado."""
    return {
        "stage": stage,
        "status": "pass",
        "score": score,
        "threshold": sdd_mod.MIN_DONE_SCORE,
        "blockers": [],
        "warnings": [],
        "artifacts": [{"path": f"sdd/{stage}.md", "status": "pass", "score": score}],
        "artifacts_checked": 1,
    }


class VacuousAuditCliTest(unittest.TestCase):
    """Reprodução end-to-end (store real) do defeito relatado: só `surface`
    concluído, nenhum estágio de conteúdo tocado."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(os.path.join(self.repo, "src", "m1"), exist_ok=True)
        with open(os.path.join(self.repo, "src", "m1", "Service.java"), "w", encoding="utf-8") as f:
            f.write(_module_java("m1"))
        os.environ.pop("WK_STORE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, *args]

    def _only_surface_done(self) -> None:
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

    def test_strict_com_so_surface_done_nao_retorna_pass_nem_score_100(self):
        self._only_surface_done()

        code, out, err = _run(self._argv("audit", "--strict"))

        self.assertNotEqual(code, 0, f"out={out!r} err={err!r}")
        report = json.loads(out)
        self.assertNotEqual(report["status"], "pass")
        self.assertNotEqual(report["score"], 100)
        self.assertIsNone(report["score"])
        self.assertEqual(report["status"], "sem_evidencia")

    def test_strict_com_so_surface_done_expoe_contagens_e_mensagem_no_corpo_completo(self):
        self._only_surface_done()

        code, out, _err = _run(self._argv("audit", "--strict"))

        report = json.loads(out)
        self.assertEqual(report["stages_evaluated"], 0)
        self.assertEqual(report["artifacts_checked"], 0)
        self.assertIn("message", report)
        self.assertIn("0/", report["message"])
        self.assertIn("estágios avaliados", report["message"])
        self.assertIn("modules", report["message"])
        self.assertIn("sdd-brief", report["message"])
        self.assertNotEqual(code, 0)

    def test_strict_com_so_surface_done_expoe_contagens_no_modo_quiet(self):
        self._only_surface_done()

        code, out, _err = _run(self._argv("audit", "--strict", "--quiet"))

        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "sem_evidencia")
        self.assertIsNone(payload["score"])
        self.assertEqual(payload["stages_evaluated"], 0)
        self.assertEqual(payload["artifacts_checked"], 0)
        self.assertIn("message", payload)
        self.assertNotEqual(code, 0)


class VacuousAuditAggregationTest(unittest.TestCase):
    """Testes diretos de `sdd.audit_stages`/`sdd._audit_stage` (sem CLI) para
    isolar a lógica de agregação corrigida."""

    def _wd(self) -> tuple[tempfile.TemporaryDirectory[str], str]:
        tmp = tempfile.TemporaryDirectory()
        repo = os.path.join(tmp.name, "repo")
        store = os.path.join(tmp.name, "store")
        os.makedirs(repo, exist_ok=True)
        wd = st_mod.workdir(store, repo)
        st_mod.init(wd, repo, topic=None)
        return tmp, wd

    def test_audit_stages_com_lista_vazia_nao_e_pass_nem_100(self):
        tmp, wd = self._wd()
        with tmp:
            st = st_mod.load(wd)
            report = sdd_mod.audit_stages(wd, [], st)

        self.assertEqual(report["status"], "sem_evidencia")
        self.assertIsNone(report["score"])
        self.assertEqual(report["stages_evaluated"], 0)
        self.assertEqual(report["artifacts_checked"], 0)
        self.assertEqual(report["stages"], [])
        self.assertIn("message", report)

    def test_estagio_real_com_artefatos_validos_continua_pass_com_score_real(self):
        """Não pode virar falso negativo: um estágio de verdade, com artefato
        elegível e nota real, precisa permanecer `pass` após a correção."""
        tmp, wd = self._wd()
        with tmp:
            st = st_mod.load(wd)
            with mock.patch.object(sdd_mod, "_audit_stage", side_effect=lambda wd, s, st: _passing_stage_report(s)):
                report = sdd_mod.audit_stages(wd, ["modules"], st)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["score"], 96)
        self.assertEqual(report["stages_evaluated"], 1)
        self.assertEqual(report["artifacts_checked"], 1)
        self.assertNotIn("message", report)

    def test_estagio_sem_artefato_elegivel_nao_contribui_score_100(self):
        """`modules` marcado como já tocado no state, mas sem nenhuma regra
        elegível (RULES vazio para o estágio) — o mesmo formato de defeito do
        `default=100`, agora no nível de um único estágio."""
        tmp, wd = self._wd()
        with tmp:
            st = st_mod.load(wd)
            st.setdefault("stages", {})["modules"] = {"done": ["src/m1"], "status": "done"}
            st_mod.save(wd, st)

            with mock.patch.dict(sdd_mod.RULES, {"modules": ()}):
                stage_report = sdd_mod._audit_stage(wd, "modules", st)

        self.assertEqual(stage_report["status"], "sem_evidencia")
        self.assertIsNone(stage_report["score"])
        self.assertNotEqual(stage_report["score"], 100)
        self.assertEqual(stage_report["artifacts_checked"], 0)
        self.assertTrue(
            any("nenhum artefato elegível" in b for b in stage_report["blockers"]),
            stage_report["blockers"],
        )

    def test_estagio_vazio_misturado_com_estagio_real_nao_infla_agregado_para_100(self):
        """Um estágio genuinamente `pass` (score 96) ao lado de um estágio sem
        nenhum artefato elegível: o agregado não pode virar `pass`/100 (isso
        esconderia a lacuna do segundo estágio), mas também não pode apagar
        o sinal real do primeiro."""
        tmp, wd = self._wd()
        with tmp:
            st = st_mod.load(wd)
            original_audit_stage = sdd_mod._audit_stage

            def fake_audit_stage(wd, stage, st):
                if stage == "modules":
                    return _passing_stage_report("modules", score=96)
                with mock.patch.dict(sdd_mod.RULES, {stage: ()}):
                    return original_audit_stage(wd, stage, st)

            with mock.patch.object(sdd_mod, "_audit_stage", side_effect=fake_audit_stage):
                report = sdd_mod.audit_stages(wd, ["modules", "rules"], st)

        self.assertNotEqual(report["status"], "pass")
        self.assertNotEqual(report["score"], 100)
        self.assertEqual(report["status"], "sem_evidencia")
        self.assertEqual(report["stages_evaluated"], 1)
        self.assertEqual(report["artifacts_checked"], 1)
        # o sinal real do estágio que passou não é apagado pela agregação:
        self.assertEqual(report["score"], 96)
        self.assertIn("message", report)
        self.assertIn("rules", report["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
