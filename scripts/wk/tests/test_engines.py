"""Testes da resolução de engines e roteamento de globais da CLI unificada."""

import contextlib
import io
import os
import unittest

from wk import cli


class TestResolveEngines(unittest.TestCase):
    def test_all_expande(self):
        engines, err = cli._resolve_engines("all")
        self.assertIsNone(err)
        self.assertEqual(engines, sorted(cli.ENGINES))

    def test_lista_com_virgula(self):
        engines, err = cli._resolve_engines("claude-code,copilot")
        self.assertIsNone(err)
        self.assertEqual(engines, ["claude-code", "copilot"])

    def test_desconhecida_reporta(self):
        engines, err = cli._resolve_engines("cursor")
        self.assertEqual(engines, [])
        self.assertIn("cursor", err)

    def test_dedup_diretorio_agents(self):
        # antigravity, devin, copilot compartilham .agents/skills
        dirs = cli._skill_dirs(["antigravity", "devin", "copilot"], base=".")
        self.assertEqual(len(set(dirs.values())), 1)

    def test_claude_separado(self):
        dirs = cli._skill_dirs(["claude-code", "devin"], base=".")
        self.assertEqual(len(set(dirs.values())), 2)


class TestSplitGlobals(unittest.TestCase):
    def test_extrai_repo_de_qualquer_posicao(self):
        rest, found = cli._split_globals(
            ["surface", "--repo", "/x", "--topic", "y"], ("repo", "store")
        )
        self.assertEqual(found["repo"], "/x")
        self.assertEqual(rest, ["surface", "--topic", "y"])

    def test_formato_igual(self):
        rest, found = cli._split_globals(["--store=/s", "audit"], ("store",))
        self.assertEqual(found["store"], "/s")
        self.assertEqual(rest, ["audit"])

    def test_store_default_env(self):
        old = os.environ.pop("WK_STORE", None)
        try:
            self.assertEqual(cli._store({}), "./store")
            os.environ["WK_STORE"] = "/custom"
            self.assertEqual(cli._store({}), "/custom")
        finally:
            os.environ.pop("WK_STORE", None)
            if old is not None:
                os.environ["WK_STORE"] = old


class TestCodeHelp(unittest.TestCase):
    def test_code_help_nao_exige_repo(self):
        old = os.environ.pop("WK_REPO", None)
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as cm:
                    cli.main(["code", "--help"])
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("Pipeline de codebase", stdout.getvalue())
            self.assertNotIn("informe --repo", stderr.getvalue())
        finally:
            if old is not None:
                os.environ["WK_REPO"] = old

    def test_code_subcomando_help_nao_exige_repo(self):
        old = os.environ.pop("WK_REPO", None)
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as cm:
                    cli.main(["code", "surface", "--help"])
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("--topic", stdout.getvalue())
            self.assertNotIn("informe --repo", stderr.getvalue())
        finally:
            if old is not None:
                os.environ["WK_REPO"] = old


if __name__ == "__main__":
    unittest.main()
