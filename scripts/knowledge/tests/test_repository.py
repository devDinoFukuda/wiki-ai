"""Testes dos invariantes do repository.py — sustentação, natureza, ciclos."""

import unittest
import tempfile
import os
from scripts.knowledge import repository as repo_mod
from scripts.knowledge.models import (
    EntityType,
    EntityDraft,
    FactDraft,
    RelationDraft,
    FactNature,
    EpistemicStatus,
    LifecycleStatus,
    ApprovalState,
    MissingEvidence,
    SelfDeclaredSupport,
    NatureChangeRejected,
    UnsupportedContentKind,
    SupersedesCycle,
    InvalidRelationPair,
    SourceKind,
    ContentKind,
    OriginKind,
)
from scripts.knowledge import evidence as ev_mod


class TestBaseRepository(unittest.TestCase):
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


class TestSupportedRequiresEvidence(TestBaseRepository):
    """Invariante: `supported` exige `evidence_refs` com origem autorizada.

    Cobertura: aceite W1(a) — fato sem evidência não vira supported.
    """

    def setUp(self) -> super:
        super().setUp()
        # Criar entidade sujeito
        self.entity = self._create_entity("ns1", "test_key", "Test Entity")

    def _create_entity(self, ns: str, key: str, title: str) -> str:
        """Helper para criar entidade."""
        with self.repo.revision("test:setup", f"criar {key}") as rev:
            draft = EntityDraft(
                namespace=ns,
                entity_type=EntityType.SYSTEM,
                stable_key=key,
                title=title,
            )
            result = rev.put_entity(draft)
            return result.target_id

    def test_supported_without_evidence_rejected(self) -> None:
        """Rejeita `supported` sem `evidence_refs`."""
        with self.repo.revision("test:create", "criar fato") as rev:
            draft = FactDraft(
                namespace="ns1",
                subject_id=self.entity,
                predicate="has_prop",
                value="true",
                scope="unit",
                nature=FactNature.IMPLEMENTED,
                epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="llm:model1",
                evidence_refs=(),  # vazio — deve rejeitar
                support_recorded_by="pipeline:verify",
            )
            with self.assertRaises(MissingEvidence) as ctx:
                rev.put_fact(draft)
            self.assertIn("evidence_refs", str(ctx.exception))

    def test_inferred_without_evidence_allowed(self) -> None:
        """Permite `inferred` sem evidência (não é sustentação forte)."""
        with self.repo.revision("test:create", "criar fato inferred") as rev:
            draft = FactDraft(
                namespace="ns1",
                subject_id=self.entity,
                predicate="has_prop",
                value="true",
                scope="unit",
                nature=FactNature.OBSERVED,
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="llm:model1",
                evidence_refs=(),  # inferred não exige evidência
            )
            result = rev.put_fact(draft)
            self.assertTrue(result.changed)
        # Verificar que foi gravado
        fact = self.repo.get_fact(result.target_id)
        self.assertIsNotNone(fact)

    def test_supported_self_declared_rejected(self) -> None:
        """Rejeita `supported` declarado por quem afirmou (LLM)."""
        # Criar evidência fake
        source = self.repo.register_source("ns1", SourceKind.CODE, "test.py")
        sv = self.repo.register_source_version(source, "v1", "hash123")

        with self.repo.revision("test:evidence", "adicionar evidência") as rev:
            ev = ev_mod.make_evidence(
                namespace="ns1",
                source_kind=SourceKind.CODE,
                content_kind=ContentKind.EXECUTABLE,
                source_version_id=sv.source_version_id,
                locator={
                    "repo": "myrepo",
                    "commit": "abc123",
                    "path": "src/main.py",
                    "start_line": 10,
                    "end_line": 15,
                    "snippet_hash": "xyz",
                },
            )
            ev_id = rev.add_evidence(ev)

        # Tentar registrar `supported` com LLM declarando a si própria
        # Nota: LLM não é origem autorizada, será rejeitada nesse ponto
        with self.repo.revision("test:create", "criar fato") as rev:
            draft = FactDraft(
                namespace="ns1",
                subject_id=self.entity,
                predicate="has_prop",
                value="true",
                scope="unit",
                nature=FactNature.IMPLEMENTED,
                epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="llm:model1",
                evidence_refs=(ev_id,),
                support_recorded_by="llm:model1",  # LLM não é origem autorizada
            )
            with self.assertRaises(SelfDeclaredSupport) as ctx:
                rev.put_fact(draft)
            # Verifica se é por origem não autorizada (em português "autorizada")
            self.assertIn("autorizada", str(ctx.exception))

    def test_supported_with_invalid_support_origin_rejected(self) -> None:
        """Rejeita `supported` com origem de suporte não autorizada."""
        source = self.repo.register_source("ns1", SourceKind.CODE, "test.py")
        sv = self.repo.register_source_version(source, "v1", "hash123")

        with self.repo.revision("test:evidence", "adicionar evidência") as rev:
            ev = ev_mod.make_evidence(
                namespace="ns1",
                source_kind=SourceKind.CODE,
                content_kind=ContentKind.EXECUTABLE,
                source_version_id=sv.source_version_id,
                locator={
                    "repo": "myrepo",
                    "commit": "abc123",
                    "path": "src/main.py",
                    "start_line": 10,
                    "end_line": 15,
                    "snippet_hash": "xyz",
                },
            )
            ev_id = rev.add_evidence(ev)

        # Tentar registrar `supported` com origem não autorizada (extractor)
        with self.repo.revision("test:create", "criar fato") as rev:
            draft = FactDraft(
                namespace="ns1",
                subject_id=self.entity,
                predicate="has_prop",
                value="true",
                scope="unit",
                nature=FactNature.IMPLEMENTED,
                epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="llm:model1",
                evidence_refs=(ev_id,),
                support_recorded_by="extractor:scanner1",  # não autorizado
            )
            with self.assertRaises(SelfDeclaredSupport) as ctx:
                rev.put_fact(draft)
            self.assertIn("não é origem autorizada", str(ctx.exception))


class TestImplementedRequiresExecutableEvidence(TestBaseRepository):
    """Invariante: `FactNature.IMPLEMENTED` exige evidência executável.

    Cobertura: comentário/docstring não sustentam implemented.
    """

    def setUp(self) -> super:
        super().setUp()
        self.entity = self._create_entity("ns1", "test_key", "Test Entity")
        self.source = self.repo.register_source("ns1", SourceKind.CODE, "test.py")
        self.sv = self.repo.register_source_version(self.source, "v1", "hash123")

    def _create_entity(self, ns: str, key: str, title: str) -> str:
        with self.repo.revision("test:setup", f"criar {key}") as rev:
            draft = EntityDraft(
                namespace=ns,
                entity_type=EntityType.SYSTEM,
                stable_key=key,
                title=title,
            )
            result = rev.put_entity(draft)
            return result.target_id

    def test_comment_evidence_does_not_support_implemented(self) -> None:
        """Evidência de comentário não sustenta `implemented` + `supported`."""
        with self.repo.revision("test:evidence", "adicionar evidência de comentário") as rev:
            ev = ev_mod.make_evidence(
                namespace="ns1",
                source_kind=SourceKind.CODE,
                content_kind=ContentKind.COMMENT,  # comentário, não executable
                source_version_id=self.sv.source_version_id,
                locator={
                    "repo": "myrepo",
                    "commit": "abc123",
                    "path": "src/main.py",
                    "start_line": 10,
                    "end_line": 10,
                    "snippet_hash": "xyz",
                },
            )
            ev_id = rev.add_evidence(ev)

        with self.repo.revision("test:create", "criar fato") as rev:
            draft = FactDraft(
                namespace="ns1",
                subject_id=self.entity,
                predicate="has_prop",
                value="true",
                scope="unit",
                nature=FactNature.IMPLEMENTED,
                epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="llm:model1",
                evidence_refs=(ev_id,),
                support_recorded_by="pipeline:verify",
            )
            with self.assertRaises(UnsupportedContentKind) as ctx:
                rev.put_fact(draft)
            self.assertIn("executável", str(ctx.exception))

    def test_docstring_evidence_does_not_support_implemented(self) -> None:
        """Evidência de docstring não sustenta `implemented` + `supported`."""
        with self.repo.revision("test:evidence", "adicionar evidência de docstring") as rev:
            ev = ev_mod.make_evidence(
                namespace="ns1",
                source_kind=SourceKind.CODE,
                content_kind=ContentKind.DOCSTRING,  # docstring, não executable
                source_version_id=self.sv.source_version_id,
                locator={
                    "repo": "myrepo",
                    "commit": "abc123",
                    "path": "src/main.py",
                    "start_line": 1,
                    "end_line": 5,
                    "snippet_hash": "xyz",
                },
            )
            ev_id = rev.add_evidence(ev)

        with self.repo.revision("test:create", "criar fato") as rev:
            draft = FactDraft(
                namespace="ns1",
                subject_id=self.entity,
                predicate="has_prop",
                value="true",
                scope="unit",
                nature=FactNature.IMPLEMENTED,
                epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="llm:model1",
                evidence_refs=(ev_id,),
                support_recorded_by="pipeline:verify",
            )
            with self.assertRaises(UnsupportedContentKind) as ctx:
                rev.put_fact(draft)
            self.assertIn("executável", str(ctx.exception))

    def test_executable_evidence_supports_implemented(self) -> None:
        """Evidência executável sustenta `implemented` + `supported`."""
        with self.repo.revision("test:evidence", "adicionar evidência executável") as rev:
            ev = ev_mod.make_evidence(
                namespace="ns1",
                source_kind=SourceKind.CODE,
                content_kind=ContentKind.EXECUTABLE,  # executável
                source_version_id=self.sv.source_version_id,
                locator={
                    "repo": "myrepo",
                    "commit": "abc123",
                    "path": "src/main.py",
                    "start_line": 10,
                    "end_line": 15,
                    "snippet_hash": "xyz",
                },
            )
            ev_id = rev.add_evidence(ev)

        with self.repo.revision("test:create", "criar fato") as rev:
            draft = FactDraft(
                namespace="ns1",
                subject_id=self.entity,
                predicate="has_prop",
                value="true",
                scope="unit",
                nature=FactNature.IMPLEMENTED,
                epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="llm:model1",
                evidence_refs=(ev_id,),
                support_recorded_by="pipeline:verify",
            )
            result = rev.put_fact(draft)
            self.assertTrue(result.changed)


class TestApprovalDoesNotChangNature(TestBaseRepository):
    """Invariante: aprovação não muda natureza nem ciclo de vida.

    Cobertura: aceite W1(b) — requisito aprovado não aparece como implementação.
    """

    def setUp(self) -> super:
        super().setUp()
        self.entity = self._create_entity("ns1", "test_key", "Test Entity")

    def _create_entity(self, ns: str, key: str, title: str) -> str:
        with self.repo.revision("test:setup", f"criar {key}") as rev:
            draft = EntityDraft(
                namespace=ns,
                entity_type=EntityType.SYSTEM,
                stable_key=key,
                title=title,
            )
            result = rev.put_entity(draft)
            return result.target_id

    def test_approved_requirement_stays_requirement(self) -> None:
        """Requisito aprovado continua DECLARED_REQUIREMENT, não vira IMPLEMENTED."""
        # Criar requisito proposto
        with self.repo.revision("test:create", "criar requisito") as rev:
            draft = FactDraft(
                namespace="ns1",
                subject_id=self.entity,
                predicate="should_have",
                value="feature_x",
                scope="business",
                nature=FactNature.DECLARED_REQUIREMENT,
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.PROPOSED,
                asserted_by="human:analyst",
            )
            result = rev.put_fact(draft)
            fact_id = result.target_id

        # Aprovar o requisito
        with self.repo.revision("test:approve", "aprovar requisito") as rev:
            rev.approve_fact(fact_id, "human:pm", ApprovalState.APPROVED)

        # Verificar que natureza NOT mudou
        fact = self.repo.get_fact(fact_id, lifecycle=None)
        self.assertEqual(fact.nature, FactNature.DECLARED_REQUIREMENT)
        self.assertEqual(fact.approval_state, ApprovalState.APPROVED)
        self.assertEqual(fact.lifecycle_status, LifecycleStatus.PROPOSED)

    def test_approve_fact_preserves_all_properties(self) -> None:
        """Aprovação preserva natureza, epistêmica e ciclo de vida."""
        source = self.repo.register_source("ns1", SourceKind.CODE, "test.py")
        sv = self.repo.register_source_version(source, "v1", "hash123")

        with self.repo.revision("test:evidence", "adicionar evidência") as rev:
            ev = ev_mod.make_evidence(
                namespace="ns1",
                source_kind=SourceKind.CODE,
                content_kind=ContentKind.EXECUTABLE,
                source_version_id=sv.source_version_id,
                locator={
                    "repo": "myrepo",
                    "commit": "abc123",
                    "path": "src/main.py",
                    "start_line": 10,
                    "end_line": 15,
                    "snippet_hash": "xyz",
                },
            )
            ev_id = rev.add_evidence(ev)

        # Criar fato
        with self.repo.revision("test:create", "criar fato") as rev:
            draft = FactDraft(
                namespace="ns1",
                subject_id=self.entity,
                predicate="has_feature",
                value="true",
                scope="system",
                nature=FactNature.IMPLEMENTED,
                epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.PROPOSED,
                asserted_by="llm:model1",
                evidence_refs=(ev_id,),
                support_recorded_by="pipeline:verify",
            )
            result = rev.put_fact(draft)
            fact_id = result.target_id

        # Aprovar
        with self.repo.revision("test:approve", "aprovar") as rev:
            rev.approve_fact(fact_id, "human:pm", ApprovalState.APPROVED)

        fact = self.repo.get_fact(fact_id, lifecycle=None)
        self.assertEqual(fact.nature, FactNature.IMPLEMENTED)
        self.assertEqual(fact.epistemic_status, EpistemicStatus.SUPPORTED)
        self.assertEqual(fact.lifecycle_status, LifecycleStatus.PROPOSED)


class TestIdempotentIngestion(TestBaseRepository):
    """Invariante: reingestão idêntica não duplica (aceite W1(c))."""

    def setUp(self) -> super:
        super().setUp()
        self.entity = self._create_entity("ns1", "test_key", "Test Entity")

    def _create_entity(self, ns: str, key: str, title: str) -> str:
        with self.repo.revision("test:setup", f"criar {key}") as rev:
            draft = EntityDraft(
                namespace=ns,
                entity_type=EntityType.SYSTEM,
                stable_key=key,
                title=title,
            )
            result = rev.put_entity(draft)
            return result.target_id

    def test_identical_fact_ingestion_not_duplicated(self) -> None:
        """Reingestão idêntica de fato não cria nova revisão."""
        draft = FactDraft(
            namespace="ns1",
            subject_id=self.entity,
            predicate="has_prop",
            value="true",
            scope="unit",
            nature=FactNature.OBSERVED,
            epistemic_status=EpistemicStatus.INFERRED,
            lifecycle_status=LifecycleStatus.CURRENT,
            asserted_by="llm:model1",
        )

        # Primeira ingestão
        with self.repo.revision("test:ingest1", "ingerir fato") as rev:
            result1 = rev.put_fact(draft)
            self.assertTrue(result1.changed)
            fact_id1 = result1.target_id
            rev1 = result1.revision_id

        # Segunda ingestão idêntica
        with self.repo.revision("test:ingest2", "ingerir fato novamente") as rev:
            result2 = rev.put_fact(draft)
            self.assertFalse(result2.changed)  # não mudou
            fact_id2 = result2.target_id
            rev2 = result2.revision_id

        # Mesmo fato_id e mesma revisão anterior
        self.assertEqual(fact_id1, fact_id2)
        self.assertEqual(rev1, rev2)

    def test_identical_entity_ingestion_not_duplicated(self) -> None:
        """Reingestão idêntica de entidade não cria nova revisão."""
        draft = EntityDraft(
            namespace="ns1",
            entity_type=EntityType.COMPONENT,
            stable_key="comp1",
            title="Component One",
        )

        with self.repo.revision("test:ingest1", "ingerir entidade") as rev:
            result1 = rev.put_entity(draft)
            self.assertTrue(result1.changed)
            eid1 = result1.target_id
            rev1 = result1.revision_id

        with self.repo.revision("test:ingest2", "ingerir entidade novamente") as rev:
            result2 = rev.put_entity(draft)
            self.assertFalse(result2.changed)
            eid2 = result2.target_id
            rev2 = result2.revision_id

        self.assertEqual(eid1, eid2)
        self.assertEqual(rev1, rev2)


class TestSupersedeCycleRejected(TestBaseRepository):
    """Invariante: ciclos de substituição são rejeitados (aceite W1(d) parcial)."""

    def setUp(self) -> super:
        super().setUp()
        # Criar entidades para relações
        self.e1 = self._create_entity("ns1", "key1", "Entity 1")
        self.e2 = self._create_entity("ns1", "key2", "Entity 2")
        self.e3 = self._create_entity("ns1", "key3", "Entity 3")

    def _create_entity(self, ns: str, key: str, title: str) -> str:
        with self.repo.revision("test:setup", f"criar {key}") as rev:
            draft = EntityDraft(
                namespace=ns,
                entity_type=EntityType.COMPONENT,
                stable_key=key,
                title=title,
            )
            result = rev.put_entity(draft)
            return result.target_id

    def test_self_supersedes_rejected(self) -> None:
        """Rejeita A supersedes A."""
        with self.repo.revision("test:create", "criar self-supersede") as rev:
            draft = RelationDraft(
                namespace="ns1",
                source_entity_id=self.e1,
                relation_type=repo_mod.RelationType.SUPERSEDES,
                target_entity_id=self.e1,  # mesmo que source
                scope="version",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            with self.assertRaises(SupersedesCycle) as ctx:
                rev.put_relation(draft)
            self.assertIn("ciclo", str(ctx.exception).lower())

    def test_cyclic_supersedes_rejected(self) -> None:
        """Rejeita ciclo A -> B -> A."""
        # Gravar A supersedes B
        with self.repo.revision("test:create", "A supersedes B") as rev:
            draft1 = RelationDraft(
                namespace="ns1",
                source_entity_id=self.e1,
                relation_type=repo_mod.RelationType.SUPERSEDES,
                target_entity_id=self.e2,
                scope="version",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            rev.put_relation(draft1)

        # Tentar gravar B supersedes A — fecharia ciclo
        with self.repo.revision("test:create", "B supersedes A (ciclo)") as rev:
            draft2 = RelationDraft(
                namespace="ns1",
                source_entity_id=self.e2,
                relation_type=repo_mod.RelationType.SUPERSEDES,
                target_entity_id=self.e1,
                scope="version",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            with self.assertRaises(SupersedesCycle) as ctx:
                rev.put_relation(draft2)
            self.assertIn("ciclo", str(ctx.exception).lower())


class TestQueryDefaultLifecycleFilter(TestBaseRepository):
    """Invariante: consulta padrão só devolve CURRENT (aceite W1(e))."""

    def setUp(self) -> super:
        super().setUp()
        self.entity = self._create_entity("ns1", "test_key", "Test Entity")

    def _create_entity(self, ns: str, key: str, title: str) -> str:
        with self.repo.revision("test:setup", f"criar {key}") as rev:
            draft = EntityDraft(
                namespace=ns,
                entity_type=EntityType.SYSTEM,
                stable_key=key,
                title=title,
            )
            result = rev.put_entity(draft)
            return result.target_id

    def test_default_query_excludes_historical(self) -> None:
        """Consulta padrão sem `lifecycle=None` não retorna HISTORICAL."""
        # Criar fato CURRENT
        with self.repo.revision("test:create", "criar fato current") as rev:
            draft = FactDraft(
                namespace="ns1",
                subject_id=self.entity,
                predicate="prop",
                value="val",
                scope="scope",
                nature=FactNature.OBSERVED,
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:user",
            )
            result = rev.put_fact(draft)
            fact_id = result.target_id

        # Movê-lo para HISTORICAL
        with self.repo.revision("test:move", "mover para historical") as rev:
            rev.set_fact_lifecycle(fact_id, LifecycleStatus.HISTORICAL, "human:user")

        # Consulta padrão não deve retornar
        fact = self.repo.get_fact(fact_id)  # sem lifecycle=None
        self.assertIsNone(fact)

        # Consulta com lifecycle=None deve retornar
        fact_all = self.repo.get_fact(fact_id, lifecycle=None)
        self.assertIsNotNone(fact_all)
        self.assertEqual(fact_all.lifecycle_status, LifecycleStatus.HISTORICAL)

    def test_default_query_excludes_superseded(self) -> None:
        """Consulta padrão não retorna SUPERSEDED."""
        with self.repo.revision("test:create", "criar entidade") as rev:
            draft = EntityDraft(
                namespace="ns1",
                entity_type=EntityType.COMPONENT,
                stable_key="comp1",
                title="Component",
                lifecycle_status=LifecycleStatus.CURRENT,
            )
            result = rev.put_entity(draft)
            entity_id = result.target_id

        # Movê-la para SUPERSEDED
        with self.repo.revision("test:move", "mover para superseded") as rev:
            rev.set_entity_lifecycle(entity_id, LifecycleStatus.SUPERSEDED, "human:user")

        # Consulta padrão não deve retornar
        entity = self.repo.get_entity(entity_id)
        self.assertIsNone(entity)

        # Consulta com lifecycle=None deve retornar
        entity_all = self.repo.get_entity(entity_id, lifecycle=None)
        self.assertIsNotNone(entity_all)


class TestRevisionRollback(TestBaseRepository):
    """Invariante: exceção em revisão não persiste nada."""

    def setUp(self) -> super:
        super().setUp()
        self.entity = self._create_entity("ns1", "test_key", "Test Entity")

    def _create_entity(self, ns: str, key: str, title: str) -> str:
        with self.repo.revision("test:setup", f"criar {key}") as rev:
            draft = EntityDraft(
                namespace=ns,
                entity_type=EntityType.SYSTEM,
                stable_key=key,
                title=title,
            )
            result = rev.put_entity(draft)
            return result.target_id

    def test_revision_exception_rolls_back(self) -> None:
        """Exceção durante revisão desfaz todas as mudanças."""
        # Contar entidades antes
        entities_before = self.repo.find_entities("ns1")
        count_before = len(entities_before)

        try:
            with self.repo.revision("test:create", "criar e falhar") as rev:
                draft = EntityDraft(
                    namespace="ns1",
                    entity_type=EntityType.COMPONENT,
                    stable_key="fail_entity",
                    title="Should Fail",
                )
                rev.put_entity(draft)
                # Forçar erro
                raise ValueError("teste de rollback")
        except ValueError:
            pass

        # Contar entidades depois — deve ser igual
        entities_after = self.repo.find_entities("ns1")
        count_after = len(entities_after)
        self.assertEqual(count_before, count_after)


class TestRelationPairValidation(TestBaseRepository):
    """Invariante: pares de tipo inválidos são rejeitados."""

    def setUp(self) -> super:
        super().setUp()
        # SYSTEM só pode conter TECH
        self.system = self._create_entity("ns1", "sys1", "System", EntityType.SYSTEM)
        # SOURCE não pode estar na relação CONTAINS como origem
        self.source = self._create_entity("ns1", "src1", "Source", EntityType.SOURCE)

    def _create_entity(
        self, ns: str, key: str, title: str, etype: EntityType
    ) -> str:
        with self.repo.revision("test:setup", f"criar {key}") as rev:
            draft = EntityDraft(
                namespace=ns,
                entity_type=etype,
                stable_key=key,
                title=title,
            )
            result = rev.put_entity(draft)
            return result.target_id

    def test_invalid_relation_pair_rejected(self) -> None:
        """Rejeita SOURCE contains ANYTHING."""
        with self.repo.revision("test:create", "criar relação inválida") as rev:
            draft = RelationDraft(
                namespace="ns1",
                source_entity_id=self.source,  # SOURCE
                relation_type=repo_mod.RelationType.CONTAINS,
                target_entity_id=self.system,  # SYSTEM
                scope="arch",
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="human:analyst",
            )
            with self.assertRaises(InvalidRelationPair) as ctx:
                rev.put_relation(draft)
            self.assertIn("não aceita", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
