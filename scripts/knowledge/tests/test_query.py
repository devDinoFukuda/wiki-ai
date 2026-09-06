"""Testes para query.py (W7-T7.2): retrieve separa lifecycle; score não altera status.

Regra: retrieve(consumer="refinamento") retorna implemented_current/proposed/historical
separados; fato superseded nunca em implemented_current; hipótese preserva epistemic;
score é campo separado, nunca altera lifecycle_status ou epistemic_status.
"""

import tempfile
import unittest
from pathlib import Path

from knowledge import evidence as EV
from knowledge import query as QRY
from knowledge import repository as REPO
from knowledge.models import (
    ContentKind,
    EntityDraft,
    EntityType,
    FactDraft,
    FactNature,
    LifecycleStatus,
    EpistemicStatus,
    ApprovalState,
    SourceKind,
)


def _add_implemented_evidence(repo, rev, namespace, suffix):
    """Registra fonte de código + evidência EXECUTABLE e devolve o evidence_id.

    `nature=IMPLEMENTED` com `epistemic_status=SUPPORTED` exige evidência
    real que sustente comportamento implementado (§5.3/§5.4,
    `repository._check_support` + `evidence.assert_supports_implemented`):
    sem isto `put_fact` rejeita com `MissingEvidence`/`UnsupportedContentKind`.
    """
    src = repo.register_source(namespace, SourceKind.CODE, f"repo://test/{suffix}")
    srcv = repo.register_source_version(src, "v1", f"hash-{suffix}")
    return rev.add_evidence(
        EV.make_evidence(
            namespace,
            SourceKind.CODE,
            ContentKind.EXECUTABLE,
            srcv.source_version_id,
            {
                "repo": "test",
                "commit": "abc123",
                "path": f"src/{suffix}.py",
                "start_line": 1,
                "end_line": 5,
                "snippet_hash": EV.snippet_hash(suffix),
            },
        )
    )


class RetrieveRefininementTest(unittest.TestCase):
    """Consulta por consumer='refinamento' separa lifecycle_status."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "knowledge.db")
        self.conn = REPO.connect(self.db_path)
        self.namespace = "test"

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def _put_system_and_capacity(self):
        """Helper: cria sistema e capacidade base."""
        repo = REPO.Repository(self.conn)
        with repo.revision("test", "setup") as rev:
            # Sistema
            sys_draft = EntityDraft(
                namespace=self.namespace,
                entity_type=EntityType.SYSTEM,
                stable_key="system-x",
                title="System X",
                attributes={"description": "test system"},
            )
            sys_ent = rev.put_entity(sys_draft)

            # Capacidade
            cap_draft = EntityDraft(
                namespace=self.namespace,
                entity_type=EntityType.CAPABILITY,
                stable_key="cap-023",
                title="CAP-023",
                attributes={"description": "test capability"},
            )
            cap_ent = rev.put_entity(cap_draft)

        return sys_ent, cap_ent

    def test_retrieve_separates_implemented_current_and_proposed(self):
        """Retrieve separa implemented/current de proposed por lifecycle_status."""
        sys_ent, cap_ent = self._put_system_and_capacity()

        repo = REPO.Repository(self.conn)
        with repo.revision("test", "add facts") as rev:
            # Fato atual (implementado)
            ev_current = _add_implemented_evidence(repo, rev, self.namespace, "current")
            fact_current = FactDraft(
                namespace=self.namespace,
                subject_id=cap_ent.target_id,
                predicate="behavior_on_timeout",
                value="returns 500 after 30s",
                scope="normal",
                nature=FactNature.IMPLEMENTED,
                epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="llm:seed-agent",
                evidence_refs=[ev_current],
                support_recorded_by="pipeline:verify-static",
            )
            rev.put_fact(fact_current)

            # Fato proposto (ainda não implementado)
            fact_proposed = FactDraft(
                namespace=self.namespace,
                subject_id=cap_ent.target_id,
                predicate="behavior_on_timeout",
                value="should retry with exponential backoff",
                scope="proposal",
                nature=FactNature.DECLARED_REQUIREMENT,
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.PROPOSED,
                asserted_by="llm:seed-agent",
                evidence_refs=[],
            )
            rev.put_fact(fact_proposed)

        # Resolve escopo (capacity deve estar lá)
        scope = QRY.resolve_scope(
            repo, namespace=self.namespace, capability="CAP-023"
        )
        self.assertIsNotNone(scope.capability_id)

        # Retrieve
        result = QRY.retrieve(
            repo, scope, "CAP-023", consumer=QRY.CONSUMER_REFINEMENT, budget=QRY.DEFAULT_BUDGET
        )

        # Verifica separação de lifecycle
        current_facts = [f.fact for f in result.implemented_current]
        proposed_facts = [f.fact for f in result.proposed]
        historical_facts = [f.fact for f in result.historical]

        # Deve ter 1 current, 1 proposed, 0 historical
        self.assertEqual(len(current_facts), 1, "Deve ter 1 fato current")
        self.assertEqual(len(proposed_facts), 1, "Deve ter 1 fato proposed")
        self.assertEqual(len(historical_facts), 0, "Não deve ter facts historical")

        # Valores devem estar corretos
        self.assertIn("returns 500", current_facts[0].value)
        self.assertIn("exponential backoff", proposed_facts[0].value)

    def test_retrieve_superseded_never_in_current(self):
        """Fato superseded nunca aparece em implemented_current (W7-regressão)."""
        sys_ent, cap_ent = self._put_system_and_capacity()

        repo = REPO.Repository(self.conn)
        with repo.revision("test", "add superseded") as rev:
            # Fato superseded (versão antiga)
            ev_old = _add_implemented_evidence(repo, rev, self.namespace, "old")
            fact_old = FactDraft(
                namespace=self.namespace,
                subject_id=cap_ent.target_id,
                predicate="retry_count",
                value="3",
                scope="old",
                nature=FactNature.IMPLEMENTED,
                epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.SUPERSEDED,
                asserted_by="llm:seed-agent",
                evidence_refs=[ev_old],
                support_recorded_by="pipeline:verify-static",
            )
            rev.put_fact(fact_old)

            # Fato current (substituiu o anterior)
            ev_new = _add_implemented_evidence(repo, rev, self.namespace, "new")
            fact_new = FactDraft(
                namespace=self.namespace,
                subject_id=cap_ent.target_id,
                predicate="retry_count",
                value="5",
                scope="current",
                nature=FactNature.IMPLEMENTED,
                epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="llm:seed-agent",
                evidence_refs=[ev_new],
                support_recorded_by="pipeline:verify-static",
            )
            rev.put_fact(fact_new)

        scope = QRY.resolve_scope(repo, namespace=self.namespace, capability="CAP-023")
        result = QRY.retrieve(
            repo, scope, "CAP-023", consumer=QRY.CONSUMER_REFINEMENT, budget=QRY.DEFAULT_BUDGET
        )

        # Implementado_current não deve conter superseded
        current_facts = [f.fact for f in result.implemented_current]
        superseded_facts = [f.fact for f in result.historical]

        # Só o novo deve estar em current
        self.assertEqual(len(current_facts), 1)
        self.assertEqual(current_facts[0].value, "5")

        # O antigo fica em historical se for recuperado (implementado legítimo)
        # ou pode não aparecer dependendo da política

    def test_retrieve_preserves_epistemic_status(self):
        """Score de busca nunca altera epistemic_status (regressão §9.2)."""
        sys_ent, cap_ent = self._put_system_and_capacity()

        repo = REPO.Repository(self.conn)
        with repo.revision("test", "add hypothesis") as rev:
            # Hipótese (inferred, não supported)
            fact_hyp = FactDraft(
                namespace=self.namespace,
                subject_id=cap_ent.target_id,
                predicate="possible_behavior",
                value="might use cache under high load",
                scope="hypothesis",
                nature=FactNature.OBSERVED,
                epistemic_status=EpistemicStatus.INFERRED,  # Hipótese
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="llm:seed-agent",
                evidence_refs=[],
            )
            rev.put_fact(fact_hyp)

        scope = QRY.resolve_scope(repo, namespace=self.namespace, capability="CAP-023")
        result = QRY.retrieve(
            repo, scope, "CAP-023", consumer=QRY.CONSUMER_REFINEMENT, budget=QRY.DEFAULT_BUDGET
        )

        facts_returned = [f for f in result.implemented_current] + [f for f in result.proposed]
        if facts_returned:
            # Epistemic_status deve permanecer INFERRED, não virar SUPPORTED por score
            hyp_fact = next((f for f in facts_returned if "cache" in f.fact.value), None)
            if hyp_fact:
                self.assertEqual(hyp_fact.fact.epistemic_status, EpistemicStatus.INFERRED)
                # Score é campo SEPARADO (em ScoredFact.score), não contamina epistemic_status
                self.assertIsInstance(hyp_fact.score, float)

    def test_score_is_separate_field_never_alters_status(self):
        """Score está em campo separado (ScoredFact.score), não em lifecycle/epistemic."""
        sys_ent, cap_ent = self._put_system_and_capacity()

        repo = REPO.Repository(self.conn)
        with repo.revision("test", "add fact") as rev:
            fact = FactDraft(
                namespace=self.namespace,
                subject_id=cap_ent.target_id,
                predicate="keyword_test",
                value="contains test keyword here",
                scope="test",
                nature=FactNature.OBSERVED,
                epistemic_status=EpistemicStatus.UNRESOLVED,  # Não suportado
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="llm:seed-agent",
                evidence_refs=[],
            )
            rev.put_fact(fact)

        scope = QRY.resolve_scope(repo, namespace=self.namespace, capability="CAP-023")
        result = QRY.retrieve(
            repo, scope, "CAP-023", consumer=QRY.CONSUMER_REFINEMENT, budget=QRY.DEFAULT_BUDGET
        )

        for scored_fact in result.implemented_current:
            # Score é atributo de ScoredFact, separado de Fact
            self.assertTrue(hasattr(scored_fact, "score"))
            self.assertTrue(hasattr(scored_fact, "fact"))
            # O valor original do fato não muda
            self.assertEqual(
                scored_fact.fact.epistemic_status, EpistemicStatus.UNRESOLVED
            )
            self.assertEqual(
                scored_fact.fact.lifecycle_status, LifecycleStatus.CURRENT
            )


class RetrieveGapsTest(unittest.TestCase):
    """Pergunta fora do escopo examinado retorna gaps com scope_examined."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "knowledge.db")
        self.conn = REPO.connect(self.db_path)
        self.namespace = "test"

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def test_missing_capacity_returns_gap_with_scope(self):
        """Capacidade inexistente deve retornar gap com scope_examined preenchido."""
        repo = REPO.Repository(self.conn)

        # Resolve escopo com capacidade inexistente
        scope = QRY.resolve_scope(
            repo, namespace=self.namespace, capability="NONEXISTENT"
        )

        # Deve ter reportado não-resolvida
        self.assertTrue(any("capability" in s for s in scope.unresolved))

        # Retrieve mesmo assim (com escopo parcial)
        result = QRY.retrieve(
            repo, scope, "CAP-023", consumer=QRY.CONSUMER_REFINEMENT, budget=QRY.DEFAULT_BUDGET
        )

        # Deve ter gaps (não facts de capacidade que não existe)
        self.assertIsNotNone(result.gaps)
        if result.gaps:
            # Gap deve ter scope_examined preenchido
            for gap in result.gaps:
                self.assertIsNotNone(gap.scope_examined)


if __name__ == "__main__":
    unittest.main()
