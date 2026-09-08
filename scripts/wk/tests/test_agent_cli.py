"""`wk agent list/setup/connect`, `init --agent`, `doctor --probe-agent` e a
seleção de agente de `analyze`/`update`/`resume` (§10.1/§10.4.6/§11).

Contrato de agente real: `runtime.agents`/`runtime.bindings`/`runtime.
coordinator` (dono é o outro agente da onda — aqui só se CONSOME a API,
nunca se reimplementa). O adaptador `local` (bundled) e um adaptador FAKE
próprio (`_FakeCliAdapter`, registrado via `WIKI_AI_AGENT_ADAPTERS` — o
mesmo mecanismo de extensão do T32, `runtime.agents.AgentRegistry.
load_extensions`) cobrem o caminho "agente real" sem depender de rede nem
de nenhum binário externo (o binário `claude` não está disponível neste
ambiente de teste — `claude-code` sempre aparece como não-conectável, e é
exatamente esse caminho de bloqueio que os testes de `--mode deep` exercitam).
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

from runtime import agents as A
from runtime.bindings import BindingStore
from runtime.executors.agent_adapters import BaseExecutorAdapter

from wk import cli


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------
# adaptador fake próprio deste arquivo (mesmo mecanismo de extensão do T32)
# --------------------------------------------------------------------------


class _FakeExec:
    """Executor síncrono em memória — devolve o MESMO `objective_id` que
    recebeu (senão `envelopes.validate_result` rejeitaria por divergência)."""

    def __init__(self) -> None:
        self._records: dict[str, dict] = {}
        self._n = 0

    def capabilities(self) -> dict:
        return {
            "dispatch": True, "concurrency": 1, "cancellation": "process-kill",
            "structured_output": True, "deepening": True, "tools": [],
        }

    def submit(self, task_id, objective, references=None, schema=None, policy=None) -> str:
        self._n += 1
        execution_id = f"fake-cli:{task_id}:{self._n}"
        self._records[execution_id] = dict(objective or {})
        return execution_id

    def status(self, execution_id) -> dict:
        return {"state": "done"}

    def result(self, execution_id) -> dict:
        objective = self._records.get(execution_id, {})
        return {"execution_id": execution_id, "output": {"objective_id": objective.get("objective_id")}}

    def cancel(self, execution_id) -> bool:
        return True


class _FakeCliAdapter(BaseExecutorAdapter):
    """Adaptador de extensão próprio deste arquivo — não reaproveita
    `FakeAgentAdapter` de `runtime.tests.test_agent_contract` de propósito:
    aqui o handshake precisa poder FALHAR sob controle deste teste."""

    agent_id = "fake-cli"
    adapter_version = "fake-cli/1"

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self._fail = fail

    def describe(self) -> A.AgentDescriptor:
        return A.AgentDescriptor(
            agent_id=self.agent_id,
            adapter_version=self.adapter_version,
            transports=("process",),
            capabilities=A.AgentCapabilities(dispatch=True, deepening=True, physical_cancellation=True),
            verified_versions=("fake-host/1",),
            prerequisites=(
                A.Prerequisite(prerequisite_id="python", description="interpretador do teste",
                                check_argv=("python", "--version")),
            ),
            setup_steps=(
                A.SetupStep(step_id="verify", description="sonda pelo fluxo real",
                             argv=("wk", "doctor", "--probe-agent")),
            ),
            distribution="extension",
            summary="adaptador fake de scripts/wk/tests/test_agent_cli.py",
        )

    def _handshake(self, transport: str, config: dict) -> dict:
        if self._fail:
            return {"connected": False, "detail": "handshake recusado no teste (fake-cli)"}
        return {"connected": True, "detail": "ok", "host_version": "fake-host/1", "model": None}

    def _new_executor(self, config: dict):
        return _FakeExec()


def make_ok_fake_cli():
    return _FakeCliAdapter(fail=False)


def make_failing_fake_cli():
    return _FakeCliAdapter(fail=True)


class _ExtensionEnvMixin:
    """Registra `fake-cli` via `WIKI_AI_AGENT_ADAPTERS` só durante o teste —
    isolado de qualquer outro teste do processo (env var é global)."""

    _EXT_MODULE = "wk.tests.test_agent_cli"

    def _use_extension(self, factory_name: str) -> None:
        previous = os.environ.get(A.EXTENSIONS_ENV)
        os.environ[A.EXTENSIONS_ENV] = f"{self._EXT_MODULE}:{factory_name}"

        def _restore():
            if previous is None:
                os.environ.pop(A.EXTENSIONS_ENV, None)
            else:
                os.environ[A.EXTENSIONS_ENV] = previous

        self.addCleanup(_restore)


def _tmpdir(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix)


# --------------------------------------------------------------------------
# 1) `agent list` — bundled (`local`, `claude-code`) + extensão (T32)
# --------------------------------------------------------------------------


class AgentListTests(_ExtensionEnvMixin, unittest.TestCase):
    def test_lista_agentes_bundled_sem_store(self):
        code, out, err = _run(["agent", "list", "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        ids = {e["agent_id"] for e in data["agentes"]}
        self.assertIn("local", ids)
        self.assertIn("claude-code", ids)
        local_entry = next(e for e in data["agentes"] if e["agent_id"] == "local")
        self.assertTrue(local_entry["homologated"])
        claude_entry = next(e for e in data["agentes"] if e["agent_id"] == "claude-code")
        # claude binário não existe neste ambiente: nunca homologado (verified_versions vazio).
        self.assertFalse(claude_entry["homologated"])
        self.assertEqual(claude_entry["verified_versions"], [])
        self.assertIn("aviso_homologacao", claude_entry)
        self.assertIsNone(local_entry["situacao_local"])  # sem --store, sem situação local

    def test_lista_inclui_extensao_registrada_por_env(self):
        self._use_extension("make_ok_fake_cli")
        code, out, err = _run(["agent", "list", "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        entry = next((e for e in data["agentes"] if e["agent_id"] == "fake-cli"), None)
        self.assertIsNotNone(entry, data["agentes"])
        self.assertEqual(entry["distribution"], "extension")

    def test_lista_com_store_mostra_situacao_local(self):
        store = _tmpdir("wk_agent_list_store_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        os.makedirs(store, exist_ok=True)
        reg = A.default_registry()
        binding = reg.connect("local", "auto")
        BindingStore(store).save_binding(binding, repo=None)

        code, out, err = _run(["agent", "list", "--store", store, "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        local_entry = next(e for e in data["agentes"] if e["agent_id"] == "local")
        self.assertEqual(
            local_entry["situacao_local"],
            {"selecionado": True, "conectado": True, "origem_selecao": "store"},
        )
        outro = next(e for e in data["agentes"] if e["agent_id"] == "claude-code")
        self.assertEqual(
            outro["situacao_local"], {"selecionado": False, "conectado": False, "origem_selecao": None},
        )


# --------------------------------------------------------------------------
# 2) `agent setup` — passos executáveis; ID desconhecido nunca sugere outro
# --------------------------------------------------------------------------


class AgentSetupTests(unittest.TestCase):
    def test_setup_agente_conhecido_lista_passos_executaveis(self):
        code, out, err = _run(["agent", "setup", "--agent", "local", "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["agent_id"], "local")
        self.assertTrue(data["setup_steps"])
        for step in data["setup_steps"]:
            self.assertTrue(step["argv"] or (step["manual_action"] and step["verify_argv"]))

    def test_setup_agente_desconhecido_erro_2_lista_registrados_sem_sugerir_outro(self):
        code, out, err = _run(["agent", "setup", "--agent", "nao-existe-123", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        data = json.loads(err)
        self.assertIn("nao-existe-123", data["erro"])
        self.assertIn("local", data["agentes_registrados"])
        self.assertIn("claude-code", data["agentes_registrados"])
        # nunca escolhe/menciona outro agente como substituto — só lista os IDs.
        self.assertNotIn("use", data["erro"].lower())


# --------------------------------------------------------------------------
# 3) `agent connect` — sucesso persiste; falha NÃO registra e devolve 2
# --------------------------------------------------------------------------


class AgentConnectTests(_ExtensionEnvMixin, unittest.TestCase):
    def setUp(self):
        self.store = _tmpdir("wk_agent_connect_store_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)

    def test_connect_local_sucesso_persiste_binding(self):
        code, out, err = _run(["agent", "connect", "--store", self.store, "--agent", "local", "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "succeeded")
        binding = BindingStore(self.store).get_binding(None)
        self.assertIsNotNone(binding)
        self.assertEqual(binding.agent_id, "local")
        self.assertTrue(binding.connected)

    def test_connect_falho_nao_grava_binding_e_devolve_next_actions(self):
        # T15/auditoria#5 (achado de auditoria, "Terra"): `_handshake` falhando
        # também derruba `available_transports` (mesmo handshake memoizado,
        # `BaseExecutorAdapter._verify_transport`) — a seleção de transporte
        # (`AgentRegistry._resolve_transport_choice`) vê NENHUM transporte
        # disponível e levanta `TransportUnavailable` (nunca chega a chamar
        # `adapter.connect`). `cmd_agent_connect` agora usa os `setup_steps`
        # REAIS desse erro (`exc.next_actions()`) em vez de um `agent setup`
        # genérico — aqui, o passo declarado pelo `_FakeCliAdapter.describe()`
        # (`wk doctor --probe-agent`), não mais texto adivinhado pela CLI.
        self._use_extension("make_failing_fake_cli")
        code, out, err = _run(["agent", "connect", "--store", self.store, "--agent", "fake-cli", "--json"])
        self.assertEqual(code, 2, err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertIsNone(BindingStore(self.store).get_binding(None))
        self.assertTrue(payload["next_actions"])
        na = payload["next_actions"][0]
        self.assertEqual(na["argv"], ["wk", "doctor", "--probe-agent"])
        # `pending[0]` carrega o detalhe estruturado completo do erro
        # (`TransportUnavailable.to_dict()`) — não só a 1ª ação.
        pend = payload["pending"][0]
        self.assertEqual(pend["tipo"], "transporte_indisponivel")
        self.assertIn("setup_steps", pend["detalhe_estruturado"])

    def test_connect_agente_nao_registrado_next_actions_aponta_agent_list(self):
        code, out, err = _run(["agent", "connect", "--store", self.store, "--agent", "xyz-nunca", "--json"])
        self.assertEqual(code, 2, err)
        payload = json.loads(out)
        na = payload["next_actions"][0]
        self.assertIn("list", na["argv"])

    def test_connect_cancel_active_invalida_leases_do_escopo(self):
        self._use_extension("make_ok_fake_cli")
        code, out, err = _run(["agent", "connect", "--store", self.store, "--agent", "fake-cli", "--json"])
        self.assertEqual(code, 0, err)
        binding_id = json.loads(out)["summary"]["binding_id"]

        bs = BindingStore(self.store)
        lease = bs.open_lease(binding_id=binding_id, repo=None, task_id="t1")
        self.assertTrue(bs.lease_valid(lease.lease_id))

        code2, out2, err2 = _run([
            "agent", "connect", "--store", self.store, "--agent", "fake-cli",
            "--cancel-active", "--json",
        ])
        self.assertEqual(code2, 0, err2)
        payload2 = json.loads(out2)
        self.assertEqual(payload2["summary"]["leases_cancelados"], 1)
        self.assertFalse(bs.lease_valid(lease.lease_id))

    def test_connect_repo_grava_binding_no_escopo_do_repo_nao_no_store(self):
        repo = _tmpdir("wk_agent_connect_repo_")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        code, out, err = _run([
            "agent", "connect", "--store", self.store, "--repo", repo, "--agent", "local", "--json",
        ])
        self.assertEqual(code, 0, err)
        bs = BindingStore(self.store)
        self.assertIsNotNone(bs.get_binding(repo))
        self.assertIsNone(bs.get_binding(None))


# --------------------------------------------------------------------------
# 4) `init --agent` — persiste preferência (não binding); idempotente
# --------------------------------------------------------------------------


#: `cmd_init` exige o manifesto de docs embutido no `.pyz`, ausente na árvore
#: de fonte não empacotada — mesmo mock de `test_ergonomia.
#: PermissaoFormatoHonestidadeTests`/`test_corpus.InitCheckPermissionsTests`.
_FAKE_MANIFEST = {"skill": {"file": "SKILL.md", "asset": "skill.md", "title": "Wiki AI"}}


class InitAgentTests(unittest.TestCase):
    def setUp(self):
        self.base = _tmpdir("wk_init_agent_base_")
        self.store = _tmpdir("wk_init_agent_store_")
        for d in (self.base, self.store):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        patcher1 = mock.patch.object(cli, "_docs_manifest", return_value=_FAKE_MANIFEST)
        patcher2 = mock.patch.object(cli, "_doc_text", return_value="# Skill\n")
        patcher1.start()
        patcher2.start()
        self.addCleanup(patcher1.stop)
        self.addCleanup(patcher2.stop)

    def test_agent_sem_store_e_erro_explicito(self):
        code, out, err = _run(["init", "--engine", "claude-code", "--base", self.base, "--agent", "local"])
        self.assertEqual(code, 2)
        data = json.loads(err)
        self.assertIn("--store", data["error"])

    def test_agent_persiste_preferencia_do_store_nao_conecta(self):
        code, out, err = _run([
            "init", "--engine", "claude-code", "--base", self.base,
            "--store", self.store, "--agent", "local",
        ])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["agent"]["preferencia"]["agent_id"], "local")
        self.assertEqual(data["agent"]["escopo"], "store")
        self.assertFalse(data["agent"]["conectado"])  # selecionar != conectado (§10.1)
        self.assertIn("connect", data["agent"]["proxima_operacao"]["argv"])

        pref = BindingStore(self.store).get_preference(None)
        self.assertEqual(pref["agent_id"], "local")
        self.assertIsNone(BindingStore(self.store).get_binding(None))  # nunca conecta sozinho

    def test_agent_e_idempotente(self):
        argv = [
            "init", "--engine", "claude-code", "--base", self.base,
            "--store", self.store, "--agent", "local",
        ]
        code1, out1, _ = _run(argv)
        code2, out2, _ = _run(argv)
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        d1, d2 = json.loads(out1), json.loads(out2)
        self.assertEqual(d1["agent"]["preferencia"]["agent_id"], d2["agent"]["preferencia"]["agent_id"])

    def test_agent_com_repo_grava_no_escopo_do_repo(self):
        repo = _tmpdir("wk_init_agent_repo_")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        code, out, err = _run([
            "init", "--engine", "claude-code", "--base", self.base,
            "--store", self.store, "--repo", repo, "--agent", "local",
        ])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["agent"]["escopo"], "repo")
        bs = BindingStore(self.store)
        self.assertEqual(bs.get_preference(os.path.abspath(repo))["agent_id"], "local")
        self.assertIsNone(bs.get_preference(None))  # store não é tocado


# --------------------------------------------------------------------------
# 5) `doctor --probe-agent` — sem binding bloqueia; com binding local, ok
# --------------------------------------------------------------------------


class DoctorProbeAgentTests(unittest.TestCase):
    def setUp(self):
        self.base = _tmpdir("wk_doctor_probe_base_")
        self.store = _tmpdir("wk_doctor_probe_store_")
        for d in (self.base, self.store):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        code, _out, err = _run(["store", "init", self.store])
        self.assertEqual(code, 0, err)

    def test_probe_sem_binding_bloqueia(self):
        code, out, err = _run(
            ["doctor", "--store", self.store, "--base", self.base, "--probe-agent", "--json"]
        )
        # §10.3: `blocked` -> exit 2 (era 1, mesma convenção de sempre p/
        # doctor com bloqueios; só o número mudou, no esquema comum 0/2/3).
        self.assertEqual(code, 2, err)
        payload = json.loads(out)
        data = payload["summary"]["detail"]
        self.assertIn("agent_probe", data["bloqueios"])
        self.assertTrue(data["sonda_agente"]["bloqueado"])
        self.assertIn("agent", data)
        self.assertIn("agent", payload)  # também no topo do envelope comum

    def test_probe_com_binding_local_ok(self):
        reg = A.default_registry()
        binding = reg.connect("local", "auto")
        BindingStore(self.store).save_binding(binding, repo=None)

        code, out, err = _run(
            ["doctor", "--store", self.store, "--base", self.base, "--probe-agent", "--json"]
        )
        data = json.loads(out)["summary"]["detail"]
        self.assertNotIn("agent_probe", data["bloqueios"])
        self.assertTrue(data["sonda_agente"]["ok"], data["sonda_agente"])
        self.assertTrue(data["sonda_agente"]["integration_available"])
        self.assertTrue(data["sonda_agente"]["agent_connected"])
        self.assertTrue(data["sonda_agente"]["access_usable"])
        self.assertTrue(data["sonda_agente"]["contract_accepted"])

    def test_doctor_sem_probe_agent_tem_bloco_agent_mas_nao_sonda(self):
        code, out, err = _run(["doctor", "--store", self.store, "--base", self.base, "--json"])
        payload = json.loads(out)
        self.assertIn("agent", payload)
        self.assertIn("agent", payload["summary"]["detail"])
        self.assertNotIn("sonda_agente", payload["summary"]["detail"])


# --------------------------------------------------------------------------
# 6) seleção de modo/agente (`--mode`/`--agent`/`--engine` compat)
# --------------------------------------------------------------------------


class ResolveModeAndAgentTests(unittest.TestCase):
    def test_sem_flags_e_sempre_deep_nunca_implicito(self):
        ns = types.SimpleNamespace(mode=None, agent=None, engine=None)
        sel = cli._resolve_mode_and_agent(ns)
        self.assertEqual(sel, {"mode": "deep", "agent": None, "engine_compat": None})

    def test_engine_local_equivale_a_mode_structural(self):
        ns = types.SimpleNamespace(mode=None, agent=None, engine="local")
        sel = cli._resolve_mode_and_agent(ns)
        self.assertEqual(sel, {"mode": "structural", "agent": "local", "engine_compat": "local"})

    def test_engine_claude_cli_equivale_a_agent_claude_code(self):
        ns = types.SimpleNamespace(mode=None, agent=None, engine="claude-cli")
        sel = cli._resolve_mode_and_agent(ns)
        self.assertEqual(sel, {"mode": "deep", "agent": "claude-code", "engine_compat": "claude-cli"})

    def test_mode_e_agent_explicitos_vencem_engine(self):
        ns = types.SimpleNamespace(mode="deep", agent="fake-cli", engine="local")
        sel = cli._resolve_mode_and_agent(ns)
        self.assertEqual(sel, {"mode": "deep", "agent": "fake-cli", "engine_compat": "local"})


class ResolveDispatchEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = _tmpdir("wk_resolve_dispatch_store_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)

    def test_structural_e_sempre_local_explicito_sem_binding_store(self):
        engine_name, _bs, blocked = cli._resolve_dispatch_engine(
            self.store, None, {"mode": "structural", "agent": None},
        )
        self.assertEqual(engine_name, "local")
        self.assertIsNone(blocked)

    def test_deep_sem_preferencia_bloqueia(self):
        engine_name, _bs, blocked = cli._resolve_dispatch_engine(
            self.store, None, {"mode": "deep", "agent": None},
        )
        self.assertIsNone(engine_name)
        self.assertIsNotNone(blocked)
        self.assertIsNone(blocked["agent_id"])

    def test_deep_com_binding_conectado_resolve_o_agente(self):
        reg = A.default_registry()
        binding = reg.connect("local", "auto")
        BindingStore(self.store).save_binding(binding, repo=None)

        engine_name, _bs, blocked = cli._resolve_dispatch_engine(
            self.store, None, {"mode": "deep", "agent": None},
        )
        self.assertEqual(engine_name, "local")
        self.assertIsNone(blocked)


# --------------------------------------------------------------------------
# 7) `analyze --mode deep` sem binding: bloqueado, sem tarefa, sem rodada
# --------------------------------------------------------------------------


class AnalyzeDeepBlockedTests(unittest.TestCase):
    def test_analyze_mode_deep_sem_binding_bloqueia_sem_criar_nada(self):
        store = _tmpdir("wk_analyze_deep_store_")
        repo = _tmpdir("wk_analyze_deep_repo_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)

        code, out, err = _run(["analyze", "--repo", repo, "--store", store, "--json"])
        self.assertEqual(code, 2, err)
        payload = json.loads(out)
        self.assertEqual(payload["command"], "analyze")
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertIn("agent", payload)
        # nenhum trabalho aconteceu: nem runtime.db, nem manifesto de snapshot.
        self.assertFalse(os.path.exists(os.path.join(store, "runtime.db")))
        self.assertFalse(os.path.isdir(os.path.join(store, ".analysis", "snapshots")))

    def test_analyze_mode_structural_nao_bloqueia_por_falta_de_binding(self):
        """`--mode structural` (== `--engine local`) nunca depende de
        `BindingStore`: o bloqueio acima é exclusivo do modo `deep`."""
        store = _tmpdir("wk_analyze_structural_store_")
        repo = _tmpdir("wk_analyze_structural_repo_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)

        code, out, err = _run([
            "analyze", "--repo", repo, "--store", store, "--mode", "structural", "--json",
        ])
        # Repo vazio (sem capacidades) ainda assim conclui a análise estrutural
        # (nunca `operation_status: blocked` por falta de agente — isso é
        # exclusivo do modo `deep`).
        payload = json.loads(out)
        self.assertNotEqual(payload["operation_status"], "blocked")
        # §10.2/§10.3: `analyze --json` é o envelope comum; o schema antigo
        # (`escopo_efetivo`, ...) sobrevive tal-e-qual em `summary.detail`.
        data = payload["summary"]["detail"]
        self.assertEqual(data["escopo_efetivo"]["modo"], "structural")
        self.assertIn("agent", payload)


# --------------------------------------------------------------------------
# 8) `_connect_chain_binding` pelo contrato adapter+binding — `local` e uma
#    extensão real (T32) continuam despachando exatamente igual
#
# Portado do antigo `DispatchObjectivesContractTests` (testava
# `cli._dispatch_objectives`, removido nesta limpeza de código morto — sem
# chamador de produção desde que o laço de continuação virou UMA chamada por
# invocação, §7.3; quem conecta a engine da cadeia hoje é
# `_connect_chain_binding`, chamada por `_run_investigation_chain`). O
# despacho de verdade (`rt_coordinator.run`, não `run_chain` — aqui só se
# testa o CONNECT + o contrato comum de submissão, não o laço de rodadas)
# usa exatamente o `(adapter, binding)` que `_connect_chain_binding` devolve
# — a mesma garantia dos 4 testes antigos, contra o alvo novo.
# --------------------------------------------------------------------------


def _tmp_task_store(objective: dict, inputs: dict):
    from runtime import tasks as rt_tasks

    tmpdir = tempfile.mkdtemp(prefix="wk_dispatch_store_")
    store = rt_tasks.TaskStore.open(os.path.join(tmpdir, "runtime.db"))
    # `budget={}` faz `context.build_package` estourar `BudgetExceeded` de
    # cara (o mesmo orçamento real usado por `cmd_analyze`, ver `cli.
    # _DEFAULT_TASK_BUDGET`) — sem isto a tarefa nunca chega a ser SUBMETIDA
    # (vira `blocked:budget` antes do `adapter.submit`).
    task = store.create_task(
        rt_tasks.TaskKind.INVESTIGATION, objective, inputs, budget=cli._DEFAULT_TASK_BUDGET,
    )
    store.refresh_states()  # PENDING -> READY (sem isto `ready_tasks()` não a acha)
    return tmpdir, store, task


def _connect_and_dispatch(store, engine_name, *, resolver=None):
    """Mesmo par connect+despacho que o antigo `_dispatch_objectives` fazia
    internamente — reconstruído aqui a partir das duas peças que sobrevivem
    em produção (`cli._connect_chain_binding` + `runtime.coordinator.run`),
    para os testes que precisam do dispatch completo (não só do connect)."""
    from runtime import context as rt_context
    from runtime import coordinator as rt_coordinator

    registry, adapter, binding, bloqueios = cli._connect_chain_binding(engine_name, None)
    if binding is None:
        return dict(cli._EMPTY_RESULTADOS), bloqueios
    try:
        report = rt_coordinator.run(
            store, context_builder=rt_context.build_package, max_concurrency=4,
            resolver=resolver, adapter=adapter, binding=binding, bindings=None,
        )
    finally:
        registry.close(binding)
    return report.summary(), bloqueios


class ConnectChainBindingContractTests(_ExtensionEnvMixin, unittest.TestCase):
    def test_engine_local_continua_equivalente_via_adapter_binding(self):
        objetivo = {
            "kind": "capability", "objective_id": "obj_1", "capability_id": "cap1",
            "state": "partial", "contract": {},
        }
        inputs = {"snapshot_id": "scope:x", "source_version_ids": ["app/a.py@sha"]}
        tmpdir, store, task = _tmp_task_store(objetivo, inputs)
        try:
            resultados, bloqueios = _connect_and_dispatch(store, "local")
            self.assertEqual(bloqueios, [])
            self.assertEqual(resultados["submitted"], 1)
            self.assertEqual(resultados["accepted"], 1)
            done = store.get(task.task_id)
            from runtime import tasks as rt_tasks
            self.assertEqual(done.state, rt_tasks.TaskState.DONE)
            self.assertEqual(done.result.get("objective_id"), "obj_1")
        finally:
            store.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_engine_desconhecida_bloqueia_sem_propagar_excecao(self):
        objetivo = {"kind": "capability", "objective_id": "obj_1"}
        inputs = {"snapshot_id": "scope:x", "source_version_ids": []}
        tmpdir, store, _task = _tmp_task_store(objetivo, inputs)
        try:
            resultados, bloqueios = _connect_and_dispatch(store, "engine-nunca-existiu")
            self.assertEqual(resultados, dict(cli._EMPTY_RESULTADOS))
            self.assertEqual(len(bloqueios), 1)
            self.assertEqual(bloqueios[0]["tipo"], "engine_desconhecida")
        finally:
            store.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_claude_code_sem_binario_vira_bloqueio_dispatch_indisponivel(self):
        """Ambiente de teste sem `claude` no PATH: handshake falha e vira
        `dispatch_indisponivel` — nunca propaga exceção nem trava o laço."""
        objetivo = {"kind": "capability", "objective_id": "obj_1"}
        inputs = {"snapshot_id": "scope:x", "source_version_ids": []}
        tmpdir, store, _task = _tmp_task_store(objetivo, inputs)
        try:
            resultados, bloqueios = _connect_and_dispatch(store, "claude-code")
            self.assertEqual(resultados, dict(cli._EMPTY_RESULTADOS))
            self.assertEqual(len(bloqueios), 1)
            self.assertEqual(bloqueios[0]["tipo"], "dispatch_indisponivel")
        finally:
            store.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_extensao_fake_cli_despacha_pelo_contrato_comum(self):
        self._use_extension("make_ok_fake_cli")
        objetivo = {"objective_id": "obj_1"}
        inputs = {"snapshot_id": "scope:x", "source_version_ids": []}
        tmpdir, store, task = _tmp_task_store(objetivo, inputs)
        try:
            resultados, bloqueios = _connect_and_dispatch(store, "fake-cli")
            self.assertEqual(bloqueios, [])
            self.assertEqual(resultados["accepted"], 1)
            done = store.get(task.task_id)
            self.assertEqual(done.result.get("objective_id"), "obj_1")
        finally:
            store.close()
            shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------
# 9) `status` sem `--repo` — visão do store
# --------------------------------------------------------------------------


class StatusStoreScopeTests(unittest.TestCase):
    def test_status_sem_repo_devolve_visao_do_store(self):
        store = _tmpdir("wk_status_store_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        code, out, err = _run(["status", "--store", store, "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        # §10.2/§10.3: envelope comum; os campos antigos seguem em `summary.detail`.
        data = payload["summary"]["detail"]
        self.assertEqual(data["escopo"], "store")
        self.assertEqual(data["sistemas"], [])
        self.assertEqual(data["iniciativas"], [])
        self.assertIn("agent", payload)
        self.assertIn("delivery_status", payload)

    def test_status_sem_repo_com_initiative_filtro_aparece_mesmo_sem_historico(self):
        store = _tmpdir("wk_status_store_init_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        code, out, err = _run(["status", "--store", store, "--initiative", "INIC-1", "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)["summary"]["detail"]
        self.assertIn("INIC-1", data["iniciativas"])
        self.assertEqual(data["iniciativa_filtro"], "INIC-1")

    def test_status_com_repo_nao_registrado_continua_bloqueando(self):
        """§10.3: `--repo` que este store NUNCA conheceu (nem `wk init
        --repo`, nem `wk analyze`) continua `blocked`/exit 2 — é isto que
        distingue "repo não registrado" de "repo registrado sem análise"
        (ver `StatusRepoRegistradoSemAnaliseTests` abaixo, que cobre o caso
        que passou a ser `succeeded`/exit 0)."""
        store = _tmpdir("wk_status_repo_store_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        repo = _tmpdir("wk_status_repo_repo_")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        code, out, err = _run(["status", "--store", store, "--repo", repo])
        self.assertEqual(code, 2)  # repo nunca registrado neste store: bloqueado
        # §10.3: precondição inválida agora vai pelo envelope comum (stdout,
        # texto humano por padrão) — não mais um `print` solto em stderr.
        self.assertIn("não registrado", out)


class StatusRepoRegistradoSemAnaliseTests(unittest.TestCase):
    """§10.3: `status --repo` num repo REGISTRADO (via `wk init --repo`) mas
    ainda sem `wk analyze` é conhecimento incompleto normal — `succeeded`,
    exit 0, `next_actions` aponta `wk analyze` — nunca `blocked`/exit 2."""

    def test_repo_registrado_via_init_sem_analise_e_succeeded_exit0(self):
        store = _tmpdir("wk_status_registrado_store_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        repo = _tmpdir("wk_status_registrado_repo_")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)

        code_init, out_init, err_init = _run(["init", "--store", store, "--repo", repo, "--json"])
        self.assertIn(code_init, (0, 3), err_init)  # `init` nunca bloqueia por si só aqui

        code, out, err = _run(["status", "--store", store, "--repo", repo, "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "succeeded")
        self.assertEqual(payload["knowledge_status"], "not_applicable")
        self.assertTrue(payload["next_actions"], payload)
        na = payload["next_actions"][0]
        self.assertIn("analyze", na["argv"])
        self.assertIn(repo, na["argv"])

    def test_store_inexistente_continua_bloqueando(self):
        """Store que nem existe em disco: `blocked`/exit 2 (nunca 0 — aqui a
        LEITURA em si falha, distinto de "repo sem análise")."""
        tmp = _tmpdir("wk_status_store_inexistente_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = os.path.join(tmp, "nao-existe")
        repo = _tmpdir("wk_status_store_inexistente_repo_")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        code, out, err = _run(["status", "--store", store, "--repo", repo, "--json"])
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "blocked")


if __name__ == "__main__":
    unittest.main()
