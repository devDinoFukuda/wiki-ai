"""Gates de qualidade endurecidos do contrato SDD (S2).

Cobre: verificação criptográfica do manifesto agent-runs (schema v2), auditoria
de modules/*.md, normalização de MATRIX_MARKERS, whitelist de rótulo de aresta
Mermaid, canonização de synth em sdd/ e blockers de conteúdo acionáveis
(caminho + medido + esperado).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from codescan import sdd as sdd_mod
from codescan import state as st_mod


GREEN = "\U0001F7E2"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class SddGatesQualityTest(unittest.TestCase):
    def _wd(self) -> tuple[tempfile.TemporaryDirectory[str], str]:
        tmp = tempfile.TemporaryDirectory()
        repo = os.path.join(tmp.name, "repo")
        store = os.path.join(tmp.name, "store")
        os.makedirs(repo, exist_ok=True)
        wd = st_mod.workdir(store, repo)
        st_mod.init(wd, repo, topic=None)
        return tmp, wd

    # ---- Tarefa 1: verificação criptográfica do manifesto ------------------

    def test_agent_run_blockers_reprova_schema_legado_v1(self):
        tmp, wd = self._wd()
        with tmp:
            input_path = os.path.join(wd, "agent-outputs", "modules-batch-01.txt")
            _write(input_path, "conteudo")
            artifact_path = os.path.join(wd, "modules", "src-payments.md")
            _write(artifact_path, "conteudo do modulo")
            manifest = {
                "schema": "wiki-ai.agent-runs.v1",
                "stage": "modules",
                "runs": [
                    {
                        "stage": "modules",
                        "input": input_path,
                        "items": [{"item": "src/payments", "artifacts": [artifact_path]}],
                        "items_count": 1,
                        "artifacts_count": 1,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
            _write_json(os.path.join(wd, "agent-runs", "modules.json"), manifest)
            blockers = sdd_mod._agent_run_blockers(wd, "modules", {"stages": {}})

        self.assertTrue(any(b.startswith("P0:") for b in blockers))
        self.assertTrue(any("legado" in b and "wiki-ai.agent-runs.v1" in b for b in blockers))
        self.assertTrue(any("wiki-ai.agent-runs.v2" in b for b in blockers))

    def test_agent_run_blockers_reprova_input_sha256_divergente(self):
        tmp, wd = self._wd()
        with tmp:
            input_path = os.path.join(wd, "agent-outputs", "modules-batch-01.txt")
            _write(input_path, "conteudo original do input")
            artifact_path = os.path.join(wd, "modules", "src-payments.md")
            _write(artifact_path, "conteudo do modulo")
            manifest = {
                "schema": "wiki-ai.agent-runs.v2",
                "stage": "modules",
                "runs": [
                    {
                        "stage": "modules",
                        "input": input_path,
                        "input_sha256": "0" * 64,
                        "input_bytes": os.path.getsize(input_path),
                        "agent": None,
                        "items": [
                            {
                                "item": "src/payments",
                                "artifacts": [
                                    {
                                        "path": artifact_path,
                                        "sha256": st_mod.sha256_file(artifact_path),
                                        "bytes": os.path.getsize(artifact_path),
                                    }
                                ],
                            }
                        ],
                        "items_count": 1,
                        "artifacts_count": 1,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
            _write_json(os.path.join(wd, "agent-runs", "modules.json"), manifest)
            blockers = sdd_mod._agent_run_blockers(
                wd, "modules", {"stages": {"modules": {"done": ["src/payments"]}}}
            )

        self.assertTrue(any(b.startswith("P0:") and "input alterado" in b for b in blockers))

    def test_agent_run_blockers_reprova_artefato_sha256_divergente(self):
        tmp, wd = self._wd()
        with tmp:
            input_path = os.path.join(wd, "agent-outputs", "modules-batch-01.txt")
            _write(input_path, "conteudo original do input")
            artifact_path = os.path.join(wd, "modules", "src-payments.md")
            _write(artifact_path, "conteudo do modulo gravado pelo merge")
            manifest = {
                "schema": "wiki-ai.agent-runs.v2",
                "stage": "modules",
                "runs": [
                    {
                        "stage": "modules",
                        "input": input_path,
                        "input_sha256": st_mod.sha256_file(input_path),
                        "input_bytes": os.path.getsize(input_path),
                        "agent": None,
                        "items": [
                            {
                                "item": "src/payments",
                                "artifacts": [
                                    {"path": artifact_path, "sha256": "f" * 64, "bytes": 1},
                                ],
                            }
                        ],
                        "items_count": 1,
                        "artifacts_count": 1,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
            _write_json(os.path.join(wd, "agent-runs", "modules.json"), manifest)
            blockers = sdd_mod._agent_run_blockers(
                wd, "modules", {"stages": {"modules": {"done": ["src/payments"]}}}
            )

        self.assertTrue(
            any(
                b.startswith("P0:") and "artefato alterado após o merge" in b and "src-payments.md" in b
                for b in blockers
            )
        )

    def test_agent_run_blockers_aceita_manifesto_v2_valido(self):
        tmp, wd = self._wd()
        with tmp:
            input_path = os.path.join(wd, "agent-outputs", "modules-batch-01.txt")
            _write(input_path, "conteudo do input")
            artifact_path = os.path.join(wd, "modules", "src-payments.md")
            _write(artifact_path, "conteudo do modulo")
            manifest = {
                "schema": "wiki-ai.agent-runs.v2",
                "stage": "modules",
                "runs": [
                    {
                        "stage": "modules",
                        "input": input_path,
                        "input_sha256": st_mod.sha256_file(input_path),
                        "input_bytes": os.path.getsize(input_path),
                        "agent": None,
                        "items": [
                            {
                                "item": "src/payments",
                                "artifacts": [
                                    {
                                        "path": artifact_path,
                                        "sha256": st_mod.sha256_file(artifact_path),
                                        "bytes": os.path.getsize(artifact_path),
                                    }
                                ],
                            }
                        ],
                        "items_count": 1,
                        "artifacts_count": 1,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
            _write_json(os.path.join(wd, "agent-runs", "modules.json"), manifest)
            blockers = sdd_mod._agent_run_blockers(
                wd, "modules", {"stages": {"modules": {"done": ["src/payments"]}}}
            )

        self.assertEqual(blockers, [])

    def test_merged_artifacts_retorna_caminhos_absolutos_normalizados(self):
        tmp, wd = self._wd()
        with tmp:
            artifact_path = os.path.join(wd, "modules", "src-payments.md")
            _write(artifact_path, "conteudo")
            manifest = {
                "schema": "wiki-ai.agent-runs.v2",
                "stage": "modules",
                "runs": [
                    {
                        "stage": "modules",
                        "input": os.path.join(wd, "agent-outputs", "in.txt"),
                        "input_sha256": "0" * 64,
                        "input_bytes": 0,
                        "agent": None,
                        "items": [
                            {
                                "item": "src/payments",
                                "artifacts": [{"path": artifact_path, "sha256": "0" * 64, "bytes": 1}],
                            }
                        ],
                        "items_count": 1,
                        "artifacts_count": 1,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
            _write_json(os.path.join(wd, "agent-runs", "modules.json"), manifest)
            result = sdd_mod.merged_artifacts(wd, "modules")

        self.assertIn(os.path.normpath(os.path.abspath(artifact_path)), result)

    def test_merged_artifacts_vazio_quando_manifesto_ausente(self):
        tmp, wd = self._wd()
        with tmp:
            result = sdd_mod.merged_artifacts(wd, "modules")
        self.assertEqual(result, set())

    # ---- Tarefa 2: auditar todos os módulos ---------------------------------

    def test_modules_glob_rule_reprova_arquivo_curto_demais(self):
        """Reprova continua acontecendo, mas agora pela checagem de
        integridade real (citações insuficientes) — "tamanho" deixou de ser
        motivo de reprovação (W8-T8.3, plano §3.1: "Score baseado em ...
        tamanho de texto apresentado como medida de entendimento")."""
        tmp, wd = self._wd()
        with tmp:
            path = os.path.join(wd, "modules", "curto.md")
            _write(path, "# Curto\n\nMuito pouco conteudo, sem citacao nenhuma.\n")
            rule = next(r for r in sdd_mod.RULES["modules"] if r.rel == "modules/*.md")
            result = sdd_mod._audit_file(path, rule, wd=wd)

        self.assertNotEqual(result["status"], "pass")
        self.assertTrue(any("citações insuficientes" in b for b in result["blockers"]))
        self.assertFalse(any("conteúdo insuficiente" in b for b in result["blockers"]))
        self.assertTrue(any("tamanho abaixo do sugerido" in a for a in result["avisos_cerimoniais"]))

    def test_modules_glob_rule_nao_reprova_mais_so_por_tamanho(self):
        """W8-T8.3 (plano §3.1): artefato curto porém com integridade real
        (citações válidas, sem placeholder) passa — tamanho vira aviso
        informativo (`avisos_cerimoniais`), nunca `blocker`."""
        tmp, wd = self._wd()
        with tmp:
            path = os.path.join(wd, "modules", "curto.md")
            _write(
                path,
                "\n".join(
                    [
                        "# Curto",
                        "",
                        f"- {GREEN} fluxo com erro, dependencia, entrada, saida e teste em src/a.py:1.",
                        f"- {GREEN} regra operacional cobre criterio e rastreabilidade em src/a.py:2.",
                        "- estado do processo cobre integracao entre etapas do fluxo.",
                        "- reimplementacao do teste cobre criterio de aceite do fluxo.",
                        "- dependencia externa registra entrada e saida do processo.",
                        "",
                    ]
                ),
            )
            rule = next(r for r in sdd_mod.RULES["modules"] if r.rel == "modules/*.md")
            result = sdd_mod._audit_file(path, rule, wd=wd)

        self.assertEqual(result["status"], "pass", result["blockers"])
        self.assertTrue(any("tamanho abaixo do sugerido" in a for a in result["avisos_cerimoniais"]))

    def test_modules_glob_rule_vale_para_todos_os_doc_levels(self):
        rule = next(r for r in sdd_mod.RULES["modules"] if r.rel == "modules/*.md")
        self.assertEqual(set(rule.levels), set(sdd_mod.LEVELS))
        self.assertEqual(rule.min_bytes, 600)
        self.assertEqual(rule.min_citations, 2)

    # ---- Tarefa 3a: MATRIX_MARKERS normalizado ------------------------------

    def test_matrix_error_aceita_cabecalho_acentuado(self):
        tmp, wd = self._wd()
        with tmp:
            unit = os.path.join(wd, "sdd", "specs", "checkout")
            _write(
                os.path.join(unit, "requirements.md"),
                "| Código | Regra de negócio | Requisito | Design | Tarefa |\n"
                "|---|---|---|---|---|\n"
                "| C1 | R1 | Q1 | D1 | T1 |\n",
            )
            _write(os.path.join(unit, "design.md"), "design\n")
            _write(os.path.join(unit, "tasks.md"), "tasks\n")
            error = sdd_mod._matrix_error(wd)

        self.assertIsNone(error)

    def test_matrix_error_ainda_reprova_quando_marcador_realmente_ausente(self):
        tmp, wd = self._wd()
        with tmp:
            unit = os.path.join(wd, "sdd", "specs", "checkout")
            _write(os.path.join(unit, "requirements.md"), "| Código | Regra | Evidência |\n|---|---|---|\n")
            _write(os.path.join(unit, "design.md"), "design\n")
            _write(os.path.join(unit, "tasks.md"), "tasks\n")
            error = sdd_mod._matrix_error(wd)

        self.assertIsNotNone(error)
        self.assertIn("requisito", error)

    # ---- Tarefa 3b: whitelist de rótulo de aresta Mermaid -------------------

    def test_edge_label_whitelist_aceita_quoted_e_unquoted(self):
        self.assertTrue(sdd_mod.MERMAID_EDGE_LINE_RE.match('A -->|"x"| B'))
        self.assertTrue(sdd_mod.MERMAID_EDGE_LINE_RE.match("A -->|x| B"))

    def test_flowchart_com_rotulo_de_aresta_quoted_nao_gera_erros(self):
        text = '```mermaid\nflowchart LR\n  A["Inicio"] -->|"rotulo"| B["Fim"]\n```'
        self.assertEqual([], sdd_mod._mermaid_errors(text))

    def test_flowchart_com_rotulo_de_aresta_unquoted_nao_gera_erros(self):
        text = '```mermaid\nflowchart LR\n  A["Inicio"] -->|rotulo| B["Fim"]\n```'
        self.assertEqual([], sdd_mod._mermaid_errors(text))

    def test_flowchart_com_rotulos_encadeados_continua_referenciando_nos_definidos(self):
        text = '```mermaid\nflowchart LR\n  A["Inicio"] --> B["Meio"] -->|"ok"| C["Fim"]\n```'
        self.assertEqual([], sdd_mod._mermaid_errors(text))

    def test_flowchart_ainda_rejeita_sintaxe_fora_do_whitelist(self):
        text = '```mermaid\nflowchart LR\n  A["Inicio"] -- texto --> B["Fim"]\n```'
        self.assertTrue(any("whitelist" in e for e in sdd_mod._mermaid_errors(text)))

    def test_flowchart_denso_continua_reprovando_mesmo_com_rotulos(self):
        text = (
            '```mermaid\nflowchart LR\n'
            '  A["A"] -->|"l1"| B["B"] -->|"l2"| C["C"] -->|"l3"| D["D"]\n```'
        )
        errors = sdd_mod._mermaid_errors(text)
        self.assertTrue(any("denso demais" in e for e in errors))

    # ---- Tarefa 4: canonização de synth --------------------------------------

    def test_synth_rules_apontam_para_sdd(self):
        rels = {r.rel for r in sdd_mod.RULES["synth"]}
        self.assertEqual(rels, {"sdd/confirmed.md", "sdd/inferred.md"})

    def test_synth_root_divergence_blockers_reprova_copia_divergente(self):
        tmp, wd = self._wd()
        with tmp:
            _write(os.path.join(wd, "sdd", "confirmed.md"), "conteudo canonico atual")
            _write(os.path.join(wd, "confirmed.md"), "conteudo divergente stale na raiz")
            blockers = sdd_mod._synth_root_divergence_blockers(wd)

        self.assertTrue(
            any(b.startswith("P0:") and "confirmed.md" in b and "sdd/confirmed.md" in b for b in blockers)
        )

    def test_synth_root_divergence_blockers_aceita_copias_identicas(self):
        tmp, wd = self._wd()
        with tmp:
            _write(os.path.join(wd, "sdd", "confirmed.md"), "mesmo conteudo\n")
            _write(os.path.join(wd, "confirmed.md"), "mesmo conteudo\n")
            blockers = sdd_mod._synth_root_divergence_blockers(wd)

        self.assertEqual(blockers, [])

    def test_synth_root_divergence_blockers_ok_quando_so_existe_canonico(self):
        tmp, wd = self._wd()
        with tmp:
            _write(os.path.join(wd, "sdd", "inferred.md"), "conteudo\n")
            blockers = sdd_mod._synth_root_divergence_blockers(wd)

        self.assertEqual(blockers, [])

    # ---- Tarefa 6: blockers acionáveis (caminho + medido + esperado) --------

    def test_aviso_cerimonial_tamanho_inclui_caminho_medido_e_sugerido(self):
        """W8-T8.3 (plano §3.1: "Score baseado em ... tamanho de texto
        apresentado como medida de entendimento"): tamanho insuficiente não é
        mais `blocker` — vira `avisos_cerimoniais`, mas continua acionável
        (caminho + medido + sugerido)."""
        tmp, wd = self._wd()
        with tmp:
            path = os.path.join(wd, "modules", "curto.md")
            _write(path, "curto\n")
            rule = sdd_mod.ArtifactRule("modules/curto.md", min_bytes=600, min_citations=2)
            result = sdd_mod._audit_file(path, rule, wd=wd)

        self.assertFalse(any("conteúdo insuficiente" in b for b in result["blockers"]))
        joined = "\n".join(result["avisos_cerimoniais"])
        self.assertIn("modules/curto.md", joined)
        self.assertIn("sugerido", joined)
        self.assertRegex(joined, r"\d")

    def test_blocker_escala_de_confianca_ausente_inclui_caminho_e_valores(self):
        tmp, wd = self._wd()
        with tmp:
            path = os.path.join(wd, "modules", "sem_confianca.md")
            _write(
                path,
                "# Modulo\n\n"
                "- fluxo operacional com erro, dependencia, entrada, saida e teste rastreavel em src/a.py:1.\n"
                "- regra operacional cobre criterio de aceite e rastreabilidade em src/a.py:2.\n",
            )
            rule = sdd_mod.ArtifactRule("modules/sem_confianca.md", min_bytes=20, min_citations=1)
            result = sdd_mod._audit_file(path, rule, wd=wd)

        blocker = next(b for b in result["blockers"] if "escala de confiança ausente" in b)
        self.assertIn("sem_confianca.md", blocker)
        self.assertIn("esperado", blocker)

    def test_blocker_detalhamento_operacional_insuficiente_inclui_caminho_e_valores(self):
        tmp, wd = self._wd()
        with tmp:
            path = os.path.join(wd, "modules", "raso.md")
            _write(path, f"# Modulo\n\n- {GREEN} Texto simples em src/a.py:1.\n")
            rule = sdd_mod.ArtifactRule("modules/raso.md", min_bytes=10, min_citations=1)
            result = sdd_mod._audit_file(path, rule, wd=wd)

        blocker = next(b for b in result["blockers"] if "detalhamento operacional insuficiente" in b)
        self.assertIn("raso.md", blocker)
        self.assertIn("esperado", blocker)
        self.assertRegex(blocker, r"\d")

    # ---- Dedup de blockers repetidos na agregação do stage ------------------

    def test_audit_stage_deduplica_blocker_de_artefato_alterado_citado_em_duas_runs(self):
        tmp, wd = self._wd()
        with tmp:
            artifact_path = os.path.join(wd, "modules", "policy.md")
            _write(artifact_path, "conteudo original do policy")
            stale_sha = st_mod.sha256_file(artifact_path)
            input_path = os.path.join(wd, "agent-outputs", "modules-batch-01.txt")
            _write(input_path, "conteudo do input")
            run_entry = {
                "stage": "modules",
                "input": input_path,
                "input_sha256": st_mod.sha256_file(input_path),
                "input_bytes": os.path.getsize(input_path),
                "agent": None,
                "items": [
                    {
                        "item": "src/policy",
                        "artifacts": [
                            {
                                "path": artifact_path,
                                "sha256": stale_sha,
                                "bytes": os.path.getsize(artifact_path),
                            }
                        ],
                    }
                ],
                "items_count": 1,
                "artifacts_count": 1,
                "created_at": "2026-01-01T00:00:00Z",
            }
            # Duas runs distintas (ex.: re-merge idempotente) citam o mesmo
            # artefato com o mesmo sha256 esperado.
            manifest = {
                "schema": "wiki-ai.agent-runs.v2",
                "stage": "modules",
                "runs": [json.loads(json.dumps(run_entry)), json.loads(json.dumps(run_entry))],
            }
            _write_json(os.path.join(wd, "agent-runs", "modules.json"), manifest)
            # Adultera o artefato depois de ambas as runs terem sido registradas.
            _write(artifact_path, "conteudo adulterado depois do merge")
            st = st_mod.load(wd)
            report = sdd_mod._audit_stage(wd, "modules", st)

        matches = [
            b for b in report["blockers"]
            if "artefato alterado após o merge" in b and "policy.md" in b
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(report["blockers"]), len(set(report["blockers"])))

    def test_audit_stage_nao_deduplica_blockers_de_artefatos_distintos_adulterados(self):
        tmp, wd = self._wd()
        with tmp:
            artifact_a = os.path.join(wd, "modules", "policy.md")
            artifact_b = os.path.join(wd, "modules", "pricing.md")
            _write(artifact_a, "conteudo original a")
            _write(artifact_b, "conteudo original b")
            sha_a = st_mod.sha256_file(artifact_a)
            sha_b = st_mod.sha256_file(artifact_b)
            input_path = os.path.join(wd, "agent-outputs", "modules-batch-01.txt")
            _write(input_path, "conteudo do input")
            manifest = {
                "schema": "wiki-ai.agent-runs.v2",
                "stage": "modules",
                "runs": [
                    {
                        "stage": "modules",
                        "input": input_path,
                        "input_sha256": st_mod.sha256_file(input_path),
                        "input_bytes": os.path.getsize(input_path),
                        "agent": None,
                        "items": [
                            {
                                "item": "src/policy",
                                "artifacts": [
                                    {"path": artifact_a, "sha256": sha_a, "bytes": os.path.getsize(artifact_a)}
                                ],
                            },
                            {
                                "item": "src/pricing",
                                "artifacts": [
                                    {"path": artifact_b, "sha256": sha_b, "bytes": os.path.getsize(artifact_b)}
                                ],
                            },
                        ],
                        "items_count": 2,
                        "artifacts_count": 2,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
            _write_json(os.path.join(wd, "agent-runs", "modules.json"), manifest)
            _write(artifact_a, "conteudo adulterado a")
            _write(artifact_b, "conteudo adulterado b")
            st = st_mod.load(wd)
            report = sdd_mod._audit_stage(wd, "modules", st)

        matches = [b for b in report["blockers"] if "artefato alterado após o merge" in b]
        self.assertEqual(len(matches), 2)
        self.assertTrue(any("policy.md" in b for b in matches))
        self.assertTrue(any("pricing.md" in b for b in matches))
        self.assertNotEqual(matches[0], matches[1])

    def test_audit_stage_score_reflete_contagem_de_blockers_unicos(self):
        tmp, wd = self._wd()
        with tmp:
            artifact_path = os.path.join(wd, "modules", "policy.md")
            _write(artifact_path, "conteudo original do policy")
            stale_sha = st_mod.sha256_file(artifact_path)
            input_path = os.path.join(wd, "agent-outputs", "modules-batch-01.txt")
            _write(input_path, "conteudo do input")
            run_entry = {
                "stage": "modules",
                "input": input_path,
                "input_sha256": st_mod.sha256_file(input_path),
                "input_bytes": os.path.getsize(input_path),
                "agent": None,
                "items": [
                    {
                        "item": "src/policy",
                        "artifacts": [
                            {"path": artifact_path, "sha256": stale_sha, "bytes": os.path.getsize(artifact_path)}
                        ],
                    }
                ],
                "items_count": 1,
                "artifacts_count": 1,
                "created_at": "2026-01-01T00:00:00Z",
            }
            manifest_dup = {
                "schema": "wiki-ai.agent-runs.v2",
                "stage": "modules",
                "runs": [json.loads(json.dumps(run_entry)) for _ in range(3)],
            }
            _write_json(os.path.join(wd, "agent-runs", "modules.json"), manifest_dup)
            _write(artifact_path, "conteudo adulterado depois do merge")
            st = st_mod.load(wd)
            report_dup = sdd_mod._audit_stage(wd, "modules", st)

        self.assertEqual(report_dup["score"], 0)
        self.assertEqual(len(report_dup["blockers"]), len(set(report_dup["blockers"])))


class CeremonialRulesW8T83Test(unittest.TestCase):
    """W8-T8.3 (plano-evolucao-wiki-ai.md §3.1 "Remoções obrigatórias"): a
    EXIGÊNCIA DE EXISTÊNCIA de ADR retroativo, histórias de usuário
    autogeradas, máquina de estados/ERD/diagrama de sequência vazio e
    arquivo intermediário (`modules/*.md`) deixou de bloquear/zerar score de
    estágio — vira `avisos_cerimoniais`. Cobre os dois caminhos de
    `_audit_stage` que precisam do mesmo tratamento: regra glob (`paths_for_rule`
    devolve lista vazia) e regra não-glob (`paths_for_rule` sempre devolve um
    candidato; a ausência só aparece depois, em `_audit_file`)."""

    def _wd(self) -> tuple[tempfile.TemporaryDirectory[str], str]:
        tmp = tempfile.TemporaryDirectory()
        repo = os.path.join(tmp.name, "repo")
        store = os.path.join(tmp.name, "store")
        os.makedirs(os.path.join(repo, "src"), exist_ok=True)
        _write(os.path.join(repo, "src", "a.py"), "\n".join(f"linha {i}" for i in range(1, 10)))
        wd = st_mod.workdir(store, repo)
        st_mod.init(wd, repo, topic=None)
        return tmp, wd

    def _healthy(self, extra_sections: str = "") -> str:
        bullets = "\n".join(
            f"- {GREEN} fluxo com erro, dependencia, entrada, saida, criterio e teste "
            f"rastreavel entre codigo e requisito em src/a.py:{i}."
            for i in range(1, 6)
        )
        return f"{extra_sections}\n\n{bullets}\n"

    def test_ceremonial_rules_set_cobre_os_seis_artefatos_do_3_1(self):
        self.assertEqual(
            sdd_mod.CEREMONIAL_RULES,
            frozenset({
                "sdd/adrs/*.md",
                "sdd/user-stories/*.md",
                "sdd/state-machines.md",
                "sdd/erd-complete.md",
                "sdd/sequences/*.md",
                "modules/*.md",
            }),
        )

    def _agent_runs_ok(self, wd: str, stage: str) -> None:
        input_path = os.path.join(wd, "agent-outputs", "in.txt")
        _write(input_path, "conteudo do input")
        manifest = {
            "schema": "wiki-ai.agent-runs.v2",
            "stage": stage,
            "runs": [{
                "stage": stage,
                "input": input_path,
                "input_sha256": st_mod.sha256_file(input_path),
                "input_bytes": os.path.getsize(input_path),
                "agent": None,
                "items": [],
                "items_count": 1,
                "artifacts_count": 0,
                "created_at": "2026-01-01T00:00:00Z",
            }],
        }
        _write_json(os.path.join(wd, "agent-runs", f"{stage}.json"), manifest)

    def test_rules_stage_passa_sem_adr_retroativo_e_sem_maquina_de_estados(self):
        """`sdd/adrs/*.md` (glob) e `sdd/state-machines.md` (não-glob) ausentes
        — nenhum dos dois bloqueia nem zera o score do estágio `rules`."""
        tmp, wd = self._wd()
        with tmp:
            st = st_mod.load(wd)
            st["sdd"] = {"doc_level": "completo"}
            st_mod.save(wd, st)
            _write(
                os.path.join(wd, "sdd", "domain.md"),
                self._healthy("# Domínio\n\n## Glossário\n\n## Regras de negócio\n\n## Lacunas"),
            )
            _write(
                os.path.join(wd, "sdd", "permissions.md"),
                self._healthy("# Permissões\n\n## Matriz\n\n## Lacunas"),
            )
            self._agent_runs_ok(wd, "rules")
            # sdd/adrs/*.md e sdd/state-machines.md ausentes de propósito.

            report = sdd_mod._audit_stage(wd, "rules", st_mod.load(wd))

        self.assertEqual(report["status"], "pass", report["blockers"])
        self.assertEqual(report["score"], 100)
        self.assertEqual(report["blockers"], [])
        joined_avisos = "\n".join(report["avisos_cerimoniais"])
        self.assertIn("sdd/adrs/*.md", joined_avisos)
        self.assertIn("sdd/state-machines.md", joined_avisos)

    def test_specs_stage_passa_sem_historias_de_usuario_autogeradas(self):
        """`sdd/user-stories/*.md` (glob, completo/detalhado) ausente não
        bloqueia nem zera o score do estágio `specs`."""
        tmp, wd = self._wd()
        with tmp:
            st = st_mod.load(wd)
            st["sdd"] = {"doc_level": "completo"}
            st["stages"]["specs"] = {"status": "in_progress", "pending": [], "done": ["checkout"], "blocked": [], "failed": []}
            st_mod.save(wd, st)
            root = os.path.join(wd, "sdd", "specs", "checkout")
            _write(os.path.join(root, "requirements.md"), self._healthy("# Requisitos"))
            _write(os.path.join(root, "design.md"), self._healthy("# Design"))
            _write(os.path.join(root, "tasks.md"), self._healthy("# Tarefas"))
            _write(
                os.path.join(wd, "sdd", "confidence-report.md"),
                self._healthy("# Confiança\n\n## Contagem\n\n## Rebaixados\n\n## Lacunas"),
            )
            _write(
                os.path.join(wd, "sdd", "traceability", "code-spec-matrix.md"),
                "# Matriz\n\n## Matriz\n\n| Código | Regra | Requisito | Design | Tarefa |\n|---|---|---|---|---|\n| C1 | R1 | Q1 | D1 | T1 |\n",
            )
            self._agent_runs_ok(wd, "specs")
            # sdd/user-stories/*.md ausente de propósito.

            report = sdd_mod._audit_stage(wd, "specs", st_mod.load(wd))

        self.assertFalse(any("user-stories" in b for b in report["blockers"]))
        self.assertIn("sdd/user-stories/*.md", "\n".join(report["avisos_cerimoniais"]))

    def test_modules_intermediario_ausente_nao_zera_score_quando_code_analysis_ok(self):
        """`modules/*.md` (glob, arquivo intermediário do próprio estágio)
        ausente não bloqueia nem zera o score de `modules` quando o artefato
        consolidado (`sdd/code-analysis.md`) está íntegro."""
        tmp, wd = self._wd()
        with tmp:
            st = st_mod.load(wd)
            st_mod.save(wd, st)
            _write(
                os.path.join(wd, "sdd", "code-analysis.md"),
                self._healthy("# Análise\n\n## Visão geral\n\n## Módulos\n\n## Fluxos\n\n## Riscos\n\n## Rastreabilidade"),
            )
            self._agent_runs_ok(wd, "modules")
            st = st_mod.load(wd)
            st.setdefault("stages", {})["modules"] = {"status": "done", "pending": [], "done": ["src/a"], "blocked": [], "failed": []}
            st_mod.save(wd, st)
            # modules/*.md ausente de propósito.

            report = sdd_mod._audit_stage(wd, "modules", st_mod.load(wd))

        self.assertFalse(any("modules/*.md" in b for b in report["blockers"]))
        self.assertIn("modules/*.md", "\n".join(report["avisos_cerimoniais"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
