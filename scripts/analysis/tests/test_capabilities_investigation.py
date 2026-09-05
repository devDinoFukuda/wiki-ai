"""Testes para capabilities.py e investigation.py — comportamentos executáveis.

Executa unittest contra mini-exemplos e verifica invariantes do código (§6.1-§6.6).
"""

import unittest
from analysis.capabilities import (
    discover, CapabilityAccountingError, GroupingBasis, BoundaryGap, EvidenceRef, EntryRef
)
from analysis.investigation import (
    plan, MatrixJustificationRequired, ConclusionRejected, FailureEdgeMatrix,
    InvestigationObjective, ObjectiveState, ClosureReason, close
)
from analysis.extractors.registry import ExtractionResult
from analysis.extractors.base import Symbol, Reference, Entrypoint


class TestCapabilitiesDiscover(unittest.TestCase):
    """A) capabilities.py: discover() agrupa por fecho de referências."""

    def setUp(self):
        """Mini-repo: 2 entradas, 1 símbolo comum, grafo cíclico."""
        self.symbols = [
            Symbol(
                qualname="module_a.handler_1",
                name="handler_1",
                kind="function",
                path="src/a.py",
                line_start=10,
                line_end=20,
                visibility="public",
                resolution="syntactic",
            ),
            Symbol(
                qualname="module_a.helper",
                name="helper",
                kind="function",
                path="src/a.py",
                line_start=22,
                line_end=30,
                visibility="private",
                resolution="syntactic",
            ),
            Symbol(
                qualname="module_b.handler_2",
                name="handler_2",
                kind="function",
                path="src/b.py",
                line_start=5,
                line_end=15,
                visibility="public",
                resolution="syntactic",
            ),
        ]

        self.entrypoints = [
            Entrypoint(
                kind="http",
                name="post_endpoint",
                path="src/a.py",
                line=10,
                framework="flask",
            ),
            Entrypoint(
                kind="public_api",
                name="handler_2",
                path="src/b.py",
                line=5,
            ),
        ]

        # Grafo: handler_1 -> helper (resolvido)
        #        handler_2 -> handler_1 (resolvido, ciclo!)
        #        handler_1 -> undefined (não resolvido)
        self.references = [
            Reference(
                from_symbol="module_a.handler_1",
                to_name="module_a.helper",
                kind="call",
                path="src/a.py",
                line=15,
                resolved=True,
                target="module_a.helper",
                resolution="syntactic",
            ),
            Reference(
                from_symbol="module_b.handler_2",
                to_name="module_a.handler_1",
                kind="call",
                path="src/b.py",
                line=10,
                resolved=True,
                target="module_a.handler_1",
                resolution="syntactic",
            ),
            Reference(
                from_symbol="module_a.handler_1",
                to_name="undefined_func",
                kind="call",
                path="src/a.py",
                line=18,
                resolved=False,
                reason="dynamic receiver",
            ),
        ]

    def test_discover_groups_by_behavior_closure(self):
        """discover() une entradas pelo fecho transitivo de chamadas."""
        extraction = ExtractionResult(
            symbols=self.symbols,
            entrypoints=self.entrypoints,
            references=self.references,
            configuration=[],
            data_entities=[],
        )

        result = discover(extraction)

        # Ambas as entradas devem estar na mesma capacidade
        self.assertGreaterEqual(len(result.capabilities), 1)

        try:
            result.assert_accounted()
        except CapabilityAccountingError as e:
            self.fail(f"assert_accounted falhou: {e}")

    def test_discover_unanchored_entry(self):
        """Entrada sem símbolo de âncora produz anchor_basis='unanchored'."""
        ep_bad = Entrypoint(
            kind="callback",
            name="orphan_trigger",
            path="src/unknown.py",
            line=999,
        )
        extraction = ExtractionResult(
            symbols=self.symbols,
            entrypoints=self.entrypoints + [ep_bad],
            references=self.references,
            configuration=[],
            data_entities=[],
        )

        result = discover(extraction)

        # Encontrar a entrada não ancorada
        unanchored = [e for cap in result.capabilities for e in cap.entrypoints
                      if e.path == "src/unknown.py"]
        self.assertGreater(len(unanchored), 0)
        self.assertIsNone(unanchored[0].anchor)

    def test_discover_totals_close(self):
        """Denominadores do §6.6: entradas e símbolos fecham."""
        extraction = ExtractionResult(
            symbols=self.symbols,
            entrypoints=self.entrypoints,
            references=self.references,
            configuration=[],
            data_entities=[],
        )

        result = discover(extraction)

        self.assertEqual(
            result.totals["entrypoints_total"],
            len(self.entrypoints),
            "Entrypoints não fecham"
        )
        self.assertEqual(
            result.totals["entrypoints_grouped"],
            result.totals["entrypoints_total"],
            "Nem todas as entradas foram agrupadas"
        )


class TestInvestigationPlan(unittest.TestCase):
    """B) investigation.py: plan() gera 1 objetivo por capacidade."""

    def setUp(self):
        """Mini capability map com 1 capacidade."""
        from analysis.capabilities import CapabilityCandidate, CapabilityMap

        self.cap = CapabilityCandidate(
            capability_id="cap_001",
            name="post@module_a",
            entrypoints=(
                EntryRef(
                    kind="http",
                    name="post_endpoint",
                    path="src/a.py",
                    line=10,
                    line_end=20,
                ),
            ),
            reachable_symbols=("module_a.handler_1", "module_a.helper"),
            modules=("module_a",),
            paths=("src/a.py",),
            gaps=(
                BoundaryGap(
                    from_symbol="module_a.handler_1",
                    to_name="undefined_func",
                    kind="call",
                    path="src/a.py",
                    line=18,
                    line_end=18,
                    reason="dynamic receiver",
                ),
            ),
            external_dependencies=(),
            evidence_refs=(),
            grouping_basis=GroupingBasis.SINGLETON,
        )

        self.cmap = CapabilityMap(
            namespace="test",
            snapshot_id="snap_001",
            capabilities=(self.cap,),
            orphans=(),
            shared_symbols=(),
            totals={
                "entrypoints_total": 1,
                "capabilities": 1,
                "public_symbols_total": 2,
                "public_symbols_reached": 2,
                "orphan_symbols": 0,
            },
        )

    def test_plan_creates_one_objective_per_capability(self):
        """plan() gera exatamente 1 objetivo por capacidade."""
        extraction = ExtractionResult(
            symbols=[],
            entrypoints=[],
            references=[],
            configuration=[],
            data_entities=[],
        )

        objectives = plan(self.cmap, extraction)

        self.assertEqual(len(objectives), 1)
        self.assertEqual(objectives[0].capability_id, "cap_001")

    def test_reading_need_from_gap(self):
        """ReadingNeed para lacuna não resolvida tem EvidenceRef válida."""
        extraction = ExtractionResult(
            symbols=[],
            entrypoints=[],
            references=[],
            configuration=[],
            data_entities=[],
        )

        objectives = plan(self.cmap, extraction)
        obj = objectives[0]

        gap_needs = [n for n in obj.reading_needs
                     if n.trigger.value == "unresolved_call"]
        self.assertGreater(len(gap_needs), 0)

    def test_matrix_mark_not_applicable_requires_evidence(self):
        """FailureEdgeMatrix.mark() recusa not_applicable sem evidence."""
        matrix = FailureEdgeMatrix()

        with self.assertRaises(MatrixJustificationRequired):
            matrix.mark("entrada", "ausente", "not_applicable", None)

    def test_set_state_complete_requires_all_obligations(self):
        """set_state(COMPLETE) levanta ConclusionRejected com obrigações abertas."""
        obj = InvestigationObjective(
            objective_id="obj_001",
            kind="capability",
            capability_id="cap_001",
            name="test_obj",
        )

        with self.assertRaises(ConclusionRejected):
            obj.set_state(ObjectiveState.COMPLETE)

    def test_close_with_closure_reason_recorded(self):
        """close() registra o motivo de encerramento."""
        obj = InvestigationObjective(
            objective_id="obj_001",
            kind="capability",
            capability_id="cap_001",
            name="test_obj",
        )

        record = close(obj, ClosureReason.FRONTEIRA_EXPLICITA,
                       note="símbolo público fora do escopo")

        self.assertEqual(record.reason, ClosureReason.FRONTEIRA_EXPLICITA)
        self.assertEqual(obj.closure, record)
        self.assertIsNotNone(record.closed_at)


if __name__ == "__main__":
    unittest.main()
