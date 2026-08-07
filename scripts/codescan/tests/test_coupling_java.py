"""Dispatcher de acoplamento (`coupling.py`) e motor Java (`coupling_java*`).

Cobre o roteamento do dispatcher (Java vs. genérico, inclusive o fallback
quando todas as classes Java são excluídas), determinismo do motor Java, o
conteúdo mínimo do render Markdown/HTML e a detecção de ciclo entre pacotes.
Os testes do motor genérico em si (extração de import, zonas, render
Mermaid do fallback) continuam em `test_coupling.py` — este arquivo cobre só
o que é específico do roteamento e do motor Java.
"""

import os
import tempfile
import unittest

from codescan import coupling as cp
from codescan import sdd
from codescan.surface import scan


def _w(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def _surface_for(repo, module_min_files=1):
    s = scan(repo, module_min_files=module_min_files)
    return {
        "repo": repo,
        "modules": [{"path": m.path, "role": m.role} for m in s.modules],
        "languages": s.languages,
        "git": {},
    }


def _build_java_cycle_repo(repo):
    """Duas classes em pacotes distintos que se importam mutuamente —
    ciclo de classes E de pacotes ao mesmo tempo (import explícito FQCN,
    resolvido no passo 1 de `_resolve_dependencies`, sem depender da
    heurística de identificador)."""
    _w(repo, "src/main/java/com/acme/a/AService.java",
       "package com.acme.a;\n"
       "import com.acme.b.BService;\n"
       "public class AService {\n"
       "    void call(BService b) {\n"
       "        if (b != null) { b.toString(); }\n"
       "    }\n"
       "}\n")
    _w(repo, "src/main/java/com/acme/b/BService.java",
       "package com.acme.b;\n"
       "import com.acme.a.AService;\n"
       "public class BService {\n"
       "    void call(AService a) {\n"
       "        if (a != null) { a.toString(); }\n"
       "    }\n"
       "}\n")


def _build_python_only_repo(repo):
    _w(repo, "src/app/domain/order.py", "class Order:\n    pass\n")
    _w(repo, "src/app/api/handler.py",
       "from app.domain.order import Order\n\nclass Handler:\n    pass\n")


class TestDispatcherRouting(unittest.TestCase):
    def test_repo_java_routes_to_java_engine(self):
        with tempfile.TemporaryDirectory() as repo:
            _build_java_cycle_repo(repo)
            surface = _surface_for(repo)
            res = cp.analyze(repo, surface)
            self.assertEqual(res["engine"], "java")

    def test_repo_python_only_routes_to_generic_engine(self):
        with tempfile.TemporaryDirectory() as repo:
            _build_python_only_repo(repo)
            surface = _surface_for(repo)
            res = cp.analyze(repo, surface)
            self.assertEqual(res["engine"], "generic")

    def test_repo_java_all_classes_excluded_falls_back_to_generic(self):
        """Só `*Dto.java` e `*Config.java` — o motor Java exclui os dois por
        sufixo de nome (`exclude_file_suffixes`), zera `total_classes` e o
        dispatcher cai no motor genérico em vez de emitir relatório vazio."""
        with tempfile.TemporaryDirectory() as repo:
            _w(repo, "src/main/java/com/acme/dto/OrderDto.java",
               "package com.acme.dto;\npublic class OrderDto {}\n")
            _w(repo, "src/main/java/com/acme/config/AppConfig.java",
               "package com.acme.config;\npublic class AppConfig {}\n")
            surface = _surface_for(repo)
            res = cp.analyze(repo, surface)
            self.assertEqual(res["engine"], "generic")


class TestJavaEngineDeterminism(unittest.TestCase):
    def test_analyze_is_deterministic(self):
        with tempfile.TemporaryDirectory() as repo:
            _build_java_cycle_repo(repo)
            surface = _surface_for(repo)
            res1 = cp.analyze(repo, surface)
            res2 = cp.analyze(repo, surface)
            self.assertEqual(res1, res2)


class TestJavaEngineRender(unittest.TestCase):
    def test_render_has_required_sections_and_min_size(self):
        with tempfile.TemporaryDirectory() as repo:
            _build_java_cycle_repo(repo)
            surface = _surface_for(repo)
            analysis = cp.analyze(repo, surface)
            self.assertEqual(analysis["engine"], "java")
            md = cp.render(surface, analysis, "t", "2026-08-06T00:00:00Z")
            self.assertIn("## Métricas por módulo", md)
            self.assertIn("## Plano Abstração", md)
            self.assertGreater(len(md.encode("utf-8")), 500)

    def test_all_mermaid_blocks_pass_sdd_gate(self):
        with tempfile.TemporaryDirectory() as repo:
            _build_java_cycle_repo(repo)
            surface = _surface_for(repo)
            analysis = cp.analyze(repo, surface)
            md = cp.render(surface, analysis, None, "2026-08-06T00:00:00Z")
            self.assertIn("```mermaid", md)
            errors = sdd._mermaid_errors(md)
            self.assertEqual(errors, [], f"Mermaid inválido no render Java: {errors}")

    def test_package_cycle_is_detected_and_rendered(self):
        with tempfile.TemporaryDirectory() as repo:
            _build_java_cycle_repo(repo)
            surface = _surface_for(repo)
            analysis = cp.analyze(repo, surface)
            self.assertGreaterEqual(analysis["summary"]["cycles_packages"], 1)
            self.assertTrue(analysis["cycles_packages"])
            cyc = analysis["cycles_packages"][0]
            self.assertIn("com.acme.a", cyc)
            self.assertIn("com.acme.b", cyc)

            md = cp.render(surface, analysis, None, "2026-08-06T00:00:00Z")
            self.assertIn("## Dependências circulares", md)
            self.assertIn("### Pacotes", md)
            self.assertIn("com.acme.a", md)
            self.assertIn("com.acme.b", md)
            self.assertNotIn("Nenhuma dependência circular detectada entre pacotes.", md)


class TestRenderHtml(unittest.TestCase):
    def test_render_html_returns_string_for_java(self):
        with tempfile.TemporaryDirectory() as repo:
            _build_java_cycle_repo(repo)
            surface = _surface_for(repo)
            analysis = cp.analyze(repo, surface)
            self.assertEqual(analysis["engine"], "java")
            html_out = cp.render_html(surface, analysis, "2026-08-06T00:00:00Z")
            self.assertIsInstance(html_out, str)
            self.assertIn("<!DOCTYPE html>", html_out)

    def test_render_html_returns_none_for_generic(self):
        with tempfile.TemporaryDirectory() as repo:
            _build_python_only_repo(repo)
            surface = _surface_for(repo)
            analysis = cp.analyze(repo, surface)
            self.assertEqual(analysis["engine"], "generic")
            html_out = cp.render_html(surface, analysis, "2026-08-06T00:00:00Z")
            self.assertIsNone(html_out)


if __name__ == "__main__":
    unittest.main()
