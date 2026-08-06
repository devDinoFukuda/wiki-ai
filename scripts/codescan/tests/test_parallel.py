"""Testes: role main/test, partição em batches e lock concorrente do estado."""

import os
import tempfile
import threading
import unittest

from codescan.surface import scan, _role_of
from codescan import state as st_mod
from codescan.cli import _balance_batches, _auto_batches, MAX_AGENTS, TARGET_LOC_PER_AGENT


def _write(root, rel, content="x = 1\n"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


class TestRole(unittest.TestCase):
    def test_role_of(self):
        self.assertEqual(_role_of("src/main/java/x"), "main")
        self.assertEqual(_role_of("src/test/java/x"), "test")
        self.assertEqual(_role_of("app/__tests__/y"), "test")
        self.assertEqual(_role_of("lib/specs/z"), "test")

    def test_main_ranqueado_antes_de_test_maior(self):
        with tempfile.TemporaryDirectory() as repo:
            # test com MAIS loc que main; main deve vir primeiro mesmo assim
            for i in range(3):
                _write(repo, f"src/main/java/app/M{i}.java", "class A {}\n")
            for i in range(6):
                _write(repo, f"src/test/java/app/T{i}.java", "class A {}\nclass B {}\n")
            s = scan(repo, module_min_files=1)
            self.assertEqual(s.modules[0].role, "main")
            roles = [m.role for m in s.modules]
            # nenhum test aparece antes de um main
            first_test = roles.index("test") if "test" in roles else len(roles)
            last_main = max(i for i, r in enumerate(roles) if r == "main")
            self.assertLess(last_main, first_test)


class TestBatches(unittest.TestCase):
    def test_balanceia_por_loc(self):
        mods = [
            {"path": "a", "loc": 1000}, {"path": "b", "loc": 900},
            {"path": "c", "loc": 100}, {"path": "d", "loc": 50},
        ]
        groups = _balance_batches(mods, 2)
        self.assertEqual(len(groups), 2)
        loads = sorted(sum(m["loc"] for m in g) for g in groups)
        # os dois grupos ficam próximos: 1000+50 vs 900+100
        self.assertLessEqual(loads[1] - loads[0], 100)

    def test_particao_e_cobertura_total(self):
        mods = [{"path": str(i), "loc": i} for i in range(1, 8)]
        groups = _balance_batches(mods, 3)
        paths = sorted(m["path"] for g in groups for m in g)
        self.assertEqual(paths, sorted(str(i) for i in range(1, 8)))
        # sem sobreposição
        flat = [m["path"] for g in groups for m in g]
        self.assertEqual(len(flat), len(set(flat)))


class TestAutoBatches(unittest.TestCase):
    def _mods(self, locs):
        return [{"path": str(i), "loc": v} for i, v in enumerate(locs)]

    def test_um_modulo_um_agente(self):
        self.assertEqual(_auto_batches(self._mods([500])), 1)

    def test_zero_modulos(self):
        self.assertEqual(_auto_batches([]), 1)

    def test_nunca_mais_grupos_que_modulos(self):
        # 3 módulos minúsculos: LOC pediria 1, contagem limita — nunca vazio
        n = _auto_batches(self._mods([10, 10, 10]))
        self.assertLessEqual(n, 3)
        self.assertGreaterEqual(n, 1)

    def test_dimensiona_por_loc(self):
        # 10 módulos de 1000 LOC = 10000; ceil(10000/2000)=5
        n = _auto_batches(self._mods([1000] * 10))
        self.assertEqual(n, 5)

    def test_teto_max_agents(self):
        # 100 módulos gigantes: LOC pediria muito, mas o teto segura
        n = _auto_batches(self._mods([5000] * 100))
        self.assertEqual(n, MAX_AGENTS)

    def test_particao_com_n_auto_cobre_tudo_sem_vazio(self):
        mods = self._mods([300, 900, 100, 1500, 50, 2000, 700])
        n = _auto_batches(mods)
        groups = _balance_batches(mods, n)
        self.assertTrue(all(len(g) > 0 for g in groups), "nenhum grupo vazio")
        flat = [m["path"] for g in groups for m in g]
        self.assertEqual(sorted(flat), sorted(m["path"] for m in mods))
        self.assertEqual(len(flat), len(set(flat)), "sem módulo duplicado")


class TestLockConcurrente(unittest.TestCase):
    def test_marks_paralelos_nao_se_perdem(self):
        with tempfile.TemporaryDirectory() as wd:
            st_mod.init(wd, wd, topic=None)
            items = [f"mod-{i}" for i in range(40)]
            st_mod.set_pending(wd, "modules", items)

            def worker(it):
                st_mod.mark_item(wd, "modules", it, done=True)

            threads = [threading.Thread(target=worker, args=(it,)) for it in items]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            st = st_mod.load(wd)
            done = set(st["stages"]["modules"]["done"])
            self.assertEqual(done, set(items), "nenhum done pode se perder")
            self.assertEqual(st["stages"]["modules"]["status"], "in_progress")
            self.assertTrue(st["stages"]["modules"]["items_complete"])


if __name__ == "__main__":
    unittest.main()
