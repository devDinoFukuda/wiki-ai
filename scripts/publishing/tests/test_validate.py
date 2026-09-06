"""Cenário D — validate: semantics, equivalence, docx_structure."""

import io
import json
import tempfile
import unittest
import zipfile
from publishing.document import (
    DocKind, KnowledgeDocument, SemanticUnit, Statement, UnitState,
    Belonging,
)
from publishing import markdown, word, validate
from publishing.planner import PublicationPlan
from knowledge.models import (
    EntityType, EpistemicStatus, FactNature, LifecycleStatus,
)


class TestValidateSemantics(unittest.TestCase):
    """D1 — validate_semantics detecta valor crítico apagado."""

    def _make_doc_with_comparator(self):
        """Documento com comparador crítico."""
        st = Statement(
            "fct_comp", "behavior", "saldo > 0 é verificado", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id="unt_comp", title="Unidade com comparador",
            state=UnitState.IMPLEMENTED, subject="Teste",
            subject_key="teste", belonging=Belonging(),
            entity_id="ent_test", entity_type=EntityType.CAPABILITY,
            namespace="test", revision_id="rev_1",
            behavior=(st,),
        )
        return KnowledgeDocument(
            document_id="doc_comp", title="Teste comparador",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_test", anchor_entity_type=EntityType.CAPABILITY,
        )

    def test_semantics_detects_missing_comparator(self):
        """Validador bloqueia quando comparador '>' é apagado."""
        doc = self._make_doc_with_comparator()
        md_original = markdown.render(doc)
        docx_bytes = word.render(doc)

        # Simular Markdown onde "> 0" foi alterado para "positivo"
        md_corrupted = md_original.replace("saldo > 0", "saldo positivo")

        rep = validate.validate_semantics(doc, md_corrupted, docx_bytes)
        self.assertFalse(rep.ok, "Validador deveria bloquear comparador apagado")
        self.assertGreater(len(rep.errors), 0)

    def test_semantics_accepts_valid_markdown(self):
        """Validador aceita Markdown íntegro."""
        doc = self._make_doc_with_comparator()
        md = markdown.render(doc)
        docx_bytes = word.render(doc)
        rep = validate.validate_semantics(doc, md, docx_bytes)
        self.assertTrue(rep.ok, f"Validador rejeitou markdown válido: {rep.errors}")


class TestValidateEquivalence(unittest.TestCase):
    """D2 — validate_equivalence detecta unit_id/fact_id divergente."""

    def _make_simple_plan(self):
        """Plano simples com uma unidade."""
        st = Statement(
            "fct_1", "behavior", "comportamento", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id="unt_equiv", title="Unidade equivalência",
            state=UnitState.IMPLEMENTED, subject="Teste",
            subject_key="teste", belonging=Belonging(),
            entity_id="ent_test", entity_type=EntityType.CAPABILITY,
            namespace="test", revision_id="rev_1",
            behavior=(st,),
        )
        doc = KnowledgeDocument(
            document_id="doc_equiv", title="Teste equivalência",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_test", anchor_entity_type=EntityType.CAPABILITY,
        )
        return PublicationPlan(
            revision_id="rev_1", namespace="test",
            documents=(doc,), catalog_units=(), skipped=(), consumers=()
        )

    def test_equivalence_accepts_matching_manifests(self):
        """Manifestos idênticos são aceitos."""
        plan = self._make_simple_plan()
        md_man = markdown.render_manifest(plan)
        wd_man = word.render_manifest_word(plan)
        rep = validate.validate_equivalence(md_man, wd_man, plan)
        self.assertTrue(rep.ok, f"Manifestos idênticos rejeitados: {rep.errors}")

    def test_equivalence_detects_missing_unit(self):
        """Validador detecta unit_id ausente em Markdown."""
        plan = self._make_simple_plan()
        md_man = markdown.render_manifest(plan)
        wd_man = word.render_manifest_word(plan)

        # Simular unit faltando no Markdown
        unit_key = list(md_man.keys())[0]
        del md_man[unit_key]

        rep = validate.validate_equivalence(md_man, wd_man, plan)
        self.assertFalse(rep.ok, "Validador deveria bloquear unit_id faltante")

    def test_equivalence_detects_different_fact_ids(self):
        """Validador detecta fact_ids divergentes."""
        plan = self._make_simple_plan()
        md_man = markdown.render_manifest(plan)
        wd_man = word.render_manifest_word(plan)

        # Simular fact_ids diferente
        unit_key = list(md_man.keys())[0]
        md_man[unit_key]["fact_ids"] = ["fct_999"]

        rep = validate.validate_equivalence(md_man, wd_man, plan)
        self.assertFalse(rep.ok, "Validador deveria bloquear fact_ids divergentes")


class TestValidateDocxStructure(unittest.TestCase):
    """D3 — validate_docx_structure rejeita zip inválido e DOCTYPE."""

    def test_rejects_invalid_zip(self):
        """Bytes não-zip são rejeitados."""
        rep = validate.validate_docx_structure(b"nao eh zip")
        self.assertFalse(rep.ok)
        self.assertGreater(len(rep.errors), 0)

    def test_rejects_doctype_in_xml(self):
        """DOCTYPE em document.xml é rejeitado (XXE defense)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            # Estrutura mínima com DOCTYPE
            for entry in validate.REQUIRED_DOCX_ENTRIES:
                if entry == "word/document.xml":
                    zf.writestr(entry, b'<?xml version="1.0"?><!DOCTYPE x><w:document/>')
                else:
                    zf.writestr(entry, b'<?xml version="1.0"?><x/>')

        rep = validate.validate_docx_structure(buf.getvalue())
        self.assertFalse(rep.ok)
        self.assertGreater(len(rep.errors), 0)

    def test_accepts_valid_docx(self):
        """DOCX válido é aceito."""
        st = Statement(
            "fct_1", "behavior", "comportamento", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id="unt_valid", title="Unidade válida",
            state=UnitState.IMPLEMENTED, subject="Teste",
            subject_key="teste", belonging=Belonging(),
            entity_id="ent_test", entity_type=EntityType.CAPABILITY,
            namespace="test", revision_id="rev_1",
            behavior=(st,),
        )
        doc = KnowledgeDocument(
            document_id="doc_valid", title="DOCX válido",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_test", anchor_entity_type=EntityType.CAPABILITY,
        )
        docx_bytes = word.render(doc)
        rep = validate.validate_docx_structure(docx_bytes)
        self.assertTrue(rep.ok, f"DOCX válido rejeitado: {rep.errors}")


if __name__ == "__main__":
    unittest.main()
