"""Testes dos invariantes de evidence.py — localizadores e sustentação."""

import unittest
from scripts.knowledge import evidence as ev_mod
from scripts.knowledge.models import (
    SourceKind,
    ContentKind,
    LocatorInvalid,
    UnsupportedContentKind,
)


class TestLocatorValidation(unittest.TestCase):
    """Invariante: localizador deve ter todos campos obrigatórios do tipo."""

    def test_code_locator_requires_all_fields(self) -> None:
        """Localizador de código exige repo, commit, path, linhas."""
        # Missing commit
        locator = {
            "repo": "myrepo",
            "path": "src/main.py",
            "start_line": 10,
            "end_line": 15,
            "snippet_hash": "xyz",
        }
        with self.assertRaises(LocatorInvalid) as ctx:
            ev_mod.validate_locator(SourceKind.CODE, locator)
        self.assertIn("commit", str(ctx.exception))

    def test_code_locator_requires_valid_line_range(self) -> None:
        """Linhas devem ser inteiras positivas com start <= end."""
        locator = {
            "repo": "myrepo",
            "commit": "abc123",
            "path": "src/main.py",
            "start_line": 15,
            "end_line": 10,  # end < start
            "snippet_hash": "xyz",
        }
        with self.assertRaises(LocatorInvalid) as ctx:
            ev_mod.validate_locator(SourceKind.CODE, locator)
        self.assertIn("inválido", str(ctx.exception).lower())

    def test_code_locator_requires_positive_line_numbers(self) -> None:
        """Linhas devem ser >= 1."""
        locator = {
            "repo": "myrepo",
            "commit": "abc123",
            "path": "src/main.py",
            "start_line": 0,  # inválido
            "end_line": 10,
            "snippet_hash": "xyz",
        }
        with self.assertRaises(LocatorInvalid):
            ev_mod.validate_locator(SourceKind.CODE, locator)

    def test_code_locator_valid(self) -> None:
        """Localizador de código válido passa."""
        locator = {
            "repo": "myrepo",
            "commit": "abc123",
            "path": "src/main.py",
            "start_line": 10,
            "end_line": 15,
            "snippet_hash": "xyz",
        }
        result = ev_mod.validate_locator(SourceKind.CODE, locator)
        self.assertEqual(result["path"], "src/main.py")

    def test_test_locator_requires_assertions(self) -> None:
        """Localizador de teste exige lista não vazia de assertions."""
        locator = {
            "case": "test_something",
            "assertions": [],  # vazio — inválido
            "version": "1.0",
        }
        with self.assertRaises(LocatorInvalid) as ctx:
            ev_mod.validate_locator(SourceKind.TEST, locator)
        self.assertIn("assertions", str(ctx.exception))

    def test_test_locator_executed_requires_condition(self) -> None:
        """Teste marcado como executado exige execution_condition."""
        locator = {
            "case": "test_something",
            "assertions": ["assert x"],
            "version": "1.0",
            "executed": True,
            # falta execution_condition
        }
        with self.assertRaises(LocatorInvalid) as ctx:
            ev_mod.validate_locator(SourceKind.TEST, locator)
        self.assertIn("execution_condition", str(ctx.exception))

    def test_config_locator_masked_contradiction(self) -> None:
        """Rejeita value_masked + masked=False."""
        locator = {
            "file": "config.yaml",
            "key": "api_key",
            "version": "1.0",
            "value_masked": "***",
            "masked": False,  # contradição
        }
        with self.assertRaises(LocatorInvalid) as ctx:
            ev_mod.validate_locator(SourceKind.CONFIG, locator)
        self.assertIn("contradição", str(ctx.exception))

    def test_document_locator_page_requires_block(self) -> None:
        """Documento com página exige bloco/parágrafo (localizador estável)."""
        locator = {
            "version": "1.0",
            "section": "Introduction",
            "block": "para1",
            "page": 5,
        }
        # Este é válido. Agora testar sem block
        result = ev_mod.validate_locator(SourceKind.DOCUMENT, locator)
        self.assertIsNotNone(result)

        # Agora testar com página mas sem bloco
        locator2 = {
            "version": "1.0",
            "section": "Introduction",
            "block": "",  # vazio
            "page": 5,
        }
        with self.assertRaises(LocatorInvalid) as ctx:
            ev_mod.validate_locator(SourceKind.DOCUMENT, locator2)
        self.assertIn("block", str(ctx.exception))

    def test_transcript_time_range_requires_both(self) -> None:
        """Intervalo de tempo exige time_start E time_end."""
        locator = {
            "file": "meeting.mp3",
            "version": "1.0",
            "block": "00:15-00:30",
            "time_start": "00:15",
            # falta time_end
        }
        with self.assertRaises(LocatorInvalid) as ctx:
            ev_mod.validate_locator(SourceKind.TRANSCRIPT, locator)
        self.assertIn("time_end", str(ctx.exception))

    def test_unknown_field_rejected(self) -> None:
        """Campo não previsto é rejeitado (schema fechado)."""
        locator = {
            "repo": "myrepo",
            "commit": "abc123",
            "path": "src/main.py",
            "start_line": 10,
            "end_line": 15,
            "snippet_hash": "xyz",
            "unknown_field": "value",  # não previsto
        }
        with self.assertRaises(LocatorInvalid) as ctx:
            ev_mod.validate_locator(SourceKind.CODE, locator)
        self.assertIn("não previstos", str(ctx.exception))


class TestEvidenceId(unittest.TestCase):
    """Invariante: `evidence_id` é determinístico."""

    def test_evidence_id_deterministic(self) -> None:
        """Mesma evidência produz mesmo id."""
        locator = {
            "repo": "myrepo",
            "commit": "abc123",
            "path": "src/main.py",
            "start_line": 10,
            "end_line": 15,
            "snippet_hash": "xyz",
        }
        id1 = ev_mod.evidence_id("ns", SourceKind.CODE, ContentKind.EXECUTABLE, "srv1", locator)
        id2 = ev_mod.evidence_id("ns", SourceKind.CODE, ContentKind.EXECUTABLE, "srv1", locator)
        self.assertEqual(id1, id2)

    def test_evidence_id_different_for_different_locators(self) -> None:
        """Localizadores diferentes produzem ids diferentes."""
        locator1 = {
            "repo": "myrepo",
            "commit": "abc123",
            "path": "src/main.py",
            "start_line": 10,
            "end_line": 15,
            "snippet_hash": "xyz",
        }
        locator2 = {
            "repo": "myrepo",
            "commit": "abc123",
            "path": "src/main.py",
            "start_line": 20,
            "end_line": 25,
            "snippet_hash": "abc",
        }
        id1 = ev_mod.evidence_id("ns", SourceKind.CODE, ContentKind.EXECUTABLE, "srv1", locator1)
        id2 = ev_mod.evidence_id("ns", SourceKind.CODE, ContentKind.EXECUTABLE, "srv1", locator2)
        self.assertNotEqual(id1, id2)


class TestSupportsImplemented(unittest.TestCase):
    """Invariante: apenas EXECUTABLE e CONFIG_VALUE sustentam IMPLEMENTED."""

    def test_executable_supports_implemented(self) -> None:
        """EXECUTABLE sustenta IMPLEMENTED."""
        ev = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.CODE,
            content_kind=ContentKind.EXECUTABLE,
            source_version_id="srv1",
            locator={
                "repo": "r",
                "commit": "c",
                "path": "p",
                "start_line": 1,
                "end_line": 2,
                "snippet_hash": "h",
            },
        )
        self.assertTrue(ev_mod.supports_implemented(ev))

    def test_config_value_supports_implemented(self) -> None:
        """CONFIG_VALUE sustenta IMPLEMENTED."""
        ev = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.CONFIG,
            content_kind=ContentKind.CONFIG_VALUE,
            source_version_id="srv1",
            locator={
                "file": "config.yaml",
                "key": "feature_flag",
                "version": "1.0",
            },
        )
        self.assertTrue(ev_mod.supports_implemented(ev))

    def test_comment_does_not_support_implemented(self) -> None:
        """COMMENT não sustenta IMPLEMENTED."""
        ev = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.CODE,
            content_kind=ContentKind.COMMENT,
            source_version_id="srv1",
            locator={
                "repo": "r",
                "commit": "c",
                "path": "p",
                "start_line": 1,
                "end_line": 1,
                "snippet_hash": "h",
            },
        )
        self.assertFalse(ev_mod.supports_implemented(ev))

    def test_docstring_does_not_support_implemented(self) -> None:
        """DOCSTRING não sustenta IMPLEMENTED."""
        ev = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.CODE,
            content_kind=ContentKind.DOCSTRING,
            source_version_id="srv1",
            locator={
                "repo": "r",
                "commit": "c",
                "path": "p",
                "start_line": 1,
                "end_line": 5,
                "snippet_hash": "h",
            },
        )
        self.assertFalse(ev_mod.supports_implemented(ev))

    def test_markdown_does_not_support_implemented(self) -> None:
        """MARKDOWN não sustenta IMPLEMENTED."""
        ev = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.DOCUMENT,
            content_kind=ContentKind.MARKDOWN,
            source_version_id="srv1",
            locator={
                "version": "1.0",
                "section": "Implementation",
                "block": "par1",
            },
        )
        self.assertFalse(ev_mod.supports_implemented(ev))

    def test_prose_does_not_support_implemented(self) -> None:
        """PROSE não sustenta IMPLEMENTED."""
        ev = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.DOCUMENT,
            content_kind=ContentKind.PROSE,
            source_version_id="srv1",
            locator={
                "version": "1.0",
                "section": "Design",
                "block": "par1",
            },
        )
        self.assertFalse(ev_mod.supports_implemented(ev))

    def test_assert_supports_implemented_rejects_only_comments(self) -> None:
        """Rejeita quando TODAS as evidências são não-sustentadoras."""
        ev1 = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.CODE,
            content_kind=ContentKind.COMMENT,
            source_version_id="srv1",
            locator={
                "repo": "r",
                "commit": "c",
                "path": "p",
                "start_line": 1,
                "end_line": 1,
                "snippet_hash": "h",
            },
        )
        ev2 = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.CODE,
            content_kind=ContentKind.DOCSTRING,
            source_version_id="srv1",
            locator={
                "repo": "r",
                "commit": "c",
                "path": "p",
                "start_line": 1,
                "end_line": 5,
                "snippet_hash": "h",
            },
        )
        with self.assertRaises(UnsupportedContentKind) as ctx:
            ev_mod.assert_supports_implemented([ev1, ev2])
        self.assertIn("executável", str(ctx.exception))

    def test_assert_supports_implemented_accepts_one_executable(self) -> None:
        """Aceita quando ao menos uma evidência sustenta."""
        ev1 = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.CODE,
            content_kind=ContentKind.COMMENT,
            source_version_id="srv1",
            locator={
                "repo": "r",
                "commit": "c",
                "path": "p",
                "start_line": 1,
                "end_line": 1,
                "snippet_hash": "h",
            },
        )
        ev2 = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.CODE,
            content_kind=ContentKind.EXECUTABLE,
            source_version_id="srv1",
            locator={
                "repo": "r",
                "commit": "c",
                "path": "p",
                "start_line": 10,
                "end_line": 15,
                "snippet_hash": "h",
            },
        )
        # Não deve lançar exceção
        ev_mod.assert_supports_implemented([ev1, ev2])


class TestContentKindValidation(unittest.TestCase):
    """Invariante: `content_kind` deve ser compatível com `source_kind`."""

    def test_code_accepts_executable(self) -> None:
        """CODE aceita EXECUTABLE."""
        ev = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.CODE,
            content_kind=ContentKind.EXECUTABLE,
            source_version_id="srv1",
            locator={
                "repo": "r",
                "commit": "c",
                "path": "p",
                "start_line": 1,
                "end_line": 2,
                "snippet_hash": "h",
            },
        )
        self.assertEqual(ev.content_kind, ContentKind.EXECUTABLE)

    def test_code_rejects_observation_record(self) -> None:
        """CODE rejeita OBSERVATION_RECORD."""
        with self.assertRaises(LocatorInvalid) as ctx:
            ev_mod.make_evidence(
                namespace="ns",
                source_kind=SourceKind.CODE,
                content_kind=ContentKind.OBSERVATION_RECORD,  # incompat
                source_version_id="srv1",
                locator={
                    "repo": "r",
                    "commit": "c",
                    "path": "p",
                    "start_line": 1,
                    "end_line": 2,
                    "snippet_hash": "h",
                },
            )
        self.assertIn("incompatível", str(ctx.exception))

    def test_observation_requires_observation_record(self) -> None:
        """OBSERVATION só aceita OBSERVATION_RECORD."""
        ev = ev_mod.make_evidence(
            namespace="ns",
            source_kind=SourceKind.OBSERVATION,
            content_kind=ContentKind.OBSERVATION_RECORD,
            source_version_id="srv1",
            locator={
                "origin": "monitoring_system",
                "instant": "2024-01-01T12:00:00Z",
                "scope": "prod_server",
                "execution_id": "exec123",
            },
        )
        self.assertEqual(ev.content_kind, ContentKind.OBSERVATION_RECORD)

    def test_observation_rejects_executable(self) -> None:
        """OBSERVATION rejeita EXECUTABLE."""
        with self.assertRaises(LocatorInvalid) as ctx:
            ev_mod.make_evidence(
                namespace="ns",
                source_kind=SourceKind.OBSERVATION,
                content_kind=ContentKind.EXECUTABLE,
                source_version_id="srv1",
                locator={
                    "origin": "monitoring",
                    "instant": "2024-01-01T12:00:00Z",
                    "scope": "prod",
                    "execution_id": "e1",
                },
            )
        self.assertIn("incompatível", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
