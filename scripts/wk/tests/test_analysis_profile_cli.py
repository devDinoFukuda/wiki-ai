"""§ perfil de análise por sistema: `wk analyze`/`update`/`resume` resolvendo
`analysis.profile` em 3 camadas (`<repo>/.wiki-ai.json` < `profile.json[repo]
["perfil"]` < flags) e `wk profile show/set/unset`.

Dono exclusivo: `scripts/wk/cli.py` e `scripts/wk/tests/**` — mesma regra de
`test_run_chain_cli.py`. Roda contra o `analysis.profile` REAL (nenhum stub):
o módulo já existe no branch no momento em que esta suíte roda.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from wk import cli


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


CODIGO_APP = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def subtract(a, b):\n"
    "    return a - b\n"
)


class _RepoStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-profile-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo")
        _write(os.path.join(self.repo, "src", "app.py"), CODIGO_APP)

    def _analyze(self, *extra):
        return _run_cli([
            "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "structural", "--json", *extra,
        ])


# ---------------------------------------------------------------------------
# parser: cada flag nova chega íntegra no Namespace (sem rodar o pipeline)
# ---------------------------------------------------------------------------


class ParserFlagsTest(unittest.TestCase):
    def test_analyze_aceita_todas_as_flags_de_perfil(self):
        parser = cli._build_parser()
        ns = parser.parse_args([
            "analyze", "--repo", "R", "--store", "S",
            "--include", "src", "--include", "lib",
            "--exclude", "src/gen",
            "--objective", "cap-1", "--objective", "cap-2",
            "--max-reading-needs", "7",
            "--budget-bytes", "1000",
            "--budget-tokens", "200",
            "--profile", "perfil.json",
        ])
        self.assertEqual(ns.include, ["src", "lib"])
        self.assertEqual(ns.exclude, ["src/gen"])
        self.assertEqual(ns.objective, ["cap-1", "cap-2"])
        self.assertEqual(ns.max_reading_needs, 7)
        self.assertEqual(ns.budget_bytes, 1000)
        self.assertEqual(ns.budget_tokens, 200)
        self.assertEqual(ns.profile_file, "perfil.json")

    def test_update_e_resume_tambem_aceitam_as_flags_de_perfil(self):
        parser = cli._build_parser()
        for cmd in ("update", "resume"):
            ns = parser.parse_args([
                cmd, "--repo", "R", "--store", "S",
                "--include", "src", "--topic", "app",
                "--max-reading-needs", "3",
            ])
            self.assertEqual(ns.include, ["src"], cmd)
            self.assertEqual(ns.topic, "app", cmd)
            self.assertEqual(ns.max_reading_needs, 3, cmd)

    def test_profile_set_aceita_json_file_e_flags_individuais(self):
        parser = cli._build_parser()
        ns = parser.parse_args([
            "profile", "set", "--repo", "R", "--store", "S",
            "--json-file", "perfil.json", "--include", "src",
            "--budget-bytes", "500",
        ])
        self.assertEqual(ns.json_file, "perfil.json")
        self.assertEqual(ns.include, ["src"])
        self.assertEqual(ns.budget_bytes, 500)

    def test_profile_show_e_unset_exigem_repo(self):
        parser = cli._build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["profile", "show", "--store", "S"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["profile", "unset", "--store", "S"])


# ---------------------------------------------------------------------------
# `_profile_fields_from_flags`/`_profile_cli_overrides`: --topic é alias de
# --objective, e as duas listas se somam.
# ---------------------------------------------------------------------------


class ProfileFieldsFromFlagsTest(unittest.TestCase):
    def test_topic_soma_com_objective_sem_substituir(self):
        parser = cli._build_parser()
        ns = parser.parse_args([
            "analyze", "--repo", "R", "--objective", "cap-1", "--topic", "app",
        ])
        fields = cli._profile_fields_from_flags(ns)
        self.assertEqual(fields["objectives"], ["cap-1", "app"])

    def test_budget_bytes_e_tokens_viram_chaves_do_perfil(self):
        parser = cli._build_parser()
        ns = parser.parse_args([
            "analyze", "--repo", "R", "--budget-bytes", "111", "--budget-tokens", "222",
        ])
        fields = cli._profile_fields_from_flags(ns)
        self.assertEqual(fields["budget"], {"max_bytes": 111, "max_tokens": 222})

    def test_sem_nenhuma_flag_devolve_dict_vazio(self):
        parser = cli._build_parser()
        ns = parser.parse_args(["analyze", "--repo", "R"])
        self.assertEqual(cli._profile_fields_from_flags(ns), {})
        self.assertEqual(cli._profile_cli_overrides(ns), {})


# ---------------------------------------------------------------------------
# `wk profile show/set/unset`
# ---------------------------------------------------------------------------


class ProfileCommandTest(_RepoStoreTestCase):
    def test_show_sem_nada_gravado_e_default(self):
        code, out, err = _run_cli([
            "profile", "show", "--repo", self.repo, "--store", self.store, "--json",
        ])
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["command"], "profile show")
        self.assertEqual(payload["operation_status"], "succeeded")
        detalhe = payload["summary"]["detail"]
        self.assertIsNone(detalhe["camadas"]["repo"])
        self.assertIsNone(detalhe["camadas"]["store"])
        self.assertTrue(detalhe["resolvido"]["is_default"])
        self.assertEqual(detalhe["origem"], [])

    def test_set_grava_arquivo_e_show_reflete(self):
        code, out, err = _run_cli([
            "profile", "set", "--repo", self.repo, "--store", self.store,
            "--include", "src", "--exclude", "src/gen", "--max-reading-needs", "4",
            "--json",
        ])
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "succeeded")
        gravado = payload["summary"]["detail"]["perfil_gravado"]
        self.assertEqual(gravado["include"], ["src"])
        self.assertEqual(gravado["exclude"], ["src/gen"])
        self.assertEqual(gravado["max_reading_needs"], 4)

        # arquivo em disco: profile.json[repo_key]["perfil"] (§10.3)
        profile_all = cli._load_analysis_profile(self.store)
        entry = profile_all[cli._repo_key(self.repo)]
        self.assertEqual(entry["perfil"]["include"], ["src"])
        self.assertNotIn("source", entry["perfil"])  # `AnalysisProfile.to_dict()` nunca emite `source`

        code2, out2, err2 = _run_cli([
            "profile", "show", "--repo", self.repo, "--store", self.store, "--json",
        ])
        self.assertEqual(code2, 0, out2)
        payload2 = json.loads(out2)
        detalhe2 = payload2["summary"]["detail"]
        self.assertEqual(detalhe2["camadas"]["store"]["include"], ["src"])
        self.assertEqual(detalhe2["resolvido"]["source"], ["store"])
        self.assertFalse(detalhe2["resolvido"]["is_default"])

    def test_show_apos_set_nao_quebra_com_profileerror_de_source(self):
        """Round-trip `to_dict()` -> `resolve_profile()` sem `ProfileError`
        (`AnalysisProfile.to_dict()` nunca emite `source`, e `from_mapping`
        tolera `source` na entrada quando presente — round-trip fechado no
        próprio `analysis.profile`)."""
        code, _out, _err = _run_cli([
            "profile", "set", "--repo", self.repo, "--store", self.store,
            "--include", "src", "--json",
        ])
        self.assertEqual(code, 0)
        code2, out2, _err2 = _run_cli([
            "profile", "show", "--repo", self.repo, "--store", self.store, "--json",
        ])
        self.assertEqual(code2, 0, out2)
        self.assertNotIn("ProfileError", out2)

    def test_set_json_invalido_bloqueia_exit2(self):
        code, out, err = _run_cli([
            "profile", "set", "--repo", self.repo, "--store", self.store,
            "--max-reading-needs", "-1", "--json",
        ])
        self.assertEqual(code, 2, out)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertEqual(payload["pending"][0]["tipo"], "perfil_invalido")

    def test_unset_remove_e_e_noop_na_segunda_vez(self):
        _run_cli([
            "profile", "set", "--repo", self.repo, "--store", self.store,
            "--include", "src", "--json",
        ])
        code, out, err = _run_cli([
            "profile", "unset", "--repo", self.repo, "--store", self.store, "--json",
        ])
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "succeeded")
        self.assertTrue(payload["summary"]["detail"]["removido"])

        code2, out2, err2 = _run_cli([
            "profile", "unset", "--repo", self.repo, "--store", self.store, "--json",
        ])
        self.assertEqual(code2, 0, out2)
        payload2 = json.loads(out2)
        self.assertEqual(payload2["operation_status"], "noop")
        self.assertFalse(payload2["summary"]["detail"]["removido"])

        profile_all = cli._load_analysis_profile(self.store)
        self.assertNotIn("perfil", profile_all.get(cli._repo_key(self.repo), {}))


# ---------------------------------------------------------------------------
# `ProfileError` de `.wiki-ai.json` -> blocked/exit 2, sem rodar análise
# ---------------------------------------------------------------------------


class ProfileErrorBlocksAnalysisTest(_RepoStoreTestCase):
    def test_wiki_ai_json_invalido_bloqueia_analyze_sem_rodar_nada(self):
        _write(os.path.join(self.repo, ".wiki-ai.json"), "{ nao e json")
        code, out, err = self._analyze()
        self.assertEqual(code, 2, out)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertEqual(payload["pending"][0]["tipo"], "perfil_invalido")
        # nenhum runtime.db foi criado — nenhuma tarefa nasceu
        self.assertFalse(os.path.exists(cli._runtime_db_path(self.store)))
        # next_action aponta para `wk profile show`
        argv = payload["next_actions"][0]["argv"]
        self.assertEqual(argv[:3], ["wk", "profile", "show"])

    def test_wiki_ai_json_invalido_bloqueia_update_e_resume_tambem(self):
        code, _out, _err = self._analyze()
        self.assertIn(code, (0, 3), _out)

        _write(os.path.join(self.repo, ".wiki-ai.json"), "{ nao e json")
        for cmd in ("update", "resume"):
            code, out, err = _run_cli([
                cmd, "--repo", self.repo, "--store", self.store,
                "--mode", "structural", "--json",
            ])
            self.assertEqual(code, 2, f"{cmd}: {out}")
            payload = json.loads(out)
            self.assertEqual(payload["pending"][0]["tipo"], "perfil_invalido", cmd)

    def test_doctor_valida_wiki_ai_json_do_repo(self):
        _write(os.path.join(self.repo, ".wiki-ai.json"), "{ nao e json")
        code, out, err = _run_cli([
            "doctor", "--store", self.store, "--repo", self.repo, "--json",
        ])
        self.assertEqual(code, 2, out)
        payload = json.loads(out)
        perfil = payload["summary"]["detail"]["perfil"]
        self.assertFalse(perfil["validado"])
        self.assertIn(".wiki-ai.json", perfil["caminho"])
        self.assertIn("perfil", payload["summary"]["detail"]["bloqueios"])

    def test_doctor_repo_valido_ou_ausente_de_wiki_ai_json_nao_bloqueia_por_perfil(self):
        code, out, err = _run_cli([
            "doctor", "--store", self.store, "--repo", self.repo, "--json",
        ])
        payload = json.loads(out)
        self.assertNotIn("perfil", payload["summary"]["detail"]["bloqueios"])
        self.assertTrue(payload["summary"]["detail"]["perfil"]["validado"])


# ---------------------------------------------------------------------------
# ordem repo < store < cli comprovada por `source`
# ---------------------------------------------------------------------------


class LayerOrderTest(_RepoStoreTestCase):
    def test_source_reflete_as_camadas_que_participaram(self):
        # só repo
        _write(
            os.path.join(self.repo, ".wiki-ai.json"),
            json.dumps({"include": ["src"]}),
        )
        code, out, _err = _run_cli([
            "profile", "show", "--repo", self.repo, "--store", self.store, "--json",
        ])
        self.assertEqual(code, 0, out)
        detalhe = json.loads(out)["summary"]["detail"]
        self.assertEqual(detalhe["resolvido"]["source"], [f"repo:{'.wiki-ai.json'}"])

        # + store
        _run_cli([
            "profile", "set", "--repo", self.repo, "--store", self.store,
            "--exclude", "src/gen", "--json",
        ])
        code, out, _err = _run_cli([
            "profile", "show", "--repo", self.repo, "--store", self.store, "--json",
        ])
        detalhe = json.loads(out)["summary"]["detail"]
        self.assertEqual(detalhe["resolvido"]["source"], [f"repo:{'.wiki-ai.json'}", "store"])
        # o include do repo sobrevive (merge de tuplas, não substituição)
        self.assertEqual(detalhe["resolvido"]["include"], 1)
        self.assertEqual(detalhe["resolvido"]["exclude"], 1)

    def test_cli_override_aparece_no_scope_profile_do_analyze(self):
        code, out, err = self._analyze("--exclude", "src/gen")
        self.assertIn(code, (0, 3), out)
        payload = json.loads(out)
        self.assertIn("cli", payload["scope"]["profile"]["source"])
        self.assertEqual(payload["scope"]["profile"]["exclude"], 1)


# ---------------------------------------------------------------------------
# `--topic` filtra objetivos (alias de --objective)
# ---------------------------------------------------------------------------


class TopicFiltersObjectivesTest(_RepoStoreTestCase):
    def test_topic_reduz_objetivos_do_chain(self):
        code_full, out_full, _err = self._analyze()
        self.assertIn(code_full, (0, 3), out_full)
        full_ids = json.loads(out_full)["summary"]["detail"]["chain"]["objective_ids"]
        self.assertGreaterEqual(len(full_ids), 1)

        code, out, err = _run_cli([
            "analyze", "--repo", self.repo, "--store",
            os.path.join(self.tmp, "store2"), "--mode", "structural", "--json",
            "--topic", "app",
        ])
        self.assertIn(code, (0, 3), out)
        payload = json.loads(out)
        filtered_ids = payload["summary"]["detail"]["chain"]["objective_ids"]
        self.assertLessEqual(len(filtered_ids), len(full_ids))
        self.assertEqual(payload["scope"]["profile"]["objectives"], 1)


# ---------------------------------------------------------------------------
# perfil efetivo persistido + "perfil alterado" sinalizado em update/resume
# ---------------------------------------------------------------------------


class PerfilEfetivoTest(_RepoStoreTestCase):
    def test_perfil_efetivo_persistido_apos_analyze(self):
        code, out, err = self._analyze("--include", "src")
        self.assertIn(code, (0, 3), out)
        profile_all = cli._load_analysis_profile(self.store)
        entry = profile_all[cli._repo_key(self.repo)]
        self.assertIn("perfil_efetivo", entry)
        self.assertIn("perfil_efetivo_hash", entry)
        self.assertEqual(entry["perfil_efetivo"]["include"], ["src"])
        self.assertEqual(
            entry["perfil_efetivo_hash"], cli._profile_hash(entry["perfil_efetivo"]),
        )

    def test_update_sinaliza_perfil_alterado_quando_store_muda(self):
        code, out, err = self._analyze()
        self.assertIn(code, (0, 3), out)

        _run_cli([
            "profile", "set", "--repo", self.repo, "--store", self.store,
            "--max-reading-needs", "9", "--json",
        ])
        # força delta para não sair cedo pelo ramo noop
        _write(os.path.join(self.repo, "src", "novo.py"), "def nova():\n    return 1\n")

        code2, out2, err2 = _run_cli([
            "update", "--repo", self.repo, "--store", self.store,
            "--mode", "structural", "--json",
        ])
        self.assertIn(code2, (0, 3), out2)
        payload2 = json.loads(out2)
        tipos = [p.get("tipo") for p in payload2["pending"]]
        self.assertIn("perfil_alterado", tipos)

    def test_update_sem_mudanca_de_perfil_nao_sinaliza(self):
        code, out, err = self._analyze()
        self.assertIn(code, (0, 3), out)
        _write(os.path.join(self.repo, "src", "novo.py"), "def nova():\n    return 1\n")

        code2, out2, err2 = _run_cli([
            "update", "--repo", self.repo, "--store", self.store,
            "--mode", "structural", "--json",
        ])
        self.assertIn(code2, (0, 3), out2)
        payload2 = json.loads(out2)
        tipos = [p.get("tipo") for p in payload2["pending"]]
        self.assertNotIn("perfil_alterado", tipos)


# ---------------------------------------------------------------------------
# compat: sem flags e sem perfil, envelope/exit code idênticos ao histórico
# ---------------------------------------------------------------------------


class CompatWithoutProfileTest(_RepoStoreTestCase):
    def test_analyze_sem_flags_de_perfil_tem_scope_profile_default(self):
        code, out, err = self._analyze()
        self.assertIn(code, (0, 3), out)
        payload = json.loads(out)
        perfil = payload["scope"]["profile"]
        self.assertTrue(perfil["is_default"])
        self.assertEqual(perfil["source"], [])
        self.assertEqual(perfil["include"], 0)
        self.assertEqual(perfil["exclude"], 0)
        self.assertEqual(perfil["objectives"], 0)

    def test_status_com_repo_traz_scope_profile_default_sem_perfil_configurado(self):
        code, _out, _err = self._analyze()
        self.assertIn(code, (0, 3), _out)
        code2, out2, _err2 = _run_cli([
            "status", "--repo", self.repo, "--store", self.store, "--json",
        ])
        self.assertEqual(code2, 0, out2)
        payload2 = json.loads(out2)
        self.assertTrue(payload2["scope"]["profile"]["is_default"])
        self.assertNotIn("perfil_erro", payload2["summary"]["detail"])


# ---------------------------------------------------------------------------
# integração com os pontos reais do pipeline (snapshot/inventory/investigation)
# ---------------------------------------------------------------------------


class PipelineIntegrationTest(_RepoStoreTestCase):
    def test_exclude_remove_arquivo_do_snapshot_e_fica_contado(self):
        code, out, err = self._analyze("--exclude", "src/app.py")
        self.assertIn(code, (0, 3), out)
        payload = json.loads(out)
        snapshot_id = payload["summary"]["detail"]["chain"]  # sanity: chain existe
        self.assertIsInstance(snapshot_id, dict)

        manifest_dir = os.path.join(self.store, ".analysis", "snapshots")
        files = os.listdir(manifest_dir)
        self.assertEqual(len(files), 1)
        with open(os.path.join(manifest_dir, files[0]), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["excluded_by_profile"], 1)
        self.assertEqual(manifest["exclude_patterns"], ["src/app.py"])
        self.assertEqual(manifest["files"], [])

    def test_objective_filtra_e_expoe_perfil_accounting(self):
        code, out, err = self._analyze("--objective", "app")
        self.assertIn(code, (0, 3), out)
        payload = json.loads(out)
        accounting = payload["summary"]["detail"]["escopo_efetivo"]["perfil_accounting"]
        self.assertIsNotNone(accounting)
        self.assertIn("orphan_symbols_suppressed_by_profile", accounting)

    def test_sem_filtro_de_objective_perfil_accounting_e_none(self):
        code, out, err = self._analyze()
        self.assertIn(code, (0, 3), out)
        payload = json.loads(out)
        accounting = payload["summary"]["detail"]["escopo_efetivo"]["perfil_accounting"]
        self.assertIsNone(accounting)


# ---------------------------------------------------------------------------
# Onda F3/item 3 (achado real, reproduzido): `--objective`/`--topic` sem
# NENHUMA correspondência não pode virar `succeeded`/`complete` vazio.
# ---------------------------------------------------------------------------


class FiltroSemCorrespondenciaTest(_RepoStoreTestCase):
    def test_analyze_objective_inexistente_bloqueia_exit2_sem_gravar_revisao(self):
        code, out, err = self._analyze("--objective", "NAO_EXISTE")
        self.assertEqual(code, 2, out)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertEqual(payload["knowledge_status"], "not_applicable")
        self.assertEqual(payload["pending"][0]["tipo"], "filtro_sem_correspondencia")
        self.assertEqual(payload["summary"]["padroes"], ["NAO_EXISTE"])
        self.assertIn("objetivos_disponiveis", payload["summary"])

        # nenhuma revisão foi gravada: nem `last_revision_id` nem
        # `perfil_efetivo` mudaram no profile.json (a chave nem existe —
        # este comando bloqueou ANTES de qualquer persistência).
        profile_all = cli._load_analysis_profile(self.store)
        self.assertNotIn(cli._repo_key(self.repo), profile_all)

        # nenhum snapshot manifest foi gravado
        manifest_dir = os.path.join(self.store, ".analysis", "snapshots")
        self.assertFalse(os.path.isdir(manifest_dir) and os.listdir(manifest_dir))

    def test_repo_so_com_orfaos_lista_objetivos_de_grupo_de_orfaos(self):
        """Reprodução direta (achado seguinte à 1ª correção): `self.repo`
        (só `src/app.py`, sem grafo suficiente para formar CAPACIDADE — o
        caso mais comum em `--mode structural`) tinha `capacidades_disponiveis`
        vazio mesmo havendo objetivo de grupo de órfãos disponível de
        verdade, e `next_actions[0].argv` era sempre `None`. Agora a lista
        inclui o(s) objetivo(s) de órfão REAIS e `argv` é um comando
        executável."""
        code, out, err = self._analyze("--objective", "NAO_EXISTE")
        self.assertEqual(code, 2, out)
        payload = json.loads(out)

        disponiveis = payload["summary"]["objetivos_disponiveis"]
        self.assertTrue(disponiveis, "nenhum objetivo disponível listado — regressão do achado")
        self.assertTrue(all(d["tipo"] == "orphan_group" for d in disponiveis), disponiveis)
        primeiro_id = disponiveis[0]["id"]

        acoes = payload["pending"][0]["acoes"][0]
        self.assertIn(primeiro_id, acoes)
        self.assertNotIn("nenhum objetivo", acoes)

        next_action = payload["next_actions"][0]
        self.assertIsNotNone(next_action["argv"], "next_actions[0].argv continua None — achado não corrigido")
        self.assertEqual(next_action["argv"], [
            "wk", "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "structural", "--objective", primeiro_id,
        ])

        # a sugestão é executável de verdade: repetir com o id sugerido
        # não bloqueia mais por `filtro_sem_correspondencia`.
        code2, out2, err2 = self._analyze("--objective", primeiro_id)
        self.assertIn(code2, (0, 3), out2)
        payload2 = json.loads(out2)
        self.assertNotEqual(payload2.get("operation_status"), "blocked")

    def test_topic_alias_tambem_bloqueia_sem_correspondencia(self):
        code, out, err = self._analyze("--topic", "NAO_EXISTE_TAMBEM")
        self.assertEqual(code, 2, out)
        payload = json.loads(out)
        self.assertEqual(payload["pending"][0]["tipo"], "filtro_sem_correspondencia")

    def test_objective_existente_nao_bloqueia(self):
        code, out, err = self._analyze("--objective", "app")
        self.assertIn(code, (0, 3), out)
        payload = json.loads(out)
        self.assertNotEqual(payload["operation_status"], "blocked")

    def test_update_com_objective_inexistente_tambem_bloqueia(self):
        code, out, err = self._analyze()
        self.assertIn(code, (0, 3), out)
        _write(os.path.join(self.repo, "src", "novo.py"), "def nova():\n    return 1\n")
        code2, out2, err2 = _run_cli([
            "update", "--repo", self.repo, "--store", self.store, "--mode", "structural",
            "--json", "--objective", "NAO_EXISTE",
        ])
        self.assertEqual(code2, 2, out2)
        payload2 = json.loads(out2)
        self.assertEqual(payload2["pending"][0]["tipo"], "filtro_sem_correspondencia")

    def test_blocked_filtro_sem_correspondencia_sugere_id_disponivel(self):
        """`_blocked_filtro_sem_correspondencia` com uma lista de objetivos
        disponíveis não vazia: `next_actions` sugere `--objective <id real>`
        com `--mode` incluído (verificado direto na função, com uma lista
        pré-computada — não depende de `capabilities.discover` formar uma
        capacidade de verdade)."""
        from analysis.profile import AnalysisProfile

        profile = AnalysisProfile.from_mapping({"objectives": ["zzz-nao-existe"]}, source="cli")
        objetivos_disponiveis = [
            {"id": "cap-real-1", "nome": "Capacidade Real", "tipo": "capability"},
            {"id": "orfaos@modulo", "nome": "orfaos@modulo", "tipo": "orphan_group"},
        ]
        payload = cli._blocked_filtro_sem_correspondencia(
            command="analyze", store_root=self.store, repo_abs=self.repo,
            profile=profile, mode="structural", objetivos_disponiveis=objetivos_disponiveis,
        )
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertEqual(payload["summary"]["objetivos_disponiveis"], objetivos_disponiveis)
        argv = payload["next_actions"][0]["argv"]
        self.assertEqual(argv, [
            "wk", "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "structural", "--objective", "cap-real-1",
        ])

    def test_blocked_filtro_sem_correspondencia_sem_nenhum_objetivo_no_repo(self):
        """Só usa a mensagem "nenhum objetivo encontrado" quando a lista
        pré-computada está REALMENTE vazia (repositório sem capacidade nem
        órfão nenhum) — nunca como efeito colateral do próprio filtro."""
        from analysis.profile import AnalysisProfile

        profile = AnalysisProfile.from_mapping({"objectives": ["zzz-nao-existe"]}, source="cli")
        payload = cli._blocked_filtro_sem_correspondencia(
            command="analyze", store_root=self.store, repo_abs=self.repo,
            profile=profile, mode="structural", objetivos_disponiveis=[],
        )
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertEqual(payload["summary"]["objetivos_disponiveis"], [])
        self.assertIsNone(payload["next_actions"][0]["argv"])
        self.assertIn("nenhum objetivo", payload["next_actions"][0]["motivo"])


# ---------------------------------------------------------------------------
# Onda F3/item 1 (achado da auditoria + reprodução real): race
# load->mutate->save em `profile.json` sem lock — 2 PROCESSOS reais
# concorrentes, escrevendo REPOS DIFERENTES do MESMO `--store`, provando que
# `_update_analysis_profile_entry`/`_ProfileTransaction` (cli.py) serializam
# a leitura-modificação-escrita e nenhuma escrita se perde.
# ---------------------------------------------------------------------------


def _subprocess_env() -> dict:
    """Mesmo `PYTHONPATH` (repo root + scripts/) usado pelo gate desta
    suíte, herdado do processo atual quando presente — para o subprocesso
    conseguir `import wk`/`analysis`/`runtime` sem depender de instalação."""
    wk_dir = os.path.dirname(os.path.abspath(cli.__file__))
    scripts_dir = os.path.dirname(wk_dir)
    repo_root = os.path.dirname(scripts_dir)
    env = dict(os.environ)
    extra = os.pathsep.join([repo_root, scripts_dir])
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = extra if not existing else f"{extra}{os.pathsep}{existing}"
    return env


_WRITE_ENTRY_SCRIPT = textwrap.dedent("""
    import sys
    import time

    from wk import cli

    repo_key = sys.argv[1]
    store_root = sys.argv[2]
    delay_s = float(sys.argv[3])


    def mutate(fresh):
        # alarga deliberadamente a janela entre `load` e `save` desta
        # transação, para GARANTIR sobreposição real com o outro processo
        # (sem isto, o SO poderia intercalar os dois processos sem nunca
        # sobrepor as seções críticas, e o teste não provaria nada).
        time.sleep(delay_s)
        entry = dict(fresh)
        entry["marker"] = repo_key
        return entry


    cli._update_analysis_profile_entry(store_root, repo_key, mutate)
""")


class ProfileConcurrentWriteTest(_RepoStoreTestCase):
    def test_dois_processos_concorrentes_em_repos_diferentes_nao_perdem_escrita(self):
        script_path = os.path.join(self.tmp, "_write_profile_entry.py")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(_WRITE_ENTRY_SCRIPT)

        env = _subprocess_env()
        p1 = subprocess.Popen(
            [sys.executable, script_path, "repoA", self.store, "0.6"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        p2 = subprocess.Popen(
            [sys.executable, script_path, "repoB", self.store, "0.6"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out1, err1 = p1.communicate(timeout=30)
        out2, err2 = p2.communicate(timeout=30)
        self.assertEqual(p1.returncode, 0, err1.decode("utf-8", "replace"))
        self.assertEqual(p2.returncode, 0, err2.decode("utf-8", "replace"))

        profile_all = cli._load_analysis_profile(self.store)
        self.assertIn("repoA", profile_all, profile_all)
        self.assertIn("repoB", profile_all, profile_all)
        self.assertEqual(profile_all["repoA"]["marker"], "repoA")
        self.assertEqual(profile_all["repoB"]["marker"], "repoB")

    def test_dez_processos_concorrentes_repos_distintos_nenhuma_escrita_perdida(self):
        script_path = os.path.join(self.tmp, "_write_profile_entry.py")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(_WRITE_ENTRY_SCRIPT)

        env = _subprocess_env()
        n = 10
        procs = [
            subprocess.Popen(
                [sys.executable, script_path, f"repo{i}", self.store, "0.15"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for i in range(n)
        ]
        results = [p.communicate(timeout=30) for p in procs]
        for i, (p, (_out, err)) in enumerate(zip(procs, results)):
            self.assertEqual(p.returncode, 0, f"repo{i}: {err.decode('utf-8', 'replace')}")

        profile_all = cli._load_analysis_profile(self.store)
        for i in range(n):
            key = f"repo{i}"
            self.assertIn(key, profile_all, profile_all)
            self.assertEqual(profile_all[key]["marker"], key)


# ---------------------------------------------------------------------------
# Onda F3/item 4: `trust_profile_cells=True` incondicional em
# `_objective_from_store_dict` — objetivo persistido com célula
# `not_applicable`/`justification_source` do PERFIL é lido de volta (por
# `wk status`) sem `MatrixIntegrityError`.
# ---------------------------------------------------------------------------


class TrustProfileCellsTest(_RepoStoreTestCase):
    def test_objetivo_com_exclusao_de_perfil_e_lido_por_status_sem_rejeicao(self):
        from runtime import tasks as rt_tasks

        profile_path = os.path.join(self.tmp, "perfil.json")
        with open(profile_path, "w", encoding="utf-8") as fh:
            json.dump(
                {"failure_families_excluded": {"mensageria": "sistema síncrono, sem fila"}}, fh,
            )

        code, out, err = self._analyze("--profile", profile_path)
        self.assertIn(code, (0, 3), out)

        store_obj = rt_tasks.TaskStore.open(cli._runtime_db_path(self.store))
        try:
            latest = cli._latest_tasks_by_objective(store_obj)
        finally:
            store_obj.close()
        self.assertTrue(latest, "nenhum objetivo persistido — cenário não montado")

        # o cenário PRECISA ter produzido ao menos uma célula excluída pelo
        # perfil (`not_applicable` + `justification_source`); senão o teste
        # não provaria nada sobre `trust_profile_cells`.
        tem_exclusao_perfil = any(
            c.get("state") == "not_applicable" and c.get("justification_source")
            for t in latest.values()
            for c in ((t.objective.get("matrix") or {}).get("cells") or [])
        )
        self.assertTrue(tem_exclusao_perfil, "nenhuma célula not_applicable/justification_source encontrada")

        # nenhuma reconstrução via `_objective_from_store_dict` (o caminho
        # que `cmd_status` usa) é rejeitada por `MatrixIntegrityError` —
        # mesmo tendo célula com proveniência de perfil. Deixa a exceção
        # propagar (falha o teste com o traceback real) em vez de um
        # try/except que só reformula a mensagem.
        reconstruidos = {oid: cli._objective_from_store_dict(t.objective) for oid, t in latest.items()}
        self.assertTrue(reconstruidos)  # MatrixIntegrityError, se houvesse, já teria propagado acima

        # `wk status --repo` usa EXATAMENTE este caminho dentro de
        # `except Exception: unmet = []` (cmd_status) — comparar bit-a-bit
        # contra a reconstrução direta descarta um fallback silencioso
        # mascarando uma rejeição (se `from_dict` tivesse lançado dentro do
        # `except`, `lacunas_status` ficaria vazio para este objective_id).
        code2, out2, err2 = _run_cli([
            "status", "--repo", self.repo, "--store", self.store, "--json",
        ])
        self.assertEqual(code2, 0, out2)
        payload2 = json.loads(out2)
        lacunas_status = {
            l["objective_id"]: l["pendencias"] for l in payload2["summary"]["detail"]["lacunas"]
        }
        algum_com_pendencia = False
        for oid, obj in reconstruidos.items():
            unmet_direto = obj.unmet_obligations()
            if unmet_direto:
                algum_com_pendencia = True
                self.assertIn(oid, lacunas_status, f"{oid} sumiu de `wk status` (fallback silencioso?)")
                self.assertEqual(lacunas_status[oid], unmet_direto)
        self.assertTrue(algum_com_pendencia, "nenhum objetivo com pendência — comparação não provou nada")


if __name__ == "__main__":
    unittest.main()
