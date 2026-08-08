"""Testes do lote A: FIX 1 (path duplicado por --store relativo) e
FIX 2 (tupla de estágios duplicada / erro compreensível p/ 'evidence')."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import unittest

from codescan import cli as cli_mod
from codescan import export as ex_mod
from codescan import sdd as sdd_mod
from codescan import state as st_mod
from codescan.cli import main


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _run(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def _chdir(path: str):
    """Isola o CWD do processo: --store relativo é resolvido contra ele, e
    testes não podem depender/afetar o diretório de trabalho real do runner."""
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _module_body(name: str, citation_base: str = "src/domain/Order.java") -> str:
    return "\n".join(
        [
            f"# {name}",
            "",
            "## Responsabilidade",
            f"- 🟢 `{name}` coordena entrada, processo e saída do fluxo principal. "
            f"{citation_base}:10",
            "",
            "## Estruturas de dados",
            f"- 🟢 `{name}Request` carrega os campos `customerId`, `amount` e `status`. "
            f"{citation_base}:12",
            f"- 🟢 `record {name}Record(String customerId, BigDecimal amount, String status)` "
            f"define entidade/tipo de dados rastreável. {citation_base}:14",
            "",
            "## Fluxos",
            f"- 🟢 Entrada validada chega ao serviço, atualiza estado e produz saída operacional. "
            f"{citation_base}:20",
            "",
            "## Dependências",
            f"- 🟢 `{name}Repository` persiste estado e integra com o módulo de domínio. "
            f"{citation_base}:30",
            "",
            "## Rastreabilidade",
            f"- 🟢 Evidência principal em `{citation_base}:10`.",
            "",
            "## Lacunas",
            "- 🟡 Não há lacuna crítica no recorte consolidado.",
        ]
    )


# ---------------------------------------------------------------------------
# FIX 1: --store relativo não pode produzir workdir/paths ambíguos.
# ---------------------------------------------------------------------------
class RelativeStoreWorkdirTest(unittest.TestCase):
    def test_relative_store_resolves_to_absolute_workdir(self) -> None:
        """`--store store` (relativo) precisa produzir um `workdir` absoluto
        na saída de `surface` — sem isso, todo caminho derivado de `wd` via
        os.path.join(wd, ...) nasce relativo e vira ambíguo para
        sdd._resolve_manifest_path (que só reconhece "já absoluto" via
        os.path.isabs)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(os.path.join(repo, "src", "a.py"), "def f():\n    return 1\n")

            with _chdir(tmp):
                code, out, err = _run(["--store", "store", "--repo", repo, "surface"])

            self.assertEqual(code, 0, err)
            result = json.loads(out)
            self.assertTrue(
                os.path.isabs(result["workdir"]),
                f"workdir deveria ser absoluto, veio: {result['workdir']!r}",
            )
            self.assertTrue(
                os.path.isabs(result["artifact"]),
                f"artifact deveria ser absoluto, veio: {result['artifact']!r}",
            )
            expected_prefix = os.path.join(tmp, "store", ".codescan")
            self.assertTrue(
                result["workdir"].startswith(expected_prefix),
                f"workdir {result['workdir']!r} deveria começar com {expected_prefix!r}",
            )
            # Sem duplicação do segmento "store" no meio do caminho.
            self.assertEqual(
                1,
                result["workdir"].replace("\\", "/").count("/store/"),
                f"workdir com prefixo duplicado: {result['workdir']!r}",
            )

    def test_relative_and_absolute_store_resolve_to_same_workdir(self) -> None:
        """O mesmo store, passado relativo ou absoluto, deve produzir
        exatamente o mesmo workdir — provando que a normalização não
        introduz uma segunda chave de estado para o mesmo lugar."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(os.path.join(repo, "src", "a.py"), "def f():\n    return 1\n")

            with _chdir(tmp):
                code, out_rel, err = _run(["--store", "store", "--repo", repo, "surface"])
            self.assertEqual(code, 0, err)

            store_abs = os.path.join(tmp, "store")
            code, out_abs, err = _run(["--store", store_abs, "--repo", repo, "surface"])
            self.assertEqual(code, 0, err)

            self.assertEqual(
                json.loads(out_rel)["workdir"],
                json.loads(out_abs)["workdir"],
            )

    def test_relative_store_end_to_end_merge_and_audit_no_duplicate_path(self) -> None:
        """Regressão direta do bug relatado: com --store relativo, roda
        merge-agent-output e confirma que o manifesto agent-runs/modules.json
        grava um `path` absoluto e resolvível — sem isso, sdd.audit()
        reportava P0 'artefato ausente após o merge' mesmo com o arquivo
        existindo em disco (era lido em <wd>/<wd relativo>/modules/x.md)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)

            with _chdir(tmp):
                store = "store"
                wd = st_mod.workdir(os.path.abspath(store), repo)
                inp = os.path.join(tmp, "agent.txt")
                _write(inp, f"=== MODULE: src/orders ===\n{_module_body('Order')}\n=== END ===\n")

                code, out, err = _run([
                    "--store", store, "--repo", repo,
                    "merge-agent-output", "modules", "--input", inp, "--agent", "modules-b01",
                ])
                self.assertEqual(code, 0, err)

                manifest_path = os.path.join(wd, "agent-runs", "modules.json")
                self.assertTrue(os.path.isfile(manifest_path), manifest_path)
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
                recorded_path = manifest["runs"][0]["items"][0]["artifacts"][0]["path"]
                self.assertTrue(
                    os.path.isabs(recorded_path),
                    f"path gravado no manifesto deveria ser absoluto: {recorded_path!r}",
                )
                self.assertTrue(
                    os.path.isfile(recorded_path),
                    f"path gravado no manifesto não existe: {recorded_path!r}",
                )
                # Nenhuma duplicação do segmento do workdir dentro do próprio valor.
                wd_norm = wd.replace("\\", "/")
                self.assertEqual(
                    1,
                    recorded_path.replace("\\", "/").count(wd_norm),
                    f"path com workdir duplicado: {recorded_path!r} (wd={wd_norm!r})",
                )

                state = st_mod.load(wd) or {}
                report = sdd_mod.audit(wd, "modules", state)
                blockers_text = json.dumps(report, ensure_ascii=False)
                self.assertNotIn("artefato ausente após o merge", blockers_text)
                self.assertNotIn("artefato alterado após o merge", blockers_text)


# ---------------------------------------------------------------------------
# FIX 2: choices de `stage` reusam sdd_mod.CRITICAL_STAGES; 'evidence' vira
# erro compreensível (não crash de argparse) nos 4 subcomandos afetados.
# ---------------------------------------------------------------------------
STAGE_SUBCOMMANDS_MIN_ARGS = {
    "run-stage": [],
    "merge-agent-output": ["--input", "x.txt", "--agent", "modules-b01"],
    "agent-pack": ["--batch", "1"],
    "redo": [],
}


class StageChoicesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(self.repo, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_evidence_stage_returns_actionable_error_not_argparse_crash(self) -> None:
        for subcmd, extra in STAGE_SUBCOMMANDS_MIN_ARGS.items():
            with self.subTest(subcmd=subcmd):
                code, out, err = _run([
                    "--store", self.store, "--repo", self.repo, subcmd, "evidence", *extra,
                ])
                self.assertEqual(code, 2)
                self.assertEqual(out, "", f"{subcmd}: stdout deveria ficar vazio em erro")
                # Precisa ser JSON válido de uma linha, não traceback do argparse.
                payload = json.loads(err)
                self.assertIn("error", payload)
                self.assertIn("evidence", payload["error"])
                self.assertIn(subcmd, payload["error"])
                self.assertIn("acao", payload)
                self.assertIn("evidence --topic", payload["acao"])

    def test_stage_choices_match_critical_stages_via_argparse_error(self) -> None:
        """Provoca o erro nativo de `choices` do argparse (stage inválido que
        não seja 'evidence') e verifica que a lista de escolhas aceitas é
        exatamente sdd_mod.CRITICAL_STAGES — prova que os 4 subcomandos usam
        a mesma fonte, não uma tupla literal divergente."""
        expected = list(sdd_mod.CRITICAL_STAGES)
        for subcmd, extra in STAGE_SUBCOMMANDS_MIN_ARGS.items():
            with self.subTest(subcmd=subcmd):
                code, out, err = _run([
                    "--store", self.store, "--repo", self.repo, subcmd, "bogus-stage", *extra,
                ])
                self.assertEqual(code, 2)
                payload = json.loads(err)
                m = re.search(r"\(choose from ([^)]+)\)", payload["error"])
                self.assertIsNotNone(m, payload["error"])
                choices = [c.strip() for c in m.group(1).split(",")]
                self.assertEqual(expected, choices)

    def test_all_critical_stages_accepted_as_stage_argument(self) -> None:
        """Todo valor de CRITICAL_STAGES precisa passar da validação de
        `choices` do argparse (não necessariamente terminar em sucesso —
        alguns exigem estado prévio — mas não pode ser 'invalid choice')."""
        for subcmd, extra in STAGE_SUBCOMMANDS_MIN_ARGS.items():
            for stage in sdd_mod.CRITICAL_STAGES:
                with self.subTest(subcmd=subcmd, stage=stage):
                    code, _out, err = _run([
                        "--store", self.store, "--repo", self.repo, subcmd, stage, *extra,
                    ])
                    self.assertNotIn("invalid choice", err)

    def test_sdd_brief_still_accepts_evidence(self) -> None:
        """`evidence` continua válido em sdd-brief (BRIEF_STAGES) — a
        restrição do FIX 2 é só para os 4 subcomandos orientados a fan-out
        por subagente, não para o contrato determinístico."""
        code, out, err = _run(["--store", self.store, "--repo", self.repo, "sdd-brief", "evidence"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload.get("stage"), "evidence")


# ---------------------------------------------------------------------------
# BUG B3: `redo` normaliza separador de path de forma inconsistente.
# ---------------------------------------------------------------------------
def _nested_module_dir(repo: str) -> str:
    """Cria um módulo com path aninhado (>=2 segmentos) — só esses expõem o
    bug: `os.path.relpath` em Windows grava '\\' entre segmentos; um path de
    1 segmento (ex.: 'src') não tem separador nenhum para divergir."""
    d = os.path.join(repo, "quote-service", "src", "main", "java", "application", "cache")
    os.makedirs(d, exist_ok=True)
    for i in range(4):
        _write(os.path.join(d, f"F{i}.java"), "class X {}\n" * 20)
    return d


def _module_merge_input(item_path: str) -> str:
    return "\n".join([
        f"=== MODULE: {item_path} ===",
        "# Cache",
        "",
        "## Responsabilidade",
        "- ok",
        "=== END ===",
        "",
    ])


class RedoPathSeparatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        _nested_module_dir(self.repo)
        self.wd = None  # preenchido depois de `surface`

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, *args]

    def _bootstrap(self) -> str:
        """surface -> config -> plan; devolve o item nativo do único módulo
        (com separador de SO — '\\' no Windows para o path aninhado)."""
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "essencial", "--granularity", "module"))
        self.assertEqual(code, 0, err)
        code, out, err = _run(self._argv("plan"))
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        item = plan["batches"][0]["modulos"][0]
        self.wd = st_mod.workdir(self.store, self.repo)
        return item

    def _merge(self, item_path: str, agent: str) -> tuple[int, str, str]:
        inp = os.path.join(self.tmp.name, f"agent-{agent}.txt")
        _write(inp, _module_merge_input(item_path))
        return _run(self._argv("merge-agent-output", "modules", "--input", inp, "--agent", agent))

    def test_redo_with_backslash_then_done_keeps_same_count_no_duplicate(self) -> None:
        item_native = self._bootstrap()
        self.assertIn("\\", item_native, "pré-condição: path precisa ter '\\' nativo do Windows")
        code, _out, err = self._merge(item_native, "modules-b01")
        self.assertEqual(code, 0, err)
        st = st_mod.load(self.wd)
        self.assertEqual(len(st["stages"]["modules"]["done"]), 1)

        # redo --item usando EXATAMENTE a grafia nativa ('\\') gravada em done.
        code, out, err = _run(self._argv("redo", "modules", "--item", item_native))
        self.assertEqual(code, 0, err)

        code, _out, err = self._merge(item_native, "modules-b01")
        self.assertEqual(code, 0, err)

        st = st_mod.load(self.wd)
        done = st["stages"]["modules"]["done"]
        self.assertEqual(len(done), 1, f"esperado 1 entrada única, veio: {done!r}")
        self.assertEqual(len(st["stages"]["modules"]["pending"]), 0)

    def test_redo_with_forward_slash_matches_item_stored_with_backslash(self) -> None:
        item_native = self._bootstrap()
        self.assertIn("\\", item_native, "pré-condição: path precisa ter '\\' nativo do Windows")
        code, _out, err = self._merge(item_native, "modules-b01")
        self.assertEqual(code, 0, err)
        st = st_mod.load(self.wd)
        self.assertEqual(st["stages"]["modules"]["done"], [item_native])

        # redo --item usando '/' (operador digitou a forma portável) sobre um
        # estado cujo `done` está gravado com '\\'.
        item_slash = item_native.replace("\\", "/")
        code, out, err = _run(self._argv("redo", "modules", "--item", item_slash))
        self.assertEqual(
            code, 0,
            f"deveria casar com o item gravado, não falhar com 'nunca coberto': {err}",
        )
        result = json.loads(out)
        self.assertEqual(len(result["itens_reabertos"]), 1)

        st = st_mod.load(self.wd)
        self.assertEqual(st["stages"]["modules"]["done"], [], "item reaberto não pode sobrar em done")
        self.assertEqual(len(st["stages"]["modules"]["pending"]), 1)

        code, _out, err = self._merge(item_native, "modules-b01")
        self.assertEqual(code, 0, err)
        st = st_mod.load(self.wd)
        self.assertEqual(len(st["stages"]["modules"]["done"]), 1, "não pode duplicar após o novo merge")


class LegacyMixedSeparatorStateTest(unittest.TestCase):
    """`_reconcile_stage_items` é o mecanismo de migração (requisito 3): um
    state.json legado com grafias mistas '\\'/'/' para o MESMO item precisa
    ser normalizado/deduplicado na primeira mutação subsequente do stage,
    sem exigir reescrita manual do arquivo pelo operador."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(self.repo, exist_ok=True)
        self.wd = st_mod.workdir(self.store, self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_legacy_state(self) -> None:
        st_mod.init(self.wd, self.repo, topic=None)
        st = st_mod.load(self.wd)
        s = st["stages"]["modules"]
        # 20 itens já canônicos ('/') + 1 item duplicado como '\\' (done,
        # legado) E '/' (pending, escrito por um redo/merge já corrigido) —
        # a mesma colisão relatada no incidente real, montada à mão para não
        # depender de reproduzir o pipeline inteiro.
        clean_done = [f"pkg/mod-{i:02d}" for i in range(20)]
        s["done"] = sorted(clean_done + ["quote-service\\src\\main\\java\\application\\cache"])
        s["pending"] = ["quote-service/src/main/java/application/cache"]
        s["items_complete"] = False
        st_mod.save(self.wd, st)

    def test_legacy_mixed_state_normalizes_and_dedupes_on_migration(self) -> None:
        self._seed_legacy_state()
        changed = cli_mod._reconcile_stage_items(self.wd, "modules")
        self.assertTrue(changed)

        st = st_mod.load(self.wd)
        s = st["stages"]["modules"]
        done_set = set(s["done"])
        pending_set = set(s["pending"])
        # A colisão resolve por precedência: pending > done — a grafia '\\'
        # órfã em done desaparece, e o item continua (só) pendente.
        self.assertNotIn("quote-service\\src\\main\\java\\application\\cache", done_set)
        self.assertEqual(pending_set, {"quote-service/src/main/java/application/cache"})
        self.assertEqual(len(done_set), 20)
        # Nenhuma duplicata lógica: união de done+pending tem exatamente 21
        # itens canônicos, cada um representado uma única vez.
        all_canonical = {cli_mod._normalize_item(x) for x in done_set | pending_set}
        self.assertEqual(len(all_canonical), 21)

    def test_legacy_mixed_state_second_migration_is_noop(self) -> None:
        """Idempotência: rodar a migração de novo sobre um state já canônico
        não reescreve nada (decisão documentada: não tocar o arquivo sem
        necessidade)."""
        self._seed_legacy_state()
        self.assertTrue(cli_mod._reconcile_stage_items(self.wd, "modules"))
        self.assertFalse(cli_mod._reconcile_stage_items(self.wd, "modules"))

    def test_done_command_triggers_migration_before_mutating(self) -> None:
        """A migração roda automaticamente no ponto de entrada de `done`, sem
        o operador precisar invocar nada manualmente."""
        self._seed_legacy_state()
        code, _out, err = _run([
            "--store", self.store, "--repo", self.repo,
            "blocked", "modules", "--item", "pkg/mod-00",
        ])
        self.assertEqual(code, 0, err)
        st = st_mod.load(self.wd)
        s = st["stages"]["modules"]
        self.assertNotIn("quote-service\\src\\main\\java\\application\\cache", set(s["done"]))
        self.assertIn("quote-service/src/main/java/application/cache", set(s["pending"]))


if __name__ == "__main__":
    unittest.main()
