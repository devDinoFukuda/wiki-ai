"""Cenário E — release: publish_revision, manifesto, poda, rollback."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from publishing import release
from publishing.document import (
    DocKind, KnowledgeDocument, SemanticUnit, Statement, UnitState, Belonging,
)
from publishing.planner import PublicationPlan


class FakeRenderers:
    """Renderers fake para testes (compatíveis com contrato de release)."""

    class FakeMarkdown:
        def render(self, document):
            text = f"# {document.document_id}\n"
            for unit in document.units:
                text += f"\n## {unit.unit_id}\n{unit.title}\n"
            return text

        def render_manifest(self, plan):
            out = {}
            for doc in plan.documents:
                filename = f"{doc.document_id}.md"
                for unit in doc.units:
                    out[unit.unit_id] = {
                        "md_filename": filename,
                        "state": unit.state.value,
                        "fact_ids": list(unit.fact_ids),
                    }
            return out

    class FakeWord:
        def render(self, document):
            text = self.render_markdown(document)
            return ("DOCX::" + text).encode("utf-8")

        def render_markdown(self, document):
            text = f"# {document.document_id}\n"
            for unit in document.units:
                text += f"\n## {unit.unit_id}\n{unit.title}\n"
            return text

        def render_manifest_word(self, plan):
            out = {}
            for doc in plan.documents:
                filename = f"{doc.document_id}.docx"
                for unit in doc.units:
                    out[unit.unit_id] = {
                        "docx_filename": filename,
                        "state": unit.state.value,
                        "fact_ids": list(unit.fact_ids),
                    }
            return out

    markdown = FakeMarkdown()
    word = FakeWord()


class FakeValidators:
    """Validadores fake para testes."""

    class Report:
        def __init__(self, ok=True, bloqueios=None):
            self.ok = ok
            self.bloqueios = bloqueios or []

        def to_dict(self):
            return {"ok": self.ok, "bloqueios": self.bloqueios}

    def validate_equivalence(self, *args, **kwargs):
        return self.Report(ok=True)

    def validate_semantics(self, *args, **kwargs):
        return self.Report(ok=True)

    def validate_docx_structure(self, *args, **kwargs):
        return self.Report(ok=True)


class FakeValidatorsFailing(FakeValidators):
    """Validadores que falham (para testes de resiliência)."""

    def validate_semantics(self, *args, **kwargs):
        return self.Report(ok=False, bloqueios=["fato sem sustentação (fake)"])


class TestPublishRevisionSuccess(unittest.TestCase):
    """E1 — publish_revision v1 ok → manifesto e arquivos em disco."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def _make_plan_v1(self):
        """Plano v1 simples."""
        from knowledge.models import FactNature, EpistemicStatus, LifecycleStatus, EntityType
        st = Statement(
            "f1", "behavior", "Conteúdo A", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev1"
        )
        unit = SemanticUnit(
            unit_id="unit_a", title="Unidade A",
            state=UnitState.IMPLEMENTED, subject="Teste",
            subject_key="teste", belonging=Belonging(),
            entity_id="ent_a", entity_type=EntityType.CAPABILITY,
            namespace="test", revision_id="rev1",
            behavior=(st,),
        )
        doc = KnowledgeDocument(
            document_id="doc_a", title="Documento A",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev1", namespace="test",
            anchor_entity_id="cap_a", anchor_entity_type=EntityType.CAPABILITY,
        )
        return PublicationPlan(
            revision_id="rev1", namespace="test",
            documents=(doc,),
        )

    def test_publish_creates_files(self):
        """publish_revision cria markdown/ e word/."""
        plan = self._make_plan_v1()
        result = release.publish_revision(
            plan, self.tmpdir, FakeRenderers(), FakeValidators(), revision_id="rev1"
        )
        self.assertTrue(result.ok)
        self.assertTrue(os.path.isdir(os.path.join(self.tmpdir, "markdown")))
        self.assertTrue(os.path.isdir(os.path.join(self.tmpdir, "word")))

    def test_publish_creates_manifest(self):
        """publish_revision cria manifest.json."""
        plan = self._make_plan_v1()
        result = release.publish_revision(
            plan, self.tmpdir, FakeRenderers(), FakeValidators(), revision_id="rev1"
        )
        self.assertTrue(result.ok)
        manifest_path = os.path.join(self.tmpdir, "manifest.json")
        self.assertTrue(os.path.isfile(manifest_path))
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.assertEqual(manifest.get("revision_id"), "rev1")


class TestPublishRevisionFailover(unittest.TestCase):
    """E2 — validador falha: v1 fica 100% intacta, staging removido."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def _publish_v1_success(self):
        """Publica v1 com sucesso."""
        from knowledge.models import EntityType, FactNature, EpistemicStatus, LifecycleStatus
        st = Statement(
            "f1", "behavior", "v1", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev1"
        )
        unit = SemanticUnit(
            unit_id="ua", title="Unidade A",
            state=UnitState.IMPLEMENTED, subject="T", subject_key="t",
            belonging=Belonging(), entity_id="ea", entity_type=EntityType.CAPABILITY,
            namespace="t", revision_id="rev1", behavior=(st,),
        )
        doc = KnowledgeDocument(
            document_id="da", title="Doc A",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev1", namespace="t",
            anchor_entity_id="ca", anchor_entity_type=EntityType.CAPABILITY,
        )
        plan = PublicationPlan(
            revision_id="rev1", namespace="t", documents=(doc,),
        )
        r1 = release.publish_revision(
            plan, self.tmpdir, FakeRenderers(), FakeValidators(), revision_id="rev1"
        )
        self.assertTrue(r1.ok)
        return r1

    def test_failed_publish_preserves_previous_version(self):
        """Falha na v2 deixa v1 intacta."""
        from knowledge.models import EntityType, FactNature, EpistemicStatus, LifecycleStatus
        # Publicar v1
        self._publish_v1_success()
        # Tentar publicar v2 que falha
        st = Statement(
            "f2", "behavior", "v2", "s",
            UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, "rev2"
        )
        unit = SemanticUnit(
            unit_id="ub", title="Unidade B",
            state=UnitState.IMPLEMENTED, subject="T", subject_key="t",
            belonging=Belonging(), entity_id="eb", entity_type=EntityType.CAPABILITY,
            namespace="t", revision_id="rev2", behavior=(st,),
        )
        doc = KnowledgeDocument(
            document_id="db", title="Doc B",
            doc_kind=DocKind.CAPACIDADE, units=(unit,),
            revision_id="rev2", namespace="t",
            anchor_entity_id="cb", anchor_entity_type=EntityType.CAPABILITY,
        )
        plan_v2 = PublicationPlan(
            revision_id="rev2", namespace="t", documents=(doc,),
        )
        r2 = release.publish_revision(
            plan_v2, self.tmpdir, FakeRenderers(), FakeValidatorsFailing(), revision_id="rev2"
        )
        self.assertFalse(r2.ok, "publish_revision com validador falhando deveria retornar ok=False")
        # Verificar que v1 permanece ativa
        manifest_path = os.path.join(self.tmpdir, "manifest.json")
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.assertEqual(manifest.get("revision_id"), "rev1")


if __name__ == "__main__":
    unittest.main()
