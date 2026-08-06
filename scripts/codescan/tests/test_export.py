"""Testes do export determinístico (inventory.md / dependencies.md)."""

import os
import unittest

from codescan import export as ex


SURFACE = {
    "repo": "/tmp/acme-api",
    "scanned_at": "2026-07-16T12:00:00Z",
    "total_files": 42,
    "total_loc": 12345,
    "languages": {
        "C#": {"files": 30, "loc": 10000},
        "SQL": {"files": 12, "loc": 2345},
    },
    "manifests": [
        {
            "path": "src/Api/Api.csproj",
            "type": "msbuild",
            "dependencies": ["Newtonsoft.Json@13.0.3", "proj:../Core/Core.csproj"],
        },
        {"path": "tools/package.json", "type": "npm", "dependencies": []},
    ],
    "entry_points": [{"path": "src/Api/Program.cs", "reason": "entry point .NET"}],
    "modules": [
        {
            "path": "src/Api",
            "files": 30,
            "loc": 10000,
            "languages": ["C#"],
            "commits": 57,
            "authors": 4,
            "last_commit": "2026-07-01T00:00:00Z",
        },
        {
            "path": "db",
            "files": 12,
            "loc": 2345,
            "languages": ["SQL"],
            "commits": None,
            "authors": None,
            "last_commit": None,
        },
    ],
    "git": {
        "available": True,
        "head": "a1b2c3d",
        "branch": "main",
        "last_commit": "2026-07-01T00:00:00Z",
        "total_commits": 321,
    },
    "skipped": {"generated": 3},
    "warnings": ["nenhum manifest encontrado — dependências desconhecidas"],
}


class TestRender(unittest.TestCase):
    def test_inventory_frontmatter_e_conteudo(self):
        md = ex.render_inventory(SURFACE, topic="pagamentos")
        self.assertTrue(md.startswith("---\n"), "frontmatter deve abrir o arquivo")
        self.assertIn("source_type: code-repo", md)
        self.assertIn("id: sb-codescan-acme-api-inventory", md)
        self.assertIn("topic: pagamentos", md)
        self.assertIn("@a1b2c3d", md)
        self.assertIn("| C# | 30 | 10.000 |", md)
        self.assertIn("`src/Api/Program.cs`", md)
        # módulo sem git: colunas com travessão, não crash
        self.assertIn("| `db` | 12 | 2.345 | SQL | — | — | — |", md)

    def test_inventory_sem_git(self):
        s = dict(SURFACE, git={"available": False})
        md = ex.render_inventory(s)
        self.assertIn("Git: indisponível", md)
        self.assertNotIn("topic:", md.split("---")[1])

    def test_dependencies(self):
        md = ex.render_dependencies(SURFACE, topic="pagamentos")
        self.assertIn("source_type: code-repo", md)
        self.assertIn("## `src/Api/Api.csproj` (msbuild)", md)
        self.assertIn("- `Newtonsoft.Json@13.0.3`", md)
        self.assertIn("_Sem dependências declaradas", md)

    def test_dependencies_sem_manifest(self):
        s = dict(SURFACE, manifests=[])
        md = ex.render_dependencies(s)
        self.assertIn("Nenhum manifest encontrado", md)


class TestExportFiles(unittest.TestCase):
    def test_escreve_arquivos(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            outdir = os.path.join(tmp, "sdd")
            paths = ex.export_deterministic(SURFACE, outdir, topic="pagamentos")
            self.assertEqual(len(paths), 2)
            for p in paths:
                self.assertTrue(os.path.isfile(p), p)
                with open(p, encoding="utf-8") as f:
                    self.assertTrue(f.read().startswith("---\n"), p)


class TestExportOutputGuardrail(unittest.TestCase):
    """`export --output` não pode furar o guardrail raw/ só muda via promote:
    escrever direto em <store>/raw ou <store>/wiki precisa ser recusado, e o
    fluxo legítimo (inbox/) precisa continuar funcionando."""

    def _run(self, argv: list[str]):
        import contextlib
        import io

        from codescan.cli import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def _surfaced_repo(self, tmp: str) -> tuple[str, str]:
        repo = os.path.join(tmp, "repo")
        store = os.path.join(tmp, "store")
        os.makedirs(os.path.join(repo, "src"), exist_ok=True)
        with open(os.path.join(repo, "src", "a.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        code, _out, err = self._run(["--store", store, "--repo", repo, "surface", "--module-min-files", "1"])
        assert code == 0, err
        return repo, store

    def test_export_output_recusa_destino_dentro_de_raw(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo, store = self._surfaced_repo(tmp)
            bad_output = os.path.join(store, "raw", "codebases", "test")

            code, _out, err = self._run(["--store", store, "--repo", repo, "export", "--output", bad_output])

            self.assertEqual(code, 2)
            self.assertIn("raw/", err)
            self.assertIn("inbox/", err)
            self.assertIn("wk publish", err)
            self.assertFalse(os.path.isdir(bad_output))

    def test_export_output_recusa_destino_dentro_de_wiki(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo, store = self._surfaced_repo(tmp)
            bad_output = os.path.join(store, "wiki", "codebases", "test")

            code, _out, err = self._run(["--store", store, "--repo", repo, "export", "--output", bad_output])

            self.assertEqual(code, 2)
            self.assertIn("wiki/", err)
            self.assertIn("inbox/", err)
            self.assertFalse(os.path.isdir(bad_output))

    def test_export_output_aceita_destino_em_inbox(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo, store = self._surfaced_repo(tmp)
            good_output = os.path.join(store, "inbox", "code-notes", "test")

            code, _out, err = self._run(["--store", store, "--repo", repo, "export", "--output", good_output])

            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.isfile(os.path.join(good_output, "inventory.md")))

    def test_export_output_default_workdir_sdd_continua_funcionando(self):
        import tempfile

        from codescan import state as st_mod

        with tempfile.TemporaryDirectory() as tmp:
            repo, store = self._surfaced_repo(tmp)
            wd = st_mod.workdir(store, repo)

            code, _out, err = self._run(["--store", store, "--repo", repo, "export"])

            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.isfile(os.path.join(wd, "sdd", "inventory.md")))


if __name__ == "__main__":
    unittest.main()
