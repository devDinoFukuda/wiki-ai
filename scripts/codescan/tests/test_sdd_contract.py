from __future__ import annotations

import os
import unittest

from codescan import sdd as sdd_mod


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


class SddContractDocsTest(unittest.TestCase):
    def _docs(self) -> str:
        paths = [
            os.path.join(ROOT, "README.md"),
            os.path.join(ROOT, "PLANO_EXECUCAO_TECNICO.md"),
            os.path.join(ROOT, "references", "sdd-contract.md"),
            os.path.join(ROOT, "operations", "ingest-codebase.md"),
        ]
        docs = []
        for path in paths:
            with open(path, encoding="utf-8") as f:
                docs.append(f.read())
        return "\n".join(docs)

    def test_temp_python_scripts_are_not_sdd_artifacts(self):
        text = self._docs()
        self.assertIn("*.py", text)
        self.assertIn("não são artefatos SDD", text)
        self.assertIn("não podem ir para `inbox/`", text)

    def test_quality_gate_documents_ptbr_traceability_and_boilerplate_rejection(self):
        text = self._docs()
        self.assertIn("PT-BR", text)
        self.assertIn("rastreabilidade", text)
        self.assertIn("boilerplate", text)
        self.assertIn("não pode virar `done`", text)
        self.assertIn("Overview", text)
        self.assertIn("conteúdo operacional suficiente para reimplementação", text)

    def test_reversa_90_operational_gate_is_documented(self):
        text = self._docs()
        self.assertIn("aderência >=90", text)
        self.assertIn("score final >=90", text)
        self.assertIn("score >=90", text)
        self.assertIn("reimplementabilidade", text)
        self.assertIn("Rastreabilidade código -> regra -> requisito -> design -> tarefa", text)

    def test_specialized_roles_are_documented(self):
        text = self._docs()
        for role in ("Scout", "Archaeologist", "Detective", "Architect", "Writer", "Reviewer"):
            self.assertIn(role, text)
        self.assertIn("Status por papel", text)
        self.assertIn("blocked", text)
        self.assertIn("failed", text)
        self.assertIn("degraded", text)

    def test_token_and_prose_reduction_rules_are_documented(self):
        text = self._docs()
        self.assertIn("Economia de tokens", text)
        self.assertIn("redução de prosa", text)
        self.assertIn("sem resumo executivo", text)
        self.assertIn("não repetir contexto", text)
        self.assertIn("limitar cada seção comum a 3-8 bullets ou 1 tabela", text)
        self.assertIn("reduzir output sem reduzir rastreabilidade", text)


    def test_brief_exposes_compact_parseable_contract(self):
        brief = sdd_mod.brief("/tmp/wd", "modules", {"sdd": {"doc_level": "essencial"}})
        contract = brief["compact_contract"]
        self.assertTrue(contract["format"]["no_preamble"])
        self.assertTrue(contract["format"]["no_summary"])
        self.assertTrue(contract["format"]["no_code_echo"])
        self.assertIn("saida_apenas_blocos_parseaveis", contract["rules"])
        self.assertIn("nao_repetir_contexto", contract["rules"])
        self.assertEqual(0, contract["limits"]["max_quote_lines"])
        self.assertLessEqual(contract["stage"]["max_lines_per_block"], 60)
        self.assertIn("MODULE", contract["stage"]["blocks"])
        self.assertIn("Rastreabilidade", contract["stage"]["required_sections"])

    def test_compact_contract_is_defined_for_agent_stages(self):
        for stage in ("modules", "rules", "architecture", "specs", "synth"):
            contract = sdd_mod.compact_contract(stage)
            self.assertIn("stage", contract)
            self.assertIn("rules", contract)
            self.assertIn("required_sections", contract["stage"])
            self.assertTrue(contract["format"]["no_diff"])

    def test_docs_require_compact_agent_output_without_context_echo(self):
        text = self._docs()
        self.assertIn("Brief compacto para subagentes", text)
        self.assertIn("Não ecoe código", text)
        self.assertIn("Não repita contexto", text)
        self.assertIn("blocos parseáveis", text)
        self.assertIn("sem resumo", text)

    def test_docs_cover_final_orchestration_constraints(self):
        text = self._docs()
        self.assertIn("run-stage", text)
        self.assertIn("agent-runs/<stage>.json", text)
        self.assertIn("E2E Determinístico", text)
        self.assertIn("parser/renderizador externo não é obrigatório", text)
        self.assertIn("compactação por excesso de contexto", text)
        self.assertIn("status do orquestrador", text)
        self.assertIn("3 linhas", text)
        self.assertIn("8 linhas", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
