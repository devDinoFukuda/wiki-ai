"""Testes de integração do fluxo do wiki-ai.

Rodar:
    PYTHONPATH=scripts python -m unittest sbindex.tests.test_flows -v

Cobre regressões dos bugs que esta skill já teve:
  #1 reindex ignorava mudança só no frontmatter (promote)
  #2 frontmatter não era lido com BOM ou comentário antes do ---
  #3 OR no lex: era tratado como termo literal
  #4 SKILL.md citava templates ausentes
  #6 _parse_minimal não descascava comentários inline (fallback sem PyYAML)
Não depende de yaml, tiktoken, numpy nem rede — só stdlib + sbindex.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

from sbindex import store
from sbindex.chunker import chunk
from sbindex.frontmatter import split, provenance_gaps, _parse_minimal, _strip_inline_comment
from sbindex.store import to_fts_query


def _write(path: str, content: str, bom: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = content.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    with open(path, "wb") as f:
        f.write(data)


FM = """\
---
id: sb-{n}
source_type: human-transcript
origin: "Maria Silva — sessao 2026-07-{n}"
captured_at: 2026-07-10T14:30:00Z
promoted: false
confidence: raw
---

# Retry de pagamentos numero {n}

O sistema retenta com dead-letter queue e DLQ e backoff.
"""


class FrontmatterParsing(unittest.TestCase):
    """Bug #2: frontmatter deve ser lido com BOM e com preambulo."""

    def test_limpo(self):
        meta, body = split(FM.format(n=1))
        self.assertEqual(meta["source_type"], "human-transcript")
        self.assertEqual(meta["origin"], "Maria Silva — sessao 2026-07-1")
        self.assertEqual(provenance_gaps(meta), [])

    def test_bom(self):
        meta, body = split("\ufeff" + FM.format(n=2))
        self.assertEqual(meta["source_type"], "human-transcript")
        self.assertEqual(meta["origin"], "Maria Silva — sessao 2026-07-2")
        self.assertEqual(provenance_gaps(meta), [])

    def test_comentario_antes_do_frontmatter(self):
        text = "# instrucao\n# outra linha\n" + FM.format(n=3)
        meta, body = split(text)
        self.assertEqual(meta["source_type"], "human-transcript")
        self.assertEqual(provenance_gaps(meta), [])

    def test_sem_frontmatter_nao_finge_que_tem(self):
        # Doc markdown comum sem frontmatter: deve retornar meta vazio, corpo intacto.
        text = "# Titulo\n\nparagrafo\n"
        meta, body = split(text)
        self.assertEqual(meta, {})
        self.assertIn("paragrafo", body)

    def test_separador_horizontal_no_meio_nao_e_frontmatter(self):
        # `---` como separador horizontal depois de conteudo NAO e frontmatter.
        text = "# Titulo\n\nparagrafo\n\n---\n\nmais texto\n"
        meta, body = split(text)
        self.assertEqual(meta, {})


class ReindexProvenance(unittest.TestCase):
    """Bug #1: mudanca so no frontmatter deve ser detectada pelo reindex."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sbtest_")
        os.makedirs(os.path.join(self.tmp, "raw"))
        os.makedirs(os.path.join(self.tmp, "wiki"))
        self.db = os.path.join(self.tmp, "index.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reindex_one(self, path: str) -> None:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
        meta, body = split(text)
        gaps = provenance_gaps(meta)
        chunks = chunk(body, "raw")
        conn = store.connect(self.db)
        store.upsert(conn, path, "raw", meta, gaps, chunks)
        conn.commit()
        conn.close()

    def _get_row(self, path: str) -> dict:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT promoted, confidence, source_id FROM documents WHERE path=?",
            (path,),
        ).fetchone()
        conn.close()
        return dict(r) if r else {}

    def test_promote_atualiza_banco(self):
        path = os.path.join(self.tmp, "raw", "src.md")
        _write(path, FM.format(n=10))
        self._reindex_one(path)
        self.assertEqual(self._get_row(path)["promoted"], 0)
        self.assertEqual(self._get_row(path)["confidence"], "raw")

        # Simula promote: muda SO o frontmatter, corpo intocado.
        _write(path, FM.format(n=10).replace(
            "promoted: false", "promoted: true"
        ).replace("confidence: raw", "confidence: reviewed"))
        self._reindex_one(path)

        row = self._get_row(path)
        self.assertEqual(row["promoted"], 1, "promote nao propagou para o banco")
        self.assertEqual(row["confidence"], "reviewed",
                         "confidence nao propagou para o banco")


class LexicalOr(unittest.TestCase):
    """Bug #3: OR no lex: deve virar operador FTS5, nao termo literal."""

    def test_or_preservado(self):
        q = to_fts_query('"dead-letter" OR "DLQ"')
        self.assertIn(' OR ', q)
        self.assertNotIn('"OR"', q)

    def test_or_sem_aspas_tambem_funciona(self):
        q = to_fts_query("dead-letter OR DLQ")
        self.assertIn(' OR ', q)
        self.assertNotIn('"OR"', q)

    def test_and_preservado(self):
        q = to_fts_query("retry AND dead-letter")
        self.assertIn(' AND ', q)

    def test_termo_simples_continua_funcionando(self):
        q = to_fts_query("dead-letter")
        self.assertEqual(q, '"dead-letter"')

    def test_negacao_continua_funcionando(self):
        q = to_fts_query("-DLQ")
        self.assertEqual(q, 'NOT "DLQ"')

    def test_case_insensitive(self):
        q = to_fts_query("dead-letter or DLQ")
        self.assertIn(' OR ', q)


class EndToEndAudit(unittest.TestCase):
    """Fluxo completo: criar fonte com BOM -> indexar -> audit deve estar limpo.

    Isto e o teste que teria pego os bugs #1 e #2 automaticamente se existisse.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sbtest_e2e_")
        os.makedirs(os.path.join(self.tmp, "raw"))
        self.db = os.path.join(self.tmp, "index.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fonte_com_bom_passa_no_audit(self):
        path = os.path.join(self.tmp, "raw", "bom.md")
        _write(path, FM.format(n=99), bom=True)
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
        meta, body = split(text)
        gaps = provenance_gaps(meta)
        self.assertEqual(gaps, [], "BOM fez frontmatter virar corpo (bug #2)")
        chunks = chunk(body, "raw")
        conn = store.connect(self.db)
        store.upsert(conn, path, "raw", meta, gaps, chunks)
        conn.commit()

        # L2: nenhuma fonte deve ter gaps.
        rows = conn.execute(
            "SELECT docid FROM documents WHERE collection='raw' AND gaps IS NOT NULL"
        ).fetchall()
        conn.close()
        self.assertEqual(rows, [], "fonte com BOM reportada como sem proveniencia")


class MinimalParserInlineComments(unittest.TestCase):
    """Bug #6: _parse_minimal deve descascar comentarios inline (fallback sem PyYAML).

    Sem isto, o template distribuído (comentarios ao lado de cada campo) produz
    valores como 'human-transcript | human-doc | ...' em vez de 'human-transcript',
    e o audit da falso positivo de L2 (source_type invalido).
    """

    def test_strip_comentario_simples(self):
        self.assertEqual(_strip_inline_comment("human-transcript # comment"), "human-transcript")

    def test_strip_comentario_no_inicio(self):
        self.assertEqual(_strip_inline_comment("# só comentario"), "")

    def test_preserva_hash_sem_espaco(self):
        # YAML: # sem espaco antes NAO é comentario.
        self.assertEqual(_strip_inline_comment("url#fragment"), "url#fragment")

    def test_preserva_hash_dentro_de_aspas_duplas(self):
        self.assertEqual(
            _strip_inline_comment('"valor # literal" # comentario'),
            '"valor # literal"',
        )

    def test_preserva_hash_dentro_de_aspas_simples(self):
        self.assertEqual(
            _strip_inline_comment("'valor # literal' # comentario"),
            "'valor # literal'",
        )

    def test_sem_comentario(self):
        self.assertEqual(_strip_inline_comment("human-transcript"), "human-transcript")

    def test_parse_minimal_com_template_distribuido(self):
        """O template real, preenchido, deve parsear limpo sem PyYAML."""
        block = (
            "# cabecalho instrutivo\n"
            "id: sb-test                  # identificador\n"
            "source_type: human-transcript           # human-transcript | human-doc\n"
            'origin: "Maria Silva"           # fonte concreta\n'
            "captured_at: 2026-07-10T14:30:00Z    # ISO 8601\n"
            "promoted: false                  # true só depois do promote\n"
            "promoted_by:                     # humano ou regra\n"
            "confidence: raw                  # raw | reviewed | canonical\n"
            "supersedes:                      # id da fonte substituida\n"
            "source_link:                     # link para original\n"
        )
        parsed = _parse_minimal(block)
        self.assertEqual(parsed["id"], "sb-test")
        self.assertEqual(parsed["source_type"], "human-transcript")
        self.assertEqual(parsed["origin"], '"Maria Silva"')
        self.assertEqual(parsed["captured_at"], "2026-07-10T14:30:00Z")
        self.assertEqual(parsed["promoted"], "false")
        self.assertEqual(parsed["promoted_by"], "")
        self.assertEqual(parsed["confidence"], "raw")
        self.assertEqual(parsed["supersedes"], "")
        self.assertEqual(parsed["source_link"], "")

    def test_split_com_template_distribuido_sem_pyyaml(self):
        """Fluxo completo: split + provenance_gaps com template, sem PyYAML."""
        import sys
        yaml_was = sys.modules.get("yaml")
        sys.modules["yaml"] = None  # type: ignore
        try:
            doc = (
                "---\n"
                "id: sb-tmpl                  # identificador\n"
                "source_type: human-transcript           # tipo\n"
                'origin: "Maria Silva"           # fonte\n'
                "captured_at: 2026-07-10T14:30:00Z    # data\n"
                "promoted: false                  # bool\n"
                "confidence: raw                  # nivel\n"
                "---\n\n"
                "# Doc\n\nCorpo.\n"
            )
            meta, body = split(doc)
            self.assertEqual(meta["source_type"], "human-transcript")
            self.assertEqual(meta["origin"], "Maria Silva")
            self.assertEqual(meta["promoted"], 0)
            self.assertEqual(meta["confidence"], "raw")
            self.assertEqual(provenance_gaps(meta), [])
        finally:
            if yaml_was is not None:
                sys.modules["yaml"] = yaml_was
            else:
                sys.modules.pop("yaml", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
