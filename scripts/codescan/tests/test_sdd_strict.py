from __future__ import annotations

import os
import tempfile
import unittest

from codescan import sdd as sdd_mod
from codescan import state as st_mod


def _write(path: str, text: str = "x\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _healthy_audit_text(extra: str = "") -> str:
    lines = [
        "# Artefato",
        "",
        "- fluxo operacional com erro, dependencia, entrada, saida e teste rastreavel.",
        "- regra operacional cobre criterio de aceite e rastreabilidade entre codigo e requisito.",
        "- dependencia externa registra estado, integracao, entrada e saida do processo.",
        "- teste de reimplementacao valida fluxo principal e fluxo de erro.",
        "- detalhe operacional evita conteudo generico e preserva contexto tecnico.",
    ]
    if extra:
        lines.append(f"- {extra}")
    return "\n".join(lines)


class StrictAuditTest(unittest.TestCase):
    def _wd(self) -> tuple[tempfile.TemporaryDirectory[str], str]:
        tmp = tempfile.TemporaryDirectory()
        repo = os.path.join(tmp.name, "repo")
        store = os.path.join(tmp.name, "store")
        os.makedirs(repo, exist_ok=True)
        wd = st_mod.workdir(store, repo)
        st_mod.init(wd, repo, topic=None)
        return tmp, wd

    def _audit(self, wd: str, stage: str = "modules") -> dict:
        return sdd_mod.audit(wd, stage, st_mod.load(wd))

    def _blockers(self, report: dict) -> str:
        return "\n".join(
            blocker
            for stage in report["stages"]
            for blocker in stage["blockers"]
        )

    def test_p0_reprova_scripts_python_no_workdir(self):
        tmp, wd = self._wd()
        with tmp:
            _write(os.path.join(wd, "check_todo.py"), "print('x')\n")
            report = self._audit(wd)

        self.assertEqual(report["score"], 0)
        self.assertNotEqual(report["status"], "pass")
        self.assertIn("script python proibido", self._blockers(report))

    def test_p0_reprova_txt_operacional_solto_no_workdir(self):
        tmp, wd = self._wd()
        with tmp:
            _write(os.path.join(wd, "todo_matches.txt"), "TODO\n")
            report = self._audit(wd)

        self.assertEqual(report["score"], 0)
        self.assertIn("txt operacional solto", self._blockers(report))

    def test_p0_reprova_diretorio_src_no_workdir(self):
        tmp, wd = self._wd()
        with tmp:
            _write(os.path.join(wd, "src", "main", "App.java"), "class App {}\n")
            report = self._audit(wd)

        self.assertEqual(report["score"], 0)
        self.assertRegex(self._blockers(report), r"src|codebase")

    def test_p0_reprova_modules_vazio(self):
        tmp, wd = self._wd()
        with tmp:
            os.makedirs(os.path.join(wd, "modules", "quote", "empty"), exist_ok=True)
            report = self._audit(wd)

        self.assertEqual(report["score"], 0)
        self.assertIn("diretório vazio em modules", self._blockers(report))

    def test_p0_reprova_specs_vazia_unit_e_orfa(self):
        tmp, wd = self._wd()
        with tmp:
            os.makedirs(os.path.join(wd, "sdd", "specs", "_unit"), exist_ok=True)
            os.makedirs(os.path.join(wd, "sdd", "specs", "orfa"), exist_ok=True)
            report = self._audit(wd, "specs")

        blockers = self._blockers(report)
        self.assertEqual(report["score"], 0)
        self.assertIn("spec placeholder proibida", blockers)
        self.assertIn("spec órfã fora do estado", blockers)
        self.assertIn("diretório vazio em specs", blockers)

    def test_p0_reprova_agent_runs_ausente_para_stages_agent_output(self):
        for stage in sdd_mod.AGENT_OUTPUT_STAGES:
            tmp, wd = self._wd()
            with self.subTest(stage=stage), tmp:
                report = self._audit(wd, stage)

                self.assertEqual(report["score"], 0)
                self.assertIn("agent-runs obrigatório ausente", self._blockers(report))

    def test_secao_ausente_vira_aviso_cerimonial_e_nao_reduz_score(self):
        """W8-T8.3 (plano §3.1: "Score baseado em ... quantidade de seções
        ... apresentado como medida de entendimento"): seção obrigatória
        ausente não é mais `warning` nem reduz `score` — vira
        `avisos_cerimoniais` informativo. Integridade OK + "poucas seções"
        agora passa (comportamento desejado pelo plano)."""
        tmp, wd = self._wd()
        with tmp:
            path = os.path.join(wd, "artifact.md")
            _write(
                path,
                "\n".join(
                    [
                        "# Artefato",
                        "",
                        "## Presente",
                        "- fluxo com erro, dependência, entrada, saída e teste rastreável.",
                        "- fluxo com erro, dependência, entrada, saída e teste rastreável.",
                        "- fluxo com erro, dependência, entrada, saída e teste rastreável.",
                        "- fluxo com erro, dependência, entrada, saída e teste rastreável.",
                        "- fluxo com erro, dependência, entrada, saída e teste rastreável.",
                    ]
                ),
            )
            result = sdd_mod._audit_file(
                path,
                sdd_mod.ArtifactRule(
                    "artifact.md",
                    min_bytes=20,
                    min_citations=0,
                    sections=("Ausente",),
                ),
            )

        self.assertEqual(result["warnings"], [])
        self.assertEqual(len(result["avisos_cerimoniais"]), 1)
        self.assertTrue(result["avisos_cerimoniais"][0].startswith("seção sugerida ausente: Ausente"))
        self.assertIn("artifact.md", result["avisos_cerimoniais"][0])
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["status"], "pass")

    def test_audit_file_aceita_generics_java_sem_placeholder(self):
        tmp, wd = self._wd()
        with tmp:
            path = os.path.join(wd, "artifact.md")
            _write(
                path,
                _healthy_audit_text(
                    "Tipos legitimos: Optional<RuleViolation>, List<Violation>, "
                    "Map<String,Object> e ResponseEntity<QuoteResponse>."
                ),
            )
            result = sdd_mod._audit_file(
                path,
                sdd_mod.ArtifactRule("artifact.md", min_bytes=20, min_citations=0),
            )

        self.assertEqual(result["status"], "pass")
        self.assertNotIn("placeholder pendente", result["blockers"])

    def test_audit_file_aceita_todo_todos_e_camelcase_to_sem_placeholder(self):
        tmp, wd = self._wd()
        with tmp:
            path = os.path.join(wd, "artifact.md")
            _write(
                path,
                _healthy_audit_text(
                    "todo fluxo preserva todos os mapeamentos toResponse e toCommand "
                    "sem marcar palavra comum como pendencia."
                ),
            )
            result = sdd_mod._audit_file(
                path,
                sdd_mod.ArtifactRule("artifact.md", min_bytes=20, min_citations=0),
            )

        self.assertEqual(result["status"], "pass")
        self.assertNotIn("placeholder pendente", result["blockers"])

    def test_audit_file_rejeita_placeholders_reais(self):
        placeholders = (
            "TODO",
            "TBD",
            "FIXME",
            "XXX",
            "preencher",
            "pendente de análise",
            "<arquivo:linha>",
            "<descrição>",
            "<descricao>",
            "<preencher>",
            "<pendente>",
        )
        for placeholder in placeholders:
            tmp, wd = self._wd()
            with self.subTest(placeholder=placeholder), tmp:
                path = os.path.join(wd, "artifact.md")
                _write(path, _healthy_audit_text(f"Placeholder real: {placeholder}."))
                result = sdd_mod._audit_file(
                    path,
                    sdd_mod.ArtifactRule("artifact.md", min_bytes=20, min_citations=0),
                )

                self.assertTrue(any("placeholder pendente" in blocker for blocker in result["blockers"]))
                self.assertNotEqual(result["status"], "pass")


if __name__ == "__main__":
    unittest.main(verbosity=2)
