"""Objetivos de DESCOBERTA: análise dirigida pelo modelo, sem depender de extrator.

Cada teste roda o pipeline inteiro (`snapshot.capture` -> `inventory.build` ->
`extract_all` -> `capabilities.discover` -> `investigation.plan`) sobre um
repositório sintético em `tempfile`, porque o defeito que estes testes fecham só
aparece com arquivo real em disco: `wk analyze --mode structural` sobre um repo
Go devolvia `succeeded/complete` com 0 objetivos, já que `plan()` só produzia
objetivo a partir de capacidades e órfãos — ambos derivados de extratores.
"""

import json
import os
import shutil
import tempfile
import unittest

from analysis import capabilities as cap_mod
from analysis import inventory as inv_mod
from analysis import snapshot as snap_mod
from analysis.extractors import registry as ext_registry
from analysis.extractors.base import SourceFile
from analysis.investigation import (
    ANALYSIS_DIRECTIVES,
    DISCOVERY_DIRECTIVES,
    InvestigationObjective,
    ObjectiveKind,
    PlanAccountingError,
    ReadingTrigger,
    assert_plan_accounted,
    objectives_from_dict,
    objectives_to_dict,
    plan,
    plan_accounting,
    plan_coverage,
)
from analysis.profile import AnalysisProfile

_KEEP = None  # preenchido em setUpModule (evita import circular no topo)


def setUpModule():
    global _KEEP
    _KEEP = {
        inv_mod.FileClass.CODE,
        inv_mod.FileClass.CONFIG,
        inv_mod.FileClass.MANIFEST,
        inv_mod.FileClass.TEST,
        inv_mod.FileClass.MIGRATION,
    }


class PipelineMixin:
    """Mesma sequência que `wk analyze` executa (cli.py `_analyze_repo`)."""

    def make_repo(self, files, prefix="wikiai_disc_"):
        repo = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, repo, True)
        for rel, body in files.items():
            full = os.path.join(repo, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(body)
        return repo

    def pipeline(self, repo, *, profile=None, with_inventory=True, **plan_kw):
        snapshot = snap_mod.capture(repo)
        inventory = inv_mod.build(snapshot)
        sources = []
        for fc in inventory.files:
            if fc.file_class not in _KEEP:
                continue
            full = os.path.join(repo, *fc.path.split("/"))
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                sources.append(
                    SourceFile(
                        path=fc.path,
                        content=fh.read(),
                        language=(fc.language.value if fc.language else ""),
                    )
                )
        extraction = ext_registry.default_registry().extract_all(sources)
        cmap = cap_mod.discover(extraction, inventory, snapshot=snapshot)
        objectives = plan(
            cmap,
            extraction,
            inventory=inventory if with_inventory else None,
            snapshot=snapshot,
            profile=profile,
            **plan_kw,
        )
        return {
            "snapshot": snapshot,
            "inventory": inventory,
            "extraction": extraction,
            "cmap": cmap,
            "objectives": objectives,
        }


GO_REPO = {
    "go.mod": "module example.com/shop\n\ngo 1.22\n",
    "cmd/api/main.go": (
        "package main\n"
        "\n"
        "import (\n"
        '\t"net/http"\n'
        '\t"example.com/shop/internal/orders"\n'
        ")\n"
        "\n"
        "func main() {\n"
        '\thttp.HandleFunc("/orders", orders.Handle)\n'
        '\thttp.ListenAndServe(":8080", nil)\n'
        "}\n"
    ),
    "internal/orders/orders.go": (
        "package orders\n"
        "\n"
        'import "net/http"\n'
        "\n"
        "func Handle(w http.ResponseWriter, r *http.Request) {\n"
        "\tif r.Method != http.MethodPost {\n"
        "\t\tw.WriteHeader(405)\n"
        "\t\treturn\n"
        "\t}\n"
        "\tw.WriteHeader(201)\n"
        "}\n"
    ),
}


# --------------------------------------------------------------------------
# 1) O defeito reproduzido: repo Go
# --------------------------------------------------------------------------


class TestGoRepoProducesDiscovery(PipelineMixin, unittest.TestCase):
    def setUp(self):
        self.repo = self.make_repo(GO_REPO, prefix="wikiai_go_")
        self.out = self.pipeline(self.repo)

    def test_no_capability_and_no_orphan_exists(self):
        """A premissa do defeito: extrator nenhum reivindica `.go`."""
        self.assertEqual(list(self.out["cmap"].capabilities), [])
        self.assertEqual(list(self.out["cmap"].orphans), [])

    def test_discovery_objectives_replace_the_zero(self):
        objs = self.out["objectives"]
        self.assertTrue(objs, "repo Go não pode produzir plano vazio")
        kinds = {o.kind for o in objs}
        self.assertEqual(kinds, {ObjectiveKind.DISCOVERY})
        self.assertEqual(
            sorted(o.name for o in objs),
            ["discovery@cmd/api", "discovery@internal/orders"],
        )

    def test_reading_need_points_to_whole_file(self):
        need = [
            n
            for o in self.out["objectives"]
            for n in o.reading_needs
            if n.target.startswith("internal/orders/orders.go")
        ]
        self.assertEqual(len(need), 1)
        (need,) = need
        self.assertEqual(need.trigger, ReadingTrigger.SOURCE_FILE)
        self.assertEqual(need.priority, 5, "source_file é prioridade máxima")
        self.assertEqual(need.target, "internal/orders/orders.go:1-11")
        self.assertIsNotNone(need.evidence)
        self.assertEqual(need.evidence.line_start, 1)
        self.assertEqual(need.evidence.line_end, 11)
        self.assertIsNotNone(
            need.evidence.locator, "faixa integral deve sair com localizador validado"
        )
        self.assertIn("linguagem go sem extrator — leitura direta", need.motivo)

    def test_objective_id_is_stable_and_derived_from_module(self):
        from analysis.capabilities import fingerprint

        obj = [o for o in self.out["objectives"] if o.name == "discovery@cmd/api"][0]
        self.assertEqual(
            obj.objective_id, "obj_" + fingerprint(["discovery", "cmd/api"])[:24]
        )
        again = self.pipeline(self.repo)["objectives"]
        self.assertEqual(
            [o.objective_id for o in self.out["objectives"]],
            [o.objective_id for o in again],
        )

    def test_matrix_and_contract_are_the_same_as_any_objective(self):
        from analysis.investigation import CONTRACT_FIELDS, FAILURE_FAMILIES

        obj = self.out["objectives"][0]
        self.assertEqual(sorted(obj.contract), sorted(CONTRACT_FIELDS))
        self.assertEqual(
            obj.matrix.summary()["total"],
            sum(len(v) for v in FAILURE_FAMILIES.values()),
        )

    def test_profile_can_still_exclude_a_failure_family(self):
        prof = AnalysisProfile.from_mapping(
            {"failure_families_excluded": {"mensageria": "sem fila neste sistema"}},
            source="cli",
        )
        obj = self.pipeline(self.repo, profile=prof)["objectives"][0]
        self.assertEqual(obj.matrix.state_of("mensageria", "duplicidade").value, "not_applicable")

    def test_identidade_prefilled_with_module_and_dependencias_unresolved(self):
        obj = [o for o in self.out["objectives"] if o.name == "discovery@cmd/api"][0]
        self.assertEqual(obj.field("identidade").status.value, "filled")
        self.assertIn("cmd/api", obj.field("identidade").content)
        self.assertEqual(obj.field("dependencias").status.value, "unresolved")
        self.assertIn("go", obj.field("dependencias").impacto)

    def test_evaluate_still_partial_until_agent_covers_everything(self):
        obj = self.out["objectives"][0]
        self.assertTrue(obj.unmet_obligations())
        self.assertEqual(obj.evaluate().value, "partial")

    def test_coverage_is_closed_by_construction(self):
        cov = plan_coverage(self.out["objectives"], self.out["inventory"])
        self.assertEqual(cov["files_uncovered"], [])
        self.assertEqual(cov["files_covered"], cov["files_total"])
        assert_plan_accounted(
            self.out["objectives"], self.out["cmap"], inventory=self.out["inventory"]
        )

    def test_accounting_names_the_gap(self):
        acc = plan_accounting(
            self.out["objectives"],
            self.out["cmap"],
            extraction=self.out["extraction"],
            inventory=self.out["inventory"],
        )
        self.assertEqual(acc["discovery_objectives"], 2)
        self.assertEqual(acc["discovery_files"], 2)
        self.assertEqual(acc["files_without_extractor"]["go"], 3)
        self.assertEqual(acc["files_uncovered"], 0)
        self.assertEqual(acc["files_total"], 2)


# --------------------------------------------------------------------------
# 2) Repo misto: capacidade para Python, descoberta para Go
# --------------------------------------------------------------------------


class TestMixedRepo(PipelineMixin, unittest.TestCase):
    def setUp(self):
        files = dict(GO_REPO)
        files["svc/app.py"] = (
            "from flask import Flask\n"
            "\n"
            "app = Flask(__name__)\n"
            "\n"
            "\n"
            '@app.route("/pay", methods=["POST"])\n'
            "def pay():\n"
            "    return charge(1)\n"
            "\n"
            "\n"
            "def charge(amount):\n"
            "    if amount <= 0:\n"
            "        raise ValueError(amount)\n"
            "    return amount\n"
        )
        self.repo = self.make_repo(files, prefix="wikiai_mix_")
        self.out = self.pipeline(self.repo)

    def test_python_still_produces_capability(self):
        self.assertTrue(self.out["cmap"].capabilities)
        kinds = {o.kind for o in self.out["objectives"]}
        self.assertIn(ObjectiveKind.CAPABILITY, kinds)
        self.assertIn(ObjectiveKind.DISCOVERY, kinds)

    def test_discovery_only_for_go_files(self):
        discovery_paths = sorted(
            n.target.split(":")[0]
            for o in self.out["objectives"]
            if o.kind is ObjectiveKind.DISCOVERY
            for n in o.reading_needs
        )
        self.assertEqual(
            discovery_paths, ["cmd/api/main.go", "internal/orders/orders.go"]
        )
        self.assertNotIn("svc/app.py", discovery_paths)

    def test_every_code_file_is_covered(self):
        cov = plan_coverage(self.out["objectives"], self.out["inventory"])
        self.assertEqual(cov["files_uncovered"], [])
        self.assertEqual(cov["files_total"], 3)


# --------------------------------------------------------------------------
# 3) Arquivo Python SEM símbolo extraído
# --------------------------------------------------------------------------


class TestEmptyExtractionFile(PipelineMixin, unittest.TestCase):
    def test_python_file_without_symbols_gets_discovery(self):
        repo = self.make_repo(
            {
                "svc/app.py": (
                    "from flask import Flask\n"
                    "\n"
                    "app = Flask(__name__)\n"
                    "\n"
                    "\n"
                    '@app.route("/pay")\n'
                    "def pay():\n"
                    "    return 1\n"
                ),
                "svc/constants.ini": "[a]\nb = 1\n",
                "data/rules.jsonc": "{\n  \"limite\": 10\n}\n",
            },
            prefix="wikiai_empty_",
        )
        out = self.pipeline(repo)
        cov = plan_coverage(out["objectives"], out["inventory"])
        self.assertEqual(cov["files_uncovered"], [])
        motivos = [
            n.motivo
            for o in out["objectives"]
            if o.kind is ObjectiveKind.DISCOVERY
            for n in o.reading_needs
        ]
        self.assertTrue(motivos, "arquivo não alcançado precisa virar descoberta")

    def test_reason_no_symbols_is_used_when_extractor_produced_nothing(self):
        from analysis.investigation import _discovery_reason

        key, razao = _discovery_reason("a/b.json", {}, {"a/b.json"}, {"a/b.json": "json"})
        self.assertEqual(key, "files_without_symbols")
        self.assertEqual(razao, "arquivo sem símbolos extraídos")


# --------------------------------------------------------------------------
# 4) Filtro por objetivo, round-trip, particionamento
# --------------------------------------------------------------------------


class TestFilterRoundTripPartition(PipelineMixin, unittest.TestCase):
    def setUp(self):
        self.repo = self.make_repo(GO_REPO, prefix="wikiai_filt_")

    def test_objective_filter_by_module_substring(self):
        prof = AnalysisProfile.from_mapping({"objectives": ["internal/orders"]}, source="cli")
        objs = self.pipeline(self.repo, profile=prof)["objectives"]
        self.assertEqual([o.name for o in objs], ["discovery@internal/orders"])

    def test_objective_filter_by_exact_id(self):
        full = self.pipeline(self.repo)["objectives"]
        target = full[0]
        prof = AnalysisProfile.from_mapping({"objectives": [target.objective_id]}, source="cli")
        objs = self.pipeline(self.repo, profile=prof)["objectives"]
        self.assertEqual([o.objective_id for o in objs], [target.objective_id])

    def test_filter_does_not_break_accounting_but_is_counted(self):
        prof = AnalysisProfile.from_mapping({"objectives": ["internal/orders"]}, source="cli")
        out = self.pipeline(self.repo, profile=prof)
        assert_plan_accounted(
            out["objectives"], out["cmap"], profile=prof, inventory=out["inventory"]
        )
        acc = plan_accounting(
            out["objectives"],
            out["cmap"],
            profile=prof,
            extraction=out["extraction"],
            inventory=out["inventory"],
        )
        self.assertEqual(acc["files_uncovered"], 1)
        self.assertEqual(acc["files_uncovered_suppressed_by_profile"], 1)

    def test_round_trip_preserves_discovery(self):
        objs = self.pipeline(self.repo)["objectives"]
        payload = objectives_to_dict(objs)
        back = objectives_from_dict(payload, trust_profile_cells=True)
        self.assertEqual(
            json.dumps(objectives_to_dict(back), sort_keys=True),
            json.dumps(payload, sort_keys=True),
        )
        self.assertEqual([o.kind for o in back], [ObjectiveKind.DISCOVERY] * len(objs))
        self.assertEqual(payload[0]["kind"], "discovery")

    def test_discovery_max_files_per_objective_partitions(self):
        files = {
            f"pkg/mod_{i}.go": f"package mod\n\nfunc F{i}() int {{ return {i} }}\n"
            for i in range(5)
        }
        repo = self.make_repo(files, prefix="wikiai_part_")
        base = self.pipeline(repo)["objectives"]
        self.assertEqual(len(base), 1)
        self.assertEqual(len(base[0].reading_needs), 5)

        prof = AnalysisProfile.from_mapping(
            {"discovery_max_files_per_objective": 2}, source="cli"
        )
        out = self.pipeline(repo, profile=prof)
        objs = out["objectives"]
        self.assertEqual([len(o.reading_needs) for o in objs], [2, 2, 1])
        self.assertEqual(
            [o.name for o in objs], ["discovery@pkg", "discovery@pkg#2", "discovery@pkg#3"]
        )
        self.assertEqual(
            plan_coverage(objs, out["inventory"])["files_uncovered"],
            [],
            "particionar não pode perder arquivo",
        )

    def test_repo_layer_accepts_the_key(self):
        prof = AnalysisProfile.from_mapping(
            {"discovery_max_files_per_objective": 3}, source="repo:.wiki-ai.json"
        )
        self.assertEqual(prof.discovery_max_files_per_objective, 3)
        self.assertEqual(prof.to_dict()["discovery_max_files_per_objective"], 3)

    def test_key_must_be_at_least_one(self):
        from analysis.profile import ProfileError

        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping(
                {"discovery_max_files_per_objective": 0}, source="cli"
            )


# --------------------------------------------------------------------------
# 5) Compat: repo totalmente coberto por capacidades/órfãos
# --------------------------------------------------------------------------


PY_ONLY_REPO = {
    "svc/app.py": (
        "from flask import Flask\n"
        "\n"
        "app = Flask(__name__)\n"
        "\n"
        "\n"
        '@app.route("/pay", methods=["POST"])\n'
        "def pay():\n"
        "    return charge(1)\n"
        "\n"
        "\n"
        "def charge(amount):\n"
        "    if amount <= 0:\n"
        "        raise ValueError(amount)\n"
        "    return amount\n"
    ),
}


class TestCompatFullyExtractedRepo(PipelineMixin, unittest.TestCase):
    def test_no_discovery_and_identical_payload(self):
        repo = self.make_repo(PY_ONLY_REPO, prefix="wikiai_compat_")
        out = self.pipeline(repo)
        objs = out["objectives"]
        self.assertTrue(objs)
        self.assertEqual(
            [o for o in objs if o.kind is ObjectiveKind.DISCOVERY],
            [],
            "repo totalmente coberto por capacidade não abre descoberta",
        )
        without_inventory = self.pipeline(repo, with_inventory=False)["objectives"]
        # `inventory=None` desliga a descoberta; o payload dos objetivos de
        # capacidade/órfão é byte a byte o mesmo dos dois lados.
        self.assertEqual(
            json.dumps(objectives_to_dict(objs), sort_keys=True),
            json.dumps(objectives_to_dict(without_inventory), sort_keys=True),
        )

    def test_uncovered_file_raises_when_discovery_is_off(self):
        repo = self.make_repo(GO_REPO, prefix="wikiai_off_")
        out = self.pipeline(repo, with_inventory=False)
        self.assertEqual(out["objectives"], [])
        with self.assertRaises(PlanAccountingError):
            assert_plan_accounted(
                out["objectives"], out["cmap"], inventory=out["inventory"]
            )


# --------------------------------------------------------------------------
# 6) Inventário agnóstico de linguagem
# --------------------------------------------------------------------------


POLYGLOT_REPO = {
    "cobol/PAYROLL.cbl": (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. PAYROLL.\n"
        "       PROCEDURE DIVISION.\n"
        "           IF WS-SALARIO > 5000\n"
        "              COMPUTE WS-IMPOSTO = WS-SALARIO * 0.275\n"
        "           END-IF.\n"
    ),
    "cobol/COPY01.cpy": "       01 WS-SALARIO PIC 9(7)V99.\n",
    "jcl/RUNPAY.jcl": "//RUNPAY  JOB (ACCT),'PAYROLL'\n//STEP1   EXEC PGM=PAYROLL\n",
    "pli/CALC.pli": "CALC: PROCEDURE OPTIONS(MAIN);\n  PUT LIST('x');\nEND CALC;\n",
    "asm/IEFBR14.asm": "IEFBR14  CSECT\n         BR    14\n         END\n",
    "sybase/sp_pay.prc": "create procedure sp_pay as select 1\ngo\n",
    "sybase/trg_pay.trg": "create trigger trg_pay on pay for insert as select 1\ngo\n",
    "cpp/engine.cpp": "int compute(int a) {\n  if (a < 0) return 0;\n  return a * 2;\n}\n",
    "cpp/engine.hpp": "int compute(int a);\n",
    "rust/lib.rs": "pub fn compute(a: i32) -> i32 { if a < 0 { 0 } else { a * 2 } }\n",
    "go/main.go": "package main\n\nfunc main() {}\n",
    "delphi/Unit1.pas": "unit Unit1;\ninterface\nimplementation\nend.\n",
    "abap/zreport.abap": "REPORT zreport.\nWRITE 'x'.\n",
    "vb/Module1.bas": "Sub Main()\nEnd Sub\n",
    "shell/run.sh": "#!/bin/sh\nset -e\necho ok\n",
    "misc/weird.qqq": "regra: se x > 10 entao rejeita\n",
    "docs/README.md": "# doc\n",
    "LICENSE": "MIT\n",
}


class TestPolyglotInventory(PipelineMixin, unittest.TestCase):
    def setUp(self):
        self.repo = self.make_repo(POLYGLOT_REPO, prefix="wikiai_poly_")
        self.out = self.pipeline(self.repo)
        self.by_path = {f.path: f for f in self.out["inventory"].files}

    def test_every_source_file_is_code_with_a_language(self):
        expected = {
            "cobol/PAYROLL.cbl": "cobol",
            "cobol/COPY01.cpy": "cobol",
            "jcl/RUNPAY.jcl": "jcl",
            "pli/CALC.pli": "pli",
            "asm/IEFBR14.asm": "assembler",
            "sybase/sp_pay.prc": "sql",
            "sybase/trg_pay.trg": "sql",
            "cpp/engine.cpp": "cpp",
            "cpp/engine.hpp": "cpp",
            "rust/lib.rs": "rust",
            "go/main.go": "go",
            "delphi/Unit1.pas": "pascal",
            "abap/zreport.abap": "abap",
            "vb/Module1.bas": "vbnet",
            "shell/run.sh": "shell",
            "misc/weird.qqq": "unknown",
        }
        for path, lang in expected.items():
            with self.subTest(path=path):
                fc = self.by_path[path]
                self.assertEqual(fc.file_class, inv_mod.FileClass.CODE)
                self.assertIsNotNone(fc.language)
                self.assertEqual(fc.language.value, lang)

    def test_documents_are_not_code(self):
        self.assertEqual(self.by_path["docs/README.md"].file_class, inv_mod.FileClass.DOC)
        self.assertEqual(self.by_path["LICENSE"].file_class, inv_mod.FileClass.DOC)

    def test_unknown_language_is_code_and_still_a_declared_limitation(self):
        self.assertEqual(
            self.by_path["misc/weird.qqq"].language, inv_mod.Language.UNKNOWN
        )
        self.assertTrue(
            any(l.path == "misc/weird.qqq" for l in self.out["inventory"].limitations),
            "linguagem não reconhecida continua sendo limitação registrada",
        )

    def test_nothing_is_dropped_silently(self):
        summary = self.out["inventory"].summary
        self.assertEqual(summary["by_class"]["code"], 16)
        self.assertEqual(summary["exclusions_count"], len(self.out["inventory"].exclusions))
        cov = plan_coverage(self.out["objectives"], self.out["inventory"])
        self.assertEqual(cov["files_total"], 16)
        self.assertEqual(cov["files_uncovered"], [])

    def test_extraction_exposes_the_languages_without_adapter(self):
        without = self.out["extraction"].files_without_adapter
        for lang in ("cobol", "jcl", "pli", "assembler", "cpp", "rust", "go", "pascal", "abap"):
            with self.subTest(lang=lang):
                self.assertIn(lang, without)
        as_dict = self.out["extraction"].as_dict()
        self.assertIn("files_without_adapter", as_dict)
        self.assertIn("files_without_symbols", as_dict)

    def test_accounting_reports_per_language(self):
        acc = plan_accounting(
            self.out["objectives"],
            self.out["cmap"],
            extraction=self.out["extraction"],
            inventory=self.out["inventory"],
        )
        self.assertEqual(acc["files_without_extractor"]["cobol"], 2)
        self.assertEqual(acc["files_without_extractor"]["jcl"], 1)
        self.assertEqual(acc["files_uncovered"], 0)
        assert_plan_accounted(
            self.out["objectives"], self.out["cmap"], inventory=self.out["inventory"]
        )


# --------------------------------------------------------------------------
# 7) Diretivas de análise dentro do objetivo serializado
# --------------------------------------------------------------------------


class TestAnalysisDirectives(PipelineMixin, unittest.TestCase):
    def test_all_objective_kinds_carry_the_directives(self):
        files = dict(GO_REPO)
        files.update(PY_ONLY_REPO)
        files["svc/orphan.py"] = "def solto():\n    return 1\n"
        repo = self.make_repo(files, prefix="wikiai_dir_")
        objs = self.pipeline(repo)["objectives"]
        kinds = {o.kind for o in objs}
        self.assertIn(ObjectiveKind.CAPABILITY, kinds)
        self.assertIn(ObjectiveKind.DISCOVERY, kinds)
        for obj in objs:
            with self.subTest(kind=obj.kind.value, name=obj.name):
                payload = obj.to_dict()
                self.assertIn("analysis_directives", payload)
                texto = " ".join(payload["analysis_directives"])
                self.assertIn("regras de negócio", texto)
                self.assertIn("invariantes", texto)
                self.assertIn("edge cases", texto)
                self.assertIn("CÓDIGO EXECUTÁVEL", texto)
                self.assertIn("docstrings", texto)
                self.assertIn("NÃO são evidência", texto)

    def test_discovery_adds_its_own_directives(self):
        repo = self.make_repo(GO_REPO, prefix="wikiai_dir2_")
        obj = self.pipeline(repo)["objectives"][0]
        self.assertEqual(
            obj.analysis_directives, list(ANALYSIS_DIRECTIVES) + list(DISCOVERY_DIRECTIVES)
        )
        self.assertIn("DESCOBERTA", " ".join(obj.analysis_directives))

    def test_directives_survive_round_trip(self):
        repo = self.make_repo(GO_REPO, prefix="wikiai_dir3_")
        objs = self.pipeline(repo)["objectives"]
        back = objectives_from_dict(objectives_to_dict(objs), trust_profile_cells=True)
        self.assertEqual(back[0].analysis_directives, objs[0].analysis_directives)

    def test_default_when_payload_omits_the_field(self):
        payload = InvestigationObjective(
            objective_id="obj_x", kind=ObjectiveKind.CAPABILITY, capability_id="c", name="n"
        ).to_dict()
        payload.pop("analysis_directives")
        back = InvestigationObjective.from_dict(payload)
        self.assertEqual(back.analysis_directives, list(ANALYSIS_DIRECTIVES))


# --------------------------------------------------------------------------
# 8) Orçamento particiona em vez de descartar
# --------------------------------------------------------------------------


class TestBudgetPartitionsInsteadOfDropping(PipelineMixin, unittest.TestCase):
    def setUp(self):
        files = dict(PY_ONLY_REPO)
        files["svc/extra.py"] = (
            "from svc.app import charge\n"
            "\n"
            "\n"
            "def reembolsa(x):\n"
            "    return charge(x)\n"
        )
        self.repo = self.make_repo(files, prefix="wikiai_budget_")

    def test_with_inventory_needs_are_deferred_not_dropped(self):
        out = self.pipeline(self.repo, max_reading_needs=1)
        caps = [o for o in out["objectives"] if o.kind is ObjectiveKind.CAPABILITY]
        self.assertTrue(caps)
        self.assertEqual(sum(o.accounting["reading_needs_dropped"] for o in caps), 0)
        self.assertGreater(sum(o.accounting["reading_needs_deferred"] for o in caps), 0)
        for obj in caps:
            self.assertFalse(
                any("descartada(s) por orçamento" in u for u in obj.unmet_obligations()),
                "obrigação diferida não pode bloquear complete como descarte",
            )
        self.assertEqual(
            plan_coverage(out["objectives"], out["inventory"])["files_uncovered"], []
        )

    def test_without_inventory_the_historic_drop_is_preserved(self):
        out = self.pipeline(self.repo, max_reading_needs=1, with_inventory=False)
        caps = [o for o in out["objectives"] if o.kind is ObjectiveKind.CAPABILITY]
        self.assertGreater(sum(o.accounting["reading_needs_dropped"] for o in caps), 0)
        self.assertTrue(
            any(
                "descartada(s) por orçamento" in u
                for o in caps
                for u in o.unmet_obligations()
            )
        )


if __name__ == "__main__":
    unittest.main()
