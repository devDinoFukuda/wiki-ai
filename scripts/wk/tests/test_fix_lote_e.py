# -*- coding: utf-8 -*-
"""Testes do lote E (6 fixes no mesmo arquivo: scripts/wk/cli.py).

FIX 1 (P0)  coupling.html não é mais descartado no publish — vira asset
            navegável (raw/assets/<id>.html + stub .md), copiado para dentro
            de wiki/ pelo compile e linkado; docx nunca tenta convertê-lo.
FIX 2 (P1)  wiki/<topic>/overview.md: síntese determinística que EMBUTE
            arquitetura/C4/ADRs/edge-cases/diagramas/confiança, linkada em
            destaque no topo de wiki/index.md; tolerante a artefato ausente.
FIX 3 (P1)  `verify` falhado (state.json стages.verify.status == "failed")
            bloqueia promote/compile/docx (por item em promote, por escopo de
            topic em compile/docx); --allow-unverified faz override
            registrado no log.
FIX 4 (P2)  publish cruza modules/*.md com state.json['stages']['modules']
            ['done'] e descarta órfãos (relatados); fallback seguro publica
            tudo se não houver state.json/lista `done`.
FIX 5 (P2)  build_pyz.py embute `_build_manifest.json` (source_sha256 +
            built_at); wk/cli.py sabe comparar isso com um `scripts/` irmão
            do `.pyz` em disco, degradando sem alarme falso quando a fonte
            não está disponível.
FIX 6 (P3)  docx gera um `.docx` agregador por tópico (`index.docx`) via
            markdown agregado (`_build_topic_overview`) + `docxgen.
            build_document` — sem tocar docxgen.py/docx_md.py/docx_ooxml.py/
            docx_meta.py.

Fixtures 100% sintéticas — não depende do conteúdo de teste_wk/.

Rodar:
    cd scripts && python -m pytest wk/tests/test_fix_lote_e.py -q
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


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- fixture: workdir sintético de codescan ----------


def _build_workdir(
    root: str, *, verify_status: str = "done", extra_module_orphan: bool = True, full: bool = False
) -> str:
    """Monta `<root>/demo-workdir-aaaaaaaa/{sdd,modules,state.json}` com o
    essencial de cada categoria de artefato do contrato (sdd-contract.md):
    architecture+C4, ADR, code-analysis com 2 módulos, specs/<unit>/
    edge-cases.md, flowcharts/_index.md, sequences/<fluxo>.md,
    confidence-report.md, gaps.md, coupling.md/coupling.html,
    inventory.md/dependencies.md, sdd/confirmed.md/sdd/inferred.md (canônicos
    sob sdd/ — codescan/sdd.py:262-263), modules/*.md (mais um "órfão" fora
    de state.json['stages']['modules']['done']).

    `full=True` completa as duas únicas categorias que o fixture "essencial"
    deixa de fora (c4-containers.md/c4-components.md) — usado pelo teste B2
    de "zero lacunas de síntese com todos os artefatos presentes"."""
    wd = os.path.join(root, "demo-workdir-aaaaaaaa")

    _write(os.path.join(wd, "sdd", "architecture.md"), (
        "# Visão Arquitetural — demo\n\n## Visão geral\nSistema demo.\n\n"
        "```mermaid\nflowchart LR\n  a[A] --> b[B]\n```\n"
    ))
    _write(os.path.join(wd, "sdd", "c4-context.md"), "# Diagrama C4 — Contexto\n\n## Contexto\nCliente usa o sistema.\n")
    if full:
        _write(os.path.join(wd, "sdd", "c4-containers.md"), "# Diagrama C4 — Containers\n\n## Containers\nServiço A e B.\n")
        _write(os.path.join(wd, "sdd", "c4-components.md"), "# Diagrama C4 — Componentes\n\n## Componentes\nControllers e use cases.\n")
    _write(os.path.join(wd, "sdd", "coupling.md"), "# Coupling\n\n```mermaid\nflowchart LR\n  a --> b\n```\n")
    _write(os.path.join(wd, "sdd", "coupling.html"), "<html><body><h1>Coupling interativo</h1></body></html>\n")
    _write(os.path.join(wd, "sdd", "inventory.md"), "# Inventory\n- item 1\n")
    _write(os.path.join(wd, "sdd", "dependencies.md"), "# Dependencies\n- dep 1\n")
    _write(os.path.join(wd, "sdd", "adrs", "001-outbox.md"), (
        "# ADR-001 — Padrão Outbox\n\n## Contexto\n- necessidade de publicar eventos.\n\n"
        "## Decisão\n- Adotar outbox transacional.\n\n## Consequências\n- eventual consistency.\n"
    ))
    _write(os.path.join(wd, "sdd", "code-analysis.md"), (
        "# Análise de código\n\n## Visão geral\n- consolidação de 2 módulos.\n\n"
        "### Módulo: mod-a\n\n## Responsabilidade\n- faz A.\n\n"
        "### Módulo: mod-b\n\n## Responsabilidade\n- faz B.\n"
    ))
    _write(os.path.join(wd, "sdd", "specs", "api", "edge-cases.md"), "# Edge cases — api\n\n- caso 1: entrada vazia.\n")
    _write(os.path.join(wd, "sdd", "flowcharts", "_index.md"), "# Fluxos de módulos\n\n```mermaid\nflowchart LR\n  inicio --> fim\n```\n")
    _write(os.path.join(wd, "sdd", "sequences", "create.md"), "# Sequência — Criação\n\n```mermaid\nsequenceDiagram\n  A->>B: msg\n```\n")
    _write(os.path.join(wd, "sdd", "confidence-report.md"), "# Relatório de Confiança\n\n| Artefato | Confiança |\n|---|---|\n| x | verde |\n")
    _write(os.path.join(wd, "sdd", "gaps.md"), "# Lacunas\n\n## Lacunas críticas\n- alguma lacuna.\n")
    # Canônicos sob sdd/ (codescan/sdd.py:262-263 ArtifactRule) — NUNCA na
    # raiz do workdir (linha 1076-1078: cópia solta ali é P0 no audit do
    # codescan). B2: um fixture com confirmed/inferred na raiz mascarava o
    # bug de chave sem prefixo "sdd/" em `_build_topic_overview`.
    _write(os.path.join(wd, "sdd", "confirmed.md"), "# Confirmados\n\n- fato confirmado 1.\n")
    _write(os.path.join(wd, "sdd", "inferred.md"), "# Inferidos\n\n- fato inferido 1.\n")

    _write(os.path.join(wd, "modules", "mod-a.md"), "# Módulo mod-a\nconteúdo.\n")
    _write(os.path.join(wd, "modules", "mod-b.md"), "# Módulo mod-b\nconteúdo.\n")
    if extra_module_orphan:
        # Slug diferente de "mod-a"/"mod-b" -> nunca bate com state.json.done,
        # nunca é "órfão disfarçado de válido" por coincidência de slug.
        _write(os.path.join(wd, "modules", "modaorphan.md"), "# módulo órfão duplicado\nconteúdo duplicado.\n")

    _write_json(os.path.join(wd, "state.json"), {
        "repo": "/tmp/demo-repo",
        "topic": "codebases/demo",
        "stages": {
            "modules": {"status": "done", "done": ["mod-a", "mod-b"]},
            "verify": {"status": verify_status, "at": "2026-08-07T10:00:00Z"},
        },
    })
    return wd


class LoteEBase(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk_lote_e_store_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def _codescan_root(self) -> str:
        return os.path.join(self.store, ".codescan")

    def _publish(self, wd: str, topic: str = "codebases/demo") -> dict:
        code, out, err = _run(["publish", "--workdir", wd, "--topic", topic, "--store", self.store])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def _promote_all(self, topic: str = "codebases/demo", **extra_flags) -> dict:
        argv = ["promote", "--store", self.store, "--approve-all", "--source-type", "agent-output",
                "--topic", topic, "--approved-by", "tester"]
        argv += extra_flags.get("argv", [])
        code, out, err = _run(argv)
        self.assertEqual(code, 0, err)
        return json.loads(out)


# ---------- FIX 1: coupling.html vira asset navegável ----------


class Fix1CouplingHtmlTests(LoteEBase):
    def test_publish_inclui_coupling_html_como_asset_code_repo(self):
        wd = _build_workdir(self._codescan_root())
        data = self._publish(wd)
        entries = {e["origem"]: e for e in data["publicados"]}
        self.assertIn("sdd/coupling.html", entries)
        html_entry = entries["sdd/coupling.html"]
        self.assertEqual(html_entry["source_type"], "code-repo")
        self.assertIn("asset", html_entry)
        # asset preservado intacto em raw/assets/, byte a byte
        asset_abs = os.path.join(self.store, html_entry["asset"])
        self.assertTrue(os.path.isfile(asset_abs))
        with open(asset_abs, encoding="utf-8") as f:
            self.assertIn("Coupling interativo", f.read())

    def test_coupling_html_e_coupling_md_tem_ids_distintos(self):
        wd = _build_workdir(self._codescan_root())
        data = self._publish(wd)
        ids = [e["id"] for e in data["publicados"] if e["origem"] in ("sdd/coupling.html", "sdd/coupling.md")]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2, "coupling.html e coupling.md não podem colidir no mesmo doc_id")

    def test_compile_copia_asset_para_wiki_e_linka(self):
        wd = _build_workdir(self._codescan_root())
        self._publish(wd)
        self._promote_all()
        code, out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)

        html_files = []
        for dirpath, _dirnames, filenames in os.walk(os.path.join(self.store, "wiki")):
            html_files.extend(f for f in filenames if f.endswith(".html"))
        self.assertEqual(len(html_files), 1, "asset .html deve ser copiado para dentro de wiki/")

        overview_href = data["overview"][0]["href"]
        overview_path = os.path.join(self.store, "wiki", overview_href)
        with open(overview_path, encoding="utf-8") as f:
            overview_text = f.read()
        self.assertIn(".html", overview_text)
        self.assertIn("Visualização interativa de acoplamento", overview_text)

        # lint não pode acusar link quebrado para o asset recém-copiado
        code, out, err = _run(["lint", "--store", self.store])
        lint = json.loads(out)
        self.assertEqual(lint["regras"]["W1_link_quebrado"], 0, out)

    def test_docx_nao_converte_html_como_markdown(self):
        wd = _build_workdir(self._codescan_root())
        self._publish(wd)
        self._promote_all()
        code, out, err = _run(["docx", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data["ignorados_asset_html"]), 1)
        self.assertEqual(data["pulados"], [])  # skip intencional != falha
        gerado_paths = [d["path"] for d in data["documentos"]]
        self.assertFalse(any("coupling-html" in p for p in gerado_paths))


# ---------- FIX 4: módulos órfãos descartados no publish ----------


class Fix4OrphanModulesTests(LoteEBase):
    def test_orfao_descartado_e_reportado(self):
        wd = _build_workdir(self._codescan_root(), extra_module_orphan=True)
        data = self._publish(wd)
        self.assertEqual(data["modulos_orfaos_descartados"]["n"], 1)
        self.assertEqual(data["modulos_orfaos_descartados"]["arquivos"], ["modaorphan.md"])
        origens = [e["origem"] for e in data["publicados"]]
        self.assertIn("modules/mod-a.md", origens)
        self.assertIn("modules/mod-b.md", origens)
        self.assertNotIn("modules/modaorphan.md", origens)

    def test_sem_state_json_fallback_publica_tudo(self):
        wd = _build_workdir(self._codescan_root(), extra_module_orphan=True)
        os.remove(os.path.join(wd, "state.json"))
        data = self._publish(wd)
        self.assertNotIn("modulos_orfaos_descartados", data)
        origens = [e["origem"] for e in data["publicados"]]
        self.assertIn("modules/modaorphan.md", origens, "sem state.json, fallback seguro publica tudo")

    def test_sem_lista_done_fallback_publica_tudo(self):
        wd = _build_workdir(self._codescan_root(), extra_module_orphan=True)
        _write_json(os.path.join(wd, "state.json"), {"topic": "codebases/demo", "stages": {}})
        data = self._publish(wd)
        self.assertNotIn("modulos_orfaos_descartados", data)


# ---------- FIX 3: portão de verify ----------


class Fix3VerifyGateTests(LoteEBase):
    def test_publish_nao_bloqueia_mas_avisa(self):
        wd = _build_workdir(self._codescan_root(), verify_status="failed")
        data = self._publish(wd)
        self.assertGreater(len(data["publicados"]), 0, "publish não bloqueia — só staging em inbox/")
        self.assertIn("aviso_verify", data)

    def test_promote_bloqueia_por_item_e_permite_override(self):
        wd = _build_workdir(self._codescan_root(), verify_status="failed")
        self._publish(wd)

        data = self._promote_all()
        self.assertEqual(data["promovidos"], [])
        self.assertGreater(len(data["bloqueados_verify"]), 0)

        code, out, err = _run(
            ["promote", "--store", self.store, "--approve-all", "--source-type", "agent-output",
             "--topic", "codebases/demo", "--approved-by", "tester", "--allow-unverified"]
        )
        self.assertEqual(code, 0, err)
        data2 = json.loads(out)
        self.assertGreater(len(data2["promovidos"]), 0)
        self.assertIn("verify_override", data2)

    def test_compile_bloqueia_topic_com_verify_falhado(self):
        wd = _build_workdir(self._codescan_root(), verify_status="failed")
        self._publish(wd)
        code, out, err = _run(["compile", "codebases/demo", "--store", self.store])
        self.assertNotEqual(code, 0)
        self.assertIn("verify", err.lower())

        code, out, err = _run(["compile", "codebases/demo", "--store", self.store, "--allow-unverified"])
        self.assertEqual(code, 0, err)

    def test_docx_bloqueia_topic_com_verify_falhado(self):
        wd = _build_workdir(self._codescan_root(), verify_status="failed")
        self._publish(wd)
        code, out, err = _run(["docx", "codebases/demo", "--store", self.store])
        self.assertNotEqual(code, 0)

    def test_fonte_nao_codescan_nunca_e_bloqueada(self):
        # Workdir de codescan com verify falhado, mas noutro topic — uma
        # fonte manual (sem workdir/topic de codescan) tem que passar batido.
        wd = _build_workdir(self._codescan_root(), verify_status="failed")
        self._publish(wd, topic="codebases/demo")

        manual_path = os.path.join(self.store, "inbox", "transcripts", "manual.md")
        _write(manual_path, (
            "---\n"
            'id: "sb-manual-1"\n'
            "source_type: human-transcript\n"
            'origin: "reuniao"\n'
            "captured_at: 2026-08-07T10:00:00Z\n"
            "promoted: false\n"
            "topic: outro-topico-manual\n"
            "---\n\n# manual\nConteúdo manual.\n"
        ))
        code, out, err = _run(["promote", manual_path, "--approve", manual_path, "--approved-by", "tester", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data["promovidos"]), 1)
        self.assertEqual(data["promovidos"][0]["id"], "sb-manual-1")
        self.assertEqual(data["bloqueados_verify"], [])


# ---------- FIX 2: página de síntese por tópico ----------


class Fix2OverviewSynthesisTests(LoteEBase):
    def _compile(self) -> dict:
        code, out, err = _run(["compile", "--store", self.store])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_overview_embute_arquitetura_adrs_edgecases_diagramas_confianca(self):
        wd = _build_workdir(self._codescan_root())
        self._publish(wd)
        self._promote_all()
        data = self._compile()

        self.assertEqual(len(data["overview"]), 1)
        overview_href = data["overview"][0]["href"]
        overview_path = os.path.join(self.store, "wiki", overview_href)
        self.assertTrue(os.path.isfile(overview_path))
        with open(overview_path, encoding="utf-8") as f:
            text = f.read()

        # Arquitetura embutida (não só link) + mermaid preservado
        self.assertIn("Sistema demo", text)
        self.assertIn("```mermaid", text)
        self.assertIn("flowchart LR", text)
        # C4 contexto embutido
        self.assertIn("Cliente usa o sistema", text)
        # ADR em tabela
        self.assertIn("ADR-001", text)
        self.assertIn("Adotar outbox transacional", text)
        # Análise de código: índice, não os 72KB inteiros
        self.assertIn("mod-a", text)
        self.assertIn("mod-b", text)
        # Edge cases consolidados
        self.assertIn("caso 1: entrada vazia", text)
        # Diagramas: flowchart consolidado embutido + sequência linkada
        self.assertIn("```mermaid\nflowchart LR\n  inicio --> fim", text)
        self.assertIn("Sequências", text)
        # Confiança
        self.assertIn("Relatório de Confiança", text)
        self.assertIn("Lacunas críticas", text)
        self.assertIn("Confirmado", text)
        self.assertIn("Inferido", text)

        # frontmatter com sources declaradas (senão lint acusa
        # wiki_sem_fontes_declaradas)
        self.assertTrue(text.startswith("---\n"))
        header = "\n".join(text.splitlines()[:8])
        self.assertIn("sources:", header)

    def test_overview_linkada_em_destaque_no_index(self):
        wd = _build_workdir(self._codescan_root())
        self._publish(wd)
        self._promote_all()
        self._compile()
        index_path = os.path.join(self.store, "wiki", "index.md")
        with open(index_path, encoding="utf-8") as f:
            index_text = f.read()
        self.assertIn("Visão geral por tópico", index_text)
        self.assertIn("overview.md", index_text)
        # a seção de destaque vem antes da listagem plana de páginas
        self.assertLess(index_text.index("Visão geral por tópico"), index_text.index("## codebases/demo ("))

    def test_tolerante_a_artefato_ausente_registra_lacuna(self):
        wd = _build_workdir(self._codescan_root())
        os.remove(os.path.join(wd, "sdd", "gaps.md"))  # remove um artefato
        self._publish(wd)
        self._promote_all()
        data = self._compile()
        lacunas = data["overview"][0]["lacunas"]
        self.assertTrue(any("gaps.md" in item for item in lacunas))
        # não deve ter quebrado a compilação
        self.assertTrue(data["reindexed"])

    def test_topico_sem_artefato_codescan_nao_gera_overview_vazio(self):
        # Fonte code-repo "pura" (sem origin de `wk publish`) não deve ganhar
        # overview.md — nada reconhecível para sintetizar.
        path = os.path.join(self.store, "raw", "code-notes", "solto.md")
        _write(path, (
            "---\n"
            'id: "sb-solto-1"\nsource_type: code-repo\norigin: "manual"\n'
            "captured_at: 2026-08-07T10:00:00Z\npromoted: true\n"
            "confidence: reviewed\ntopic: sem-codescan\n---\n\n# solto\nConteúdo.\n"
        ))
        data = self._compile()
        self.assertEqual(data["overview"], [])
        self.assertFalse(os.path.isfile(os.path.join(self.store, "wiki", "sem-codescan", "overview.md")))

    def test_nao_hardcode_generaliza_para_outro_topico(self):
        # Mesmo fixture, topic diferente do "nome de exemplo" — overview deve
        # sair igualmente rico (identificação é por caminho relativo, não por
        # nome de tópico/repo).
        wd = _build_workdir(self._codescan_root())
        self._publish(wd, topic="qualquer-outro-nome/xyz")
        self._promote_all(topic="qualquer-outro-nome/xyz")
        data = self._compile()
        self.assertEqual(len(data["overview"]), 1)
        self.assertEqual(data["overview"][0]["topic"], "qualquer-outro-nome/xyz")
        overview_path = os.path.join(self.store, "wiki", data["overview"][0]["href"])
        with open(overview_path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("ADR-001", text)

    def test_b2_todos_artefatos_presentes_zero_lacunas_e_confirmado_inferido(self):
        """B2 (validação E2E): a chave de lookup de confirmed.md/inferred.md em
        `_build_topic_overview` tinha que ser "sdd/confirmed.md"/"sdd/inferred.md"
        (mesmo prefixo que `_codescan_artifact_rel` sempre produz) — sem isso a
        subseção "### Confirmado vs. inferido" nunca aparecia e uma lacuna
        espúria era relatada mesmo com os dois artefatos promovidos. Um teste
        que só checasse a presença de "## Confiança" não pegava isso (a seção
        aparece de qualquer forma, via confidence-report.md/gaps.md) — por
        isso este teste asserta ZERO lacunas com TODOS os artefatos presentes,
        não apenas a presença da seção."""
        wd = _build_workdir(self._codescan_root(), full=True)
        self._publish(wd)
        self._promote_all()
        data = self._compile()

        self.assertEqual(len(data["overview"]), 1)
        lacunas = data["overview"][0]["lacunas"]
        self.assertEqual(lacunas, [], f"esperado zero lacunas com todos os artefatos presentes, achou: {lacunas}")

        overview_path = os.path.join(self.store, "wiki", data["overview"][0]["href"])
        with open(overview_path, encoding="utf-8") as f:
            text = f.read()

        self.assertNotIn("## Lacunas de síntese", text)
        self.assertIn("### Confirmado vs. inferido", text)
        self.assertIn("- Confirmado: [", text)
        self.assertIn("- Inferido: [", text)
        # os links apontam para as páginas reais de sdd/confirmed.md e
        # sdd/inferred.md (não para hrefs quebrados "#" de fallback).
        confirmed_line = next(ln for ln in text.splitlines() if ln.startswith("- Confirmado: ["))
        inferred_line = next(ln for ln in text.splitlines() if ln.startswith("- Inferido: ["))
        self.assertNotIn("(#)", confirmed_line)
        self.assertNotIn("(#)", inferred_line)
        confirmed_href = confirmed_line.split("(", 1)[1].split(")", 1)[0]
        inferred_href = inferred_line.split("(", 1)[1].split(")", 1)[0]
        self.assertTrue(os.path.isfile(os.path.join(self.store, "wiki", "codebases", "demo", confirmed_href)))
        self.assertTrue(os.path.isfile(os.path.join(self.store, "wiki", "codebases", "demo", inferred_href)))

        # e as duas subseções de C4 que faltavam no fixture "essencial"
        # também aparecem embutidas (Containers/Componentes), confirmando que
        # `full=True` de fato fechou as únicas duas lacunas anteriores.
        self.assertIn("Containers (C4)", text)
        self.assertIn("Componentes (C4)", text)

        code, out, err = _run(["lint", "--store", self.store])
        lint = json.loads(out)
        self.assertEqual(lint["regras"]["W1_link_quebrado"], 0, out)


# ---------- FIX 6: docx agregador por tópico ----------


class Fix6DocxAggregatorTests(LoteEBase):
    def test_gera_index_docx_por_topico(self):
        wd = _build_workdir(self._codescan_root())
        self._publish(wd)
        self._promote_all()
        code, out, err = _run(["docx", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data["agregadores"]), 1)
        agg_path = os.path.join(self.store, data["agregadores"][0]["path"])
        self.assertEqual(os.path.basename(agg_path), "index.docx")
        self.assertTrue(os.path.isfile(agg_path))

        import zipfile
        with zipfile.ZipFile(agg_path) as zf:
            self.assertIsNone(zf.testzip())
            self.assertIn("word/document.xml", zf.namelist())

    def test_prune_nao_remove_o_agregador_em_execucao_repetida(self):
        wd = _build_workdir(self._codescan_root())
        self._publish(wd)
        self._promote_all()
        _run(["docx", "--store", self.store])
        code, out, err = _run(["docx", "--store", self.store])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        agg_path = os.path.join(self.store, data["agregadores"][0]["path"])
        self.assertTrue(os.path.isfile(agg_path))
        self.assertFalse(any("index.docx" in r["path"] for r in data["removidos"]))


# ---------- FIX 5: build_pyz manifest + doctor ----------


class Fix5PyzFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk_lote_e_pyz_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _build_pyz_module(self):
        import importlib.util

        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        build_pyz_path = os.path.join(here, "build_pyz.py")
        spec = importlib.util.spec_from_file_location("build_pyz_under_test", build_pyz_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_source_sha256_muda_com_conteudo_e_bate_entre_build_pyz_e_cli(self):
        bp = self._build_pyz_module()
        scripts_dir = os.path.join(self.tmp, "scripts")
        os.makedirs(os.path.join(scripts_dir, "wk"))
        with open(os.path.join(scripts_dir, "wk", "cli.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        h1_build = bp._source_sha256(scripts_dir)
        h1_cli = cli._source_sha256(scripts_dir)
        self.assertEqual(h1_build, h1_cli, "wk/cli.py:_source_sha256 tem que ficar em sincronia com build_pyz.py")

        with open(os.path.join(scripts_dir, "wk", "cli.py"), "w", encoding="utf-8") as f:
            f.write("x = 2\n")
        h2 = bp._source_sha256(scripts_dir)
        self.assertNotEqual(h1_build, h2)

    def test_check_pyz_freshness_detecta_desatualizado_e_degrada_sem_fonte(self):
        scripts_dir = os.path.join(self.tmp, "scripts")
        os.makedirs(os.path.join(scripts_dir, "wk"))
        with open(os.path.join(scripts_dir, "wk", "cli.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        archive = os.path.join(self.tmp, "wk.pyz")
        open(archive, "wb").close()
        current_hash = cli._source_sha256(scripts_dir)

        orig_read_asset = cli._read_asset
        try:
            manifest_bytes = json.dumps({"source_sha256": current_hash, "built_at": "2026-01-01T00:00:00Z"}).encode()
            cli._read_asset = lambda name: manifest_bytes if name == cli._BUILD_MANIFEST_NAME else None
            r_fresh = cli._check_pyz_freshness(archive)
            self.assertTrue(r_fresh["fonte_disponivel"])
            self.assertFalse(r_fresh["pyz_desatualizado"])

            with open(os.path.join(scripts_dir, "wk", "cli.py"), "w", encoding="utf-8") as f:
                f.write("x = 2\n")
            r_stale = cli._check_pyz_freshness(archive)
            self.assertTrue(r_stale["pyz_desatualizado"])
            self.assertIn("build_pyz.py", r_stale["acao"])

            cli._read_asset = lambda name: None
            r_no_manifest = cli._check_pyz_freshness(archive)
            self.assertFalse(r_no_manifest["fonte_disponivel"])
            self.assertTrue(r_no_manifest.get("manifesto_ausente"))
        finally:
            cli._read_asset = orig_read_asset

    def test_check_pyz_freshness_sem_scripts_irmao_nao_da_falso_alarme(self):
        archive = os.path.join(self.tmp, "wk.pyz")
        open(archive, "wb").close()  # sem scripts/ ao lado
        orig_read_asset = cli._read_asset
        try:
            manifest_bytes = json.dumps({"source_sha256": "deadbeef"}).encode()
            cli._read_asset = lambda name: manifest_bytes if name == cli._BUILD_MANIFEST_NAME else None
            result = cli._check_pyz_freshness(archive)
            self.assertFalse(result["fonte_disponivel"])
            self.assertNotIn("pyz_desatualizado", result)
        finally:
            cli._read_asset = orig_read_asset

    def test_doctor_nao_quebra_rodando_de_codigo_fonte_solto(self):
        # `python -m pytest` não roda de um .pyz — doctor deve degradar
        # graciosamente (sem alarme falso, sem exception) e reportar isso.
        store = tempfile.mkdtemp(prefix="wk_lote_e_doctor_store_")
        try:
            code, _out, err = _run(["store", "init", store])
            self.assertEqual(code, 0, err)
            code, out, err = _run(["doctor", "--store", store, "--engine", "claude-code"])
            data = json.loads(out)
            self.assertIn("pyz", data["wk"])
            self.assertFalse(data["wk"]["pyz"]["fonte_disponivel"])
        finally:
            shutil.rmtree(store, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
