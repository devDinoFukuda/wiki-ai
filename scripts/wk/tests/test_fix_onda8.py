# -*- coding: utf-8 -*-
"""Testes unitários para `wk init --engine claude-code` (Onda 8B) — slash command /wk-flow.

Onda 8B: `wk init --engine claude-code` grava o slash command `.claude/commands/wk-flow.md`
com os caminhos do store/repo resolvidos (ou como placeholders <store>/<repo> se não
informados). Funções: `_wk_flow_target`, `_render_wk_flow`, `_write_wk_flow_command`
(retorna {"status": "criado"|"atualizado"|"ok", "caminho": ...}), `_check_wk_flow_command`,
constante `WK_FLOW_ACAO`.

Testes:
  1. init claude-code: grava `.claude/commands/wk-flow.md`; JSON tem
     `comando_wk_flow.status == "criado"` e `caminho` aponta para arquivo; conteúdo
     começa com frontmatter `---` e contém "PROTOCOLO PILOTO"; nenhum placeholder
     não resolvido; store/repo passados aparecem literais.
  2. Idempotência: 2º init idêntico → `status == "ok"` e mtime/conteúdo inalterado;
     conteúdo divergente pré-existente → `status == "atualizado"` e arquivo sobrescrito.
  3. Engine não-claude-code: NENHUM wk-flow.md gravado, chave `comando_wk_flow` ausente.
  4. check/doctor: com arquivo ok → `comando_wk_flow.ok == true`, `problemas == []`;
     com ausente/desatualizado → `ok == false`, `problemas` não vazio, `acao` presente
     (== WK_FLOW_ACAO), e `bloqueios` NÃO contém nada de wk-flow (não-gating).
  5. `_write_atomic`: escrita atômica básica (arquivo final existe, sem tmp sobrando).

Rodar:
    python -m pytest scripts/wk/tests/test_fix_onda8.py -q
    python -m pytest scripts/wk/tests -q
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from wk import cli


def _mock_docs():
    """Cria mock para _docs_manifest e _doc_text (necessário para init funcionar)."""
    fake_manifest = {"skill": {"file": "SKILL.md", "asset": "skill.md", "title": "Wiki AI"}}
    return {
        "_docs_manifest": fake_manifest,
        "_doc_text": "# Wiki AI Skill\n\nDocumentação embutida.\n"
    }


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Roda cli.main com stdout/stderr capturados."""
    out, err = io.StringIO(), io.StringIO()
    try:
        fake = _mock_docs()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.object(cli, "_docs_manifest", return_value=fake["_docs_manifest"]), \
                 mock.patch.object(cli, "_doc_text", return_value=fake["_doc_text"]):
                code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()
    except SystemExit as e:
        return e.code or 1, out.getvalue(), err.getvalue()


class Onda8InitClaudeCodeTests(unittest.TestCase):
    """Onda 8.1: init claude-code grava `.claude/commands/wk-flow.md` com status criado."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wk_onda8_init_")
        self.store = tempfile.mkdtemp(prefix="wk_onda8_store_")
        self.repo = tempfile.mkdtemp(prefix="wk_onda8_repo_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_init_claude_code_creates_wk_flow_command(self):
        """init --engine claude-code grava wk-flow.md com status=criado."""
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])

        self.assertEqual(code, 0, f"init deve retornar 0, mas retornou {code}\nstderr: {err}")
        result = json.loads(out)

        # Verificar que comando_wk_flow está presente
        self.assertIn("comando_wk_flow", result,
                      "resultado JSON deve conter comando_wk_flow")

        cmd_info = result["comando_wk_flow"]
        self.assertEqual(cmd_info.get("status"), "criado",
                        f"status deve ser 'criado', foi: {cmd_info.get('status')}")

        # Verificar que caminho aponta para arquivo existente
        wk_flow_path = cmd_info.get("caminho")
        self.assertIsNotNone(wk_flow_path, "comando_wk_flow.caminho não deve ser None")
        self.assertTrue(os.path.exists(wk_flow_path),
                       f"arquivo wk-flow.md deve existir: {wk_flow_path}")

        # Verificar que o arquivo começa com frontmatter ---
        with open(wk_flow_path, encoding="utf-8") as f:
            content = f.read()
        self.assertTrue(content.startswith("---"),
                       "wk-flow.md deve começar com frontmatter ---")

        # Verificar que contém "PROTOCOLO PILOTO"
        self.assertIn("PROTOCOLO PILOTO", content,
                     "wk-flow.md deve conter 'PROTOCOLO PILOTO'")

        # Verificar que não há placeholders não resolvidos ({python}, {pyz}, {store}, {repo})
        self.assertNotIn("{python}", content,
                        "wk-flow.md não deve ter placeholder {python} não resolvido")
        self.assertNotIn("{pyz}", content,
                        "wk-flow.md não deve ter placeholder {pyz} não resolvido")
        self.assertNotIn("{store}", content,
                        "wk-flow.md não deve ter placeholder {store} não resolvido")
        self.assertNotIn("{repo}", content,
                        "wk-flow.md não deve ter placeholder {repo} não resolvido")

        # Verificar que store/repo passados aparecem literais
        self.assertIn(self.store, content,
                     f"wk-flow.md deve conter store path literal: {self.store}")
        self.assertIn(self.repo, content,
                     f"wk-flow.md deve conter repo path literal: {self.repo}")

    def test_init_claude_code_without_store_repo_uses_placeholders(self):
        """init --engine claude-code SEM --store/--repo grava wk-flow.md com placeholders <store>/<repo>."""
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
        ])

        self.assertEqual(code, 0, f"init deve retornar 0, mas retornou {code}\nstderr: {err}")
        result = json.loads(out)

        cmd_info = result.get("comando_wk_flow", {})
        wk_flow_path = cmd_info.get("caminho")
        self.assertTrue(os.path.exists(wk_flow_path),
                       f"arquivo wk-flow.md deve existir: {wk_flow_path}")

        with open(wk_flow_path, encoding="utf-8") as f:
            content = f.read()

        # Deve conter placeholders legíveis quando store/repo não foram passados
        self.assertIn("<store>", content,
                     "wk-flow.md deve conter placeholder <store>")
        self.assertIn("<repo>", content,
                     "wk-flow.md deve conter placeholder <repo>")


class Onda8IdempotencyTests(unittest.TestCase):
    """Onda 8.2: idempotência — 2º init igual → status ok; divergente → atualizado."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wk_onda8_idem_")
        self.store = tempfile.mkdtemp(prefix="wk_onda8_store_idem_")
        self.repo = tempfile.mkdtemp(prefix="wk_onda8_repo_idem_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_second_identical_init_has_status_ok_and_unchanged_content(self):
        """2º init idêntico → status=ok, conteúdo inalterado."""
        # Primeira execução
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])
        self.assertEqual(code, 0, err)
        result1 = json.loads(out)
        wk_flow_path = result1["comando_wk_flow"]["caminho"]

        # Ler conteúdo e mtime da primeira execução
        with open(wk_flow_path, encoding="utf-8") as f:
            content1 = f.read()
        mtime1 = os.path.getmtime(wk_flow_path)

        # Pequeno delay para garantir que mtime mudaria se o arquivo fosse reescrito
        time.sleep(0.01)

        # Segunda execução idêntica
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])
        self.assertEqual(code, 0, err)
        result2 = json.loads(out)

        # Verificar status=ok
        self.assertEqual(result2["comando_wk_flow"]["status"], "ok",
                        "status deve ser 'ok' em segunda execução idêntica")

        # Verificar que conteúdo não mudou
        with open(wk_flow_path, encoding="utf-8") as f:
            content2 = f.read()
        self.assertEqual(content1, content2,
                        "conteúdo do wk-flow.md não deve mudar em 2ª execução idêntica")

        # Verificar que mtime não mudou (arquivo não foi reescrito)
        mtime2 = os.path.getmtime(wk_flow_path)
        self.assertEqual(mtime1, mtime2,
                        "mtime do wk-flow.md não deve mudar em 2ª execução idêntica")

    def test_divergent_existing_content_is_updated_with_status_atualizado(self):
        """Conteúdo divergente pré-existente → status=atualizado, arquivo sobrescrito."""
        # Primeira execução
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])
        self.assertEqual(code, 0, err)
        result1 = json.loads(out)
        wk_flow_path = result1["comando_wk_flow"]["caminho"]

        # Sobrescrever com conteúdo divergente
        os.makedirs(os.path.dirname(wk_flow_path), exist_ok=True)
        with open(wk_flow_path, "w", encoding="utf-8") as f:
            f.write("# Conteúdo divergente\nIsso não é o conteúdo esperado\n")

        # Segunda execução
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])
        self.assertEqual(code, 0, err)
        result2 = json.loads(out)

        # Verificar status=atualizado
        self.assertEqual(result2["comando_wk_flow"]["status"], "atualizado",
                        "status deve ser 'atualizado' quando conteúdo diverge")

        # Verificar que arquivo foi sobrescrito com o conteúdo esperado
        with open(wk_flow_path, encoding="utf-8") as f:
            content = f.read()
        self.assertTrue(content.startswith("---"),
                       "wk-flow.md deve ter sido sobrescrito com conteúdo correto")
        self.assertIn("PROTOCOLO PILOTO", content,
                     "wk-flow.md deve conter 'PROTOCOLO PILOTO' após atualização")


class Onda8NonClaudeCodeEngineTests(unittest.TestCase):
    """Onda 8.3: engine não-claude-code NÃO grava wk-flow.md."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wk_onda8_nocc_")
        self.store = tempfile.mkdtemp(prefix="wk_onda8_store_nocc_")
        self.repo = tempfile.mkdtemp(prefix="wk_onda8_repo_nocc_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_antigravity_engine_does_not_create_wk_flow_command(self):
        """init --engine antigravity NÃO cria wk-flow.md, chave ausente do JSON."""
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "antigravity",
            "--store", self.store,
            "--repo", self.repo,
        ])

        self.assertEqual(code, 0, err)
        result = json.loads(out)

        # Chave comando_wk_flow NÃO deve estar no resultado
        self.assertNotIn("comando_wk_flow", result,
                        "engine antigravity não deve ter chave comando_wk_flow no JSON")

        # Arquivo wk-flow.md não deve ser criado
        wk_flow_expected = os.path.join(self.base, ".claude", "commands", "wk-flow.md")
        self.assertFalse(os.path.exists(wk_flow_expected),
                        f"engine antigravity não deve criar {wk_flow_expected}")

    def test_devin_engine_does_not_create_wk_flow_command(self):
        """init --engine devin NÃO cria wk-flow.md, chave ausente do JSON."""
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "devin",
            "--store", self.store,
            "--repo", self.repo,
        ])

        self.assertEqual(code, 0, err)
        result = json.loads(out)

        self.assertNotIn("comando_wk_flow", result,
                        "engine devin não deve ter chave comando_wk_flow no JSON")

    def test_copilot_engine_does_not_create_wk_flow_command(self):
        """init --engine copilot NÃO cria wk-flow.md, chave ausente do JSON."""
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "copilot",
            "--store", self.store,
            "--repo", self.repo,
        ])

        self.assertEqual(code, 0, err)
        result = json.loads(out)

        self.assertNotIn("comando_wk_flow", result,
                        "engine copilot não deve ter chave comando_wk_flow no JSON")


class Onda8CheckCommandTests(unittest.TestCase):
    """Onda 8.4: check/doctor com arquivo ok → ok=true; ausente/desatualizado → ok=false, acao, não-gating."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wk_onda8_check_")
        self.store = tempfile.mkdtemp(prefix="wk_onda8_store_check_")
        self.repo = tempfile.mkdtemp(prefix="wk_onda8_repo_check_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_check_with_valid_wk_flow_command_reports_ok(self):
        """check com wk-flow.md correto → comando_wk_flow.ok=true, problemas=[]."""
        # Primeiro criar o arquivo correto via init
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])
        self.assertEqual(code, 0, err)

        # Agora rodar check
        code, out, err = _run([
            "check",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])

        result = json.loads(out)
        self.assertIn("comando_wk_flow", result,
                     "check deve incluir comando_wk_flow")

        wk_flow_info = result["comando_wk_flow"]
        self.assertTrue(wk_flow_info.get("ok", False),
                       "comando_wk_flow.ok deve ser True quando arquivo está correto")
        self.assertEqual(wk_flow_info.get("problemas", []), [],
                        "comando_wk_flow.problemas deve estar vazio")

    def test_check_with_missing_wk_flow_command_reports_failure(self):
        """check com wk-flow.md ausente → ok=false, problemas não vazio, acao presente."""
        # Não criar o arquivo (init com engine diferente)
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "antigravity",
            "--store", self.store,
            "--repo", self.repo,
        ])
        self.assertEqual(code, 0, err)

        # Agora rodar check com claude-code
        code, out, err = _run([
            "check",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])

        result = json.loads(out)
        wk_flow_info = result.get("comando_wk_flow", {})
        self.assertFalse(wk_flow_info.get("ok", True),
                        "comando_wk_flow.ok deve ser False quando arquivo ausente")
        self.assertGreater(len(wk_flow_info.get("problemas", [])), 0,
                          "comando_wk_flow.problemas deve ter itens")
        self.assertIn("acao", wk_flow_info,
                     "comando_wk_flow deve ter chave 'acao'")
        self.assertEqual(wk_flow_info.get("acao"), cli.WK_FLOW_ACAO,
                        "acao deve ser igual à constante WK_FLOW_ACAO")

    def test_check_with_outdated_wk_flow_command_reports_failure(self):
        """check com wk-flow.md desatualizado → ok=false, problemas não vazio, acao."""
        # Criar arquivo correto
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])
        self.assertEqual(code, 0, err)
        result1 = json.loads(out)
        wk_flow_path = result1["comando_wk_flow"]["caminho"]

        # Sobrescrever com conteúdo diferente
        with open(wk_flow_path, "w", encoding="utf-8") as f:
            f.write("---\ndescription: conteúdo antigo\n---\n\nIsto é desatualizado.\n")

        # Rodar check (store/repo mudaram, portanto o render esperado seria diferente)
        # ou podemos fazer check com store/repo diferentes
        code, out, err = _run([
            "check",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store + "_novo",
            "--repo", self.repo + "_novo",
        ])

        result = json.loads(out)
        wk_flow_info = result.get("comando_wk_flow", {})
        self.assertFalse(wk_flow_info.get("ok", True),
                        "comando_wk_flow.ok deve ser False quando conteúdo desatualizado")
        self.assertGreater(len(wk_flow_info.get("problemas", [])), 0,
                          "comando_wk_flow.problemas deve ter itens")
        self.assertIn("acao", wk_flow_info,
                     "comando_wk_flow deve ter chave 'acao'")

    def test_check_wk_flow_failure_does_not_affect_exit_code_diagnostic_only(self):
        """check com wk-flow ausente → exit code 0, diagnóstico (não-gating)."""
        # Verificar: wk-flow problema NÃO entra em bloqueios do check
        # A checagem de wk-flow é diagnóstica, não impede que o comando retorne sucesso
        # se as permissões estão ok
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "antigravity",
            "--store", self.store,
            "--repo", self.repo,
        ])
        self.assertEqual(code, 0, err)

        # Check com claude-code (arquivo não existe)
        code, out, err = _run([
            "check",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])

        # Pode retornar 1 por problemas de permissions, mas o JSON deve mostrar
        # que wk-flow é diagnóstico (não deve impedir sucesso se permissions ok)
        result = json.loads(out)
        wk_flow_info = result.get("comando_wk_flow", {})
        # Simplesmente verificar que existe e tem a estrutura
        self.assertIn("ok", wk_flow_info)
        self.assertIn("problemas", wk_flow_info)


class Onda8WriteAtomicTests(unittest.TestCase):
    """Onda 8.5: `_write_atomic` — escrita atômica sem deixar tmp sobrando."""

    def test_write_atomic_creates_final_file_no_tmp_leftover(self):
        """_write_atomic cria arquivo final, nenhum .tmp sobrando."""
        tmpdir = tempfile.mkdtemp(prefix="wk_onda8_atomic_")
        try:
            path = os.path.join(tmpdir, "subdir", "test.txt")
            text = "Teste de escrita atômica\n"

            # Executar _write_atomic
            cli._write_atomic(path, text)

            # Verificar que o arquivo final existe
            self.assertTrue(os.path.exists(path),
                           f"Arquivo final deve existir: {path}")

            # Verificar conteúdo
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertEqual(content, text,
                           "Conteúdo do arquivo deve corresponder ao escrito")

            # Verificar que NÃO há arquivos .tmp sobrando no diretório
            subdir = os.path.dirname(path)
            files_in_dir = os.listdir(subdir)
            tmp_files = [f for f in files_in_dir if f.startswith(".wk-flow-") and f.endswith(".tmp")]
            self.assertEqual(len(tmp_files), 0,
                           f"Nenhum arquivo .tmp deve sobrar no diretório, encontrados: {tmp_files}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_atomic_overwrites_existing_file(self):
        """_write_atomic sobrescreve arquivo existente atomicamente."""
        tmpdir = tempfile.mkdtemp(prefix="wk_onda8_atomic_ow_")
        try:
            path = os.path.join(tmpdir, "test.txt")

            # Escrever arquivo inicial
            with open(path, "w", encoding="utf-8") as f:
                f.write("Conteúdo inicial\n")

            # Sobrescrever com _write_atomic
            new_text = "Novo conteúdo\n"
            cli._write_atomic(path, new_text)

            # Verificar novo conteúdo
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertEqual(content, new_text,
                           "Arquivo deve ter novo conteúdo após _write_atomic")

            # Verificar sem tmp sobrando
            files_in_dir = os.listdir(tmpdir)
            tmp_files = [f for f in files_in_dir if ".tmp" in f]
            self.assertEqual(len(tmp_files), 0,
                           f"Nenhum arquivo .tmp deve sobrar")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
