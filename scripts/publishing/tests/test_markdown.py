"""Cenário B — markdown: determinístico, verbatim, rótulos de estado."""

import json
import unittest
from publishing.document import (
    DocKind, KnowledgeDocument, SemanticUnit, Statement, UnitState,
    Belonging, RelationRef, EvidenceRef,
)
from publishing.markdown import render, render_manifest, render_manifest_envelope, document_path
from knowledge.models import (
    EntityType, EpistemicStatus, FactNature, LifecycleStatus, SourceKind, ContentKind, RelationType,
)


class TestMarkdownDeterministic(unittest.TestCase):
    """B1 — render determinístico (mesma revisão → mesmos bytes)."""

    def _make_simple_doc(self):
        """Documento simples para testes."""
        st = Statement(
            "fct_1", "behavior", "saldo_disponivel > 0", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id="unt_test", title="Regra de saldo",
            state=UnitState.IMPLEMENTED, subject="Comportamento atual",
            subject_key="comportamento", belonging=Belonging(
                system_id="sys_x", system_title="Sistema X"
            ),
            entity_id="rn_001", entity_type=EntityType.BUSINESS_RULE,
            namespace="test", revision_id="rev_1",
            behavior=(st,),
        )
        return KnowledgeDocument(
            document_id="doc_x", title="Regras do Sistema X",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_x", anchor_entity_type=EntityType.CAPABILITY,
        )

    def test_deterministic_two_renders(self):
        """Duas renderizações do mesmo documento → bytes idênticos."""
        doc = self._make_simple_doc()
        md1 = render(doc)
        md2 = render(doc)
        self.assertEqual(md1.encode("utf-8"), md2.encode("utf-8"))

    def test_deterministic_manifest(self):
        """Manifesto renderizado duas vezes → idêntico."""
        doc = self._make_simple_doc()
        from publishing.planner import PublicationPlan
        plan = PublicationPlan(
            revision_id="rev_1", namespace="test",
            documents=(doc,), catalog_units=(), skipped=(), consumers=()
        )
        man1 = render_manifest_envelope(plan)
        man2 = render_manifest_envelope(plan)
        self.assertEqual(
            json.dumps(man1, sort_keys=True),
            json.dumps(man2, sort_keys=True),
        )


class TestMarkdownVerbatim(unittest.TestCase):
    """B2 — value do fato aparece verbatim (D12/D13)."""

    def test_comparator_preserved(self):
        """Comparador '>' preservado intacto."""
        st = Statement(
            "fct_comp", "behavior", "saldo_disponivel > 0", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id="unt_cmp", title="Comparador no fato",
            state=UnitState.IMPLEMENTED, subject="Teste",
            subject_key="teste", belonging=Belonging(),
            entity_id="ent_test", entity_type=EntityType.BUSINESS_RULE,
            namespace="test", revision_id="rev_1",
            behavior=(st,),
        )
        doc = KnowledgeDocument(
            document_id="doc_test", title="Teste de comparador",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_test", anchor_entity_type=EntityType.CAPABILITY,
        )
        md = render(doc)
        self.assertIn("> 0", md)

    def test_negation_preserved(self):
        """Negação 'nunca' preservada."""
        st = Statement(
            "fct_neg", "exception", "a reserva nunca é persistida se saldo <= 0", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id="unt_neg", title="Exceção com negação",
            state=UnitState.IMPLEMENTED, subject="Teste",
            subject_key="teste", belonging=Belonging(),
            entity_id="ent_test", entity_type=EntityType.BUSINESS_RULE,
            namespace="test", revision_id="rev_1",
            exceptions=(st,),
        )
        doc = KnowledgeDocument(
            document_id="doc_neg", title="Teste de negação",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_test", anchor_entity_type=EntityType.CAPABILITY,
        )
        md = render(doc)
        self.assertIn("nunca", md)

    def test_unit_preserved(self):
        """Unidade '10 minutos' preservada."""
        st = Statement(
            "fct_unit", "behavior", "expira em 10 minutos", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id="unt_unit", title="Comportamento com unidade",
            state=UnitState.IMPLEMENTED, subject="Teste",
            subject_key="teste", belonging=Belonging(),
            entity_id="ent_test", entity_type=EntityType.BUSINESS_RULE,
            namespace="test", revision_id="rev_1",
            behavior=(st,),
        )
        doc = KnowledgeDocument(
            document_id="doc_unit", title="Teste de unidade",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_test", anchor_entity_type=EntityType.CAPABILITY,
        )
        md = render(doc)
        self.assertIn("10 minutos", md)


class TestMarkdownStateSeparation(unittest.TestCase):
    """B3 — blocos implementado e proposto com rótulos distintos."""

    def test_implemented_and_proposed_separate_blocks(self):
        """Implementado e proposto em seções separadas."""
        st_impl = Statement(
            "fct_impl", "behavior", "implementado agora", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        st_prop = Statement(
            "fct_prop", "behavior", "proposto para depois", "s",
            UnitState.PROPOSED, FactNature.DECLARED_REQUIREMENT,
            EpistemicStatus.SUPPORTED, LifecycleStatus.PROPOSED, "rev_1"
        )
        unit_impl = SemanticUnit(
            unit_id="unt_impl", title="Regra: comportamento em vigor",
            state=UnitState.IMPLEMENTED, subject="Implementado",
            subject_key="comportamento", belonging=Belonging(),
            entity_id="rn_x", entity_type=EntityType.BUSINESS_RULE,
            namespace="test", revision_id="rev_1",
            behavior=(st_impl,),
        )
        unit_prop = SemanticUnit(
            unit_id="unt_prop", title="Regra: mudança proposta e não implementada",
            state=UnitState.PROPOSED, subject="Proposto",
            subject_key="proposta", belonging=Belonging(),
            entity_id="rn_x", entity_type=EntityType.BUSINESS_RULE,
            namespace="test", revision_id="rev_1",
            behavior=(st_prop,),
        )
        doc = KnowledgeDocument(
            document_id="doc_states", title="Teste de estados",
            doc_kind=DocKind.EVOLUCAO, units=(unit_impl, unit_prop),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="rn_x", anchor_entity_type=EntityType.BUSINESS_RULE,
        )
        md = render(doc)
        # Procurar pelos rótulos de bloco (em português)
        self.assertIn("Implementado", md)
        self.assertIn("Proposto", md)


class TestMarkdownManifest(unittest.TestCase):
    """B4 — render_manifest plano com unit_id → {md_path, state, fact_ids}."""

    def test_manifest_structure(self):
        """Manifesto tem estrutura esperada."""
        st = Statement(
            "fct_1", "behavior", "comportamento", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id="unt_manifest", title="Unidade no manifesto",
            state=UnitState.IMPLEMENTED, subject="Teste",
            subject_key="teste", belonging=Belonging(),
            entity_id="ent_test", entity_type=EntityType.BUSINESS_RULE,
            namespace="test", revision_id="rev_1",
            behavior=(st,),
        )
        doc = KnowledgeDocument(
            document_id="doc_man", title="Documento manifesto",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_test", anchor_entity_type=EntityType.CAPABILITY,
        )
        from publishing.planner import PublicationPlan
        plan = PublicationPlan(
            revision_id="rev_1", namespace="test",
            documents=(doc,), catalog_units=(), skipped=(), consumers=()
        )
        manifest = render_manifest(plan)
        self.assertIn("unt_manifest", manifest)
        entry = manifest["unt_manifest"]
        self.assertIn("state", entry)
        self.assertIn("fact_ids", entry)
        self.assertIn("fct_1", entry["fact_ids"])


if __name__ == "__main__":
    unittest.main()
