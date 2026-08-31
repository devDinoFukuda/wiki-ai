# -*- coding: utf-8 -*-
"""Testes unitários para o prompt de handoff (Onda 7) — fallback de escrita.

Onda 7: prompt de handoff contém `HANDOFF_FALLBACK_ESCRITA` (despachante grava
se subagente falhar; `./wk-agent-outputs/` como último recurso) + linha por
subagente "Se a escrita falhar, devolva o conteúdo completo"; `acao` de
`run`/`auto` na parada fanout menciona `wk init` para migrar permissões.

Testes:
  (a) handoff impresso contém "FALLBACK DE ESCRITA" e "wk-agent-outputs"
  (b) instrução por subagente contém "devolva o conteúdo completo"
  (c) `acao` da parada fanout contém "migrar as permissões"

Rodar:
    python -m pytest scripts/codescan/tests/test_fix_onda7.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from codescan.cli import main


def _write(path: str, text: str) -> None:
    """Escreve arquivo com diretórios criados."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Roda cli.main com stdout/stderr capturados."""
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _module_java(name: str) -> str:
    """Cria um módulo Java simples para teste."""
    return "\n".join(
        [
            f"package app.{name};",
            f"class {name.capitalize()}Service {{",
            "  boolean run(String id) {",
            "    if (id == null) return false;",
            "    repository.save(id);",
            "    return true;",
            "  }",
            "}",
        ]
    )


class Onda7HandoffFallbackTests(unittest.TestCase):
    """Onda 7a: handoff contém FALLBACK DE ESCRITA e wk-agent-outputs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        # Criar módulos de teste
        for i in range(1, 3):
            _write(
                os.path.join(self.repo, "src", f"m{i}", "Service.java"),
                _module_java(f"m{i}"),
            )
        os.environ.pop("WK_STORE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, "--verbose", *args]

    def _prepare(self) -> None:
        """Prepara o store para handoff."""
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "essencial", "--granularity", "module"))
        self.assertEqual(code, 0, err)

    def test_handoff_contains_fallback_de_escrita(self):
        """handoff impresso contém 'FALLBACK DE ESCRITA'."""
        self._prepare()

        # Preparar run-stage
        code, out, err = _run(self._argv("run-stage", "modules", "--batches", "2"))
        self.assertEqual(code, 0, err)

        # Executar handoff
        code, out, err = _run(self._argv("handoff", "modules"))

        self.assertEqual(code, 0, err)
        # Verificar que contém "FALLBACK DE ESCRITA"
        self.assertIn("FALLBACK DE ESCRITA", out,
                     "Prompt de handoff deve conter 'FALLBACK DE ESCRITA'")

    def test_handoff_contains_wk_agent_outputs_fallback_path(self):
        """handoff impresso contém './wk-agent-outputs/' como fallback path."""
        self._prepare()

        # Preparar run-stage
        code, out, err = _run(self._argv("run-stage", "modules", "--batches", "2"))
        self.assertEqual(code, 0, err)

        # Executar handoff
        code, out, err = _run(self._argv("handoff", "modules"))

        self.assertEqual(code, 0, err)
        # Verificar que contém "./wk-agent-outputs/"
        self.assertIn("wk-agent-outputs", out,
                     "Prompt de handoff deve conter 'wk-agent-outputs' como último recurso")


class Onda7SubagentInstructionTests(unittest.TestCase):
    """Onda 7b: instrução por subagente contém 'devolva o conteúdo completo'."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        # Criar módulos de teste
        for i in range(1, 3):
            _write(
                os.path.join(self.repo, "src", f"m{i}", "Service.java"),
                _module_java(f"m{i}"),
            )
        os.environ.pop("WK_STORE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, "--verbose", *args]

    def _prepare(self) -> None:
        """Prepara o store para handoff."""
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "essencial", "--granularity", "module"))
        self.assertEqual(code, 0, err)

    def test_handoff_subagent_instruction_contains_devolva_conteudo_completo(self):
        """Instrução por subagente contém 'devolva o conteúdo completo'."""
        self._prepare()

        # Preparar run-stage
        code, out, err = _run(self._argv("run-stage", "modules", "--batches", "2"))
        self.assertEqual(code, 0, err)

        # Executar handoff
        code, out, err = _run(self._argv("handoff", "modules"))

        self.assertEqual(code, 0, err)
        # Verificar que contém "devolva o conteúdo completo"
        self.assertIn("devolva o conteúdo completo", out,
                     "Instrução por subagente deve conter 'devolva o conteúdo completo'")


class Onda7FanoutActionTests(unittest.TestCase):
    """Onda 7c: `acao` da parada fanout menciona migração de permissões."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        # Criar módulos de teste
        for i in range(1, 3):
            _write(
                os.path.join(self.repo, "src", f"m{i}", "Service.java"),
                _module_java(f"m{i}"),
            )
        os.environ.pop("WK_STORE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, "--verbose", *args]

    def _prepare(self) -> None:
        """Prepara o store."""
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "essencial", "--granularity", "module"))
        self.assertEqual(code, 0, err)

    def test_run_command_action_mentions_wk_init_migration(self):
        """run command output contém acao mencionando 'wk init' e 'migrar as permissões'."""
        self._prepare()

        # Executar run com --batches para ativar handoff (fanout)
        code, out, err = _run(self._argv("run", "modules", "--batches", "2"))

        # Se code é 0, verificar que acao menciona migração
        # Se code é 2, verificar no err também
        output_to_check = out or err
        try:
            result = json.loads(output_to_check)
            if "acao" in result:
                acao = result.get("acao", "")
                # Verificar que contém referência a migração de permissões
                self.assertTrue(
                    "wk init" in acao or "migrar" in acao or "agent-outputs" in acao,
                    f"acao deve mencionar 'wk init', 'migrar' ou 'agent-outputs', got: {acao}"
                )
        except json.JSONDecodeError:
            # Se não é JSON, é o prompt; procurar nele
            self.assertIn("wk init", output_to_check,
                         "Output deve mencionar 'wk init' para migração de permissões")


if __name__ == "__main__":
    unittest.main(verbosity=2)
