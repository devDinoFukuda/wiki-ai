"""T15 — reprodução real pelo orquestrador (store temporário, `init --agent
local` + `agent connect --agent local`) e o pacote de auditoria associado.

Cobre A-D da reprodução e os achados 1/2/5/7 da auditoria subsequente
(mesmo dono: `scripts/wk/cli.py`/`scripts/wk/tests/**` — nunca
runtime/knowledge/publishing):

  A. `doctor --probe-agent` com binding `local` conectado sai `succeeded`/
     exit 0 (nunca `--engine` em `next_actions`/`proximo_passo`).
  B. `ingest` duplicado (no-op) em repo cujo escopo de análise está
     `partial` preserva `knowledge_status=partial` — nunca "nada mudou" ->
     `complete`.
  C. `ingest` `partial` sempre tem `next_actions` com argv concreto.
  D. `--mode deep` com um executor sem `deepening` (`local`) nunca cita o
     nome legado `claude-cli`; o `next_action` é `agent connect --agent
     claude-code` (ou o agente preferido).
  1. Lock entre processos/threads sobre `efeitos_publicacao_pendentes.json`.
  2. Falha de persistência do registro auxiliar de publicação nunca escapa
     como exceção não tratada — vira `blocked`/exit 2 com `pending`
     `tipo=falha_interna`.
  5. `runtime.agents.TransportUnavailable` (setup_steps verificados) tem
     prioridade sobre `agent connect`/`agent setup` genérico em
     doctor/agent connect/despacho.
  7. `wk update` sem delta retenta um efeito `publicacao_pendente` já
     registrado para o namespace, em vez de sair cedo sem olhar para ele.

Sem git, sem rede, sem e2e.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from analysis.capabilities import EvidenceRef
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
from runtime.bindings import BindingStore
from runtime.coordinator import plan_from_objectives

from wk import cli
from wk.tests.test_agent_cli import (
    _ExtensionEnvMixin,
    make_failing_fake_cli,  # noqa: F401 - usado via WIKI_AI_AGENT_ADAPTERS
    make_ok_fake_cli,  # noqa: F401 - usado via WIKI_AI_AGENT_ADAPTERS
)
from wk.tests.test_run_chain_cli import _CapMap, _Cap

_CODIGO_A = "def a():\n    return 1\n"
_CODIGO_B = "def b():\n    return 2\n"


def _extraction_of(snapshot):
    registry = extractor_registry()
    files = []
    for entry in snapshot.files:
        with open(os.path.join(snapshot.repo, entry.path), "r", encoding="utf-8") as fh:
            files.append(SourceFile(path=entry.path, content=fh.read()))
    return registry.extract_all(files)


def _objetivo_partial_sem_deepening(oid: str, cap_id: str) -> InvestigationObjective:
    """Objetivo com UM campo do contrato já `filled` (extração estática) e
    obrigações de leitura ainda abertas — `evaluate()` (§6.6) dá `partial`
    (nunca `blocked`: `blocked` exige NENHUM campo `filled`), o estado que
    `runtime.coordinator.plan_continuations` de fato avalia antes de recusar
    por falta de `deepening`."""
    contract = {n: ContractField(name=n, label=CONTRACT_LABELS[n]) for n in CONTRACT_FIELDS}
    contract["decisoes"].fill(
        "decisão já extraída pela análise estática",
        [EvidenceRef(path="app/a.py", line_start=1, line_end=1)],
    )
    return InvestigationObjective(
        objective_id=oid, kind=ObjectiveKind.CAPABILITY, capability_id=cap_id, name=cap_id,
        contract=contract,
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


def _setup_partial_sem_deepening(tmp_root: str, *, oid: str = "obj-1", cap_id: str = "cap-1") -> dict:
    """Mesma fixture de `wk.tests.test_run_chain_cli._setup`, com o objetivo
    trocado por `_objetivo_partial_sem_deepening` (o worker `local` nunca
    preenche contrato/leitura — precisa nascer `partial`, não `blocked`,
    para `plan_continuations` chegar a AVALIAR `engine_capabilities` de
    verdade)."""
    repo_dir = os.path.join(tmp_root, "repo")
    os.makedirs(os.path.join(repo_dir, "app"), exist_ok=True)
    with open(os.path.join(repo_dir, "app", "a.py"), "w", encoding="utf-8") as fh:
        fh.write(_CODIGO_A)
    with open(os.path.join(repo_dir, "app", "b.py"), "w", encoding="utf-8") as fh:
        fh.write(_CODIGO_B)

    snapshot = capture(repo_dir)
    extraction = _extraction_of(snapshot)
    capability_map = _CapMap([_Cap(cap_id, ["app/a.py", "app/b.py"])])
    objective = _objetivo_partial_sem_deepening(oid, cap_id)
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


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _tmpdir(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix)


def _sweep_text(payload: dict) -> str:
    """Todo texto de `next_actions`/`pending`/`proximo_passo`, concatenado —
    para a asserção "nenhuma substring banida em lugar nenhum destes campos"
    (§11: `--engine`/`claude-cli` nunca no fluxo padrão)."""
    partes = [json.dumps(payload.get("next_actions") or []), json.dumps(payload.get("pending") or [])]
    detalhe = payload.get("summary", {}).get("detail")
    if isinstance(detalhe, dict) and "proximo_passo" in detalhe:
        partes.append(json.dumps(detalhe["proximo_passo"]))
    return "\n".join(partes)


# ---------------------------------------------------------------------------
# A — doctor --probe-agent com binding local conectado: succeeded, sem
#     --engine em next_actions/proximo_passo (fluxo padrão, sem --engine)
# ---------------------------------------------------------------------------


class DoctorProbeLocalSemEngineTests(unittest.TestCase):
    def setUp(self):
        self.base = _tmpdir("wk_t15a_base_")
        self.store = _tmpdir("wk_t15a_store_")
        self.repo = _tmpdir("wk_t15a_repo_")
        for d in (self.base, self.store, self.repo):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # `init --agent local` (fluxo padrão) + `agent connect --agent local`
        # — EXATAMENTE a reprodução real do orquestrador, nunca `--engine`.
        code_i, out_i, err_i = _run(["init", "--store", self.store, "--agent", "local", "--json"])
        self.assertEqual(code_i, 0, err_i)
        code_c, out_c, err_c = _run([
            "agent", "connect", "--store", self.store, "--agent", "local", "--json",
        ])
        self.assertEqual(code_c, 0, err_c)

    def test_probe_com_local_conectado_sai_ok_sem_engine_em_lugar_nenhum(self):
        code, out, err = _run([
            "doctor", "--store", self.store, "--repo", self.repo, "--base", self.base,
            "--probe-agent", "--json",
        ])
        payload = json.loads(out)
        detail = payload["summary"]["detail"]
        # Defeito (1): sonda com binding local conectado deveria executar
        # `reg.probe(binding)` e sair `ok` — nunca bloquear por skill/
        # permissão de engine (infra do caminho de COMPATIBILIDADE, §10.1/
        # §11 — irrelevante para quem só usa `init`/`agent connect`).
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["operation_status"], "succeeded", detail)
        self.assertEqual(detail["agent"]["connection_status"], "connected")
        self.assertTrue(detail["sonda_agente"]["ok"], detail["sonda_agente"])
        self.assertNotIn("agent_probe", detail["bloqueios"])
        # Defeito (2): `--engine` NUNCA em next_actions/proximo_passo do
        # fluxo padrão.
        texto = _sweep_text(payload) + json.dumps(detail.get("proximo_passo"))
        self.assertNotIn("--engine", texto, texto)


# ---------------------------------------------------------------------------
# Sweep — nenhum next_action de doctor/analyze/update/resume/ingest/status
#         cita `--engine`/`claude-cli` (varrendo todos os next_actions)
# ---------------------------------------------------------------------------


class NextActionsSemEngineNemClaudeCliTests(unittest.TestCase):
    """`_chain_envelope` é a MESMA função usada por `analyze`/`update`/
    `resume` (3 chamadores idênticos, ver `cli.py`) — varrer o vocabulário
    fechado de `stop_reason` aqui cobre os 3 comandos de uma vez."""

    def _sem_banidos(self, next_actions: list, pending: list) -> None:
        texto = json.dumps(next_actions) + json.dumps(pending)
        self.assertNotIn("--engine", texto, texto)
        self.assertNotIn("claude-cli", texto, texto)

    def test_todos_os_stop_reason_conhecidos(self):
        casos = [
            {"stop_reason": "executor_unavailable", "detail": "engine indisponível", "chain": {}},
            {"stop_reason": "budget_exhausted", "detail": "orçamento", "chain": {}},
            {"stop_reason": "no_progress", "detail": "sem progresso semântico qualquer", "chain": {}},
            {
                "stop_reason": "no_progress",
                "detail": (
                    "engine sem capacidade de aprofundamento (deepening=False); "
                    "continuações exigem engine com leitura (ex.: claude-cli)"
                ),
                "chain": {}, "engine_capabilities": {"deepening": False},
            },
            {"stop_reason": "ambiguity", "detail": "ambiguidade X", "chain": {}},
            {"stop_reason": "evidence_changed", "detail": "invalidado", "chain": {}},
            {"stop_reason": "interrupted", "detail": "interrompido", "chain": {}},
            {"stop_reason": "algo-novo-desconhecido", "detail": "motivo novo", "chain": {}},
        ]
        for command in ("analyze", "update", "resume"):
            for chain in casos:
                with self.subTest(command=command, stop_reason=chain["stop_reason"]):
                    operation_status, knowledge_status, pending, next_actions = cli._chain_envelope(
                        command=command, store_root="S", repo_abs="R", chain=chain, agent_id="local",
                    )
                    self.assertIn(operation_status, ("succeeded", "partial", "blocked"))
                    self._sem_banidos(next_actions, pending)

    def test_ingest_partial_e_status_tambem_nunca_citam_engine_ou_claude_cli(self):
        tmp = _tmpdir("wk_t15_sweep_ingest_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = os.path.join(tmp, "store")
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo, exist_ok=True)
        src = os.path.join(tmp, "fonte.md")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("# Fonte\n\nConteudo qualquer.\n")

        code_i, out_i, err_i = _run(["init", "--store", store, "--repo", repo, "--json"])
        self.assertEqual(code_i, 0, err_i)

        # escopo de análise deste repo seedado como `partial` (mesma leitura
        # de `wk status --repo`) — força `ingest`/`status` pelo ramo
        # `partial` sem depender de capacidade real de extração.
        repo_abs = os.path.abspath(repo)
        namespace = f"code/{cli._repo_key(repo_abs)}"
        cli._save_analysis_profile(store, {
            cli._repo_key(repo_abs): {
                "namespace": namespace, "last_snapshot_id": "snap-1",
                "current_objective_ids": ["obj-1"],
            },
        })
        cli._save_last_integration(store, namespace, {
            "objetivos": [{"objective_id": "obj-1", "state": "partial"}],
        })

        code_g, out_g, err_g = _run([
            "ingest", src, "--store", store, "--repo", repo, "--json",
        ])
        payload_g = json.loads(out_g)
        self.assertEqual(code_g, 3, err_g)
        self._sem_banidos(payload_g["next_actions"], payload_g["pending"])

        code_s, out_s, err_s = _run(["status", "--store", store, "--repo", repo, "--json"])
        payload_s = json.loads(out_s)
        self.assertEqual(err_s, "", err_s)
        self._sem_banidos(payload_s["next_actions"], payload_s["pending"])


# ---------------------------------------------------------------------------
# B/C — ingest sobre escopo de análise `partial`
# ---------------------------------------------------------------------------


class IngestEscopoAnalisePartialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir("wk_t15bc_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo, exist_ok=True)
        self.src = os.path.join(self.tmp, "fonte.md")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("# Fonte de teste\n\nConteudo qualquer para ingestao.\n")

        code, out, err = _run(["init", "--store", self.store, "--repo", self.repo, "--json"])
        self.assertEqual(code, 0, err)

        self.repo_abs = os.path.abspath(self.repo)
        self.namespace = f"code/{cli._repo_key(self.repo_abs)}"
        # T15/B: escopo de análise (`wk analyze`/`resume`) deste repo já
        # `partial` — a MESMA leitura de `wk status --repo`
        # (`objetivos_por_estado`), gravada diretamente pelas funções reais
        # de persistência (`_save_analysis_profile`/`_save_last_integration`)
        # em vez de rodar uma cadeia de investigação inteira só para chegar
        # neste estado.
        cli._save_analysis_profile(self.store, {
            cli._repo_key(self.repo_abs): {
                "namespace": self.namespace, "last_snapshot_id": "snap-1",
                "current_objective_ids": ["obj-1"],
            },
        })
        cli._save_last_integration(self.store, self.namespace, {
            "objetivos": [{"objective_id": "obj-1", "state": "partial"}],
        })

    def _ingest(self):
        return _run(["ingest", self.src, "--store", self.store, "--repo", self.repo, "--json"])

    def test_c_primeira_ingestao_partial_tem_next_action_concreto(self):
        code, out, err = self._ingest()
        payload = json.loads(out)
        self.assertEqual(err, "", err)
        self.assertEqual(code, 3, payload)
        self.assertEqual(payload["operation_status"], "partial", payload)
        self.assertEqual(payload["knowledge_status"], "partial", payload)
        # Defeito C: `next_actions` NUNCA vazio quando `partial`.
        self.assertTrue(payload["next_actions"], "next_actions não pode ficar vazio em partial")
        na = payload["next_actions"][0]
        self.assertTrue(na.get("argv"), na)
        self.assertEqual(na["argv"][:2], ["wk", "resume"])
        self.assertIn("--store", na["argv"])
        self.assertIn("--repo", na["argv"])

    def test_b_reingestao_identica_preserva_partial_nunca_complete(self):
        code1, out1, err1 = self._ingest()
        self.assertEqual(err1, "", err1)
        payload1 = json.loads(out1)
        self.assertEqual(payload1["knowledge_status"], "partial")

        code2, out2, err2 = self._ingest()
        self.assertEqual(err2, "", err2)
        payload2 = json.loads(out2)
        # Defeito B: reingestão IDÊNTICA (no-op) não pode reportar
        # `knowledge_status=complete` só porque nada mudou NESTA invocação —
        # preserva o estado agregado do escopo (`partial`).
        self.assertEqual(payload2["knowledge_status"], "partial", payload2)
        detail2 = payload2["summary"]["detail"]
        self.assertEqual(detail2["revisoes"], [], "reingestão idêntica não grava revisão nova")
        self.assertIn("escopo_repo", detail2)
        self.assertEqual(detail2["escopo_repo"]["knowledge_status"], "partial")
        # `delivery_status` vem da ELEGIBILIDADE REAL (mesma função de `wk
        # status --repo`/`delivery prepare`), não de "publicou nesta
        # invocação?" — comparado à mesma leitura direta.
        self.assertEqual(
            payload2["delivery_status"],
            cli._status_delivery_status(os.path.abspath(self.store), self.repo_abs),
        )


class IngestNextActionsUnitTests(unittest.TestCase):
    """`_ingest_next_actions` isolada — cada ramo de causa material."""

    def test_completo_sem_next_action(self):
        self.assertEqual(
            cli._ingest_next_actions(
                store_root="S", repo_abs="R", status="completo", publicacoes=None,
                decisoes_pendentes=0, fontes_incompletas=0, bloqueios_execucao=0,
                escopo={"knowledge_status": None, "stop_reason": None, "agent_id": None},
                argv_ingest_recuperacao=["wk", "ingest", "f", "--store", "S"],
            ),
            [],
        )

    def test_executor_indisponivel_sugere_agent_connect(self):
        acoes = cli._ingest_next_actions(
            store_root="S", repo_abs="R", status="parcial", publicacoes=None,
            decisoes_pendentes=0, fontes_incompletas=0, bloqueios_execucao=0,
            escopo={"knowledge_status": "partial", "stop_reason": "executor_unavailable", "agent_id": "claude-code"},
            argv_ingest_recuperacao=["wk", "ingest", "f", "--store", "S"],
        )
        self.assertTrue(acoes)
        self.assertEqual(acoes[0]["argv"][:3], ["wk", "agent", "connect"])
        self.assertIn("claude-code", acoes[0]["argv"])

    def test_publicacao_bloqueada_sugere_retentar_ingest(self):
        acoes = cli._ingest_next_actions(
            store_root="S", repo_abs="R", status="parcial",
            publicacoes={"bloqueios": ["falha X"]},
            decisoes_pendentes=0, fontes_incompletas=0, bloqueios_execucao=0,
            escopo={"knowledge_status": None, "stop_reason": None, "agent_id": None},
            argv_ingest_recuperacao=["wk", "ingest", "f", "--store", "S"],
        )
        self.assertEqual(acoes[0]["argv"], ["wk", "ingest", "f", "--store", "S"])

    def test_escopo_partial_sugere_resume(self):
        acoes = cli._ingest_next_actions(
            store_root="S", repo_abs="R", status="parcial", publicacoes=None,
            decisoes_pendentes=0, fontes_incompletas=0, bloqueios_execucao=0,
            escopo={"knowledge_status": "partial", "stop_reason": None, "agent_id": None},
            argv_ingest_recuperacao=["wk", "ingest", "f", "--store", "S"],
        )
        self.assertEqual(acoes[0]["argv"], ["wk", "resume", "--store", "S", "--repo", "R"])

    def test_pendencia_de_correlacao_sugere_status(self):
        acoes = cli._ingest_next_actions(
            store_root="S", repo_abs="R", status="parcial", publicacoes=None,
            decisoes_pendentes=1, fontes_incompletas=0, bloqueios_execucao=0,
            escopo={"knowledge_status": None, "stop_reason": None, "agent_id": None},
            argv_ingest_recuperacao=["wk", "ingest", "f", "--store", "S"],
        )
        self.assertEqual(acoes[0]["argv"], ["wk", "status", "--store", "S", "--repo", "R"])


# ---------------------------------------------------------------------------
# D — --mode deep com executor sem `deepening` (local): agent connect
#     claude-code, nunca claude-cli
# ---------------------------------------------------------------------------


class NoDeepeningNextActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir("wk_t15d_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_local_sem_deepening_gera_agent_connect_claude_code_sem_claude_cli(self):
        ctx = _setup_partial_sem_deepening(self.tmp, oid="obj-1", cap_id="cap-1")
        self.addCleanup(ctx["store"].close)
        resolver = cli._snapshot_resolver(ctx["snapshot"])

        resultado = cli._run_investigation_chain(
            ctx["store"], store_root=ctx["store_root"], namespace=ctx["namespace"],
            snapshot=ctx["snapshot"], extraction=ctx["extraction"],
            objectives=ctx["objectives"], capability_map=ctx["capability_map"],
            objectives_by_id=ctx["objectives_by_id"],
            inputs_by_objective=ctx["inputs_by_objective"],
            engine_name="local", bindings_store=None, resolver=resolver,
            reason="teste T15/D", max_rounds=1,
        )
        chain = resultado["chain"]
        # `local` nunca resolve `reading_needs` (worker estrutural) — a
        # cadeia para por FALTA DE CAPACIDADE de aprofundamento, não por
        # orçamento/ambiguidade.
        self.assertEqual(chain["stop_reason"], "no_progress", chain)
        self.assertIn("engine_capabilities", chain)
        self.assertFalse(chain["engine_capabilities"].get("deepening"), chain["engine_capabilities"])

        for command in ("analyze", "update", "resume"):
            with self.subTest(command=command):
                operation_status, knowledge_status, pending, next_actions = cli._chain_envelope(
                    command=command, store_root=ctx["store_root"], repo_abs=ctx["repo_dir"],
                    chain=chain, agent_id="local",
                )
                self.assertEqual(operation_status, "partial")
                self.assertTrue(next_actions)
                na = next_actions[0]
                self.assertEqual(na["argv"][:3], ["wk", "agent", "connect"])
                self.assertIn("--agent", na["argv"])
                self.assertEqual(na["argv"][na["argv"].index("--agent") + 1], "claude-code")
                blob = json.dumps(next_actions) + json.dumps(pending)
                self.assertNotIn("claude-cli", blob, blob)
                self.assertIn("aprofundamento", na["motivo"])


# ---------------------------------------------------------------------------
# Auditoria #1 — lock entre threads/processos sobre efeitos pendentes
# ---------------------------------------------------------------------------


class PendingEffectsLockTests(unittest.TestCase):
    def setUp(self):
        self.store = _tmpdir("wk_t15_lock_store_")
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)

    def test_escritas_concorrentes_nenhum_efeito_perdido(self):
        n = 20
        erros: list[Exception] = []

        def _grava(i: int) -> None:
            try:
                cli._record_pending_publish_effect(
                    self.store, namespace="ns-x", revision_id=f"rev-{i}",
                    motivo=f"falha {i}", comando="ingest",
                    argv_recuperacao=["wk", "ingest", "f", "--store", self.store],
                )
            except Exception as exc:  # pragma: no cover - reportado abaixo
                erros.append(exc)

        threads = [threading.Thread(target=_grava, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertFalse(erros, erros)
        data = cli._load_pending_publish_effects(self.store)
        revisoes = {e["revision_id"] for e in data["efeitos"]}
        self.assertEqual(len(data["efeitos"]), n, data["efeitos"])
        self.assertEqual(revisoes, {f"rev-{i}" for i in range(n)})

    def test_lock_e_reentrante_entre_chamadas_sequenciais(self):
        # `_record_pending_publish_effect` seguido de `_resolve_pending_publish_effects_for`
        # na mesma thread — cada `with _PendingEffectsTransaction(...)` precisa
        # liberar o lock ao sair, senão a 2ª chamada trava.
        cli._record_pending_publish_effect(
            self.store, namespace="ns-y", revision_id="rev-1", motivo="falha",
            comando="ingest", argv_recuperacao=["wk", "ingest", "f", "--store", self.store],
        )
        resolvidos = cli._resolve_pending_publish_effects_for(
            self.store, namespace="ns-y", revision_id="rev-1", detail="ok",
        )
        self.assertEqual(len(resolvidos), 1)


# ---------------------------------------------------------------------------
# Auditoria #2 — falha de persistência do registro auxiliar vira `blocked`
# ---------------------------------------------------------------------------


class PublishPersistenceFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir("wk_t15_persist_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.src = os.path.join(self.tmp, "doc.md")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("# Documento\n\nConteudo qualquer.\n")

    def test_oserror_de_write_atomic_vira_blocked_exit2_sem_traceback(self):
        # `_record_pending_publish_effect` (chamada quando `_publish_local`
        # falha) grava INCONDICIONALMENTE — força esse ramo (mesmo mecanismo
        # de `test_efeito_publicacao_pendente._falha_na_primeira_publicacao`)
        # para o `OSError` de `_save_pending_publish_effects` (`_write_atomic`)
        # ser efetivamente exercitado, em vez do ramo de resolução (que só
        # grava quando HÁ efeito pendente pré-existente para concluir).
        with mock.patch("publishing.release.publish_revision", side_effect=RuntimeError("falha de publicacao (teste)")):
            with mock.patch.object(cli, "_save_pending_publish_effects", side_effect=OSError("disco cheio")):
                code, out, err = _run(["ingest", self.src, "--store", self.store, "--json"])
        # Nunca escapa como traceback/exit genérico (achado MÉDIA: "fora do
        # contrato 0/2/3") — sempre um envelope comum válido.
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["operation_status"], "blocked", payload)
        falhas = [p for p in payload["pending"] if p["tipo"] == "falha_interna"]
        self.assertTrue(falhas, payload["pending"])
        self.assertIn("disco cheio", falhas[0]["causa"])
        publicacoes = payload["summary"]["detail"].get("publicacoes") or {}
        self.assertIn("disco cheio", publicacoes.get("efeito_pendente_falha_persistencia", ""))

    def test_timeout_do_lock_tambem_vira_blocked_sem_escapar(self):
        with mock.patch.object(
            cli, "_PendingEffectsTransaction",
            side_effect=TimeoutError("lock ocupado (teste)"),
        ):
            code, out, err = _run(["ingest", self.src, "--store", self.store, "--json"])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["operation_status"], "blocked", payload)


# ---------------------------------------------------------------------------
# Auditoria #5 — TransportUnavailable tem prioridade sobre agent
#                connect/setup genérico (doctor/agent connect/despacho)
# ---------------------------------------------------------------------------


class TransportUnavailableNextActionsUnitTests(unittest.TestCase):
    """`_transport_unavailable_next_actions` isolada — sem depender de
    handshake real, prova a conversão setup_steps -> next_actions."""

    def _exc(self) -> A.TransportUnavailable:
        passos = (
            A.SetupStep(
                step_id="login", description="autenticar o provedor",
                argv=("wk", "doctor", "--probe-agent"),
            ),
            A.SetupStep(
                step_id="manual", description="revisar credenciais",
                manual_action="abra o painel do provedor e gere um token novo",
                verify_argv=("wk", "doctor",),
            ),
        )
        return A.TransportUnavailable(
            "agente 'x': sem transporte disponível", agent_id="x", transport="auto",
            options=(), setup_steps=passos,
        )

    def test_converte_setup_steps_em_next_actions_do_modulo(self):
        acoes = cli._transport_unavailable_next_actions(self._exc())
        self.assertEqual(len(acoes), 2)
        self.assertEqual(acoes[0]["argv"], ["wk", "doctor", "--probe-agent"])
        self.assertIsNotNone(acoes[0]["shell"])
        # 2º passo é manual (`manual_action`+`verify_argv`, sem `argv`
        # próprio) — `SetupStep.__post_init__` exige um dos dois; `TransportUnavailable.
        # next_actions()` usa `verify_argv` como o argv EXECUTÁVEL de
        # verificação quando não há `argv` de ação (§10.4.6): o operador
        # ainda tem o que RODAR para confirmar o passo manual, mesmo que a
        # ação em si não seja automatizável.
        self.assertEqual(acoes[1]["argv"], ["wk", "doctor"])
        self.assertEqual(acoes[1]["acao_externa"], "abra o painel do provedor e gere um token novo")


class TransportUnavailableWiringTests(_ExtensionEnvMixin, unittest.TestCase):
    def test_despacho_connect_chain_binding_usa_setup_steps_reais(self):
        self._use_extension("make_failing_fake_cli")
        registry, adapter, binding, bloqueios = cli._connect_chain_binding("fake-cli", None)
        self.assertIsNone(binding)
        self.assertTrue(bloqueios)
        alvo = bloqueios[0]
        self.assertIn("next_actions", alvo)
        self.assertEqual(alvo["next_actions"][0]["argv"], ["wk", "doctor", "--probe-agent"])
        self.assertIn("detalhe_estruturado", alvo)

    def test_doctor_probe_agent_reconexao_falha_usa_setup_steps_reais(self):
        store = _tmpdir("wk_t15_5_doctor_store_")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)

        # 1) conecta com sucesso e persiste o binding (estado inicial: "já
        #    esteve conectado" — o mesmo cenário real de `doctor
        #    --probe-agent` reconectando um binding persistido).
        self._use_extension("make_ok_fake_cli")
        reg_ok = A.default_registry()
        binding = reg_ok.connect("fake-cli", "auto")
        BindingStore(store).save_binding(binding, repo=None)
        reg_ok.close(binding)

        # 2) troca a extensão para a variante que FALHA — a reconexão que a
        #    sonda faz (mesmo `agent_id`) agora esbarra em `TransportUnavailable`.
        self._use_extension("make_failing_fake_cli")
        code, out, err = _run(["doctor", "--store", store, "--probe-agent", "--json"])
        payload = json.loads(out)
        detail = payload["summary"]["detail"]
        self.assertIn("agent_probe", detail["bloqueios"])
        sonda = detail["sonda_agente"]
        self.assertIn("proximos_passos_setup", sonda)
        self.assertEqual(sonda["proximos_passos_setup"][0]["argv"], ["wk", "doctor", "--probe-agent"])
        # `next_action` de TOPO também usa o argv real (não o texto manual
        # genérico "conecte um agente e repita...").
        na = payload["next_actions"][0]
        self.assertEqual(na["argv"], ["wk", "doctor", "--probe-agent"])


# ---------------------------------------------------------------------------
# Auditoria #7 — update sem delta retenta publicação pendente
# ---------------------------------------------------------------------------


class UpdateSemDeltaRetentaPublicacaoPendenteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir("wk_t15_7_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo, exist_ok=True)

        code, out, err = _run([
            "analyze", "--repo", self.repo, "--store", self.store, "--mode", "structural", "--json",
        ])
        self.assertEqual(err, "", err)
        self.assertNotEqual(json.loads(out)["operation_status"], "blocked")

        self.repo_abs = os.path.abspath(self.repo)
        self.namespace = f"code/{cli._repo_key(self.repo_abs)}"

    def test_com_efeito_pendente_retenta_publicacao_no_noop(self):
        cli._record_pending_publish_effect(
            self.store, namespace=self.namespace, revision_id="rev-pendente",
            motivo="falha simulada de publicação anterior", comando="analyze",
            argv_recuperacao=["wk", "analyze", "--repo", self.repo_abs, "--store", self.store],
        )
        publicacoes_fake = {
            "revisao": "rev-pendente", "namespaces_publicados": [self.namespace],
            "efeito_pendente_concluido": ["eff-fake"],
        }
        with mock.patch.object(
            cli, "_publish_local_and_track_effect", return_value=dict(publicacoes_fake),
        ) as m:
            code, out, err = _run([
                "update", "--repo", self.repo, "--store", self.store, "--mode", "structural", "--json",
            ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "noop", payload)
        self.assertEqual(payload["knowledge_status"], "not_applicable", payload)
        # T15/auditoria#7: a retentativa FOI acionada (não saiu cedo sem
        # olhar o efeito pendente) — com o `revision_id` do efeito e o
        # `comando`/`argv_recuperacao` corretos para `update`.
        m.assert_called_once()
        _args, kwargs = m.call_args
        self.assertEqual(_args[0], os.path.abspath(self.store))
        self.assertEqual(_args[1], self.namespace)
        self.assertEqual(_args[2], "rev-pendente")
        self.assertEqual(kwargs["comando"], "update")
        self.assertEqual(payload["summary"]["detail"]["publicacoes"], publicacoes_fake)

    def test_sem_efeito_pendente_nao_publica_no_noop(self):
        with mock.patch.object(cli, "_publish_local_and_track_effect") as m:
            code, out, err = _run([
                "update", "--repo", self.repo, "--store", self.store, "--mode", "structural", "--json",
            ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "noop", payload)
        m.assert_not_called()

    def test_efeito_pendente_ainda_bloqueado_apos_retentativa_vira_blocked(self):
        cli._record_pending_publish_effect(
            self.store, namespace=self.namespace, revision_id="rev-pendente",
            motivo="falha simulada", comando="analyze",
            argv_recuperacao=["wk", "analyze", "--repo", self.repo_abs, "--store", self.store],
        )
        with mock.patch.object(
            cli, "_publish_local_and_track_effect",
            return_value={"revisao": "rev-pendente", "bloqueios": ["ainda falhando"]},
        ):
            code, out, err = _run([
                "update", "--repo", self.repo, "--store", self.store, "--mode", "structural", "--json",
            ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["operation_status"], "blocked", payload)
        self.assertTrue(payload["next_actions"], payload)


# ---------------------------------------------------------------------------
# T15/#1, #2, row3, row4 — segunda reprodução real (2026-09-08): update
# no-op preserva knowledge_status agregado; ingest duplicado sem efeito
# pendente é noop/exit0; doctor sem --engine nunca cita o caminho de
# compatibilidade; varredura recursiva do payload INTEIRO (não só
# next_actions/pending/proximo_passo) de analyze/update/resume/ingest/
# status/doctor sem --engine.
# ---------------------------------------------------------------------------


def _sweep_full_payload(payload: dict) -> list:
    """Toda string do payload inteiro (recursivo — não só next_actions/
    pending/proximo_passo) que contém `--engine`/`claude-cli` — §11: nenhuma
    string do envelope do fluxo padrão (sem --engine) cita o caminho de
    compatibilidade nem o nome legado."""
    banidos = ("--engine", "claude-cli")
    achados: list = []
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            for b in banidos:
                if b in item:
                    achados.append((b, item))
    return achados


class UpdateNoopPreservaKnowledgeStatusAgregadoTests(unittest.TestCase):
    """T15/#1 (2ª reprodução real): `update` sem mudança no repo preserva o
    estado AGREGADO do escopo (mesma leitura de `wk status --repo`) — nunca
    `not_applicable` fixo escondendo um escopo `partial` já existente."""

    def setUp(self):
        self.tmp = _tmpdir("wk_t15b2_1_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "app"), exist_ok=True)
        with open(os.path.join(self.repo, "app", "a.py"), "w", encoding="utf-8") as fh:
            fh.write("def f(a, b):\n    if a and b:\n        return 1\n    return 0\n")

        code_i, out_i, err_i = _run(["init", "--store", self.store, "--agent", "local", "--json"])
        self.assertEqual(code_i, 0, err_i)
        code_c, out_c, err_c = _run([
            "agent", "connect", "--store", self.store, "--agent", "local", "--json",
        ])
        self.assertEqual(code_c, 0, err_c)

        code_a, out_a, err_a = _run([
            "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "deep", "--max-rounds", "1", "--json",
        ])
        self.assertEqual(err_a, "", err_a)
        payload_a = json.loads(out_a)
        self.assertEqual(payload_a["operation_status"], "partial", payload_a)
        self.assertEqual(payload_a["knowledge_status"], "partial", payload_a)

    def test_update_sem_mudanca_e_noop_com_knowledge_status_partial_preservado(self):
        code, out, err = _run(["update", "--store", self.store, "--repo", self.repo, "--json"])
        self.assertEqual(err, "", err)
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["operation_status"], "noop", payload)
        # A mesma leitura de `wk status --repo` — não uma segunda implementação.
        self.assertEqual(
            payload["knowledge_status"],
            cli._ingest_repo_scope_status(self.store, os.path.abspath(self.repo))["knowledge_status"],
            payload,
        )
        self.assertEqual(payload["knowledge_status"], "partial", payload)
        self.assertEqual(
            payload["delivery_status"],
            cli._status_delivery_status(os.path.abspath(self.store), os.path.abspath(self.repo)),
            payload,
        )
        # `pending`/`next_actions` não ficam vazios só porque a captura em
        # si não mudou — o escopo `partial` continua sendo trabalho real.
        self.assertTrue(payload["pending"], payload)
        self.assertTrue(any(p["tipo"] == "conhecimento_parcial" for p in payload["pending"]), payload)
        self.assertTrue(payload["next_actions"], payload)
        na = payload["next_actions"][0]
        self.assertIn(na["argv"][:2], (["wk", "resume"], ["wk", "agent"]))
        self._sem_banidos(payload)

    def _sem_banidos(self, payload: dict) -> None:
        achados = _sweep_full_payload(payload)
        self.assertFalse(achados, achados)


class IngestDuplicadoSemEfeitoPendenteEhNoopTests(unittest.TestCase):
    """T15/#2 (2ª reprodução real): reingestão idêntica SEM nenhum efeito
    pendente desta invocação (nada de novo, nenhuma decisão/fonte
    incompleta/falha) é um NO-OP desta operação — nunca `operation_status=
    partial`/exit 3 por algo que esta invocação não tentou fazer nem deixou
    pendente. `knowledge_status` continua `partial` (estado agregado do
    escopo), e o aviso D8 de reingestão idêntica permanece."""

    def setUp(self):
        self.tmp = _tmpdir("wk_t15b2_2_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "app"), exist_ok=True)
        with open(os.path.join(self.repo, "app", "a.py"), "w", encoding="utf-8") as fh:
            fh.write("def f(a, b):\n    if a and b:\n        return 1\n    return 0\n")
        self.src = os.path.join(self.tmp, "fonte.md")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("# Fonte\n\nConteudo qualquer.\n")

        code_i, out_i, err_i = _run(["init", "--store", self.store, "--agent", "local", "--json"])
        self.assertEqual(code_i, 0, err_i)
        code_c, out_c, err_c = _run([
            "agent", "connect", "--store", self.store, "--agent", "local", "--json",
        ])
        self.assertEqual(code_c, 0, err_c)
        code_a, out_a, err_a = _run([
            "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "deep", "--max-rounds", "1", "--json",
        ])
        self.assertEqual(err_a, "", err_a)
        self.assertEqual(json.loads(out_a)["knowledge_status"], "partial")

    def test_reingestao_identica_e_noop_exit0_com_knowledge_status_partial(self):
        code1, out1, err1 = _run(["ingest", self.src, "--store", self.store, "--repo", self.repo, "--json"])
        self.assertEqual(err1, "", err1)
        payload1 = json.loads(out1)
        self.assertEqual(payload1["operation_status"], "partial", payload1)

        code2, out2, err2 = _run(["ingest", self.src, "--store", self.store, "--repo", self.repo, "--json"])
        self.assertEqual(err2, "", err2)
        payload2 = json.loads(out2)
        # Defeito (2): reingestão idêntica sem efeito pendente é NOOP/exit 0,
        # não `partial`/exit 3.
        self.assertEqual(code2, 0, payload2)
        self.assertEqual(payload2["operation_status"], "noop", payload2)
        self.assertEqual(payload2["knowledge_status"], "partial", payload2)
        # Aviso D8 (reingestão idêntica) continua presente.
        avisos = payload2["summary"]["detail"]["avisos"]
        self.assertTrue(any("reingestao identica" in a for a in avisos), avisos)
        self.assertEqual(payload2["summary"]["detail"]["revisoes"], [], payload2)
        achados = _sweep_full_payload(payload2)
        self.assertFalse(achados, achados)


class DoctorSemEngineNuncaCitaCompatibilidadeTests(unittest.TestCase):
    """T15/row3 (2ª reprodução real): sem `--engine` explícito (fluxo
    padrão), `summary.detail.engine.config_permissoes[].acao`,
    `acao_permissao` e `comando_wk_flow.acao` (texto "rode wk init
    --engine ...") NUNCA são emitidos — só aparecem quando o operador usou
    `--engine`."""

    def setUp(self):
        self.tmp = _tmpdir("wk_t15b2_3_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo")
        self.base = os.path.join(self.tmp, "base")
        os.makedirs(self.repo, exist_ok=True)
        os.makedirs(self.base, exist_ok=True)

        code_i, out_i, err_i = _run(["init", "--store", self.store, "--agent", "local", "--json"])
        self.assertEqual(code_i, 0, err_i)
        code_c, out_c, err_c = _run([
            "agent", "connect", "--store", self.store, "--agent", "local", "--json",
        ])
        self.assertEqual(code_c, 0, err_c)

    def test_doctor_sem_engine_nao_emite_acao_de_compatibilidade(self):
        code, out, err = _run([
            "doctor", "--store", self.store, "--repo", self.repo, "--base", self.base, "--json",
        ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        engine_detail = payload["summary"]["detail"]["engine"]
        for entry in engine_detail.get("config_permissoes") or []:
            if isinstance(entry, dict):
                self.assertNotIn("acao", entry, entry)
        self.assertNotIn("acao_permissao", engine_detail, engine_detail)
        wk_flow = engine_detail.get("comando_wk_flow")
        if isinstance(wk_flow, dict):
            self.assertNotIn("acao", wk_flow, wk_flow)
        achados = _sweep_full_payload(payload)
        self.assertFalse(achados, achados)

    def test_doctor_com_engine_explicito_ainda_emite_acao_de_compatibilidade(self):
        # Contraprova: o caminho de COMPATIBILIDADE continua funcionando —
        # só deixou de vazar para quem NÃO pediu `--engine`. Settings.json
        # PRESENTE mas incompleto (deny vazio) — não "ausente" (que não gera
        # `acao` nenhuma) — é o estado real que dispara `MIGRACAO_ACAO`.
        settings_path = os.path.join(self.base, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as fh:
            json.dump({"permissions": {}}, fh)

        code, out, err = _run([
            "doctor", "--store", self.store, "--repo", self.repo, "--base", self.base,
            "--engine", "claude-code", "--json",
        ])
        self.assertEqual(err, "", err)
        payload = json.loads(out)
        engine_detail = payload["summary"]["detail"]["engine"]
        entries = engine_detail.get("config_permissoes") or []
        self.assertTrue(entries, engine_detail)
        self.assertIn("acao", entries[0], entries[0])
        self.assertIn("acao_permissao", engine_detail, engine_detail)


class VarreduraCompletaEnvelopeSemEngineTests(unittest.TestCase):
    """Varredura recursiva do payload INTEIRO (não só next_actions/pending/
    proximo_passo) de analyze/update/resume/ingest/status/doctor no fluxo
    padrão (reprodução real: store temporário, `init --agent local` +
    `agent connect --agent local`, `analyze --mode deep --max-rounds 1`
    ⇒ `partial`) — nenhuma string do envelope cita `--engine`/`claude-cli`."""

    def setUp(self):
        self.tmp = _tmpdir("wk_t15b2_4_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "app"), exist_ok=True)
        with open(os.path.join(self.repo, "app", "a.py"), "w", encoding="utf-8") as fh:
            fh.write("def f(a, b):\n    if a and b:\n        return 1\n    return 0\n")

        code_i, out_i, err_i = _run(["init", "--store", self.store, "--agent", "local", "--json"])
        self.assertEqual(code_i, 0, err_i)
        code_c, out_c, err_c = _run([
            "agent", "connect", "--store", self.store, "--agent", "local", "--json",
        ])
        self.assertEqual(code_c, 0, err_c)

        code_a, out_a, err_a = _run([
            "analyze", "--repo", self.repo, "--store", self.store,
            "--mode", "deep", "--max-rounds", "1", "--json",
        ])
        self.assertEqual(err_a, "", err_a)
        payload_a = json.loads(out_a)
        self.assertEqual(payload_a["operation_status"], "partial", payload_a)
        self.assertEqual(payload_a["knowledge_status"], "partial", payload_a)
        self._assert_sem_banidos(payload_a)

    def _assert_sem_banidos(self, payload: dict) -> None:
        achados = _sweep_full_payload(payload)
        self.assertFalse(achados, achados)

    def test_update_sem_banidos(self):
        code, out, err = _run(["update", "--repo", self.repo, "--store", self.store, "--json"])
        self.assertEqual(err, "", err)
        self._assert_sem_banidos(json.loads(out))

    def test_resume_sem_banidos(self):
        code, out, err = _run(["resume", "--repo", self.repo, "--store", self.store, "--json"])
        self.assertEqual(err, "", err)
        self._assert_sem_banidos(json.loads(out))

    def test_ingest_duplicado_sem_banidos(self):
        src = os.path.join(self.tmp, "fonte.md")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("# Fonte\n\nConteudo qualquer.\n")
        code1, out1, err1 = _run(["ingest", src, "--store", self.store, "--repo", self.repo, "--json"])
        self.assertEqual(err1, "", err1)
        self._assert_sem_banidos(json.loads(out1))
        code2, out2, err2 = _run(["ingest", src, "--store", self.store, "--repo", self.repo, "--json"])
        self.assertEqual(err2, "", err2)
        payload2 = json.loads(out2)
        self.assertEqual(code2, 0, payload2)
        self.assertEqual(payload2["operation_status"], "noop", payload2)
        self._assert_sem_banidos(payload2)

    def test_status_sem_banidos(self):
        code, out, err = _run(["status", "--repo", self.repo, "--store", self.store, "--json"])
        self.assertEqual(err, "", err)
        self._assert_sem_banidos(json.loads(out))

    def test_doctor_sem_banidos(self):
        code, out, err = _run(["doctor", "--store", self.store, "--repo", self.repo, "--json"])
        self.assertEqual(err, "", err)
        self._assert_sem_banidos(json.loads(out))


if __name__ == "__main__":
    unittest.main()
