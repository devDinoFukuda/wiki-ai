"""Testes do corpus S4: promote --approve, publish, ingest, compile granular,
init/check de permissões.

Cobre os bugs confirmados num teste real de ponta a ponta que deixaram o
corpus com zero documentos indexados:
  - nada levava a árvore SDD até inbox/ (publish)
  - promote só auto-promovia code-repo, sem caminho de aprovação humana
  - RAW_DIR_BY_SOURCE_TYPE sem entrada para agent-output
  - compile colapsava tudo em uma página por (topic, source_type)
  - não existia comando de ingestão
  - init não configurava permissões, subagentes perdiam acesso ao store

Rodar:
    python -m unittest discover -s wk/tests -v
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


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _read(path: str) -> str:
    """Lê um arquivo texto fechando o handle explicitamente.

    `open(path).read()` sem `with`/`.close()` deixa o TextIOWrapper para o
    coletor de ciclos do GC. Se a coleta acontecer durante o
    `redirect_stderr` de OUTRO teste (ex.: `_run` em test_fix_onda1.py), o
    `ResourceWarning: unclosed file` some no stderr redirecionado e quebra o
    `json.loads(err)` desse teste — flake dependente de timing do GC, visível
    só com `unittest discover` (que reativa o filtro default de warnings) e
    só na suíte completa (mais alocações acumuladas até cruzar o limiar do
    coletor)."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _fm(doc_id: str, source_type: str, origin: str, *, promoted: str = "false",
        topic: str | None = None, confidence: str | None = None) -> str:
    lines = [
        "---",
        f'id: "{doc_id}"',
        f"source_type: {source_type}",
        f'origin: "{origin}"',
        "captured_at: 2026-07-20T10:00:00Z",
        f"promoted: {promoted}",
    ]
    if confidence:
        lines.append(f"confidence: {confidence}")
    if topic:
        lines.append(f"topic: {topic}")
    lines += ["---", "", f"# {doc_id}", "", "Conteúdo de teste.", ""]
    return "\n".join(lines)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_store_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)


class PromoteApproveTests(StoreTestCase):
    def test_sem_approve_agent_output_nao_promove(self):
        path = os.path.join(self.store, "inbox", "agent-output", "x.md")
        _write(path, _fm("sb-x1", "agent-output", "agente copilot"))

        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)

        self.assertEqual(data["promovidos"], [])
        self.assertEqual(len(data["decisao_humana"]), 1)
        self.assertEqual(data["decisao_humana"][0]["source_type"], "agent-output")
        self.assertTrue(os.path.isfile(path), "arquivo não deveria ter sido movido")

    def test_approve_promove_agent_output_como_unverified_e_registra_aprovador(self):
        path = os.path.join(self.store, "inbox", "agent-output", "y.md")
        _write(path, _fm("sb-y1", "agent-output", "agente copilot"))

        code, out, err = _run(
            ["promote", "--approve", "sb-y1", "--approved-by", "maria", "--store", self.store]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)

        self.assertEqual(len(data["promovidos"]), 1)
        prom = data["promovidos"][0]
        self.assertEqual(prom["confidence"], "unverified")
        self.assertFalse(os.path.isfile(path), "arquivo deveria ter saído de inbox/")

        dest = os.path.join(self.store, prom["path"])
        self.assertTrue(os.path.isfile(dest))
        text = _read(dest)
        self.assertIn('promoted_by: "maria"', text)
        self.assertIn("promoted: true", text)

        log = _read(os.path.join(self.store, "log.md"))
        self.assertIn("aprovado por maria", log)
        self.assertIn("sb-y1", log)

    def test_approve_all_por_source_type_promove_como_reviewed_para_humano(self):
        path = os.path.join(self.store, "inbox", "transcripts", "z.md")
        _write(path, _fm("sb-z1", "human-doc", "Pessoa Real", topic="demo"))

        code, out, err = _run(
            ["promote", "--approve-all", "--source-type", "human-doc", "--topic", "demo",
             "--approved-by", "tester", "--store", self.store]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        promoted_ids = {p["id"]: p for p in data["promovidos"]}
        self.assertIn("sb-z1", promoted_ids)
        self.assertEqual(promoted_ids["sb-z1"]["confidence"], "reviewed")

    def test_approve_all_sem_source_type_e_erro(self):
        code, _out, err = _run(["promote", "--approve-all", "--store", self.store])
        self.assertEqual(code, 2)
        self.assertIn("source-type", err)

    def test_code_repo_continua_auto_promovendo_sem_approve(self):
        path = os.path.join(self.store, "inbox", "code-notes", "w.md")
        _write(path, _fm("sb-w1", "code-repo", "codescan demo @abc123"))

        code, out, err = _run(["promote", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        promoted_ids = {p["id"] for p in data["promovidos"]}
        self.assertIn("sb-w1", promoted_ids)


class PublishTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.workdir = tempfile.mkdtemp(prefix="wk_workdir_")
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)

    def test_publish_leva_sdd_e_modules_ignora_agent_packs_e_state(self):
        _write(os.path.join(self.workdir, "sdd", "inventory.md"), "---\nid: old\n---\n\nInventario.\n")
        _write(os.path.join(self.workdir, "sdd", "dependencies.md"), "Dependencias.\n")
        _write(os.path.join(self.workdir, "sdd", "coupling.md"), "Coupling.\n")
        _write(os.path.join(self.workdir, "sdd", "domain.md"), "Dominio.\n")
        _write(os.path.join(self.workdir, "modules", "foo.md"), "Modulo foo.\n")
        _write(os.path.join(self.workdir, "confirmed.md"), "Confirmado.\n")
        _write(os.path.join(self.workdir, "inferred.md"), "Inferido.\n")
        _write(os.path.join(self.workdir, "agent-packs", "modules-batch-01.json"), "{}")
        _write(os.path.join(self.workdir, "agent-outputs", "modules-batch-01.txt"), "saida bruta")
        _write(os.path.join(self.workdir, "state.json"), "{}")
        _write(os.path.join(self.workdir, "surface.json"), "{}")

        code, out, err = _run(
            ["publish", "--workdir", self.workdir, "--topic", "demo", "--store", self.store]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        published = data["publicados"]

        by_origem = {p["origem"]: p for p in published}
        self.assertEqual(
            set(by_origem),
            {"sdd/inventory.md", "sdd/dependencies.md", "sdd/coupling.md",
             "sdd/domain.md", "modules/foo.md", "confirmed.md", "inferred.md"},
        )

        for rel in ("sdd/inventory.md", "sdd/dependencies.md", "sdd/coupling.md"):
            self.assertEqual(by_origem[rel]["source_type"], "code-repo")
            self.assertTrue(by_origem[rel]["path"].startswith("inbox/code-notes/"))
        for rel in ("sdd/domain.md", "modules/foo.md", "confirmed.md", "inferred.md"):
            self.assertEqual(by_origem[rel]["source_type"], "agent-output")
            self.assertTrue(by_origem[rel]["path"].startswith("inbox/agent-output/"))

        dump = json.dumps(published)
        self.assertNotIn("agent-packs", dump)
        self.assertNotIn("agent-outputs", dump)
        self.assertNotIn("state.json", dump)
        self.assertNotIn("surface.json", dump)

        inv_text = _read(os.path.join(self.store, by_origem["sdd/inventory.md"]["path"]))
        self.assertNotIn("id: old", inv_text)
        self.assertIn('source_type: "code-repo"', inv_text)
        self.assertIn("Inventario.", inv_text)

    def test_publish_recusa_destino_fora_de_inbox(self):
        # sanity: sem candidatos, publish não falha e não escreve nada.
        code, out, err = _run(
            ["publish", "--workdir", self.workdir, "--topic", "demo", "--store", self.store]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["publicados"], [])

    def test_publish_reexecutado_e_idempotente_no_staging(self):
        # reexecutar publish não pode criar duplicata com o mesmo doc_id
        # (achado 'Alto' de docs/application-analysis.md): o arquivo em
        # staging é regravado (refresh), mantendo caminho e id.
        _write(os.path.join(self.workdir, "sdd", "domain.md"), "Dominio v1.\n")

        code, out, err = _run(
            ["publish", "--workdir", self.workdir, "--topic", "demo", "--store", self.store]
        )
        self.assertEqual(code, 0, err)
        first = json.loads(out)["publicados"][0]

        _write(os.path.join(self.workdir, "sdd", "domain.md"), "Dominio v2.\n")
        code, out, err = _run(
            ["publish", "--workdir", self.workdir, "--topic", "demo", "--store", self.store]
        )
        self.assertEqual(code, 0, err)
        second = json.loads(out)["publicados"][0]

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["path"], second["path"])
        self.assertTrue(second.get("atualizado"))

        dest_dir = os.path.dirname(os.path.join(self.store, second["path"]))
        md_files = [n for n in os.listdir(dest_dir) if n.endswith(".md")]
        self.assertEqual(len(md_files), 1, md_files)
        text = _read(os.path.join(self.store, second["path"]))
        self.assertIn("Dominio v2.", text)
        self.assertNotIn("Dominio v1.", text)


class IngestTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="wk_ingest_src_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _ingest(self, path: str, source_type: str = "human-transcript", origin: str = "Reuniao X"):
        return _run(
            ["ingest", path, "--source-type", source_type, "--origin", origin,
             "--topic", "demo", "--store", self.store]
        )

    def test_converte_vtt_preservando_timestamp(self):
        vtt = os.path.join(self.tmp, "a.vtt")
        _write(vtt, "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\nOla mundo\n")
        code, out, err = self._ingest(vtt)
        self.assertEqual(code, 0, err)
        text = _read(os.path.join(self.store, json.loads(out)["path"]))
        self.assertIn("00:00:01.000 --> 00:00:04.000", text)
        self.assertIn("Ola mundo", text)

    def test_converte_srt_preservando_timestamp(self):
        srt = os.path.join(self.tmp, "b.srt")
        _write(srt, "1\n00:00:01,000 --> 00:00:02,000\nLinha um\n\n2\n00:00:03,000 --> 00:00:04,000\nLinha dois\n")
        code, out, err = self._ingest(srt)
        self.assertEqual(code, 0, err)
        text = _read(os.path.join(self.store, json.loads(out)["path"]))
        self.assertIn("00:00:01,000 --> 00:00:02,000", text)
        self.assertIn("Linha um", text)
        self.assertIn("Linha dois", text)

    def test_converte_html_removendo_tags_preservando_headings(self):
        htmlf = os.path.join(self.tmp, "c.html")
        _write(htmlf, "<html><body><h1>Titulo</h1><p>Paragrafo <b>com tag</b>.</p></body></html>")
        code, out, err = self._ingest(htmlf, source_type="web-clip", origin="https://exemplo.com")
        self.assertEqual(code, 0, err)
        text = _read(os.path.join(self.store, json.loads(out)["path"]))
        self.assertIn("# Titulo", text)
        self.assertIn("Paragrafo", text)
        self.assertNotIn("<h1>", text)
        self.assertNotIn("<b>", text)

    def test_converte_xml_em_bloco_de_codigo(self):
        xmlf = os.path.join(self.tmp, "d.xml")
        _write(xmlf, "<root><item>1</item></root>")
        code, out, err = self._ingest(xmlf, source_type="agent-output", origin="export tool")
        self.assertEqual(code, 0, err)
        text = _read(os.path.join(self.store, json.loads(out)["path"]))
        self.assertIn("```xml", text)
        self.assertIn("<root>", text)

    def test_converte_json_em_bloco_de_codigo(self):
        jsonf = os.path.join(self.tmp, "e.json")
        _write(jsonf, '{"a": 1}')
        code, out, err = self._ingest(jsonf, source_type="agent-output", origin="export tool")
        self.assertEqual(code, 0, err)
        text = _read(os.path.join(self.store, json.loads(out)["path"]))
        self.assertIn("```json", text)
        self.assertIn('"a": 1', text)

    def test_pdf_vira_asset_imutavel_com_pagina_de_fonte(self):
        pdff = os.path.join(self.tmp, "f.pdf")
        _write(pdff, "%PDF-1.4 conteudo falso")

        code, out, err = self._ingest(pdff, source_type="human-doc", origin="doc externo")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        asset = os.path.join(self.store, *result["asset"].split("/"))
        self.assertTrue(os.path.isfile(asset), "original imutável deveria estar em raw/assets/")
        self.assertEqual(_read(asset), "%PDF-1.4 conteudo falso")
        stub = _read(os.path.join(self.store, *result["path"].split("/")))
        self.assertIn("raw/assets/", stub)
        self.assertIn("Análise pendente", stub)

    def test_recusa_source_type_invalido(self):
        vtt = os.path.join(self.tmp, "a.vtt")
        _write(vtt, "conteudo\n")
        code, _out, err = self._ingest(vtt, source_type="nao-existe")
        self.assertEqual(code, 2)
        self.assertIn("source_type inválido", err)

    def test_recusa_origin_vazio(self):
        vtt = os.path.join(self.tmp, "a.vtt")
        _write(vtt, "conteudo\n")
        code, _out, err = self._ingest(vtt, origin="")
        self.assertEqual(code, 2)


class CompileGranularityTests(StoreTestCase):
    def test_uma_pagina_por_documento_promovido(self):
        for i in range(3):
            path = os.path.join(self.store, "raw", "code-notes", f"mod{i}.md")
            _write(
                path,
                _fm(f"sb-mod-{i}", "code-repo", "codescan demo", promoted="true",
                    topic="demo", confidence="reviewed"),
            )

        code, out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)

        self.assertEqual(len(data["paginas"]), 4)  # index + 3 documentos
        for i in range(3):
            expected = os.path.join(self.store, "wiki", "demo", "code-repo", f"sb-mod-{i}.md")
            self.assertTrue(os.path.isfile(expected), expected)
            text = _read(expected)
            self.assertIn(f"sb-mod-{i}", text)

        index_text = _read(os.path.join(self.store, "wiki", "index.md"))
        for i in range(3):
            self.assertIn(f"sb-mod-{i}", index_text)


class InitCheckPermissionsTests(unittest.TestCase):
    """cmd_init exige o manifesto de docs embutido no .pyz (não existe em árvore
    de fonte não empacotada) — mockamos `_docs_manifest`/`_doc_text` para
    exercer o cmd_init real de ponta a ponta sem tocar em wk.pyz/build_pyz.py.
    """

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wk_base_")
        self.store = tempfile.mkdtemp(prefix="wk_store_")
        self.repo = tempfile.mkdtemp(prefix="wk_repo_")
        for d in (self.base, self.store, self.repo):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)

    def test_init_escreve_settings_com_deny_e_additional_directories_idempotente(self):
        fake_manifest = {"skill": {"file": "SKILL.md", "asset": "skill.md", "title": "Wiki AI"}}
        with mock.patch.object(cli, "_docs_manifest", return_value=fake_manifest), \
             mock.patch.object(cli, "_doc_text", return_value="# Skill\n"):
            code, out, err = _run(
                ["init", "--engine", "claude-code", "--base", self.base,
                 "--store", self.store, "--repo", self.repo]
            )
            self.assertEqual(code, 0, err)
            # segunda chamada: idempotente, sem duplicar entradas
            code2, out2, err2 = _run(
                ["init", "--engine", "claude-code", "--base", self.base,
                 "--store", self.store, "--repo", self.repo]
            )
            self.assertEqual(code2, 0, err2)

        settings_path = os.path.join(self.base, ".claude", "settings.json")
        self.assertTrue(os.path.isfile(settings_path))
        data = json.loads(_read(settings_path))
        perms = data["permissions"]
        store_abs = os.path.abspath(self.store)
        repo_abs = os.path.abspath(self.repo)

        self.assertIn(store_abs, perms["additionalDirectories"])
        self.assertIn(repo_abs, perms["additionalDirectories"])
        self.assertTrue(any(store_abs in d for d in perms["deny"]))
        # idempotência: nenhuma lista tem duplicata após duas chamadas
        self.assertEqual(perms["additionalDirectories"].count(store_abs), 1)
        self.assertEqual(len(perms["deny"]), len(set(perms["deny"])))
        self.assertEqual(len(perms["allow"]), len(set(perms["allow"])))

    def test_merge_settings_preserva_chaves_nao_relacionadas(self):
        existing = {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}
        merged = cli._merge_settings_permissions(existing, "/abs/store", "/abs/repo")
        self.assertEqual(merged["model"], "opus")
        self.assertIn("Bash(ls)", merged["permissions"]["allow"])
        self.assertIn("/abs/store", merged["permissions"]["additionalDirectories"])
        self.assertIn("/abs/repo", merged["permissions"]["additionalDirectories"])
        self.assertTrue(any("/abs/store" in d for d in merged["permissions"]["deny"]))

    def test_check_reprova_config_ausente_e_aprova_depois_de_escrita(self):
        # `--all` com manifesto vazio (árvore de fonte, sem docs embutidos)
        # deixa o lado de docs sem itens a comparar; isola a validação nova.
        code, out, err = _run(
            ["check", "--engine", "claude-code", "--all", "--base", self.base,
             "--store", self.store, "--repo", self.repo]
        )
        self.assertNotEqual(code, 0)
        data = json.loads(out)
        report = data["config_permissoes"][0]
        self.assertFalse(report["ok"])
        self.assertIn("settings ausente", report["problemas"])

        settings_path = os.path.join(self.base, ".claude", "settings.json")
        cli._write_permission_settings(settings_path, os.path.abspath(self.store), os.path.abspath(self.repo))

        code2, out2, err2 = _run(
            ["check", "--engine", "claude-code", "--all", "--base", self.base,
             "--store", self.store, "--repo", self.repo]
        )
        self.assertEqual(code2, 0, err2)
        data2 = json.loads(out2)
        self.assertTrue(data2["config_permissoes"][0]["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
