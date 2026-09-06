"""Cenário C — word: bytes zip válido, estrutura OOXML, sem gridSpan/vMerge."""

import io
import unittest
import zipfile
from publishing.document import (
    DocKind, KnowledgeDocument, SemanticUnit, Statement, UnitState,
    Belonging,
)
from publishing import word, validate
from knowledge.models import (
    EntityType, EpistemicStatus, FactNature, LifecycleStatus,
)


class TestWordRenderBasic(unittest.TestCase):
    """C1 — word.render(document) → bytes zip válido."""

    def _make_test_doc(self):
        """Documento simples para testes."""
        st = Statement(
            "fct_1", "behavior", "comportamento testado", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id="unt_word", title="Unidade para Word",
            state=UnitState.IMPLEMENTED, subject="Teste",
            subject_key="teste", belonging=Belonging(),
            entity_id="ent_test", entity_type=EntityType.CAPABILITY,
            namespace="test", revision_id="rev_1",
            behavior=(st,),
        )
        return KnowledgeDocument(
            document_id="doc_word", title="Documento Word",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_test", anchor_entity_type=EntityType.CAPABILITY,
        )

    def test_word_render_returns_bytes(self):
        """render() retorna bytes."""
        doc = self._make_test_doc()
        result = word.render(doc)
        self.assertIsInstance(result, bytes)
        self.assertGreater(len(result), 0)

    def test_word_bytes_valid_zip(self):
        """Bytes são zip válido (.docx)."""
        doc = self._make_test_doc()
        result = word.render(doc)
        try:
            with zipfile.ZipFile(io.BytesIO(result)) as zf:
                entries = zf.namelist()
                self.assertGreater(len(entries), 0)
        except zipfile.BadZipFile:
            self.fail("Bytes não formam zip válido")

    def test_word_render_document_compatibility(self):
        """render() e render_document() retornam mesmos bytes."""
        doc = self._make_test_doc()
        md_bytes = word.render(doc)
        doc_bytes, _rep = word.render_document(doc)
        self.assertEqual(md_bytes, doc_bytes)

    def test_word_deterministic(self):
        """Renderização é determinística (mesmo doc → mesmos bytes)."""
        doc = self._make_test_doc()
        bytes1 = word.render(doc)
        bytes2 = word.render(doc)
        self.assertEqual(bytes1, bytes2)


class TestWordStructure(unittest.TestCase):
    """C2 — document.xml com Heading1/2 nativos, sem gridSpan/vMerge."""

    def _extract_document_xml(self, docx_bytes):
        """Extrai document.xml do .docx."""
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
            return zf.read("word/document.xml").decode("utf-8")

    def _make_doc_with_unit(self):
        """Documento com unidade simples."""
        st = Statement(
            "fct_1", "behavior", "comportamento", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id="unt_struct", title="Unidade estrutura",
            state=UnitState.IMPLEMENTED, subject="Teste",
            subject_key="teste", belonging=Belonging(),
            entity_id="ent_test", entity_type=EntityType.CAPABILITY,
            namespace="test", revision_id="rev_1",
            behavior=(st,),
        )
        return KnowledgeDocument(
            document_id="doc_struct", title="Teste estrutura",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_test", anchor_entity_type=EntityType.CAPABILITY,
        )

    def test_docx_has_document_xml(self):
        """DOCX contém word/document.xml."""
        doc = self._make_doc_with_unit()
        docx_bytes = word.render(doc)
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
            self.assertIn("word/document.xml", zf.namelist())

    def test_document_xml_valid(self):
        """document.xml é XML válido (parseable)."""
        doc = self._make_doc_with_unit()
        docx_bytes = word.render(doc)
        xml = self._extract_document_xml(docx_bytes)
        self.assertIn('<?xml', xml)
        self.assertIn('<w:document', xml)
        # Não deve lançar parse error ao validar
        rep = validate.validate_docx_structure(docx_bytes)
        self.assertTrue(rep.ok, f"Estrutura inválida: {rep.errors}")

    def test_no_merged_cells(self):
        """Sem gridSpan ou vMerge (células mescladas) em tabelas."""
        doc = self._make_doc_with_unit()
        docx_bytes = word.render(doc)
        xml = self._extract_document_xml(docx_bytes)
        # gridSpan/vMerge são flags de merge; tabelas devem ter estrutura plana
        self.assertNotIn("gridSpan=", xml, "gridSpan encontrado (mesclagem horizontal)")
        self.assertNotIn("vMerge", xml, "vMerge encontrado (mesclagem vertical)")


class TestWordStateLabels(unittest.TestCase):
    """C3 — estado aparece no corpo (D05), não só em metadata."""

    def _make_doc_by_state(self, state):
        """Documento com unidade em estado específico."""
        st = Statement(
            "fct_1", "behavior", "comportamento", "s",
            state, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id=f"unt_{state.value}", title=f"Unidade {state.value}",
            state=state, subject="Teste",
            subject_key="teste", belonging=Belonging(),
            entity_id="ent_test", entity_type=EntityType.CAPABILITY,
            namespace="test", revision_id="rev_1",
            behavior=(st,),
        )
        return KnowledgeDocument(
            document_id=f"doc_{state.value}", title=f"Doc {state.value}",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_test", anchor_entity_type=EntityType.CAPABILITY,
        )

    def test_implemented_label_in_body(self):
        """Estado 'Implementado' aparece no corpo."""
        doc = self._make_doc_by_state(UnitState.IMPLEMENTED)
        docx_bytes = word.render(doc)
        text = validate.docx_text(docx_bytes)
        self.assertIn("Implementado", text)

    def test_proposed_label_in_body(self):
        """Estado 'Proposto' aparece no corpo."""
        doc = self._make_doc_by_state(UnitState.PROPOSED)
        docx_bytes = word.render(doc)
        text = validate.docx_text(docx_bytes)
        self.assertIn("Proposto", text)


if __name__ == "__main__":
    unittest.main()
