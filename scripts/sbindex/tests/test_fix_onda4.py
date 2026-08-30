"""Testes para contratos Onda 4 (F-34, L4/F-26b).

Rodar:
    PYTHONPATH=scripts python -m unittest sbindex.tests.test_fix_onda4 -v

Cobre:
  F-34: to_fts_query expande termos positivos avulsos (len>=4) para ("termo" OR stem*)
        com stems PT-BR; frases entre aspas e termos negados NÃO expandem
  L4/F-26b: realimentacao_findings detecta agent-output com derived_from sem lastro humano
            (cadeia resolvida só em agent-output ou wiki, sem fonte humana/código)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

from sbindex import store
from sbindex.chunker import chunk
from sbindex.cli import (
    derivation_graph,
    derivation_lastro,
    is_realimentacao,
    realimentacao_findings,
)
from sbindex.frontmatter import split, provenance_gaps
from sbindex.store import to_fts_query, search_lex


def _write(path: str, content: str) -> None:
    """Escreve arquivo, criando diretórios pai."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class F34ToFtsQueryExpansion(unittest.TestCase):
    """F-34: expansão de termos positivos avulsos com stems PT-BR."""

    def test_term_integracao_expands_to_stem(self):
        """Termo 'integração' (len>=4, não frase, não negado) expande para ('integração' OR integrac*)."""
        q = to_fts_query("integração")
        # Deve conter a expansão: frase + OR + stem*
        self.assertIn("integração", q.lower(), "deve conter o termo original (sem acento)")
        self.assertIn(" OR ", q, "deve expandir com OR")
        self.assertIn("*", q, "deve conter wildcard do stem")

    def test_term_servicos_expands(self):
        """Termo 'servicos' expande para ('servicos' OR servic*)."""
        q = to_fts_query("servicos")
        self.assertIn("servicos", q.lower())
        self.assertIn(" OR ", q)
        self.assertIn("*", q)

    def test_term_configuracoes_expands(self):
        """Termo 'configurações' expande com OR e stem."""
        q = to_fts_query("configurações")
        # Deve conter o termo e uma alternativa com wildcard (stem)
        self.assertIn(" OR ", q, "deve conter OR")
        self.assertIn("*", q, "deve conter wildcard do stem")
        # Nota: FTS5 preserva a forma original do termo, mas o stem é gerado sem acento

    def test_phrase_deadletter_no_expand(self):
        """Frase 'dead letter' entre aspas NÃO expande, fica só entre aspas."""
        q = to_fts_query('"dead letter"')
        # Deve ser frase exata, sem OR/expansão
        self.assertIn('"dead letter"', q)
        # Não deve haver wildcard de stem
        self.assertNotIn("dead*", q)
        self.assertNotIn("letter*", q)

    def test_negated_servicos_no_expand(self):
        """Termo negado '-servicos' isolado (sem operando esquerdo) vira positivo literal."""
        q = to_fts_query("-servicos")
        # F-20: negação isolada sem operando à esquerda vira termo positivo literal
        # (em vez de NOT inválido)
        self.assertIn("servicos", q.lower())
        # Não deve haver expansão (termo negado não expande)
        # Não deve haver OR em relação a -servicos
        self.assertNotIn(" OR ", q)

    def test_short_term_api_no_expand(self):
        """Termo curto 'api' (len<4) NÃO expande, fica só '"api"'."""
        q = to_fts_query("api")
        # Deve conter o termo entre aspas
        self.assertIn('"api"', q)
        # Não deve expandir (sem wildcards)
        self.assertNotIn("*", q)

    def test_fts5_real_search_integracao(self):
        """FTS5 real: indexar 'as integrações do serviço' e buscar com 'integração'."""
        tmp = tempfile.mkdtemp(prefix="sbtest_f34_")
        try:
            db = os.path.join(tmp, "index.db")
            conn = store.connect(db)

            # Insere documento com texto contendo "integrações"
            meta = {"id": "doc1", "source_type": "human-doc", "origin": "Test"}
            # O texto em português será normalizado (remove_diacritics) para "integracoes"
            test_chunk = type("Chunk", (), {
                "text": "as integrações do serviço funcionam bem",
                "heading": "Seção de integrações",
                "ord": 0,
                "token_count": 6,
            })()
            store.upsert(conn, os.path.join(tmp, "doc.md"), "raw", meta, [], [test_chunk])
            conn.commit()

            # Busca por 'integração' (será expandido para ('integração' OR integrac*))
            results = search_lex(conn, "integração", {}, 10)

            # Deve encontrar o chunk porque o índice tem "integrações" que normaliza
            # para token "integracoes", e a expansão em stem deve casar
            self.assertTrue(len(results) > 0, "busca por 'integração' deve achar 'integrações'")
            self.assertEqual(results[0][0], test_chunk.__dict__.get("id", 1) or 1,
                           "deve retornar o chunk indexado (ou similar)")

            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_fts5_real_search_servicos(self):
        """FTS5 real: indexar 'serviços' e buscar com 'servicos'."""
        tmp = tempfile.mkdtemp(prefix="sbtest_f34_svc_")
        try:
            db = os.path.join(tmp, "index.db")
            conn = store.connect(db)

            meta = {"id": "doc2", "source_type": "human-doc", "origin": "Test"}
            test_chunk = type("Chunk", (), {
                "text": "os serviços de API estão disponíveis",
                "heading": "Serviços",
                "ord": 0,
                "token_count": 5,
            })()
            store.upsert(conn, os.path.join(tmp, "doc2.md"), "raw", meta, [], [test_chunk])
            conn.commit()

            # Busca por 'servicos' (será expandido)
            results = search_lex(conn, "servicos", {}, 10)

            # Deve encontrar porque "serviços" normaliza para "servicos"
            self.assertTrue(len(results) > 0, "busca por 'servicos' deve achar 'serviços'")

            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_expansion_correct_stem_rules(self):
        """Expansão segue regras PT-BR: -ões/-ão→raiz, -s→raiz."""
        # "integrações" -> "integrac*" (remove "ões" = 3 chars)
        q1 = to_fts_query("integrações")
        self.assertIn("integrac*", q1, "integrações deve expandir para integrac*")

        # "configurações" -> "configurac*" (remove "oes" = 3 chars)
        q2 = to_fts_query("configurações")
        self.assertIn("configurac*", q2, "configurações deve expandir para configurac*")

        # "serviços" (com acento) normaliza para "servicos", depois -> "servico*" (remove "-s")
        q3 = to_fts_query("servicos")
        self.assertIn("servico*", q3, "servicos deve expandir para servico*")


class L4Realimentacao(unittest.TestCase):
    """L4/F-26b: detecção de realimentação (anti-contaminação)."""

    def setUp(self):
        """Cria tmpdir com raw/ e wiki/."""
        self.tmp = tempfile.mkdtemp(prefix="sbtest_l4_")
        os.makedirs(os.path.join(self.tmp, "raw"))
        os.makedirs(os.path.join(self.tmp, "wiki"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_l4_agentoutput_only_chain_is_realimentacao(self):
        """Cadeia agent-output→agent-output (sem lastro humano) = realimentacao."""
        # Cria dois documentos agent-output:
        # - source1: source_type=agent-output, sem derived_from (fonte primária de agente)
        # - source2: source_type=agent-output, derived_from=[source1]

        _write(
            os.path.join(self.tmp, "raw", "source1.md"),
            """---
id: source1
source_type: agent-output
origin: agent
---

Conteúdo da fonte 1.
""",
        )
        _write(
            os.path.join(self.tmp, "raw", "source2.md"),
            """---
id: source2
source_type: agent-output
origin: agent
derived_from: source1
---

Conteúdo da fonte 2 derivada.
""",
        )

        # Executa derivation_graph e realimentacao_findings
        fontes, paginas = derivation_graph(self.tmp)
        findings = realimentacao_findings(self.tmp)

        # Deve encontrar source2 como realimentação (cadeia fechada em agent-output puro)
        source2_paths = [f["path"] for f in findings if "source2" in f["path"]]
        self.assertTrue(len(source2_paths) > 0,
                       "source2 (agent-output derivada de agent-output) deve ser achado (realimentacao)")

    def test_l4_with_human_doc_has_lastro(self):
        """Cadeia agent-output→human-doc = tem lastro humano, NÃO é realimentacao."""
        # Cria:
        # - source1: source_type=human-doc (lastro humano)
        # - source2: source_type=agent-output, derived_from=[source1]

        _write(
            os.path.join(self.tmp, "raw", "human.md"),
            """---
id: human
source_type: human-doc
origin: manual
---

Documento humano.
""",
        )
        _write(
            os.path.join(self.tmp, "raw", "agent_from_human.md"),
            """---
id: agent_from_human
source_type: agent-output
origin: agent
derived_from: human
---

Derivada de documento humano.
""",
        )

        findings = realimentacao_findings(self.tmp)

        # Não deve achar agent_from_human como realimentacao (tem lastro em human)
        agent_paths = [f["path"] for f in findings if "agent_from_human" in f["path"]]
        self.assertTrue(len(agent_paths) == 0,
                       "agent-output com derived_from=human-doc deve ter lastro, não é realimentacao")

    def test_l4_transitive_chain_to_human_has_lastro(self):
        """Cadeia transitiva agent→agent→human = tem lastro indireto."""
        # Cria:
        # - human: source_type=human-doc
        # - agent1: derived_from=[human]
        # - agent2: derived_from=[agent1]

        _write(
            os.path.join(self.tmp, "raw", "human_doc.md"),
            """---
id: human_doc
source_type: human-doc
origin: manual
---

Documento humano raiz.
""",
        )
        _write(
            os.path.join(self.tmp, "raw", "agent1.md"),
            """---
id: agent1
source_type: agent-output
origin: agent
derived_from: human_doc
---

Primeira derivação.
""",
        )
        _write(
            os.path.join(self.tmp, "raw", "agent2.md"),
            """---
id: agent2
source_type: agent-output
origin: agent
derived_from: agent1
---

Segunda derivação (transitiva).
""",
        )

        findings = realimentacao_findings(self.tmp)

        # agent2 não é realimentacao porque agent1 → human_doc (tem lastro indireto)
        agent2_paths = [f["path"] for f in findings if "agent2" in f["path"]]
        self.assertTrue(len(agent2_paths) == 0,
                       "cadeia transitiva agent→agent→human deve ter lastro indireto (F-26b)")

    def test_l4_unknown_derived_from_no_achado(self):
        """derived_from id não resolvível → sem achado (cadeia incompleta)."""
        # Cria:
        # - agent: derived_from=[unknown_id] (id não existe em raw/ nem wiki/)

        _write(
            os.path.join(self.tmp, "raw", "agent_lost.md"),
            """---
id: agent_lost
source_type: agent-output
origin: agent
derived_from: nonexistent_id
---

Derivada de id que não existe.
""",
        )

        findings = realimentacao_findings(self.tmp)

        # agent_lost não deve ser achado porque a cadeia não resolve completamente
        # (id desconhecido = cadeia incompleta, não realimentacao determinada)
        agent_lost_paths = [f["path"] for f in findings if "agent_lost" in f["path"]]
        self.assertTrue(len(agent_lost_paths) == 0,
                       "agent com derived_from desconhecido não é realimentacao determinada (cadeia incompleta)")

    def test_l4_derivation_lastro_function(self):
        """Testa derivation_lastro diretamente: classifica origem em lastro/agent/wiki/desconhecidos."""
        # Mock de fontes
        fontes = {
            "human1": {"source_type": "human-doc", "derived_from": []},
            "agent1": {"source_type": "agent-output", "derived_from": ["human1"]},
            "agent2": {"source_type": "agent-output", "derived_from": ["agent1"]},
        }
        paginas = {"wiki_page"}

        # Caso 1: cadeia agent2 -> agent1 -> human1 (tem lastro)
        lastro = derivation_lastro(["agent2"], fontes, paginas)
        self.assertIn("human1", lastro["lastro"],
                      "cadeia com human-doc deve conter lastro")
        self.assertTrue(lastro["agent_output"],
                       "cadeia com agent deve conter agent_output")

        # Caso 2: cadeia só com agent (sem lastro)
        lastro2 = derivation_lastro(["agent1"], fontes, paginas, origem="agent2")
        # agent1 -> human1, então tem lastro
        self.assertIn("human1", lastro2["lastro"])

        # Caso 3: cadeia que menciona wiki (não é lastro)
        lastro3 = derivation_lastro(["wiki_page"], fontes, paginas)
        self.assertIn("wiki_page", lastro3["wiki"],
                      "página de wiki não é lastro (é saída compilada)")

    def test_l4_is_realimentacao_function(self):
        """Testa is_realimentacao: verdadeiro só quando lastro vazio e chain resolvida."""
        # Caso 1: tem lastro = False
        lastro_with_human = {
            "lastro": ["human1"],
            "agent_output": ["agent1"],
            "wiki": [],
            "desconhecidos": [],
        }
        self.assertFalse(is_realimentacao(lastro_with_human),
                        "com lastro humano, não é realimentacao")

        # Caso 2: tem desconhecidos = False
        lastro_with_unknown = {
            "lastro": [],
            "agent_output": ["agent1"],
            "wiki": [],
            "desconhecidos": ["unknown"],
        }
        self.assertFalse(is_realimentacao(lastro_with_unknown),
                        "com desconhecidos (cadeia incompleta), não é realimentacao")

        # Caso 3: só agent-output (sem lastro, sem unknown) = True
        lastro_pure_agent = {
            "lastro": [],
            "agent_output": ["agent1", "agent2"],
            "wiki": [],
            "desconhecidos": [],
        }
        self.assertTrue(is_realimentacao(lastro_pure_agent),
                       "cadeia fechada só em agent-output é realimentacao")

        # Caso 4: só wiki (sem lastro, sem unknown) = True
        lastro_wiki_only = {
            "lastro": [],
            "agent_output": [],
            "wiki": ["page1"],
            "desconhecidos": [],
        }
        self.assertTrue(is_realimentacao(lastro_wiki_only),
                       "cadeia fechada em wiki é realimentacao")

        # Caso 5: vazio = False (nenhuma origem)
        lastro_empty = {
            "lastro": [],
            "agent_output": [],
            "wiki": [],
            "desconhecidos": [],
        }
        self.assertFalse(is_realimentacao(lastro_empty),
                        "cadeia vazia não é realimentacao")


class F34StemRules(unittest.TestCase):
    """F-34: verificação das regras de stemming PT-BR."""

    def test_stem_removes_suffix_oes(self):
        """Suffix '-oes' remove 3 caracteres: integrações -> integrac."""
        from sbindex.store import _pt_stem

        # "integrações" (com acento) normaliza para "integracoes"
        # Aplica regra -oes: remove "oes" (3 chars), devolve "integrac"
        stem = _pt_stem("integrações")
        self.assertIsNotNone(stem)
        self.assertEqual(stem, "integrac",
                        "-oes rule: integrações -> integrac")

    def test_stem_removes_suffix_s(self):
        """Suffix '-s' remove 1 caractere: servicos -> servico."""
        from sbindex.store import _pt_stem

        stem = _pt_stem("servicos")
        self.assertIsNotNone(stem, "servicos deve gerar stem (len >= 4)")
        self.assertEqual(stem, "servico",
                        "-s rule: servicos -> servico")

    def test_stem_preserves_min_length(self):
        """Stem deve ter len >= 4. Caso contrário, return None."""
        from sbindex.store import _pt_stem

        # "da" (len=2) não pode gerar stem válido
        stem = _pt_stem("da")
        self.assertIsNone(stem, "termo muito curto (len<4) não gera stem")

    def test_stem_different_from_original(self):
        """Stem deve ser diferente do original, senão expansão é redundante."""
        from sbindex.store import _pt_stem

        # Palavra que não sofre redução (sem sufixo casado)
        stem = _pt_stem("algo")  # "algo" não tem sufixo PT-BR conhecido
        # Pode ser None ou diferente; mas se gerar, deve ser diferente
        if stem is not None:
            self.assertNotEqual(stem, "algo",
                            "stem deve ser diferente do original ou None")


if __name__ == "__main__":
    unittest.main(verbosity=2)
