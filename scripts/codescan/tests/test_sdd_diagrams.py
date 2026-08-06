"""Gates Mermaid do contrato SDD."""

import unittest

from codescan import sdd


class TestMermaidErrors(unittest.TestCase):
    def test_rejects_unquoted_at_label(self):
        text = "```mermaid\nflowchart TD\n  A[@Scheduled publish]\n  A --> B\n  B[Done]\n```"
        self.assertTrue(any("label" in err for err in sdd._mermaid_errors(text)))

    def test_rejects_flowchart_without_edge(self):
        text = "```mermaid\nflowchart LR\n  A[Inicio]\n  B[Fim]\n```"
        self.assertTrue(any("sem nos/arestas" in err for err in sdd._mermaid_errors(text)))

    def test_rejects_flowchart_without_node(self):
        text = "```mermaid\nflowchart LR\n  A --> B\n```"
        self.assertTrue(any("sem nos/arestas" in err for err in sdd._mermaid_errors(text)))

    def test_rejects_long_line(self):
        label = "x" * 141
        text = f"```mermaid\nflowchart LR\n  A[\"{label}\"]\n  B[\"Fim\"]\n  A --> B\n```"
        self.assertTrue(any("linha longa" in err for err in sdd._mermaid_errors(text)))

    def test_rejects_duplicate_node_id(self):
        text = "```mermaid\nflowchart LR\n  A[\"Inicio\"]\n  A[\"Fim\"]\n  A --> B\n```"
        self.assertTrue(any("duplicado" in err for err in sdd._mermaid_errors(text)))

    def test_rejects_unbalanced_brackets(self):
        text = "```mermaid\nflowchart LR\n  A[\"Inicio\"\n  A --> B[\"Fim\"]\n```"
        self.assertTrue(any("desbalanceados" in err for err in sdd._mermaid_errors(text)))

    def test_rejects_unbalanced_subgraph(self):
        text = "```mermaid\nflowchart LR\n  subgraph G[\"Grupo\"]\n  A[\"Inicio\"] --> B[\"Fim\"]\n```"
        self.assertTrue(any("subgraph/end" in err for err in sdd._mermaid_errors(text)))

    def test_rejects_end_without_subgraph(self):
        text = "```mermaid\nflowchart LR\n  A[\"Inicio\"] --> B[\"Fim\"]\n  end\n```"
        self.assertTrue(any("end sem subgraph" in err for err in sdd._mermaid_errors(text)))

    def test_rejects_edge_with_undefined_bare_nodes(self):
        text = "```mermaid\nflowchart LR\n  A --> B\n```"
        errors = sdd._mermaid_errors(text)
        self.assertTrue(any("sem definição" in err for err in errors))

    def test_accepts_edge_with_inline_quoted_labels(self):
        text = "```mermaid\nflowchart LR\n  A[\"Inicio\"] --> B[\"Fim\"]\n```"
        self.assertEqual([], sdd._mermaid_errors(text))

    def test_rejects_unquoted_plain_label(self):
        text = "```mermaid\nflowchart LR\n  A[Inicio] --> B[\"Fim\"]\n```"
        self.assertTrue(any("label" in err for err in sdd._mermaid_errors(text)))

    def test_rejects_fragile_character_outside_quoted_label(self):
        text = "```mermaid\nflowchart LR\n  A[\"Inicio\"] --> B[\"Fim\"]; C[\"Extra\"]\n```"
        self.assertTrue(any("caractere frágil" in err for err in sdd._mermaid_errors(text)))

    def test_rejects_non_whitelisted_edge_syntax(self):
        text = "```mermaid\nflowchart LR\n  A[\"Inicio\"] -- texto --> B[\"Fim\"]\n```"
        self.assertTrue(any("whitelist" in err for err in sdd._mermaid_errors(text)))


if __name__ == "__main__":
    unittest.main()
