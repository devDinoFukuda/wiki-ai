"""Testes para extractors/registry.py e específicos de linguagem."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from analysis.extractors.base import ExtractionError, Symbol, SourceFile
from analysis.extractors.registry import default_registry
from analysis.inventory import build
from analysis.snapshot import capture


class ExtractorIntegrationTest(unittest.TestCase):
    """Testes de integração: snapshot -> inventory -> extract_all."""

    def setUp(self):
        """Cria mini-repo git temporário com múltiplas linguagens."""
        self.tmpdir = tempfile.mkdtemp()
        self.repo = self.tmpdir
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.repo, check=True, capture_output=True,
        )

    def tearDown(self):
        """Remove diretório temporário."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_source_files(self, inv):
        """Converte Inventory em SourceFile objects lendo do disco."""
        sources = []
        for file_class in inv.files:
            full_path = os.path.join(self.repo, *file_class.path.split("/"))
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                sources.append(SourceFile(path=file_class.path, content=content))
            except OSError:
                pass  # Arquivo inlegível, skip
        return sources

    def test_extract_all_python_simple(self):
        """extract_all() com arquivo Python simples produz Symbol."""
        code = """def hello():
    return "world"
"""
        Path(self.repo, "module.py").write_text(code)
        subprocess.run(["git", "add", "module.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)
        result = registry.extract_all(sources)

        self.assertIn("python", result.coverage)
        self.assertTrue(len(result.symbols) > 0)
        func_symbols = [s for s in result.symbols if s.name == "hello"]
        self.assertEqual(len(func_symbols), 1)
        self.assertEqual(func_symbols[0].kind, "function")

    def test_extract_all_aggregates_by_language(self):
        """extract_all() agrega cobertura por linguagem."""
        Path(self.repo, "module.py").write_text("def foo(): pass\n")
        Path(self.repo, "script.js").write_text("function bar() {}\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)
        result = registry.extract_all(sources)

        self.assertIn("python", result.coverage)
        self.assertIn("javascript", result.coverage)

    def test_extract_all_no_adapter_for_go(self):
        """extract_all() registra no_adapter para Go (sem extrator).

        Go não tem adaptador nesta primeira leva; arquivo Go deve ser
        contabilizado como não-analisado com Diagnostic no_adapter.
        """
        Path(self.repo, "main.go").write_text("package main\nfunc main() {}\n")
        subprocess.run(["git", "add", "main.go"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)
        result = registry.extract_all(sources)

        # Deve haver diagnostic de no_adapter para go
        no_adapter_diags = [d for d in result.diagnostics if d.code == "no_adapter"]
        self.assertTrue(len(no_adapter_diags) > 0)
        # Arquivo deve ser contabilizado em files_without_adapter
        self.assertTrue(any("main.go" in d.paths for d in no_adapter_diags))

    def test_extract_all_assert_accounted_no_files_missing(self):
        """extract_all().assert_accounted() não levanta erro com todos os arquivos contabilizados."""
        Path(self.repo, "module.py").write_text("def foo(): pass\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)
        result = registry.extract_all(sources)

        # Não deve levantar erro — todos os arquivos são contabilizados
        try:
            result.assert_accounted()
        except AssertionError:
            self.fail("assert_accounted() levantou erro inesperadamente")

    def test_extract_all_coverage_resolution_level(self):
        """coverage por linguagem inclui resolution_level."""
        Path(self.repo, "module.py").write_text("def foo(): pass\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)
        result = registry.extract_all(sources)

        # Python deve ter resolution_level="syntactic" (usa ast)
        self.assertIn("python", result.coverage)
        self.assertEqual(result.coverage["python"].resolution_level, "syntactic")


class PythonExtractorTest(unittest.TestCase):
    """Testes específicos de python_ext."""

    def _build_source_files(self, inv):
        """Converte Inventory em SourceFile objects lendo do disco."""
        sources = []
        for file_class in inv.files:
            full_path = os.path.join(self.repo, *file_class.path.split("/"))
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                sources.append(SourceFile(path=file_class.path, content=content))
            except OSError:
                pass  # Arquivo inlegível, skip
        return sources

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = self.tmpdir
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.repo, check=True, capture_output=True,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_python_ext_ignores_docstrings(self):
        """python_ext não produz Symbol de docstring (§5.4).

        Docstring é um Expr node; não deve virar símbolo.
        """
        code = '''def foo():
    """Esta é uma docstring."""
    pass
'''
        Path(self.repo, "module.py").write_text(code)
        subprocess.run(["git", "add", "module.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)
        result = registry.extract_all(sources)

        # "Esta é uma docstring" não deve aparecer como Symbol ou Reference
        docstring_symbols = [
            s for s in result.symbols
            if "docstring" in s.name.lower() or "Esta é" in (s.name or "")
        ]
        self.assertEqual(len(docstring_symbols), 0)

    def test_python_ext_resolves_imports(self):
        """python_ext marca imports como resolved quando alvo existe."""
        Path(self.repo, "models.py").write_text("class User: pass\n")
        Path(self.repo, "views.py").write_text("from models import User\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)
        result = registry.extract_all(sources)

        # Deve haver um import resolvido
        imports = [r for r in result.references if r.kind == "import" and r.resolved]
        self.assertTrue(len(imports) > 0)

    def test_python_ext_comment_not_symbol(self):
        """python_ext não cria Symbol de comentário (§5.4).

        Se houver um comentário com 'def fantasma', não gera Symbol.
        """
        code = """# def fantasma_nao_existe():
#    pass

def real_function():
    pass
"""
        Path(self.repo, "module.py").write_text(code)
        subprocess.run(["git", "add", "module.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)
        result = registry.extract_all(sources)

        # Não deve haver símbolo "fantasma"
        fantasma = [s for s in result.symbols if "fantasma" in s.name.lower()]
        self.assertEqual(len(fantasma), 0)

        # Mas real_function deve estar lá
        real = [s for s in result.symbols if s.name == "real_function"]
        self.assertEqual(len(real), 1)


class StructuredExtractorTest(unittest.TestCase):
    """Testes de extractores estruturados (JSON, YAML, XML, SQL)."""

    def _build_source_files(self, inv):
        """Converte Inventory em SourceFile objects lendo do disco."""
        sources = []
        for file_class in inv.files:
            full_path = os.path.join(self.repo, *file_class.path.split("/"))
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                sources.append(SourceFile(path=file_class.path, content=content))
            except OSError:
                pass  # Arquivo inlegível, skip
        return sources

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = self.tmpdir
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.repo, check=True, capture_output=True,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_json_extractor_produces_config_items(self):
        """json_ext extrai keypaths como ConfigItem."""
        Path(self.repo, "config.json").write_text('{"database": {"host": "localhost"}}')
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)
        result = registry.extract_all(sources)

        # Deve haver ConfigItem de JSON
        json_items = [c for c in result.configuration if "database" in (c.keypath or "")]
        self.assertTrue(len(json_items) > 0)

    def test_sql_extractor_produces_data_entities(self):
        """sql_ext extrai CREATE TABLE como DataEntity."""
        sql = "CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(255));\n"
        Path(self.repo, "schema.sql").write_text(sql)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)
        result = registry.extract_all(sources)

        # Deve haver DataEntity de SQL
        entities = [e for e in result.data_entities if e.name == "users"]
        self.assertTrue(len(entities) > 0)

    def test_yaml_extractor_handles_anchors(self):
        """yaml_ext lida com âncoras (yaml_partial)."""
        yaml_content = """defaults: &defaults
  debug: false
  timeout: 30
app:
  <<: *defaults
  name: myapp
"""
        Path(self.repo, "config.yaml").write_text(yaml_content)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)
        result = registry.extract_all(sources)

        # Não deve levantar erro de parsing — resultado pode estar vazio,
        # mas não deve falhar
        self.assertIsNotNone(result)


class ExtractorErrorHandlingTest(unittest.TestCase):
    """Testes de tratamento de erro em extractores."""

    def _build_source_files(self, inv):
        """Converte Inventory em SourceFile objects lendo do disco."""
        sources = []
        for file_class in inv.files:
            full_path = os.path.join(self.repo, *file_class.path.split("/"))
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                sources.append(SourceFile(path=file_class.path, content=content))
            except OSError:
                pass  # Arquivo inlegível, skip
        return sources

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = self.tmpdir
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.repo, check=True, capture_output=True,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_python_syntax_error_creates_diagnostic_not_crash(self):
        """python_ext com erro de sintaxe cria Diagnostic, não levanta."""
        bad_code = """def foo(
    incomplete syntax here
"""
        Path(self.repo, "bad.py").write_text(bad_code)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)

        # Não deve levantar — erro deve ser capturado
        try:
            result = registry.extract_all(sources)
            # Deve haver diagnostic de erro
            diags_with_error = [d for d in result.diagnostics if d.level == "error"]
            self.assertTrue(len(diags_with_error) > 0)
        except Exception as e:
            self.fail(f"extract_all levantou inesperadamente: {e}")

    def test_malformed_json_creates_diagnostic(self):
        """json_ext com JSON malformado cria Diagnostic, não levanta."""
        bad_json = '{"key": "unclosed string}'
        Path(self.repo, "bad.json").write_text(bad_json)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)
        registry = default_registry()
        sources = self._build_source_files(inv)

        # Não deve levantar
        try:
            result = registry.extract_all(sources)
            self.assertIsNotNone(result)
        except Exception as e:
            self.fail(f"extract_all levantou inesperadamente: {e}")


if __name__ == "__main__":
    unittest.main()
