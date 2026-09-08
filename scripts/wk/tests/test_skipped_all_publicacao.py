"""Achado (baixa) release.py:581-588 `_render_all` — documento do plano com
`unit_ids` vazio era pulado (`continue`) sem entrar em `blockers`; se TODOS
os documentos do plano caíssem nesse caso, `entries={}`/`blockers=[]` e
`publish_revision` devolvia `nothing_to_publish=True`, indistinguível de um
plano legitimamente vazio.

`ReleaseResult.skipped`/`skipped_all` (novo, em `publishing.release`)
distingue os dois casos; este arquivo cobre a costura no CLI: `_publish_local`
mapeia o resultado para o envelope (`publicacoes["skipped"]`/`["skipped_all"]`),
`_apply_publicacao_status` rebaixa `succeeded` -> `partial` e soma um `pending`
tipado (`publicacao_documentos_pulados`), e `_delivery_status_from_publicacoes`
reporta `not_ready` (nunca `not_applicable`, que é reservado a
`nothing_to_publish`).

Os testes de `ReleaseResult`/`publish_revision` propriamente ditos (plano com
2 docs sem unidades ⇒ `skipped_all`; plano vazio ⇒ `nothing_to_publish`; misto
⇒ publica os válidos e lista `skipped`) estão em
`scripts/publishing/tests/test_release.py::TestSkippedDocuments`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from wk import cli


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Unidade: `_apply_publicacao_status` / `_delivery_status_from_publicacoes`
# ---------------------------------------------------------------------------


class TestApplyPublicacaoStatusSkippedAll(unittest.TestCase):
    """`_apply_publicacao_status` sobre um `publicacoes` fake com
    `skipped_all=True` — sem tocar disco/DB, só a lógica de dobra."""

    def _publicacoes_skipped_all(self) -> dict:
        return {
            "revisao": "rev1",
            "namespace_origem": "wiki",
            "namespaces_publicados": ["wiki"],
            "skipped": [
                {"doc_id": "doc_a", "reason": "documento sem unidades (unit_ids vazio)"},
                {"doc_id": "doc_b", "reason": "documento sem unidades (unit_ids vazio)"},
            ],
            "skipped_all": True,
        }

    def test_rebaixa_succeeded_para_partial(self):
        operation_status, knowledge_status, pending = cli._apply_publicacao_status(
            "succeeded", "complete", [],
            command="ingest", repo_abs=None,
            publicacoes=self._publicacoes_skipped_all(),
        )
        self.assertEqual(operation_status, "partial")
        self.assertEqual(knowledge_status, "partial")

    def test_adiciona_pending_tipado(self):
        _op, _kn, pending = cli._apply_publicacao_status(
            "succeeded", "complete", [],
            command="ingest", repo_abs=None,
            publicacoes=self._publicacoes_skipped_all(),
        )
        tipos = [p["tipo"] for p in pending]
        self.assertIn("publicacao_documentos_pulados", tipos)
        item = next(p for p in pending if p["tipo"] == "publicacao_documentos_pulados")
        self.assertIn("doc_a", item["causa"])
        self.assertIn("doc_b", item["causa"])
        self.assertFalse(item["recuperacao_automatica"])

    def test_nao_confunde_com_bloqueio_comum(self):
        """`skipped_all` nunca populariza `publicacoes["bloqueios"]` — o ramo
        de bloqueio comum não deve também disparar (evita pending duplicado)."""
        _op, _kn, pending = cli._apply_publicacao_status(
            "succeeded", "complete", [],
            command="ingest", repo_abs=None,
            publicacoes=self._publicacoes_skipped_all(),
        )
        self.assertEqual(len(pending), 1)

    def test_plano_vazio_nothing_to_publish_nao_e_afetado(self):
        """Regressão: caso `nothing_to_publish` (plano SEM documento) continua
        sem rebaixar `succeeded` nem adicionar `pending` — comportamento
        pré-existente preservado."""
        operation_status, knowledge_status, pending = cli._apply_publicacao_status(
            "succeeded", "complete", [],
            command="ingest", repo_abs=None,
            publicacoes={"revisao": "rev1", "nothing_to_publish": True},
        )
        self.assertEqual(operation_status, "succeeded")
        self.assertEqual(knowledge_status, "complete")
        self.assertEqual(pending, [])

    def test_bloqueio_comum_continua_rebaixando_sem_pending_extra(self):
        """Regressão: bloqueio de publicação comum (`bloqueios` não-vazio,
        sem `skipped_all`) continua só rebaixando `succeeded` -> `partial`,
        sem o `pending` tipado de `skipped_all`."""
        operation_status, knowledge_status, pending = cli._apply_publicacao_status(
            "succeeded", "complete", [],
            command="ingest", repo_abs=None,
            publicacoes={"revisao": "rev1", "bloqueios": ["falha X"]},
        )
        self.assertEqual(operation_status, "partial")
        self.assertEqual(knowledge_status, "partial")
        self.assertEqual(pending, [])


class TestDeliveryStatusSkippedAll(unittest.TestCase):
    def test_skipped_all_e_not_ready(self):
        self.assertEqual(
            cli._delivery_status_from_publicacoes({"skipped_all": True}),
            "not_ready",
        )

    def test_nothing_to_publish_continua_not_applicable(self):
        self.assertEqual(
            cli._delivery_status_from_publicacoes({"nothing_to_publish": True}),
            "not_applicable",
        )

    def test_bloqueios_continua_not_ready(self):
        self.assertEqual(
            cli._delivery_status_from_publicacoes({"bloqueios": ["x"]}),
            "not_ready",
        )

    def test_sucesso_normal_continua_ready(self):
        self.assertEqual(cli._delivery_status_from_publicacoes({}), "ready")


# ---------------------------------------------------------------------------
# Ponta a ponta: `wk ingest` com `publish_revision` fake devolvendo
# `skipped_all=True` — confirma a costura completa do envelope.
# ---------------------------------------------------------------------------


class TestIngestEnvelopeSkippedAll(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wiki-ai-skipped-all-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.src = os.path.join(self.tmp, "doc.md")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("# Documento de teste\n\nConteudo qualquer para ingestao.\n")

    @contextlib.contextmanager
    def _fake_skipped_all(self):
        """`publishing.release.publish_revision` real, mas com o resultado
        forçado para `skipped_all=True` (mesmo mecanismo de mock de
        `test_efeito_publicacao_pendente.py`: delega para a implementação
        real e só reescreve o retorno)."""
        import publishing.release as pub_release_mod

        original_publish = pub_release_mod.publish_revision

        def _fake_publish(*args, **kwargs):
            result = original_publish(*args, **kwargs)
            return dataclasses.replace(
                result,
                ok=True,
                nothing_to_publish=False,
                skipped_all=True,
                skipped=(
                    {"doc_id": "doc-fake", "reason": "documento sem unidades (unit_ids vazio, teste)"},
                ),
            )

        with mock.patch("publishing.release.publish_revision", side_effect=_fake_publish):
            yield

    def test_envelope_marca_partial_com_pending_tipado(self):
        with self._fake_skipped_all():
            code, out, err = _run(["ingest", self.src, "--store", self.store, "--json"])
        self.assertEqual(err, "", err)
        payload = json.loads(out)

        self.assertEqual(payload["operation_status"], "partial")

        publicacoes = payload["summary"]["detail"].get("publicacoes") or {}
        self.assertTrue(publicacoes.get("skipped_all"))
        self.assertEqual(
            [s["doc_id"] for s in publicacoes.get("skipped", [])], ["doc-fake"]
        )
        self.assertNotIn("nothing_to_publish", publicacoes)
        self.assertNotIn("bloqueios", publicacoes)

        tipos_pending = [p["tipo"] for p in payload.get("pending", [])]
        self.assertIn("publicacao_documentos_pulados", tipos_pending)
        self.assertEqual(code, 3)  # §10.3: partial -> exit 3


if __name__ == "__main__":
    unittest.main()
