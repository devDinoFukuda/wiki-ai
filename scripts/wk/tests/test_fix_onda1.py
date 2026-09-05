# -*- coding: utf-8 -*-
"""Testes unitários novos para o contrato F-11, F-18, F-01, F-41.

F-11: `promote --approve-all` exige `--topic` E `--approved-by` (sem default).
F-18: `publish` exige `--topic` não vazio.
F-01: `id`/`topic`/`source_type` com traversal (`..`, `/`, componente com `.`) recusados.
F-41: `_yaml_scalar` — bool→true/false; int 0/1 permanecem 0/1.

Rodar:
    python -m pytest scripts/wk/tests/test_fix_onda1.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from wk import cli


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


class PromoteApproveAllValidationTests(unittest.TestCase):
    """F-11: validações rigorosas de --approve-all"""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda1_store_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_approve_all_sem_topic_retorna_exit_2_com_erro_json(self):
        """--approve-all sem --topic deve retornar exit 2 com erro JSON."""
        code, out, err = _run(
            ["promote", "--approve-all", "--source-type", "agent-output",
             "--approved-by", "tester", "--store", self.store]
        )
        self.assertEqual(code, 2)
        data = json.loads(err)
        self.assertIn("error", data)
        self.assertIn("--topic", data["error"])
        self.assertIn("acao", data)

    def test_approve_all_sem_approved_by_retorna_exit_2_com_erro_json(self):
        """--approve-all sem --approved-by deve retornar exit 2."""
        code, out, err = _run(
            ["promote", "--approve-all", "--source-type", "agent-output",
             "--topic", "demo", "--store", self.store]
        )
        self.assertEqual(code, 2)
        data = json.loads(err)
        self.assertIn("error", data)
        self.assertIn("--approved-by", data["error"])

    def test_approve_all_source_type_invalido_retorna_exit_2_com_validos(self):
        """--source-type com valor inválido deve listar os válidos."""
        code, out, err = _run(
            ["promote", "--approve-all", "--source-type", "tipo-invalido",
             "--topic", "demo", "--approved-by", "tester", "--store", self.store]
        )
        self.assertEqual(code, 2)
        data = json.loads(err)
        self.assertIn("error", data)
        self.assertIn("inválido", data["error"].lower())
        self.assertIn("validos", data)
        self.assertIsInstance(data["validos"], list)
        self.assertGreater(len(data["validos"]), 0)

    def test_approve_all_com_topic_X_nao_promove_item_topic_Y(self):
        """--approve-all --topic X não deve promover items com topic Y diferente."""
        # Criar dois documentos com topics diferentes
        path_x = os.path.join(self.store, "inbox", "agent-output", "x.md")
        path_y = os.path.join(self.store, "inbox", "agent-output", "y.md")

        _write(path_x, (
            "---\nid: sb-x1\nsource_type: agent-output\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo-x\n---\n\n# X\nConteúdo X.\n"
        ))
        _write(path_y, (
            "---\nid: sb-y1\nsource_type: agent-output\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: false\ntopic: demo-y\n---\n\n# Y\nConteúdo Y.\n"
        ))

        # Promover apenas topic demo-x
        code, out, err = _run(
            ["promote", "--approve-all", "--source-type", "agent-output",
             "--topic", "demo-x", "--approved-by", "tester", "--store", self.store]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)

        # Apenas x deve ser promovido
        promovidos_ids = {p["id"] for p in data["promovidos"]}
        self.assertIn("sb-x1", promovidos_ids)
        self.assertNotIn("sb-y1", promovidos_ids)

        # y deve estar em decisao_humana
        humana_ids = {h["id"] for h in data["decisao_humana"]}
        self.assertIn("sb-y1", humana_ids)


class PublishTopicValidationTests(unittest.TestCase):
    """F-18: --topic é obrigatório e não pode ser vazio"""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda1_store_")
        self.workdir = tempfile.mkdtemp(prefix="wk_onda1_workdir_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_publish_sem_topic_retorna_exit_2_com_erro_json(self):
        """publish sem --topic deve retornar exit 2 com erro JSON."""
        # Criar um arquivo no workdir
        _write(os.path.join(self.workdir, "test.md"), "# Test\nContent.\n")

        code, out, err = _run(
            ["publish", "--workdir", self.workdir, "--store", self.store]
        )
        self.assertEqual(code, 2)
        data = json.loads(err)
        self.assertIn("error", data)
        self.assertIn("--topic", data["error"])
        self.assertIn("acao", data)

    def test_publish_com_topic_vazio_retorna_exit_2(self):
        """publish com --topic="" ou "--topic '   '" deve retornar exit 2."""
        _write(os.path.join(self.workdir, "test.md"), "# Test\nContent.\n")

        code, out, err = _run(
            ["publish", "--workdir", self.workdir, "--topic", "   ",
             "--store", self.store]
        )
        self.assertEqual(code, 2)
        data = json.loads(err)
        self.assertIn("error", data)
        self.assertIn("--topic", data["error"])


class TraversalAttackTests(unittest.TestCase):
    """F-01: id/topic/source_type com traversal são recusados"""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_onda1_traverse_")
        self.workdir = tempfile.mkdtemp(prefix="wk_onda1_workdir_traverse_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_compile_recusa_id_com_traversal_em_recusados(self):
        """F09 (W0): compile rejeita id: ../evil e bloqueia toda a compilação.

        F09 mudou a semântica: qualquer recusado bloqueia a promoção inteira
        (exit 1, "bloqueado" no JSON, nada é promovido). Antes, o teste esperava
        que a página boa fosse compilada mesmo com um recusado, mas agora uma
        fonte recusada aborta tudo."""
        # Criar documento com id malicioso
        bad_path = os.path.join(self.store, "raw", "code-notes", "malicious.md")
        _write(bad_path, (
            "---\nid: ../../../evil\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: true\n"
            "confidence: reviewed\ntopic: demo\n---\n\nMalicious content.\n"
        ))

        # Criar um documento legítimo também (não será compilado por causa do bloqueio)
        good_path = os.path.join(self.store, "raw", "code-notes", "good.md")
        _write(good_path, (
            "---\nid: sb-good-1\nsource_type: code-repo\norigin: test\n"
            "captured_at: 2026-08-07T10:00:00Z\npromoted: true\n"
            "confidence: reviewed\ntopic: demo\n---\n\nGood content.\n"
        ))

        code, out, err = _run(["compile", "--store", self.store])

        # F09: exit 1 (bloqueado)
        self.assertEqual(code, 1, f"F09: compile bloqueado deve retornar 1, foi {code}")
        data = json.loads(out)

        # O malicioso deve estar em recusados
        self.assertIn("recusados", data)
        self.assertGreater(len(data["recusados"]), 0)
        # Verificar que o mal-intencionado está na lista de recusados
        recusados_strs = str(data["recusados"])
        self.assertIn("evil", recusados_strs)

        # F09: paginas vazio (promoção bloqueada)
        self.assertEqual(len(data["paginas"]), 0,
                        "F09: com recusado, paginas deve ser vazio (promoção bloqueada)")

        # F09: "bloqueado" presente no JSON
        self.assertIn("bloqueado", data)

        # Verificar que nada foi criado fora de wiki/
        wiki_root = os.path.join(self.store, "wiki")
        # Só index.md será criado (vazio), nada do conteúdo foi promovido
        for filename in os.listdir(wiki_root):
            if filename != "index.md":
                filepath = os.path.join(wiki_root, filename)
                if os.path.isfile(filepath):
                    with open(filepath, encoding="utf-8") as fp:
                        content = fp.read()
                        # Nenhum arquivo deve conter "evil" ou "good"
                        self.assertNotIn("evil", content.lower())
                        self.assertNotIn("good", content.lower())

    def test_publish_recusa_topic_com_componente_iniciado_por_ponto(self):
        """publish --topic com componente iniciado por . é recusado"""
        _write(os.path.join(self.workdir, "test.md"), "# Test\n")

        code, out, err = _run(
            ["publish", "--workdir", self.workdir, "--topic", ".hidden/path",
             "--store", self.store]
        )
        self.assertEqual(code, 2)
        data = json.loads(err)
        self.assertIn("error", data)
        self.assertIn("iniciado por", data["error"])

    def test_publish_recusa_topic_com_traversal(self):
        """publish --topic com .. é recusado"""
        _write(os.path.join(self.workdir, "test.md"), "# Test\n")

        code, out, err = _run(
            ["publish", "--workdir", self.workdir, "--topic", "../evil",
             "--store", self.store]
        )
        self.assertEqual(code, 2)
        data = json.loads(err)
        self.assertIn("error", data)


class YamlScalarTests(unittest.TestCase):
    """F-41: _yaml_scalar bool→true/false; int 0/1 permanecem 0/1"""

    def test_yaml_scalar_bool_true(self):
        """_yaml_scalar(True) deve retornar "true" (string)"""
        result = cli._yaml_scalar(True)
        self.assertEqual(result, "true")

    def test_yaml_scalar_bool_false(self):
        """_yaml_scalar(False) deve retornar "false" (string)"""
        result = cli._yaml_scalar(False)
        self.assertEqual(result, "false")

    def test_yaml_scalar_int_0(self):
        """_yaml_scalar(0) deve retornar "0" (não "false")"""
        result = cli._yaml_scalar(0)
        self.assertEqual(result, "0")

    def test_yaml_scalar_int_1(self):
        """_yaml_scalar(1) deve retornar "1" (não "true")"""
        result = cli._yaml_scalar(1)
        self.assertEqual(result, "1")

    def test_yaml_scalar_int_2(self):
        """_yaml_scalar(2) deve retornar "2" """
        result = cli._yaml_scalar(2)
        self.assertEqual(result, "2")

    def test_yaml_scalar_negative_int(self):
        """_yaml_scalar(-1) deve retornar "-1" """
        result = cli._yaml_scalar(-1)
        self.assertEqual(result, "-1")

    def test_yaml_scalar_string(self):
        """_yaml_scalar("text") deve retornar JSON-encoded string"""
        result = cli._yaml_scalar("hello")
        # Deve ser quoted JSON
        self.assertEqual(result, '"hello"')


if __name__ == "__main__":
    unittest.main(verbosity=2)
