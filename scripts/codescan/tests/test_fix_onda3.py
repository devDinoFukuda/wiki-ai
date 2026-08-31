from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from codescan import state as st_mod
from codescan.cli import main


GREEN = "\U0001F7E2"
YELLOW = "\U0001F7E1"
RED = "\U0001F534"


def _write(path: str, text: str = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if text is None:
        text = "\n".join(f"# Line {i}" for i in range(1, 21)) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _run(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _module_doc() -> str:
    return "\n".join([
        "# Módulo de pagamentos",
        "",
        "## Responsabilidade",
        f"- O módulo coordena tentativas de pagamento. {GREEN} `src/payments.py:1`",
        "",
        "## Fluxos",
        f"- Fluxo principal: receber e executar. {GREEN} `src/payments.py:4`",
        "",
        "## Dependências",
        f"- O módulo depende apenas da política local. {GREEN} `src/payments.py:1`",
    ])


def _code_analysis_doc() -> str:
    return "\n".join([
        "# Análise de código",
        "",
        "## Visão geral",
        f"- O serviço separa a lógica de pagamento. {GREEN} `src/payments.py:1`",
        f"- O comportamento confirmado cobre retry e validação. {GREEN} `src/payments.py:2`",
        "",
        "## Módulos",
        f"- `src/payments` concentra regras. {GREEN} `src/payments.py:3`",
        "",
        "## Fluxos",
        f"- Fluxo principal: receber, validar e aplicar. {GREEN} `src/payments.py:4`",
        f"- Fluxo alternativo: retornar falha. {GREEN} `src/payments.py:5`",
        "",
        "## Rastreabilidade",
        "| Arquivo | Responsabilidade | Evidência |",
        "|---|---|---|",
        f"| `src/payments.py` | retry e validação | {GREEN} `src/payments.py:1` |",
        "",
        "## Detalhamento operacional",
        *[f"- Item {i}: descreve comportamento preservável. {YELLOW}" for i in range(18)],
    ])


class Onda3AIntegrationTest(unittest.TestCase):
    """Testes das funcionalidades Onda 3A: `run`, `integrate`, `next --run`, `progresso`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(self.repo, exist_ok=True)
        self.wd = st_mod.workdir(self.store, self.repo)
        st_mod.init(self.wd, self.repo, topic="codebases/test")
        _write(os.path.join(self.repo, "src", "payments.py"), None)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, "--verbose", *args]

    def test_run_modules_without_config_fails_without_prompt(self):
        """Test (a): `run modules` sem config → exit 2, sem prompt no stdout."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")

        code, out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("run", "modules"))

        self.assertEqual(code, 2)
        self.assertIn("config SDD obrigatória", err)
        # Verify no prompt is printed to stdout
        self.assertNotIn("Module Archaeologist", out)
        self.assertNotIn("=== INSTRUÇÕES ===", out)

    def test_run_modules_with_config_returns_json_with_progresso(self):
        """Test (a): `run modules` com config → JSON com progresso."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        _write(os.path.join(self.repo, "src", "payments", "PaymentPolicy.java"), "class PaymentPolicy {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("run", "modules", "--batches", "1"))

        self.assertEqual(code, 0, err)
        # First line should be JSON
        lines = out.split("\n")
        payload = json.loads(lines[0])
        # Verify payload structure
        self.assertIn("stage", payload)
        self.assertEqual(payload["stage"], "modules")
        self.assertIn("progresso", payload)
        self.assertIn("etapa", payload["progresso"])
        self.assertIn("acao", payload)
        self.assertIn("integrate", payload["acao"])

    def test_integrate_without_manifest_fails_with_acao(self):
        """Test (b): `integrate` sem manifesto → erro com acao."""
        code, out, err = _run(self._argv("integrate", "modules"))

        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("error", payload)
        self.assertIn("manifesto ausente", payload["error"])
        self.assertIn("acao", payload)
        self.assertIn("run modules", payload["acao"])
        self.assertIn("progresso", payload)

    def test_integrate_without_outputs_fails_with_faltantes(self):
        """Test (b): `integrate` sem outputs → exit 2 com `faltantes[]`."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        _write(os.path.join(self.repo, "src", "payments", "PaymentPolicy.java"), "class PaymentPolicy {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("run", "modules", "--batches", "1"))
        self.assertEqual(code, 0, err)

        # Now try integrate without outputs
        code, out, err = _run(self._argv("integrate", "modules"))

        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("faltantes", payload)
        self.assertIsInstance(payload["faltantes"], list)
        self.assertGreater(len(payload["faltantes"]), 0)
        self.assertIn("output", payload["faltantes"][0])
        self.assertIn("progresso", payload)

    def test_integrate_with_outputs_attempts_merge(self):
        """Test (b): `integrate` com outputs simulados → merges tentado."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        _write(os.path.join(self.repo, "src", "payments", "PaymentPolicy.java"), "class PaymentPolicy {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("run", "modules", "--batches", "1"))
        self.assertEqual(code, 0, err)

        # Parse the manifest to find output path
        lines = out.split("\n")
        payload = json.loads(lines[0])
        batches = payload.get("batches") or []
        self.assertGreater(len(batches), 0)
        output_path = batches[0].get("output")
        self.assertIsNotNone(output_path)

        # Create simulated output
        module_content = _module_doc()
        agent_output = f"=== MODULE: src/payments ===\n{module_content}\n=== END ===\n"
        _write(output_path, agent_output)

        # Now integrate - may fail on done validation but should attempt merge
        code, out, err = _run(self._argv("integrate", "modules"))

        # Response should contain merge info
        response_text = out if code == 0 else err
        if response_text.strip():
            payload = json.loads(response_text)
            self.assertIn("merges", payload)
            # Verify at least one merge was attempted
            self.assertGreater(len(payload["merges"]), 0)
            self.assertIn("progresso", payload)

    def test_integrate_partial_without_partial_flag_fails(self):
        """Test (c): `integrate` com 1 de 2 outputs sem `--partial` → erro."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        _write(os.path.join(self.repo, "src", "payments", "PaymentPolicy.java"), "class PaymentPolicy {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("run", "modules", "--batches", "2"))
        self.assertEqual(code, 0, err)

        # Parse manifest to get both batch outputs
        lines = out.split("\n")
        payload = json.loads(lines[0])
        batches = payload.get("batches") or []
        self.assertGreaterEqual(len(batches), 2)

        # Create output only for first batch
        output_path_1 = batches[0].get("output")
        agent_output = f"=== MODULE: src/payments ===\n{_module_doc()}\n=== END ===\n"
        _write(output_path_1, agent_output)

        # Try integrate without --partial (should fail)
        code, out, err = _run(self._argv("integrate", "modules"))

        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("faltantes", payload)
        self.assertEqual(len(payload["faltantes"]), 1)

    def test_integrate_partial_with_partial_flag_partial_merge_result(self):
        """Test (c): `integrate` com `--partial` → merge parcial ocorre."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        _write(os.path.join(self.repo, "src", "payments", "PaymentPolicy.java"), "class PaymentPolicy {}\n")
        # Create a second module so we can have 2 batches with one module each
        _write(os.path.join(self.repo, "src", "orders", "OrderService.java"), "class OrderService {}\n")
        _write(os.path.join(self.repo, "src", "orders", "OrderPolicy.java"), "class OrderPolicy {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("run", "modules", "--batches", "2"))
        self.assertEqual(code, 0, err)

        lines = out.split("\n")
        payload = json.loads(lines[0])
        batches = payload.get("batches") or []
        self.assertGreaterEqual(len(batches), 2)

        # Create output only for first batch - use the module id from the batch
        output_path_1 = batches[0].get("output")
        # Get the module id(s) for this batch from the manifest
        batch_items_1 = batches[0].get("items") or []
        first_module = batch_items_1[0] if batch_items_1 else "src"
        agent_output = f"=== MODULE: {first_module} ===\n{_module_doc()}\n=== END ===\n"
        _write(output_path_1, agent_output)

        # Integrate with --partial should process the available batches
        # (done will fail due to validation, but merge/partial processing should work)
        code, out, err = _run(self._argv("integrate", "modules", "--partial"))

        # The response should contain merges info showing partial processing
        if code == 0:
            payload = json.loads(out)
        else:
            payload = json.loads(err)

        self.assertIn("merges", payload)
        # Verify that at least one merge was attempted
        statuses = [m.get("status") for m in payload["merges"]]
        self.assertTrue(any(s in statuses for s in ["ok", "ausente"]))

    def test_next_run_blocked_on_human_decision_returns_bloqueado(self):
        """Test (d): `next --run` quando próximo é decisão humana → bloqueado_em sem execução."""
        code, out, err = _run(self._argv("next", "--run"))

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIn("bloqueado_em", payload)
        # Should be blocked on initial config or surface
        self.assertIn(payload["bloqueado_em"], ("decisao_humana", "passo_manual"))
        # Should not have executed anything
        self.assertNotIn("executado", payload)
        self.assertIn("progresso", payload)

    def test_next_run_may_execute_or_block(self):
        """Test (d): `next --run` executa ou bloqueia conforme próxima ação."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")

        # Do surface first
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        # Now next should point to export (deterministic) or be blocked
        code, out, err = _run(self._argv("next", "--run"))

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        # Should either have executed something or be blocked
        self.assertTrue(
            "executado" in payload or "bloqueado_em" in payload,
            f"payload must have executado or bloqueado_em: {list(payload.keys())}"
        )
        self.assertIn("progresso", payload)

    def test_progresso_in_next_response(self):
        """Test (e): `progresso` presente en next."""
        code, out, err = _run(self._argv("next"))

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIn("progresso", payload)
        prog = payload["progresso"]
        self.assertIn("etapa", prog)
        self.assertIn("de", prog)
        self.assertIn("fase", prog)

    def test_progresso_in_state_response(self):
        """Test (e): `progresso` presente en state."""
        code, out, err = _run(self._argv("state"))

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIn("progresso", payload)
        prog = payload["progresso"]
        self.assertIn("etapa", prog)
        self.assertIn("de", prog)
        self.assertIn("fase", prog)

    def test_progresso_in_run_response(self):
        """Test (e): `progresso` presente em run."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("run", "modules", "--batches", "1"))

        self.assertEqual(code, 0, err)
        lines = out.split("\n")
        payload = json.loads(lines[0])
        self.assertIn("progresso", payload)
        prog = payload["progresso"]
        self.assertIn("etapa", prog)
        self.assertIn("de", prog)
        self.assertIn("fase", prog)

    def test_progresso_format_etapa_number(self):
        """Test que `progresso` tem formato correto: etapa N de M."""
        code, out, err = _run(self._argv("next"))

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        prog = payload["progresso"]
        # Should match pattern "etapa N de M — fase X"
        self.assertRegex(prog, r"^etapa \d+ de \d+ — fase \w+$")

    def test_run_modules_handoff_action_mentions_integrate(self):
        """Test que acao de `run modules` menciona `integrate`."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        _write(os.path.join(self.repo, "src", "payments", "PaymentPolicy.java"), "class PaymentPolicy {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("run", "modules", "--batches", "1"))

        self.assertEqual(code, 0, err)
        lines = out.split("\n")
        payload = json.loads(lines[0])
        self.assertIn("acao", payload)
        self.assertIn("integrate modules", payload["acao"])

    def test_next_run_respects_blocking_conditions(self):
        """Test que `next --run` respeita condições de bloqueio."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        _write(os.path.join(self.repo, "src", "payments", "PaymentPolicy.java"), "class PaymentPolicy {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("run", "modules", "--batches", "1"))
        self.assertEqual(code, 0, err)

        # Now next --run should be blocked or execute
        code, out, err = _run(self._argv("next", "--run"))

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        # Should either be blocked or executed
        self.assertTrue(
            "bloqueado_em" in payload or "executado" in payload,
            f"payload must have bloqueado_em or executado: {payload}"
        )

    def test_integrate_error_includes_batch_context(self):
        """Test que erro do integrate inclui contexto de batch."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        _write(os.path.join(self.repo, "src", "payments", "PaymentPolicy.java"), "class PaymentPolicy {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("run", "modules", "--batches", "1"))
        self.assertEqual(code, 0, err)

        lines = out.split("\n")
        payload = json.loads(lines[0])
        batches = payload.get("batches") or []
        output_path = batches[0].get("output")

        # Create malformed output
        _write(output_path, "malformed content without === MODULE ===")

        code, out, err = _run(self._argv("integrate", "modules"))

        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("batch", payload)
        self.assertIn("agent", payload)


if __name__ == "__main__":
    unittest.main()
