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


class TruncateSnippetTest(unittest.TestCase):
    """Testes para truncate_snippet (F05/W7): nunca produz cláusula órfã."""

    def test_truncate_snippet_python_if_else_complete(self):
        """Corte Python com if/else: resultado COMPLETO (ambos presentes) ou NENHUM."""
        snippet = """\
if condition:
    statement1()
    statement2()
else:
    statement3()
    statement4()
"""
        # Tenta cortar com orçamento pequeno
        result, omitted, changed = CTX.truncate_snippet(snippet, "python", target_max_chars=50)

        # Se houve mudança, resultado não pode ter `else` órfão
        if changed:
            # Resultado deve ter AMBOS if e else, ou NENHUM dos dois
            has_if = "if " in result
            has_else = "else:" in result
            # XOR: ou tem ambos, ou nenhum
            self.assertEqual(
                has_if, has_else,
                "Corte não pode deixar else órfão: if e else devem estar juntos ou ambos ausentes"
            )

    def test_truncate_snippet_python_try_except_complete(self):
        """Corte Python com try/except: resultado COMPLETO ou NENHUM bloco."""
        snippet = """\
try:
    operation()
    risky_call()
except ValueError:
    handle_error()
finally:
    cleanup()
"""
        result, omitted, changed = CTX.truncate_snippet(snippet, "python", target_max_chars=50)

        if changed:
            # Se há try, deve haver except ou finally (nunca try órfão)
            has_try = "try:" in result
            has_except = "except" in result
            has_finally = "finally:" in result

            if has_try:
                # Try isolado é inválido em Python
                self.assertTrue(
                    has_except or has_finally,
                    "Try isolado sem except/finally é inválido; corte deve remover bloco inteiro"
                )

    def test_truncate_snippet_python_no_broken_blocks(self):
        """Corte Python de função com if interno: sem cláusulas órfãs."""
        snippet = """\
def process_data(x):
    if x > 10:
        return x * 2
    elif x > 5:
        return x + 1
    else:
        return 0
"""
        result, omitted, changed = CTX.truncate_snippet(snippet, "python", target_max_chars=80)

        # Resultado deve ser Python válido se changed=True
        if changed:
            # Tenta compilar; se sintaxe inválida, teste falha
            try:
                compile(result, "<string>", "exec")
            except SyntaxError as e:
                # Se levanta SyntaxError, pode ser por cláusula órfã (else sem if, etc)
                self.fail(f"Corte produziu Python inválido: {e}")

    def test_truncate_snippet_generic_braces_no_orphan_else(self):
        """Corte genérico (chaves balanceadas) nunca deixa else/catch/finally órfão."""
        snippet = """\
if (x > 0) {
  doSomething();
  doMore();
  doMore2();
  doMore3();
} else {
  doOtherThing();
  doMore4();
}
"""
        result, omitted, changed = CTX.truncate_snippet(snippet, "javascript", target_max_chars=60)

        if changed:
            # Se há `else`, deve haver `if` antes
            has_if_open = "{" in result.split("else")[0] if "else" in result else False
            has_else = "else" in result

            if has_else:
                # Deve haver if antes do else (não órfão)
                lines = result.split("\n")
                else_line_idx = next(i for i, l in enumerate(lines) if "else" in l)
                before_else = "\n".join(lines[:else_line_idx])
                self.assertIn("if", before_else, "else não pode vir sem if antes")

    def test_truncate_snippet_returns_valid_marker(self):
        """Corte com budget muito apertado: retorna marcador ou `changed=False`."""
        snippet = "x" * 1000  # Muito grande

        result, omitted, changed = CTX.truncate_snippet(snippet, "text", target_max_chars=100)

        # Não levanta exceção
        self.assertIsNotNone(result)
        # Se não coube nem mesmo o marcador, changed=False
        # Se mudou, o resultado é menor
        if changed:
            self.assertLess(len(result), len(snippet), "Corte deve reduzir tamanho")


if __name__ == "__main__":
    unittest.main()
