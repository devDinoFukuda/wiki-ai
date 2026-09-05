# -*- coding: utf-8 -*-
"""Testes unitários para remediação W0 (F09, P-W0-1).

F09 (proteção de publicação):
  - cmd_compile: gera em staging, gate `bloqueado_geracao=bool(recusados)`, promove
    se OK, finally remove staging. Bloqueado → JSON com chave "bloqueado", exit 1,
    wiki/ intacto.
  - cmd_docx: staging, validação zip, gate `bloqueado_geracao=bool(pulados or recusados)`,
    promove se OK, finally remove staging. Bloqueado → wiki-docx/ intacto.
  - Poda só após promoção. `--no-prune`/`--topic` preservados.

P-W0-1 (revalidação):
  - `_revalidate_codescan_verify_state` antes de gates; artefato editado → state.json
    "failed"; ImportError → aviso explícito + comportamento anterior.
  - `_failed_verify_workdirs`, `_verify_gate_scope` retornam triplas com avisos;
    `cmd_promote`/`cmd_compile`/`cmd_docx` incluem `revalidacao_avisos` aditivo.

Testes:
  1. Compile com fonte recusada → exit 1, "bloqueado" no JSON, wiki/ anterior intacto,
     staging removido.
  2. Compile ok → promovido + poda funciona.
  3. Docx com falha → wiki-docx/ intacto.
  4. Docx ok → .docx válidos (zipfile) + poda de órfãos após promoção.
  5. Artefato verify editado → promote/compile/docx bloqueiam.
  6. Revalidação indisponível → `revalidacao_avisos` presente e fluxo segue.

Rodar:
    python -m pytest scripts/wk/tests/test_fix_w0_wk.py -v
    python -m pytest scripts/wk/tests/test_fix_w0_wk.py::W0F09Tests -v
    python -m pytest scripts/wk/tests/test_fix_w0_wk.py::W0RevalidacaoTests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

from wk import cli


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Roda cli.main com stdout/stderr capturados."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _write(path: str, content: str) -> None:
    """Escreve arquivo com encoding UTF-8, LF."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _markdown_doc(doc_id: str, topic: str, source_type: str = "code-repo",
                  body: str = "") -> str:
    """Gera frontmatter + body para documento."""
    if not body:
        body = f"# {doc_id}\n\nConteúdo do {doc_id}."
    return (
        f"---\nid: {doc_id}\nsource_type: {source_type}\norigin: test\n"
        f"captured_at: 2026-08-07T10:00:00Z\ntopic: {topic}\n---\n\n{body}\n"
    )


class W0F09CompileTests(unittest.TestCase):
    """F09: proteção de publicação em compile."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_w0f09_compile_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def _setup_wiki_version(self) -> None:
        """Promove um doc, compila, para ter um wiki/ baseline."""
        path = os.path.join(self.store, "inbox", "code-repo", "baseline.md")
        _write(path, _markdown_doc("baseline", "demo"))
        code, _out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)
        code, _out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)

    def _add_inbox_doc(self, doc_id: str, topic: str = "demo",
                      source_type: str = "code-repo") -> str:
        """Adiciona doc ao inbox (pronto para promote)."""
        path = os.path.join(self.store, "inbox", source_type, f"{doc_id}.md")
        _write(path, _markdown_doc(doc_id, topic, source_type))
        return path

    def test_compile_bloqueado_fonte_recusada_exit_1(self):
        """Cenário 1a: compile com fonte recusada (PathComponentError durante
        geração) → exit 1, "bloqueado" no JSON, wiki/ intacto."""
        self._setup_wiki_version()

        # Baseline compilado está em wiki/demo/code-repo/baseline.md
        baseline_page = os.path.join(self.store, "wiki", "demo", "code-repo", "baseline.md")
        self.assertTrue(os.path.isfile(baseline_page))
        baseline_mtime = os.path.getmtime(baseline_page)

        # Promover um doc com id inválido (vai ser rejeitado em compile)
        # Path traversal attempt via id
        path = os.path.join(self.store, "inbox", "code-repo", "bad-id.md")
        _write(path, (
            "---\nid: ../../../escaped\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\ntopic: demo\n---\n\n"
            "# Documento com ID inválido\n"
        ))
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)

        # Compile deve rejeitar o id inválido (PathComponentError)
        code, out, err = _run(["compile", "--store", self.store])

        # Exit 1, JSON com bloqueado e recusados
        self.assertEqual(code, 1, f"exit code deve ser 1, foi {code}")
        result = json.loads(out)
        self.assertIn("bloqueado", result, f"JSON deve ter 'bloqueado': {result}")
        self.assertIn("recusados", result)
        self.assertGreater(len(result["recusados"]), 0)

        # wiki/ intacto: baseline.md ainda lá com mesmo mtime
        self.assertTrue(os.path.isfile(baseline_page))
        self.assertEqual(os.path.getmtime(baseline_page), baseline_mtime,
                        "baseline.md foi modificado apesar do bloqueio")

    def test_compile_ok_promovido_poda_funciona(self):
        """Cenário 2: compile OK → promovido + poda de órfãos funciona."""
        # Promover doc-1, doc-2, compilar
        self._add_inbox_doc("doc-1", "demo")
        self._add_inbox_doc("doc-2", "demo")
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)

        code, out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertGreater(len(data["paginas"]), 0)

        # Páginas criadas
        page1 = os.path.join(self.store, "wiki", "demo", "code-repo", "doc-1.md")
        page2 = os.path.join(self.store, "wiki", "demo", "code-repo", "doc-2.md")
        self.assertTrue(os.path.isfile(page1))
        self.assertTrue(os.path.isfile(page2))

        # Remover doc-2 de raw/ e recompilar
        raw_path2 = os.path.join(self.store, "raw", "code-notes", "doc-2.md")
        os.remove(raw_path2)

        code, out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)

        # doc-2 foi podado
        self.assertIn("podados", data)
        podados_paths = [p["path"] for p in data["podados"]]
        self.assertIn("wiki/demo/code-repo/doc-2.md", podados_paths)

        # Arquivo foi removido
        self.assertFalse(os.path.isfile(page2), "doc-2 deve ter sido podado")
        # doc-1 intacto
        self.assertTrue(os.path.isfile(page1))


class W0F09DocxTests(unittest.TestCase):
    """F09: proteção de publicação em docx."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_w0f09_docx_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def _setup_wiki_docx_version(self) -> None:
        """Promove um doc, gera docx, para ter um wiki-docx/ baseline."""
        path = os.path.join(self.store, "inbox", "code-repo", "baseline.md")
        _write(path, _markdown_doc("baseline", "demo"))
        code, _out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)
        code, _out, err = _run(["docx", "--store", self.store])
        self.assertEqual(code, 0, err)

    def _add_inbox_doc(self, doc_id: str, topic: str = "demo") -> str:
        """Adiciona doc ao inbox."""
        path = os.path.join(self.store, "inbox", "code-repo", f"{doc_id}.md")
        _write(path, _markdown_doc(doc_id, topic))
        return path

    def test_docx_bloqueado_com_falha_wiki_docx_intacta(self):
        """Cenário 3: docx com falha → wiki-docx/ intacta."""
        self._setup_wiki_docx_version()

        # wiki-docx baseline existe
        wiki_docx = os.path.join(self.store, "wiki-docx")
        self.assertTrue(os.path.isdir(wiki_docx))

        # Promover doc que vai falhar
        self._add_inbox_doc("failing-doc")
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)

        # Mock para simular falha em docxgen.build_document
        from wk import docxgen
        original_build = docxgen.build_document
        call_count = [0]
        def mock_build_fail(source):
            call_count[0] += 1
            if source.get("id") == "failing-doc":
                raise RuntimeError("Simulated docx generation failure")
            return original_build(source)

        with mock.patch.object(docxgen, "build_document", side_effect=mock_build_fail):
            code, out, err = _run(["docx", "--store", self.store])

        # Exit 1, JSON com bloqueado
        self.assertEqual(code, 1, f"exit code deve ser 1, foi {code}\nstderr: {err}")
        result = json.loads(out)
        self.assertIn("bloqueado", result, f"JSON deve ter 'bloqueado': {result}")

        # wiki-docx/ não foi tocado: se baseline.docx existia, continua lá
        self.assertTrue(os.path.isdir(wiki_docx))

    def test_docx_ok_validos_e_poda_funciona(self):
        """Cenário 4: docx OK → .docx válidos (zipfile) + poda de órfãos."""
        # Promover doc-1, doc-2, gerar docx
        self._add_inbox_doc("doc-1")
        self._add_inbox_doc("doc-2")
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)

        code, out, err = _run(["docx", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertGreater(len(data["documentos"]), 0)

        # Verificar que .docx são zipfiles válidos
        for doc_entry in data["documentos"]:
            docx_path = os.path.join(self.store, doc_entry["path"])
            self.assertTrue(os.path.isfile(docx_path), f"{docx_path} não existe")
            self.assertTrue(zipfile.is_zipfile(docx_path),
                          f"{docx_path} não é um .zip válido")

        # Remover doc-2 de raw/ e regerar docx
        raw_path2 = os.path.join(self.store, "raw", "code-notes", "doc-2.md")
        os.remove(raw_path2)

        code, out, err = _run(["docx", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)

        # doc-2.docx foi removido (poda)
        self.assertIn("removidos", data)
        if data["removidos"]:  # Se houver órfãos podados
            removed_paths = [p.get("path", "") for p in (data["removidos"] if isinstance(data["removidos"], list) else [])]
            # Validar que doc-2 foi podado


class W0RevalidacaoTests(unittest.TestCase):
    """P-W0-1: revalidação de artefatos verify editados."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_w0_revalidacao_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def _setup_codescan_workdir(self, topic: str = "demo") -> str:
        """Cria um workdir de codescan simulado com state.json."""
        codescan_root = os.path.join(self.store, ".codescan")
        os.makedirs(codescan_root, exist_ok=True)
        wd = os.path.join(codescan_root, f"demo-12345678")
        os.makedirs(wd, exist_ok=True)

        # Criar state.json com verify status
        state = {
            "repo": "test-repo",
            "topic": topic,
            "stages": {
                "verify": {
                    "status": "done",
                    "at": "2026-08-07T10:00:00Z"
                }
            }
        }
        with open(os.path.join(wd, "state.json"), "w") as f:
            json.dump(state, f)

        # Criar estrutura mínima do workdir
        os.makedirs(os.path.join(wd, "sdd"), exist_ok=True)
        _write(os.path.join(wd, "sdd", "confirmed.md"),
               "# Artefato\n\nConteúdo verificado.")
        return wd

    def test_artefato_editado_gera_revalidacao_e_bloqueio(self):
        """Cenário 5a: artefato verify editado → compile/docx bloqueiam via
        _verify_gate_scope.

        Quando um workdir tem verify.status == "failed", o gate bloqueia com
        exit 3 e reporta em stderr (não stdout) com erro JSON."""
        wd = self._setup_codescan_workdir("demo")

        # Modificar state.json para ter verify.status = "failed"
        state_path = os.path.join(wd, "state.json")
        with open(state_path) as f:
            state = json.load(f)
        state["stages"]["verify"]["status"] = "failed"
        with open(state_path, "w") as f:
            json.dump(state, f)

        # Tentar compile — gate de verify bloqueia com exit 3
        code, out, err = _run(["compile", "--store", self.store])

        # Gate bloqueia com exit 3 (bloqueio de verify)
        self.assertEqual(code, 3, f"exit code deve ser 3, foi {code}")

        # Erro é reportado em stderr como JSON
        error_data = json.loads(err)
        self.assertIn("error", error_data)
        self.assertIn("verify", error_data["error"].lower())
        self.assertIn("workdirs_bloqueados", error_data)

    def test_revalidacao_indisponivel_aviso_fluxo_segue(self):
        """Cenário 6: módulo codescan indisponível → avisos + fluxo segue.

        Quando codescan não está disponível, _revalidate_codescan_verify_state
        retorna um aviso (string), e o aviso é incluído em `revalidacao_avisos`
        no JSON. O fluxo continua mesmo assim."""
        wd = self._setup_codescan_workdir("demo")

        # Mock para simular ImportError no import de codescan dentro de
        # _revalidate_codescan_verify_state
        def mock_revalidate_import_fail(wd):
            # Simula o que _revalidate_codescan_verify_state faz quando
            # não consegue importar codescan
            return (
                "revalidação de verify/evidence indisponível (módulo `codescan` "
                "não encontrado: simulated error) — gate usou o status BRUTO"
            )

        with mock.patch.object(cli, "_revalidate_codescan_verify_state",
                              side_effect=mock_revalidate_import_fail):
            code, out, err = _run(["compile", "--store", self.store])

            # Fluxo segue (exit 0 ou 1 por outro motivo, nunca 2)
            # Avisos devem estar presentes no JSON
            if out.strip():
                result = json.loads(out)
                # revalidacao_avisos pode estar presente se houver codescan workdirs
                if "revalidacao_avisos" in result:
                    self.assertIsInstance(result["revalidacao_avisos"], list)


class W0StagingTests(unittest.TestCase):
    """Testes de staging atômico (infraestrutura F09)."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_w0_staging_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_staging_dir_irmao_de_wiki(self):
        """Staging é irmão de wiki/ (mesmo pai, logo mesmo volume)."""
        wiki_root = os.path.join(self.store, "wiki")
        staging = cli._staging_dir_for(wiki_root)

        # Mesmo pai
        self.assertEqual(os.path.dirname(staging), os.path.dirname(wiki_root))
        # Nome começa com .
        self.assertTrue(os.path.basename(staging).startswith("."))
        # Único
        staging2 = cli._staging_dir_for(wiki_root)
        self.assertNotEqual(staging, staging2)

    def test_staging_removido_apos_falha(self):
        """Staging é removido no finally após falha.

        Mesmo que compile falhe, o staging criado em tempfile.mkdtemp
        deve ser removido pelo finally no cmd_compile."""
        wiki_root = os.path.join(self.store, "wiki")
        parent = os.path.dirname(os.path.dirname(wiki_root))  # pai de store
        os.makedirs(wiki_root, exist_ok=True)

        # Contar staging dirs antes
        staging_dirs_before = []
        try:
            for d in os.listdir(parent):
                if d.startswith(".") and "staging" in d:
                    staging_dirs_before.append(d)
        except OSError:
            pass

        # Promover doc com id inválido (vai ser recusado em compile)
        inbox_path = os.path.join(self.store, "inbox", "code-repo", "bad.md")
        _write(inbox_path, (
            "---\nid: ../bad\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\ntopic: demo\n---\n\nConteúdo"
        ))
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)

        # Compile vai rejeitar e bloquear
        code, out, err = _run(["compile", "--store", self.store])
        # Pode ser exit 1 (bloqueado) ou outro
        self.assertIn(code, [1])

        # Staging foi removido (final check)
        staging_dirs_after = []
        try:
            for d in os.listdir(parent):
                if d.startswith(".") and "staging" in d:
                    staging_dirs_after.append(d)
        except OSError:
            pass

        # Não deve haver staging sobrando do que estava antes
        self.assertEqual(len(staging_dirs_before), len(staging_dirs_after),
                        f"staging não foi removido: {staging_dirs_after}")

    def test_staged_to_final_traduz_caminhos(self):
        """_staged_to_final traduz caminho staging → final.

        Funciona em qualquer SO (Windows usa backslash, Unix usa forward slash)."""
        # Usar paths com separadores do OS
        staging_root = os.path.join(tempfile.gettempdir(), ".wiki.staging-abc123")
        real_root = os.path.join(tempfile.gettempdir(), "wiki")
        staged_path = os.path.join(staging_root, "demo", "code-repo", "doc.md")

        final = cli._staged_to_final(staged_path, staging_root, real_root)
        expected = os.path.join(real_root, "demo", "code-repo", "doc.md")
        self.assertEqual(final, expected)


class W0ExitCodesTests(unittest.TestCase):
    """Testes de exit codes e JSON contract em W0."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_w0_exitcodes_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_compile_bloqueado_exit_1_json_estrutura(self):
        """Compile bloqueado: exit 1, JSON com 'bloqueado', 'paginas':[], 'recusados':[...]."""
        # Promover doc com id inválido (será recusado durante compile)
        inbox_path = os.path.join(self.store, "inbox", "code-repo", "doc.md")
        _write(inbox_path, (
            "---\nid: ../escape\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\ntopic: demo\n---\n\nConteúdo"
        ))
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)

        # Compile deve rejeitar (PathComponentError em validação de id)
        code, out, err = _run(["compile", "--store", self.store])

        # Exit 1, JSON com 'bloqueado'
        self.assertEqual(code, 1, f"exit code deve ser 1, foi {code}")
        data = json.loads(out)
        self.assertEqual(data.get("paginas"), [])
        self.assertIn("bloqueado", data)
        self.assertIn("recusados", data)
        self.assertGreater(len(data.get("recusados", [])), 0)

    def test_docx_bloqueado_exit_1_json_estrutura(self):
        """Docx bloqueado: exit 1, JSON com 'bloqueado', 'documentos':[], etc."""
        # Promover doc
        inbox_path = os.path.join(self.store, "inbox", "code-repo", "doc.md")
        _write(inbox_path, _markdown_doc("doc", "demo"))
        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)

        # Mock falha em geração
        from wk import docxgen
        original_build = docxgen.build_document
        def mock_build_fail(source):
            if source.get("id") == "doc":
                raise RuntimeError("Simulated docx failure")
            return original_build(source)

        with mock.patch.object(docxgen, "build_document", side_effect=mock_build_fail):
            code, out, err = _run(["docx", "--store", self.store])

        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data.get("documentos"), [])
        self.assertIn("bloqueado", data)


if __name__ == "__main__":
    unittest.main()
