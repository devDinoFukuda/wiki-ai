from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from codescan import agentpack
from codescan.cli import main


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _run(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class AgentPackV2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(self.repo, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, *args]

    def test_build_pack_respeita_limites_e_prioriza_evidencia_operacional(self):
        module = os.path.join(self.repo, "src", "quote")
        _write(os.path.join(module, "QuoteController.java"), "\n".join([
            "package app.quote;",
            "@RestController",
            "class QuoteController {",
            "  @PostMapping(\"/quotes\")",
            "  QuoteResponse create(QuoteRequest request) {",
            "    if (request == null) throw new IllegalArgumentException();",
            "    return service.create(request);",
            "  }",
            "}",
        ]))
        _write(os.path.join(module, "QuoteDto.java"), "\n".join([f"class Dto{i} {{}}" for i in range(120)]))
        _write(os.path.join(module, "QuoteRepository.java"), "\n".join([
            "class QuoteRepository {",
            "  void save(Quote quote) {}",
            "}",
        ]))
        pack = agentpack.build_agent_pack(
            repo=self.repo,
            repo_label=self.repo,
            stage="modules",
            batch=1,
            total_batches=1,
            modules=[{"path": "src/quote", "loc": 200, "files": 3, "languages": ["java"]}],
            limits=agentpack.PackLimits(max_bytes=45_000, max_files_per_module=2, max_lines_per_file=4),
        )

        self.assertEqual(pack["schema"], "wiki-ai.agent-pack.v2")
        self.assertLessEqual(pack["metrics"]["bytes"], 45_000)
        evidence = pack["modules"][0]["evidence"]
        self.assertLessEqual(len(evidence), 2)
        self.assertLessEqual(max(len(item["signals"]) for item in evidence), 4)
        self.assertEqual(evidence[0]["path"], "src/quote/QuoteController.java")

    def test_cli_agent_pack_default_v2_compacto(self):
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "\n".join([
            "package app.payments;",
            "class PaymentService {",
            "  boolean quote(String id) {",
            "    if (id == null) return false;",
            "    repository.save(id);",
            "    return true;",
            "  }",
            "}",
        ]))

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, out, err = _run(self._argv("agent-pack", "modules", "--batch", "1"))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["schema"], "wiki-ai.agent-pack.v2")
        self.assertLessEqual(report["bytes"], 45_000)
        self.assertLessEqual(report["max_bytes"], 45_000)
        self.assertFalse(os.path.isdir(os.path.join(self.store, ".codescan", "src")))

    def test_plan_considera_budget_de_bytes(self):
        for i in range(6):
            _write(os.path.join(self.repo, "src", f"m{i}", "Service.java"), "\n".join([
                f"package app.m{i};",
                "class Service {",
                "  void run() {",
                "    if (true) repository.save(\"x\");",
                "  }",
                "}",
            ]))

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)
        code, out, err = _run(["--store", self.store, "--repo", self.repo, "--verbose", "plan", "--agent-pack-max-bytes", "1200"])
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertIn("agent_pack_budget", plan)
        self.assertEqual(plan["agent_pack_budget"]["max_bytes"], 1200)
        self.assertGreater(plan["subagentes"], 1)

    def test_agent_pack_batch_invalido_cita_sdd_brief_no_erro(self):
        """O campo `acao` de um erro de `agent-pack` precisa citar
        `sdd-brief modules` como fonte canônica do contrato do estágio, em
        vez de deixar o agente adivinhar lendo o código-fonte do produto."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("agent-pack", "modules", "--batch", "99"))

        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("batch invalido", payload["error"])
        self.assertIn("sdd-brief modules", payload["acao"])

    def test_agent_pack_aceita_quiet_e_verbose_apos_o_subcomando(self):
        """`--quiet`/`--verbose` precisam ter efeito idêntico em qualquer
        posição do argv, não só antes do subcomando."""
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("agent-pack", "modules", "--batch", "1", "--quiet"))
        self.assertEqual(code, 0, err)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        summary = json.loads(lines[0])
        self.assertTrue(summary["ok"])

        code, out, err = _run(self._argv("agent-pack", "modules", "--batch", "1", "--verbose"))
        self.assertEqual(code, 0, err)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertGreater(len(lines), 1, "--verbose e no-op: corpo completo continua saindo")


if __name__ == "__main__":
    unittest.main()
