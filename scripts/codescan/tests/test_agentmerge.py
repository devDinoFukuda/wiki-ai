from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from codescan import agentmerge as agentmerge_mod
from codescan import export as ex_mod
from codescan import sdd as sdd_mod
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


def _module_body(name: str, citation_base: str = "src/domain/Order.java") -> str:
    return "\n".join(
        [
            f"# {name}",
            "",
            "## Responsabilidade",
            f"- 🟢 `{name}` coordena entrada, processo e saída do fluxo principal. "
            f"{citation_base}:10",
            "",
            "## Estruturas de dados",
            f"- 🟢 `{name}Request` carrega os campos `customerId`, `amount` e `status`. "
            f"{citation_base}:12",
            f"- 🟢 `record {name}Record(String customerId, BigDecimal amount, String status)` "
            f"define entidade/tipo de dados rastreável. {citation_base}:14",
            "",
            "## Fluxos",
            f"- 🟢 Entrada validada chega ao serviço, atualiza estado e produz saída operacional. "
            f"{citation_base}:20",
            "",
            "## Dependências",
            f"- 🟢 `{name}Repository` persiste estado e integra com o módulo de domínio. "
            f"{citation_base}:30",
            "",
            "## Rastreabilidade",
            f"- 🟢 Evidência principal em `{citation_base}:10`.",
            "",
            "## Lacunas",
            "- 🟡 Não há lacuna crítica no recorte consolidado.",
        ]
    )


class AgentMergeTest(unittest.TestCase):
    def test_merge_modules_rejects_prose_outside_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            st_mod.set_pending(wd, "modules", ["src/payments"])
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "texto solto\n=== MODULE: src/payments ===\n# Pagamentos\n=== END ===\n")

            code, _out, err = _run(["--repo", repo, "--store", store, "merge-agent-output", "modules", "--input", inp, "--agent", "modules-b01"])

            self.assertEqual(code, 2)
            self.assertIn("prosa fora de bloco MODULE", err)
            state = st_mod.load(wd) or {}
            self.assertEqual([], state["stages"]["modules"]["done"])
            self.assertEqual(["src/payments"], state["stages"]["modules"]["pending"])

    def test_merge_modules_normalizes_path_writes_artifact_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== MODULE: src\\payments ===\n# Pagamentos\n\n## Responsabilidade\n- ok\n=== END ===\n")

            code, out, err = _run(["--repo", repo, "--store", store, "merge-agent-output", "modules", "--input", inp, "--agent", "modules-b01"])

            self.assertEqual(code, 0, err)
            result = json.loads(out)
            self.assertEqual(result["items"], 1)
            artifact = os.path.join(wd, "modules", f"{ex_mod._slug('src/payments')}.md")
            self.assertTrue(os.path.isfile(artifact))
            manifest = os.path.join(wd, "agent-runs", "modules.json")
            self.assertTrue(os.path.isfile(manifest))
            with open(manifest, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["schema"], "wiki-ai.agent-runs.v2")
            self.assertEqual(data["runs"][0]["items"][0]["item"], "src/payments")
            state = st_mod.load(wd) or {}
            self.assertIn("src/payments", state["stages"]["modules"]["done"])

    def test_merge_modules_two_batches_preserve_code_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            first = os.path.join(tmp, "agent-1.txt")
            second = os.path.join(tmp, "agent-2.txt")
            _write(first, f"=== MODULE: src/orders ===\n{_module_body('Order')}\n=== END ===\n")
            _write(second, f"=== MODULE: src/payments ===\n{_module_body('Payment', 'src/domain/Payment.java')}\n=== END ===\n")

            code, _out, err = _run(["--repo", repo, "--store", store, "merge-agent-output", "modules", "--input", first, "--agent", "modules-b01"])
            self.assertEqual(code, 0, err)
            code, _out, err = _run(["--repo", repo, "--store", store, "merge-agent-output", "modules", "--input", second, "--agent", "modules-b02"])
            self.assertEqual(code, 0, err)

            analysis = os.path.join(wd, "sdd", "code-analysis.md")
            with open(analysis, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("OrderRequest", text)
            self.assertIn("PaymentRequest", text)
            self.assertEqual(1, text.count("OrderRequest"))
            self.assertEqual(1, text.count("PaymentRequest"))

    def test_merge_modules_completo_generates_dictionary_and_sanitized_flowchart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            state = st_mod.init(wd, repo, "codebases/test")
            state["sdd"] = {"doc_level": "completo"}
            st_mod.save(wd, state)
            inp = os.path.join(tmp, "agent.txt")
            _write(
                inp,
                f"=== MODULE: src/order@Service[List] ===\n"
                f"{_module_body('Order', 'src/domain/Order.java')}\n"
                f"=== END ===\n",
            )

            code, out, err = _run(["--repo", repo, "--store", store, "--verbose", "merge-agent-output", "modules", "--input", inp, "--agent", "modules-b01"])

            self.assertEqual(code, 0, err)
            self.assertEqual([], json.loads(out)["blockers"])
            dictionary = os.path.join(wd, "sdd", "data-dictionary.md")
            flowchart = os.path.join(wd, "sdd", "flowcharts", "_index.md")
            self.assertTrue(os.path.getsize(dictionary) > 800)
            with open(dictionary, encoding="utf-8") as f:
                dictionary_text = f.read()
            self.assertIn("## Entidades", dictionary_text)
            self.assertIn("OrderRequest", dictionary_text)
            self.assertIn("customerId", dictionary_text)
            with open(flowchart, encoding="utf-8") as f:
                flowchart_text = f.read()
            self.assertIn("```mermaid", flowchart_text)
            self.assertEqual([], sdd_mod._mermaid_errors(flowchart_text))
            mermaid_body = flowchart_text.split("```mermaid", 1)[1].split("```", 1)[0]
            self.assertNotIn("@", mermaid_body)
            self.assertNotIn("[List]", mermaid_body)

    def test_merge_rejects_temp_script_inside_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            script = os.path.join(wd, "agent-outputs", "tmp.py")
            _write(script, "print('x')\n")
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, f"=== MODULE: src/orders ===\n{_module_body('Order')}\n=== END ===\n")

            code, _out, err = _run(["--repo", repo, "--store", store, "merge-agent-output", "modules", "--input", inp, "--agent", "modules-b01"])

            self.assertEqual(code, 2)
            self.assertIn("script temporário proibido", err)
            state = st_mod.load(wd) or {}
            self.assertEqual([], state["stages"]["modules"]["done"])

    def test_merge_modules_multiplos_batches_preserva_code_analysis_anterior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            st_mod.set_pending(wd, "modules", ["src/payments", "src/quotes"])
            first = os.path.join(tmp, "batch-01.txt")
            second = os.path.join(tmp, "batch-02.txt")
            _write(
                first,
                "\n".join(
                    [
                        "=== MODULE: src/payments ===",
                        "# Pagamentos",
                        "",
                        "## Responsabilidade",
                        "- Fluxo de pagamento preservado em src/payments.py:1.",
                        "=== END ===",
                        "",
                    ]
                ),
            )
            _write(
                second,
                "\n".join(
                    [
                        "=== MODULE: src/quotes ===",
                        "# Cotações",
                        "",
                        "## Responsabilidade",
                        "- Fluxo de cotação preservado em src/quotes.py:1.",
                        "=== END ===",
                        "",
                    ]
                ),
            )

            code, _out, err = _run(["--repo", repo, "--store", store, "merge-agent-output", "modules", "--input", first, "--agent", "modules-b01"])
            self.assertEqual(code, 0, err)
            code, _out, err = _run(["--repo", repo, "--store", store, "merge-agent-output", "modules", "--input", second, "--agent", "modules-b02"])
            self.assertEqual(code, 0, err)

            with open(os.path.join(wd, "sdd", "code-analysis.md"), encoding="utf-8") as f:
                analysis = f.read()
            self.assertIn("### Módulo: src-payments", analysis)
            self.assertIn("### Módulo: src-quotes", analysis)
            manifest = os.path.join(wd, "agent-runs", "modules.json")
            with open(manifest, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data["runs"]), 2)

    def test_merge_specs_writes_canonical_triplet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            inp = os.path.join(tmp, "agent.txt")
            _write(
                inp,
                "\n".join(
                    [
                        "=== SPEC: Checkout\\Payment ===",
                        "--- requirements.md ---",
                        "# Requisitos",
                        "--- design.md ---",
                        "# Design",
                        "--- tasks.md ---",
                        "# Tarefas",
                        "=== END ===",
                        "",
                    ]
                ),
            )

            code, out, err = _run(["--repo", repo, "--store", store, "merge-agent-output", "specs", "--input", inp, "--agent", "specs-b01"])

            self.assertEqual(code, 0, err)
            result = json.loads(out)
            self.assertEqual(result["artifacts"], 3)
            root = os.path.join(wd, "sdd", "specs", ex_mod._slug("Checkout/Payment"))
            self.assertTrue(os.path.isfile(os.path.join(root, "requirements.md")))
            self.assertTrue(os.path.isfile(os.path.join(root, "design.md")))
            self.assertTrue(os.path.isfile(os.path.join(root, "tasks.md")))
            self.assertTrue(os.path.isfile(os.path.join(wd, "agent-runs", "specs.json")))

    def test_merge_specs_rejects_incomplete_triplet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== SPEC: checkout ===\n--- requirements.md ---\n# Req\n=== END ===\n")

            code, _out, err = _run(["--repo", repo, "--store", store, "merge-agent-output", "specs", "--input", inp, "--agent", "specs-b01"])

            self.assertEqual(code, 2)
            self.assertIn("SPEC incompleta", err)

    def test_merge_rejects_noise_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== MODULE: src/payments ===\nRan command: x\n=== END ===\n")

            code, _out, err = _run(["--repo", repo, "--store", store, "merge-agent-output", "modules", "--input", inp, "--agent", "modules-b01"])

            self.assertEqual(code, 2)
            self.assertIn("ruído rejeitado", err)

    def test_merge_rules_writes_named_artifact_and_marks_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            st_mod.set_pending(wd, "rules", ["domain"])
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== RULES: domain ===\n# Regras de domínio\n\n- 🟢 Regra A.\n=== END ===\n")

            result = agentmerge_mod.merge_agent_output(wd, "rules", inp)

            self.assertEqual(result["items"], 1)
            artifact = os.path.join(wd, "sdd", "domain.md")
            self.assertTrue(os.path.isfile(artifact))
            state = st_mod.load(wd) or {}
            self.assertIn("domain", state["stages"]["rules"]["done"])

    def test_merge_architecture_writes_named_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== ARCHITECTURE: c4-context ===\n# Contexto C4\n\n- 🟢 Visão geral.\n=== END ===\n")

            result = agentmerge_mod.merge_agent_output(wd, "architecture", inp)

            self.assertEqual(result["items"], 1)
            artifact = os.path.join(wd, "sdd", "c4-context.md")
            self.assertTrue(os.path.isfile(artifact))
            state = st_mod.load(wd) or {}
            self.assertIn("c4-context", state["stages"]["architecture"]["done"])

    def test_merge_synth_writes_named_artifact_and_preserves_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== SYNTH: confirmed ===\n# Confirmado\n\n- 🟢 Fato confirmado.\n=== END ===\n")

            result = agentmerge_mod.merge_agent_output(wd, "synth", inp, agent="Synth Agent")

            self.assertEqual(result["items"], 1)
            artifact = os.path.join(wd, "sdd", "confirmed.md")
            self.assertTrue(os.path.isfile(artifact))
            manifest = os.path.join(wd, "agent-runs", "synth.json")
            with open(manifest, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["runs"][0]["agent"], "Synth Agent")
            state = st_mod.load(wd) or {}
            self.assertIn("confirmed", state["stages"]["synth"]["done"])

    def test_merge_rules_rejects_unknown_artifact_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== RULES: nao-existe ===\n# Regra\n=== END ===\n")

            with self.assertRaises(agentmerge_mod.MergeError) as ctx:
                agentmerge_mod.merge_agent_output(wd, "rules", inp)
            self.assertIn("nao-existe", str(ctx.exception))
            self.assertIn("domain", str(ctx.exception))

    def test_merge_architecture_rejects_prose_outside_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "texto solto\n=== ARCHITECTURE: architecture ===\n# Arquitetura\n=== END ===\n")

            with self.assertRaises(agentmerge_mod.MergeError) as ctx:
                agentmerge_mod.merge_agent_output(wd, "architecture", inp)
            self.assertIn("prosa fora de bloco ARCHITECTURE", str(ctx.exception))

    def test_merge_synth_rejects_duplicate_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            inp = os.path.join(tmp, "agent.txt")
            _write(
                inp,
                "\n".join(
                    [
                        "=== SYNTH: inferred ===",
                        "# Inferido A",
                        "=== END ===",
                        "=== SYNTH: inferred ===",
                        "# Inferido B",
                        "=== END ===",
                        "",
                    ]
                ),
            )

            with self.assertRaises(agentmerge_mod.MergeError) as ctx:
                agentmerge_mod.merge_agent_output(wd, "synth", inp)
            self.assertIn("bloco SYNTH duplicado", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
