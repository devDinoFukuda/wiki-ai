"""Testes do lote D: diagnóstico de ruído, dedupe cross-call por conteúdo e
auditoria de custo (tokens/model) em agent-runs.

Cobre exatamente as 5 obrigações do lote:
  (a) edit_echo devolve linha e trecho exatos;
  (b) 550 linhas devolve "550" e "220" na mensagem;
  (c) lista de violações é truncada em 10 com total reportado;
  (d) artefato duplicado por conteúdo gera aviso estruturado (sem bloquear);
  (e) agent-runs/<stage>.json contém campos de tokens e model.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from codescan import agentmerge as agentmerge_mod
from codescan import noise as noise_mod
from codescan import state as st_mod


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _new_wd(tmp: str) -> tuple[str, str, str]:
    repo = os.path.join(tmp, "repo")
    store = os.path.join(tmp, "store")
    os.makedirs(repo)
    wd = st_mod.workdir(store, repo)
    st_mod.init(wd, repo, "codebases/test")
    return repo, store, wd


class NoiseDiagnosticsTest(unittest.TestCase):
    """(a) e (b): `noise.detect_violations` devolve diagnóstico acionável."""

    def test_detect_violations_edit_echo_reports_exact_line_and_snippet(self) -> None:
        text = "\n".join(["primeira linha", "Edited foo.py by mistake", "terceira linha"])

        violations = noise_mod.detect_violations(text, agent=True)

        edit = [v for v in violations if v["regra"] == "edit_echo"]
        self.assertEqual(1, len(edit))
        self.assertEqual(2, edit[0]["linha"])
        self.assertEqual("Edited foo.py by mistake", edit[0]["trecho"])
        self.assertIn("Edited", edit[0]["padrao"])

    def test_detect_violations_long_snippet_is_truncated(self) -> None:
        long_line = "Edited " + ("x" * 300)
        violations = noise_mod.detect_violations(long_line, agent=True)
        edit = [v for v in violations if v["regra"] == "edit_echo"][0]
        self.assertLessEqual(len(edit["trecho"]), noise_mod.MAX_VIOLATION_SNIPPET_LEN)
        self.assertTrue(edit["trecho"].endswith("…"))

    def test_detect_violations_agent_output_too_long_reports_measured_vs_limit(self) -> None:
        text = "\n".join(f"conteudo generico numero {i}" for i in range(550))

        violations = noise_mod.detect_violations(text, agent=True)

        too_long = [v for v in violations if v["regra"] == "agent_output_too_long"]
        self.assertEqual(1, len(too_long))
        self.assertIsNone(too_long[0]["linha"])
        self.assertIn("550", too_long[0]["trecho"])
        self.assertIn("220", too_long[0]["trecho"])

    def test_merge_agent_output_too_long_message_contains_measured_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "\n".join(f"conteudo generico numero {i}" for i in range(550)))

            with self.assertRaises(agentmerge_mod.MergeError) as ctx:
                agentmerge_mod.merge_agent_output(wd, "modules", inp, agent="modules-b01")

            message = str(ctx.exception)
            self.assertIn("550", message)
            self.assertIn("220", message)
            self.assertIn("agent_output_too_long", message)


class NoiseViolationTruncationTest(unittest.TestCase):
    """(c): lista de violações truncada nas ~10 primeiras + total reportado."""

    def test_detect_violations_does_not_truncate_by_itself(self) -> None:
        text = "\n".join(f"Ran command: passo {i}" for i in range(15))
        violations = noise_mod.detect_violations(text, agent=True)
        self.assertEqual(15, len(violations))

    def test_reject_noise_truncates_to_ten_and_reports_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            body = "\n".join(f"Ran command: passo {i}" for i in range(15))
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, f"=== MODULE: src/noisy ===\n{body}\n=== END ===\n")

            with self.assertRaises(agentmerge_mod.MergeError) as ctx:
                agentmerge_mod.merge_agent_output(wd, "modules", inp, agent="modules-b01")

            exc = ctx.exception
            self.assertEqual(15, exc.violacoes_total)
            self.assertLessEqual(len(exc.violacoes), agentmerge_mod.MAX_REPORTED_NOISE_VIOLATIONS)
            message = str(exc)
            self.assertIn("total 15", message)
            # As 10 primeiras ocorrências (em ordem de linha) aparecem...
            self.assertIn("passo 0", message)
            self.assertIn("passo 9", message)
            # ...mas a lista é truncada: a 15ª ocorrência fica de fora do
            # detalhamento, só contando para o total.
            self.assertNotIn("passo 14", message)

    def test_reject_noise_message_still_readable_string_for_backcompat(self) -> None:
        """(d) da obrigação de FIX 1: chave "error" continua string legível
        quando o CLI faz `"error": str(e)` (cli.py não é tocado aqui, então
        validamos a garantia na origem: str(MergeError) é sempre str)."""
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== MODULE: src/x ===\nRan command: y\n=== END ===\n")

            with self.assertRaises(agentmerge_mod.MergeError) as ctx:
                agentmerge_mod.merge_agent_output(wd, "modules", inp, agent="modules-b01")

            self.assertIsInstance(str(ctx.exception), str)
            self.assertIn("ruído rejeitado", str(ctx.exception))
            self.assertIn("sdd-brief", ctx.exception.acao or "")


class DuplicateArtifactByContentTest(unittest.TestCase):
    """(d) FIX 2: duplicata cross-call por conteúdo vira aviso, não bloqueio."""

    def test_duplicate_content_across_calls_reports_structured_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            body = "\n".join(["# Modulo", "", "## Responsabilidade", "- conteudo identico"])

            first = os.path.join(tmp, "first.txt")
            _write(first, f"=== MODULE: src/alpha ===\n{body}\n=== END ===\n")
            result1 = agentmerge_mod.merge_agent_output(wd, "modules", first, agent="modules-b01")
            self.assertEqual([], result1["warnings"])

            second = os.path.join(tmp, "second.txt")
            _write(second, f"=== MODULE: src/alpha-workaround ===\n{body}\n=== END ===\n")
            result2 = agentmerge_mod.merge_agent_output(wd, "modules", second, agent="modules-b02")

            self.assertEqual(1, len(result2["warnings"]))
            warning = result2["warnings"][0]
            self.assertEqual("artefato_duplicado_por_conteudo", warning["tipo"])
            self.assertEqual("src/alpha-workaround", warning["item"])
            self.assertEqual("src-alpha.md", warning["arquivo_existente"])
            self.assertEqual("src-alpha-workaround.md", warning["novo_arquivo"])

            # Nunca bloqueia nem apaga: os dois artefatos existem em disco.
            self.assertTrue(os.path.isfile(os.path.join(wd, "modules", "src-alpha.md")))
            self.assertTrue(os.path.isfile(os.path.join(wd, "modules", "src-alpha-workaround.md")))

    def test_distinct_content_never_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            first = os.path.join(tmp, "first.txt")
            _write(first, "=== MODULE: src/alpha ===\n# Modulo A\n\n- conteudo A\n=== END ===\n")
            agentmerge_mod.merge_agent_output(wd, "modules", first, agent="modules-b01")

            second = os.path.join(tmp, "second.txt")
            _write(second, "=== MODULE: src/beta ===\n# Modulo B\n\n- conteudo B\n=== END ===\n")
            result2 = agentmerge_mod.merge_agent_output(wd, "modules", second, agent="modules-b02")

            self.assertEqual([], result2["warnings"])


class AgentRunTokenAuditTest(unittest.TestCase):
    """(e) FIX 3: agent-runs/<stage>.json ganha tokens estimados e model."""

    def setUp(self) -> None:
        self._prev_env = os.environ.pop("WK_AGENT_MODEL", None)

    def tearDown(self) -> None:
        if self._prev_env is None:
            os.environ.pop("WK_AGENT_MODEL", None)
        else:
            os.environ["WK_AGENT_MODEL"] = self._prev_env

    def test_manifest_has_token_estimates_and_null_model_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== MODULE: src/gamma ===\n# Modulo\n\n- conteudo\n=== END ===\n")

            agentmerge_mod.merge_agent_output(wd, "modules", inp, agent="modules-b01")

            manifest_path = os.path.join(wd, "agent-runs", "modules.json")
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            run = data["runs"][0]

            self.assertIn("tokens", run)
            self.assertIn("input_estimated", run["tokens"])
            self.assertIn("output_estimated", run["tokens"])
            self.assertGreater(run["tokens"]["output_estimated"], 0)
            self.assertEqual(3.6, run["tokens"]["chars_per_token"])
            self.assertIn("model", run)
            self.assertIsNone(run["model"])
            self.assertIn("estimated_cost_usd", run)
            self.assertIsNone(run["estimated_cost_usd"])

    def test_manifest_reads_model_from_env_var(self) -> None:
        os.environ["WK_AGENT_MODEL"] = "claude-sonnet-4.5"
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== MODULE: src/delta ===\n# Modulo\n\n- conteudo\n=== END ===\n")

            agentmerge_mod.merge_agent_output(wd, "modules", inp, agent="modules-b01")

            manifest_path = os.path.join(wd, "agent-runs", "modules.json")
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual("claude-sonnet-4.5", data["runs"][0]["model"])

    def test_explicit_model_param_wins_over_env_var(self) -> None:
        os.environ["WK_AGENT_MODEL"] = "claude-haiku-4.5"
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== MODULE: src/epsilon ===\n# Modulo\n\n- conteudo\n=== END ===\n")

            agentmerge_mod.merge_agent_output(
                wd, "modules", inp, agent="modules-b01", model="claude-opus-4.5"
            )

            manifest_path = os.path.join(wd, "agent-runs", "modules.json")
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual("claude-opus-4.5", data["runs"][0]["model"])

    def test_estimated_cost_computed_when_model_and_input_tokens_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            pack_path = os.path.join(wd, "agent-packs", "modules-batch-01.json")
            _write(pack_path, json.dumps({"schema": "wiki-ai.agent-pack.v2", "filler": "x" * 3600}))
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== MODULE: src/zeta ===\n# Modulo\n\n- conteudo\n=== END ===\n")

            agentmerge_mod.merge_agent_output(
                wd, "modules", inp, agent="modules-b01", model="claude-sonnet-4.5"
            )

            manifest_path = os.path.join(wd, "agent-runs", "modules.json")
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            run = data["runs"][0]
            self.assertIsNotNone(run["tokens"]["input_estimated"])
            self.assertGreater(run["tokens"]["input_estimated"], 0)
            self.assertIsNotNone(run["estimated_cost_usd"])
            self.assertGreater(run["estimated_cost_usd"], 0)


class AgentPackSuffixResolutionTest(unittest.TestCase):
    """BUG B4 (validação E2E, lote D): `--agent` com sufixo extra depois do
    `-b<NN>` deve resolver o agent-pack; quando não resolve, o `null` de
    `input_estimated` precisa vir acompanhado do motivo."""

    def _write_pack(self, wd: str, filename: str, chars: int = 360) -> str:
        path = os.path.join(wd, "agent-packs", filename)
        _write(path, json.dumps({"schema": "wiki-ai.agent-pack.v2", "filler": "x" * chars}))
        return path

    def test_agent_with_suffix_after_batch_does_not_resolve_pack(self) -> None:
        """F-36: suffix após `-b<NN>` (ex: modules-b01-model-test) NÃO resolve pack.
        Contrato B4 foi revogado; sufixo extra impede resolução."""
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            self._write_pack(wd, "modules-batch-01.json")
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== MODULE: src/x ===\n# Modulo\n\n- conteudo\n=== END ===\n")

            agentmerge_mod.merge_agent_output(wd, "modules", inp, agent="modules-b01-model-test")

            manifest_path = os.path.join(wd, "agent-runs", "modules.json")
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            tokens = data["runs"][0]["tokens"]
            # Suffix após batch impede resolução
            self.assertIsNone(tokens["input_estimated"])
            self.assertIsNone(tokens["agent_pack_used"])
            self.assertIn("input_unresolved_reason", tokens)
            self.assertIn("modules-b01-model-test", tokens["input_unresolved_reason"])

    def test_agent_with_no_batch_pattern_stays_null_with_explicit_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== MODULE: src/x ===\n# Modulo\n\n- conteudo\n=== END ===\n")

            agentmerge_mod.merge_agent_output(wd, "modules", inp, agent="nome-totalmente-arbitrario")

            manifest_path = os.path.join(wd, "agent-runs", "modules.json")
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            tokens = data["runs"][0]["tokens"]
            self.assertIsNone(tokens["input_estimated"])
            self.assertIsNone(tokens["agent_pack_used"])
            self.assertIn("input_unresolved_reason", tokens)
            self.assertIn("nome-totalmente-arbitrario", tokens["input_unresolved_reason"])

    def test_agent_plain_batch_pattern_still_resolves_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, _store, wd = _new_wd(tmp)
            self._write_pack(wd, "modules-batch-01.json")
            inp = os.path.join(tmp, "agent.txt")
            _write(inp, "=== MODULE: src/x ===\n# Modulo\n\n- conteudo\n=== END ===\n")

            agentmerge_mod.merge_agent_output(wd, "modules", inp, agent="modules-b01")

            manifest_path = os.path.join(wd, "agent-runs", "modules.json")
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            tokens = data["runs"][0]["tokens"]
            self.assertIsNotNone(tokens["input_estimated"])
            self.assertGreater(tokens["input_estimated"], 0)
            self.assertNotIn("input_unresolved_reason", tokens)


if __name__ == "__main__":
    unittest.main()
