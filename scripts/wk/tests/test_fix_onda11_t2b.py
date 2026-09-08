"""Onda11-T2b — fiação do achado BLOQUEANTE #2 (3ª auditoria externa) em
`wk/cli.py`: continuações com contexto completo + engine sem leitura não
consome rodada.

Infra já pronta (Onda11-T2a, NÃO editada aqui — dona é `runtime`):
`runtime.tasks.create_continuation_tasks` (needs com evidence/contract_state/
parent_result/capability_context), `runtime.context.build_package` (partes
reais a partir de `needs[].evidence`), `runtime.coordinator.plan_continuations
(engine_capabilities=...)` (recusa sem consumir rodada quando `deepening` é
`False`), `LocalThreadExecutor`/`ClaudeCliExecutor` declarando
`capabilities()["deepening"]` — ver `scripts/runtime/tests/test_onda11_t2a.py`.

ATUALIZAÇÃO (limpeza de código morto, posterior à Onda11-T2b): o mecanismo de
UMA rodada por invocação (`_plan_continuation_round`/`_continuation_cycle`/
`_proximo_passo_continuacao`/`_dispatch_disponivel`/`_EMPTY_CONTINUACAO`) foi
REMOVIDO de `cli.py` — sem chamador de produção desde que o laço inteiro de
continuação passou a rodar em `_run_investigation_chain` (§7.3, UMA chamada
por invocação, até conclusão ou motivo material de parada). `_unmet_needs_of`/
`_contract_state_of` também foram removidos: o primeiro porque
`knowledge.integrate` já publica `reading_needs` nativamente (com `evidence`
— `ReadingNeed.to_dict()` sempre incluiu o campo, então a causa original do
achado #1 abaixo não pode mais acontecer por construção); o segundo porque
`contract_state` também é campo nativo do relatório de `knowledge.integrate`
(`_contract_state_dict`). Uma fatia ficou de fato só neste arquivo — releitura
pedida pela VERIFICAÇÃO (`knowledge.integrate.reread_obligations`, que NÃO é
nativa do outcome) — e sobreviveu como `cli._reread_needs_of`, testada na
seção 1 abaixo contra o alvo novo.

O que ESTE arquivo cobre agora, teste a teste, contra os alvos que sobrevivem:

1. `_reread_needs_of` preserva `evidence` das obrigações de releitura da
   verificação (mesma garantia do antigo `_unmet_needs_of` para esta fatia —
   a fatia de `ReadingNeed` comum não precisa mais de teste aqui: é
   `ReadingNeed.to_dict()`, testado em `analysis/tests`, quem garante);
2. `_capability_context_of`/`_engine_capabilities`/`_build_executor`
   continuam vivos (sem chamador de produção os dois últimos — ver
   comentário em `cli.py` — mas ainda testados isoladamente);
3. `_run_investigation_chain` com engine SEM `deepening` (`local`): nenhuma
   continuação é criada, nenhuma rodada é consumida, repetir não muda nada
   — MESMA garantia do antigo `test_plan_continuation_round_com_engine_
   sem_deepening_nao_cria_nem_avanca_rodada`, contra o alvo novo;
4. `_run_investigation_chain` com engine COM `deepening`: a continuação É
   criada, e o `objective["capability_context"]` da tarefa filha carrega
   entry_keys/símbolos/módulos — a garantia que motivou o achado desta onda,
   e que a limpeza de código morto quase perdeu (`_capability_context_of`
   ficou sem NENHUM chamador de produção até ser religado em
   `_run_investigation_chain._outcomes_fn_for`); o pacote (`context.
   build_package`) montado sobre essa tarefa carrega código real — MESMA
   garantia do antigo `test_needs_de_unmet_needs_of_produzem_partes_reais_
   no_pacote`/`test_plan_continuation_round_com_engine_deepening_cria_e_
   pacote_tem_codigo`, contra o alvo novo.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from wk import cli
from wk.tests.test_run_chain_cli import (
    CODIGO_A,
    _Cap,
    _CapMap,
    _extraction_of,
    _RECORDS,
    _reset_worker,
    make_scripted_worker,
)

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _objetivo_com_need_evidenciada():
    from analysis.capabilities import EvidenceRef
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

    return InvestigationObjective(
        objective_id="obj_1",
        kind=ObjectiveKind.CAPABILITY,
        capability_id="cap1",
        name="capacidade 1",
        entry_keys=("entry1",),
        symbols=("mod.f",),
        contract={n: ContractField(name=n, label=CONTRACT_LABELS[n]) for n in CONTRACT_FIELDS},
        evidence_refs=[EvidenceRef(path="app/a.py", line_start=1, line_end=3, role="entrypoint")],
        reading_needs=[
            ReadingNeed(
                need_id="n1",
                kind=ReadingKind.SYMBOL,
                target="app/a.py::f",
                motivo="símbolo alcançado pela entrada",
                trigger=ReadingTrigger.UNRESOLVED_CALL,
                evidence=EvidenceRef(path="app/a.py", line_start=10, line_end=20),
            ),
        ],
    )


def _tmp_store(objective_dict: dict, inputs: dict):
    from runtime import tasks as rt_tasks

    tmpdir = tempfile.mkdtemp(prefix="onda11t2b-")
    store = rt_tasks.TaskStore.open(os.path.join(tmpdir, "runtime.db"))
    task = store.create_task(rt_tasks.TaskKind.INVESTIGATION, objective_dict, inputs)
    return tmpdir, store, task


def _fake_resolver(path, start, end):
    return {
        "snippet": f"def f():\n    return {start}\n",
        "locator": {"path": path, "commit": "deadbeef"},
    }


def _setup_chain(tmp_root: str, objetivo):
    """Mesma fiação de `wk.tests.test_run_chain_cli._setup`, mas com um
    objetivo/capacidade escolhidos pelo chamador (aqui, `_objetivo_com_need_
    evidenciada()` — entry_keys/símbolos/evidence_refs preenchidos, para que
    `capability_context` tenha o que carregar de verdade)."""
    from analysis.snapshot import capture
    from runtime import tasks as rt_tasks
    from runtime.coordinator import plan_from_objectives

    repo_dir = os.path.join(tmp_root, "repo")
    os.makedirs(os.path.join(repo_dir, "app"), exist_ok=True)
    with open(os.path.join(repo_dir, "app", "a.py"), "w", encoding="utf-8") as fh:
        fh.write(CODIGO_A)

    snapshot = capture(repo_dir)
    extraction = _extraction_of(snapshot)
    capability_map = _CapMap([_Cap("cap1", ["app/a.py"])])
    oid = objetivo.objective_id
    objectives = [objetivo]
    objectives_by_id = {oid: objetivo}
    objective_dicts = [objetivo.to_dict()]
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


#: Round 0 sem fechar `n1` (nenhum `reading_satisfied`) — o objetivo continua
#: `partial` com uma necessidade de leitura ABERTA e com alvo concreto, o
#: gatilho mínimo para `coordinator.plan_continuations` tentar criar uma
#: continuação (e para a checagem de `deepening` decidir se cria ou recusa).
_RESULTADO_PARCIAL_SEM_FECHAR_N1 = {
    "objective_id": "obj_1", "capability_id": "cap1",
    "contract": {"decisoes": {"status": "filled", "content": "primeira apuração"}},
    "reading_satisfied": [],
}


class _NoDeepeningExec:
    """Executor roteirizado que sempre devolve `_RESULTADO_PARCIAL_SEM_FECHAR_N1`
    — usado pela seção 3 (engine SEM `deepening`) para forçar o objetivo a
    `partial` (nunca `blocked`) sem depender do conteúdo que o worker `local`
    de verdade preencheria."""

    def capabilities(self) -> dict:
        return {"dispatch": True, "concurrency": 1, "cancellation": "process-kill",
                "structured_output": True, "deepening": False, "tools": []}

    def submit(self, task_id, objective, references=None, schema=None, policy=None) -> str:
        execution_id = f"nodeepen:{task_id}"
        _RECORDS[execution_id] = dict(_RESULTADO_PARCIAL_SEM_FECHAR_N1)
        return execution_id

    def status(self, execution_id) -> dict:
        return {"state": "done"}

    def result(self, execution_id) -> dict:
        return {"execution_id": execution_id, "output": _RECORDS.get(execution_id, {})}

    def cancel(self, execution_id) -> bool:
        return True


def _no_deepening_adapter_cls():
    from runtime import agents as A
    from runtime.executors.agent_adapters import BaseExecutorAdapter

    class _NoDeepeningAdapter(BaseExecutorAdapter):
        agent_id = "no-deepening-chain"
        adapter_version = "no-deepening-chain/1"

        def describe(self) -> A.AgentDescriptor:
            return A.AgentDescriptor(
                agent_id=self.agent_id, adapter_version=self.adapter_version,
                transports=("process",),
                capabilities=A.AgentCapabilities(dispatch=True, deepening=False, physical_cancellation=True),
                verified_versions=("fake/1",), distribution="extension",
                summary="worker roteirizado SEM deepening (teste onda11-t2b)",
            )

        def _handshake(self, transport: str, config: dict) -> dict:
            return {"connected": True, "detail": "ok", "host_version": "fake/1", "model": None}

        def _new_executor(self, config: dict):
            return _NoDeepeningExec()

    return _NoDeepeningAdapter


def make_no_deepening_worker():
    return _no_deepening_adapter_cls()()


# --------------------------------------------------------------------------
# 1) _reread_needs_of preserva evidence das obrigações de releitura da
#    verificação — a única fatia do antigo _unmet_needs_of que sobrevive
#    como função própria deste arquivo (o resto é nativo de `knowledge.
#    integrate`, ver docstring do módulo).
# --------------------------------------------------------------------------


def test_reread_needs_of_preserva_evidence_da_fonte_primaria():
    reread = [
        {
            "objective_id": "obj_1",
            "claim_id": "c1",
            "kind": "reread_primary_source",
            "triggers": ["veredito disputed: divergência entre claim e trecho"],
            "statement": "X sempre faz Y",
            "primary_source": {"path": "app/b.py", "start_line": 5, "end_line": 9},
        },
        {  # de OUTRO objetivo: não deve entrar na lista de obj_1
            "objective_id": "obj_2",
            "claim_id": "c2",
            "primary_source": {"path": "app/c.py", "start_line": 1, "end_line": 2},
        },
    ]
    needs = cli._reread_needs_of("obj_1", reread)
    assert len(needs) == 1
    assert needs[0]["need_id"] == "reread:c1"
    assert needs[0]["evidence"] == {"path": "app/b.py", "line_start": 5, "line_end": 9}
    assert "disputed" in needs[0]["motivo"]


def test_reread_needs_of_sem_fonte_primaria_e_ignorado():
    reread = [{"objective_id": "obj_1", "claim_id": "c1", "primary_source": None}]
    assert cli._reread_needs_of("obj_1", reread) == []


def test_reread_needs_of_sem_obrigacoes_e_vazio():
    assert cli._reread_needs_of("obj_1", []) == []
    assert cli._reread_needs_of("obj_1", None) == []


# --------------------------------------------------------------------------
# 2) funções auxiliares que sobrevivem sem chamador de produção (testadas
#    isoladamente — `_engine_capabilities`/`_build_executor` documentam a
#    ausência de chamador em `cli.py`, ver comentário acima de
#    `_engine_capabilities`)
# --------------------------------------------------------------------------


def test_capability_context_of_traz_entradas_simbolos_modulos():
    ctx = cli._capability_context_of(_objetivo_com_need_evidenciada())
    assert ctx["capability_id"] == "cap1"
    assert ctx["entry_keys"] == ["entry1"]
    assert ctx["symbols"] == ["mod.f"]
    assert ctx["modules"] == ["app/a.py"]


def test_engine_capabilities_local_e_sem_deepening():
    caps = cli._engine_capabilities("local")
    assert caps.get("dispatch") is True  # local sempre despacha...
    assert caps.get("deepening") is False  # ...mas nunca aprofunda leitura


def test_engine_capabilities_engine_desconhecida_nao_propaga_valueerror():
    assert cli._engine_capabilities("engine-inexistente") == {}


def test_build_executor_local_recusa_objetivo_de_continuacao_sem_kind():
    """A engine local não tem worker de continuação (`_local_continuation_worker`
    foi removido onda11-T2b) — um objetivo de continuação (sem `kind`,
    vocabulário do runtime) é recusado como qualquer objetivo cujo `kind` o
    `LocalThreadExecutor` não reconhece; nunca mais "responde" fingindo ter
    lido."""
    executor = cli._build_executor("local", {})
    try:
        objetivo_continuacao = {
            "objective_id": "obj_1", "continuation": True, "round": 1,
            "needs": [{"need_id": "n1", "target": "app/a.py::f", "motivo": "x"}],
            "parent_task_id": "task_mae",
        }
        try:
            executor.submit("task_filha", objetivo_continuacao, [], {}, {})
            raise AssertionError("esperava ValueError: objetivo de continuação sem 'kind'")
        except ValueError as exc:
            assert "kind" in str(exc)
    finally:
        executor.shutdown(wait=True)


def test_simbolos_de_continuacao_removidos_do_modulo():
    """Documenta a remoção (Onda11-T2b): reintroduzir estes símbolos reabriria
    o achado — a engine local voltaria a "executar" continuação sem ler."""
    assert not hasattr(cli, "_local_continuation_worker")
    assert not hasattr(cli, "_LocalEngineEnvelope")
    assert not hasattr(cli, "_CONTINUATION_KIND")


# --------------------------------------------------------------------------
# 3) engine sem deepening (`local`), via `_run_investigation_chain`: NENHUMA
#    continuação é criada, NENHUMA rodada é consumida, repetir não muda nada
# --------------------------------------------------------------------------


def test_run_investigation_chain_engine_sem_deepening_nao_cria_continuacao_nem_avanca_rodada(monkeypatch):
    """`local` de verdade não serve para este teste: sem conhecer os
    `reading_needs` desta fixture, o worker local não preenche NENHUM campo
    do contrato — `InvestigationObjective.evaluate()` cai em `BLOCKED` (não
    `PARTIAL`) já na rodada 0, e a cadeia para em `completed` (motivo real:
    "nenhum objetivo do escopo continua parcial" — nem `partial` conta como
    pendente para aquele check) ANTES de `plan_continuations` sequer ser
    chamado — o cenário que este teste precisa exercitar (recusa POR
    deepening) nunca é alcançado. `_NoDeepeningAdapter` (módulo, abaixo)
    reproduz o mesmo sinal que `claude-cli`/`local` dão
    (`engine_caps["deepening"]`) sem depender do conteúdo que o worker local
    de verdade preenche."""
    from runtime import agents as A

    monkeypatch.setenv(A.EXTENSIONS_ENV, f"{__name__}:make_no_deepening_worker")
    _RECORDS.clear()

    tmp = tempfile.mkdtemp(prefix="onda11t2b-nodeepen-")
    try:
        ctx = _setup_chain(tmp, _objetivo_com_need_evidenciada())
        try:
            resolver = cli._snapshot_resolver(ctx["snapshot"])
            total_antes = len(ctx["store"].all_tasks())

            resultado = cli._run_investigation_chain(
                ctx["store"], store_root=ctx["store_root"], namespace=ctx["namespace"],
                snapshot=ctx["snapshot"], extraction=ctx["extraction"],
                objectives=ctx["objectives"], capability_map=ctx["capability_map"],
                objectives_by_id=ctx["objectives_by_id"],
                inputs_by_objective=ctx["inputs_by_objective"],
                engine_name="no-deepening-chain", bindings_store=None, resolver=resolver,
                reason="teste sem deepening", max_rounds=3,
            )
            chain = resultado["chain"]
            assert chain["stop_reason"] == "no_progress", chain
            assert "deepening=False" in chain["detail"]
            # §11: nunca o nome legado `claude-cli` — o motivo aponta o
            # caminho novo (`wk agent list`) para conectar um agente com
            # capacidade de aprofundamento.
            assert "claude-cli" not in chain["detail"]
            assert "aprofundamento" in chain["detail"]
            assert "wk agent list" in chain["detail"]

            status = ctx["store"].chain_status(ctx["oid"])
            assert status["rounds_used"] == 0
            # só a tarefa-base: nenhuma continuação nasceu.
            assert len(ctx["store"].all_tasks()) == total_antes

            # repetir (equivalente a `wk resume` de novo com a mesma engine)
            # não cria nada a mais nem avança rodada.
            resultado2 = cli._run_investigation_chain(
                ctx["store"], store_root=ctx["store_root"], namespace=ctx["namespace"],
                snapshot=ctx["snapshot"], extraction=ctx["extraction"],
                objectives=ctx["objectives"], capability_map=ctx["capability_map"],
                objectives_by_id=ctx["objectives_by_id"],
                inputs_by_objective=ctx["inputs_by_objective"],
                engine_name="no-deepening-chain", bindings_store=None, resolver=resolver,
                reason="teste sem deepening — repetição", max_rounds=3,
            )
            assert resultado2["chain"]["rounds"] == 0
            assert len(ctx["store"].all_tasks()) == total_antes
            assert ctx["store"].chain_status(ctx["oid"])["rounds_used"] == 0
        finally:
            ctx["store"].close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4) engine COM deepening: continuação É criada, `capability_context` viaja
#    até a tarefa filha, e o pacote montado sobre ela tem código real
# --------------------------------------------------------------------------


def test_run_investigation_chain_engine_com_deepening_cria_continuacao_com_capability_context(monkeypatch):
    """A garantia central desta onda (e o que a limpeza de código morto quase
    perdeu, ver docstring do módulo): sem `_capability_context_of` religada
    em `_run_investigation_chain._outcomes_fn_for`, a tarefa filha nasceria
    com `capability_context={}` — perdendo entry_keys/símbolos/módulos
    silenciosamente."""
    from runtime import agents as A
    from runtime import context as rt_context

    tmp = tempfile.mkdtemp(prefix="onda11t2b-deepen-")
    monkeypatch.setenv(A.EXTENSIONS_ENV, f"{make_scripted_worker.__module__}:make_scripted_worker")
    _reset_worker([_RESULTADO_PARCIAL_SEM_FECHAR_N1])
    _RECORDS.clear()
    try:
        ctx = _setup_chain(tmp, _objetivo_com_need_evidenciada())
        try:
            resolver = cli._snapshot_resolver(ctx["snapshot"])
            resultado = cli._run_investigation_chain(
                ctx["store"], store_root=ctx["store_root"], namespace=ctx["namespace"],
                snapshot=ctx["snapshot"], extraction=ctx["extraction"],
                objectives=ctx["objectives"], capability_map=ctx["capability_map"],
                objectives_by_id=ctx["objectives_by_id"],
                inputs_by_objective=ctx["inputs_by_objective"],
                engine_name="scripted-chain", bindings_store=None, resolver=resolver,
                reason="teste com deepening", max_rounds=3,
            )
            assert resultado["chain"]["rounds"] >= 1, resultado["chain"]

            continuacoes = [
                t for t in ctx["store"].all_tasks() if t.objective.get("continuation")
            ]
            assert len(continuacoes) >= 1, "engine com deepening deveria ter criado continuação"
            cont_task = continuacoes[0]

            ctx_capacidade = cont_task.objective.get("capability_context") or {}
            assert ctx_capacidade.get("capability_id") == "cap1"
            assert ctx_capacidade.get("entry_keys") == ["entry1"]
            assert ctx_capacidade.get("symbols") == ["mod.f"]
            assert ctx_capacidade.get("modules") == ["app/a.py"]

            # o pacote montado sobre a tarefa filha carrega código REAL (0
            # `parts` é exatamente o achado que a Onda11-T2b eliminou).
            budget = rt_context.Budget(max_bytes=200_000, max_tokens=100_000)
            package = rt_context.build_package(cont_task.objective, budget, _fake_resolver)
            assert len(package.parts) >= 1
            assert "def f()" in package.parts[0].snippet
            envelope = package.refs[0]
            assert envelope["capability_context"]["capability_id"] == "cap1"
        finally:
            ctx["store"].close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
