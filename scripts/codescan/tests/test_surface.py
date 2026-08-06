"""Testes da varredura surface — foco na poda de diretórios."""

import os
import tempfile
import unittest

from codescan.surface import scan


def _write(root, rel, content="x = 1\n"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


class TestPackagesPruning(unittest.TestCase):
    def test_monorepo_js_packages_nao_e_podado(self):
        with tempfile.TemporaryDirectory() as repo:
            _write(repo, "packages/app/package.json", '{"name": "app"}\n')
            _write(repo, "packages/app/src/index.ts", "export const a = 1;\n")
            _write(repo, "packages/lib/package.json", '{"name": "lib"}\n')
            _write(repo, "packages/lib/src/util.ts", "export const b = 2;\n")
            s = scan(repo, module_min_files=1)
            paths = [m.path for m in s.modules]
            self.assertTrue(
                any("packages" in p for p in paths),
                f"monorepo JS deve manter packages/: {paths}",
            )
            self.assertGreaterEqual(s.total_files, 2)

    def test_packages_nuget_continua_podado(self):
        with tempfile.TemporaryDirectory() as repo:
            _write(repo, "src/Program.cs", "class P {}\n")
            # vendoring NuGet: sem package.json um nível abaixo
            _write(repo, "packages/Newtonsoft.Json.13.0.3/lib/net45/readme.txt", "bin\n")
            s = scan(repo, module_min_files=1)
            paths = [m.path for m in s.modules]
            self.assertFalse(any(p.startswith("packages") for p in paths), paths)
            self.assertEqual(s.total_files, 1)


if __name__ == "__main__":
    unittest.main()
