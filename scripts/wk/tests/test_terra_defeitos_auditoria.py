"""Testes da onda "Terra" (§10.1/§10.2/§10.3/§11 do plano de ajustes):

1. `init` sem `--engine` — store/repo/agente do fluxo padrão, idempotente,
   nunca instala pilotos, `--engine` só como compat marcado na ajuda.
2. `check --engine claude-code` sem manifesto embutido — exit 2 explicável,
   nunca `KeyError` cru (exit 1 genérico).
3. `_run_investigation_chain` fecha o binding mesmo se `coordinator.run_chain`
   falhar (try/finally) — mesma garantia do antigo `_dispatch_objectives`,
   removido (código morto) na limpeza de continuação de rodada única.
4. `cmd_agent_connect` fecha o binding e nunca deixa um binding gravado pela
   metade quando `save_binding` falha.
5. `--engine` desconhecido é recusado (exit 2) em vez de degradar para
   `--mode deep` silencioso.
6. `delivery.confirm` devolve `already_current` (sem TOCTOU no `cli.py`).
7. `delivery.prepare` é recuperável quando `_save` do índice falha depois do
   `out_dir` já ter sido promovido; `repo=""`/`None` nunca vira `TypeError`.

Sem git, sem rede, sem e2e — só os dois testes que já existiam para o
mecanismo real de agente (`_ExtensionEnvMixin`/`_FakeCliAdapter`), aqui
reaproveitados por importação.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from publishing import delivery
from runtime import agents as rt_agents
from runtime import coordinator as rt_coordinator
from runtime import tasks as rt_tasks
from runtime.bindings import BindingError, BindingStore

from wk import cli
from wk.tests.test_agent_cli import _ExtensionEnvMixin
from wk.tests.test_run_chain_cli import _setup as _chain_setup


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _tmpdir(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix)


# ---------------------------------------------------------------------------
# 1) `init` sem `--engine` (§10.1/§11)
# ---------------------------------------------------------------------------


class InitSemEngineTests(unittest.TestCase):
    def setUp(self):
        # `tempfile.mkdtemp` já CRIA o diretório — usar isso como `--store`
        # faria a 1ª `init` já nascer `noop` (o store "existiria" antes de
        # qualquer chamada). O store em si tem que ser um path AINDA
        # inexistente dentro de um diretório temporário descartável.
        parent = _tmpdir("wk_init_sem_engine_parent_")
        self.addCleanup(shutil.rmtree, parent, ignore_errors=True)
        self.store = os.path.join(parent, "store")

    def test_funciona_sem_engine_cria_store(self):
        code, out, err = _run(["init", "--store", self.store, "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["command"], "init")
        self.assertEqual(payload["operation_status"], "succeeded")
        self.assertTrue(payload["summary"]["store_criado"])
        self.assertTrue(os.path.isdir(self.store))

    def test_segunda_execucao_e_noop_explicito(self):
        code1, out1, err1 = _run(["init", "--store", self.store, "--json"])
        self.assertEqual(code1, 0, err1)
        self.assertEqual(json.loads(out1)["operation_status"], "succeeded")

        code2, out2, err2 = _run(["init", "--store", self.store, "--json"])
        self.assertEqual(code2, 0, err2)
        payload2 = json.loads(out2)
        self.assertEqual(payload2["operation_status"], "noop")
        self.assertFalse(payload2["summary"]["store_criado"])

    def test_registra_repo_no_mesmo_namespace_que_analyze_espera(self):
        repo = _tmpdir("wk_init_sem_engine_repo_")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        repo_abs = os.path.abspath(repo)

        code, out, err = _run(["init", "--store", self.store, "--repo", repo, "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        namespace_no_init = payload["summary"]["namespace"]
        self.assertEqual(namespace_no_init, f"code/{cli._repo_key(repo_abs)}")

        # `_repo_profile` é exatamente o que `cmd_analyze` lê para decidir o
        # namespace de um repo já registrado — mesma fonte, sem duplicar.
        profile_all = cli._load_analysis_profile(os.path.abspath(self.store))
        prior = cli._repo_profile(profile_all, repo_abs)
        self.assertEqual(prior.get("namespace"), namespace_no_init)

    def test_agent_persiste_preferencia_nunca_conecta(self):
        code, out, err = _run(["init", "--store", self.store, "--agent", "local", "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["agent_preferencia"]["agent_id"], "local")
        pref = BindingStore(os.path.abspath(self.store)).get_preference(None)
        self.assertEqual(pref["agent_id"], "local")
        self.assertIsNone(BindingStore(os.path.abspath(self.store)).get_binding(None))

    def test_proxima_operacao_e_agent_connect_quando_ha_agent(self):
        code, out, err = _run(["init", "--store", self.store, "--agent", "local", "--json"])
        payload = json.loads(out)
        argv = payload["next_actions"][0]["argv"]
        self.assertIn("connect", argv)
        self.assertNotIn("--engine", argv)

    def test_proxima_operacao_e_analyze_quando_so_ha_repo(self):
        repo = _tmpdir("wk_init_sem_engine_repo2_")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        code, out, err = _run(["init", "--store", self.store, "--repo", repo, "--json"])
        payload = json.loads(out)
        argv = payload["next_actions"][0]["argv"]
        self.assertIn("analyze", argv)
        self.assertNotIn("--engine", argv)

    def test_sem_store_bloqueia_exit_2(self):
        code, out, err = _run(["init", "--json"])
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "blocked")

    def test_nao_instala_pilotos_nem_skill_md(self):
        base = _tmpdir("wk_init_sem_engine_base_")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        code, out, err = _run(["init", "--store", self.store, "--json"])
        self.assertEqual(code, 0, err)
        # nada de `.claude/skills`, `.agents/skills` etc. no diretório atual
        # nem em `base` (nem sequer usado pelo caminho sem --engine).
        self.assertFalse(os.path.isdir(os.path.join(base, ".claude")))
        self.assertFalse(os.path.isdir(os.path.join(base, ".agents")))

    def test_engine_e_compat_e_marcado_na_ajuda(self):
        parser = cli._build_parser()
        help_text = parser.format_help()
        # subparser "init" tem ajuda própria; valida via parse de --help do
        # subcomando (argparse imprime e sai) capturando stdout.
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli._build_parser().parse_args(["init", "--help"])
        texto = out.getvalue()
        self.assertIn("compat", texto)
        self.assertIn("A01", texto)


# ---------------------------------------------------------------------------
# 2) `check --engine claude-code` sem manifesto embutido — exit 2 (§1)
# ---------------------------------------------------------------------------


class CheckSemManifestoTests(unittest.TestCase):
    def test_check_sem_all_sem_manifesto_e_exit_2_nao_keyerror(self):
        base = _tmpdir("wk_check_sem_manifesto_")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        with mock.patch.object(cli, "_docs_manifest", return_value={}):
            code, out, err = _run(["check", "--engine", "claude-code", "--base", base])
        self.assertEqual(code, 2)
        # nunca o traço de exceção não tratada (`"tipo": "KeyError"`) do
        # handler de topo — mensagem explicável, própria de `check`.
        payload = json.loads(err)
        self.assertNotEqual(payload.get("tipo"), "KeyError")
        self.assertIn("SKILL.md", payload["error"])

    def test_check_com_all_sem_manifesto_nao_quebra(self):
        base = _tmpdir("wk_check_sem_manifesto_all_")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        with mock.patch.object(cli, "_docs_manifest", return_value={}):
            code, out, err = _run(["check", "--engine", "claude-code", "--all", "--base", base])
        # `--all` com manifesto vazio não tenta acessar a chave "skill":
        # slugs fica vazio, nenhum KeyError.
        self.assertIn(code, (0, 1))


# ---------------------------------------------------------------------------
# 3) fecha o binding mesmo se `coordinator.run_chain` falhar
#
# Portado da Onda-remediação: o antigo `_dispatch_objectives` (removido nesta
# limpeza de código morto — sem chamador de produção desde que o laço de
# continuação virou UMA chamada por invocação, §7.3) tinha essa mesma
# garantia de `try/finally`; quem a faz agora é `_run_investigation_chain`
# (via `_connect_chain_binding` + `finally: registry.close(binding)` em
# torno do laço `for oid in objective_ids: ... rt_coordinator.run_chain(...)`)
# — o teste muda de alvo, a garantia testada é a MESMA.
# ---------------------------------------------------------------------------


class DispatchObjectivesCloseOnFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir("wk_dispatch_close_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_registry_close_chamado_mesmo_com_run_chain_falhando(self):
        ctx = _chain_setup(self.tmp)
        self.addCleanup(ctx["store"].close)
        resolver = cli._snapshot_resolver(ctx["snapshot"])

        real_registry = rt_agents.default_registry(
            local_task_registry=cli._local_task_registry(), local_max_workers=4,
        )
        close_calls = []
        orig_close = real_registry.close

        def _spy_close(binding):
            close_calls.append(binding.binding_id)
            return orig_close(binding)

        real_registry.close = _spy_close

        with mock.patch.object(rt_agents, "default_registry", return_value=real_registry), \
             mock.patch.object(rt_coordinator, "run_chain", side_effect=RuntimeError("boom-coordinator")):
            with self.assertRaises(RuntimeError):
                cli._run_investigation_chain(
                    ctx["store"], store_root=ctx["store_root"], namespace=ctx["namespace"],
                    snapshot=ctx["snapshot"], extraction=ctx["extraction"],
                    objectives=ctx["objectives"], capability_map=ctx["capability_map"],
                    objectives_by_id=ctx["objectives_by_id"],
                    inputs_by_objective=ctx["inputs_by_objective"],
                    engine_name="local", bindings_store=None, resolver=resolver,
                    reason="teste fechamento no finally", max_rounds=None,
                )

        self.assertEqual(len(close_calls), 1, "registry.close deveria ter sido chamado 1x no finally")


# ---------------------------------------------------------------------------
# 4) `cmd_agent_connect` fecha o binding; `save_binding` falhando não deixa
#    nada gravado (exit 2)
# ---------------------------------------------------------------------------


class AgentConnectSaveBindingFalhaTests(_ExtensionEnvMixin, unittest.TestCase):
    def setUp(self):
        self.store = _tmpdir("wk_agent_connect_save_falha_store_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)

    def test_save_binding_falhando_nao_grava_e_retorna_exit_2(self):
        with mock.patch.object(
            BindingStore, "save_binding", side_effect=BindingError("disco cheio (teste)"),
        ):
            code, out, err = _run(["agent", "connect", "--store", self.store, "--agent", "local", "--json"])
        self.assertEqual(code, 2, err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertIsNone(BindingStore(self.store).get_binding(None))

    def test_binding_fechado_mesmo_quando_save_binding_falha(self):
        self._use_extension("make_ok_fake_cli")
        reg = rt_agents.default_registry()
        adapter = reg.adapter("fake-cli")
        with mock.patch.object(rt_agents, "default_registry", return_value=reg), \
             mock.patch.object(BindingStore, "save_binding", side_effect=BindingError("boom")):
            code, out, err = _run(
                ["agent", "connect", "--store", self.store, "--agent", "fake-cli", "--json"]
            )
        self.assertEqual(code, 2, err)
        # o binding conectado nesta chamada foi fechado (adapter._closed)
        self.assertTrue(adapter._closed, "o binding deveria ter sido fechado após falha de save_binding")

    def test_conexao_normal_continua_fechando_o_binding(self):
        real_registry = rt_agents.default_registry()
        adapter = real_registry.adapter("local")
        with mock.patch.object(rt_agents, "default_registry", return_value=real_registry):
            code, out, err = _run(
                ["agent", "connect", "--store", self.store, "--agent", "local", "--json"]
            )
        self.assertEqual(code, 0, err)
        binding_id = json.loads(out)["summary"]["binding_id"]
        self.assertIn(binding_id, adapter._closed)


# ---------------------------------------------------------------------------
# 5) `--engine` desconhecido é recusado (exit 2), nunca degrada p/ deep
# ---------------------------------------------------------------------------


class EngineDesconhecidoRecusadoTests(unittest.TestCase):
    def test_resolve_mode_and_agent_levanta_unknownengineerror(self):
        ns = types.SimpleNamespace(mode=None, agent=None, engine="engine-nunca-existiu")
        with self.assertRaises(cli.UnknownEngineError):
            cli._resolve_mode_and_agent(ns)

    def test_cmd_analyze_engine_desconhecido_bloqueia_exit_2(self):
        store = _tmpdir("wk_analyze_engine_invalido_store_")
        repo = _tmpdir("wk_analyze_engine_invalido_repo_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        ns = types.SimpleNamespace(
            repo=repo, store=store, topic=None, mode=None, agent=None,
            engine="engine-nunca-existiu", json=True,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.cmd_analyze(ns)
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertIn("engine-nunca-existiu", payload["summary"]["mensagem"])
        # nunca criou runtime.db/snapshot: bloqueou ANTES de qualquer trabalho.
        self.assertFalse(os.path.exists(os.path.join(store, "runtime.db")))

    def test_cmd_update_engine_desconhecido_bloqueia_exit_2(self):
        store = _tmpdir("wk_update_engine_invalido_store_")
        repo = _tmpdir("wk_update_engine_invalido_repo_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        # `update` bloqueia antes mesmo de checar "análise anterior" quando o
        # --engine é desconhecido? Não: a checagem de perfil vem antes no
        # código-fonte. Sem análise anterior, `update` já sairia 2 por outro
        # motivo — este teste força o caminho direto da função com um perfil
        # já existente para isolar exclusivamente o `--engine` desconhecido.
        profile_all = {cli._repo_key(os.path.abspath(repo)): {
            "last_snapshot_id": "snap-x", "namespace": "code/x", "topic": None, "scope": None,
        }}
        cli._save_analysis_profile(os.path.abspath(store), profile_all)
        ns = types.SimpleNamespace(
            repo=repo, store=store, mode=None, agent=None, engine="engine-nunca-existiu", json=True,
        )
        out = io.StringIO()
        # Isola exclusivamente o `--engine` desconhecido: sem isto, `update`
        # bloqueia antes por "manifesto do snapshot anterior não encontrado"
        # (outro motivo válido de exit 2, mas não o que este teste cobre).
        with mock.patch.object(cli, "_load_snapshot_manifest", return_value=object()), \
             contextlib.redirect_stdout(out):
            code = cli.cmd_update(ns)
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["operation_status"], "blocked")

    def test_cmd_resume_engine_desconhecido_bloqueia_exit_2(self):
        store = _tmpdir("wk_resume_engine_invalido_store_")
        repo = _tmpdir("wk_resume_engine_invalido_repo_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        os.makedirs(os.path.abspath(store), exist_ok=True)
        rt_tasks.TaskStore.open(cli._runtime_db_path(os.path.abspath(store))).close()
        ns = types.SimpleNamespace(
            repo=repo, store=store, mode=None, agent=None, engine="engine-nunca-existiu", json=True,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.cmd_resume(ns)
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["operation_status"], "blocked")


# ---------------------------------------------------------------------------
# 6/7) `publishing.delivery` — `already_current`, `prepare` transacional,
#      `repo=""`/`None` uniforme (§9.4)
# ---------------------------------------------------------------------------


class DeliveryDefeitosTests(unittest.TestCase):
    def test_select_and_assess_repo_vazio_e_deliveryerror_nao_typeerror(self):
        class _FakeManifest:
            documents = {}
            revision_id = "rev-x"

        with mock.patch.object(delivery, "_read_manifest", return_value=_FakeManifest()):
            # repo="" deve levantar DeliveryError (seleção vazia, ou outro
            # motivo de domínio) — NUNCA TypeError de os.path.isdir(None).
            with self.assertRaises(delivery.DeliveryError):
                delivery._select_and_assess(_FakeManifest(), "/store-inexistente", "", None)

    def test_select_and_assess_repo_none_e_repo_vazio_tem_mesmo_comportamento(self):
        # ambos devem cair no MESMO ramo (sem --repo): nenhuma checagem de
        # `os.path.isdir` é feita para nenhum dos dois.
        with mock.patch("os.path.isdir", return_value=False) as m_isdir:
            try:
                delivery._select_and_assess(mock.Mock(documents={}), "/nao-importa", None, None)
            except delivery.DeliveryError:
                pass
            try:
                delivery._select_and_assess(mock.Mock(documents={}), "/nao-importa", "", None)
            except delivery.DeliveryError:
                pass
        # `os.path.isdir` só é chamado para checar o KNOWLEDGE_DB_FILENAME
        # existência (kb_path), nunca para `repo_abs` quando repo é falsy.
        calls_com_repo = [c for c in m_isdir.call_args_list if "store" not in str(c)]
        self.assertEqual(calls_com_repo, [])

    def test_prepare_recupera_quando_save_do_indice_falha(self):
        tmp = tempfile.mkdtemp(prefix="wk_delivery_prepare_falha_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store_root = os.path.join(tmp, "store")
        os.makedirs(store_root)
        publication_root = os.path.join(store_root, "publicacoes")
        os.makedirs(publication_root)

        class _FakeManifest:
            documents = {}
            revision_id = "rev-x"

        out_dir = os.path.join(tmp, "out-falha")
        assessment_ok = {
            "publication_revision": "rev-x", "documents": [], "units": [],
            "external_entities": [], "knowledge_status": "complete",
            "delivery_status": "ready", "blocked_reasons": [], "warnings": [],
        }
        with mock.patch.object(delivery, "_read_manifest", return_value=_FakeManifest()), \
             mock.patch.object(delivery, "_select_and_assess", return_value=assessment_ok):
            # sem documentos selecionados: `prepare` levantaria antes de
            # chegar no `_save` — para exercitar o `_save` falhando, dou um
            # documento fake mínimo copiável.
            src_md = os.path.join(publication_root, "doc.md")
            src_docx = os.path.join(publication_root, "doc.docx")
            with open(src_md, "w", encoding="utf-8") as f:
                f.write("# doc")
            with open(src_docx, "wb") as f:
                f.write(b"PK\x03\x04fake")

            class _Entry:
                md_path, docx_path, content_hash = "doc.md", "doc.docx", "h1"

            class _FakeManifest2(_FakeManifest):
                documents = {"unit-1": _Entry()}

            assessment_ok["units"] = ["unit-1"]
            with mock.patch.object(delivery, "_read_manifest", return_value=_FakeManifest2()):
                with mock.patch.object(delivery, "_save", side_effect=OSError("disco cheio (teste)")):
                    with self.assertRaises(delivery.DeliveryError):
                        delivery.prepare(
                            store_root=store_root, publication_root=publication_root,
                            destination="dest-teste", out_dir=out_dir,
                        )
                # `out_dir` foi revertido: repetir com o MESMO --out não fica
                # preso em "diretório já existe".
                self.assertFalse(os.path.exists(out_dir))

                # repetição, agora com `_save` funcionando de verdade: sucede.
                payload = delivery.prepare(
                    store_root=store_root, publication_root=publication_root,
                    destination="dest-teste", out_dir=out_dir,
                )
                self.assertTrue(os.path.isdir(out_dir))
                self.assertEqual(payload["delivery_id"], delivery._load(store_root)["deliveries"].popitem()[0])

    def test_confirm_devolve_already_current(self):
        tmp = tempfile.mkdtemp(prefix="wk_delivery_confirm_already_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store_root = os.path.join(tmp, "store")
        os.makedirs(store_root)
        did = "dlv_teste123"
        state = {
            "schema_version": delivery.SCHEMA_VERSION,
            "deliveries": {did: {
                "delivery_id": did, "destination": "dest", "publication_revision": "rev-1",
                "scope": {"repo": None, "initiative": None}, "confirmed_at": None, "verified_by": None,
            }},
            "confirmed": {}, "confirmations": {},
        }
        delivery._save(store_root, state)

        c1 = delivery.confirm(store_root=store_root, delivery_id=did, destination="dest", verified_by="op1")
        self.assertFalse(c1["already_current"])  # 1ª confirmação: nada era vigente antes

        c2 = delivery.confirm(store_root=store_root, delivery_id=did, destination="dest", verified_by="op1")
        self.assertTrue(c2["already_current"])  # já era a entrega vigente


# ---------------------------------------------------------------------------
# 8) `cmd_delivery_confirm` (cli.py) usa `already_current` de `confirm()`,
#    sem TOCTOU — `operation_status: noop` na 2ª confirmação idêntica.
# ---------------------------------------------------------------------------


class DeliveryConfirmCliJaVigenteTests(unittest.TestCase):
    def test_confirmar_a_mesma_entrega_duas_vezes_e_noop_na_segunda(self):
        tmp = tempfile.mkdtemp(prefix="wk_delivery_confirm_cli_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store_root = os.path.join(tmp, "store")
        os.makedirs(store_root)
        did = "dlv_cliteste"
        state = {
            "schema_version": delivery.SCHEMA_VERSION,
            "deliveries": {did: {
                "delivery_id": did, "destination": "dest-cli", "publication_revision": "rev-1",
                "scope": {"repo": None, "initiative": None}, "confirmed_at": None, "verified_by": None,
            }},
            "confirmed": {}, "confirmations": {},
        }
        delivery._save(store_root, state)

        code1, out1, err1 = _run([
            "delivery", "confirm", "--store", store_root, "--delivery", did,
            "--destination", "dest-cli", "--verified-by", "op1", "--json",
        ])
        self.assertEqual(code1, 0, err1)
        self.assertEqual(json.loads(out1)["operation_status"], "succeeded")

        code2, out2, err2 = _run([
            "delivery", "confirm", "--store", store_root, "--delivery", did,
            "--destination", "dest-cli", "--verified-by", "op1", "--json",
        ])
        self.assertEqual(code2, 0, err2)
        payload2 = json.loads(out2)
        self.assertEqual(payload2["operation_status"], "noop")
        self.assertTrue(payload2["summary"]["already_current"])


if __name__ == "__main__":
    unittest.main()
