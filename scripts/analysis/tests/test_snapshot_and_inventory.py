"""Testes para snapshot.py e inventory.py com mini-repo git."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from analysis.inventory import FileClass, Language, build
from analysis.snapshot import (
    FileEntry,
    FileState,
    Snapshot,
    SnapshotStale,
    capture,
    diff,
    resolve_evidence,
)


class SnapshotGitTest(unittest.TestCase):
    """Testes de captura com git (staged, unstaged, untracked, deleted)."""

    def setUp(self):
        """Cria mini-repo git temporário."""
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

    def test_capture_tracked_clean(self):
        """Arquivo rastreado e limpo entra na captura."""
        Path(self.repo, "clean.py").write_text("print('hello')\n")
        subprocess.run(["git", "add", "clean.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        self.assertEqual(len(snap.files), 1)
        self.assertEqual(snap.files[0].path, "clean.py")
        self.assertEqual(snap.files[0].state, FileState.TRACKED_CLEAN)

    def test_capture_staged(self):
        """Arquivo staged entra na captura."""
        Path(self.repo, "file.py").write_text("print('added')\n")
        subprocess.run(["git", "add", "file.py"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        self.assertEqual(len(snap.files), 1)
        self.assertEqual(snap.files[0].state, FileState.STAGED)

    def test_capture_unstaged(self):
        """Arquivo com mudanças unstaged entra na captura."""
        Path(self.repo, "edit.py").write_text("initial\n")
        subprocess.run(["git", "add", "edit.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo, check=True, capture_output=True)

        Path(self.repo, "edit.py").write_text("modified\n")
        snap = capture(self.repo)
        self.assertEqual(snap.files[0].state, FileState.UNSTAGED)

    def test_capture_untracked(self):
        """Arquivo não-rastreado (respeitando .gitignore) entra na captura."""
        Path(self.repo, "new.py").write_text("new file\n")
        snap = capture(self.repo)
        self.assertEqual(len(snap.files), 1)
        self.assertEqual(snap.files[0].state, FileState.UNTRACKED)

    def test_capture_deleted(self):
        """Arquivo deletado entra como DELETED, sem conteúdo."""
        Path(self.repo, "deleted.py").write_text("gone\n")
        subprocess.run(["git", "add", "deleted.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add deleted"], cwd=self.repo, check=True, capture_output=True)

        os.remove(os.path.join(self.repo, "deleted.py"))
        snap = capture(self.repo)

        entry = [e for e in snap.files if e.path == "deleted.py"][0]
        self.assertEqual(entry.state, FileState.DELETED)
        self.assertIsNone(entry.sha256)
        self.assertIsNone(entry.size)

    def test_snapshot_id_deterministic_same_content(self):
        """Mesmo conteúdo = mesmo snapshot_id, mesmo em HEADs diferentes."""
        Path(self.repo, "file.py").write_text("content\n")
        subprocess.run(["git", "add", "file.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit1"], cwd=self.repo, check=True, capture_output=True)

        snap1 = capture(self.repo)
        snap1_id = snap1.snapshot_id

        Path(self.repo, "other.txt").write_text("other\n")
        subprocess.run(["git", "add", "other.txt"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit2"], cwd=self.repo, check=True, capture_output=True)

        Path(self.repo, "file.py").write_text("content\n")  # reescreve idêntico
        snap2 = capture(self.repo, scope=["file.py"])

        self.assertEqual(snap1_id, snap2.snapshot_id)

    def test_snapshot_id_changes_with_dirty_worktree(self):
        """Worktree sujo (sem commit novo) muda snapshot_id porque conteúdo muda."""
        Path(self.repo, "file.py").write_text("original\n")
        subprocess.run(["git", "add", "file.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo, check=True, capture_output=True)

        snap1 = capture(self.repo)
        snap1_id = snap1.snapshot_id

        Path(self.repo, "file.py").write_text("modified\n")
        snap2 = capture(self.repo)
        snap2_id = snap2.snapshot_id

        self.assertNotEqual(snap1_id, snap2_id)

    def test_resolve_evidence_valid(self):
        """resolve_evidence() retorna locator validado + snippet."""
        content = "line 1\nline 2\nline 3\n"
        Path(self.repo, "code.py").write_text(content)
        subprocess.run(["git", "add", "code.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add code"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        result = resolve_evidence(snap, "code.py", 1, 2)

        self.assertIn("locator", result)
        self.assertIn("snippet", result)
        # normalize line endings (Windows may have \r\n)
        snippet_lines = result["snippet"].splitlines(keepends=True)
        self.assertEqual(len(snippet_lines), 2)
        self.assertTrue(snippet_lines[0].startswith("line 1"))
        self.assertTrue(snippet_lines[1].startswith("line 2"))
        self.assertEqual(result["locator"]["start_line"], 1)
        self.assertEqual(result["locator"]["end_line"], 2)

    def test_resolve_evidence_stale_raises(self):
        """resolve_evidence() levanta SnapshotStale se arquivo mudou após captura."""
        Path(self.repo, "code.py").write_text("original\n")
        subprocess.run(["git", "add", "code.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)

        Path(self.repo, "code.py").write_text("changed\n")

        with self.assertRaises(SnapshotStale):
            resolve_evidence(snap, "code.py", 1, 1)

    def test_diff_added_removed_changed(self):
        """diff() identifica adicionado, removido e modificado."""
        Path(self.repo, "file1.py").write_text("original1\n")
        subprocess.run(["git", "add", "file1.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo, check=True, capture_output=True)

        snap1 = capture(self.repo)

        os.remove(os.path.join(self.repo, "file1.py"))
        subprocess.run(["git", "add", "file1.py"], cwd=self.repo, check=True, capture_output=True)

        Path(self.repo, "file2.py").write_text("new\n")
        subprocess.run(["git", "add", "file2.py"], cwd=self.repo, check=True, capture_output=True)

        Path(self.repo, "file1.py").write_text("modified\n")
        subprocess.run(["git", "add", "file1.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "changes"], cwd=self.repo, check=True, capture_output=True)

        snap2 = capture(self.repo)

        result = diff(snap1, snap2)
        self.assertIn("file2.py", result["added"])
        self.assertIn("file1.py", result["changed"])


class SnapshotWithoutGitTest(unittest.TestCase):
    """Testes de captura sem git (fallback por walk+hash)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = self.tmpdir

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_capture_without_git_walks_directory(self):
        """Sem git, capture() faz walk + hash, gerando warning."""
        Path(self.repo, "file.py").write_text("python code\n")
        Path(self.repo, "other.txt").write_text("text\n")

        snap = capture(self.repo)
        self.assertEqual(len(snap.files), 2)
        self.assertFalse(snap.git_available)
        self.assertTrue(any("sem git" in w for w in snap.warnings))

    def test_capture_without_git_all_files_untracked(self):
        """Sem git, todos os arquivos são UNTRACKED."""
        Path(self.repo, "file.py").write_text("code\n")
        snap = capture(self.repo)
        self.assertEqual(snap.files[0].state, FileState.UNTRACKED)


class InventoryTest(unittest.TestCase):
    """Testes de inventory.py (classificação)."""

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

    def test_build_classifies_python_code(self):
        """Arquivo .py é classificado como CODE/PYTHON."""
        Path(self.repo, "script.py").write_text("print('hello')\n")
        subprocess.run(["git", "add", "script.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)

        self.assertEqual(len(inv.files), 1)
        self.assertEqual(inv.files[0].file_class, FileClass.CODE)
        self.assertEqual(inv.files[0].language, Language.PYTHON)

    def test_build_classifies_markdown_as_doc(self):
        """Arquivo .md é classificado como DOC com evidence_grade=False."""
        Path(self.repo, "README.md").write_text("# Title\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)

        file_class = [f for f in inv.files if f.path == "README.md"][0]
        self.assertEqual(file_class.file_class, FileClass.DOC)
        self.assertFalse(file_class.evidence_grade)

    def test_build_classifies_test_files(self):
        """Arquivo test_*.py é classificado como TEST."""
        Path(self.repo, "test_main.py").write_text("import unittest\n")
        subprocess.run(["git", "add", "test_main.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)

        test_file = [f for f in inv.files if f.path == "test_main.py"][0]
        self.assertEqual(test_file.file_class, FileClass.TEST)

    def test_build_classifies_config(self):
        """Arquivo .env é classificado como CONFIG."""
        Path(self.repo, ".env").write_text("DEBUG=true\n")
        subprocess.run(["git", "add", ".env"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)

        config_file = [f for f in inv.files if f.path == ".env"][0]
        self.assertEqual(config_file.file_class, FileClass.CONFIG)

    def test_build_excludes_excluded_directory(self):
        """Diretórios em _EXCLUDED_DIR_NAMES são excluídos explicitamente com Exclusion."""
        # Criar um arquivo dentro de node_modules (que é exclusão padrão)
        os.makedirs(os.path.join(self.repo, "node_modules"), exist_ok=True)
        Path(self.repo, "node_modules", "package.js").write_text("javascript\n")
        subprocess.run(["git", "add", "node_modules"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)

        # node_modules deve estar em exclusions, não em files
        self.assertTrue(any("node_modules" in e.path for e in inv.exclusions))

    def test_build_excludes_pycache(self):
        """Diretório __pycache__ é excluído."""
        os.makedirs(os.path.join(self.repo, "__pycache__"), exist_ok=True)
        Path(self.repo, "__pycache__", "module.pyc").write_text("bytecode\n")
        subprocess.run(["git", "commit", "--allow-empty", "-m", "initial"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)

        self.assertTrue(any("__pycache__" in e.path for e in inv.exclusions))

    def test_build_unsupported_creates_limitation(self):
        """Arquivo .unknown cria Limitation (não é silencioso)."""
        Path(self.repo, "file.unknown").write_text("unknown format\n")
        subprocess.run(["git", "add", "file.unknown"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)

        self.assertTrue(any(l.path == "file.unknown" for l in inv.limitations))

    def test_build_single_file_in_directory_included(self):
        """Um arquivo sozinho numa pasta é incluído (sem piso de tamanho)."""
        os.makedirs(os.path.join(self.repo, "src"), exist_ok=True)
        Path(self.repo, "src", "single.py").write_text("code\n")
        subprocess.run(["git", "add", "src/single.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=self.repo, check=True, capture_output=True)

        snap = capture(self.repo)
        inv = build(snap)

        self.assertTrue(any(f.path == "src/single.py" for f in inv.files))


if __name__ == "__main__":
    unittest.main()
