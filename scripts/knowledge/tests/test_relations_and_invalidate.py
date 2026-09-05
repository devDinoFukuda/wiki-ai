"""Testes para relations.py e invalidate.py — pares de tipo, ciclos, staleness."""

import unittest
import tempfile
import os
from scripts.knowledge import repository as repo_mod
from scripts.knowledge import relations as rel_mod
from scripts.knowledge import invalidate as inv_mod
from scripts.knowledge.models import (
    EntityType,
    EntityDraft,
    RelationDraft,
    FactDraft,
    EpistemicStatus,
    LifecycleStatus,
    FactNature,
    SourceKind,
    ContentKind,
)
from scripts.knowledge import evidence as ev_mod


class TestBaseRepositoryForRelations(unittest.TestCase):
    """Base class para testes com DB temporário."""

    def setUp(self) -> None:
        """Cria um banco temporário para cada teste."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_knowledge.db")
        self.repo = repo_mod.Repository.open(self.db_path)

    def tearDown(self) -> None:
        """Fecha o banco e limpa arquivos temporários."""
        self.repo.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestRelationPairValidation(TestBaseRepositoryForRelations):
    """Invariante: apenas pares de tipo permitidos (matriz ALLOWED_PAIRS)."""

    def setUp(self) -> super:
        super().setUp()
        # Criar entidades de tipos variados
        with self.repo.revision("test:setup", "criar entidades") as rev:
            self.system = self._create_entity_in_rev(
                rev, "ns", "sys1", "System", EntityType.SYSTEM
            )
            self.component = self._create_entity_in_rev(
                rev, "ns", "comp1", "Component", EntityType.COMPONENT
            )
            self.source = self._create_entity_in_rev(
                rev, "ns", "src1", "Source", EntityType.SOURCE
            )
            self.initiative = self._create_entity_in_rev(
                rev, "ns", "ini1", "Initiative", EntityType.INITIATIVE
            )
        self.system_id = self.system
        self.component_id = self.component
        self.source_id = self.source

    def _create_entity_in_rev(
        self, rev, ns: str, key: str, title: str, etype: EntityType
    ) -> str:
        draft = EntityDraft(
            namespace=ns,
            entity_type=etype,
            stable_key=key,
            title=title,
        )
        result = rev.put_entity(draft)
        return result.target_id

    def test_system_contains_component_allowed(self) -> None:
        """SYSTEM contains COMPONENT é par válido."""
        with self.repo.revision("test:relation", "criar relação") as rev:
            draft = RelationDraft(
                namespace="ns",
                source_entity_id=self.system_id,
                relation_type=repo_mod.RelationType.CONTAINS,
                target_entity_id=self.component_id,
                scope="arch",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            result = rev.put_relation(draft)
            self.assertTrue(result.changed)

    def test_source_contains_system_invalid(self) -> None:
        """SOURCE contains SYSTEM é par inválido."""
        from scripts.knowledge.models import InvalidRelationPair

        with self.repo.revision("test:relation", "criar relação") as rev:
            draft = RelationDraft(
                namespace="ns",
                source_entity_id=self.source_id,
                relation_type=repo_mod.RelationType.CONTAINS,
                target_entity_id=self.system_id,
                scope="arch",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            with self.assertRaises(InvalidRelationPair):
                rev.put_relation(draft)

    def test_contradicts_requires_same_type(self) -> None:
        """CONTRADICTS só funciona com mesma EntityType."""
        # Tentar criar SYSTEM contradicts COMPONENT — deve falhar
        from scripts.knowledge.models import InvalidRelationPair

        with self.repo.revision("test:relation", "criar relação") as rev:
            draft = RelationDraft(
                namespace="ns",
                source_entity_id=self.system_id,
                relation_type=repo_mod.RelationType.CONTRADICTS,
                target_entity_id=self.component_id,  # tipo diferente
                scope="logic",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            with self.assertRaises(InvalidRelationPair) as ctx:
                rev.put_relation(draft)
            self.assertIn("MESMA classe", str(ctx.exception))

    def test_supersedes_requires_same_type(self) -> None:
        """SUPERSEDES só funciona com mesma EntityType."""
        from scripts.knowledge.models import InvalidRelationPair

        with self.repo.revision("test:relation", "criar relação") as rev:
            draft = RelationDraft(
                namespace="ns",
                source_entity_id=self.system_id,
                relation_type=repo_mod.RelationType.SUPERSEDES,
                target_entity_id=self.component_id,  # tipo diferente
                scope="version",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            with self.assertRaises(InvalidRelationPair):
                rev.put_relation(draft)


class TestCycleDetection(TestBaseRepositoryForRelations):
    """Invariante: ciclos em SUPERSEDES são rejeitados."""

    def setUp(self) -> super:
        super().setUp()
        # Criar 3 componentes
        with self.repo.revision("test:setup", "criar entidades") as rev:
            e1 = self._create_entity_in_rev(
                rev, "ns", "e1", "Entity1", EntityType.COMPONENT
            )
            e2 = self._create_entity_in_rev(
                rev, "ns", "e2", "Entity2", EntityType.COMPONENT
            )
            e3 = self._create_entity_in_rev(
                rev, "ns", "e3", "Entity3", EntityType.COMPONENT
            )
        self.e1 = e1
        self.e2 = e2
        self.e3 = e3

    def _create_entity_in_rev(
        self, rev, ns: str, key: str, title: str, etype: EntityType
    ) -> str:
        draft = EntityDraft(
            namespace=ns,
            entity_type=etype,
            stable_key=key,
            title=title,
        )
        result = rev.put_entity(draft)
        return result.target_id

    def test_long_cycle_rejected(self) -> None:
        """Ciclo A -> B -> C -> A é rejeitado."""
        from scripts.knowledge.models import SupersedesCycle

        # A -> B
        with self.repo.revision("test:rel1", "A -> B") as rev:
            draft = RelationDraft(
                namespace="ns",
                source_entity_id=self.e1,
                relation_type=repo_mod.RelationType.SUPERSEDES,
                target_entity_id=self.e2,
                scope="version",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            rev.put_relation(draft)

        # B -> C
        with self.repo.revision("test:rel2", "B -> C") as rev:
            draft = RelationDraft(
                namespace="ns",
                source_entity_id=self.e2,
                relation_type=repo_mod.RelationType.SUPERSEDES,
                target_entity_id=self.e3,
                scope="version",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            rev.put_relation(draft)

        # C -> A deveria falhar (fecharia ciclo)
        with self.repo.revision("test:rel3", "C -> A (ciclo)") as rev:
            draft = RelationDraft(
                namespace="ns",
                source_entity_id=self.e3,
                relation_type=repo_mod.RelationType.SUPERSEDES,
                target_entity_id=self.e1,
                scope="version",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            with self.assertRaises(SupersedesCycle):
                rev.put_relation(draft)


class TestStalenessMarking(TestBaseRepositoryForRelations):
    """Invariante: fonte alterada marca dependentes stale (direto + transitivo).

    Cobertura: aceite W1(d) — fonte alterada marca dependentes stale.
    """

    def setUp(self) -> super:
        super().setUp()
        # Criar entidade, fato sobre ela, relação
        with self.repo.revision("test:setup", "setup") as rev:
            entity = self._create_entity_in_rev(
                rev, "ns", "e1", "Entity1", EntityType.SYSTEM
            )
        self.entity = entity

        # Registrar fonte
        source = self.repo.register_source("ns", SourceKind.CODE, "test.py")
        self.sv1 = self.repo.register_source_version(source, "v1", "hash1")

    def _create_entity_in_rev(
        self, rev, ns: str, key: str, title: str, etype: EntityType
    ) -> str:
        draft = EntityDraft(
            namespace=ns,
            entity_type=etype,
            stable_key=key,
            title=title,
        )
        result = rev.put_entity(draft)
        return result.target_id

    def test_direct_fact_marked_stale(self) -> None:
        """Fato com source_version alterada é marcado STALE."""
        # Criar fato com source_version
        with self.repo.revision("test:create", "criar fato") as rev:
            draft = FactDraft(
                namespace="ns",
                subject_id=self.entity,
                predicate="prop",
                value="val",
                scope="scope",
                nature=FactNature.OBSERVED,
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:user",
                source_version_id=self.sv1.source_version_id,
            )
            result = rev.put_fact(draft)
            fact_id = result.target_id

        # Verificar que fato é CURRENT
        fact = self.repo.get_fact(fact_id)
        self.assertEqual(fact.lifecycle_status, LifecycleStatus.CURRENT)

        # Marcar dependentes da source_version como stale
        report = inv_mod.mark_stale_for_source_version(
            self.repo, self.sv1.source_version_id, "human:user", "versão alterada"
        )
        self.assertIn(fact_id, report.dependents.facts)

        # Verificar que fato agora é STALE
        fact_stale = self.repo.get_fact(fact_id, lifecycle=None)
        self.assertEqual(fact_stale.lifecycle_status, LifecycleStatus.STALE)

    def test_direct_entity_marked_stale(self) -> None:
        """Entidade com source_version alterada é marcada STALE."""
        # Criar entidade com source_version
        with self.repo.revision("test:create", "criar entidade com source") as rev:
            draft = EntityDraft(
                namespace="ns",
                entity_type=EntityType.COMPONENT,
                stable_key="comp1",
                title="Component",
                source_version_id=self.sv1.source_version_id,
            )
            result = rev.put_entity(draft)
            entity_id = result.target_id

        # Verificar CURRENT
        entity = self.repo.get_entity(entity_id)
        self.assertEqual(entity.lifecycle_status, LifecycleStatus.CURRENT)

        # Marcar como stale
        report = inv_mod.mark_stale_for_source_version(
            self.repo, self.sv1.source_version_id, "human:user", "versão alterada"
        )
        self.assertIn(entity_id, report.dependents.entities)

        # Verificar STALE
        entity_stale = self.repo.get_entity(entity_id, lifecycle=None)
        self.assertEqual(entity_stale.lifecycle_status, LifecycleStatus.STALE)

    def test_transitive_derived_from_stale_marking(self) -> None:
        """Entidade derivada de fonte stale também fica stale (transitivo)."""
        # E1 com source_version
        with self.repo.revision("test:create", "criar E1 com source") as rev:
            draft = EntityDraft(
                namespace="ns",
                entity_type=EntityType.SYSTEM,
                stable_key="e1",
                title="E1",
                source_version_id=self.sv1.source_version_id,
            )
            result = rev.put_entity(draft)
            e1_id = result.target_id

        # E2 derived_from E1
        with self.repo.revision("test:create", "criar E2") as rev:
            draft = EntityDraft(
                namespace="ns",
                entity_type=EntityType.COMPONENT,
                stable_key="e2",
                title="E2",
            )
            result = rev.put_entity(draft)
            e2_id = result.target_id

        # Relação E2 derived_from E1
        with self.repo.revision("test:create", "E2 derived_from E1") as rev:
            draft = RelationDraft(
                namespace="ns",
                source_entity_id=e2_id,
                relation_type=repo_mod.RelationType.DERIVED_FROM,
                target_entity_id=e1_id,
                scope="data",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            rev.put_relation(draft)

        # Marcar E1's source como stale
        report = inv_mod.mark_stale_for_source_version(
            self.repo, self.sv1.source_version_id, "human:user", "versão alterada"
        )

        # E1 deve estar nos dependentes diretos
        self.assertIn(e1_id, report.dependents.entities)

        # E2 deve estar nos dependentes (transitivos)
        self.assertIn(e2_id, report.dependents.entities)

        # Ambas devem estar STALE agora
        e1_stale = self.repo.get_entity(e1_id, lifecycle=None)
        e2_stale = self.repo.get_entity(e2_id, lifecycle=None)
        self.assertEqual(e1_stale.lifecycle_status, LifecycleStatus.STALE)
        self.assertEqual(e2_stale.lifecycle_status, LifecycleStatus.STALE)

    def test_stale_historical_not_remarked(self) -> None:
        """Não marca como STALE o que já é HISTORICAL/SUPERSEDED."""
        # Criar fato com source_version
        with self.repo.revision("test:create", "criar fato") as rev:
            draft = FactDraft(
                namespace="ns",
                subject_id=self.entity,
                predicate="prop",
                value="val",
                scope="scope",
                nature=FactNature.OBSERVED,
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:user",
                source_version_id=self.sv1.source_version_id,
            )
            result = rev.put_fact(draft)
            fact_id = result.target_id

        # Movê-lo para HISTORICAL
        with self.repo.revision("test:move", "mover para historical") as rev:
            rev.set_fact_lifecycle(fact_id, LifecycleStatus.HISTORICAL, "human:user")

        # Marcar dependentes como stale
        report = inv_mod.mark_stale_for_source_version(
            self.repo, self.sv1.source_version_id, "human:user"
        )

        # Fato não deve estar nos dependentes (já é histórico)
        self.assertNotIn(fact_id, report.dependents.facts)

        # Deve continuar HISTORICAL
        fact = self.repo.get_fact(fact_id, lifecycle=None)
        self.assertEqual(fact.lifecycle_status, LifecycleStatus.HISTORICAL)


class TestSupersdesPathDetection(unittest.TestCase):
    """Teste isolado para `supersedes_path_exists` — detecta caminhos."""

    def test_supersedes_path_exists(self) -> None:
        """Detecta caminho A -> B -> C."""
        import sqlite3
        import tempfile
        import os

        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")

        # Setup schema
        from scripts.knowledge import repository as repo_mod

        conn = repo_mod.connect(db_path)

        # Criar revisão primeiro
        conn.execute(
            "INSERT INTO revisions(revision_id, created_at, author, reason) "
            "VALUES (?,?,?,?)",
            ("r1", "2024-01-01T00:00:00", "test", "test"),
        )

        # Criar entidades
        e1 = "e1"
        e2 = "e2"
        e3 = "e3"
        conn.execute(
            "INSERT INTO entities(entity_id, namespace, entity_type, stable_key, "
            "created_revision_id, head_revision_id) VALUES (?,?,?,?,?,?)",
            (e1, "ns", "Component", "k1", "r1", "r1"),
        )
        conn.execute(
            "INSERT INTO entities(entity_id, namespace, entity_type, stable_key, "
            "created_revision_id, head_revision_id) VALUES (?,?,?,?,?,?)",
            (e2, "ns", "Component", "k2", "r1", "r1"),
        )
        conn.execute(
            "INSERT INTO entities(entity_id, namespace, entity_type, stable_key, "
            "created_revision_id, head_revision_id) VALUES (?,?,?,?,?,?)",
            (e3, "ns", "Component", "k3", "r1", "r1"),
        )

        # Criar relações: e1 -> e2 -> e3
        conn.execute(
            "INSERT INTO relations(relation_id, namespace, source_entity_id, relation_type, "
            "target_entity_id, scope, created_revision_id, head_revision_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("r1", "ns", e1, "supersedes", e2, "v", "r1", "r1"),
        )
        conn.execute(
            "INSERT INTO relation_revisions(relation_id, revision_id, source_entity_id, "
            "relation_type, target_entity_id, scope, epistemic_status, lifecycle_status, "
            "asserted_by, support_recorded_by, source_version_id, content_hash, "
            "valid_from, valid_to, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "r1",
                "r1",
                e1,
                "supersedes",
                e2,
                "v",
                "inferred",
                "current",
                "human:a",
                None,
                None,
                "h1",
                None,
                None,
                "2024-01-01T00:00:00",
            ),
        )
        conn.execute(
            "INSERT INTO relations(relation_id, namespace, source_entity_id, relation_type, "
            "target_entity_id, scope, created_revision_id, head_revision_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("r2", "ns", e2, "supersedes", e3, "v", "r1", "r1"),
        )
        conn.execute(
            "INSERT INTO relation_revisions(relation_id, revision_id, source_entity_id, "
            "relation_type, target_entity_id, scope, epistemic_status, lifecycle_status, "
            "asserted_by, support_recorded_by, source_version_id, content_hash, "
            "valid_from, valid_to, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "r2",
                "r1",
                e2,
                "supersedes",
                e3,
                "v",
                "inferred",
                "current",
                "human:a",
                None,
                None,
                "h2",
                None,
                None,
                "2024-01-01T00:00:00",
            ),
        )
        conn.commit()

        # Testar: caminho de e3 para e1?
        exists = rel_mod.supersedes_path_exists(conn, e3, e1)
        self.assertFalse(
            exists
        )  # e3 não pode alcançar e1 (direção reversa)

        # Testar: caminho de e1 para e3?
        exists = rel_mod.supersedes_path_exists(conn, e1, e3)
        self.assertTrue(exists)  # e1 -> e2 -> e3

        conn.close()
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
