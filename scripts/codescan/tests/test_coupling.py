"""Zonas de design: extração de import, resolução, Ce/Ca/I, zonas."""

import os
import re
import tempfile
import unittest

from codescan import coupling as cp
from codescan import coupling_generic
from codescan.surface import scan


def _w(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


class TestExtract(unittest.TestCase):
    def test_linguagens(self):
        t = cp._extract_targets(
            "import br.com.acme.orders.Order;\n"
            "from app.users import User\n"
            "using Acme.Payments;\n"
            "import x from './utils'\n"
            'const y = require("../db/conn")\n'
        )
        self.assertIn("br.com.acme.orders.Order", t)
        self.assertIn("app.users", t)
        self.assertIn("Acme.Payments", t)
        self.assertIn("./utils", t)
        self.assertIn("../db/conn", t)


class TestZone(unittest.TestCase):
    def test_dor(self):
        self.assertEqual(cp._zone(0.0, 0.0), "dor")

    def test_inutilidade(self):
        self.assertEqual(cp._zone(1.0, 1.0), "inutilidade")

    def test_saudavel_na_sequencia(self):
        self.assertEqual(cp._zone(1.0, 0.0), "saudável")   # A+I=1
        self.assertEqual(cp._zone(0.0, 1.0), "saudável")

    def test_sem_abstracao(self):
        self.assertIn("indeterminada", cp._zone(0.5, None))


class TestAnalyzeSintetico(unittest.TestCase):
    def test_grafo_e_instabilidade(self):
        """Fixtures são `.java`, então `cp.analyze` (dispatcher) roteia
        legitimamente para o motor Java, que agrega por PACOTE Java (lido do
        `package` do arquivo, aqui ausente/"(default)") — não por diretório
        do surface como o motor genérico. Este teste quer validar
        especificamente o motor genérico por diretório, então chama
        `coupling_generic.analyze` direto, contornando o dispatcher."""
        with tempfile.TemporaryDirectory() as repo:
            # domain: ninguém importa de fora; concreto → estável + dor
            _w(repo, "src/main/java/app/domain/Order.java", "class Order {}\n")
            # api depende de domain e service
            _w(repo, "src/main/java/app/api/OrderApi.java",
               "import app.domain.Order;\nimport app.service.OrderService;\nclass OrderApi {}\n")
            # service depende de domain
            _w(repo, "src/main/java/app/service/OrderService.java",
               "import app.domain.Order;\ninterface OrderService {}\n")
            s = scan(repo, module_min_files=1)
            surface = {"modules": [{"path": m.path, "role": m.role} for m in s.modules]}
            res = coupling_generic.analyze(repo, surface)
            by = {os.path.basename(r["module"]): r for r in res["modules"]}

            # domain: Ca alto, Ce=0 → I=0 → dor (concreto)
            self.assertEqual(by["domain"]["ce"], 0)
            self.assertGreaterEqual(by["domain"]["ca"], 2)
            self.assertEqual(by["domain"]["instability"], 0.0)
            self.assertEqual(by["domain"]["zone"], "dor")

            # api: só depende, ninguém depende dele → I=1
            self.assertEqual(by["api"]["ca"], 0)
            self.assertGreaterEqual(by["api"]["ce"], 2)
            self.assertEqual(by["api"]["instability"], 1.0)

            # service é interface → A alto
            self.assertGreater(by["service"]["abstractness"], 0.5)

    def test_render_frontmatter_e_mermaid(self):
        """Fixtures são `.java` → `cp.analyze` roteia para o motor Java
        legitimamente (ver docstring de `test_grafo_e_instabilidade`). Este
        teste valida o Mermaid `flowchart` do RENDER GENÉRICO especificamente
        (o motor Java tem seu próprio teste de render em
        `test_coupling_java.py`), então usa `coupling_generic.analyze`/
        `coupling_generic.render` direto, contornando o dispatcher."""
        with tempfile.TemporaryDirectory() as repo:
            _w(repo, "src/main/java/app/domain/Order.java", "class Order {}\n")
            _w(repo, "src/main/java/app/api/Api.java",
               "import app.domain.Order;\nclass Api {}\n")
            s = scan(repo, module_min_files=1)
            surface = {"repo": repo, "modules": [{"path": m.path, "role": m.role} for m in s.modules], "git": {}}
            res = coupling_generic.analyze(repo, surface)
            md = coupling_generic.render(surface, res, "t", "2026-07-22T00:00:00Z")
            self.assertTrue(md.startswith("---\n"))
            self.assertIn("source_type: code-repo", md)
            self.assertIn("```mermaid\nflowchart", md)
            self.assertNotIn("quadrantChart", md)
            self.assertIn("## Grafo de dependências internas", md)
            self.assertRegex(md, r"M\d{3} --> M\d{3}")
            self.assertIn("Ce 🟢", md)

    def test_analyze_flags_unresolved_internal_imports(self):
        with tempfile.TemporaryDirectory() as repo:
            _w(repo, "src/main/java/app/domain/Order.java", "package app.domain;\nclass Order {}\n")
            _w(repo, "src/main/java/app/api/Api.java",
               "package app.api;\nimport app.missing.MissingGateway;\nclass Api {}\n")
            s = scan(repo, module_min_files=1)
            surface = {"modules": [{"path": m.path, "role": m.role} for m in s.modules]}
            res = cp.analyze(repo, surface)
            self.assertGreaterEqual(res["unresolved_internal_imports"], 1)

    def test_render_warns_when_internal_imports_do_not_become_edges(self):
        surface = {"repo": "repo", "modules": [], "git": {}}
        analysis = {
            "edges": [],
            "unresolved_internal_imports": 2,
            "modules": [
                {
                    "module": "src/main/java/app/api",
                    "ce": 0,
                    "ca": 0,
                    "instability": None,
                    "abstractness": 0.0,
                    "distance": None,
                    "zone": "isolado",
                },
                {
                    "module": "src/main/java/app/domain",
                    "ce": 0,
                    "ca": 0,
                    "instability": None,
                    "abstractness": 0.0,
                    "distance": None,
                    "zone": "isolado",
                },
            ],
        }
        md = cp.render(surface, analysis, None, "2026-07-22T00:00:00Z")
        self.assertIn("Imports internos detectados", md)
        self.assertIn("flowchart LR", md)


    def test_render_mermaid_flowchart_is_legible_and_does_not_emit_quadrant_points(self):
        surface = {"repo": "repo", "modules": [], "git": {}}
        analysis = {
            "edges": [],
            "modules": [
                {
                    "module": "src/app/api",
                    "ce": 1,
                    "ca": 0,
                    "instability": 1.0,
                    "abstractness": 0.2,
                    "distance": 0.2,
                    "zone": "saudavel",
                },
                {
                    "module": "libs/shared/api",
                    "ce": 0,
                    "ca": 1,
                    "instability": 0.0,
                    "abstractness": 0.8,
                    "distance": 0.2,
                    "zone": "saudavel",
                },
                {
                    "module": "packages/O'Reilly API",
                    "ce": 1,
                    "ca": 1,
                    "instability": 0.5,
                    "abstractness": 0.5,
                    "distance": 0.0,
                    "zone": "saudavel",
                },
                {
                    "module": "src/main/java/br/com/acme/infra/dynamodb_26",
                    "ce": 1,
                    "ca": 2,
                    "instability": 0.31,
                    "abstractness": 1.0,
                    "distance": 0.31,
                    "zone": "transicao",
                },
                {
                    "module": "weird:path/[queue] m_t_8e96612a",
                    "ce": 2,
                    "ca": 1,
                    "instability": 0.69,
                    "abstractness": 0.0,
                    "distance": 0.31,
                    "zone": "transicao",
                },
            ],
        }

        md = cp.render(surface, analysis, None, "2026-07-22T00:00:00Z")
        block = re.search(r"```mermaid\n(?P<body>.*?)\n```", md, re.S)
        self.assertIsNotNone(block)
        body_lines = block.group("body").splitlines()
        body = block.group("body")

        self.assertEqual(body_lines[0], "flowchart LR")
        self.assertNotIn("quadrantChart", body)
        self.assertNotRegex(body, r"P[0-9]{3}:\s*\[[0-9.]+,\s*[0-9.]+\]")
        self.assertNotRegex(body, r"\]\s+P[0-9]+:")
        node_lines = [line for line in body_lines if re.search(r'\[[^\]]+\]', line)]
        self.assertGreaterEqual(len(node_lines), 3)
        for line in body_lines:
            self.assertLessEqual(len(line), 140)


if __name__ == "__main__":
    unittest.main()
