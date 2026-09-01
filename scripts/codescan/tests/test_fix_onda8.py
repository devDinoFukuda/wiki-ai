# -*- coding: utf-8 -*-
"""Testes unitários para o módulo piloto (Onda 8) — protocolo PILOTO da LLM.

Onda 8: novo módulo `scripts/codescan/pilot.py` e comando `wk code pilot`.

Testes:
  1. pilot_prompt contém caminhos LITERALMENTE + 12 seções + tabela despacho + R1-R10 + constantes
  2. WK_FLOW_COMMAND_TEMPLATE tem 4 placeholders + frontmatter
  3. render_wk_flow_command substitui placeholders + preserva JSON literals
  4. WK_FLOW_COMMAND_FILENAME == "wk-flow.md"
  5. CLI: code pilot + code pilot --command-file + code pilot --quiet
  6. Contrato JSON do auto tem os campos citados no prompt

Rodar:
    python -m pytest scripts/codescan/tests/test_fix_onda8.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import unittest

from codescan import pilot
from codescan.cli import (
    AUTO_MAX_ACOES,
    HANDOFF_CITACAO_REGRA,
    HANDOFF_FALLBACK_ESCRITA,
    main,
)


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


class PilotPromptStructureTests(unittest.TestCase):
    """Teste 1: pilot_prompt contém estrutura completa."""

    def test_pilot_prompt_contains_literal_paths(self):
        """pilot_prompt impresso contém caminhos LITERALMENTE como passados."""
        python = "/custom/python"
        pyz = "/custom/wk.pyz"
        store = "/tmp/store"
        repo = "/tmp/repo"

        prompt = pilot.pilot_prompt(python=python, pyz=pyz, store=store, repo=repo)

        # Verificar que os caminhos aparecem LITERALMENTE
        self.assertIn(python, prompt)
        self.assertIn(pyz, prompt)
        self.assertIn(store, prompt)
        self.assertIn(repo, prompt)

    def test_pilot_prompt_contains_12_sections(self):
        """pilot_prompt contém 12 seções numeradas (## 1 ... ## 12)."""
        prompt = pilot.pilot_prompt(store="/tmp/s", repo="/tmp/r")

        # Verificar cada seção de 1 a 12
        for i in range(1, 13):
            pattern = rf"^## {i}\."
            self.assertTrue(
                re.search(pattern, prompt, re.MULTILINE),
                f"Seção ## {i} não encontrada no prompt",
            )

    def test_pilot_prompt_dispatch_table_all_parado_em(self):
        """Tabela de despacho contém TODOS os valores de parado_em."""
        prompt = pilot.pilot_prompt(store="/tmp/s", repo="/tmp/r")

        required_values = [
            "fanout:",
            "decisao_humana",
            "pipeline_completo",
            "limite_de_acoes",
            "erro",
            "intervencao",
            "sem_progresso",
        ]
        for value in required_values:
            self.assertIn(
                value,
                prompt,
                f"parado_em value '{value}' não encontrado na tabela de despacho",
            )

    def test_pilot_prompt_contains_all_rules_r1_to_r10(self):
        """pilot_prompt contém as 10 regras R1 a R10."""
        prompt = pilot.pilot_prompt(store="/tmp/s", repo="/tmp/r")

        for i in range(1, 11):
            pattern = rf"R{i}\s*\|"  # Formato: R1 | regra
            self.assertTrue(
                re.search(pattern, prompt),
                f"Regra R{i} não encontrada no prompt",
            )

    def test_pilot_prompt_interpolates_handoff_citacao_regra_literally(self):
        """HANDOFF_CITACAO_REGRA aparece LITERAL no prompt (mesma fonte de verdade)."""
        prompt = pilot.pilot_prompt(store="/tmp/s", repo="/tmp/r")

        # A constante inteira deve aparecer textualmente
        self.assertIn(
            HANDOFF_CITACAO_REGRA,
            prompt,
            "HANDOFF_CITACAO_REGRA não interpolada literalmente no prompt",
        )

    def test_pilot_prompt_interpolates_handoff_fallback_escrita_literally(self):
        """HANDOFF_FALLBACK_ESCRITA aparece LITERAL no prompt (mesma fonte de verdade)."""
        prompt = pilot.pilot_prompt(store="/tmp/s", repo="/tmp/r")

        # A constante inteira deve aparecer textualmente
        self.assertIn(
            HANDOFF_FALLBACK_ESCRITA,
            prompt,
            "HANDOFF_FALLBACK_ESCRITA não interpolada literalmente no prompt",
        )

    def test_pilot_prompt_mentions_pilot_max_acoes_ceiling(self):
        """Prompt menciona o teto PILOT_MAX_ACOES (40)."""
        prompt = pilot.pilot_prompt(store="/tmp/s", repo="/tmp/r")

        # Verificar que o teto é mencionado
        self.assertIn(
            str(pilot.PILOT_MAX_ACOES),
            prompt,
            f"PILOT_MAX_ACOES ({pilot.PILOT_MAX_ACOES}) não mencionado no prompt",
        )
        self.assertEqual(
            pilot.PILOT_MAX_ACOES,
            40,
            "PILOT_MAX_ACOES deve ser 40",
        )


class WKFlowCommandTemplateTests(unittest.TestCase):
    """Teste 2: WK_FLOW_COMMAND_TEMPLATE tem placeholders e frontmatter."""

    def test_template_has_4_placeholders(self):
        """WK_FLOW_COMMAND_TEMPLATE contém exatamente os 4 placeholders."""
        template = pilot.WK_FLOW_COMMAND_TEMPLATE

        for placeholder in pilot.WK_FLOW_PLACEHOLDERS:
            self.assertIn(
                placeholder,
                template,
                f"Placeholder {placeholder} não encontrado no template",
            )
        self.assertEqual(len(pilot.WK_FLOW_PLACEHOLDERS), 4)

    def test_template_has_frontmatter_with_description(self):
        """WK_FLOW_COMMAND_TEMPLATE começa com frontmatter --- e contém description."""
        template = pilot.WK_FLOW_COMMAND_TEMPLATE

        # Deve começar com ---
        self.assertTrue(template.startswith("---\n"))
        # Deve conter description
        self.assertIn("description:", template)

    def test_template_wrapped_by_wk_flow_command(self):
        """Verificar que o template é envolvido por wk_flow_command()."""
        # wk_flow_command() adiciona frontmatter e passa o prompt
        # Então WK_FLOW_COMMAND_TEMPLATE já deve ter o frontmatter incluído
        lines = pilot.WK_FLOW_COMMAND_TEMPLATE.split("\n")
        self.assertEqual(lines[0], "---")
        self.assertTrue(any("description:" in line for line in lines[:5]))


class RenderWKFlowCommandTests(unittest.TestCase):
    """Teste 3: render_wk_flow_command substitui placeholders + preserva JSON."""

    def test_render_replaces_all_placeholders(self):
        """render_wk_flow_command substitui TODOS os 4 placeholders."""
        result = pilot.render_wk_flow_command(
            python="/p",
            pyz="/z",
            store="/s",
            repo="/r",
        )

        # Nenhum placeholder deve sobrar
        for placeholder in pilot.WK_FLOW_PLACEHOLDERS:
            self.assertNotIn(
                placeholder,
                result,
                f"Placeholder {placeholder} não foi substituído",
            )

        # E os valores devem estar lá
        self.assertIn("/p", result)
        self.assertIn("/z", result)
        self.assertIn("/s", result)
        self.assertIn("/r", result)

    def test_render_preserves_json_literals_in_body(self):
        """render_wk_flow_command preserva chaves JSON literais do corpo."""
        result = pilot.render_wk_flow_command(
            python="/p",
            pyz="/z",
            store="/s",
            repo="/r",
        )

        # O prompt contém um exemplo JSON com {"parado_em":...}
        # Esse deve ser preservado LITERAL
        self.assertIn('{"parado_em":', result, "Exemplo JSON não preservado")
        self.assertIn('"executados":', result, "Campo JSON não preservado")

    def test_render_doesnt_use_format_method(self):
        """Verificação indireta: render usa str.replace, não str.format."""
        # Se usasse format, falharia com KeyError nos {} das tabelas
        # Como não falha, deduzimos que usa replace
        result = pilot.render_wk_flow_command(
            python="/p",
            pyz="/z",
            store="/s",
            repo="/r",
        )
        # Se chegou aqui sem erro, passou
        self.assertIsNotNone(result)


class WKFlowCommandFilenameTests(unittest.TestCase):
    """Teste 4: WK_FLOW_COMMAND_FILENAME tem nome exato."""

    def test_filename_is_wk_flow_md(self):
        """WK_FLOW_COMMAND_FILENAME == "wk-flow.md"."""
        self.assertEqual(pilot.WK_FLOW_COMMAND_FILENAME, "wk-flow.md")


class CliPilotTests(unittest.TestCase):
    """Teste 5: CLI `code pilot`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        # Criar módulos de teste
        for i in range(1, 2):
            _write(
                os.path.join(self.repo, "src", f"m{i}", "Service.java"),
                _module_java(f"m{i}"),
            )
        os.environ.pop("WK_STORE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, *args]

    def test_pilot_exits_0_and_prints_prompt(self):
        """`code pilot` sai com exit 0 e imprime prompt contendo 'PROTOCOLO PILOTO'."""
        code, out, _err = _run(self._argv("pilot"))

        self.assertEqual(code, 0, f"Esperado exit 0, got {code}")
        self.assertIn("PROTOCOLO PILOTO", out, "Prompt não contém 'PROTOCOLO PILOTO'")

    def test_pilot_prompt_contains_resolved_paths(self):
        """`code pilot` resolve os caminhos store/repo no prompt."""
        code, out, _err = _run(self._argv("pilot"))

        self.assertEqual(code, 0)
        # Os caminhos devem aparecer resolvidos (não placeholders)
        self.assertIn(self.store, out, "store não aparece resolvido no prompt")
        self.assertIn(self.repo, out, "repo não aparece resolvido no prompt")

    def test_pilot_command_file_exits_0_and_prints_frontmatter(self):
        """`code pilot --command-file` sai 0 e imprime frontmatter sem placeholders não resolvidos."""
        code, out, _err = _run(self._argv("pilot", "--command-file"))

        self.assertEqual(code, 0)
        # Deve começar com frontmatter ---
        self.assertTrue(out.startswith("---\n"), "Esperado frontmatter no início")
        # Não deve conter placeholders não resolvidos
        for placeholder in pilot.WK_FLOW_PLACEHOLDERS:
            self.assertNotIn(
                placeholder,
                out,
                f"Placeholder {placeholder} não foi resolvido em --command-file",
            )

    def test_pilot_quiet_flag_doesnt_degrade_output(self):
        """`code pilot --quiet` imprime prompt íntegro (sem degradação)."""
        code, out, _err = _run(self._argv("pilot", "--quiet"))

        self.assertEqual(code, 0)
        # A flag --quiet não deve alterar o conteúdo do prompt
        self.assertIn("PROTOCOLO PILOTO", out, "Prompt degradado com --quiet")
        # Deve conter as 12 seções
        for i in range(1, 13):
            pattern = rf"^## {i}\."
            self.assertTrue(
                re.search(pattern, out, re.MULTILINE),
                f"Seção ## {i} ausente com --quiet",
            )


class AutoContractJsonTests(unittest.TestCase):
    """Teste 6: Contrato JSON do auto emite campos citados no prompt."""

    def test_auto_contract_fields_match_prompt_references(self):
        """Os campos do JSON do auto coincidem com os nomes citados no prompt."""
        prompt = pilot.pilot_prompt(store="/tmp/s", repo="/tmp/r")

        # Campos esperados, citados no prompt (executados aparece como executados[])
        field_patterns = [
            (r"`parado_em`", "parado_em"),
            (r"`motivo`", "motivo"),
            (r"`acao`", "acao"),
            (r"executados\[\]", "executados[]"),  # Pode estar em backticks ou não
            (r"`progresso`", "progresso"),
        ]

        for pattern, field_name in field_patterns:
            self.assertTrue(
                re.search(pattern, prompt),
                f"Campo '{field_name}' não citado no prompt com padrão '{pattern}'",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
