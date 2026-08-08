"""Regressão: o aviso de `reindex` em modo léxico dizia "vec/hyde retornam
vazio", mas o `search` na verdade BARRA vec/hyde com erro e exit 2 antes de
montar `results`. Duas mensagens do mesmo CLI descrevendo o mesmo cenário de
formas contraditórias — a mensagem errada manda o operador pro caminho
errado ("refine a query" em vez de "configure embeddings ou use lex:").

Cobre:
  - sem AZURE_OPENAI_*: vec: e hyde: falham com exit 2, erro explícito, sem
    `results` no payload.
  - sem AZURE_OPENAI_*: lex: funciona normalmente (exit 0, `results`).
  - o aviso do reindex não afirma mais "retorna vazio" e mantém a orientação
    acionável (AZURE_OPENAI_* / --lex-only).
  - o aviso do reindex e o erro do search usam o mesmo vocabulário para o
    mesmo cenário (consistência entre as duas mensagens).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from sbindex.cli import main as sbindex_main

AZURE_VARS = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_EMBED_DEPLOY",
    "AZURE_OPENAI_API_VERSION",
)

RAW_DOC = """\
---
id: sb-fixlex-1
source_type: human-transcript
origin: "Fulano de Tal — sessao 2026-08-07"
captured_at: 2026-08-07T10:00:00Z
promoted: false
confidence: raw
---

# Retry de pagamentos

O sistema retenta operacoes com backoff exponencial e fila de dead-letter.
"""


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sbindex_main(argv)
    return code, out.getvalue(), err.getvalue()


class LexModeMessagesConsistent(unittest.TestCase):
    def setUp(self):
        self._saved_env = {k: os.environ.pop(k, None) for k in AZURE_VARS}
        self.tmp = tempfile.mkdtemp(prefix="sbtest_fixlex_")
        os.makedirs(os.path.join(self.tmp, "raw"))
        os.makedirs(os.path.join(self.tmp, "wiki"))
        with open(os.path.join(self.tmp, "raw", "doc.md"), "w", encoding="utf-8") as f:
            f.write(RAW_DOC)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reindex(self) -> dict:
        code, out, err = _run(["--store", self.tmp, "reindex"])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    # -- aviso do reindex --------------------------------------------------

    def test_reindex_warning_nao_afirma_retorno_vazio(self):
        report = self._reindex()
        self.assertIn("warning", report)
        warning = report["warning"]
        self.assertNotIn("retornam vazio", warning)
        self.assertNotIn("retorna vazio", warning)
        # orientação acionável precisa continuar presente
        self.assertIn("AZURE_OPENAI_*", warning)
        self.assertIn("--lex-only", warning)

    # -- comportamento real: vec/hyde falham, lex funciona -----------------

    def test_vec_falha_com_exit_2_e_sem_results(self):
        self._reindex()
        code, out, err = _run(
            ["--store", self.tmp, "search", "--format", "json", "vec: pagamentos"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        payload = json.loads(err)
        self.assertIn("error", payload)
        self.assertNotIn("results", payload)

    def test_hyde_falha_com_exit_2_e_sem_results(self):
        self._reindex()
        code, out, err = _run(
            [
                "--store",
                self.tmp,
                "search",
                "--format",
                "json",
                "hyde: pagamentos com retry e dead-letter",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        payload = json.loads(err)
        self.assertIn("error", payload)
        self.assertNotIn("results", payload)

    def test_lex_funciona_com_results(self):
        self._reindex()
        code, out, err = _run(
            ["--store", self.tmp, "search", "--format", "json", "lex: retry"]
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIn("results", payload)
        self.assertGreater(len(payload["results"]), 0)

    # -- consistência entre as duas mensagens -------------------------------

    def test_aviso_do_reindex_condiz_com_erro_do_search(self):
        report = self._reindex()
        warning = report["warning"]

        code, _, err = _run(
            ["--store", self.tmp, "search", "--format", "json", "vec: pagamentos"]
        )
        self.assertEqual(code, 2)
        search_error = json.loads(err)["error"]

        # mesmo cenário, mesmo vocabulário nos dois lados do CLI.
        self.assertIn("exigem embeddings", warning)
        self.assertIn("exigem embeddings", search_error)
        self.assertIn("modo léxico", warning.lower())
        self.assertIn("modo léxico", search_error.lower())
        # nenhuma das duas pode sugerir que a busca simplesmente "não achou nada"
        self.assertNotIn("vazio", warning)
        self.assertNotIn("vazio", search_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
