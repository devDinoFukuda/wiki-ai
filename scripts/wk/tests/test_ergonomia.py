"""Testes S6: ergonomia/ambiente do CLI `wk`.

Cobre bugs confirmados num teste de ponta a ponta:
  - `wk ingest codebase <repo>` colidia com a operação `ingest codebase` da
    skill (pipeline `wk code ... surface`) e só falhava com erro genérico de
    argparse ("--source-type/--origin required"), sem explicar o real
    problema nem apontar o comando certo.
  - o agente gastou ~10 chamadas só descobrindo o ambiente (--help repetido,
    `ls`, `git log`, `jq --version`) e ainda assim rodou comandos em
    PowerShell (sintaxe incompatível com o CLI/skill, que assumem Git Bash).
  - `check` validava permissão só quando recebia --store/--repo, e ficava
    silencioso (sem sinalizar "não verificado") quando não recebia.

Rodar:
    python -m unittest discover -s wk/tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from wk import cli


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class IngestCodebaseGuardTests(unittest.TestCase):
    def test_ingest_codebase_bloqueia_e_cita_wk_code_surface(self):
        code, _out, err = _run(["ingest", "codebase", "algum-repo", "--topic", "demo"])
        self.assertNotEqual(code, 0)
        data = json.loads(err)
        self.assertIn("wk code", data["comando_correto"])
        self.assertIn("surface", data["comando_correto"])
        self.assertIn("wk code", err)
        self.assertIn("surface", err)

    def test_ingest_repo_bloqueia(self):
        code, _out, err = _run(["ingest", "repo", "algum-repo"])
        self.assertNotEqual(code, 0)
        self.assertIn("wk code", err)

    def test_ingest_repositorio_e_repository_bloqueiam(self):
        for palavra in ("repositorio", "repository"):
            code, _out, err = _run(["ingest", palavra])
            self.assertNotEqual(code, 0, palavra)
            self.assertIn("wk code", err, palavra)

    def test_arquivo_codebase_md_real_continua_ingerivel(self):
        tmp = tempfile.mkdtemp(prefix="wk_ingest_guard_src_")
        store = tempfile.mkdtemp(prefix="wk_ingest_guard_store_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        code, _out, err = _run(["store", "init", store])
        self.assertEqual(code, 0, err)

        path = os.path.join(tmp, "codebase.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# conteudo real\n")

        # W8: --source-type/--origin são do legado (`ingest-legacy`); `wk
        # ingest` (novo) é o composto extração+correlação em knowledge.db.
        code, out, err = _run(
            ["ingest-legacy", path, "--source-type", "human-doc", "--origin", "teste",
             "--topic", "demo", "--store", store]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertTrue(data["path"].startswith("inbox/"))

    def test_arquivo_chamado_exatamente_codebase_nao_e_bloqueado_pela_guarda(self):
        # a guarda casa a palavra reservada, mas só bloqueia quando o
        # positional NÃO é um caminho existente — aqui existe, então quem
        # rejeita é o próprio `ingest` (formato sem extensão suportada), não
        # a guarda de palavra reservada (o erro não deve citar `wk code`).
        tmp = tempfile.mkdtemp(prefix="wk_ingest_guard_exact_")
        store = tempfile.mkdtemp(prefix="wk_ingest_guard_exact_store_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        code, _out, err = _run(["store", "init", store])
        self.assertEqual(code, 0, err)

        path = os.path.join(tmp, "codebase")
        with open(path, "w", encoding="utf-8") as f:
            f.write("conteudo sem extensao\n")

        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            code, _out, err = _run(
                ["ingest-legacy", "codebase", "--source-type", "human-doc", "--origin", "teste",
                 "--topic", "demo", "--store", store]
            )
        finally:
            os.chdir(cwd)
        self.assertNotEqual(code, 0)
        self.assertIn("fora de escopo", err)
        self.assertNotIn("wk code", err)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wk_doctor_base_")
        self.store = tempfile.mkdtemp(prefix="wk_doctor_store_")
        self.repo = tempfile.mkdtemp(prefix="wk_doctor_repo_")
        for d in (self.base, self.store, self.repo):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_doctor_store_valido_com_permissoes_ok_retorna_0_com_todas_as_chaves(self):
        store_abs = os.path.abspath(self.store)
        repo_abs = os.path.abspath(self.repo)
        settings_path = os.path.join(self.base, ".claude", "settings.json")
        cli._write_permission_settings(settings_path, store_abs, repo_abs)

        code, out, err = _run(
            ["doctor", "--store", self.store, "--repo", self.repo,
             "--engine", "claude-code", "--base", self.base, "--json"]
        )
        self.assertEqual(code, 0, err)
        # §10.2/§10.3: `doctor --json` é o envelope comum; os campos antigos
        # (testados abaixo) sobrevivem tal-e-qual dentro de `summary.detail`.
        data = json.loads(out)["summary"]["detail"]
        for key in ("shell", "python", "wk", "store", "repo", "engine",
                    "bloqueios", "proximo_passo"):
            self.assertIn(key, data)
        self.assertIn("powershell_provavel", data["shell"])
        self.assertTrue(data["store"]["existe"])
        self.assertTrue(data["repo"]["existe"])
        self.assertEqual(data["bloqueios"], [])

    def test_doctor_store_inexistente_bloqueia_e_indica_criacao(self):
        missing = os.path.join(self.base, "nao-existe-store")
        code, out, err = _run(["doctor", "--store", missing, "--json"])
        self.assertNotEqual(code, 0, err)
        data = json.loads(out)["summary"]["detail"]
        self.assertFalse(data["store"]["existe"])
        self.assertIn("store", data["bloqueios"])
        self.assertIn("wk store init", data["proximo_passo"])

    def test_doctor_sinaliza_powershell_provavel(self):
        original_shell = os.environ.pop("SHELL", None)
        original_msystem = os.environ.pop("MSYSTEM", None)
        try:
            with mock.patch.dict(os.environ, {"PSModulePath": "C:\\fake\\ps\\modules"}):
                code, out, _err = _run(["doctor", "--store", self.store, "--json"])
        finally:
            if original_shell is not None:
                os.environ["SHELL"] = original_shell
            if original_msystem is not None:
                os.environ["MSYSTEM"] = original_msystem
        data = json.loads(out)["summary"]["detail"]
        self.assertTrue(data["shell"]["powershell_provavel"])
        self.assertTrue(any("bash -c" in a for a in data["shell"]["avisos"]))
        # §10.3: `succeeded`/`noop` -> 0, `blocked` -> 2 (era 1); powershell
        # não é, por si só, um bloqueio.
        self.assertIn(code, (0, 2))


class CheckConfigPermissoesTests(unittest.TestCase):
    def test_check_sem_flags_traz_config_permissoes_nao_verificado(self):
        base = tempfile.mkdtemp(prefix="wk_check_base_")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)

        code, out, _err = _run(["check", "--engine", "claude-code", "--all", "--base", base])
        data = json.loads(out)
        self.assertIn("config_permissoes", data)
        self.assertEqual(data["config_permissoes"]["estado"], "nao_verificado")
        self.assertIn("wk check", data["config_permissoes"]["aviso"])
        # sem manifesto embutido (árvore de fonte) e sem docs divergentes,
        # o código de saída não deve depender da checagem de permissão aqui.
        self.assertEqual(code, 0)


class DoctorEngineInvalidaTests(unittest.TestCase):
    """DEFEITO 1: engine desconhecida não pode passar por `doctor` sem
    bloquear, e `proximo_passo` não pode ecoar a engine inválida."""

    def test_engine_invalida_bloqueia_exit_nao_zero_e_cita_engines_validas(self):
        base = tempfile.mkdtemp(prefix="wk_doctor_engineinv_base_")
        store = tempfile.mkdtemp(prefix="wk_doctor_engineinv_store_")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        code, _out, err = _run(["store", "init", store])
        self.assertEqual(code, 0, err)

        code, out, err = _run(
            ["doctor", "--store", store, "--engine", "claude", "--base", base, "--json"]
        )
        self.assertNotEqual(code, 0, err)
        data = json.loads(out)["summary"]["detail"]
        self.assertIn("engine", data["bloqueios"])
        self.assertIn("erro", data["engine"])
        self.assertEqual(
            data["engine"]["engines_validas"],
            sorted(cli.ENGINES),
        )
        # a forma correta sugerida nunca pode repetir a engine inválida.
        self.assertNotIn(" --engine claude ", data["proximo_passo"])
        self.assertFalse(data["proximo_passo"].rstrip(").").endswith("claude"))
        for valida in sorted(cli.ENGINES):
            self.assertIn(valida, data["proximo_passo"])

    def test_doctor_ambiente_ok_retorna_0_com_bloqueios_vazio(self):
        base = tempfile.mkdtemp(prefix="wk_doctor_ok_base_")
        store = tempfile.mkdtemp(prefix="wk_doctor_ok_store_")
        repo = tempfile.mkdtemp(prefix="wk_doctor_ok_repo_")
        for d in (base, store, repo):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        code, _out, err = _run(["store", "init", store])
        self.assertEqual(code, 0, err)
        settings_path = os.path.join(base, ".claude", "settings.json")
        cli._write_permission_settings(
            settings_path, os.path.abspath(store), os.path.abspath(repo)
        )

        code, out, err = _run(
            ["doctor", "--store", store, "--repo", repo,
             "--engine", "claude-code", "--base", base, "--json"]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)["summary"]["detail"]
        self.assertEqual(data["bloqueios"], [])


class DoctorInvarianteExitBloqueiosTests(unittest.TestCase):
    """Percorre store inexistente, repo inexistente, engine inválida e tudo
    ok, e garante em todos: exit 0 <=> bloqueios vazio (nunca se contradizem).
    """

    def _make_store(self):
        store = tempfile.mkdtemp(prefix="wk_doctor_matrix_store_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        code, _out, err = _run(["store", "init", store])
        self.assertEqual(code, 0, err)
        return store

    def _assert_invariante(self, code, data, err, nome):
        with self.subTest(nome):
            if data["bloqueios"]:
                self.assertNotEqual(code, 0, err)
            else:
                self.assertEqual(code, 0, err)

    def test_invariante_exit_e_bloqueios_nunca_se_contradizem(self):
        # cenário: store inexistente
        base1 = tempfile.mkdtemp(prefix="wk_doctor_matrix_base1_")
        self.addCleanup(shutil.rmtree, base1, ignore_errors=True)
        code, out, err = _run(
            ["doctor", "--store", os.path.join(base1, "nao-existe"), "--base", base1, "--json"]
        )
        data = json.loads(out)["summary"]["detail"]
        self._assert_invariante(code, data, err, "store_inexistente")
        self.assertIn("store", data["bloqueios"])

        # cenário: repo inexistente
        base2 = tempfile.mkdtemp(prefix="wk_doctor_matrix_base2_")
        self.addCleanup(shutil.rmtree, base2, ignore_errors=True)
        store2 = self._make_store()
        code, out, err = _run(
            ["doctor", "--store", store2,
             "--repo", os.path.join(base2, "nao-existe-repo"), "--base", base2, "--json"]
        )
        data = json.loads(out)["summary"]["detail"]
        self._assert_invariante(code, data, err, "repo_inexistente")
        self.assertIn("repo", data["bloqueios"])

        # cenário: engine inválida
        base3 = tempfile.mkdtemp(prefix="wk_doctor_matrix_base3_")
        self.addCleanup(shutil.rmtree, base3, ignore_errors=True)
        store3 = self._make_store()
        code, out, err = _run(
            ["doctor", "--store", store3, "--engine", "claude", "--base", base3, "--json"]
        )
        data = json.loads(out)["summary"]["detail"]
        self._assert_invariante(code, data, err, "engine_invalida")
        self.assertIn("engine", data["bloqueios"])

        # cenário: tudo ok
        base4 = tempfile.mkdtemp(prefix="wk_doctor_matrix_base4_")
        repo4 = tempfile.mkdtemp(prefix="wk_doctor_matrix_repo4_")
        self.addCleanup(shutil.rmtree, base4, ignore_errors=True)
        self.addCleanup(shutil.rmtree, repo4, ignore_errors=True)
        store4 = self._make_store()
        settings_path = os.path.join(base4, ".claude", "settings.json")
        cli._write_permission_settings(
            settings_path, os.path.abspath(store4), os.path.abspath(repo4)
        )
        code, out, err = _run(
            ["doctor", "--store", store4, "--repo", repo4,
             "--engine", "claude-code", "--base", base4, "--json"]
        )
        data = json.loads(out)["summary"]["detail"]
        self._assert_invariante(code, data, err, "tudo_ok")
        self.assertEqual(data["bloqueios"], [])


class PermissaoFormatoHonestidadeTests(unittest.TestCase):
    """DEFEITO 2: só claude-code tem formato de permissão `verificado`; as
    demais engines (compartilhando `.agents/settings.json`) são `best-effort`
    — o arquivo é gravado, mas nada garante que a engine o leia."""

    def _paths(self, base):
        claude = os.path.abspath(os.path.join(base, ".claude", "settings.json"))
        agents = os.path.abspath(os.path.join(base, ".agents", "settings.json"))
        return claude, agents

    # cmd_init exige o manifesto de docs embutido no .pyz, ausente na árvore
    # de fonte não empacotada; mockamos como test_corpus.InitCheckPermissionsTests.
    _fake_manifest = {"skill": {"file": "SKILL.md", "asset": "skill.md", "title": "Wiki AI"}}

    def _run_init(self, argv):
        with mock.patch.object(cli, "_docs_manifest", return_value=self._fake_manifest), \
             mock.patch.object(cli, "_doc_text", return_value="# Skill\n"):
            return _run(argv)

    def test_init_marca_verificado_claude_code_e_best_effort_devin(self):
        base = tempfile.mkdtemp(prefix="wk_perm_init_base_")
        store = tempfile.mkdtemp(prefix="wk_perm_init_store_")
        repo = tempfile.mkdtemp(prefix="wk_perm_init_repo_")
        for d in (base, store, repo):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)

        code, out, err = self._run_init(
            ["init", "--engine", "claude-code,devin", "--base", base,
             "--store", store, "--repo", repo]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        claude_path, agents_path = self._paths(base)
        by_path = {p["path"]: p for p in data["permissoes"]}
        self.assertEqual(by_path[claude_path]["formato"], "verificado")
        self.assertNotIn("aviso", by_path[claude_path])
        self.assertEqual(by_path[agents_path]["formato"], "best-effort")
        self.assertIn("aviso", by_path[agents_path])

    def test_check_marca_verificado_claude_code_e_best_effort_devin(self):
        base = tempfile.mkdtemp(prefix="wk_perm_check_base_")
        store = tempfile.mkdtemp(prefix="wk_perm_check_store_")
        repo = tempfile.mkdtemp(prefix="wk_perm_check_repo_")
        for d in (base, store, repo):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        code, _out, err = self._run_init(
            ["init", "--engine", "claude-code,devin", "--base", base,
             "--store", store, "--repo", repo]
        )
        self.assertEqual(code, 0, err)

        code, out, err = _run(
            ["check", "--engine", "claude-code,devin", "--all", "--base", base,
             "--store", store, "--repo", repo]
        )
        data = json.loads(out)
        claude_path, agents_path = self._paths(base)
        by_path = {e["path"]: e for e in data["config_permissoes"]}
        self.assertEqual(by_path[claude_path]["formato"], "verificado")
        self.assertNotIn("aviso", by_path[claude_path])
        self.assertEqual(by_path[agents_path]["formato"], "best-effort")
        self.assertIn("aviso", by_path[agents_path])

    def test_doctor_nao_reporta_permissao_garantida_para_engine_best_effort(self):
        base = tempfile.mkdtemp(prefix="wk_perm_doctor_base_")
        store = tempfile.mkdtemp(prefix="wk_perm_doctor_store_")
        repo = tempfile.mkdtemp(prefix="wk_perm_doctor_repo_")
        for d in (base, store, repo):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        code, _out, err = self._run_init(
            ["init", "--engine", "devin", "--base", base,
             "--store", store, "--repo", repo]
        )
        self.assertEqual(code, 0, err)

        code, out, err = _run(
            ["doctor", "--store", store, "--repo", repo,
             "--engine", "devin", "--base", base, "--json"]
        )
        data = json.loads(out)["summary"]["detail"]
        # o arquivo best-effort foi escrito corretamente (não deve travar por
        # si só), mas isso não pode virar uma afirmação de garantia.
        self.assertFalse(data["engine"]["permissao_garantida"])
        self.assertIn("aviso_permissao", data["engine"])
        self.assertTrue(
            all(r["formato"] == "best-effort" for r in data["engine"]["config_permissoes"])
        )

    def test_doctor_engine_claude_code_ok_reporta_permissao_garantida(self):
        base = tempfile.mkdtemp(prefix="wk_perm_doctor_ok_base_")
        store = tempfile.mkdtemp(prefix="wk_perm_doctor_ok_store_")
        repo = tempfile.mkdtemp(prefix="wk_perm_doctor_ok_repo_")
        for d in (base, store, repo):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        code, _out, err = self._run_init(
            ["init", "--engine", "claude-code", "--base", base,
             "--store", store, "--repo", repo]
        )
        self.assertEqual(code, 0, err)

        code, out, err = _run(
            ["doctor", "--store", store, "--repo", repo,
             "--engine", "claude-code", "--base", base, "--json"]
        )
        data = json.loads(out)["summary"]["detail"]
        self.assertTrue(data["engine"]["permissao_garantida"])
        self.assertNotIn("aviso_permissao", data["engine"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
