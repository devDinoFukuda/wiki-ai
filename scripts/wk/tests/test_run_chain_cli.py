"""§7.3 pela CLI: `cli._run_investigation_chain` (usada por `cmd_analyze`/
`cmd_update`/`cmd_resume`) roda o laço inteiro numa chamada só, com um
worker EXTERNO roteirizado (registrado via `WIKI_AI_AGENT_ADAPTERS`, o mesmo
mecanismo de extensão de `runtime.agents.AgentRegistry.load_extensions`) que
devolve resultados PROGRAMADOS por rodada — nunca um `while` desta suíte.

Dono exclusivo: `scripts/wk/cli.py` e `scripts/wk/tests/**`. Não importa
implementação de `runtime`/`knowledge` além do que os módulos já expõem como
API pública (mesmo padrão de `scripts/knowledge/tests/
test_integrate_estado_acumulado.py` e `scripts/runtime/tests/
test_run_chain.py`, citados como referência).

T06 (leituras A depois B em rodadas distintas -> ambas permanecem
satisfeitas, contexto acumulado) roda com um repositório REAL de dois
arquivos (`app/a.py`/`app/b.py`) para que a evidência do worker resolva de
verdade contra o snapshot (`knowledge.integrate._satisfy_readings` exige
localizador resolvível, não só declaração).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from analysis.extractors.base import SourceFile
from analysis.extractors.registry import default_registry as extractor_registry
from analysis.investigation import (
    CONTRACT_FIELDS,
    CONTRACT_LABELS,
    ContractField,
    InvestigationObjective,
    ObjectiveKind,
    ReadingKind,
    ReadingNeed,
    ReadingTrigger,
)
from analysis.snapshot import capture
from runtime import agents as A
from runtime import tasks as rt_tasks
from runtime.coordinator import plan_from_objectives
from runtime.executors.agent_adapters import BaseExecutorAdapter

from wk import cli

CODIGO_A = "def a():\n    return 1\n"
CODIGO_B = "def b():\n    return 2\n"


# ---------------------------------------------------------------------------
# fixtures mínimas de capacidade/objetivo (mesmo padrão de
# scripts/wk/tests/test_fix_onda10d.py — `_Cap`/`_CapMap`)
# ---------------------------------------------------------------------------


class _Cap:
    def __init__(self, capability_id, paths):
        self.capability_id = capability_id
        self.paths = tuple(paths)


class _CapMap:
    def __init__(self, caps):
        self._by_id = {c.capability_id: c for c in caps}

    def by_id(self, cap_id):
        return self._by_id.get(cap_id)


def _extraction_of(snapshot):
    registry = extractor_registry()
    files = []
    for entry in snapshot.files:
        with open(os.path.join(snapshot.repo, entry.path), "r", encoding="utf-8") as fh:
            files.append(SourceFile(path=entry.path, content=fh.read()))
    return registry.extract_all(files)


def _objective(oid: str, cap_id: str) -> InvestigationObjective:
    return InvestigationObjective(
        objective_id=oid,
        kind=ObjectiveKind.CAPABILITY,
        capability_id=cap_id,
        name=cap_id,
        contract={n: ContractField(name=n, label=CONTRACT_LABELS[n]) for n in CONTRACT_FIELDS},
        reading_needs=[
            ReadingNeed(
                need_id="n1", kind=ReadingKind.SYMBOL, target="app/a.py",
                motivo="dependencia declarada em a.py", trigger=ReadingTrigger.UNRESOLVED_CALL,
            ),
            ReadingNeed(
                need_id="n2", kind=ReadingKind.SYMBOL, target="app/b.py",
                motivo="dependencia declarada em b.py", trigger=ReadingTrigger.UNRESOLVED_CALL,
            ),
        ],
    )


def _setup(tmp_root: str, *, oid: str = "obj-1", cap_id: str = "cap-1") -> dict:
    repo_dir = os.path.join(tmp_root, "repo")
    os.makedirs(os.path.join(repo_dir, "app"), exist_ok=True)
    with open(os.path.join(repo_dir, "app", "a.py"), "w", encoding="utf-8") as fh:
        fh.write(CODIGO_A)
    with open(os.path.join(repo_dir, "app", "b.py"), "w", encoding="utf-8") as fh:
        fh.write(CODIGO_B)

    snapshot = capture(repo_dir)
    extraction = _extraction_of(snapshot)
    capability_map = _CapMap([_Cap(cap_id, ["app/a.py", "app/b.py"])])
    objective = _objective(oid, cap_id)
    objectives = [objective]
    objectives_by_id = {oid: objective}
    objective_dicts = [objective.to_dict()]
    inputs_by_objective = cli._objective_input_versions(
        objective_dicts, snapshot, capability_map, {}
    )

    store_root = os.path.join(tmp_root, "store")
    os.makedirs(store_root, exist_ok=True)
    namespace = f"code/{cli._repo_key(repo_dir)}"
    store = rt_tasks.TaskStore.open(cli._runtime_db_path(store_root))
    inputs = inputs_by_objective[oid]
    plan_from_objectives(
        store, objective_dicts, snapshot_id=inputs["snapshot_id"],
        source_version_ids=inputs["source_version_ids"],
        kind=rt_tasks.TaskKind.INVESTIGATION, budget=cli._DEFAULT_TASK_BUDGET,
    )
    return {
        "repo_dir": repo_dir, "store_root": store_root, "namespace": namespace,
        "snapshot": snapshot, "extraction": extraction, "capability_map": capability_map,
        "objectives": objectives, "objectives_by_id": objectives_by_id,
        "inputs_by_objective": inputs_by_objective, "store": store, "oid": oid,
    }


# ---------------------------------------------------------------------------
# worker externo roteirizado — registrado via WIKI_AI_AGENT_ADAPTERS
# (mesmo mecanismo de scripts/wk/tests/test_agent_cli.py)
# ---------------------------------------------------------------------------

#: Passo GLOBAL entre invocações: uma nova conexão (`registry.connect`) cria
#: um `_ScriptedExec` NOVO a cada `_run_investigation_chain` — o contador
#: precisa sobreviver a isso para que uma retomada leia o próximo item do
#: roteiro, não o primeiro de novo (mesmo racional do `self._passo` de
#: `runtime.tests.test_run_chain.RunChainTest._outcomes_factory`).
_STEP = {"n": 0}
_SCRIPT: list = []
_FAIL_HANDSHAKE = {"value": False}


def _reset_worker(script=None, *, fail=False):
    _STEP["n"] = 0
    _SCRIPT[:] = list(script or [])
    _FAIL_HANDSHAKE["value"] = bool(fail)


class _ScriptedExec:
    """Cada `submit` devolve `_SCRIPT[_STEP]` e avança o passo GLOBAL."""

    def capabilities(self) -> dict:
        return {
            "dispatch": True, "concurrency": 2, "cancellation": "process-kill",
            "structured_output": True, "deepening": True, "tools": [],
        }

    def submit(self, task_id, objective, references=None, schema=None, policy=None) -> str:
        idx = min(_STEP["n"], len(_SCRIPT) - 1) if _SCRIPT else 0
        _STEP["n"] += 1
        execution_id = f"scripted:{task_id}:{idx}"
        payload = dict(_SCRIPT[idx]) if _SCRIPT else {}
        payload.setdefault("objective_id", (objective or {}).get("objective_id"))
        self._out = payload
        _RECORDS[execution_id] = payload
        return execution_id

    def status(self, execution_id) -> dict:
        return {"state": "done"}

    def result(self, execution_id) -> dict:
        return {"execution_id": execution_id, "output": _RECORDS.get(execution_id, {})}

    def cancel(self, execution_id) -> bool:
        return True


_RECORDS: dict = {}


class _ScriptedAdapter(BaseExecutorAdapter):
    agent_id = "scripted-chain"
    adapter_version = "scripted-chain/1"

    def describe(self) -> A.AgentDescriptor:
        return A.AgentDescriptor(
            agent_id=self.agent_id,
            adapter_version=self.adapter_version,
            transports=("process",),
            capabilities=A.AgentCapabilities(dispatch=True, deepening=True, physical_cancellation=True),
            verified_versions=("fake/1",),
            distribution="extension",
            summary="worker roteirizado por rodada (testes §7.3 de scripts/wk/tests)",
        )

    def _handshake(self, transport: str, config: dict) -> dict:
        if _FAIL_HANDSHAKE["value"]:
            return {"connected": False, "detail": "handshake recusado no teste (scripted-chain)"}
        return {"connected": True, "detail": "ok", "host_version": "fake/1", "model": None}

    def _new_executor(self, config: dict):
        return _ScriptedExec()


def make_scripted_worker():
    return _ScriptedAdapter()


# ---------------------------------------------------------------------------
# base comum dos testes de cadeia
# ---------------------------------------------------------------------------


class _ChainTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-run-chain-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._prev_ext = os.environ.get(A.EXTENSIONS_ENV)
        os.environ[A.EXTENSIONS_ENV] = f"{__name__}:make_scripted_worker"
        self.addCleanup(self._restore_ext)
        _reset_worker([])
        _RECORDS.clear()

    def _restore_ext(self):
        if self._prev_ext is None:
            os.environ.pop(A.EXTENSIONS_ENV, None)
        else:
            os.environ[A.EXTENSIONS_ENV] = self._prev_ext

    def _new_ctx(self):
        ctx = _setup(self.tmp)
        self.addCleanup(ctx["store"].close)
        return ctx

    def _run_chain(self, ctx, *, max_rounds=None, engine_name="scripted-chain"):
        resolver = cli._snapshot_resolver(ctx["snapshot"])
        return cli._run_investigation_chain(
            ctx["store"], store_root=ctx["store_root"], namespace=ctx["namespace"],
            snapshot=ctx["snapshot"], extraction=ctx["extraction"],
            objectives=ctx["objectives"], capability_map=ctx["capability_map"],
            objectives_by_id=ctx["objectives_by_id"],
            inputs_by_objective=ctx["inputs_by_objective"],
            engine_name=engine_name, bindings_store=None, resolver=resolver,
            reason="teste §7.3", max_rounds=max_rounds,
        )


_RESULTADO_A = {
    "objective_id": "obj-1", "capability_id": "cap-1",
    "contract": {"decisoes": {"status": "filled", "content": "A"}},
    "reading_satisfied": [{
        "need_id": "n1", "target": "app/a.py",
        "evidence_refs": [{"path": "app/a.py", "line_start": 1, "line_end": 2}],
    }],
}
_RESULTADO_B = {
    "objective_id": "obj-1", "capability_id": "cap-1",
    "contract": {"falhas": {"status": "filled", "content": "B"}},
    "reading_satisfied": [{
        "need_id": "n2", "target": "app/b.py",
        "evidence_refs": [{"path": "app/b.py", "line_start": 1, "line_end": 2}],
    }],
}
_RESULTADO_SEM_PROGRESSO = {
    # `decisoes` filled desde a rodada 0 mantém o objetivo em `partial` (não
    # `blocked`: `InvestigationObjective.evaluate()` só fica `partial` quando
    # ALGUM campo do contrato está `filled` — sem isso o objetivo nunca teria
    # chegado a rodar uma continuação para medir "sem progresso").
    "objective_id": "obj-1", "capability_id": "cap-1",
    "contract": {"decisoes": {"status": "filled", "content": "sempre a mesma coisa"}},
    "reading_satisfied": [],
}


class T06MultiRoundaTest(_ChainTestBase):
    def test_rodada_a_depois_b_numa_invocacao_so_acumula_a_mais_b(self):
        """T06: leituras A e B em rodadas DISTINTAS, ambas permanecem
        satisfeitas — e o laço roda as duas rodadas NA MESMA invocação
        (nenhum `wk resume` intermediário)."""
        ctx = self._new_ctx()
        _reset_worker([_RESULTADO_A, _RESULTADO_B])

        resultado = self._run_chain(ctx, max_rounds=5)
        ctx["store"].close()

        chain = resultado["chain"]
        self.assertGreaterEqual(chain["rounds"], 1, chain)
        estado = chain["state"]["obj-1"]
        self.assertEqual(estado["contract"]["decisoes"]["content"], "A")
        self.assertEqual(estado["contract"]["falhas"]["content"], "B")
        self.assertEqual(
            set(estado["satisfied_readings"]), {"n1", "n2"},
            "leitura fechada na rodada A precisa continuar fechada depois de B",
        )
        # o JSON de integração (o que `summary.detail.integracao` publica)
        # também reflete as DUAS leituras satisfeitas — não só o estado bruto.
        leituras = resultado["integracao"]["leituras"]
        self.assertEqual(leituras["satisfeitas"], 2, leituras)
        self.assertEqual(
            {item["need_id"] for item in leituras["itens_satisfeitos"]}, {"n1", "n2"},
        )
        # duas rodadas de fato despacharam duas tarefas (base + 1 continuação).
        self.assertEqual(_STEP["n"], 2)


class NoProgressTest(_ChainTestBase):
    def test_para_em_no_progress_sem_criar_tarefa_repetida(self):
        """Worker que nunca fecha nem declara nada de novo: a cadeia para
        (`no_progress`) e NÃO cria uma segunda continuação para o mesmo
        pacote — só a base + no máximo uma continuação."""
        ctx = self._new_ctx()
        _reset_worker([_RESULTADO_SEM_PROGRESSO])

        resultado = self._run_chain(ctx, max_rounds=5)
        chain = resultado["chain"]
        tarefas = ctx["store"].all_tasks()
        ctx["store"].close()

        self.assertEqual(chain["stop_reason"], "no_progress", chain)
        self.assertLessEqual(len(tarefas), 2, "pacote idêntico não pode gerar 3ª tarefa")


class MaxRoundsTest(_ChainTestBase):
    def test_max_rounds_e_teto_total_entre_duas_invocacoes(self):
        """`--max-rounds` é o teto TOTAL da cadeia: uma segunda invocação sem
        ampliar não ganha rodada nova; ampliar de 0 para 1 libera exatamente
        mais uma (fecha n2/B). `max_rounds=0` proíbe QUALQUER continuação
        (round_no da 1ª continuação é 1, sempre > 0) — a tarefa-base (que
        fecha n1/A) não conta como rodada (§7.3), então a rodada 0 nunca
        aparece em `rounds_used`."""
        ctx = self._new_ctx()
        _reset_worker([_RESULTADO_A, _RESULTADO_B])

        primeira = self._run_chain(ctx, max_rounds=0)
        self.assertEqual(primeira["chain"]["stop_reason"], "budget_exhausted", primeira["chain"])
        usado = ctx["store"].chain_status("obj-1")["rounds_used"]
        self.assertEqual(usado, 0, "tarefa-base não conta como rodada da cadeia")
        estado1 = primeira["chain"]["state"]["obj-1"]
        self.assertIn("n1", estado1["satisfied_readings"], "rodada-base ainda fecha n1/A")
        self.assertNotIn("n2", estado1["satisfied_readings"])

        # segunda invocação, MESMO teto: nenhum crédito novo.
        segunda = self._run_chain(ctx, max_rounds=0)
        self.assertEqual(segunda["chain"]["stop_reason"], "budget_exhausted", segunda["chain"])
        self.assertEqual(segunda["chain"]["rounds"], 0, "invocação nova não ganha rodada")
        self.assertEqual(ctx["store"].chain_status("obj-1")["rounds_used"], 0)

        # ampliar de 0 para 1 libera exatamente mais uma rodada (fecha n2/B).
        terceira = self._run_chain(ctx, max_rounds=1)
        self.assertEqual(ctx["store"].chain_status("obj-1")["rounds_used"], 1)
        estado = terceira["chain"]["state"]["obj-1"]
        self.assertIn("n2", estado["satisfied_readings"])


class ExecutorUnavailableTest(_ChainTestBase):
    def test_executor_indisponivel_nao_consome_rodada_e_preserva_progresso(self):
        ctx = self._new_ctx()
        _reset_worker([_RESULTADO_A, _RESULTADO_B])

        primeira = self._run_chain(ctx, max_rounds=5)
        self.assertNotEqual(primeira["chain"]["stop_reason"], "executor_unavailable")
        usado_antes = ctx["store"].chain_status("obj-1")["rounds_used"]
        self.assertGreaterEqual(usado_antes, 1)

        # engine agora recusa o handshake (ex.: credencial expirada) — a
        # retomada seguinte NÃO pode consumir rodada nem perder progresso.
        _FAIL_HANDSHAKE["value"] = True
        segunda = self._run_chain(ctx, max_rounds=5)
        ctx["store"].close()

        chain = segunda["chain"]
        self.assertEqual(chain["stop_reason"], "executor_unavailable", chain)
        self.assertEqual(chain["rounds"], 0)
        self.assertTrue(segunda["bloqueios"], "handshake recusado precisa aparecer em bloqueios")

    def test_next_action_de_executor_indisponivel_e_concreto(self):
        """§7.3: `executor_unavailable` -> `agent connect`/`agent setup` do
        agente selecionado, com argv completo (store/repo/agent) — nunca
        `resume` genérico como "próxima rodada"."""
        store_root = os.path.join(self.tmp, "store2")
        repo_abs = os.path.join(self.tmp, "repo2")
        os.makedirs(store_root, exist_ok=True)
        os.makedirs(repo_abs, exist_ok=True)
        operation_status, knowledge_status, pending, next_actions = cli._chain_envelope(
            command="analyze", store_root=store_root, repo_abs=repo_abs,
            chain={"stop_reason": "executor_unavailable", "detail": "handshake recusado",
                   "chain": {}},
            agent_id="scripted-chain",
        )
        self.assertEqual(operation_status, "blocked")
        self.assertTrue(next_actions)
        na = next_actions[0]
        self.assertIn("agent", na["argv"])
        self.assertIn("connect", na["argv"])
        self.assertIn("scripted-chain", na["argv"])
        self.assertIn(store_root, na["argv"])


# ---------------------------------------------------------------------------
# ingest --json / saída humana (§10.2/§10.3)
# ---------------------------------------------------------------------------


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class IngestEnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-ingest-envelope-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.src = os.path.join(self.tmp, "doc.md")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("# Documento\n\nConteudo qualquer.\n")

    def test_ingest_json_devolve_envelope_comum(self):
        code, out, err = _run_cli(["ingest", self.src, "--store", self.store, "--json"])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(payload["command"], "ingest")
        self.assertIn(payload["operation_status"], ("succeeded", "partial", "blocked", "noop"))
        self.assertIn("fontes", payload["summary"]["detail"])
        self.assertEqual(code, cli._exit_code_common(payload["operation_status"]))

    def test_ingest_sem_json_e_saida_humana_concisa(self):
        code, out, err = _run_cli(["ingest", self.src, "--store", self.store])
        self.assertEqual(err, "", err)
        self.assertIn("resultado:", out)
        self.assertIn("conhecimento:", out)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# Tarefa 6: `wk ingest --repo R` associa ao sistema registrado (§11) — mesma
# convenção de namespace de `wk analyze --repo` (`code/<repo>`), derivada
# aqui em `cli.py`; `ingestion.correlate` já era agnóstico ao namespace
# (nenhuma mudança em `scripts/ingestion/**`).
# ---------------------------------------------------------------------------


class IngestRepoAssociationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-ingest-repo-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.src = os.path.join(self.tmp, "doc.md")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("# Documento\n\nConteudo qualquer.\n")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo, exist_ok=True)

    def test_ingest_com_repo_deriva_namespace_code_repo(self):
        code, out, err = _run_cli([
            "ingest", self.src, "--store", self.store, "--repo", self.repo, "--json",
        ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(payload["scope"]["repo"], os.path.abspath(self.repo))
        self.assertEqual(
            payload["scope"]["namespace"], f"code/{cli._repo_key(os.path.abspath(self.repo))}",
        )
        self.assertNotEqual(code, 2, out)

    def test_ingest_com_repo_e_namespace_explicito_namespace_vence(self):
        """`--namespace` explícito nunca é sobrescrito por `--repo` — só
        preenche o default quando `--namespace` foi omitido."""
        code, out, err = _run_cli([
            "ingest", self.src, "--store", self.store, "--repo", self.repo,
            "--namespace", "wiki", "--json",
        ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(payload["scope"]["namespace"], "wiki")
        self.assertEqual(payload["scope"]["repo"], os.path.abspath(self.repo))

    def test_ingest_sem_repo_continua_com_namespace_default(self):
        code, out, err = _run_cli(["ingest", self.src, "--store", self.store, "--json"])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertIsNone(payload["scope"]["repo"])
        self.assertEqual(payload["scope"]["namespace"], cli._INGEST2_DEFAULT_NAMESPACE)

    def test_ingest_repo_inexistente_e_erro_explicito_exit2(self):
        repo_falso = os.path.join(self.tmp, "nao-existe")
        code, out, err = _run_cli([
            "ingest", self.src, "--store", self.store, "--repo", repo_falso, "--json",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(out, "", out)
        payload = json.loads(err)
        self.assertIn(repo_falso, payload["error"])


# ---------------------------------------------------------------------------
# doctor.next_actions como argv estruturado (§10.2)
# ---------------------------------------------------------------------------


class DoctorNextActionArgvTest(unittest.TestCase):
    def test_store_ausente_next_action_e_argv_executavel(self):
        tmp = tempfile.mkdtemp(prefix="wk-doctor-argv-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = os.path.join(tmp, "nao-existe")
        base = os.path.join(tmp, "base")
        os.makedirs(base, exist_ok=True)
        a = argparse.Namespace(
            store=store, repo=None, engine=None, base=base, probe_agent=False, json=True,
        )
        code, out, err = self._run_doctor(a)
        payload = json.loads(out)
        na = payload["next_actions"][0]
        self.assertEqual(na["argv"], ["wk", "store", "init", store])
        self.assertIsNone(na["acao_externa"])

    def test_repo_invalido_next_action_e_manual_nao_argv(self):
        tmp = tempfile.mkdtemp(prefix="wk-doctor-argv-repo-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = os.path.join(tmp, "store")
        os.makedirs(store, exist_ok=True)
        base = os.path.join(tmp, "base")
        os.makedirs(base, exist_ok=True)
        repo = os.path.join(tmp, "repo-inexistente")
        a = argparse.Namespace(
            store=store, repo=repo, engine=None, base=base, probe_agent=False, json=True,
        )
        # isola o bloqueio de `--repo`: sem isto, a checagem de permissão
        # (que também roda quando --store/--repo são informados, e vem ANTES
        # de `repo` na prioridade de `cmd_doctor`) venceria primeiro, num
        # `base` fake que não tem settings gravado.
        with mock.patch.object(cli, "_check_permission_settings", return_value=(True, [], None)):
            code, out, err = self._run_doctor(a)
        payload = json.loads(out)
        na = payload["next_actions"][0]
        self.assertIsNone(na["argv"])
        self.assertIsNotNone(na["acao_externa"])
        self.assertIn(repo, na["acao_externa"])

    @staticmethod
    def _run_doctor(a):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_doctor(a)
        return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# precondições de analyze/update/resume/status no envelope comum (exit 2)
# ---------------------------------------------------------------------------


class PreconditionEnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-precondicoes-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")

    def _assert_envelope_blocked(self, code, out, command):
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(payload["command"], command)
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertTrue(payload["pending"])
        self.assertEqual(payload["pending"][0]["tipo"], "precondicao_invalida")

    def test_analyze_repo_inexistente_envelope_comum_exit2(self):
        repo = os.path.join(self.tmp, "nao-existe")
        code, out, err = _run_cli(["analyze", "--repo", repo, "--store", self.store, "--json"])
        self._assert_envelope_blocked(code, out, "analyze")

    def test_update_sem_analise_anterior_envelope_comum_exit2(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo, exist_ok=True)
        code, out, err = _run_cli(["update", "--repo", repo, "--store", self.store, "--json"])
        self._assert_envelope_blocked(code, out, "update")

    def test_resume_runtime_db_ausente_envelope_comum_exit2(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo, exist_ok=True)
        code, out, err = _run_cli(["resume", "--repo", repo, "--store", self.store, "--json"])
        self._assert_envelope_blocked(code, out, "resume")

    def test_status_runtime_db_ausente_envelope_comum_exit2(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo, exist_ok=True)
        code, out, err = _run_cli(["status", "--repo", repo, "--store", self.store, "--json"])
        self._assert_envelope_blocked(code, out, "status")


# ---------------------------------------------------------------------------
# A2: falha INESPERADA de `_run_investigation_chain` (bug, não motivo
# material de parada) vira o MESMO envelope comum — nunca traceback cru,
# nunca `exit` fora do vocabulário de `_exit_code_common`, e nunca apaga
# progresso já persistido.
# ---------------------------------------------------------------------------


class InternalErrorEnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-internal-error-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo, exist_ok=True)

    def _assert_internal_error_envelope(self, code, out, command):
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(payload["command"], command)
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertTrue(payload["pending"])
        pend = payload["pending"][0]
        self.assertEqual(pend["tipo"], "falha_interna")
        self.assertIn("RuntimeError", pend["causa"])
        self.assertIn("boom-interno", pend["causa"])
        argvs = [na["argv"] for na in payload["next_actions"] if na.get("argv")]
        self.assertTrue(any("doctor" in argv for argv in argvs), payload["next_actions"])
        self.assertTrue(any("resume" in argv for argv in argvs), payload["next_actions"])
        return payload

    def test_analyze_falha_interna_run_chain_vira_envelope_comum_exit2(self):
        with mock.patch.object(cli, "_run_investigation_chain", side_effect=RuntimeError("boom-interno")):
            code, out, err = _run_cli([
                "analyze", "--repo", self.repo, "--store", self.store,
                "--mode", "structural", "--json",
            ])
        self._assert_internal_error_envelope(code, out, "analyze")
        # `finally: store.close()` roda do mesmo jeito: `runtime.db` (criado
        # ANTES da falha) continua íntegro/legível por outro comando depois.
        self.assertTrue(os.path.exists(cli._runtime_db_path(self.store)))
        from runtime import tasks as rt_tasks
        reaberto = rt_tasks.TaskStore.open(cli._runtime_db_path(self.store))
        reaberto.close()

    def test_update_falha_interna_preserva_perfil_e_revisao_anteriores(self):
        code, out, err = _run_cli([
            "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "structural", "--json",
        ])
        self.assertIn(code, (0, 3), out)  # repo vazio: publicacoes sem documento -> partial/3, nunca blocked
        perfil_antes = cli._load_analysis_profile(self.store)
        revisao_antes = perfil_antes[cli._repo_key(self.repo)]["last_revision_id"]

        # força um delta — senão `cmd_update` sai por `noop` ANTES de chegar
        # em `_run_investigation_chain` e o patch nunca dispararia.
        with open(os.path.join(self.repo, "novo.py"), "w", encoding="utf-8") as fh:
            fh.write("def nova():\n    return 1\n")

        with mock.patch.object(cli, "_run_investigation_chain", side_effect=RuntimeError("boom-interno")):
            code, out, err = _run_cli([
                "update", "--repo", self.repo, "--store", self.store,
                "--mode", "structural", "--json",
            ])
        self._assert_internal_error_envelope(code, out, "update")

        # progresso da `wk analyze` anterior não foi apagado nem sobrescrito
        # pela falha desta `wk update` (nada é limpo no caminho de exceção).
        perfil_depois = cli._load_analysis_profile(self.store)
        self.assertEqual(
            perfil_depois[cli._repo_key(self.repo)]["last_revision_id"], revisao_antes,
        )

    def test_resume_falha_interna_preserva_tarefas_ja_persistidas(self):
        code, out, err = _run_cli([
            "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "structural", "--json",
        ])
        self.assertIn(code, (0, 3), out)  # repo vazio: publicacoes sem documento -> partial/3, nunca blocked

        from runtime import tasks as rt_tasks
        antes = rt_tasks.TaskStore.open(cli._runtime_db_path(self.store))
        tarefas_antes = {t.task_id: t.state.value for t in antes.all_tasks()}
        antes.close()

        with mock.patch.object(cli, "_run_investigation_chain", side_effect=RuntimeError("boom-interno")):
            code, out, err = _run_cli([
                "resume", "--repo", self.repo, "--store", self.store,
                "--mode", "structural", "--json",
            ])
        self._assert_internal_error_envelope(code, out, "resume")

        depois = rt_tasks.TaskStore.open(cli._runtime_db_path(self.store))
        tarefas_depois = {t.task_id: t.state.value for t in depois.all_tasks()}
        depois.close()
        self.assertEqual(tarefas_antes, tarefas_depois, "chain_status/tarefas persistidas intactas")


# ---------------------------------------------------------------------------
# Tarefa 4: `wk resume` numa cadeia já CONCLUÍDA (`stop_reason: completed`)
# nunca é `blocked`/exit 2 — `_CHAIN_STOP_OPERATION["completed"] ==
# "succeeded"` já garante isto (`cli.py` em torno de `_chain_envelope`); este
# teste fixa o comportamento ponta a ponta por `cmd_resume`, contra regressão.
# ---------------------------------------------------------------------------


def _cadeia_completed(objective_ids=()) -> dict:
    """Retorno sintético de `_run_investigation_chain` como se TODOS os
    objetivos já estivessem fechados nesta invocação (o cenário real de
    "nada para retomar") — mesma forma de retorno usada por `cmd_resume`
    (`integracao`/`investigation_states`/`revisao`/`bloqueios`/`resultados`/
    `chain`), só que sem exercitar `runtime.coordinator`/`knowledge.integrate`
    de verdade (construir um objetivo `complete` de ponta a ponta — 13 campos
    do §6.3 + matriz §6.5 + todas as leituras fechadas — pertence aos testes
    de `runtime`/`knowledge`, não a este)."""
    return {
        "chain": {
            "objective_ids": list(objective_ids), "rounds": 0,
            "stop_reason": "completed", "detail": "nenhum objetivo do escopo continua parcial",
            "state": {}, "chain": {}, "diagnostics": [],
        },
        "integracao": {}, "investigation_states": {}, "revisao": None,
        "bloqueios": [], "resultados": dict(cli._EMPTY_RESULTADOS),
    }


class ResumeChainCompletedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-resume-completed-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo, exist_ok=True)

    def test_resume_com_stop_reason_completed_nunca_e_blocked(self):
        code, out, err = _run_cli([
            "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "structural", "--json",
        ])
        self.assertIn(code, (0, 3), out)  # repo vazio: publicacoes sem documento -> partial/3

        with mock.patch.object(cli, "_run_investigation_chain", return_value=_cadeia_completed()):
            code, out, err = _run_cli([
                "resume", "--repo", self.repo, "--store", self.store,
                "--mode", "structural", "--json",
            ])
        payload = json.loads(out)
        self.assertEqual(payload["command"], "resume")
        # `stop_reason: completed` nunca é `blocked`/exit 2 — a única razão
        # para não ser `succeeded`/0 aqui é o repo vazio não ter documento
        # publicável (`publicacoes.bloqueios`, mecanismo TOTALMENTE separado
        # de `_chain_envelope`), que rebaixa para `partial`/exit 3, nunca
        # `blocked`/exit 2.
        self.assertNotEqual(payload["operation_status"], "blocked", payload)
        self.assertIn(payload["operation_status"], ("succeeded", "noop", "partial"), payload)
        self.assertNotEqual(code, 2, out)
        self.assertEqual(code, cli._exit_code_common(payload["operation_status"]))

    def test_chain_envelope_completed_mapeia_succeeded_nunca_blocked(self):
        """Mesma garantia, direto na tradução (`_chain_envelope`) que
        `cmd_analyze`/`cmd_update`/`cmd_resume` compartilham — `completed`
        SEMPRE `succeeded`, nunca `blocked`, independente de quem chama."""
        operation_status, knowledge_status, pending, next_actions = cli._chain_envelope(
            command="resume", store_root=self.store, repo_abs=self.repo,
            chain=_cadeia_completed()["chain"], agent_id="local",
        )
        self.assertEqual(operation_status, "succeeded")
        self.assertEqual(knowledge_status, "complete")
        self.assertEqual(pending, [])
        self.assertEqual(next_actions, [])
        self.assertEqual(cli._exit_code_common(operation_status), 0)


if __name__ == "__main__":
    unittest.main()
