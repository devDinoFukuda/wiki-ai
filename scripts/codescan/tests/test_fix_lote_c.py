"""Testes do fix lote C (agentpack.py):

  1. pack gravado em disco e JSON indentado e parseavel; o orcamento de
     bytes continua medido pela forma compacta.
  2. dependencias obrigatorias `sdd/*.md` de um estagio (ex. `architecture`)
     tem prioridade de orcamento sobre o preenchimento oportunista com
     `modules/*.md`.
  3. deduplicacao por hash de conteudo em `_stage_input_files`.
  4. `metrics` do pack expõe `dropped_by_glob` e `duplicates_dropped`.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from codescan import agentpack


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class DumpCompactJsonIsIndentedTest(unittest.TestCase):
    """FIX 1: `dump_compact_json` grava JSON indentado (nao mais em linha
    unica) — um pack em linha unica foi interpretado por um subagente como
    conteudo truncado e abortou um batch inteiro (evidencia real:
    modules-batch-02, logs.txt:438-450)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_pack_gravado_e_json_indentado_e_parseavel(self):
        obj = {"a": 1, "nested": {"b": [1, 2, 3], "c": "texto"}}
        path = os.path.join(self.tmp.name, "out", "pack.json")
        agentpack.dump_compact_json(path, obj)

        with open(path, encoding="utf-8") as f:
            raw = f.read()

        lines = raw.splitlines()
        self.assertGreater(
            len(lines), 1,
            "pack em linha unica: workaround real foi rodar `python -m json.tool` manualmente",
        )
        self.assertTrue(
            any(line.startswith("  ") for line in lines),
            "esperava indentacao de 2 espacos (json.dump indent=2)",
        )
        self.assertEqual(json.loads(raw), obj, "arquivo gravado precisa continuar sendo JSON valido")

    def test_pack_vazio_tambem_e_indentado(self):
        path = os.path.join(self.tmp.name, "out2", "pack.json")
        agentpack.dump_compact_json(path, {"schema": "wiki-ai.agent-pack.v2", "sources": []})
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("\n", raw)


class BudgetMedidoComoFormaCompactaTest(unittest.TestCase):
    """FIX 1 (continuacao): o calculo de orcamento (`compact_json_size`,
    usado por `limits.max_bytes`) precisa continuar medindo a forma
    compacta do pack, mesmo que o arquivo gravado em disco seja indentado —
    senao a capacidade util do pack cai ~30% so por causa da indentacao."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = os.path.join(self.tmp.name, "wd")
        _write(os.path.join(self.wd, "modules", "m1.md"), "# modulo um\n" + ("linha de evidencia operacional\n" * 5))

    def tearDown(self):
        self.tmp.cleanup()

    def test_metrics_bytes_usa_forma_compacta(self):
        pack = agentpack.build_stage_pack(
            wd=self.wd,
            repo_label="repo",
            stage="rules",
            batch=1,
            total_batches=1,
            items=["rules"],
            limits=agentpack.PackLimits(max_bytes=100_000, max_lines_per_file=40),
        )
        compact_len = agentpack.compact_json_size(pack)
        self.assertEqual(pack["metrics"]["bytes"], compact_len)
        self.assertEqual(pack["budget"]["budget_measured_as"], "compact-json")
        self.assertEqual(pack["budget"]["format"], "json-indent2")

    def test_forma_indentada_em_disco_e_maior_que_a_compacta_usada_no_budget(self):
        pack = agentpack.build_stage_pack(
            wd=self.wd,
            repo_label="repo",
            stage="rules",
            batch=1,
            total_batches=1,
            items=["rules"],
            limits=agentpack.PackLimits(max_bytes=100_000, max_lines_per_file=40),
        )
        compact_len = agentpack.compact_json_size(pack)

        out_path = os.path.join(self.wd, "agent-packs", "pack.json")
        agentpack.dump_compact_json(out_path, pack)
        with open(out_path, encoding="utf-8") as f:
            indented_bytes = len(f.read().encode("utf-8"))

        self.assertGreater(indented_bytes, compact_len)

    def test_budget_apertado_na_forma_compacta_ainda_cabe_o_conteudo(self):
        """Um max_bytes calibrado para a forma compacta nao deve descartar
        conteudo que so "estouraria" na forma indentada — prova de que o
        budget nao regrediu para medir a forma em disco."""
        pack_full = agentpack.build_stage_pack(
            wd=self.wd, repo_label="repo", stage="rules", batch=1, total_batches=1,
            items=["rules"], limits=agentpack.PackLimits(max_bytes=100_000, max_lines_per_file=40),
        )
        compact_len = agentpack.compact_json_size(pack_full)

        out_path = os.path.join(self.wd, "agent-packs", "pack.json")
        agentpack.dump_compact_json(out_path, pack_full)
        with open(out_path, encoding="utf-8") as f:
            indented_bytes = len(f.read().encode("utf-8"))

        # orcamento entre o tamanho compacto e o indentado: se o budget
        # medisse a forma indentada, o unico source seria descartado.
        self.assertLess(compact_len, indented_bytes)
        tight_limits = agentpack.PackLimits(max_bytes=compact_len, max_lines_per_file=40)
        pack_tight = agentpack.build_stage_pack(
            wd=self.wd, repo_label="repo", stage="rules", batch=1, total_batches=1,
            items=["rules"], limits=tight_limits,
        )
        self.assertEqual(len(pack_tight["sources"]), 1)
        self.assertEqual(pack_tight["metrics"]["dropped"], 0)


class ArchitectureDepsPriorityTest(unittest.TestCase):
    """FIX 2: dependencias obrigatorias (`sdd/domain.md`,
    `sdd/state-machines.md`, `sdd/permissions.md`) do estagio `architecture`
    tem prioridade de orcamento sobre `modules/*.md` — caso real:
    architecture-batch-01.json teve dropped:32 e NENHUM sdd/*.md em
    `sources`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = os.path.join(self.tmp.name, "wd")
        _write(os.path.join(self.wd, "sdd", "domain.md"), "# dominio\n" + ("regra de dominio critica\n" * 8))
        _write(os.path.join(self.wd, "sdd", "state-machines.md"), "# estados\n" + ("transicao de estado\n" * 8))
        _write(os.path.join(self.wd, "sdd", "permissions.md"), "# permissoes\n" + ("regra de permissao\n" * 8))
        for i in range(30):
            _write(
                os.path.join(self.wd, "modules", f"module-{i:02d}.md"),
                f"# modulo {i}\n" + "".join(f"linha {i}-{j} de evidencia do modulo\n" for j in range(25)),
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_sdd_deps_entram_mesmo_com_muitos_module_docs_concorrendo(self):
        limits = agentpack.PackLimits(max_bytes=3_000, max_lines_per_file=40)
        pack = agentpack.build_stage_pack(
            wd=self.wd, repo_label="repo", stage="architecture", batch=1, total_batches=1,
            items=["architecture"], limits=limits,
        )
        source_paths = {s["path"] for s in pack["sources"]}
        self.assertIn("sdd/domain.md", source_paths)
        self.assertIn("sdd/state-machines.md", source_paths)
        self.assertIn("sdd/permissions.md", source_paths)
        # o budget de 3_000 bytes com 30 module-docs concorrentes precisa
        # ter descartado module-docs — senao o teste nao exercita o caso real.
        self.assertGreater(pack["metrics"]["dropped"], 0)
        dropped_modules_glob = pack["metrics"]["dropped_by_glob"].get("modules/*.md", 0)
        self.assertGreater(dropped_modules_glob, 0)

    def test_specs_deps_tambem_tem_prioridade(self):
        _write(os.path.join(self.wd, "sdd", "architecture.md"), "# arquitetura\n" + ("decisao arquitetural\n" * 8))
        limits = agentpack.PackLimits(max_bytes=3_000, max_lines_per_file=40)
        pack = agentpack.build_stage_pack(
            wd=self.wd, repo_label="repo", stage="specs", batch=1, total_batches=1,
            items=["specs"], limits=limits,
        )
        source_paths = {s["path"] for s in pack["sources"]}
        self.assertIn("sdd/architecture.md", source_paths)
        self.assertIn("sdd/domain.md", source_paths)


class DuplicateContentDedupTest(unittest.TestCase):
    """FIX 3: arquivos com conteudo byte-identico sob nomes/slugs diferentes
    entram apenas uma vez no pack — caso real: `policy-service...kafka`
    entrou duplicado (2.881 chars) em 3 packs diferentes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = os.path.join(self.tmp.name, "wd")

    def tearDown(self):
        self.tmp.cleanup()

    def test_conteudo_identico_sob_nomes_diferentes_entra_uma_unica_vez(self):
        content = "# policy-service kafka\n" + ("mesmo conteudo byte-a-byte\n" * 10)
        _write(os.path.join(self.wd, "modules", "policy-service-src-main-kafka.md"), content)
        _write(os.path.join(self.wd, "modules", "policyservicesrcmainkafka.md"), content)
        _write(os.path.join(self.wd, "modules", "outro-modulo.md"), "# outro\nconteudo diferente\n")

        pack = agentpack.build_stage_pack(
            wd=self.wd, repo_label="repo", stage="rules", batch=1, total_batches=1,
            items=["rules"], limits=agentpack.PackLimits(max_bytes=100_000, max_lines_per_file=40),
        )

        source_paths = [s["path"] for s in pack["sources"]]
        self.assertEqual(len(source_paths), 2, source_paths)
        # a ocorrencia mantida e a primeira em ordem alfabetica de nome:
        # "policy-service-src-main-kafka.md" (com hifen) < "policyservicesrcmainkafka.md"
        self.assertIn("modules/policy-service-src-main-kafka.md", source_paths)
        self.assertNotIn("modules/policyservicesrcmainkafka.md", source_paths)
        self.assertIn("modules/outro-modulo.md", source_paths)

        self.assertEqual(pack["metrics"]["duplicates_dropped"], 1)
        dup_entries = [d for d in pack["dropped"] if d.get("reason") == "duplicate-content"]
        self.assertEqual(len(dup_entries), 1)
        self.assertEqual(dup_entries[0]["path"], "modules/policyservicesrcmainkafka.md")
        self.assertEqual(dup_entries[0]["duplicate_of"], "modules/policy-service-src-main-kafka.md")

    def test_conteudo_diferente_nao_e_deduplicado(self):
        _write(os.path.join(self.wd, "modules", "a.md"), "# a\nconteudo A\n")
        _write(os.path.join(self.wd, "modules", "b.md"), "# b\nconteudo B\n")

        pack = agentpack.build_stage_pack(
            wd=self.wd, repo_label="repo", stage="rules", batch=1, total_batches=1,
            items=["rules"], limits=agentpack.PackLimits(max_bytes=100_000, max_lines_per_file=40),
        )
        self.assertEqual(len(pack["sources"]), 2)
        self.assertEqual(pack["metrics"]["duplicates_dropped"], 0)


class MetricsExposeDroppedDetailTest(unittest.TestCase):
    """FIX 2/3: `metrics` do pack expõe `dropped_by_glob` (contagem de
    descartes por budget, por glob de origem) e `duplicates_dropped`
    (contagem de descartes por conteudo duplicado), para que um `dropped:32`
    deixe de ser opaco."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = os.path.join(self.tmp.name, "wd")
        _write(os.path.join(self.wd, "sdd", "domain.md"), "# dominio\n" + ("regra\n" * 6))
        _write(os.path.join(self.wd, "sdd", "state-machines.md"), "# estados\n" + ("estado\n" * 6))
        _write(os.path.join(self.wd, "sdd", "permissions.md"), "# permissoes\n" + ("permissao\n" * 6))
        dup_content = "# duplicado\n" + ("conteudo repetido\n" * 6)
        _write(os.path.join(self.wd, "modules", "dup-a.md"), dup_content)
        _write(os.path.join(self.wd, "modules", "dup-b.md"), dup_content)
        for i in range(20):
            _write(
                os.path.join(self.wd, "modules", f"unico-{i:02d}.md"),
                f"# modulo unico {i}\n" + "".join(f"linha {i}-{j}\n" for j in range(20)),
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_metrics_reporta_dropped_by_glob_e_duplicates_dropped(self):
        limits = agentpack.PackLimits(max_bytes=2_500, max_lines_per_file=40)
        pack = agentpack.build_stage_pack(
            wd=self.wd, repo_label="repo", stage="architecture", batch=1, total_batches=1,
            items=["architecture"], limits=limits,
        )
        metrics = pack["metrics"]
        self.assertIn("dropped_by_glob", metrics)
        self.assertIn("duplicates_dropped", metrics)
        self.assertIsInstance(metrics["dropped_by_glob"], dict)
        self.assertEqual(metrics["duplicates_dropped"], 1)
        # total dropped precisa cobrir tanto descartes por budget quanto por duplicidade
        self.assertEqual(
            metrics["dropped"],
            sum(metrics["dropped_by_glob"].values()) + metrics["duplicates_dropped"],
        )
        self.assertGreater(metrics["dropped_by_glob"].get("modules/*.md", 0), 0)


if __name__ == "__main__":
    unittest.main()
