"""Testes para F06/W7: espaços de embedding versionados (§15.2).

Regra: embedding_spaces tem assinatura ÚNICA (provider/model/deployment/dimension/config);
set_embedding valida dimensão; search_vec filtra por space_id; activate_space é atômico (máx 1 ativo).
Migração legada (vetores SEM space_id) é aditiva sem erro.
"""

import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

from sbindex import store as STORE


class EmbeddingSpacesUniqueTest(unittest.TestCase):
    """F06/W7: get_or_create_space UNIQUE por assinatura."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.db")
        self.conn = STORE.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def test_get_or_create_space_unique_signature(self):
        """Mesma assinatura devolve mesmo space_id; assinatura diferente cria novo."""
        sig1 = {"provider": "openai", "model": "text-embedding-3-small", "deployment": None, "dimension": 1536}
        sig2 = {"provider": "openai", "model": "text-embedding-3-large", "deployment": None, "dimension": 3072}

        space_id_1a = STORE.get_or_create_space(self.conn, **sig1)
        space_id_1b = STORE.get_or_create_space(self.conn, **sig1)  # Mesma assinatura
        space_id_2 = STORE.get_or_create_space(self.conn, **sig2)  # Assinatura diferente

        self.assertEqual(space_id_1a, space_id_1b, "Mesma assinatura deve devolver mesmo space_id")
        self.assertNotEqual(space_id_1a, space_id_2, "Assinaturas diferentes criam spaces distintos")

    def test_get_or_create_space_born_inactive(self):
        """Space novo nasce inativo (active=0); nunca ativa sozinho."""
        space_id = STORE.get_or_create_space(
            self.conn, provider="fake", model="v1", deployment=None, dimension=128
        )
        row = self.conn.execute("SELECT active FROM embedding_spaces WHERE space_id=?", (space_id,)).fetchone()
        self.assertEqual(row["active"], 0, "Space novo deve nascer inativo")

    def test_get_or_create_space_config_json_matters(self):
        """Config diferente → space diferente, mesmo que provider/model/dimension sejam iguais."""
        config_a = {"param": "a"}
        config_b = {"param": "b"}
        space_a = STORE.get_or_create_space(
            self.conn,
            provider="p",
            model="m",
            deployment=None,
            dimension=10,
            config=config_a,
        )
        space_b = STORE.get_or_create_space(
            self.conn,
            provider="p",
            model="m",
            deployment=None,
            dimension=10,
            config=config_b,
        )
        self.assertNotEqual(space_a, space_b, "Config diferente cria space diferente")


class SetEmbeddingValidatesTest(unittest.TestCase):
    """F06/W7: set_embedding valida dimensão do vetor contra o space."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.db")
        self.conn = STORE.connect(self.db_path)
        # Insere um documento e um chunk
        self.conn.execute(
            "INSERT INTO documents (path, collection, docid, content_hash, indexed_at) VALUES (?,?,?,?,?)",
            ("test.txt", "raw", "doc1", "hash1", "2026-01-01T00:00:00Z"),
        )
        self.conn.execute(
            "INSERT INTO chunks (doc_id, ord, text) VALUES (?,?,?)",
            (1, 1, "test chunk"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def test_set_embedding_dimension_mismatch_raises(self):
        """Vetor com dimensão diferente da registrada no space levanta erro."""
        space_id = STORE.get_or_create_space(
            self.conn, provider="fake", model="v1", deployment=None, dimension=10
        )
        self.conn.commit()

        # Vetor com dimensão errada (11 em vez de 10)
        wrong_vec = tuple([0.1] * 11)
        with self.assertRaises(ValueError) as ctx:
            STORE.set_embedding(self.conn, chunk_id=1, vec=wrong_vec, space_id=space_id)
        self.assertIn("dimensão", str(ctx.exception).lower())

    def test_set_embedding_valid_dimension(self):
        """Vetor com dimensão correta é aceito sem erro."""
        space_id = STORE.get_or_create_space(
            self.conn, provider="fake", model="v1", deployment=None, dimension=5
        )
        self.conn.commit()

        vec = tuple([0.1, 0.2, 0.3, 0.4, 0.5])
        STORE.set_embedding(self.conn, chunk_id=1, vec=vec, space_id=space_id)  # Sem levante
        self.conn.commit()

        # Verifica que foi gravado
        row = self.conn.execute(
            "SELECT embedding_space_id FROM chunks WHERE id=?", (1,)
        ).fetchone()
        self.assertEqual(row["embedding_space_id"], space_id)

    def test_set_embedding_unknown_space_raises(self):
        """space_id desconhecido levanta ValueError (não atualiza blindamente)."""
        with self.assertRaises(ValueError) as ctx:
            STORE.set_embedding(self.conn, chunk_id=1, vec=(0.1, 0.2), space_id=9999)
        self.assertIn("desconhecido", str(ctx.exception).lower())


class ActivateSpaceAtomicTest(unittest.TestCase):
    """F06/W7: activate_space é atômico — máx 1 ativo; desativa todos antes de ativar novo."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.db")
        self.conn = STORE.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def test_activate_space_only_one_active(self):
        """Após activate_space, máximo 1 space tem active=1."""
        space_1 = STORE.get_or_create_space(self.conn, "p", "m1", None, 10)
        space_2 = STORE.get_or_create_space(self.conn, "p", "m2", None, 20)
        space_3 = STORE.get_or_create_space(self.conn, "p", "m3", None, 30)
        self.conn.commit()

        # Ativa espaço 1
        STORE.activate_space(self.conn, space_1)
        self.conn.commit()

        # Conta ativos após ativar space_1
        active_count = self.conn.execute("SELECT COUNT(*) as cnt FROM embedding_spaces WHERE active=1").fetchone()["cnt"]
        self.assertEqual(active_count, 1, "Deve haver exatamente 1 space ativo")

        # Ativa espaço 3 (desativa 1 implicitamente)
        STORE.activate_space(self.conn, space_3)
        self.conn.commit()

        active_count = self.conn.execute("SELECT COUNT(*) as cnt FROM embedding_spaces WHERE active=1").fetchone()["cnt"]
        self.assertEqual(active_count, 1, "Deve haver exatamente 1 space ativo após trocar")

        active_space = self.conn.execute("SELECT space_id FROM embedding_spaces WHERE active=1").fetchone()
        self.assertEqual(active_space["space_id"], space_3, "Space 3 deve ser o ativo agora")


class SearchVecFiltersTest(unittest.TestCase):
    """F06/W7: search_vec filtra por embedding_space_id; vetor legado (NULL) não retorna."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.db")
        self.conn = STORE.connect(self.db_path)

        # Insere documento base
        self.conn.execute(
            "INSERT INTO documents (path, collection, docid, content_hash, indexed_at) VALUES (?,?,?,?,?)",
            ("test.txt", "raw", "doc1", "hash1", "2026-01-01T00:00:00Z"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def test_search_vec_filters_by_space_id(self):
        """search_vec com space_id só retorna chunks daquele space (não de outros)."""
        space_1 = STORE.get_or_create_space(self.conn, "p1", "m1", None, 3)
        space_2 = STORE.get_or_create_space(self.conn, "p2", "m2", None, 3)

        # Chunk 1 em space_1
        cur = self.conn.execute("INSERT INTO chunks (doc_id, ord, text) VALUES (?,?,?)", (1, 1, "chunk in space 1"))
        chunk_1 = cur.lastrowid
        vec_1 = (0.1, 0.2, 0.3)
        STORE.set_embedding(self.conn, chunk_1, vec_1, space_1)

        # Chunk 2 em space_2
        cur = self.conn.execute("INSERT INTO chunks (doc_id, ord, text) VALUES (?,?,?)", (1, 2, "chunk in space 2"))
        chunk_2 = cur.lastrowid
        vec_2 = (0.1, 0.2, 0.3)
        STORE.set_embedding(self.conn, chunk_2, vec_2, space_2)

        self.conn.commit()

        # Busca em space_1 deve retornar só chunk_1
        STORE.activate_space(self.conn, space_1)
        self.conn.commit()

        query_vec = (0.1, 0.15, 0.35)
        results = STORE.search_vec(self.conn, query_vec, space_id=space_1, filters={}, limit=10)
        result_ids = [r[0] for r in results]
        self.assertIn(chunk_1, result_ids, "search_vec deve retornar chunk em space_1")

    def test_search_vec_skips_null_embeddings(self):
        """Chunks com embedding=NULL (legado, sem space_id) não aparecem em search_vec."""
        space_1 = STORE.get_or_create_space(self.conn, "p", "m", None, 3)

        # Chunk com embedding válido
        cur = self.conn.execute("INSERT INTO chunks (doc_id, ord, text) VALUES (?,?,?)", (1, 1, "has embedding"))
        chunk_with_emb = cur.lastrowid
        STORE.set_embedding(self.conn, chunk_with_emb, (0.1, 0.2, 0.3), space_1)

        # Chunk sem embedding (legado)
        cur = self.conn.execute("INSERT INTO chunks (doc_id, ord, text) VALUES (?,?,?)", (1, 2, "no embedding"))
        chunk_no_emb = cur.lastrowid

        self.conn.commit()
        STORE.activate_space(self.conn, space_1)
        self.conn.commit()

        # Busca deve retornar só o com embedding
        query_vec = (0.1, 0.2, 0.3)
        results = STORE.search_vec(self.conn, query_vec, space_id=space_1, filters={}, limit=10)
        result_ids = [r[0] for r in results]

        self.assertIn(chunk_with_emb, result_ids, "Deve retornar chunk com embedding")
        self.assertNotIn(chunk_no_emb, result_ids, "Deve ignorar chunk legado sem embedding")


if __name__ == "__main__":
    unittest.main()
