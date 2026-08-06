"""Chunking de artefatos SDD: tabelas e mermaid não podem ser partidos."""

import unittest

from sbindex.chunker import chunk_raw


def _big_table(n=40):
    header = "| ID | Requisito | Prioridade | Evidência |\n|----|----|----|----|\n"
    rows = "\n".join(f"| RF-{i:02d} | req {i} | Must | `F{i}.java:{i}` |" for i in range(1, n))
    return header + rows


class TestTableAtomic(unittest.TestCase):
    def test_tabela_grande_nao_orfana_cabecalho(self):
        text = "# spec\n\n## Requisitos\n" + _big_table(40) + "\n"
        chunks = chunk_raw(text)
        # toda ocorrência de linha de dados deve estar no mesmo chunk do cabeçalho
        for c in chunks:
            if "| RF-" in c.text:
                self.assertIn("| ID | Requisito", c.text,
                              "linha de tabela sem cabeçalho no chunk")

    def test_mermaid_preservado(self):
        mer = "```mermaid\nerDiagram\n" + "\n".join(
            f"  E{i} ||--o{{ E{i+1} : r{i}" for i in range(1, 30)) + "\n```"
        text = "# spec\n\n## ERD\n\n" + mer + "\n"
        chunks = chunk_raw(text)
        holding = [c for c in chunks if "erDiagram" in c.text]
        self.assertEqual(len(holding), 1, "mermaid deve ficar num único chunk")
        self.assertIn("```", holding[0].text)

    def test_tabela_pequena_junta_com_texto(self):
        text = "# spec\n\ntexto antes\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\ntexto depois\n"
        chunks = chunk_raw(text)
        self.assertEqual(len(chunks), 1)
        self.assertIn("| A | B |", chunks[0].text)

    def test_pipe_em_prosa_nao_vira_tabela(self):
        # sem linha separadora → não é tabela; recursive normal
        text = "# spec\n\nuse `a | b` no shell. " + ("palavra " * 400)
        chunks = chunk_raw(text)
        self.assertGreaterEqual(len(chunks), 1)


if __name__ == "__main__":
    unittest.main()
