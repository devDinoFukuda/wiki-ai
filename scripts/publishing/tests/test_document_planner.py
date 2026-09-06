"""Cenário A — document/planner: unit_id estável, títulos, placeholders, monolito."""

import unittest
from publishing.document import (
    DocKind, GenericTitle, InvalidDocumentGrouping, KnowledgeDocument,
    MixedStateBlock, PlaceholderContent, SemanticUnit, Statement, UnitState,
    Belonging, find_placeholders, is_generic_title, unit_id as make_unit_id,
)
from publishing.planner import assert_single_system
from knowledge.models import EntityType, EpistemicStatus, FactNature, LifecycleStatus, RelationType


class TestUnitIDStability(unittest.TestCase):
    """A1 — unit_id estável por (namespace, entidade, assunto, estado)."""

    def test_unit_id_same_with_different_title(self):
        """Mesmo unit_id quando entidade/assunto/estado iguais, título diferente."""
        ns, ent, subj = "acme/sys", "ent_rule_1", "comportamento"
        id1 = make_unit_id(ns, ent, subj, UnitState.IMPLEMENTED)
        id2 = make_unit_id(ns, ent, subj, UnitState.IMPLEMENTED)
        self.assertEqual(id1, id2)

    def test_unit_id_changes_with_state(self):
        """unit_id diferente quando estado muda."""
        ns, ent, subj = "acme/sys", "ent_rule_1", "comportamento"
        id_impl = make_unit_id(ns, ent, subj, UnitState.IMPLEMENTED)
        id_prop = make_unit_id(ns, ent, subj, UnitState.PROPOSED)
        self.assertNotEqual(id_impl, id_prop)

    def test_unit_id_changes_with_subject(self):
        """unit_id diferente quando assunto muda."""
        ns, ent, state = "acme/sys", "ent_rule_1", UnitState.IMPLEMENTED
        id1 = make_unit_id(ns, ent, "comportamento", state)
        id2 = make_unit_id(ns, ent, "proposta", state)
        self.assertNotEqual(id1, id2)


class TestGenericTitles(unittest.TestCase):
    """A2 — títulos genéricos rejeitados (D02)."""

    def test_generic_title_detalhes(self):
        self.assertTrue(is_generic_title("Detalhes"))
        self.assertTrue(is_generic_title("Outros"))
        self.assertTrue(is_generic_title("Geral"))

    def test_generic_title_pure_numeric(self):
        self.assertTrue(is_generic_title("1"))
        self.assertTrue(is_generic_title("1.2.3"))
        self.assertTrue(is_generic_title("III"))

    def test_specific_title_accepted(self):
        self.assertFalse(is_generic_title("RN-023 Limite de tentativas"))
        self.assertFalse(is_generic_title("Autenticação de parceiro"))

    def test_short_title_rejected(self):
        self.assertTrue(is_generic_title("AB"))
        self.assertTrue(is_generic_title("  "))


class TestPlaceholders(unittest.TestCase):
    """A3 — placeholders entre colchetes rejeitados (§10.3)."""

    def test_placeholder_ellipsis(self):
        found = find_placeholders("a resposta é [...] conforme")
        self.assertEqual(len(found), 1)
        self.assertIn("[...", found[0])

    def test_placeholder_space_inside(self):
        found = find_placeholders("o campo [a completar aqui] é obrigatório")
        self.assertEqual(len(found), 1)

    def test_technical_bracket_no_space(self):
        """Colchete SEM espaço é sintaxe técnica legítima."""
        found = find_placeholders("type list[int] e array[string]")
        self.assertEqual(len(found), 0)

    def test_semantic_unit_rejects_placeholder(self):
        """SemanticUnit.__post_init__ valida placeholders."""
        base_st = Statement(
            "fct_1", "behavior", "faz [algo por fazer]", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        with self.assertRaises(PlaceholderContent):
            SemanticUnit(
                unit_id="unt_1", title="Regra validada",
                state=UnitState.IMPLEMENTED, subject="Comportamento",
                subject_key="comportamento", belonging=Belonging(),
                entity_id="ent_rule", entity_type=EntityType.BUSINESS_RULE,
                namespace="test", revision_id="rev_1",
                behavior=(base_st,),
            )


class TestEmptyUnitsNotPublishable(unittest.TestCase):
    """A4 — unidade sem conteúdo (§10.2) é_publicável=False."""

    def test_unit_with_only_frame(self):
        """Título e pertencimento não contam como conteúdo."""
        unit = SemanticUnit(
            unit_id="unt_empty", title="Regra de negócio X",
            state=UnitState.IMPLEMENTED, subject="Comportamento",
            subject_key="comportamento", belonging=Belonging(),
            entity_id="ent_rule", entity_type=EntityType.BUSINESS_RULE,
            namespace="test", revision_id="rev_1",
        )
        self.assertFalse(unit.is_publishable())
        reason = unit.unpublishable_reason()
        self.assertIn("§10.2", reason)
        # Mensagem diz "sem comportamento, exceção, lacuna ou relação"
        self.assertIn("sem", reason.lower())

    def test_unit_with_behavior_is_publishable(self):
        """Com behavior, é publicável."""
        st = Statement(
            "fct_1", "behavior", "faz algo", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        unit = SemanticUnit(
            unit_id="unt_ok", title="Regra de negócio Y",
            state=UnitState.IMPLEMENTED, subject="Comportamento",
            subject_key="comportamento", belonging=Belonging(),
            entity_id="ent_rule", entity_type=EntityType.BUSINESS_RULE,
            namespace="test", revision_id="rev_1",
            behavior=(st,),
        )
        self.assertTrue(unit.is_publishable())


class TestMixedStateBlock(unittest.TestCase):
    """A5 — implementado e proposta no MESMO bloco rejeitados (D10)."""

    def test_mixed_state_in_behavior_rejected(self):
        """Statement proposto dentro de unidade implementada → erro."""
        st_impl = Statement(
            "fct_1", "behavior", "comportamento vigente", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
        )
        st_prop = Statement(
            "fct_2", "behavior", "comportamento proposto", "s",
            UnitState.PROPOSED, FactNature.DECLARED_REQUIREMENT,
            EpistemicStatus.SUPPORTED, LifecycleStatus.PROPOSED, "rev_1"
        )
        with self.assertRaises(MixedStateBlock):
            SemanticUnit(
                unit_id="unt_mixed", title="Regra confusa",
                state=UnitState.IMPLEMENTED, subject="Comportamento",
                subject_key="comportamento", belonging=Belonging(),
                entity_id="ent_rule", entity_type=EntityType.BUSINESS_RULE,
                namespace="test", revision_id="rev_1",
                behavior=(st_impl, st_prop),
            )


class TestDocumentMonolith(unittest.TestCase):
    """A6 — documento com 2 Systems rejeitado (anti-monolito)."""

    def test_single_system_doc_accepted(self):
        """Um único System → aceito."""
        doc = KnowledgeDocument(
            document_id="doc_sys", title="Visão do Sistema A",
            doc_kind=DocKind.VISAO_SISTEMA, units=(),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="sys_a", anchor_entity_type=EntityType.SYSTEM,
        )
        try:
            assert_single_system(doc)
        except InvalidDocumentGrouping:
            self.fail("Documento com 1 system deveria ser aceito")

    def test_multiple_systems_rejected(self):
        """Dois Systems em doc não-visão → rejeitado."""
        unit_sys_a = SemanticUnit(
            unit_id="unt_a", title="Regra do sistema A",
            state=UnitState.IMPLEMENTED, subject="Comportamento",
            subject_key="comportamento",
            belonging=Belonging(system_id="sys_a", system_title="Sistema A"),
            entity_id="ent_rule_a", entity_type=EntityType.BUSINESS_RULE,
            namespace="test", revision_id="rev_1",
            behavior=(Statement(
                "fct_1", "behavior", "faz algo", "s",
                UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
                EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
            ),),
        )
        unit_sys_b = SemanticUnit(
            unit_id="unt_b", title="Regra do sistema B",
            state=UnitState.IMPLEMENTED, subject="Comportamento",
            subject_key="comportamento",
            belonging=Belonging(system_id="sys_b", system_title="Sistema B"),
            entity_id="ent_rule_b", entity_type=EntityType.BUSINESS_RULE,
            namespace="test", revision_id="rev_1",
            behavior=(Statement(
                "fct_2", "behavior", "faz outra coisa", "s",
                UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
                EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev_1"
            ),),
        )
        doc = KnowledgeDocument(
            document_id="doc_cap", title="Capacidade X",
            doc_kind=DocKind.CAPACIDADE, units=(unit_sys_a, unit_sys_b),
            revision_id="rev_1", namespace="test",
            anchor_entity_id="cap_x", anchor_entity_type=EntityType.CAPABILITY,
        )
        with self.assertRaises(InvalidDocumentGrouping):
            assert_single_system(doc)


class TestInvalidDocumentGrouping(unittest.TestCase):
    """A7 — documento com tipo de entidade não-permitido rejeitado."""

    def test_component_cannot_anchor_document(self):
        """COMPONENT não pode ser âncora de documento."""
        with self.assertRaises(InvalidDocumentGrouping):
            KnowledgeDocument(
                document_id="doc_comp", title="Módulo auth",
                doc_kind=DocKind.CAPACIDADE, units=(),
                revision_id="rev_1", namespace="test",
                anchor_entity_id="comp_auth", anchor_entity_type=EntityType.COMPONENT,
            )

    def test_system_can_anchor_visao(self):
        """SYSTEM pode ancorar VISAO_SISTEMA."""
        try:
            KnowledgeDocument(
                document_id="doc_sys", title="Visão do sistema X",
                doc_kind=DocKind.VISAO_SISTEMA, units=(),
                revision_id="rev_1", namespace="test",
                anchor_entity_id="sys_x", anchor_entity_type=EntityType.SYSTEM,
            )
        except InvalidDocumentGrouping:
            self.fail("SYSTEM deveria poder ancorar VISAO_SISTEMA")


if __name__ == "__main__":
    unittest.main()
