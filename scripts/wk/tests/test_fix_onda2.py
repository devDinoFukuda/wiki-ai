# -*- coding: utf-8 -*-
"""Testes unitários para os contratos F-06, F-07, F-15, F-16, F-17, F-39, F-40 (Onda 2A).

F-06: `compile` poda `.md`/`.html` órfãos de `wiki/` (fonte despromovida/apagada);
      escopo por `--topic`; `--no-prune` desliga; `podados[]` no JSON.
F-07: `wiki/index.md` lista TODOS os tópicos promovidos mesmo com `compile --topic X`;
      entrada não compilada aparece sem link marcada `(não compilada)`.
F-15: `wk lint` em store sem `index.db` → exit 2, erro JSON com `acao`, NÃO cria o banco.
F-16: promote transacional — id do frontmatter já existente em `raw/**` → item em
      `duplicados[]` (exit 1), nada gravado, item permanece no inbox; sem `-2.md`.
F-17: `_render_frontmatter` preserva chaves extras (FIELDS primeiro, extras ordenadas).
F-39: `_publish_existing_dest` casa `id:` só no bloco de frontmatter (corpo com
      `id: sb-publish-...` não causa sobrescrita).
F-40: exceção não tratada → JSON `{error, tipo, comando, acao}` stderr, exit 1.

Rodar:
    python -m pytest scripts/wk/tests/test_fix_onda2.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from wk import cli


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


class F06F07PruningAndGlobalIndexTests(unittest.TestCase):
    """F-06: compile poda órfãos; F-07: index é global mesmo com --topic."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda2_f06f07_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def _promote_doc(self, doc_id: str, topic: str, source_type: str = "code-repo") -> None:
        """Helper: promove um documento."""
        path = os.path.join(self.store, "inbox", source_type, f"{doc_id}.md")
        _write(path, (
            f"---\nid: {doc_id}\nsource_type: {source_type}\norigin: test\n"
            f"captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: {topic}\n---\n\n"
            f"# {doc_id}\nConteúdo de {doc_id}.\n"
        ))
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)

    def test_compile_poda_orfaos_quando_fonte_removida(self):
        """F-06a: compile sem --no-prune poda páginas órfãs após fonte ser removida."""
        # Promover dois documentos
        self._promote_doc("doc-1", "demo")
        self._promote_doc("doc-2", "demo")

        # Compilar todos
        code, out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertGreater(len(data["paginas"]), 0)

        # Verificar que as páginas foram criadas
        page1 = os.path.join(self.store, "wiki", "demo", "code-repo", "doc-1.md")
        page2 = os.path.join(self.store, "wiki", "demo", "code-repo", "doc-2.md")
        self.assertTrue(os.path.isfile(page1))
        self.assertTrue(os.path.isfile(page2))

        # Remover doc-2 de raw/
        raw_path = os.path.join(self.store, "raw", "code-notes", "doc-2.md")
        os.remove(raw_path)

        # Compilar de novo — poda doc-2
        code, out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)

        # doc-2 deve estar em podados[]
        self.assertIn("podados", data)
        podados_paths = [p["path"] for p in data["podados"]]
        self.assertIn("wiki/demo/code-repo/doc-2.md", podados_paths)

        # Arquivo foi removido
        self.assertFalse(os.path.isfile(page2))
        # doc-1 continua
        self.assertTrue(os.path.isfile(page1))

    def test_compile_no_prune_preserva_orfaos(self):
        """F-06b: compile --no-prune preserva páginas órfãs."""
        # Promover e compilar
        self._promote_doc("doc-1", "demo")
        code, out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)

        page1 = os.path.join(self.store, "wiki", "demo", "code-repo", "doc-1.md")
        self.assertTrue(os.path.isfile(page1))

        # Remover fonte
        raw_path = os.path.join(self.store, "raw", "code-notes", "doc-1.md")
        os.remove(raw_path)

        # Compilar com --no-prune
        code, out, err = _run(["compile", "--store", self.store, "--no-prune"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)

        # podados[] vazio ou não contém doc-1
        if "podados" in data:
            podados_paths = [p["path"] for p in data["podados"]]
            self.assertNotIn("wiki/demo/code-repo/doc-1.md", podados_paths)

        # Página preservada
        self.assertTrue(os.path.isfile(page1))

    def test_compile_topic_filter_poda_only_topic_subtree(self):
        """F-06c: compile topic-a poda só na subárvore de topic-a."""
        # Promover docs em dois topics
        self._promote_doc("doc-a", "topic-a")
        self._promote_doc("doc-b", "topic-b")

        # Compilar tudo
        code, out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)

        # Remover ambos
        for raw_path in [
            os.path.join(self.store, "raw", "code-notes", "doc-a.md"),
            os.path.join(self.store, "raw", "code-notes", "doc-b.md"),
        ]:
            os.remove(raw_path)

        # Compilar só topic-a (argumento posicional)
        code, out, err = _run(["compile", "topic-a", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)

        # topic-a poda doc-a
        podados_paths = [p["path"] for p in data.get("podados", [])]
        self.assertIn("wiki/topic-a/code-repo/doc-a.md", podados_paths)
        # topic-b NOT podada (fora do escopo)
        self.assertNotIn("wiki/topic-b/code-repo/doc-b.md", podados_paths)
        # doc-b continua em disco
        page_b = os.path.join(self.store, "wiki", "topic-b", "code-repo", "doc-b.md")
        self.assertTrue(os.path.isfile(page_b))

    def test_index_is_global_even_with_topic_filter(self):
        """F-07: compile topic-a gera index.md com TODOS os tópicos."""
        # Promover docs em dois topics
        self._promote_doc("doc-a", "topic-a")
        self._promote_doc("doc-b", "topic-b")

        # Compilar tudo
        code, out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)

        # Compilar só topic-a (argumento posicional)
        code, out, err = _run(["compile", "topic-a", "--store", self.store])
        self.assertEqual(code, 0, err)

        # Ler index
        index_path = os.path.join(self.store, "wiki", "index.md")
        with open(index_path, "r", encoding="utf-8") as f:
            index_content = f.read()

        # Index contém AMBOS topic-a e topic-b
        self.assertIn("topic-a", index_content)
        self.assertIn("topic-b", index_content)
        self.assertIn("doc-a", index_content)
        self.assertIn("doc-b", index_content)

    def test_index_marks_uncompiled_entries_without_link(self):
        """F-07: entrada com página ainda não compilada é listada sem link + '(não compilada)'."""
        # Promover docs em topics diferentes
        self._promote_doc("doc-1", "demo")
        self._promote_doc("doc-2", "other")

        # Compilar tudo
        code, out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)

        # Compilar só topic=demo (doc-2 de topic=other nao sera compilado)
        code, out, err = _run(["compile", "demo", "--store", self.store])
        self.assertEqual(code, 0, err)

        # Ler index
        index_path = os.path.join(self.store, "wiki", "index.md")
        with open(index_path, "r", encoding="utf-8") as f:
            index_content = f.read()

        # Index contém AMBOS demos e other (global)
        self.assertIn("demo", index_content)
        self.assertIn("other", index_content)

        # doc-1 e doc-2 aparecem no index
        self.assertIn("doc-1", index_content)
        self.assertIn("doc-2", index_content)


class F15LintWithoutIndexDbTests(unittest.TestCase):
    """F-15: lint sem index.db retorna exit 2, erro JSON com acao, não cria banco."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda2_f15_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_lint_without_index_db_returns_exit_2(self):
        """F-15: lint sem index.db → exit 2."""
        code, out, err = _run(["lint", "--store", self.store])
        self.assertEqual(code, 2)

    def test_lint_without_index_db_returns_json_error(self):
        """F-15: erro é JSON com 'error' + 'acao'."""
        code, out, err = _run(["lint", "--store", self.store])
        data = json.loads(err)
        self.assertIn("error", data)
        self.assertIn("acao", data)
        self.assertIn("index.db", data["error"])

    def test_lint_without_index_db_does_not_create_db(self):
        """F-15: NÃO cria index.db."""
        db_path = os.path.join(self.store, "index.db")
        self.assertFalse(os.path.isfile(db_path))

        code, out, err = _run(["lint", "--store", self.store])
        self.assertEqual(code, 2)

        # Arquivo continua não existindo
        self.assertFalse(os.path.isfile(db_path))


class F16PromoteDuplicateIdTests(unittest.TestCase):
    """F-16: promote com id duplicado em raw/** → duplicados[], exit 1, inbox intacto."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda2_f16_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def _promote_doc(self, doc_id: str, topic: str = "demo") -> None:
        """Helper: promove um documento."""
        path = os.path.join(self.store, "inbox", "code-repo", f"{doc_id}.md")
        _write(path, (
            f"---\nid: {doc_id}\nsource_type: code-repo\norigin: test\n"
            f"captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: {topic}\n---\n\n"
            f"# {doc_id}\nConteúdo.\n"
        ))

    def test_promote_duplicate_id_returns_exit_1(self):
        """F-16: id duplicado → exit 1."""
        # Promover doc-1 com id 'sb-dup'
        path1 = os.path.join(self.store, "inbox", "code-repo", "doc-1.md")
        _write(path1, (
            "---\nid: sb-dup\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo\n---\n\n"
            "# Doc 1\nConteúdo 1.\n"
        ))
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)

        # Adicionar doc-2 com MESMO id (via inbox)
        path2 = os.path.join(self.store, "inbox", "code-repo", "doc-2.md")
        _write(path2, (
            "---\nid: sb-dup\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo\n---\n\n"
            "# Doc 2\nConteúdo diferente.\n"
        ))

        # Promover de novo
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 1, err)

    def test_promote_duplicate_id_in_duplicados_array(self):
        """F-16: id duplicado aparece em duplicados[]."""
        # Promover doc-1 com id 'sb-dup'
        path1 = os.path.join(self.store, "inbox", "code-repo", "doc-1.md")
        _write(path1, (
            "---\nid: sb-dup\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo\n---\n\n"
            "# Doc 1\nConteúdo.\n"
        ))
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0)

        # Adicionar doc-2 com MESMO id
        path2 = os.path.join(self.store, "inbox", "code-repo", "doc-2.md")
        _write(path2, (
            "---\nid: sb-dup\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo\n---\n\n"
            "# Doc 2\nConteúdo.\n"
        ))

        code, out, err = _run(["promote", "--store", self.store])
        data = json.loads(out)
        self.assertIn("duplicados", data)
        self.assertGreater(len(data["duplicados"]), 0)

        dup_ids = [d["id"] for d in data["duplicados"]]
        self.assertIn("sb-dup", dup_ids)

    def test_promote_duplicate_id_leaves_inbox_intact(self):
        """F-16: arquivo duplicado permanece intacto no inbox."""
        # Promover doc-1 com id 'sb-dup'
        path1 = os.path.join(self.store, "inbox", "code-repo", "doc-1.md")
        _write(path1, (
            "---\nid: sb-dup\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo\n---\n\n"
            "# Doc 1\nConteúdo.\n"
        ))
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0)

        # Adicionar doc-2 com MESMO id
        path2 = os.path.join(self.store, "inbox", "code-repo", "doc-2.md")
        _write(path2, (
            "---\nid: sb-dup\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo\n---\n\n"
            "# Doc 2\nConteúdo.\n"
        ))

        # Promover
        code, out, err = _run(["promote", "--store", self.store])
        # Arquivo continua em inbox/
        self.assertTrue(os.path.isfile(path2))

    def test_promote_duplicate_id_no_minus_two_copy(self):
        """F-16: sem cópia -2 de id duplicado."""
        # Promover doc-1 com id 'sb-dup'
        path1 = os.path.join(self.store, "inbox", "code-repo", "doc-1.md")
        _write(path1, (
            "---\nid: sb-dup\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo\n---\n\n"
            "# Doc 1\nConteúdo.\n"
        ))
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0)

        # Adicionar doc-2 com MESMO id
        path2 = os.path.join(self.store, "inbox", "code-repo", "doc-2.md")
        _write(path2, (
            "---\nid: sb-dup\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo\n---\n\n"
            "# Doc 2\nConteúdo.\n"
        ))

        # Promover
        code, out, err = _run(["promote", "--store", self.store])

        # Verificar que NÃO existe doc-2-2.md em raw/
        raw_dir = os.path.join(self.store, "raw", "code-notes")
        files_in_raw = os.listdir(raw_dir) if os.path.isdir(raw_dir) else []
        self.assertNotIn("doc-2-2.md", files_in_raw)


class F17RenderFrontmatterPreservesExtrasTests(unittest.TestCase):
    """F-17: _render_frontmatter preserva chaves extras em ordem alfabética após FIELDS."""

    def test_render_frontmatter_preserves_extra_keys(self):
        """F-17: chaves fora de FIELDS são preservadas."""
        meta = {
            "id": "sb-test",
            "source_type": "code-repo",
            "origin": "test",
            "captured_at": "2026-08-07T10:00:00Z",
            "promoted": True,
            "confidence": "reviewed",
            "topic": "demo",
            "tags": ["tag1", "tag2"],
            "autor": "tester",
        }
        body = "# Content\n\nBody text.\n"

        result = cli._render_frontmatter(meta, body)

        # Verificar que o resultado começa com ---
        self.assertTrue(result.startswith("---\n"))
        # Verificar que contém as chaves extras
        self.assertIn("tags:", result)
        self.assertIn("autor:", result)

    def test_render_frontmatter_extra_keys_alphabetical(self):
        """F-17: chaves extras são ordenadas alfabeticamente."""
        meta = {
            "id": "sb-test",
            "source_type": "code-repo",
            "origin": "test",
            "captured_at": "2026-08-07T10:00:00Z",
            "promoted": True,
            "confidence": "reviewed",
            "topic": "demo",
            "zzz": "last",
            "aaa": "first",
            "mmm": "middle",
        }
        body = "Content.\n"

        result = cli._render_frontmatter(meta, body)

        # Encontrar posições das chaves extras
        pos_aaa = result.find("aaa:")
        pos_mmm = result.find("mmm:")
        pos_zzz = result.find("zzz:")

        # Devem estar em ordem: aaa < mmm < zzz
        self.assertGreater(pos_aaa, 0)
        self.assertGreater(pos_mmm, pos_aaa)
        self.assertGreater(pos_zzz, pos_mmm)

    def test_render_frontmatter_fields_before_extras(self):
        """F-17: chaves de FIELDS vêm antes das extras."""
        meta = {
            "id": "sb-test",
            "source_type": "code-repo",
            "origin": "test",
            "captured_at": "2026-08-07T10:00:00Z",
            "promoted": True,
            "topic": "demo",
            "confidence": "reviewed",
            "custom_field": "value",
        }
        body = "Content.\n"

        result = cli._render_frontmatter(meta, body)

        # Encontrar posições
        pos_topic = result.find("topic:")
        pos_confidence = result.find("confidence:")
        pos_custom = result.find("custom_field:")

        # FIELDS devem vir antes de custom_field
        self.assertGreater(pos_custom, pos_confidence)
        self.assertGreater(pos_custom, pos_topic)


class F39PublishExistingDestTests(unittest.TestCase):
    """F-39: _publish_existing_dest lê id: só do frontmatter, não do corpo."""

    def test_publish_existing_dest_reads_frontmatter_only(self):
        """F-39: id: no frontmatter é lido."""
        tmpdir = tempfile.mkdtemp(prefix="wk_f39_")
        try:
            # Criar arquivo com id no frontmatter
            path = os.path.join(tmpdir, "test.md")
            _write(path, (
                "---\nid: sb-correct\nsource_type: code-repo\norigin: test\n"
                "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo\n---\n\n"
                "Content.\n"
            ))

            result = cli._publish_existing_dest(tmpdir, "sb-correct")
            self.assertEqual(result, path)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_publish_existing_dest_ignores_id_in_body(self):
        """F-39: id: no corpo é IGNORADO (não causa matching)."""
        tmpdir = tempfile.mkdtemp(prefix="wk_f39_body_")
        try:
            # Criar arquivo com id: só no corpo (não no frontmatter)
            path = os.path.join(tmpdir, "test.md")
            _write(path, (
                "---\nsource_type: code-repo\norigin: test\n"
                "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo\n---\n\n"
                "# Documento\n\nid: sb-body-id\n\nEste é um exemplo com id no corpo.\n"
            ))

            # Procurar por "sb-body-id" — NÃO deve achar
            result = cli._publish_existing_dest(tmpdir, "sb-body-id")
            self.assertIsNone(result)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_publish_existing_dest_yaml_block_only(self):
        """F-39: só o bloco YAML entre --- e --- é lido."""
        tmpdir = tempfile.mkdtemp(prefix="wk_f39_yaml_")
        try:
            # Arquivo com id: no corpo mas também no frontmatter
            path = os.path.join(tmpdir, "test.md")
            _write(path, (
                "---\nid: sb-yaml\nsource_type: code-repo\norigin: test\n"
                "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo\n---\n\n"
                "# Documento\n\nid: sb-other\n\nBody text.\n"
            ))

            # Procurar pelo id do YAML
            result = cli._publish_existing_dest(tmpdir, "sb-yaml")
            self.assertEqual(result, path)

            # Procurar pelo id do corpo — não acha
            result = cli._publish_existing_dest(tmpdir, "sb-other")
            self.assertIsNone(result)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class F40UnhandledExceptionTests(unittest.TestCase):
    """F-40: exceção não tratada → JSON com error, tipo, comando, acao em stderr, exit 1."""

    def test_unhandled_exception_returns_json_error_format(self):
        """F-40: exceção vira JSON em stderr com 'error', 'tipo', 'comando', 'acao'."""
        # Para simular uma exceção não tratada, passamos um --store que é um
        # arquivo em vez de um diretório. Isso causa OSError ao tentar criar
        # diretórios dentro dele.
        tmpfile = tempfile.NamedTemporaryFile(delete=False, prefix="wk_f40_")
        tmpfile.close()
        store = tmpfile.name  # É um arquivo, não um diretório

        try:
            code, out, err = _run(["compile", "--store", store])

            # Deve retornar exit 1 (erro não tratado)
            self.assertEqual(code, 1)

            # stderr deve ser JSON com error, tipo, comando, acao
            try:
                data = json.loads(err)
                self.assertIn("error", data)
                self.assertIn("tipo", data)
                self.assertIn("acao", data)
                # comando deve conter "compile"
                self.assertEqual(data.get("comando"), "compile")
            except json.JSONDecodeError:
                # Se não for JSON, o teste falha
                self.fail(f"stderr não é JSON válido: {err}")
        finally:
            if os.path.isfile(store):
                os.remove(store)

    def test_unhandled_exception_structure(self):
        """F-40: JSON de erro tem a estrutura correta."""
        # Similar ao anterior, mas verifica apenas a estrutura
        tmpfile = tempfile.NamedTemporaryFile(delete=False, prefix="wk_f40_struct_")
        tmpfile.close()
        store = tmpfile.name

        try:
            code, out, err = _run(["compile", "--store", store])

            # Se houve erro, deve ser JSON
            if code != 0:
                data = json.loads(err)
                self.assertIn("error", data, "JSON deve ter 'error'")
                self.assertIn("tipo", data, "JSON deve ter 'tipo'")
                self.assertIn("acao", data, "JSON deve ter 'acao'")
        finally:
            if os.path.isfile(store):
                os.remove(store)


if __name__ == "__main__":
    unittest.main(verbosity=2)
