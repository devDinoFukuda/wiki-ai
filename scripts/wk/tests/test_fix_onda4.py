# -*- coding: utf-8 -*-
"""Testes unitários para a Onda 4B: F-14 (reindex), F-26 (derived_from e feedback loop).

F-14: `_run_reindex` roda completo quando AZURE_OPENAI_ENDPOINT+AZURE_OPENAI_API_KEY+
      AZURE_OPENAI_EMBED_DEPLOY presentes; senão lex-only. Helpers:
      _embeddings_configurados() → bool, _reindex_modo() → str.

F-26: `ingest --derived-from "a,b"` grava `derived_from` no frontmatter (dedup/trim).
      promote bloqueia item agent-output cujo grafo de derivação (resolvido contra raw/)
      só contém agent-output/wiki → bloqueados_realimentacao[] com motivo/acao.
      `--allow-feedback-loop` promove e loga override.
      `derived_from` apontando para fonte humana → promove normal.
      sem derived_from → sem bloqueio novo.

Rodar:
    python -m pytest scripts/wk/tests/test_fix_onda4.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from wk import cli


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Roda cli.main com stdout/stderr capturados."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()
    except SystemExit as e:
        # argparse chama sys.exit quando há erro de parsing
        return e.code or 1, out.getvalue(), err.getvalue()


def _write(path: str, content: str) -> None:
    """Escreve arquivo com diretórios criados."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _read(path: str) -> str:
    """Lê arquivo como texto."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class F14EmbeddingsConfiguradosTests(unittest.TestCase):
    """F-14(a): testa _embeddings_configurados() com/sem env vars."""

    def test_embeddings_configurados_false_sem_vars(self):
        """Sem nenhuma var → False."""
        with mock.patch.dict(os.environ, {}, clear=False):
            # Remove as vars se existirem
            for key in ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_EMBED_DEPLOY"]:
                os.environ.pop(key, None)
            result = cli._embeddings_configurados()
            self.assertFalse(result)

    def test_embeddings_configurados_false_com_duas_vars(self):
        """Com 2 de 3 vars → False."""
        with mock.patch.dict(
            os.environ,
            {
                "AZURE_OPENAI_ENDPOINT": "https://fake.openai.azure.com/",
                "AZURE_OPENAI_API_KEY": "fake-key-123",
            },
            clear=False
        ):
            os.environ.pop("AZURE_OPENAI_EMBED_DEPLOY", None)
            result = cli._embeddings_configurados()
            self.assertFalse(result)

    def test_embeddings_configurados_true_com_tres_vars(self):
        """Com 3 vars (fake) → True."""
        with mock.patch.dict(
            os.environ,
            {
                "AZURE_OPENAI_ENDPOINT": "https://fake.openai.azure.com/",
                "AZURE_OPENAI_API_KEY": "fake-key-123",
                "AZURE_OPENAI_EMBED_DEPLOY": "fake-embed-deploy",
            },
            clear=False
        ):
            result = cli._embeddings_configurados()
            self.assertTrue(result)

    def test_embeddings_configurados_false_com_valor_vazio(self):
        """Com 3 vars mas uma vazia → False."""
        with mock.patch.dict(
            os.environ,
            {
                "AZURE_OPENAI_ENDPOINT": "https://fake.openai.azure.com/",
                "AZURE_OPENAI_API_KEY": "",
                "AZURE_OPENAI_EMBED_DEPLOY": "fake-embed-deploy",
            },
            clear=False
        ):
            result = cli._embeddings_configurados()
            self.assertFalse(result)

    def test_embeddings_configurados_false_com_valor_whitespace(self):
        """Com 3 vars mas uma só whitespace → False."""
        with mock.patch.dict(
            os.environ,
            {
                "AZURE_OPENAI_ENDPOINT": "https://fake.openai.azure.com/",
                "AZURE_OPENAI_API_KEY": "   ",
                "AZURE_OPENAI_EMBED_DEPLOY": "fake-embed-deploy",
            },
            clear=False
        ):
            result = cli._embeddings_configurados()
            self.assertFalse(result)


class F14ReindexModoTests(unittest.TestCase):
    """F-14(b): testa _reindex_modo() retorna strings corretas."""

    def test_reindex_modo_completo(self):
        """Com 3 vars presentes → 'completo'."""
        with mock.patch.dict(
            os.environ,
            {
                "AZURE_OPENAI_ENDPOINT": "https://fake.openai.azure.com/",
                "AZURE_OPENAI_API_KEY": "fake-key-123",
                "AZURE_OPENAI_EMBED_DEPLOY": "fake-embed-deploy",
            },
            clear=False
        ):
            result = cli._reindex_modo()
            self.assertEqual(result, "completo")

    def test_reindex_modo_lex_only(self):
        """Sem vars (ou incompleto) → contém 'lex-only'."""
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_EMBED_DEPLOY"]:
                os.environ.pop(key, None)
            result = cli._reindex_modo()
            self.assertIn("lex-only", result)


class F26IngestDerivedFromTests(unittest.TestCase):
    """F-26(a): ingest com --derived-from dedup/trim."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda4_ingest_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)

        # Inicializar store
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_ingest_derived_from_dedup(self):
        """ingest com --derived-from " x , x ,y " → frontmatter dedup "x,y"."""
        # Criar arquivo temporário para ingerir
        temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        try:
            temp_file.write("Conteúdo do documento derivado.")
            temp_file.close()

            code, out, err = _run([
                "ingest",
                "--store", self.store,
                temp_file.name,
                "--source-type", "human-doc",
                "--origin", "test-origin",
                "--topic", "test-topic",
                "--derived-from", " x , x , y ",
            ])

            self.assertEqual(code, 0, err)
            # Verificar JSON de resposta
            data = json.loads(out)
            self.assertIn("derived_from", data)
            # Deve conter x e y (dedup e trim)
            self.assertIn("x", data["derived_from"])
            self.assertIn("y", data["derived_from"])
            # x deve aparecer uma vez só
            self.assertEqual(data["derived_from"].count("x"), 1)

            # Verificar que foi gravado corretamente em raw/
            ingested_path = os.path.join(self.store, data["path"])
            self.assertTrue(os.path.isfile(ingested_path))
            content = _read(ingested_path)
            # O frontmatter deve ter derived_from
            self.assertIn("derived_from:", content)
        finally:
            os.unlink(temp_file.name)

    def test_ingest_no_derived_from(self):
        """ingest SEM --derived-from → sem `derived_from` no frontmatter."""
        temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        try:
            temp_file.write("Conteúdo normal.")
            temp_file.close()

            code, out, err = _run([
                "ingest",
                "--store", self.store,
                temp_file.name,
                "--source-type", "human-doc",
                "--origin", "test-origin",
                "--topic", "test-topic",
            ])

            self.assertEqual(code, 0, err)
            data = json.loads(out)
            # derived_from não deve estar no output
            self.assertNotIn("derived_from", data)
        finally:
            os.unlink(temp_file.name)


class F26PromoteBlockFeedbackLoopTests(unittest.TestCase):
    """F-26(c): promote bloqueia agent-output derivado só de agent-output/wiki."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda4_promote_block_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)

        # Inicializar store
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_promote_block_agent_output_feedback_loop(self):
        """promote de agent-output derivado só de agent-output → bloqueado."""
        # Criar fonte agent-output em raw/ (para ser referenciada)
        raw_dir = os.path.join(self.store, "raw", "agent-output")
        os.makedirs(raw_dir, exist_ok=True)

        agent_output_1_path = os.path.join(raw_dir, "agent-1.md")
        _write(
            agent_output_1_path,
            """---
id: agent-output-1
source_type: agent-output
origin: test-agent
captured_at: 2026-01-01T00:00:00Z
promoted: true
promoted_by: test
promoted_at: 2026-01-01T00:00:00Z
confidence: unverified
---

Primeira resposta do agente.
"""
        )

        # Criar um novo agent-output derivado do anterior
        inbox_dir = os.path.join(self.store, "inbox", "agent-outputs")
        os.makedirs(inbox_dir, exist_ok=True)

        agent_output_2_path = os.path.join(inbox_dir, "agent-2.md")
        _write(
            agent_output_2_path,
            """---
id: agent-output-2
source_type: agent-output
origin: test-agent
captured_at: 2026-01-02T00:00:00Z
promoted: false
derived_from: agent-output-1
---

Resposta derivada de agent-output-1, sem fonte humana.
"""
        )

        code, out, err = _run([
            "promote",
            "--store", self.store,
            "--approve", "agent-output-2",
            "--approved-by", "test-user",
        ])

        data = json.loads(err) if err else json.loads(out)

        # agent-output-2 deve estar bloqueado em bloqueados_realimentacao
        self.assertIn("bloqueados_realimentacao", data)
        self.assertTrue(any(
            item["id"] == "agent-output-2"
            for item in data["bloqueados_realimentacao"]
        ))
        # Verificar motivo
        item = next((i for i in data["bloqueados_realimentacao"] if i["id"] == "agent-output-2"), None)
        self.assertIsNotNone(item)
        self.assertEqual(item["motivo"], "realimentacao")


class F26PromoteAllowFeedbackLoopTests(unittest.TestCase):
    """F-26(c): promote com --allow-feedback-loop promove e loga override."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda4_promote_allow_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)

        # Inicializar store
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_promote_allow_feedback_loop_with_approve(self):
        """promote com --allow-feedback-loop --approve promove agent-output circular."""
        # Criar fonte agent-output em raw/ (para ser referenciada)
        raw_dir = os.path.join(self.store, "raw", "agent-output")
        os.makedirs(raw_dir, exist_ok=True)

        agent_output_1_path = os.path.join(raw_dir, "agent-1.md")
        _write(
            agent_output_1_path,
            """---
id: agent-output-1
source_type: agent-output
origin: test-agent
captured_at: 2026-01-01T00:00:00Z
promoted: true
promoted_by: test
promoted_at: 2026-01-01T00:00:00Z
confidence: unverified
---

Primeira resposta do agente.
"""
        )

        # Criar um novo agent-output derivado do anterior
        inbox_dir = os.path.join(self.store, "inbox", "agent-outputs")
        os.makedirs(inbox_dir, exist_ok=True)

        agent_output_2_path = os.path.join(inbox_dir, "agent-2.md")
        _write(
            agent_output_2_path,
            """---
id: agent-output-2
source_type: agent-output
origin: test-agent
captured_at: 2026-01-02T00:00:00Z
promoted: false
derived_from: agent-output-1
---

Resposta derivada de agent-output-1, sem fonte humana.
"""
        )

        code, out, err = _run([
            "promote",
            "--store", self.store,
            "--approve", "agent-output-2",
            "--approved-by", "tester",
            "--allow-feedback-loop",
        ])

        data = json.loads(err) if err else json.loads(out)

        # agent-output-2 deve estar em realimentacao_override
        self.assertIn("realimentacao_override", data)
        self.assertTrue(any(
            item["id"] == "agent-output-2"
            for item in data["realimentacao_override"]
        ))

        # Verificar que foi promovido (deve estar em promovidos[])
        self.assertIn("promovidos", data)
        self.assertTrue(any(
            item["id"] == "agent-output-2"
            for item in data["promovidos"]
        ))
        promoted_item = next(
            i for i in data["promovidos"] if i["id"] == "agent-output-2"
        )
        # Deve ter confidence = unverified (agent-output com aprovação humana)
        self.assertEqual(promoted_item["confidence"], "unverified")

        # Verificar log.md menciona o override
        log_path = os.path.join(self.store, "log.md")
        self.assertTrue(os.path.isfile(log_path))
        log_content = _read(log_path)
        # Deve mencionar agent-output-2 e realimentacao override
        self.assertIn("agent-output-2", log_content)
        self.assertIn("realimentacao", log_content)


class F26PromoteHumanSourceNoBlockTests(unittest.TestCase):
    """F-26(c): agent-output derivado de fonte humana → promove normal (sem bloqueio)."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda4_promote_human_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)

        # Inicializar store
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_promote_agent_output_from_human_doc_not_blocked(self):
        """agent-output derivado de human-doc → promove sem bloqueio."""
        # Criar fonte human-doc em raw/
        raw_dir = os.path.join(self.store, "raw", "human-transcripts")
        os.makedirs(raw_dir, exist_ok=True)

        human_doc_path = os.path.join(raw_dir, "human-1.md")
        _write(
            human_doc_path,
            """---
id: human-doc-1
source_type: human-doc
origin: human-source
captured_at: 2026-01-01T00:00:00Z
promoted: true
promoted_by: test
promoted_at: 2026-01-01T00:00:00Z
confidence: reviewed
---

Documento original escrito por humano.
"""
        )

        # Criar agent-output derivado da fonte humana
        inbox_dir = os.path.join(self.store, "inbox", "agent-outputs")
        os.makedirs(inbox_dir, exist_ok=True)

        agent_output_path = os.path.join(inbox_dir, "agent-1.md")
        _write(
            agent_output_path,
            """---
id: agent-output-derived-human
source_type: agent-output
origin: test-agent
captured_at: 2026-01-02T00:00:00Z
promoted: false
derived_from: human-doc-1
---

Resposta do agente baseada em fonte humana legítima.
"""
        )

        code, out, err = _run([
            "promote",
            "--store", self.store,
            "--approve", "agent-output-derived-human",
            "--approved-by", "tester",
        ])

        data = json.loads(err) if err else json.loads(out)

        # agent-output-derived-human NÃO deve estar em bloqueados_realimentacao
        blocked = data.get("bloqueados_realimentacao", [])
        self.assertFalse(any(
            item["id"] == "agent-output-derived-human"
            for item in blocked
        ))

        # Deve estar em promovidos[]
        self.assertIn("promovidos", data)
        self.assertTrue(any(
            item["id"] == "agent-output-derived-human"
            for item in data["promovidos"]
        ))


class F26PromoteNoDerivedFromTests(unittest.TestCase):
    """F-26(c): item SEM derived_from → comportamento normal (sem bloqueio novo)."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda4_no_derived_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)

        # Inicializar store
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_promote_agent_output_without_derived_from_not_blocked(self):
        """agent-output SEM derived_from → promove normal (sem bloqueio novo)."""
        inbox_dir = os.path.join(self.store, "inbox", "agent-outputs")
        os.makedirs(inbox_dir, exist_ok=True)

        agent_output_path = os.path.join(inbox_dir, "agent-no-derived.md")
        _write(
            agent_output_path,
            """---
id: agent-output-standalone
source_type: agent-output
origin: test-agent
captured_at: 2026-01-01T00:00:00Z
promoted: false
---

Resposta do agente sem derived_from declarado.
"""
        )

        code, out, err = _run([
            "promote",
            "--store", self.store,
            "--approve", "agent-output-standalone",
            "--approved-by", "tester",
        ])

        data = json.loads(err) if err else json.loads(out)

        # NÃO deve estar em bloqueados_realimentacao (não tem derived_from)
        blocked = data.get("bloqueados_realimentacao", [])
        self.assertFalse(any(
            item["id"] == "agent-output-standalone"
            for item in blocked
        ))

        # Deve estar em promovidos[]
        self.assertIn("promovidos", data)
        self.assertTrue(any(
            item["id"] == "agent-output-standalone"
            for item in data["promovidos"]
        ))


if __name__ == "__main__":
    unittest.main()
