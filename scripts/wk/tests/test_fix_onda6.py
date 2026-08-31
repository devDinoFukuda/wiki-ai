# -*- coding: utf-8 -*-
"""Testes unitários para o comando `wk finish` (Onda 6D) — novo passo `evidence`.

Onda 6D: `finish` ganhou passo "0) evidence" antes do verify:
  - se `stages.evidence.status != "done"` no state.json do workdir, roda
    `evidence --topic` + `done evidence` via codescan (2 entradas em `passos[]`);
    falha → exit 2 `parado_em:"evidence"`;
  - se já done → 1 entrada `"ok (já feito)"` e segue para verify.

Rodar:
    python -m pytest scripts/wk/tests/test_fix_onda6.py -v
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from wk import cli


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Roda cli.main com stdout/stderr capturados."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()
    except SystemExit as e:
        # argparse chama sys.exit quando há erro de parsing
        return e.code or 1, out.getvalue(), err.getvalue()


def _write(path: str, content: str) -> None:
    """Escreve arquivo com diretórios criados."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


class Onda6DEvidenceFailsTests(unittest.TestCase):
    """Onda 6D: finish com evidence NOT done e codescan falhando → exit 2, parado_em='evidence'."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda6_evidence_fail_")
        self.workdir = tempfile.mkdtemp(prefix="wk_onda6_workdir_fail_")
        self.repo = tempfile.mkdtemp(prefix="wk_onda6_repo_fail_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

        # Inicializar store
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_finish_evidence_not_done_fails_on_evidence_command(self):
        """finish com evidence NOT done e 1ª chamada codescan falhando → exit 2, parado_em='evidence'."""
        # Workdir SEM state.json (ou com evidence.status != "done")
        sdd_dir = os.path.join(self.workdir, "sdd")
        os.makedirs(sdd_dir, exist_ok=True)
        _write(
            os.path.join(sdd_dir, "confirmed.md"),
            "# Confirmed\n\nClaim.\n"
        )

        with mock.patch("wk.cli._finish_capture") as mock_capture:
            call_count = [0]

            def capture_side_effect(fn, argv, **kwargs):
                call_count[0] += 1
                # Primeira chamada é evidence (falhando)
                if call_count[0] == 1 and "evidence" in str(argv):
                    return (1, "", json.dumps({
                        "error": "falha ao gerar evidence-pack"
                    }))
                # Se chegasse a verify, seria a segunda chamada
                return (0, json.dumps({"ok": True}), "")

            mock_capture.side_effect = capture_side_effect

            code, out, err = _run([
                "finish",
                "--workdir", self.workdir,
                "--topic", "demo",
                "--repo", self.repo,
                "--store", self.store,
                "--approved-by", "tester",
            ])

        self.assertEqual(code, 2)
        try:
            data = json.loads(err) if err else json.loads(out)
            self.assertIn("passos", data)
            self.assertIn("parado_em", data)
            self.assertEqual(data["parado_em"], "evidence")
            self.assertIn("acao", data)
            # Procurar pelo passo evidence que falhou
            evidence_steps = [p for p in data["passos"] if p.get("passo") == "evidence"]
            self.assertGreater(len(evidence_steps), 0)
            self.assertEqual(evidence_steps[0]["status"], "falhou")
            # Verificar que verify nunca foi chamado
            verify_steps = [p for p in data["passos"] if p.get("passo") == "verify"]
            self.assertEqual(len(verify_steps), 0, "verify não deve ter sido chamado quando evidence falha")
            # Apenas 1 chamada deve ter sido feita (evidence falhando)
            self.assertEqual(call_count[0], 1)
        except json.JSONDecodeError:
            self.fail(f"Resposta não é JSON válido: out={out}, err={err}")

    def test_finish_evidence_not_done_succeeds_then_calls_verify(self):
        """finish com evidence NOT done, evidence sucede → passos[] tem 2x evidence, depois verify."""
        # Workdir SEM state.json - evidence será gerado
        sdd_dir = os.path.join(self.workdir, "sdd")
        os.makedirs(sdd_dir, exist_ok=True)
        _write(
            os.path.join(sdd_dir, "confirmed.md"),
            "# Confirmed\n\nClaim.\n"
        )

        with mock.patch("wk.cli._finish_capture") as mock_capture:
            call_count = [0]

            def capture_side_effect(fn, argv, **kwargs):
                call_count[0] += 1
                # Verificar se é evidence, done, ou verify
                if call_count[0] == 1:
                    # Primeira chamada: evidence (gerar pack)
                    return (0, json.dumps({"items": 5, "warnings": []}), "")
                elif call_count[0] == 2:
                    # Segunda chamada: done evidence
                    return (0, json.dumps({"ok": True}), "")
                else:
                    # Demais: verify falha (para parar aí)
                    return (1, "", json.dumps({"error": "teste"}))

            mock_capture.side_effect = capture_side_effect

            code, out, err = _run([
                "finish",
                "--workdir", self.workdir,
                "--topic", "demo",
                "--repo", self.repo,
                "--store", self.store,
                "--approved-by", "tester",
            ])

        # Esperamos exit 2 (verify falha)
        self.assertEqual(code, 2)
        data = json.loads(err) if err else json.loads(out)
        self.assertIn("passos", data)

        # Verificar que evidence foi executado (2 vezes: gerar + marcar done)
        evidence_steps = [p for p in data["passos"] if p.get("passo") == "evidence"]
        self.assertGreaterEqual(len(evidence_steps), 1,
                              "evidence deve aparecer pelo menos 1 vez em passos")

        # Verificar que verify também foi tentado (e falhou)
        verify_steps = [p for p in data["passos"] if p.get("passo") == "verify"]
        self.assertGreater(len(verify_steps), 0, "verify deve ter sido tentado após evidence")


class Onda6DEvidenceAlreadyDoneTests(unittest.TestCase):
    """Onda 6D: finish com evidence JÁ done no state.json → passo 'ok (já feito)', verify chamado."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda6_evidence_done_")
        self.workdir = tempfile.mkdtemp(prefix="wk_onda6_workdir_done_")
        self.repo = tempfile.mkdtemp(prefix="wk_onda6_repo_done_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

        # Inicializar store
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def _create_state_json_with_evidence_done(self):
        """Cria state.json com evidence.status = 'done'."""
        state_data = {
            "stages": {
                "evidence": {
                    "status": "done",
                    "done": [],
                    "pending": [],
                    "artifact": None
                }
            }
        }
        state_path = os.path.join(self.workdir, "state.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state_data, f)

    def test_finish_evidence_already_done_skips_evidence_generation(self):
        """finish com evidence já done → passo evidence com status 'ok (já feito)'."""
        # Criar state.json com evidence.status = "done"
        self._create_state_json_with_evidence_done()

        sdd_dir = os.path.join(self.workdir, "sdd")
        os.makedirs(sdd_dir, exist_ok=True)
        _write(
            os.path.join(sdd_dir, "confirmed.md"),
            "# Confirmed\n\nClaim.\n"
        )

        with mock.patch("wk.cli._finish_capture") as mock_capture:
            call_count = [0]

            def capture_side_effect(fn, argv, **kwargs):
                call_count[0] += 1
                # Primeira chamada deve ser verify, não evidence (porque evidence já está done)
                if call_count[0] == 1:
                    # Esperado: verify (não evidence)
                    return (1, "", json.dumps({"error": "teste"}))
                return (0, json.dumps({"ok": True}), "")

            mock_capture.side_effect = capture_side_effect

            code, out, err = _run([
                "finish",
                "--workdir", self.workdir,
                "--topic", "demo",
                "--repo", self.repo,
                "--store", self.store,
                "--approved-by", "tester",
            ])

        # Esperamos exit 2 (verify falha)
        self.assertEqual(code, 2)
        data = json.loads(err) if err else json.loads(out)
        self.assertIn("passos", data)

        # Procurar pelo passo evidence
        evidence_steps = [p for p in data["passos"] if p.get("passo") == "evidence"]
        self.assertGreater(len(evidence_steps), 0, "evidence deve estar em passos")
        # Deve ter apenas 1 passo evidence com status "ok (já feito)"
        self.assertEqual(len(evidence_steps), 1,
                        "evidence já done deve aparecer apenas 1 vez em passos")
        self.assertEqual(evidence_steps[0]["status"], "ok (já feito)",
                        f"evidence já done deve ter status 'ok (já feito)', got {evidence_steps[0]['status']}")

        # Apenas 1 chamada foi feita (verify), não 3 (evidence + done evidence + verify)
        self.assertEqual(call_count[0], 1, "Apenas 1 chamada esperada (verify), pois evidence já estava done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
