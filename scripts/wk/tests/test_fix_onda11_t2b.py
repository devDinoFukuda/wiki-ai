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

O que ESTA onda fia em `cli.py` (único arquivo desta onda, dono exclusivo):

1. `_unmet_needs_of` preserva `evidence` de `ReadingNeed` e de
   `knowledge.integrate.reread_obligations` (antes: needs serializados SEM
   localizador, então `context.build_package` da continuação nunca achava
   trecho — pacote de 0 `parts`);
2. `_plan_continuation_round` repassa `engine_capabilities=executor.capabilities()`
   e `contract_state`/`capability_context` a `plan_continuations` (antes:
   nenhum dos dois viajava, então a engine `local` gastava rodada sem poder
   ler, e a continuação chegava sem o contrato/símbolos já estabelecidos);
3. `_local_continuation_worker`/`_LocalEngineEnvelope` (onda 10) foram
   REMOVIDOS — a engine `local` nunca mais executa continuação porque nunca
   mais recebe uma (recusada na CRIAÇÃO, não na execução).

Causas descritas na 3ª auditoria e cobertas aqui: `_unmet_needs_of` (linha
anterior à onda: cli.py:5743) serializava needs sem `evidence`;
`_plan_continuation_round` não passava `engine_capabilities` nem contexto
acumulado; `_local_continuation_worker` executava continuação sem ler
(queimava rodada).
"""

from __future__ import annotations

import os
import shutil
import tempfile

from wk import cli


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


# --------------------------------------------------------------------------
# 1) _unmet_needs_of preserva evidence — ReadingNeed e reread_obligations
# --------------------------------------------------------------------------


def test_unmet_needs_preserva_evidence_do_reading_need():
    needs = cli._unmet_needs_of(_objetivo_com_need_evidenciada(), {})
    assert len(needs) == 1
    ev = needs[0]["evidence"]
    assert ev is not None
    assert ev["path"] == "app/a.py"
    assert ev["line_start"] == 10
    assert ev["line_end"] == 20


def test_unmet_needs_sem_evidence_no_reading_need_vira_none_nao_quebra():
    """`ReadingNeed` sem `evidence` (formato anterior à onda) não quebra —
    vira `None`; `context.build_package` (Onda11-T2a) é quem grava o
    diagnóstico de "sem localizador", nunca este arquivo."""
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

    objetivo = InvestigationObjective(
        objective_id="obj_1", kind=ObjectiveKind.CAPABILITY, capability_id="cap1", name="c1",
        contract={n: ContractField(name=n, label=CONTRACT_LABELS[n]) for n in CONTRACT_FIELDS},
        reading_needs=[
            ReadingNeed(need_id="n1", kind=ReadingKind.SYMBOL, target="app/a.py::f",
                        motivo="m", trigger=ReadingTrigger.UNRESOLVED_CALL),
        ],
    )
    needs = cli._unmet_needs_of(objetivo, {})
    assert needs[0]["evidence"] is None


def test_unmet_needs_inclui_reread_obligations_do_proprio_objetivo():
    objetivo = _objetivo_com_need_evidenciada()
    for need in objetivo.reading_needs:
        need.waive("já coberto por outra leitura")  # só o reread deve sobrar

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
    needs = cli._unmet_needs_of(objetivo, {}, reread)
    assert len(needs) == 1
    assert needs[0]["need_id"] == "reread:c1"
    assert needs[0]["evidence"] == {"path": "app/b.py", "line_start": 5, "line_end": 9}
    assert "disputed" in needs[0]["motivo"]


def test_unmet_needs_reread_sem_fonte_primaria_e_ignorado():
    objetivo = _objetivo_com_need_evidenciada()
    for need in objetivo.reading_needs:
        need.waive("já coberto")
    reread = [{"objective_id": "obj_1", "claim_id": "c1", "primary_source": None}]
    assert cli._unmet_needs_of(objetivo, {}, reread) == []


# --------------------------------------------------------------------------
# needs de _unmet_needs_of produzem trecho REAL via create_continuation_tasks
# + context.build_package (ponta a ponta do achado — 0-parts eliminado)
# --------------------------------------------------------------------------


def _fake_resolver(path, start, end):
    return {
        "snippet": f"def f():\n    return {start}\n",
        "locator": {"path": path, "commit": "deadbeef"},
    }


def test_needs_de_unmet_needs_of_produzem_partes_reais_no_pacote():
    from runtime import context as rt_context
    from runtime import tasks as rt_tasks

    objetivo = _objetivo_com_need_evidenciada()
    needs = cli._unmet_needs_of(objetivo, {})
    assert needs and needs[0]["evidence"] is not None

    inputs = {"snapshot_id": "scope:x", "source_version_ids": ["app/a.py@sha"]}
    tmpdir, store, parent = _tmp_store(objetivo.to_dict(), inputs)
    try:
        task_ids = rt_tasks.create_continuation_tasks(
            store,
            parent_task_id=parent.task_id,
            objective_id="obj_1",
            needs=needs,
            input_versions=inputs,
            round_no=1,
            contract_state=cli._contract_state_of(objetivo),
            capability_context=cli._capability_context_of(objetivo),
        )
        assert len(task_ids) == 1
        cont_task = store.get(task_ids[0])

        budget = rt_context.Budget(max_bytes=200_000, max_tokens=100_000)
        package = rt_context.build_package(cont_task.objective, budget, _fake_resolver)
        assert len(package.parts) >= 1, "0 parts é exatamente o achado que esta onda elimina"
        assert "def f()" in package.parts[0].snippet
    finally:
        store.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------
# 2) contract_state / capability_context
# --------------------------------------------------------------------------


def test_contract_state_of_so_traz_campos_preenchidos():
    objetivo = _objetivo_com_need_evidenciada()
    objetivo.field("identidade").fill("X", [objetivo.evidence_refs[0]])
    state = cli._contract_state_of(objetivo)
    assert set(state.keys()) == {"identidade"}
    assert state["identidade"]["status"] == "filled"
    assert state["identidade"]["content"] == "X"


def test_contract_state_of_sem_campo_preenchido_e_vazio():
    assert cli._contract_state_of(_objetivo_com_need_evidenciada()) == {}


def test_capability_context_of_traz_entradas_simbolos_modulos():
    ctx = cli._capability_context_of(_objetivo_com_need_evidenciada())
    assert ctx["capability_id"] == "cap1"
    assert ctx["entry_keys"] == ["entry1"]
    assert ctx["symbols"] == ["mod.f"]
    assert ctx["modules"] == ["app/a.py"]


# --------------------------------------------------------------------------
# 3) engine sem deepening: NENHUMA continuação criada, rodada não avança
# --------------------------------------------------------------------------


def test_engine_capabilities_local_e_sem_deepening():
    caps = cli._engine_capabilities("local")
    assert caps.get("dispatch") is True  # local sempre despacha...
    assert caps.get("deepening") is False  # ...mas nunca aprofunda leitura


def test_engine_capabilities_engine_desconhecida_nao_propaga_valueerror():
    assert cli._engine_capabilities("engine-inexistente") == {}


def test_plan_continuation_round_com_engine_sem_deepening_nao_cria_nem_avanca_rodada():
    """Reprodução da auditoria: engine local + investigação parcial -> 0
    rodadas consumidas, recusa explícita com próximo passo; repetir 3x não
    muda o round."""
    from runtime import tasks as rt_tasks

    objetivo = _objetivo_com_need_evidenciada()
    inputs = {"snapshot_id": "scope:x", "source_version_ids": ["app/a.py@sha"]}
    tmpdir, store, task = _tmp_store(objetivo.to_dict(), inputs)
    try:
        report = {"objetivos": [{"objective_id": "obj_1", "task_id": task.task_id, "state": "partial"}]}
        objetivos_by_id = {"obj_1": objetivo}
        inputs_by_objective = {"obj_1": inputs}
        local_caps = cli._engine_capabilities("local")

        total_antes = len(store.all_tasks())
        plano = cli._plan_continuation_round(
            store, report, objetivos_by_id, inputs_by_objective,
            engine_capabilities=local_caps, engine_name="local",
        )
        assert plano["criadas"] == []
        assert plano["round"] == 0
        assert len(plano["recusadas"]) == 1
        assert plano["recusadas"][0]["objective_id"] == "obj_1"
        assert "deepening" in plano["recusadas"][0]["motivo"]
        assert plano["motivo"] and "claude-cli" in plano["motivo"]
        assert len(store.all_tasks()) == total_antes
        assert rt_tasks.continuation_rounds(store, "obj_1") == 0

        proximo = cli._proximo_passo_continuacao("/repo", plano, [])
        assert "claude-cli" in proximo

        # "3 invocações de resume não mudam round" — repetir não cria nada.
        for _ in range(3):
            outra = cli._plan_continuation_round(
                store, report, objetivos_by_id, inputs_by_objective,
                engine_capabilities=local_caps, engine_name="local",
            )
            assert outra["criadas"] == []
            assert rt_tasks.continuation_rounds(store, "obj_1") == 0
        assert len(store.all_tasks()) == total_antes
    finally:
        store.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_plan_continuation_round_sem_engine_capabilities_preserva_comportamento_antigo():
    """`engine_capabilities=None` (default) — compatibilidade com quem chama
    sem informar a engine (mesma regra de `coordinator.plan_continuations`)."""
    objetivo = _objetivo_com_need_evidenciada()
    inputs = {"snapshot_id": "scope:x", "source_version_ids": ["app/a.py@sha"]}
    tmpdir, store, task = _tmp_store(objetivo.to_dict(), inputs)
    try:
        report = {"objetivos": [{"objective_id": "obj_1", "task_id": task.task_id, "state": "partial"}]}
        plano = cli._plan_continuation_round(store, report, {"obj_1": objetivo}, {"obj_1": inputs})
        assert len(plano["criadas"]) == 1
        assert plano["motivo"] is None
    finally:
        store.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_build_executor_local_recusa_objetivo_de_continuacao_sem_kind():
    """A engine local não tem mais worker de continuação (`_local_continuation_worker`
    foi removido) — um objetivo de continuação (sem `kind`, vocabulário do
    runtime) é recusado como qualquer objetivo cujo `kind` o
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
# 4) engine COM deepening: continuação é criada e o pacote carrega código
# --------------------------------------------------------------------------


def test_plan_continuation_round_com_engine_deepening_cria_e_pacote_tem_codigo():
    """`engine_capabilities={"deepening": True}` (o que `ClaudeCliExecutor`
    declara quando o binário é detectado — ver `capabilities()` em
    `runtime/executors/claude_cli.py`) permite a criação; o pacote da
    continuação criada é montado por `context.build_package` diretamente,
    sem processo real nenhum (§7.3: nunca LLM de verdade na validação)."""
    from runtime import context as rt_context
    from runtime import tasks as rt_tasks

    objetivo = _objetivo_com_need_evidenciada()
    inputs = {"snapshot_id": "scope:x", "source_version_ids": ["app/a.py@sha"]}
    tmpdir, store, task = _tmp_store(objetivo.to_dict(), inputs)
    try:
        report = {"objetivos": [{"objective_id": "obj_1", "task_id": task.task_id, "state": "partial"}]}
        plano = cli._plan_continuation_round(
            store, report, {"obj_1": objetivo}, {"obj_1": inputs},
            engine_capabilities={"dispatch": True, "deepening": True}, engine_name="claude-cli",
        )
        assert len(plano["criadas"]) == 1
        assert plano["recusadas"] == []
        assert plano["motivo"] is None

        cont_task = store.get(plano["criadas"][0])
        assert cont_task.objective["capability_context"]["capability_id"] == "cap1"
        assert cont_task.objective["capability_context"]["modules"] == ["app/a.py"]

        budget = rt_context.Budget(max_bytes=200_000, max_tokens=100_000)
        package = rt_context.build_package(cont_task.objective, budget, _fake_resolver)
        assert len(package.parts) >= 1
        assert "def f()" in package.parts[0].snippet
        envelope = package.refs[0]
        assert envelope["capability_context"]["capability_id"] == "cap1"
    finally:
        store.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------
# 5) proximo_passo orienta o comando exato quando a engine não aprofunda
# --------------------------------------------------------------------------


def test_proximo_passo_orienta_engine_claude_cli_quando_sem_deepening():
    continuacao = dict(cli._EMPTY_CONTINUACAO)
    continuacao["motivo"] = (
        "engine 'local' não aprofunda leitura (capabilities()['deepening'] é False); "
        "nenhuma continuação foi criada — use --engine claude-cli para leitura adicional real"
    )
    texto = cli._proximo_passo_continuacao("/repo", continuacao, [])
    assert "claude-cli" in texto
    assert "não aprofunda leitura" in texto
