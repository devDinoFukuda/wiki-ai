"""Erros de merge autoexplicativos.

Execução real (logs) mostrou o agente decompilando o `wk.pyz` para descobrir
os cabeçalhos aceitos depois de um `prosa fora de bloco SYNTH`. O texto do
erro agora carrega o formato esperado e os ids aceitos — este teste fixa esse
contrato.
"""

from __future__ import annotations

import unittest

from codescan import agentmerge


class MergeErrorHintTest(unittest.TestCase):
    def test_prosa_fora_de_bloco_synth_lista_ids_aceitos(self):
        with self.assertRaises(agentmerge.MergeError) as ctx:
            agentmerge._parse_named_blocks("synth", "=== CONFIRMED: x ===\ncorpo\n=== END ===\n")
        msg = str(ctx.exception)
        self.assertIn("prosa fora de bloco SYNTH", msg)
        self.assertIn("=== SYNTH: <id> ===", msg)
        self.assertIn("confirmed", msg)
        self.assertIn("inferred", msg)

    def test_prosa_fora_de_bloco_rules_lista_ids_e_prefixos(self):
        with self.assertRaises(agentmerge.MergeError) as ctx:
            agentmerge._parse_named_blocks("rules", "texto solto\n")
        msg = str(ctx.exception)
        self.assertIn("prosa fora de bloco RULES", msg)
        self.assertIn("domain", msg)
        self.assertIn("adrs/NNN-<slug>", msg)

    def test_prosa_fora_de_bloco_module_traz_formato_esperado(self):
        with self.assertRaises(agentmerge.MergeError) as ctx:
            agentmerge._parse_modules("texto solto\n")
        msg = str(ctx.exception)
        self.assertIn("prosa fora de bloco MODULE", msg)
        self.assertIn("=== MODULE: <path> ===", msg)

    def test_prosa_fora_de_bloco_spec_avisa_singular(self):
        with self.assertRaises(agentmerge.MergeError) as ctx:
            agentmerge._parse_specs("=== SPECS: functional ===\ncorpo\n=== END ===\n")
        msg = str(ctx.exception)
        self.assertIn("prosa fora de bloco SPEC", msg)
        self.assertIn("SPEC singular", msg)

    def test_prosa_fora_de_arquivo_spec_lista_secoes(self):
        with self.assertRaises(agentmerge.MergeError) as ctx:
            agentmerge._parse_spec_body("functional", ["conteudo sem seção de arquivo"])
        msg = str(ctx.exception)
        self.assertIn("prosa fora de arquivo SPEC", msg)
        self.assertIn("--- requirements.md ---", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
