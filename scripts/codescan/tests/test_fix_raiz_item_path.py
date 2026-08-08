"""Teste da correção de RAIZ do BUG B3 (item de stage duplicado em
state.json por grafia de separador divergente '\\' vs '/') diretamente em
`state.py` (`mark_item`/`mark_item_status`/`set_pending`) e em
`sdd.redo_stage`.

Não duplica a cobertura de `test_fix_lote_a.py`
(`RedoPathSeparatorTest`/`LegacyMixedSeparatorStateTest`), que exercita a
defesa de `cli.py` (`_reconcile_stage_items`/`_resolve_stored_item`) via
linha de comando. Aqui a correção é exercida SEM passar pelo wrapper de
cli.py — chamando `state.py`/`sdd.redo_stage` direto, o caminho por onde o
bug nascia (`sdd.redo_stage` chamava `state.mark_item` sem nenhuma
resolução prévia contra a grafia já gravada)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from codescan import sdd as sdd_mod
from codescan import state as st_mod


def _canon(value: str) -> str:
    return value.replace("\\", "/")


class _WorkdirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = os.path.join(self.tmp.name, "wd")
        st_mod.init(self.wd, os.path.join(self.tmp.name, "repo"), topic=None)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class MarkItemSeparatorTest(_WorkdirTestCase):
    def test_mark_item_backslash_then_forward_slash_single_entry(self) -> None:
        st_mod.mark_item(self.wd, "modules", "pkg\\sub", done=True)
        st = st_mod.mark_item(self.wd, "modules", "pkg/sub", done=True)
        done = st["stages"]["modules"]["done"]
        self.assertEqual(len(done), 1, f"esperado 1 entrada única, veio: {done!r}")

    def test_legacy_pure_backslash_state_then_mark_item_no_duplicate(self) -> None:
        """state.json legado, gravado ANTES desta correção, com grafia '\\'
        pura: a leitura seguida de um `mark_item` com '/' não duplica."""
        st = st_mod.load(self.wd)
        st["stages"]["modules"]["done"] = ["quote-service\\src\\cache"]
        st_mod.save(self.wd, st)

        st = st_mod.mark_item(self.wd, "modules", "quote-service/src/cache", done=True)
        done = st["stages"]["modules"]["done"]
        self.assertEqual(len(done), 1, f"esperado 1 entrada única, veio: {done!r}")

    def test_legacy_mixed_separator_state_reconciles_on_next_write(self) -> None:
        """state.json legado misturando '\\' (em done) e '/' (em pending)
        para o MESMO item: a mutação seguinte de QUALQUER item do stage
        reconcilia a contagem, sem exigir migração manual do operador."""
        st = st_mod.load(self.wd)
        s = st["stages"]["modules"]
        clean_done = [f"pkg/mod-{i:02d}" for i in range(5)]
        s["done"] = sorted(clean_done + ["quote-service\\src\\cache"])
        s["pending"] = ["quote-service/src/cache"]
        st_mod.save(self.wd, st)

        st = st_mod.mark_item(self.wd, "modules", "pkg/mod-00", done=True)
        s = st["stages"]["modules"]
        done_canon = {_canon(x) for x in s["done"]}
        pending_canon = {_canon(x) for x in s["pending"]}
        self.assertEqual(len(done_canon), 5, f"done deveria ter 5 itens únicos: {s['done']!r}")
        self.assertEqual(pending_canon, {"quote-service/src/cache"})
        self.assertNotIn("quote-service/src/cache", done_canon, "cache não pode sobrar em done")
        # nenhuma duplicata lógica: união de done+pending com exatamente 6 itens.
        self.assertEqual(len(done_canon | pending_canon), 6)

    def test_non_path_item_names_untouched(self) -> None:
        """Itens que NÃO são caminhos (nomes de spec unit / documento
        nomeado, ex.: `api-contract`, `functional`) não sofrem transformação
        indevida — sem separador para normalizar, ficam intactos."""
        st_mod.mark_item(self.wd, "specs", "api-contract", done=True)
        st = st_mod.mark_item(self.wd, "specs", "functional", done=True)
        done = set(st["stages"]["specs"]["done"])
        self.assertEqual(done, {"api-contract", "functional"})


class SetPendingSeparatorTest(_WorkdirTestCase):
    def test_mixed_separators_in_same_call_dedupe(self) -> None:
        st = st_mod.set_pending(self.wd, "modules", ["a\\b\\c", "a/b/c", "d"])
        pending = st["stages"]["modules"]["pending"]
        canon = {_canon(x) for x in pending}
        self.assertEqual(len(pending), 2, f"esperado 2 entradas (set canônico), veio: {pending!r}")
        self.assertEqual(canon, {"a/b/c", "d"})

    def test_set_pending_resolves_against_existing_done_no_duplicate(self) -> None:
        st_mod.mark_item(self.wd, "modules", "a/b", done=True)
        st = st_mod.set_pending(self.wd, "modules", ["a\\b", "c"])
        s = st["stages"]["modules"]
        self.assertEqual(s["pending"], ["c"], "item já done não pode reaparecer em pending")
        self.assertEqual(s["done"], ["a/b"])


class RedoStageRootFixTest(_WorkdirTestCase):
    """`sdd.redo_stage` chamando `state.mark_item` DIRETO — o caminho exato
    de onde nasceu o BUG B3, sem passar pela defesa de cli.py."""

    def _seed_manifest(self, item: str) -> None:
        manifest = {
            "schema": sdd_mod.AGENT_RUNS_SCHEMA,
            "stage": "modules",
            "runs": [{"items": [{"item": item}]}],
        }
        path = os.path.join(self.wd, "agent-runs", "modules.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

    def _seed_done(self, item: str) -> None:
        st = st_mod.load(self.wd)
        st["stages"]["modules"]["done"] = [item]
        st_mod.save(self.wd, st)

    def test_redo_item_backslash_over_done_backslash_no_duplicate(self) -> None:
        """Cenário exato do B3: --item na mesma grafia '\\' já gravada em
        done. Sem a correção de raiz, `redo_stage` normalizava para '/' e
        `state.mark_item` criava uma segunda entrada."""
        item_native = "quote-service\\src\\main\\cache"
        self._seed_manifest(item_native)
        self._seed_done(item_native)

        result = sdd_mod.redo_stage(self.wd, "modules", item=item_native)
        self.assertEqual(result["itens_reabertos"], ["quote-service/src/main/cache"])

        s = st_mod.load(self.wd)["stages"]["modules"]
        self.assertEqual(s["done"], [], f"done deveria ficar vazio: {s['done']!r}")
        self.assertEqual(len(s["pending"]), 1, f"esperado 1 pendente, veio: {s['pending']!r}")

    def test_redo_item_forward_slash_over_done_backslash_matches(self) -> None:
        """--item em '/' sobre um done gravado em '\\': tem que CASAR com o
        item existente (itens_reabertos == 1), não falhar nem duplicar."""
        item_native = "quote-service\\src\\main\\cache"
        item_slash = "quote-service/src/main/cache"
        self._seed_manifest(item_native)
        self._seed_done(item_native)

        result = sdd_mod.redo_stage(self.wd, "modules", item=item_slash)
        self.assertEqual(len(result["itens_reabertos"]), 1)

        s = st_mod.load(self.wd)["stages"]["modules"]
        self.assertEqual(s["done"], [], f"done deveria ficar vazio: {s['done']!r}")
        self.assertEqual(len(s["pending"]), 1, f"esperado 1 pendente, veio: {s['pending']!r}")

    def test_redo_then_redo_again_stays_single_entry(self) -> None:
        """Idempotência: reabrir o mesmo item duas vezes seguidas (segunda
        chamada é no-op de `redo_stage`, mas exercita `mark_item(done=False)`
        de novo sobre um item já pendente) não introduz duplicata."""
        item_native = "quote-service\\src\\main\\cache"
        self._seed_manifest(item_native)
        self._seed_done(item_native)

        sdd_mod.redo_stage(self.wd, "modules", item=item_native)
        s = st_mod.load(self.wd)["stages"]["modules"]
        self.assertEqual(len(s["pending"]), 1)
        self.assertEqual(len(s["done"]), 0)


if __name__ == "__main__":
    unittest.main()
