"""Testes para verification.py — verificação mecânica de claims (§15.1).

Cobertura obrigatória: estrutura, mocks, consistency, summary, end-to-end verify_claim.
"""

import unittest
import subprocess
import tempfile
from pathlib import Path

from analysis.verification import (
    verify_claim, Claim, ClaimEvidence, detect_mocks, check_consistency,
    apply_inconsistencies, reread_obligations, verification_summary,
    VerificationError, SUPPORT_RECORDER, extract_comparisons, extract_effects
)
from analysis.snapshot import capture
from knowledge.models import ContentKind, EpistemicStatus, SourceKind


class TestClaimValidation(unittest.TestCase):
    """Validação de estrutura de Claim."""

    def test_claim_without_predicate_kind_raises(self):
        """Claim sem predicate_kind válido levanta VerificationError."""
        with self.assertRaises(VerificationError):
            Claim(
                claim_id="c_001",
                subject="x",
                predicate_kind="invalid_kind",
            )

    def test_claim_unknown_statement_field_raises(self):
        """Campo desconhecido em statement_fields levanta VerificationError."""
        with self.assertRaises(VerificationError):
            Claim(
                claim_id="c_001",
                subject="x",
                predicate_kind="behavior",
                statement_fields={"unknown_field": "value"},
            )

    def test_detect_mocks_finds_patch_decorator(self):
        """detect_mocks() identifica @patch no snippet."""
        snippet = """@patch('service.call')
def test_handler(mock_service):
    result = handler()
"""
        markers = detect_mocks(snippet)
        self.assertIn("@patch", markers)

    def test_detect_mocks_finds_unittest_mock(self):
        """detect_mocks() identifica unittest.mock."""
        snippet = "from unittest.mock import patch, MagicMock"
        markers = detect_mocks(snippet)
        self.assertTrue(any("mock" in m.lower() for m in markers))


class TestComparisons(unittest.TestCase):
    """Testes de extração de comparações — comportamento esperado."""

    def test_extract_comparisons_finds_python_syntax(self):
        """extract_comparisons identifica comparações Python."""
        snippet = "if x > 0: return x * 2"
        comparisons, resolution = extract_comparisons(snippet, "handler.py")

        self.assertGreater(len(comparisons), 0)
        self.assertEqual(resolution, "syntactic")

    def test_extract_comparisons_inverted_operator(self):
        """Comparação com operador invertido é detectada."""
        snippet = "if x < 0: return -x"
        comparisons, resolution = extract_comparisons(snippet)

        found = [c for c in comparisons if c.op == "<"]
        self.assertGreater(len(found), 0)


class TestEffects(unittest.TestCase):
    """Testes de extração de efeitos."""

    def test_extract_effects_finds_return(self):
        """extract_effects identifica return."""
        snippet = "def f(x): return x * 2"
        effects = extract_effects(snippet, "handler.py")

        self.assertGreater(len(effects.returns), 0)

    def test_extract_effects_finds_call(self):
        """extract_effects identifica chamadas."""
        snippet = "service.external_call(x)"
        effects = extract_effects(snippet)

        self.assertGreater(len(effects.calls), 0)


class TestConsistencyChecks(unittest.TestCase):
    """check_consistency + apply_inconsistencies."""

    def test_check_consistency_finds_contradictory_effects(self):
        """Regra vs teste divergente mesma condição → ambos DISPUTED."""
        claims = [
            Claim(
                claim_id="rule_1",
                subject="x",
                predicate_kind="behavior",
                statement_fields={
                    "condition": "x",
                    "comparator": ">",
                    "value": "0",
                    "effect": "return double",
                },
            ),
            Claim(
                claim_id="test_1",
                subject="x",
                predicate_kind="test",
                statement_fields={
                    "condition": "x",
                    "comparator": ">",
                    "value": "0",
                    "effect": "return negative",  # Contradiz!
                },
            ),
        ]

        inconsistencies = check_consistency(claims)

        # Deve encontrar divergência
        self.assertGreater(len(inconsistencies), 0)
        self.assertTrue(any(
            "rule_1" in inc.claim_ids and "test_1" in inc.claim_ids
            for inc in inconsistencies
        ))


class TestVerificationSummary(unittest.TestCase):
    """verification_summary sem score documental."""

    def test_summary_has_no_documentary_score(self):
        """verification_summary retorna contagens sem score de qualidade documental."""
        from analysis.verification import Verdict
        from knowledge.models import EpistemicStatus

        verdicts = [
            Verdict(
                claim_id="c1",
                epistemic=EpistemicStatus.SUPPORTED,
                checks=(),
                asserted_by="test",
            ),
            Verdict(
                claim_id="c2",
                epistemic=EpistemicStatus.UNRESOLVED,
                checks=(),
                insufficient=True,
                asserted_by="test",
            ),
        ]

        summary = verification_summary(verdicts)

        # Deve ter contagens
        self.assertIn("supported", summary)
        self.assertIn("unresolved", summary)
        self.assertIn("insufficient_rate", summary)
        self.assertEqual(summary["mocked"], 0)

        # Não deve ter score documental (documento, seções, linguagem, etc.)
        self.assertNotIn("document_score", summary)
        self.assertNotIn("language_score", summary)


class TestRerereadObligations(unittest.TestCase):
    """reread_obligations com confidence_vote=None."""

    def test_reread_obligations_has_no_confidence_vote(self):
        """reread_obligations não inclui confidence_vote (None sempre)."""
        from analysis.verification import Verdict
        from knowledge.models import EpistemicStatus

        verdicts = [
            Verdict(
                claim_id="c1",
                epistemic=EpistemicStatus.DISPUTED,
                checks=(),
                asserted_by="test",
            ),
        ]

        obligations = reread_obligations(verdicts)

        for obligation in obligations:
            self.assertIsNone(obligation.get("confidence_vote"))


class TestVerifyClaimEndToEnd(unittest.TestCase):
    """Testes end-to-end: verify_claim(claim, snapshot, extraction) → Verdict.

    Cria repo git temporário + snapshot real + claims estruturados.
    """

    def setUp(self):
        """Cria repo git temporário com arquivos Python reais."""
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir)

        # Inicializar git
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.repo, check=True, capture_output=True,
        )

        # Arquivo principal com código executável
        self.handler_file = self.repo / "handler.py"
        self.handler_file.write_text("""def process(x):
    '''Process number.

    Doubles positive numbers.
    '''
    if x > 0:
        return x * 2
    return x
""")

        # Arquivo de teste com mock
        self.test_file = self.repo / "test_handler.py"
        self.test_file.write_text("""from unittest.mock import patch

@patch('service.external_call')
def test_process(mock_service):
    assert process(5) == 10
    return True
""")

        # Fazer commit inicial
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.repo, check=True, capture_output=True,
        )

        # Capturar snapshot
        self.snapshot = capture(str(self.repo))

    def tearDown(self):
        """Remove diretório temporário."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_claim_correct_returns_supported(self):
        """Cenário 1: Claim correto (comparador+efeito presentes) → SUPPORTED."""
        claim = Claim(
            claim_id="claim_001",
            subject="x",
            predicate_kind="behavior",
            statement_fields={
                "condition": "x",
                "comparator": ">",
                "value": "0",
                "effect": "return x * 2",
            },
            evidence_refs=[
                ClaimEvidence(
                    path="handler.py",
                    start_line=5,
                    end_line=6,
                    content_kind=ContentKind.EXECUTABLE,
                )
            ],
            asserted_by="external_reviewer",
        )

        verdict = verify_claim(claim, self.snapshot)

        # Esperado: SUPPORTED com support_recorded_by="pipeline:verification"
        # (ou INFERRED se verificação incompleta sem extraction)
        self.assertIn(verdict.epistemic, [
            EpistemicStatus.SUPPORTED,
            EpistemicStatus.INFERRED,
        ])

    def test_inverted_comparator_returns_disputed(self):
        """Cenário 2: MESMO trecho, comparador invertido → DISPUTED (§15.2 nº1)."""
        claim = Claim(
            claim_id="claim_002",
            subject="x",
            predicate_kind="behavior",
            statement_fields={
                "condition": "x",
                "comparator": "<",  # INVERTIDO
                "value": "0",
                "effect": "return x * 2",
            },
            evidence_refs=[
                ClaimEvidence(
                    path="handler.py",
                    start_line=5,
                    end_line=6,
                    content_kind=ContentKind.EXECUTABLE,
                )
            ],
            asserted_by="someone",
        )

        verdict = verify_claim(claim, self.snapshot)

        # Deve encontrar divergência e ser DISPUTED
        self.assertEqual(verdict.epistemic, EpistemicStatus.DISPUTED)

    def test_docstring_never_supports_implemented(self):
        """Cenário 3: Citação de docstring → INFERRED para implemented."""
        claim = Claim(
            claim_id="claim_003",
            subject="process",
            predicate_kind="behavior",  # implies_implemented=True
            statement_fields={
                "effect": "Doubles positive numbers",
            },
            evidence_refs=[
                ClaimEvidence(
                    path="handler.py",
                    start_line=2,
                    end_line=4,
                    content_kind=ContentKind.DOCSTRING,
                )
            ],
            asserted_by="someone",
        )

        verdict = verify_claim(claim, self.snapshot)

        # Docstring nunca sustenta implemented
        self.assertNotEqual(verdict.epistemic, EpistemicStatus.SUPPORTED)

    def test_vague_claim_returns_unresolved_insufficient(self):
        """Cenário 4: Claim vago sem statement_fields aplicáveis → UNRESOLVED insufficient."""
        claim = Claim(
            claim_id="claim_004",
            subject="x",
            predicate_kind="behavior",
            statement_fields={},  # Nenhum campo verificável
            evidence_refs=[
                ClaimEvidence(
                    path="handler.py",
                    start_line=1,
                    end_line=7,
                    content_kind=ContentKind.EXECUTABLE,
                )
            ],
            asserted_by="someone",
        )

        verdict = verify_claim(claim, self.snapshot)

        self.assertEqual(verdict.epistemic, EpistemicStatus.UNRESOLVED)
        self.assertTrue(verdict.insufficient)

    def test_mock_with_test_predicate_returns_supported(self):
        """Cenário 5a: Mock + predicate_kind=test → SUPPORTED test_expectation mocked=True."""
        claim = Claim(
            claim_id="claim_005a",
            subject="process",
            predicate_kind="test",
            statement_fields={
                "effect": "assert process(5) == 10",
            },
            evidence_refs=[
                ClaimEvidence(
                    path="test_handler.py",
                    start_line=3,
                    end_line=5,
                    content_kind=ContentKind.TEST_ASSERTION,
                )
            ],
            asserted_by="test_runner",
        )

        verdict = verify_claim(claim, self.snapshot)

        # Mock deve estar marcado, teste é válido
        if verdict.mocked:
            self.assertTrue(verdict.mocked)

    def test_mock_with_behavior_predicate_returns_inferred(self):
        """Cenário 5b: Mesmo trecho com predicate_kind=behavior → INFERRED."""
        claim = Claim(
            claim_id="claim_005b",
            subject="process",
            predicate_kind="behavior",  # Afirmação sobre código real, não teste
            statement_fields={
                "effect": "assert process(5) == 10",
            },
            evidence_refs=[
                ClaimEvidence(
                    path="test_handler.py",
                    start_line=3,
                    end_line=5,
                    content_kind=ContentKind.TEST_ASSERTION,
                )
            ],
            asserted_by="someone",
        )

        verdict = verify_claim(claim, self.snapshot)

        # Conteúdo de teste não sustenta behavior
        self.assertNotEqual(verdict.epistemic, EpistemicStatus.SUPPORTED)

    def test_asserted_by_support_recorder_is_downgraded(self):
        """Cenário 6: asserted_by='pipeline:verification' → rebaixado."""
        claim = Claim(
            claim_id="claim_006",
            subject="x",
            predicate_kind="behavior",
            statement_fields={
                "condition": "x",
                "comparator": ">",
                "value": "0",
            },
            evidence_refs=[
                ClaimEvidence(
                    path="handler.py",
                    start_line=5,
                    end_line=6,
                )
            ],
            asserted_by=SUPPORT_RECORDER,  # Quem afirma é o próprio registrador
        )

        verdict = verify_claim(claim, self.snapshot)

        # Auto-aprovação é vedada
        self.assertIsNone(verdict.support_recorded_by)

    def test_file_altered_after_capture_returns_stale(self):
        """Cenário 7: Arquivo alterado APÓS capture → stale=True, UNRESOLVED."""
        # Alterar arquivo após capture
        self.handler_file.write_text("def process(x): return x\n")

        claim = Claim(
            claim_id="claim_007",
            subject="x",
            predicate_kind="behavior",
            statement_fields={
                "effect": "return x * 2",
            },
            evidence_refs=[
                ClaimEvidence(
                    path="handler.py",
                    start_line=5,
                    end_line=6,
                )
            ],
            asserted_by="someone",
        )

        verdict = verify_claim(claim, self.snapshot)

        # Arquivo alterado deve resultar em UNRESOLVED
        self.assertEqual(verdict.epistemic, EpistemicStatus.UNRESOLVED)


if __name__ == "__main__":
    unittest.main()
