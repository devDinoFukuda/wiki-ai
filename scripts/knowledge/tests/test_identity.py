"""Testes dos invariantes de identity.py — namespace/alias/determinismo."""

import unittest
import tempfile
import os
import sqlite3
from scripts.knowledge import identity
from scripts.knowledge.models import (
    EntityType,
    AliasOrigin,
    IdentityConflict,
    RenameNotConfirmed,
)


class TestIdentityDeterminism(unittest.TestCase):
    """Invariante: `entity_id` é determinístico e não baseado em título."""

    def test_entity_id_deterministic(self) -> None:
        """Mesmo insumo produz mesmo `entity_id`."""
        id1 = identity.entity_id("ns/proj", EntityType.COMPONENT, "stable_key")
        id2 = identity.entity_id("ns/proj", EntityType.COMPONENT, "stable_key")
        self.assertEqual(id1, id2)

    def test_entity_id_not_derived_from_title_alone(self) -> None:
        """Identidades com mesmo namespace/tipo mas stable_keys diferentes são distintas."""
        id1 = identity.entity_id("ns/proj", EntityType.COMPONENT, "key1")
        id2 = identity.entity_id("ns/proj", EntityType.COMPONENT, "key2")
        self.assertNotEqual(id1, id2)

    def test_entity_id_requires_stable_key(self) -> None:
        """Rejeita `stable_key` vazia."""
        with self.assertRaises(IdentityConflict) as ctx:
            identity.entity_id("ns/proj", EntityType.COMPONENT, "")
        self.assertIn("stable_key vazia", str(ctx.exception))

    def test_fact_id_deterministic(self) -> None:
        """Identidades de fato são determinísticas."""
        id1 = identity.fact_id("ns", "subj1", "predicate", "scope")
        id2 = identity.fact_id("ns", "subj1", "predicate", "scope")
        self.assertEqual(id1, id2)

    def test_fact_id_different_for_different_inputs(self) -> None:
        """Fatos diferentes têm ids diferentes."""
        id1 = identity.fact_id("ns", "subj1", "pred1", "scope")
        id2 = identity.fact_id("ns", "subj2", "pred1", "scope")
        self.assertNotEqual(id1, id2)

    def test_relation_id_deterministic(self) -> None:
        """Identidades de relação são determinísticas."""
        id1 = identity.relation_id("ns", "ent1", "contains", "ent2", "arch")
        id2 = identity.relation_id("ns", "ent1", "contains", "ent2", "arch")
        self.assertEqual(id1, id2)


class TestNamespaceNormalization(unittest.TestCase):
    """Invariante: namespaces são normalizados consistentemente."""

    def test_namespace_strip_whitespace(self) -> None:
        """Normalization remove espaços."""
        ns1 = identity.normalize_namespace("  ns/proj  ")
        ns2 = identity.normalize_namespace("ns/proj")
        self.assertEqual(ns1, ns2)

    def test_namespace_empty_rejected(self) -> None:
        """Rejeita namespace vazio."""
        with self.assertRaises(IdentityConflict):
            identity.normalize_namespace("")

    def test_namespace_salt_consistent(self) -> None:
        """Namespace salt é determinístico."""
        salt1 = identity.namespace_salt("ns/proj")
        salt2 = identity.namespace_salt("ns/proj")
        self.assertEqual(salt1, salt2)

    def test_different_namespaces_different_salt(self) -> None:
        """Namespaces diferentes têm salts diferentes."""
        salt1 = identity.namespace_salt("ns1/proj")
        salt2 = identity.namespace_salt("ns2/proj")
        self.assertNotEqual(salt1, salt2)


class TestEntityIdentityResolution(unittest.TestCase):
    """Invariante: identidade resolvida nunca cruza namespace/tipo."""

    def setUp(self) -> None:
        from scripts.knowledge import repository as repo_mod
        from datetime import datetime, timezone

        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        # Usar repository.connect para inicializar corretamente
        self.conn = repo_mod.connect(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_resolve_identity_not_cross_namespace(self) -> None:
        """Resolução não cruza namespace."""
        # Criar REVISION primeiro (para atender FK)
        self.conn.execute(
            "INSERT INTO revisions(revision_id, created_at, author, reason) "
            "VALUES (?,?,?,?)",
            ("rev1", "2024-01-01T00:00:00", "test", "test"),
        )
        # Criar entidade em ns1
        eid1 = identity.entity_id("ns1", EntityType.SYSTEM, "key1")
        self.conn.execute(
            "INSERT INTO entities(entity_id, namespace, entity_type, stable_key, "
            "created_revision_id, head_revision_id) VALUES (?,?,?,?,?,?)",
            (eid1, "ns1", EntityType.SYSTEM.value, "key1", "rev1", "rev1"),
        )
        # Criar entity_revisions
        self.conn.execute(
            "INSERT INTO entity_revisions(entity_id, revision_id, title, content_hash, "
            "lifecycle_status, recorded_at) VALUES (?,?,?,?,?,?)",
            (eid1, "rev1", "Title", "hash1", "current", "2024-01-01T00:00:00"),
        )
        self.conn.commit()

        # Tentar resolver com mesma chave em ns2
        resolved = identity.resolve_identity(
            self.conn, "ns2", EntityType.SYSTEM, stable_key="key1"
        )
        self.assertIsNone(resolved)

    def test_resolve_identity_not_cross_type(self) -> None:
        """Resolução não cruza tipo de entidade."""
        # Criar REVISION primeiro (para atender FK)
        self.conn.execute(
            "INSERT INTO revisions(revision_id, created_at, author, reason) "
            "VALUES (?,?,?,?)",
            ("rev1", "2024-01-01T00:00:00", "test", "test"),
        )
        # Criar SYSTEM
        eid1 = identity.entity_id("ns1", EntityType.SYSTEM, "key1")
        self.conn.execute(
            "INSERT INTO entities(entity_id, namespace, entity_type, stable_key, "
            "created_revision_id, head_revision_id) VALUES (?,?,?,?,?,?)",
            (eid1, "ns1", EntityType.SYSTEM.value, "key1", "rev1", "rev1"),
        )
        # Criar entity_revisions
        self.conn.execute(
            "INSERT INTO entity_revisions(entity_id, revision_id, title, content_hash, "
            "lifecycle_status, recorded_at) VALUES (?,?,?,?,?,?)",
            (eid1, "rev1", "Title", "hash1", "current", "2024-01-01T00:00:00"),
        )
        self.conn.commit()

        # Tentar resolver com mesmo namespace mas tipo COMPONENT
        resolved = identity.resolve_identity(
            self.conn, "ns1", EntityType.COMPONENT, stable_key="key1"
        )
        self.assertIsNone(resolved)


class TestAliasWithOrigin(unittest.TestCase):
    """Invariante: aliases obrigatoriamente têm origem."""

    def test_confirm_rename_requires_confirming_origin(self) -> None:
        """Rejeita renomeação com origem não confirmatória."""
        evidence = identity.RenameEvidence(
            origin=AliasOrigin.EXTRACTED,  # não confirma
            detail="similar names",
            similarity=0.95,
        )
        with self.assertRaises(RenameNotConfirmed) as ctx:
            identity.confirm_rename("old_name", "new_name", evidence)
        self.assertIn("identidade só é preservada", str(ctx.exception))

    def test_confirm_rename_vcs_origin_accepted(self) -> None:
        """VCS rename é origem confirmatória."""
        evidence = identity.RenameEvidence(
            origin=AliasOrigin.VCS_RENAME,
            detail="commit abc123",
        )
        alias = identity.confirm_rename("old_name", "new_name", evidence)
        self.assertEqual(alias.alias, "old_name")
        self.assertEqual(alias.origin, AliasOrigin.VCS_RENAME)

    def test_confirm_rename_human_origin_accepted(self) -> None:
        """Confirmação humana é origem confirmatória."""
        evidence = identity.RenameEvidence(
            origin=AliasOrigin.HUMAN_CONFIRMED,
            detail="analyst verified in PR #123",
        )
        alias = identity.confirm_rename("old_name", "new_name", evidence)
        self.assertEqual(alias.origin, AliasOrigin.HUMAN_CONFIRMED)

    def test_confirm_rename_requires_detail(self) -> None:
        """Rejeita renomeação sem detalhe verificável."""
        evidence = identity.RenameEvidence(
            origin=AliasOrigin.VCS_RENAME,
            detail="",  # vazio
        )
        with self.assertRaises(RenameNotConfirmed) as ctx:
            identity.confirm_rename("old_name", "new_name", evidence)
        self.assertIn("detalhe verificável", str(ctx.exception))


class TestOriginKindExtraction(unittest.TestCase):
    """Invariante: origem é extraída de autoria no formato `kind:id`."""

    def test_origin_kind_extraction(self) -> None:
        """Extrai OriginKind de autoria formatada."""
        from scripts.knowledge.models import origin_kind

        kind = origin_kind("llm:model1")
        self.assertEqual(kind.value, "llm")

        kind = origin_kind("pipeline:verify")
        self.assertEqual(kind.value, "pipeline")

        kind = origin_kind("human:user")
        self.assertEqual(kind.value, "human")

    def test_origin_kind_invalid_format(self) -> None:
        """Retorna None para formato inválido."""
        from scripts.knowledge.models import origin_kind

        kind = origin_kind("invalidformat")
        self.assertIsNone(kind)

        kind = origin_kind("")
        self.assertIsNone(kind)


if __name__ == "__main__":
    unittest.main()
