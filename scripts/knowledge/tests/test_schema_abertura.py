"""Achado 4 da 4ª auditoria — abrir `knowledge.db` não paga lock de escrita.

`apply_schema` abria `BEGIN IMMEDIATE` em TODA abertura, mesmo com o schema já
instalado: uma transação de ESCRITA para não escrever nada. Toda operação de
leitura (`wk status` inclusive) disputava o lock de escrita do banco.

Estes testes provam, por execução:

* schema ausente ⇒ a instalação ainda acontece sob `BEGIN IMMEDIATE` (a
  decisão continua serializada — o fast-path não afrouxou a corrida);
* schema presente ⇒ nenhum `BEGIN IMMEDIATE` na abertura;
* 8 threads abrindo o MESMO arquivo ao mesmo tempo continuam sem erro e todas
  enxergam a mesma versão instalada.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
import unittest

from knowledge import repository as R
from knowledge import schema as S


class _Trace:
    """Coletor de statements do driver (`Connection.set_trace_callback`)."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, statement: str) -> None:
        self.statements.append(str(statement))

    def has(self, needle: str) -> bool:
        return any(needle.upper() in s.upper() for s in self.statements)


class AberturaDeKnowledgeDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="schema-abertura-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "knowledge.db")

    def test_instalacao_ainda_serializa_com_begin_immediate(self):
        trace = _Trace()
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.set_trace_callback(trace)
        try:
            instalada = S.apply_schema(conn, "2026-01-01T00:00:00+00:00")
        finally:
            conn.close()
        self.assertEqual(instalada, S.SCHEMA_VERSION)
        self.assertTrue(trace.has("BEGIN IMMEDIATE"), trace.statements)

    def test_reabertura_nao_executa_begin_immediate(self):
        primeira = sqlite3.connect(self.path, isolation_level=None)
        try:
            S.apply_schema(primeira, "2026-01-01T00:00:00+00:00")
        finally:
            primeira.close()

        trace = _Trace()
        segunda = sqlite3.connect(self.path, isolation_level=None)
        segunda.set_trace_callback(trace)
        try:
            instalada = S.apply_schema(segunda, "2026-01-01T00:00:01+00:00")
        finally:
            segunda.close()

        self.assertEqual(instalada, S.SCHEMA_VERSION)
        self.assertFalse(trace.has("BEGIN IMMEDIATE"), trace.statements)

    def test_versao_divergente_continua_recusada_no_fast_path(self):
        """O fast-path não pode virar 'aceita qualquer versão' (§ migração W8)."""
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            S.apply_schema(conn, "2026-01-01T00:00:00+00:00")
            conn.execute(
                "INSERT INTO schema_version(version, applied_at, note) VALUES (?,?,?)",
                (S.SCHEMA_VERSION + 7, "2026-01-01T00:00:00+00:00", "futuro"),
            )
            with self.assertRaises(Exception) as caught:
                S.apply_schema(conn, "2026-01-01T00:00:02+00:00")
        finally:
            conn.close()
        self.assertIn("migração", str(caught.exception).lower())

    def test_oito_threads_abrindo_o_mesmo_db_nao_falham(self):
        erros: list[BaseException] = []
        versoes: list[int] = []
        barreira = threading.Barrier(8)

        def abrir() -> None:
            try:
                barreira.wait(timeout=10)
                conn = R.connect(self.path)
                try:
                    versoes.append(int(S.installed_version(conn) or -1))
                finally:
                    conn.close()
            except BaseException as exc:  # pragma: no cover - só falha se regredir
                erros.append(exc)

        threads = [threading.Thread(target=abrir) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(erros, [])
        self.assertEqual(versoes, [S.SCHEMA_VERSION] * 8)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
