from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from codescan import state as st_mod
from codescan.cli import main


WORKDIR_FILES_FORBIDDEN = (".py", ".txt")


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


def _module_output(item: str) -> str:
    bullets = "\n".join(
        f"- 🟢 Fluxo operacional {i}: entrada validada, regra aplicada, dependência chamada e saída persistida em src/quote/QuoteService.java:{10 + i}."
        for i in range(1, 18)
    )
    return "\n".join(
        [
            f"=== MODULE: {item} ===",
            "# Análise do módulo quote",
            "",
            "## Responsabilidade",
            "- 🟢 Responsabilidade: processar cotação mínima com entrada, validação e resposta rastreada em src/quote/QuoteService.java:10.",
            "- 🟢 O módulo concentra a regra de aceite e registra decisão operacional em src/quote/QuoteService.java:11.",
            "",
            "## Estruturas de dados",
            "- 🟢 Entrada: QuoteRequest carrega identificador e valor segurado em src/quote/QuoteRequest.java:3.",
            "- 🟢 Saída: QuoteResponse carrega status e prêmio calculado em src/quote/QuoteResponse.java:3.",
            "",
            "## Fluxos",
            bullets,
            "",
            "## Dependências",
            "- 🟢 Persistência: QuoteRepository grava a decisão em src/quote/QuoteRepository.java:4.",
            "- 🟢 Integração: QuoteService isola chamada de repositório em src/quote/QuoteService.java:14.",
            "",
            "## Rastreabilidade",
            "| Código | Regra | Evidência |",
            "|---|---|---|",
            "| QuoteService.create | validar entrada | src/quote/QuoteService.java:10 |",
            "| QuoteRepository.save | persistir decisão | src/quote/QuoteRepository.java:4 |",
            "",
            "## Lacunas",
            "- 🔴 Lacuna objetiva: não há teste de rejeição para valor negativo em src/quote/QuoteService.java:12.",
            "=== END ===",
            "",
        ]
    )


class DeterministicE2ETest(unittest.TestCase):
    def test_minimal_valid_flow_passes_strict_without_workdir_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            _write(
                os.path.join(repo, "src", "quote", "QuoteService.java"),
                "\n".join(
                    [
                        "package app.quote;",
                        "class QuoteService {",
                        "  QuoteResponse create(QuoteRequest request) {",
                        "    if (request == null) throw new IllegalArgumentException();",
                        "    repository.save(request);",
                        "    return new QuoteResponse();",
                        "  }",
                        "}",
                    ]
                ),
            )
            _write(os.path.join(repo, "src", "quote", "QuoteRequest.java"), "package app.quote;\nclass QuoteRequest {}\n")
            _write(os.path.join(repo, "src", "quote", "QuoteResponse.java"), "package app.quote;\nclass QuoteResponse {}\n")
            _write(os.path.join(repo, "src", "quote", "QuoteRepository.java"), "package app.quote;\nclass QuoteRepository { void save(Object o) {} }\n")

            base = ["--repo", repo, "--store", store, "--verbose"]
            run_stage_out = ""
            for command in (
                ["surface", "--module-min-files", "1", "--topic", "codebases/test"],
                ["export", "--topic", "codebases/test"],
                ["config", "--doc-level", "essencial", "--granularity", "module"],
                ["plan", "--top", "1", "--batches", "1"],
                ["agent-pack", "modules", "--batch", "1", "--batches", "1"],
                ["run-stage", "modules", "--batches", "1"],
            ):
                code, out, err = _run(base + command)
                self.assertEqual(code, 0, err)
                if command[0] == "run-stage":
                    run_stage_out = out

            wd = st_mod.workdir(store, repo)
            run_stage = json.loads(run_stage_out)
            items = run_stage["batches"][0]["items"]
            response = os.path.join(wd, "agent-outputs", "modules-batch-01.txt")
            _write(response, "\n".join(_module_output(item) for item in items))

            code, _out, err = _run(base + ["merge-agent-output", "modules", "--input", response, "--agent", "modules-b01"])
            self.assertEqual(code, 0, err)
            code, _out, err = _run(base + ["done", "modules"])
            self.assertEqual(code, 0, err)
            code, out, err = _run(base + ["audit", "--stage", "modules", "--strict"])
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertEqual(report["status"], "pass")
            self.assertGreaterEqual(report["score"], 90)

            self.assertTrue(os.path.isfile(os.path.join(wd, "agent-runs", "modules.json")))
            specs_root = os.path.join(wd, "sdd", "specs")
            self.assertFalse(os.path.isdir(specs_root))
            for dirpath, _dirnames, filenames in os.walk(wd):
                rel_dir = os.path.relpath(dirpath, wd).replace("\\", "/")
                self.assertNotEqual(rel_dir, "src")
                if rel_dir == ".":
                    self.assertFalse(any(name.endswith(WORKDIR_FILES_FORBIDDEN) for name in filenames))
                else:
                    self.assertFalse(any(name.endswith(".py") for name in filenames))


if __name__ == "__main__":
    unittest.main()
