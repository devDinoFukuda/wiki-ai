"""Testes do efeito `publicacao_pendente` (§9.4/S9.4-09).

`_publish_local` nunca desfaz a revisão de conhecimento já gravada quando a
publicação falha, mas sem rastrear o efeito à parte a reingestão do MESMO
arquivo (`ingestion.correlate` devolve `duplicate=True`/`revision_id=None`,
§8.2.1 — nenhuma revisão nova) saía cedo por "fonte duplicada" e nunca
tentava publicar de novo.

Cobre:

- efeito persistido (arquivo JSON sob o store) após falha de publicação;
- reingestão idêntica retenta o efeito e o conclui;
- concluir o efeito NUNCA cria revisão de conhecimento nova (compara
  `revision_id`/contagem de revisões antes e depois);
- `wk status --store` mostra o efeito pendente (`pending[]` + `summary.detail`).
"""

from __future__ import annotations

import contextlib
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


class TestEfeitoPublicacaoPendente(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wiki-ai-efeito-pendente-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.src = os.path.join(self.tmp, "doc.md")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("# Documento de teste\n\nConteudo qualquer para ingestao.\n")

    def _efeitos_path(self) -> str:
        return os.path.join(self.store, "efeitos_publicacao_pendentes.json")

    def _load_efeitos(self) -> dict:
        with open(self._efeitos_path(), encoding="utf-8") as fh:
            return json.load(fh)

    @contextlib.contextmanager
    def _falha_na_primeira_publicacao(self):
        """`publishing.release.publish_revision` levanta na 1ª chamada, e
        delega para a implementação real dali em diante — mesmo mecanismo do
        teste de especificação em `scripts/publishing/tests/test_spec_gaps_delivery.py`
        (`TestDeliveryReingestAfterFailure`)."""
        import publishing.release as pub_release_mod

        original_publish = pub_release_mod.publish_revision
        calls = {"n": 0}

        def _fake_publish(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("falha simulada de publicacao (teste)")
            return original_publish(*args, **kwargs)

        with mock.patch("publishing.release.publish_revision", side_effect=_fake_publish):
            yield calls

    def test_efeito_persistido_apos_falha_de_publicacao(self):
        with self._falha_na_primeira_publicacao():
            code, out, err = _run(["ingest", self.src, "--store", self.store, "--json"])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertTrue(
            payload["summary"]["detail"]["publicacoes"].get("bloqueios"),
            "primeira publicação deveria estar bloqueada (falha simulada)",
        )

        self.assertTrue(
            os.path.isfile(self._efeitos_path()),
            "efeito de publicação pendente não foi persistido sob o store",
        )
        data = self._load_efeitos()
        pendentes = [e for e in data["efeitos"] if e.get("status") == "pending"]
        self.assertEqual(len(pendentes), 1, data)
        efeito = pendentes[0]
        self.assertEqual(efeito["effect_type"], "publicacao_pendente")
        self.assertEqual(efeito["namespace"], "wiki")  # default de `cmd_ingest` sem --repo
        self.assertTrue(efeito["revision_id"])
        self.assertIn("falha simulada", efeito["motivo"])
        self.assertEqual(efeito["tentativas"], 1)

    def test_reingestao_identica_retenta_e_conclui_o_efeito(self):
        with self._falha_na_primeira_publicacao() as calls:
            code1, out1, err1 = _run(["ingest", self.src, "--store", self.store, "--json"])
            self.assertEqual(calls["n"], 1)
            self.assertEqual(err1, "", err1)

            code2, out2, err2 = _run(["ingest", self.src, "--store", self.store, "--json"])
            self.assertEqual(
                calls["n"], 2,
                "reingestão idêntica deveria ter retentado o efeito de publicação pendente",
            )
            self.assertEqual(err2, "", err2)

        payload2 = json.loads(out2)
        publicacoes2 = payload2["summary"]["detail"]["publicacoes"]
        self.assertFalse(publicacoes2.get("bloqueios"), publicacoes2)
        self.assertIn(
            "efeito_pendente_concluido", publicacoes2,
            "retentativa bem-sucedida deveria reportar a conclusão do efeito pendente",
        )
        self.assertEqual(payload2["operation_status"], "succeeded", payload2)
        avisos2 = payload2["summary"]["detail"]["avisos"]
        self.assertTrue(
            any("publicacao_pendente_concluida" in a for a in avisos2), avisos2,
        )

        data = self._load_efeitos()
        self.assertTrue(data["efeitos"])
        for efeito in data["efeitos"]:
            self.assertEqual(efeito["status"], "done", efeito)

    def test_conclusao_do_efeito_nao_cria_revisao_de_conhecimento_nova(self):
        with self._falha_na_primeira_publicacao():
            code1, out1, err1 = _run(["ingest", self.src, "--store", self.store, "--json"])
            payload1 = json.loads(out1)
            revisao_antes = payload1["summary"]["detail"]["publicacoes"]["revisao"]
            self.assertTrue(revisao_antes)

            code2, out2, err2 = _run(["ingest", self.src, "--store", self.store, "--json"])
            payload2 = json.loads(out2)

        # nenhuma fonte desta 2ª execução gravou revisão nova — `duplicate`
        # continua batendo o hash da mesma versão de fonte (§8.2.1).
        self.assertEqual(payload2["summary"]["detail"]["revisoes"], [])
        revisao_depois = payload2["summary"]["detail"]["publicacoes"]["revisao"]
        self.assertEqual(
            revisao_antes, revisao_depois,
            "concluir o efeito pendente não pode publicar/apontar para uma revisão diferente "
            "da que já existia (§9.4: conclusão não cria revisão de conhecimento nova)",
        )

        from knowledge.repository import Repository

        repo = Repository.open(os.path.join(self.store, "knowledge.db"))
        try:
            total_revisoes = repo.conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        finally:
            repo.close()
        self.assertEqual(
            total_revisoes, 1,
            "reingestão idêntica + retentativa de publicação não pode gravar revisão nova",
        )

    def test_duplicada_sem_efeito_pendente_mantem_comportamento_atual(self):
        """Sem nenhuma falha de publicação anterior, reingestão idêntica
        continua noop/succeeded com o aviso D8 — nenhum efeito é criado."""
        code1, out1, err1 = _run(["ingest", self.src, "--store", self.store, "--json"])
        self.assertEqual(err1, "", err1)
        self.assertFalse(os.path.exists(self._efeitos_path()))

        code2, out2, err2 = _run(["ingest", self.src, "--store", self.store, "--json"])
        self.assertEqual(err2, "", err2)
        payload2 = json.loads(out2)
        avisos2 = payload2["summary"]["detail"]["avisos"]
        self.assertTrue(any("reingestao identica" in a for a in avisos2), avisos2)
        self.assertFalse(
            os.path.exists(self._efeitos_path()),
            "sem falha de publicação anterior, nenhum efeito pendente deveria ter sido criado",
        )

    def test_status_store_mostra_a_publicacao_pendente(self):
        with self._falha_na_primeira_publicacao():
            code, out, err = _run(["ingest", self.src, "--store", self.store, "--json"])
        self.assertTrue(json.loads(out)["summary"]["detail"]["publicacoes"].get("bloqueios"))

        code_s, out_s, err_s = _run(["status", "--store", self.store, "--json"])
        self.assertEqual(err_s, "", err_s)
        payload_s = json.loads(out_s)
        detail = payload_s["summary"]["detail"]

        self.assertTrue(detail.get("publicacao_pendente"), detail)
        efeito = detail["publicacao_pendente"][0]
        self.assertEqual(efeito["effect_type"], "publicacao_pendente")
        self.assertEqual(efeito["status"], "pending")

        pending_items = [p for p in payload_s["pending"] if p["tipo"] == "publicacao_pendente"]
        self.assertEqual(len(pending_items), 1, payload_s["pending"])
        item = pending_items[0]
        self.assertTrue(item["recuperacao_automatica"])
        self.assertIn(efeito["namespace"], item["alvo"])
        self.assertIn(efeito["revision_id"], item["alvo"])
        self.assertTrue(item["acoes"])


if __name__ == "__main__":
    unittest.main()
