"""Testes do normalize.py — A) preserve, reverify, whitelist, policy, decide_status, make_block_id."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from ingestion import normalize as norm


class TestPreserveAndReverify(unittest.TestCase):
    """A: preserve() hasheia antes; reverify() detecta mutação."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def test_preserve_file_keeps_bytes(self):
        """preserve() com keep_bytes=True armazena bytes."""
        content = b"test content 123"
        path = os.path.join(self.tmpdir, "test.txt")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path, keep_bytes=True)
        self.assertEqual(preserved.raw, content)
        self.assertEqual(preserved.size, len(content))
        self.assertTrue(len(preserved.bytes_sha256) == 64)  # SHA256

    def test_preserve_file_discards_bytes(self):
        """preserve() com keep_bytes=False não armazena."""
        content = b"test content"
        path = os.path.join(self.tmpdir, "test.txt")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path, keep_bytes=False)
        self.assertEqual(preserved.raw, b"")
        self.assertEqual(preserved.size, len(content))

    def test_preserve_directory_uses_inventory(self):
        """preserve() em diretório hasheia inventário (nomes+tamanhos)."""
        os.makedirs(os.path.join(self.tmpdir, "subdir"))
        with open(os.path.join(self.tmpdir, "file1.txt"), "wb") as f:
            f.write(b"content1")
        with open(os.path.join(self.tmpdir, "subdir", "file2.txt"), "wb") as f:
            f.write(b"content2")

        preserved = norm.preserve(self.tmpdir, keep_bytes=True)
        self.assertEqual(preserved.mime_guess, "inode/directory")
        self.assertEqual(preserved.size, 0)
        self.assertTrue(len(preserved.bytes_sha256) == 64)

    def test_reverify_unchanged_file(self):
        """reverify() retorna None quando o arquivo não mudou."""
        content = b"immutable"
        path = os.path.join(self.tmpdir, "immutable.txt")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path, keep_bytes=False)
        diag = norm.reverify(preserved)
        self.assertIsNone(diag)

    def test_reverify_mutated_file(self):
        """reverify() detecta mutação e retorna diagnóstico ERROR."""
        content = b"original"
        path = os.path.join(self.tmpdir, "mutable.txt")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path, keep_bytes=False)

        # Altera o arquivo DEPOIS de preservar
        with open(path, "wb") as f:
            f.write(b"MUTATED")

        diag = norm.reverify(preserved)
        self.assertIsNotNone(diag)
        self.assertEqual(diag.code, "source.mutated_during_extraction")
        self.assertEqual(diag.severity, norm.Severity.ERROR)

    def test_reverify_directory_skipped(self):
        """reverify() retorna None para diretório (não pode mudar inventário durante)."""
        os.makedirs(os.path.join(self.tmpdir, "d"))
        preserved = norm.preserve(self.tmpdir)
        diag = norm.reverify(preserved)
        self.assertIsNone(diag)


class TestMetadataWhitelist(unittest.TestCase):
    """A: METADATA_WHITELIST — chave fora vira metadata_extra + diagnostic."""

    def test_whitelist_chaves(self):
        """METADATA_WHITELIST contém exatamente as chaves esperadas."""
        expected = {"initiative_id", "phase", "participants", "date", "title", "source_type"}
        self.assertEqual(set(norm.METADATA_WHITELIST), expected)

    def test_sanitize_metadata_whitelisted_keys(self):
        """sanitize_metadata() preserva chaves whitelisted."""
        raw = {
            "initiative_id": "INI-042",
            "phase": "execution",
            "title": "Projeto",
        }
        clean, extra, diags = norm.sanitize_metadata(raw)
        self.assertEqual(clean["initiative_id"], "INI-042")
        self.assertEqual(clean["phase"], "execution")
        self.assertEqual(clean["title"], "Projeto")
        self.assertEqual(len(extra), 0)
        self.assertEqual(len(diags), 0)

    def test_sanitize_metadata_out_of_whitelist(self):
        """sanitize_metadata() move chaves fora da whitelist para extra."""
        raw = {
            "title": "Ok",
            "unknown_key": "value",
            "another": 123,
        }
        clean, extra, diags = norm.sanitize_metadata(raw)
        self.assertEqual(clean.get("title"), "Ok")
        self.assertNotIn("unknown_key", clean)
        self.assertEqual(extra["unknown_key"], "value")
        self.assertEqual(extra["another"], 123)
        # Deve ter diagnóstico INFO sobre chave não whitelisted
        self.assertTrue(any(d.severity == norm.Severity.INFO for d in diags))

    def test_sanitize_metadata_suspicious_keys(self):
        """sanitize_metadata() detecta chaves suspeitas (system_prompt, command, etc)."""
        raw = {
            "title": "Document",
            "system_prompt": "You are...",
            "command": "rm -rf /",
            "instruction": "do something",
        }
        clean, extra, diags = norm.sanitize_metadata(raw)
        self.assertEqual(clean["title"], "Document")
        self.assertNotIn("system_prompt", clean)
        self.assertNotIn("command", clean)
        # Chaves suspeitas vão para extra e geram WARNING
        self.assertEqual(extra["system_prompt"], "You are...")
        suspicious_warnings = [d for d in diags if d.code == "metadata.instruction_attempt"]
        self.assertEqual(len(suspicious_warnings), 3)

    def test_sanitize_metadata_participants_list(self):
        """sanitize_metadata() converte participants string em lista."""
        raw = {"participants": "Alice, Bob, Charlie"}
        clean, extra, diags = norm.sanitize_metadata(raw)
        self.assertIsInstance(clean["participants"], list)
        self.assertEqual(clean["participants"], ["Alice", "Bob", "Charlie"])

    def test_policy_from_metadata_always_empty(self):
        """policy_from_metadata() retorna {} SEMPRE, qualquer que seja o conteúdo."""
        policy1 = norm.policy_from_metadata({})
        self.assertEqual(policy1, {})

        policy2 = norm.policy_from_metadata({"initiative_id": "INI-001", "phase": "design"})
        self.assertEqual(policy2, {})

        policy3 = norm.policy_from_metadata({"system_prompt": "dangerous", "tools": ["all"]})
        self.assertEqual(policy3, {})


class TestMakeBlockId(unittest.TestCase):
    """A: make_block_id() estável — índice + hash do conteúdo."""

    def test_make_block_id_stable(self):
        """make_block_id() com mesmos índice/kind/texto produz mesmo id."""
        id1 = norm.make_block_id(0, norm.BlockKind.PARAGRAPH, "Same text")
        id2 = norm.make_block_id(0, norm.BlockKind.PARAGRAPH, "Same text")
        self.assertEqual(id1, id2)

    def test_make_block_id_format(self):
        """make_block_id() segue formato blk-NNNNN-hash."""
        bid = norm.make_block_id(42, norm.BlockKind.HEADING, "Some heading")
        self.assertTrue(bid.startswith("blk-"))
        parts = bid.split("-")
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[1], "00042")  # zero-padded

    def test_make_block_id_differs_by_kind(self):
        """make_block_id() diferencia por BlockKind."""
        id_para = norm.make_block_id(0, norm.BlockKind.PARAGRAPH, "Text")
        id_head = norm.make_block_id(0, norm.BlockKind.HEADING, "Text")
        self.assertNotEqual(id_para, id_head)

    def test_make_block_id_differs_by_text(self):
        """make_block_id() diferencia por conteúdo do texto."""
        id1 = norm.make_block_id(0, norm.BlockKind.PARAGRAPH, "Text A")
        id2 = norm.make_block_id(0, norm.BlockKind.PARAGRAPH, "Text B")
        self.assertNotEqual(id1, id2)

    def test_make_block_id_reingest_same(self):
        """Reingestão idêntica produz mesmos block_ids."""
        text = "Decidimos usar PostgreSQL"
        ids = [norm.make_block_id(i, norm.BlockKind.PARAGRAPH, text) for i in range(3)]
        # Mesmos textos em mesma ordem → mesmos ids
        ids2 = [norm.make_block_id(i, norm.BlockKind.PARAGRAPH, text) for i in range(3)]
        self.assertEqual(ids, ids2)


class TestDecideStatus(unittest.TestCase):
    """A: decide_status() rebaixa (proposed=ingested + perda → partial)."""

    def test_decide_status_ingested_clean(self):
        """decide_status() retorna INGESTED quando nenhum diagnóstico."""
        blocks = [norm.Block(
            block_id="blk-00000-abc",
            kind=norm.BlockKind.PARAGRAPH,
            text="content",
            locator={},
            index=0,
        )]
        status = norm.decide_status(blocks, [])
        self.assertEqual(status, norm.SourceStatus.INGESTED)

    def test_decide_status_partial_with_unavailable(self):
        """decide_status() retorna PARTIAL quando diagnóstico tem unavailable."""
        blocks = [norm.Block(
            block_id="blk-00000-abc",
            kind=norm.BlockKind.PARAGRAPH,
            text="partial content",
            locator={},
            index=0,
        )]
        diag = norm.Diagnostic(
            code="extraction.incomplete",
            severity=norm.Severity.INFO,
            message="Some content was unavailable",
            unavailable=("imagens", "tabelas"),
        )
        status = norm.decide_status(blocks, [diag])
        self.assertEqual(status, norm.SourceStatus.PARTIAL)

    def test_decide_status_extraction_failed_error_no_blocks(self):
        """decide_status() retorna EXTRACTION_FAILED quando ERROR sem blocos."""
        diag = norm.Diagnostic(
            code="pdf.not_readable",
            severity=norm.Severity.ERROR,
            message="PDF encrypted",
        )
        status = norm.decide_status([], [diag])
        self.assertEqual(status, norm.SourceStatus.EXTRACTION_FAILED)

    def test_decide_status_partial_error_with_blocks(self):
        """decide_status() retorna PARTIAL quando ERROR MAS há blocos."""
        blocks = [norm.Block(
            block_id="blk-00000-abc",
            kind=norm.BlockKind.PARAGRAPH,
            text="some recovered content",
            locator={},
            index=0,
        )]
        # ERROR + unavailable → PARTIAL (not EXTRACTION_FAILED because we have blocks)
        diag = norm.Diagnostic(
            code="extraction.partial",
            severity=norm.Severity.ERROR,
            message="Extraction failed but recovered some content",
            unavailable=("pagina_corrompida",),
        )
        status = norm.decide_status(blocks, [diag], aggregate=False)
        # With blocks AND unavailable, should be PARTIAL
        self.assertEqual(status, norm.SourceStatus.PARTIAL)

    def test_decide_status_unsupported_override(self):
        """decide_status() respects proposed=UNSUPPORTED mesmo com blocos."""
        blocks = [norm.Block(
            block_id="blk-00000-abc",
            kind=norm.BlockKind.PARAGRAPH,
            text="content",
            locator={},
            index=0,
        )]
        status = norm.decide_status(blocks, [], proposed=norm.SourceStatus.UNSUPPORTED)
        self.assertEqual(status, norm.SourceStatus.UNSUPPORTED)

    def test_decide_status_aggregate_directory_no_blocks(self):
        """decide_status() com aggregate=True permite sem blocos próprios."""
        status = norm.decide_status([], [], aggregate=True)
        self.assertEqual(status, norm.SourceStatus.INGESTED)


class TestDocumentLocator(unittest.TestCase):
    """A: document_locator() + validate_locator passa contrato de evidence."""

    def test_document_locator_minimal(self):
        """document_locator() com campos obrigatórios."""
        loc = norm.document_locator(
            version="sha256:abcd1234",
            section="Introdução",
            block="blk-00000-abc",
        )
        self.assertEqual(loc["version"], "sha256:abcd1234")
        self.assertEqual(loc["section"], "Introdução")
        self.assertEqual(loc["block"], "blk-00000-abc")

    def test_document_locator_empty_section_becomes_root(self):
        """document_locator() transforma seção vazia em '(raiz)'."""
        loc = norm.document_locator(
            version="sha256:abcd1234",
            section="",
            block="blk-00000-abc",
        )
        self.assertEqual(loc["section"], "(raiz)")

    def test_document_locator_with_optional_fields(self):
        """document_locator() preserva campos opcionais."""
        loc = norm.document_locator(
            version="sha256:abcd1234",
            section="Cap 2",
            block="blk-00000-abc",
            paragraph=5,
            heading_path=["Fundação", "Subsistema"],
            page=12,
        )
        self.assertEqual(loc["paragraph"], 5)
        self.assertEqual(loc["heading_path"], ["Fundação", "Subsistema"])
        self.assertEqual(loc["page"], 12)


class TestTranscriptLocator(unittest.TestCase):
    """A: transcript_locator() preserva bloco, speaker, tempos (all-or-nothing)."""

    def test_transcript_locator_minimal(self):
        """transcript_locator() com campos obrigatórios."""
        loc = norm.transcript_locator(
            file="ata-2024-09-01.vtt",
            version="sha256:xyz789",
            block="blk-00000-xyz",
        )
        self.assertEqual(loc["file"], "ata-2024-09-01.vtt")
        self.assertEqual(loc["version"], "sha256:xyz789")
        self.assertEqual(loc["block"], "blk-00000-xyz")

    def test_transcript_locator_empty_file_becomes_unknown(self):
        """transcript_locator() transforma arquivo vazio em '(desconhecido)'."""
        loc = norm.transcript_locator(
            file="",
            version="sha256:xyz789",
            block="blk-00000-xyz",
        )
        self.assertEqual(loc["file"], "(desconhecido)")

    def test_transcript_locator_time_all_or_nothing(self):
        """transcript_locator() rejeita time_start sem time_end."""
        # time_start SEM time_end é ignorado (not created)
        loc = norm.transcript_locator(
            file="ata.vtt",
            version="sha256:xyz",
            block="blk-00000-xyz",
            time_start="00:01:30",
            time_end=None,
        )
        self.assertNotIn("time_start", loc)
        self.assertNotIn("time_end", loc)

    def test_transcript_locator_with_speaker(self):
        """transcript_locator() preserva speaker."""
        loc = norm.transcript_locator(
            file="ata.vtt",
            version="sha256:xyz",
            block="blk-00000-xyz",
            time_start="00:01:30",
            time_end="00:02:00",
            speaker="Dr. Silva",
        )
        self.assertEqual(loc["speaker"], "Dr. Silva")


if __name__ == "__main__":
    unittest.main()
