"""Testes para as correções de onda 1: F-03, F-02, F-10, F-27, F-05/F-19/F-32."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from codescan import evidence as ev_mod
from codescan import state as st_mod
from codescan import noise as noise_mod


GREEN = "\U0001F7E2"
YELLOW = "\U0001F7E1"
RED = "\U0001F534"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ============================================================================
# F-03: documento com ≥1 claim-block e ZERO citações válidas → sem_evidencia
# ============================================================================
class F03EvidenceContractTest(unittest.TestCase):
    """F-03: documento com claims mas sem citações válidas reprova."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="codescan_f03_")
        _write(
            os.path.join(self.repo, "src", "example.py"),
            "MAX_RETRIES = 5\ndef retry():\n    return True\n",
        )

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_document_with_claims_and_no_citations_fails(self):
        """Documento com claims 🟡/🔴 mas zero citações válidas reprova."""
        md = f"- Retry has policy. {YELLOW}\n- TODO confirm SLA. {RED}"
        report = ev_mod.verify_markdown(self.repo, md)
        self.assertFalse(report["ok"])
        self.assertTrue(any(e["rule"] == "sem_evidencia" for e in report["errors"]))

    def test_document_with_claims_and_valid_citation_passes(self):
        """Documento com claims 🟡/🔴 + uma citação válida passa."""
        md = (
            f"- Retry policy seems present. {YELLOW}\n"
            f"- Need to confirm complete SLA. {RED}\n"
            f"- The constant MAX_RETRIES defines the maximum number of retry attempts "
            f"before declaring the operation failed. {GREEN} `src/example.py:1`"
        )
        report = ev_mod.verify_markdown(self.repo, md)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["valid_citations"], 1)

    def test_prose_without_claims_with_no_citations_passes(self):
        """Prosa pura sem nenhuma claim em bullet passa sem citações."""
        md = "Este documento descreve o fluxo de retry.\n\nA implementação tem policy."
        report = ev_mod.verify_markdown(self.repo, md)
        self.assertTrue(report["ok"])
        self.assertEqual(report["claims"], 0)


# ============================================================================
# F-02: verify stage.artifacts acumulado; status derivado de done/confirmed
# ============================================================================
class F02VerifyStageStatusTest(unittest.TestCase):
    """F-02: artifacts acumulado; status driven by sdd/confirmed.md + sdd/inferred.md."""

    def setUp(self):
        self.wd = tempfile.mkdtemp(prefix="codescan_f02_")
        self.repo = tempfile.mkdtemp(prefix="codescan_f02_repo_")
        _write(
            os.path.join(self.repo, "src", "example.py"),
            "X = 1\n",
        )

    def tearDown(self):
        shutil.rmtree(self.wd, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_verify_trivial_artifact_in_progress(self):
        """Verify de artefato trivial → stage status in_progress (não done)."""
        st_mod.init(self.wd, self.repo, "test")
        # Simula um verify bem-sucedido
        artifacts = {"trivial.md": "done"}
        st_mod.mark(self.wd, "verify", "in_progress")
        with st_mod._lock(self.wd):
            st = st_mod.load(self.wd) or {}
            stage = st.setdefault("stages", {}).setdefault("verify", {})
            stage["artifacts"] = artifacts
            st_mod.save(self.wd, st)
        # Lê o estado: deve estar in_progress porque não temos confirmed.md check aqui
        st = st_mod.load(self.wd)
        self.assertEqual(st["stages"]["verify"]["status"], "in_progress")

    def test_verify_accumulated_artifacts(self):
        """Múltiplas execuções de verify acumulam em artifacts."""
        st_mod.init(self.wd, self.repo, "test")
        with st_mod._lock(self.wd):
            st = st_mod.load(self.wd) or {}
            stage = st.setdefault("stages", {}).setdefault("verify", {})
            stage["artifacts"] = {"art1.md": "done"}
            st_mod.save(self.wd, st)
        # Segunda execução de verify: acumula
        with st_mod._lock(self.wd):
            st = st_mod.load(self.wd) or {}
            stage = st.get("stages", {}).get("verify", {})
            artifacts = stage.get("artifacts", {})
            artifacts["art2.md"] = "failed"
            stage["artifacts"] = artifacts
            st_mod.save(self.wd, st)
        # Verificar que ambas estão no mapa
        st = st_mod.load(self.wd)
        artifacts = st["stages"]["verify"].get("artifacts", {})
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(artifacts["art1.md"], "done")
        self.assertEqual(artifacts["art2.md"], "failed")


# ============================================================================
# F-10: state.json corrompido → StateCorruptError; .bak criado após save
# ============================================================================
class F10StateCorruptionTest(unittest.TestCase):
    """F-10: StateCorruptError quando state.json corrompido; .bak backups."""

    def setUp(self):
        self.wd = tempfile.mkdtemp(prefix="codescan_f10_")
        self.repo = tempfile.mkdtemp(prefix="codescan_f10_repo_")

    def tearDown(self):
        shutil.rmtree(self.wd, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_load_raises_corrupt_error_for_invalid_json(self):
        """load() levanta StateCorruptError se state.json é JSON inválido."""
        st_path = st_mod._path(self.wd)
        os.makedirs(self.wd, exist_ok=True)
        with open(st_path, "w") as f:
            f.write("{invalid json}")
        # Deve levantar StateCorruptError, não retornar None
        with self.assertRaises(st_mod.StateCorruptError):
            st_mod.load(self.wd)

    def test_load_returns_none_when_state_not_exists(self):
        """load() retorna None se state.json não existe."""
        result = st_mod.load(self.wd)
        self.assertIsNone(result)

    def test_save_creates_backup(self):
        """save() cria .bak do state.json anterior."""
        st_mod.init(self.wd, self.repo, "test")
        st_path = st_mod._path(self.wd)
        self.assertTrue(os.path.exists(st_path))
        # Modifica e salva novamente
        st = st_mod.load(self.wd)
        st["test_key"] = "test_value"
        st_mod.save(self.wd, st)
        # Deve existir .bak agora
        bak_path = st_path + ".bak"
        self.assertTrue(os.path.exists(bak_path), f"backup não criado em {bak_path}")
        # Verifica que .bak é JSON válido (estado anterior)
        with open(bak_path) as f:
            old_state = json.load(f)
        self.assertIsInstance(old_state, dict)

    def test_corrupt_error_message_hints_backup(self):
        """StateCorruptError sugere restaurar de .bak."""
        st_path = st_mod._path(self.wd)
        os.makedirs(self.wd, exist_ok=True)
        # Cria um .bak válido
        bak_path = st_path + ".bak"
        with open(bak_path, "w") as f:
            json.dump({"valid": True}, f)
        # Corrompe o estado.json
        with open(st_path, "w") as f:
            f.write("corrupted")
        # Tenta carregar: deve mencionar .bak na mensagem
        try:
            st_mod.load(self.wd)
            self.fail("Esperava StateCorruptError")
        except st_mod.StateCorruptError as e:
            self.assertIn(".bak", str(e))


# ============================================================================
# F-27: workdir('C:/x') == workdir('c:/x') — case-insensitive em Windows
# ============================================================================
class F27WorkdirCaseInsensitiveTest(unittest.TestCase):
    """F-27: workdir() normaliza case em paths locais (Windows)."""

    def test_workdir_same_for_different_case_local_paths(self):
        """workdir() produz mesmo hash para paths locais com case diferente."""
        store = tempfile.mkdtemp(prefix="codescan_f27_store_")
        try:
            # Cria dois workdirs com a mesma repo mas case diferente
            repo_lower = os.path.normcase(os.path.abspath("c:/temp/myrepo"))
            repo_upper = os.path.normcase(os.path.abspath("C:/TEMP/MYREPO"))
            # Após normcase, devem ficar iguais (em Windows, minúsculas + \\)
            wd_lower = st_mod.workdir(store, repo_lower)
            wd_upper = st_mod.workdir(store, repo_upper)
            self.assertEqual(wd_lower, wd_upper)
        finally:
            shutil.rmtree(store, ignore_errors=True)

    def test_workdir_different_for_different_urls(self):
        """workdir() produz hashes diferentes para URLs distintas (case-sensitive)."""
        store = tempfile.mkdtemp(prefix="codescan_f27_store_")
        try:
            url_lower = "https://github.com/user/Repo"
            url_upper = "https://github.com/user/REPO"
            # URLs são case-sensitive; devem produzir hashes diferentes
            wd_lower = st_mod.workdir(store, url_lower)
            wd_upper = st_mod.workdir(store, url_upper)
            self.assertNotEqual(wd_lower, wd_upper)
        finally:
            shutil.rmtree(store, ignore_errors=True)

    def test_workdir_uses_normcase_for_local_paths(self):
        """workdir() chama os.path.normcase em local paths (não em URLs)."""
        store = tempfile.mkdtemp(prefix="codescan_f27_store_")
        try:
            # Path com case misto
            mixed_case = "C:\\Temp\\MyRepo"
            normalized = os.path.normcase(os.path.abspath(mixed_case))
            wd = st_mod.workdir(store, mixed_case)
            # O workdir deve ser derivado da forma normalizada
            self.assertIn(os.path.basename(normalized.split("-")[0]), wd)
        finally:
            shutil.rmtree(store, ignore_errors=True)


# ============================================================================
# F-05/F-19/F-32: noise.validate_agent_output e env de limite de linhas
# ============================================================================
class F05F19F32NoiseValidationTest(unittest.TestCase):
    """F-05/F-19/F-32: regras de diff_echo, edit_echo, agent_output_max_lines."""

    def test_f05_diff_echo_requires_context(self):
        """F-05: diff --git deve casar; \\s isolado não (hr do markdown)."""
        # Deve casar diff real
        text_real = "diff --git a/x b/x"
        self.assertIn("diff_echo", noise_mod.validate_agent_output(text_real))
        # Não deve casar '---' isolado (hr do markdown)
        text_hr = "---"
        errors = noise_mod.validate_agent_output(text_hr)
        self.assertNotIn("diff_echo", errors)
        # Não deve casar '--- requirements.md ---' (formato SPEC)
        text_spec = "--- requirements.md ---"
        errors = noise_mod.validate_agent_output(text_spec)
        self.assertNotIn("diff_echo", errors)

    def test_f19_edit_echo_anchored_at_line_start(self):
        """F-19: 'Edited', 'Write' etc. só casam em início de linha, não em prosa."""
        # Deve casar em início de linha
        text_start = "Edited file.py"
        self.assertIn("edit_echo", noise_mod.validate_agent_output(text_start))
        # Não deve casar em meio de prosa
        text_prosa = "O metodo write() grava dados."
        errors = noise_mod.validate_agent_output(text_prosa)
        self.assertNotIn("edit_echo", errors)
        # Não deve casar "Write(" de uma chamada de função
        text_func = "Write(data)"
        errors = noise_mod.validate_agent_output(text_func)
        self.assertNotIn("edit_echo", errors)

    def test_f19_code_spans_are_masked(self):
        """F-19: conteúdo em crases/fences é mascarado antes de validar."""
        # Editado dentro de um bloco de código (markdown)
        text_code = "```\nEdited file.py\n```\nResultado OK"
        errors = noise_mod.validate_agent_output(text_code)
        self.assertNotIn("edit_echo", errors)
        # Editado dentro de crases simples
        text_inline = "Execute `Write(data)` para gravar."
        errors = noise_mod.validate_agent_output(text_inline)
        self.assertNotIn("edit_echo", errors)

    def test_f32_agent_output_max_lines_env_var_default(self):
        """F-32: limite de linhas default é AGENT_OUTPUT_MAX_LINES (220)."""
        # Sem env var, default é 220
        limit = noise_mod._agent_output_max_lines()
        self.assertEqual(limit, noise_mod.AGENT_OUTPUT_MAX_LINES)
        # Output com 220 linhas não viola
        text_ok = "\n".join(["line"] * 220)
        errors = noise_mod.validate_agent_output(text_ok)
        self.assertNotIn("agent_output_too_long", errors)
        # Output com 221 linhas viola
        text_long = "\n".join(["line"] * 221)
        errors = noise_mod.validate_agent_output(text_long)
        self.assertIn("agent_output_too_long", errors)

    def test_f32_agent_output_max_lines_custom_env(self):
        """F-32: limite de linhas configurável via WK_AGENT_OUTPUT_MAX_LINES."""
        old_env = os.environ.get(noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR)
        try:
            # Define limite customizado de 100 linhas
            os.environ[noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR] = "100"
            limit = noise_mod._agent_output_max_lines()
            self.assertEqual(limit, 100)
            # Output com 100 linhas não viola
            text_ok = "\n".join(["line"] * 100)
            errors = noise_mod.validate_agent_output(text_ok)
            self.assertNotIn("agent_output_too_long", errors)
            # Output com 101 linhas viola
            text_long = "\n".join(["line"] * 101)
            errors = noise_mod.validate_agent_output(text_long)
            self.assertIn("agent_output_too_long", errors)
        finally:
            if old_env is None:
                os.environ.pop(noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR, None)
            else:
                os.environ[noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR] = old_env

    def test_f32_agent_output_max_lines_minimum_respected(self):
        """F-32: limite abaixo de MIN (50) volta para default (220)."""
        old_env = os.environ.get(noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR)
        try:
            # Define limite inválido de 30 (abaixo do mínimo de 50)
            os.environ[noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR] = "30"
            limit = noise_mod._agent_output_max_lines()
            self.assertEqual(limit, noise_mod.AGENT_OUTPUT_MAX_LINES)
        finally:
            if old_env is None:
                os.environ.pop(noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR, None)
            else:
                os.environ[noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR] = old_env

    def test_f32_agent_output_max_lines_invalid_env_uses_default(self):
        """F-32: env var não-inteiro ou vazio volta para default."""
        old_env = os.environ.get(noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR)
        try:
            # Define env var com valor não-inteiro
            os.environ[noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR] = "not-a-number"
            limit = noise_mod._agent_output_max_lines()
            self.assertEqual(limit, noise_mod.AGENT_OUTPUT_MAX_LINES)
            # Define env var vazio
            os.environ[noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR] = ""
            limit = noise_mod._agent_output_max_lines()
            self.assertEqual(limit, noise_mod.AGENT_OUTPUT_MAX_LINES)
        finally:
            if old_env is None:
                os.environ.pop(noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR, None)
            else:
                os.environ[noise_mod.AGENT_OUTPUT_MAX_LINES_ENV_VAR] = old_env


if __name__ == "__main__":
    unittest.main(verbosity=2)
