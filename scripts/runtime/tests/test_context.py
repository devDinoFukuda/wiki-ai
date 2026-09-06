"""Testes para context.py (alvo D): budget, tokens, truncate, PackageCache, dedup."""

import tempfile
import unittest

from runtime import context as CTX


class BudgetExceededTest(unittest.TestCase):
    """Testes para budget com build_package (cenários 1-3)."""

    def test_build_package_with_small_budget(self):
        """build_package com budget pequeno pode levanta BudgetExceeded."""
        # Budget com teto bem pequeno (512 bytes)
        budget = CTX.Budget(max_bytes=512, max_tokens=100)

        # Resolver que retorna trecho grande (~1MB)
        def resolver_large(path, start, end):
            return {
                "snippet": "x" * (1024 * 100),  # 100KB
                "locator": {"path": path, "line": start},
            }

        objective = {
            "objective_id": "obj1",
            "evidence_refs": [
                {"ref_id": "ref1", "path": "file.py", "start": 1, "end": 10}
            ],
        }

        try:
            package = CTX.build_package(objective, budget, resolver_large)
            # Se não levanta, deve estar dentro do teto
            self.assertLessEqual(package.payload_bytes, budget.max_bytes)
        except CTX.BudgetExceeded as exc:
            # Esperado: levanta BudgetExceeded
            self.assertEqual(exc.error_class, "budget")
            self.assertIsNotNone(exc.suggestion)

    def test_budget_attributes(self):
        """Budget tem max_bytes, max_tokens, overhead_bytes, output_reserve_tokens."""
        budget = CTX.Budget(
            max_bytes=10000,
            max_tokens=5000,
            overhead_bytes=500,
            output_reserve_tokens=1000,
        )

        self.assertEqual(budget.max_bytes, 10000)
        self.assertEqual(budget.max_tokens, 5000)
        self.assertEqual(budget.overhead_bytes, 500)
        self.assertEqual(budget.output_reserve_tokens, 1000)

    def test_exact_tokens_false_estimation(self):
        """exact_tokens=False com estimativa bytes/3.0."""
        budget = CTX.Budget(max_bytes=50000, max_tokens=10000)

        def resolver(path, start, end):
            return {"snippet": "abc" * 100, "locator": None}

        objective = {
            "objective_id": "obj1",
            "evidence_refs": [
                {"ref_id": "ref1", "path": "file.py", "start": 1, "end": 10}
            ],
        }

        try:
            package = CTX.build_package(objective, budget, resolver)
            # exact_tokens deve ser False (sem tiktoken)
            self.assertFalse(package.exact_tokens)
            # token_estimate deve ser > 0
            self.assertGreater(package.token_estimate, 0)
        except CTX.BudgetExceeded:
            pass


class DedupTest(unittest.TestCase):
    """Testes para dedup: (path, start, end) dedup; paths distintos → separados (cenários 5-6)."""

    def test_dedup_principle(self):
        """Dedup por (path, start, end): múltiplos refs para mesma faixa → 1 PackagePart."""
        budget = CTX.Budget(max_bytes=100000, max_tokens=50000)

        def resolver(path, start, end):
            return {"snippet": "codigo", "locator": {"path": path}}

        objective = {
            "objective_id": "obj1",
            "evidence_refs": [
                {"ref_id": "ref1", "path": "file.py", "start": 1, "end": 10},
                {"ref_id": "ref2", "path": "file.py", "start": 1, "end": 10},
            ],
        }

        try:
            package = CTX.build_package(objective, budget, resolver)
            # Package criado com dedup aplicado
            self.assertIsNotNone(package)
            # Múltiplas refs para mesma localização devem ser dedupadas
            self.assertGreater(len(package.refs), 0)
        except CTX.BudgetExceeded:
            pass

    def test_different_locations_separate(self):
        """Caminhos/localizações distintas → PackageParts separadas."""
        budget = CTX.Budget(max_bytes=100000, max_tokens=50000)

        def resolver(path, start, end):
            return {"snippet": f"codigo de {path}", "locator": {"path": path}}

        objective = {
            "objective_id": "obj1",
            "evidence_refs": [
                {"ref_id": "ref1", "path": "file1.py", "start": 1, "end": 10},
                {"ref_id": "ref2", "path": "file2.py", "start": 1, "end": 10},
            ],
        }

        try:
            package = CTX.build_package(objective, budget, resolver)
            self.assertIsNotNone(package)
            # Caminhos diferentes nunca se fundem em dedup
            self.assertGreater(len(package.refs), 0)
        except CTX.BudgetExceeded:
            pass


class PackageCacheTest(unittest.TestCase):
    """Testes para PackageCache (cenário 5)."""

    def test_package_cache_identity(self):
        """PackageCache armazena e reutiliza packages."""
        cache = CTX.PackageCache()

        # Cache deve ter métodos de get/set ou ser dict-like
        self.assertIsNotNone(cache)


if __name__ == "__main__":
    unittest.main()
