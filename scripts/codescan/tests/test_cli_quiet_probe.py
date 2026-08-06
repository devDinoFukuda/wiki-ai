"""Contrato de verbosidade do `wk code`: o corpo completo do comando é o
PADRÃO (revertido de volta a isto — um ciclo anterior tornou o resumo de uma
linha o padrão para economizar contexto do orquestrador, mas sem o corpo o
agente ficou sem remediação suficiente e foi ler o código-fonte do produto,
gastando mais contexto do que a condensação economizava).

`--quiet` é opt-in: condensa qualquer comando a `{"ok": ..., "cmd": ..., ...}`
numa linha só, quando o chamador realmente só precisa do resumo. `--verbose`
continua aceito como no-op explícito (o corpo completo já é o padrão). As
duas flags valem em QUALQUER posição do argv — antes ou depois do
subcomando — com efeito idêntico.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from codescan import state as st_mod
from codescan.cli import main


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _run(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _one_line_json(text: str) -> dict:
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 1, f"esperado uma unica linha, obtido {len(lines)}: {text!r}"
    return json.loads(lines[0])


class QuietModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(os.path.join(self.repo, "src"), exist_ok=True)
        _write(os.path.join(self.repo, "src", "a.py"), "x = 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _quiet(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, "--quiet", *args]

    def _default(self, *args: str) -> list[str]:
        """Sem `--quiet` nem `--verbose`: o contrato é que o corpo completo
        já é o comportamento padrão do `wk code`."""
        return ["--store", self.store, "--repo", self.repo, *args]

    def test_corpo_completo_e_o_padrao_sem_flag_nenhuma(self):
        code, out, err = _run(self._default("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertGreater(len(lines), 1, "esperava corpo completo (várias linhas) por padrão")
        payload = json.loads(out)
        self.assertIn("linguagens", payload)
        self.assertIn("entry_points", payload)
        # não é o envelope condensado do --quiet
        self.assertNotIn("ok", payload)
        self.assertNotIn("cmd", payload)

    def test_verbose_e_no_op_explicito_igual_ao_padrao(self):
        code, out_default, err = _run(self._default("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, out_verbose, err = _run(
            ["--store", self.store, "--repo", self.repo, "--verbose", "surface", "--module-min-files", "1"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out_default), json.loads(out_verbose))

    def test_quiet_surface_produz_uma_linha_ok(self):
        code, out, err = _run(self._quiet("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        payload = _one_line_json(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["cmd"], "surface")

    def test_quiet_e_opt_in_condensa_os_mesmos_valores_do_corpo_completo(self):
        code, out_default, err = _run(self._default("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, out_quiet, err = _run(self._quiet("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        payload_default = json.loads(out_default)
        payload_quiet = _one_line_json(out_quiet)
        self.assertTrue(payload_quiet["ok"])
        for key in ("arquivos", "loc", "modulos"):
            self.assertEqual(payload_default[key], payload_quiet[key])

    def test_quiet_funciona_antes_e_depois_do_subcomando_com_efeito_identico(self):
        code, out_before, err = _run(self._quiet("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, out_after, err = _run(
            ["--store", self.store, "--repo", self.repo, "surface", "--module-min-files", "1", "--quiet"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(_one_line_json(out_before), _one_line_json(out_after))

    def test_verbose_funciona_antes_e_depois_do_subcomando_como_no_op(self):
        code, out_before, err = _run(
            ["--store", self.store, "--repo", self.repo, "--verbose", "surface", "--module-min-files", "1"]
        )
        self.assertEqual(code, 0, err)
        code, out_after, err = _run(
            ["--store", self.store, "--repo", self.repo, "surface", "--module-min-files", "1", "--verbose"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out_before), json.loads(out_after))
        # é no-op: continua sendo o corpo completo, não o resumo
        lines = [line for line in out_after.splitlines() if line.strip()]
        self.assertGreater(len(lines), 1)

    def test_quiet_plan_produz_uma_linha_e_condensa_batches(self):
        code, _out, err = _run(self._quiet("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._quiet("config", "--doc-level", "essencial", "--granularity", "module"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._quiet("plan", "--batches", "1"))

        self.assertEqual(code, 0, err)
        payload = _one_line_json(out)
        self.assertTrue(payload["ok"])
        self.assertIn("batches_count", payload)
        self.assertNotIn("batches", payload)

    def test_quiet_run_stage_produz_uma_linha(self):
        code, _out, err = _run(self._quiet("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._quiet("config", "--doc-level", "essencial", "--granularity", "module"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._quiet("plan", "--batches", "1"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._quiet("run-stage", "modules", "--batches", "1"))

        self.assertEqual(code, 0, err)
        payload = _one_line_json(out)
        self.assertTrue(payload["ok"])
        self.assertIn("batches_count", payload)

    def test_quiet_state_produz_uma_linha_e_condensa_stages(self):
        code, _out, err = _run(self._quiet("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._quiet("state"))

        self.assertEqual(code, 0, err)
        payload = _one_line_json(out)
        self.assertTrue(payload["ok"])
        self.assertIn("stages_keys", payload)
        self.assertIn("estagio_atual", payload)
        self.assertNotIn("stages", payload)

    def test_quiet_export_produz_uma_linha(self):
        code, _out, err = _run(self._quiet("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._quiet("export"))

        self.assertEqual(code, 0, err)
        payload = _one_line_json(out)
        self.assertTrue(payload["ok"])
        self.assertIn("artifacts_count", payload)

    def test_quiet_audit_produz_uma_linha_com_status_e_score(self):
        code, _out, err = _run(self._quiet("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._quiet("audit"))

        payload = _one_line_json(out)
        self.assertEqual(payload["ok"], code == 0)
        self.assertIn("status", payload)
        self.assertIn("score", payload)
        self.assertIn("stages_count", payload)
        self.assertIn("blockers_count", payload)
        self.assertNotIn("stages", payload)

    def test_quiet_erro_produz_uma_linha_com_ok_false(self):
        wd = st_mod.workdir(self.store, self.repo)
        st_mod.init(wd, self.repo, topic=None)
        st_mod.set_pending(wd, "modules", ["src/payments"])

        code, out, err = _run(self._quiet("done", "modules"))

        self.assertEqual(code, 2)
        payload = _one_line_json(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["cmd"], "done")
        self.assertIn("error", payload)


class ReadCommandErrorTest(unittest.TestCase):
    """`read <caminho inválido>` só pode imprimir uma linha de erro. Antes da
    reversão do padrão, o wrapper de `--quiet` (ativo por padrão) reemitia o
    erro no STDERR *e* imprimia um resumo condensado no STDOUT contendo o
    mesmo erro — duas linhas para o mesmo problema."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(os.path.join(self.repo, "src"), exist_ok=True)
        _write(os.path.join(self.repo, "src", "a.py"), "x = 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _lines(self, out: str, err: str) -> list[str]:
        return [line for line in (out + err).splitlines() if line.strip()]

    def test_read_invalido_imprime_exatamente_uma_linha_por_padrao(self):
        code, out, err = _run(["--store", self.store, "--repo", self.repo, "read", "nao-existe.py"])
        self.assertEqual(code, 2)
        self.assertEqual(len(self._lines(out, err)), 1)

    def test_read_invalido_imprime_exatamente_uma_linha_com_quiet(self):
        code, out, err = _run(
            ["--store", self.store, "--repo", self.repo, "--quiet", "read", "nao-existe.py"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(self._lines(out, err)), 1)
        payload = _one_line_json(out)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)


class UnknownGlobalFlagTest(unittest.TestCase):
    """Nenhuma flag global fora de lugar (ou simplesmente desconhecida) pode
    vazar o dump cru `usage: ... codescan: error: unrecognized arguments`
    do argparse: sempre vira JSON com `error` + `acao` acionável."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(self.repo, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_desconhecida_apos_subcomando_vira_json_acionavel(self):
        code, _out, err = _run(
            ["--store", self.store, "--repo", self.repo, "state", "--flag-que-nao-existe"]
        )
        self.assertEqual(code, 2)
        self.assertNotIn("usage:", err)
        payload = json.loads(err)
        self.assertIn("error", payload)
        self.assertIn("acao", payload)
        self.assertIn("wk code", payload["acao"])

    def test_run_stage_com_verbose_apos_subcomando_nao_produz_mais_erro_cru(self):
        """Reprodução literal do incidente relatado: `run-stage modules
        --verbose` batia em `unrecognized arguments: --verbose` porque só o
        parser de nível superior conhecia a flag. Agora resolve normalmente
        (podendo falhar por falta de config/surface, mas nunca por parsing)."""
        code, _out, err = _run(
            ["--store", self.store, "--repo", self.repo, "run-stage", "modules", "--verbose"]
        )
        self.assertNotIn("unrecognized arguments", err)
        self.assertNotIn("usage:", err)


class StageContractHintTest(unittest.TestCase):
    """`run-stage`/`merge-agent-output`/`agent-pack` precisam apontar
    `sdd-brief <stage>` como fonte canônica do contrato no campo `acao` dos
    erros — objetivo explícito: tirar o incentivo de o agente ler o
    código-fonte do produto para entender o contrato do estágio."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(self.repo, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_stage_sem_estado_cita_sdd_brief(self):
        code, _out, err = _run(
            ["--store", self.store, "--repo", self.repo, "run-stage", "modules"]
        )
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("sdd-brief modules", payload["acao"])

    def test_merge_agent_output_input_ausente_cita_sdd_brief(self):
        code, _out, err = _run(
            [
                "--store", self.store, "--repo", self.repo,
                "merge-agent-output", "modules", "--input", "nao-existe.txt", "--agent", "modules-b01",
            ]
        )
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("sdd-brief modules", payload["acao"])


if __name__ == "__main__":
    unittest.main()
