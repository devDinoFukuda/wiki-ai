"""Handoff do fan-out dirigido pelo humano.

Cobre: `run-stage` materializa o contrato do estágio em
`agent-packs/<stage>-contract.json` (subagente lê pack + contrato como
arquivos, sem executar comando); `handoff <stage>` imprime o prompt de
despacho pronto — batches, caminhos e slot preenchidos — e falha com `acao`
quando o `run-stage` ainda não rodou.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

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


def _module_java(name: str) -> str:
    return "\n".join(
        [
            f"package app.{name};",
            f"class {name.capitalize()}Service {{",
            "  boolean run(String id) {",
            "    if (id == null) return false;",
            "    repository.save(id);",
            "    return true;",
            "  }",
            "}",
        ]
    )


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        for i in range(1, 5):
            _write(
                os.path.join(self.repo, "src", f"m{i}", "Service.java"),
                _module_java(f"m{i}"),
            )
        os.environ.pop("WK_STORE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, "--verbose", *args]

    def _prepare(self) -> None:
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "essencial", "--granularity", "module"))
        self.assertEqual(code, 0, err)

    def test_run_stage_materializa_contrato_e_expoe_caminho(self):
        self._prepare()

        code, out, err = _run(self._argv("run-stage", "modules", "--batches", "2"))

        self.assertEqual(code, 0, err)
        result = json.loads(out)
        contract_path = result["contract"]
        self.assertTrue(os.path.isfile(contract_path), contract_path)
        self.assertTrue(contract_path.endswith("modules-contract.json"))
        with open(contract_path, encoding="utf-8") as f:
            contract = json.load(f)
        self.assertEqual(contract["stage"], "modules")
        self.assertIn("compact_contract", contract)

        with open(result["manifest"], encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["contract"], contract_path)

    def test_handoff_imprime_prompt_com_batches_contrato_e_saidas(self):
        self._prepare()
        code, out, err = _run(self._argv("run-stage", "modules", "--batches", "2"))
        self.assertEqual(code, 0, err)
        result = json.loads(out)

        code, out, err = _run(self._argv("handoff", "modules"))

        self.assertEqual(code, 0, err)
        self.assertIn("2 subagente(s)", out)
        self.assertIn(result["contract"], out)
        for batch in result["batches"]:
            self.assertIn(batch["agent_slot"], out)
            self.assertIn(batch["agent_pack"], out)
            self.assertIn(batch["output"], out)
        self.assertIn("ARQUIVO", out)
        self.assertIn("NÃO execute comandos", out)
        # prompt é texto de despacho, não payload JSON
        with self.assertRaises(ValueError):
            json.loads(out)

    def test_handoff_regrava_contrato_conforme_doc_level_atual(self):
        self._prepare()
        code, _out, err = _run(self._argv("run-stage", "modules", "--batches", "2"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "module"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("handoff", "modules"))

        self.assertEqual(code, 0, err)
        wd_contract = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("LEIA:") and line.endswith("modules-contract.json"):
                wd_contract = line.split("LEIA:", 1)[1].strip()
                break
        self.assertIsNotNone(wd_contract)
        with open(wd_contract, encoding="utf-8") as f:
            contract = json.load(f)
        self.assertEqual(contract["doc_level"], "detalhado")

    def test_handoff_sem_run_stage_falha_com_acao(self):
        self._prepare()

        code, _out, err = _run(self._argv("handoff", "modules"))

        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("run-stage", payload["error"])
        self.assertIn("run-stage modules", payload["acao"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
