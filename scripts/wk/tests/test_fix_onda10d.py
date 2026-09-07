"""Onda10-D — fiação final da 2ª remediação de auditoria em `wk/cli.py`.

Três costuras, e só o que é da COSTURA está aqui (a regra em si mora nos
vizinhos, que esta onda não edita):

* achado nº5 — escopo na integração: `objectives` correntes + o
  `expected_input_versions_hash` do snapshot corrente, e o descarte VISÍVEL do
  que ficou de fora (`integracao.descartados` / `integracao.rejeitados`);
* achado nº3 — laço de continuação BOUNDED: uma rodada por invocação,
  `input_versions` por escopo de objetivo, teto de rodadas respeitado, e a
  tarefa de continuação executável pela engine `local`;
* achado nº4 — `wk ingest` leva a sério `CorrelationResult.completa`.

O que NÃO é reproduzido aqui: o ponta a ponta com `knowledge.integrate` e os
renderizadores reais (mesma razão de `test_fix_r6`: falharia por mudança do
vizinho, não por regressão desta fiação). A validação ponta a ponta foi feita
fora do teste, com dois mini-repos num store único.
"""

from __future__ import annotations

import os
import tempfile

from wk import cli


# --------------------------------------------------------------------------
# nº5 — escopo: input_versions por objetivo e hash esperado
# --------------------------------------------------------------------------


class _Entry:
    def __init__(self, sha256: str) -> None:
        self.sha256 = sha256


class _Snapshot:
    def __init__(self, files: dict) -> None:
        self._files = {p: _Entry(s) for p, s in files.items()}

    def file_map(self) -> dict:
        return self._files


class _Cap:
    def __init__(self, capability_id: str, paths) -> None:
        self.capability_id = capability_id
        self.paths = tuple(paths)


class _CapMap:
    def __init__(self, caps) -> None:
        self._by_id = {c.capability_id: c for c in caps}

    def by_id(self, cap_id: str):
        return self._by_id.get(cap_id)


_SNAP = _Snapshot({"app/a.py": "sha_a", "app/b.py": "sha_b"})
_CAPS = _CapMap([_Cap("cap1", ["app/a.py"]), _Cap("cap2", ["app/b.py"])])
_OBJ_DICTS = [
    {"objective_id": "obj_1", "capability_id": "cap1"},
    {"objective_id": "obj_2", "capability_id": "cap2"},
]


def test_input_versions_por_objetivo_usa_o_escopo_do_objetivo():
    """Cada objetivo carrega SÓ os arquivos que descreve (nunca o repo inteiro).

    É o que faz `wk update` reaproveitar a tarefa de uma capacidade não
    afetada — e, aqui, o que faz o hash esperado da integração distinguir um
    objetivo do outro.
    """
    inputs = cli._objective_input_versions(_OBJ_DICTS, _SNAP, _CAPS, {})
    assert inputs["obj_1"]["source_version_ids"] == ["app/a.py@sha_a"]
    assert inputs["obj_2"]["source_version_ids"] == ["app/b.py@sha_b"]
    assert inputs["obj_1"]["snapshot_id"] != inputs["obj_2"]["snapshot_id"]


def test_hash_esperado_bate_com_o_que_plan_from_objectives_grava():
    """O hash da integração é o MESMO que a tarefa carrega — senão o resultado
    correto seria descartado como "de outra versão"."""
    from runtime import tasks as rt_tasks

    inputs = cli._objective_input_versions(_OBJ_DICTS, _SNAP, _CAPS, {})
    esperados = cli._expected_input_hashes(inputs)
    for oid, iv in inputs.items():
        assert esperados[oid] == rt_tasks.input_versions_hash(iv)


def test_hash_esperado_muda_quando_o_arquivo_muda():
    """Código alterado -> hash novo -> resultado antigo é descartado (achado nº5)."""
    antes = cli._expected_input_hashes(
        cli._objective_input_versions(_OBJ_DICTS, _SNAP, _CAPS, {})
    )
    depois = cli._expected_input_hashes(
        cli._objective_input_versions(
            _OBJ_DICTS,
            _Snapshot({"app/a.py": "sha_a2", "app/b.py": "sha_b"}),
            _CAPS,
            {},
        )
    )
    assert antes["obj_1"] != depois["obj_1"]
    assert antes["obj_2"] == depois["obj_2"]  # objetivo não afetado: hash estável


def test_integracao_expoe_descartados_com_motivo():
    """"Não integrei o resultado do outro repositório" é informação, não silêncio."""
    resumo = cli._integration_summary({
        "objetivos": [],
        "descartados": [
            {
                "objective_id": "obj_do_repo_A",
                "task_id": "task_1",
                "motivo": "objective_id 'obj_do_repo_A' fora do escopo desta integração",
            }
        ],
    })
    assert resumo["descartados"]["total"] == 1
    assert resumo["descartados"]["motivos"][0]["objective_id"] == "obj_do_repo_A"
    assert "fora do escopo" in resumo["descartados"]["motivos"][0]["motivo"]


def test_integracao_expoe_rejeitados_por_objetivo():
    """Claim recusada por escopo (caminho fora do snapshot) sobe ao JSON com o
    objetivo de origem — sem isso, ela só existiria dentro do relatório."""
    resumo = cli._integration_summary({
        "objetivos": [
            {
                "objective_id": "obj_1",
                "state": "partial",
                "rejeitados": [{"claim_id": "c1", "motivo": "citação fora do snapshot corrente"}],
            }
        ]
    })
    assert resumo["rejeitados"]["total"] == 1
    assert resumo["rejeitados"]["itens"][0]["objective_id"] == "obj_1"
    assert resumo["rejeitados"]["itens"][0]["claim_id"] == "c1"


def test_integrate_results_recebe_escopo_e_hash_esperado():
    """A assinatura da costura precisa aceitar as DUAS vias do escopo."""
    import inspect

    params = inspect.signature(cli._integrate_results).parameters
    assert "objectives" in params
    assert "expected_input_versions_hash" in params


# --------------------------------------------------------------------------
# nº3 — leituras satisfeitas/pendentes (o dado que o `wk status` mostra)
# --------------------------------------------------------------------------


_REPORT_LEITURAS = {
    "objetivos": [
        {
            "objective_id": "obj_1",
            "state": "partial",
            "leituras_satisfeitas": [
                {"need_id": "n1", "target": "app/a.py", "satisfeita": True, "evidencia": ["app/a.py:1-9"]},
                {
                    "need_id": "n2",
                    "target": "app/b.py",
                    "satisfeita": False,
                    "motivo": "app/b.py:1-9: snippet_hash divergente",
                },
            ],
        }
    ]
}


def test_leituras_separam_satisfeitas_de_declaradas_sem_evidencia():
    """Declaração não fecha obrigação: a leitura NÃO satisfeita aparece com motivo."""
    leituras = cli._leituras_do_relatorio(_REPORT_LEITURAS)
    assert leituras["satisfeitas"] == 1
    assert leituras["nao_satisfeitas"] == 1
    assert leituras["itens_satisfeitos"][0]["need_id"] == "n1"
    assert "snippet_hash" in leituras["itens_nao_satisfeitos"][0]["motivo"]


def test_leituras_de_relatorio_vazio_nao_explode():
    assert cli._leituras_do_relatorio({})["satisfeitas"] == 0


# --------------------------------------------------------------------------
# nº3 — unmet_needs: o que vira (e o que não vira) tarefa de continuação
# --------------------------------------------------------------------------


def _objetivo_com_needs():
    from analysis.investigation import (
        ContractField,
        CONTRACT_FIELDS,
        CONTRACT_LABELS,
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
        contract={n: ContractField(name=n, label=CONTRACT_LABELS[n]) for n in CONTRACT_FIELDS},
        reading_needs=[
            ReadingNeed(
                need_id="n1", kind=ReadingKind.SYMBOL, target="app/a.py::f",
                motivo="símbolo alcançado pela entrada", trigger=ReadingTrigger.UNRESOLVED_CALL,
            ),
            ReadingNeed(
                need_id="n2", kind=ReadingKind.SYMBOL, target="app/b.py::g",
                motivo="dependência declarada", trigger=ReadingTrigger.UNRESOLVED_CALL,
            ),
        ],
    )


def test_unmet_needs_traz_leituras_abertas_com_alvo():
    needs = cli._unmet_needs_of(_objetivo_com_needs(), {})
    assert [n["target"] for n in needs] == ["app/a.py::f", "app/b.py::g"]
    assert all(n["motivo"] for n in needs)  # `plan_continuations` exige motivo rastreável


def test_unmet_needs_desconta_o_que_a_integracao_fechou():
    """Leitura fechada COM evidência resolvida sai da lista; a declarada e não
    resolvida continua nela (senão a continuação nunca seria criada para ela)."""
    outcome = {
        "leituras_satisfeitas": [
            {"need_id": "n1", "target": "app/a.py::f", "satisfeita": True},
            {"need_id": "n2", "target": "app/b.py::g", "satisfeita": False, "motivo": "sem evidência"},
        ]
    }
    needs = cli._unmet_needs_of(_objetivo_com_needs(), outcome)
    assert [n["need_id"] for n in needs] == ["n2"]


def test_unmet_needs_de_objetivo_sem_leitura_aberta_e_vazio():
    """Sem alvo concreto não há o que investigar de novo — `plan_continuations`
    recusa, e recusar é melhor que criar tarefa que repetiria o mesmo impasse."""
    objetivo = _objetivo_com_needs()
    for need in objetivo.reading_needs:
        need.waive("fronteira explícita do escopo")
    assert cli._unmet_needs_of(objetivo, {}) == []


# --------------------------------------------------------------------------
# nº3 — o laço é BOUNDED: uma rodada por invocação, teto respeitado
# --------------------------------------------------------------------------


def _store_com_tarefa(tmpdir: str, objective: dict, inputs: dict):
    from runtime import tasks as rt_tasks

    store = rt_tasks.TaskStore.open(os.path.join(tmpdir, "runtime.db"))
    task = store.create_task(rt_tasks.TaskKind.INVESTIGATION, objective, inputs)
    return store, task


def test_plan_continuation_round_cria_uma_rodada_e_respeita_o_teto():
    """Rodada N+1 a cada invocação; passado o teto, RECUSA com motivo — nunca
    exceção, nunca laço."""
    from runtime import tasks as rt_tasks

    objetivo = _objetivo_com_needs()
    inputs = {"snapshot_id": "scope:x", "source_version_ids": ["app/a.py@sha"]}
    tmpdir = tempfile.mkdtemp()
    try:
        store, task = _store_com_tarefa(tmpdir, objetivo.to_dict(), inputs)
        try:
            rt_tasks.set_max_continuation_rounds(store, 2)
            report = {"objetivos": [{"objective_id": "obj_1", "task_id": task.task_id, "state": "partial"}]}
            objetivos_by_id = {"obj_1": objetivo}
            inputs_by_objective = {"obj_1": inputs}

            r1 = cli._plan_continuation_round(store, report, objetivos_by_id, inputs_by_objective)
            assert len(r1["criadas"]) == 1 and r1["round"] == 1 and r1["max_rounds"] == 2

            # a rodada seguinte parte da tarefa de continuação recém-criada
            filha = r1["criadas"][0]
            report2 = {"objetivos": [{"objective_id": "obj_1", "task_id": filha, "state": "partial"}]}
            r2 = cli._plan_continuation_round(store, report2, objetivos_by_id, inputs_by_objective)
            assert len(r2["criadas"]) == 1 and r2["round"] == 2

            neta = r2["criadas"][0]
            report3 = {"objetivos": [{"objective_id": "obj_1", "task_id": neta, "state": "partial"}]}
            r3 = cli._plan_continuation_round(store, report3, objetivos_by_id, inputs_by_objective)
            assert r3["criadas"] == []
            assert "excede o máximo de 2" in r3["recusadas"][0]["motivo"]
        finally:
            store.close()
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


def test_plan_continuation_round_reaplicado_nao_duplica():
    """Mesmo outcome, mesma tarefa-mãe -> mesma rodada -> `create_task` devolve
    a tarefa que já existe (§7.2). É por isso que reexecutar o comando não
    multiplica trabalho."""
    objetivo = _objetivo_com_needs()
    inputs = {"snapshot_id": "scope:x", "source_version_ids": []}
    tmpdir = tempfile.mkdtemp()
    try:
        store, task = _store_com_tarefa(tmpdir, objetivo.to_dict(), inputs)
        try:
            report = {"objetivos": [{"objective_id": "obj_1", "task_id": task.task_id, "state": "partial"}]}
            args = (store, report, {"obj_1": objetivo}, {"obj_1": inputs})
            primeira = cli._plan_continuation_round(*args)
            segunda = cli._plan_continuation_round(*args)
            assert primeira["criadas"] == segunda["criadas"]
        finally:
            store.close()
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


def test_round_relatado_e_o_criado_agora_nao_o_historico():
    """Depois de um `wk update`, as ENTRADAS mudaram: a tarefa-mãe é nova e a
    cadeia de continuação recomeça em 1, mesmo que o objetivo já tenha chegado
    à rodada 2 sobre a versão anterior do código. `round` diz o que foi criado
    agora (quanto ainda cabe até o teto); `round_historico`, o maior já visto.
    """
    objetivo = _objetivo_com_needs()
    tmpdir = tempfile.mkdtemp()
    try:
        inputs_v1 = {"snapshot_id": "scope:v1", "source_version_ids": ["app/a.py@sha1"]}
        store, mae_v1 = _store_com_tarefa(tmpdir, objetivo.to_dict(), inputs_v1)
        try:
            r1 = cli._plan_continuation_round(
                store,
                {"objetivos": [{"objective_id": "obj_1", "task_id": mae_v1.task_id, "state": "partial"}]},
                {"obj_1": objetivo}, {"obj_1": inputs_v1},
            )
            r2 = cli._plan_continuation_round(
                store,
                {"objetivos": [{"objective_id": "obj_1", "task_id": r1["criadas"][0], "state": "partial"}]},
                {"obj_1": objetivo}, {"obj_1": inputs_v1},
            )
            assert r2["round"] == 2

            # código mudou: tarefa-mãe NOVA, cadeia recomeça
            inputs_v2 = {"snapshot_id": "scope:v2", "source_version_ids": ["app/a.py@sha2"]}
            mae_v2 = store.create_task(
                mae_v1.kind, objetivo.to_dict(), inputs_v2
            )
            store.refresh_states()
            novo = cli._plan_continuation_round(
                store,
                {"objetivos": [{"objective_id": "obj_1", "task_id": mae_v2.task_id, "state": "partial"}]},
                {"obj_1": objetivo}, {"obj_1": inputs_v2},
            )
            assert novo["round"] == 1
            assert novo["round_historico"] == 2
        finally:
            store.close()
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


def test_reading_satisfied_do_worker_e_dado_aceito_pelo_schema_fechado():
    """O campo por onde a leitura satisfeita FLUI do worker até a integração.

    Aqui só se trava que ele atravessa o schema fechado do coordenador como
    DADO (quem decide se a obrigação fecha é `knowledge.integrate`, e só com
    evidência que resolve no snapshot).
    """
    from runtime.coordinator import DEFAULT_SCHEMA

    saida = {
        "objective_id": "obj_1",
        "state": "partial",
        "reading_needs": [{"need_id": "n2", "target": "app/b.py::g", "motivo": "x"}],
        "reading_satisfied": [
            {
                "need_id": "n1",
                "target": "app/a.py::f",
                "evidence_refs": [{"path": "app/a.py", "line_start": 1, "line_end": 9}],
            }
        ],
    }
    assert DEFAULT_SCHEMA.validate(saida) == []


def test_objetivo_complete_nao_gera_continuacao():
    objetivo = _objetivo_com_needs()
    inputs = {"snapshot_id": "scope:x", "source_version_ids": []}
    tmpdir = tempfile.mkdtemp()
    try:
        store, task = _store_com_tarefa(tmpdir, objetivo.to_dict(), inputs)
        try:
            report = {"objetivos": [{"objective_id": "obj_1", "task_id": task.task_id, "state": "complete"}]}
            plano = cli._plan_continuation_round(store, report, {"obj_1": objetivo}, {"obj_1": inputs})
            assert plano["criadas"] == [] and plano["recusadas"] == []
        finally:
            store.close()
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


def test_objetivo_fora_do_plano_corrente_e_recusado_com_motivo():
    """Objetivo do repositório vizinho no mesmo `runtime.db` não ganha
    continuação neste comando (achado nº5 aplicado ao laço do nº3)."""
    objetivo = _objetivo_com_needs()
    inputs = {"snapshot_id": "scope:x", "source_version_ids": []}
    tmpdir = tempfile.mkdtemp()
    try:
        store, task = _store_com_tarefa(tmpdir, objetivo.to_dict(), inputs)
        try:
            report = {"objetivos": [{"objective_id": "obj_de_outro_repo", "task_id": task.task_id, "state": "partial"}]}
            plano = cli._plan_continuation_round(store, report, {"obj_1": objetivo}, {"obj_1": inputs})
            assert plano["criadas"] == []
            assert "não pertence ao plano corrente" in plano["recusadas"][0]["motivo"]
        finally:
            store.close()
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------
# nº3 — a tarefa de continuação PRECISA ser executável pela engine local
# --------------------------------------------------------------------------


def test_engine_local_despacha_objetivo_de_continuacao():
    """`create_continuation_tasks` monta o objetivo sem `kind` (vocabulário do
    plano, não do runtime) e `LocalThreadExecutor` escolhe o worker por ele.
    Sem o envelope, toda continuação morria `blocked:transient` no submit."""
    executor = cli._build_executor("local", {"obj_1": {"capability_id": "cap1", "contract": {}}})
    try:
        objetivo_continuacao = {
            "objective_id": "obj_1",
            "continuation": True,
            "round": 1,
            "needs": [{"need_id": "n1", "target": "app/a.py::f", "motivo": "x"}],
            "parent_task_id": "task_mae",
        }
        execution_id = executor.submit("task_filha", objetivo_continuacao, [], {}, {})
        assert isinstance(execution_id, str) and execution_id
    finally:
        executor.shutdown(wait=True)


def test_worker_local_de_continuacao_nao_fecha_leitura_nem_regride_contrato():
    """A engine local não lê nada: devolve o contrato que a extração estática já
    estabeleceu (não regride o que foi gravado) e mantém as leituras ABERTAS."""
    worker = cli._local_continuation_worker({"obj_1": {"capability_id": "cap1", "contract": {"identidade": {}}}})
    out = worker(
        objective={
            "objective_id": "obj_1", "continuation": True, "round": 2,
            "needs": [{"need_id": "n1", "target": "app/a.py::f", "motivo": "x"}],
        },
        references=[], schema={}, cancel_event=None,
    )
    assert out["objective_id"] == "obj_1"
    assert out["capability_id"] == "cap1"
    assert out["contract"] == {"identidade": {}}
    assert out["state"] == "partial"
    assert out["reading_needs"] == [{"need_id": "n1", "target": "app/a.py::f", "motivo": "x"}]
    assert "reading_satisfied" not in out  # nunca declara ter lido


def test_resultado_do_worker_de_continuacao_passa_no_schema_fechado():
    """§13.1: o schema do coordenador é fechado — campo não declarado é recusa."""
    from runtime.coordinator import DEFAULT_SCHEMA

    worker = cli._local_continuation_worker({})
    out = worker(
        objective={"objective_id": "obj_1", "continuation": True, "round": 1, "needs": []},
        references=[], schema={}, cancel_event=None,
    )
    assert DEFAULT_SCHEMA.validate(out) == []


def test_dispatch_disponivel_le_o_bloqueio_ja_emitido():
    assert cli._dispatch_disponivel([]) is True
    assert cli._dispatch_disponivel([{"tipo": "dispatch_indisponivel"}]) is False
    assert cli._dispatch_disponivel([{"tipo": "engine_desconhecida"}]) is False
    assert cli._dispatch_disponivel([{"tipo": "snapshot_ausente"}]) is True


def test_proximo_passo_orienta_wk_resume_quando_nao_ha_despacho():
    """Sem despacho, a continuação fica `ready` e quem a executa é `wk resume`."""
    texto = cli._proximo_passo_continuacao(
        "/repo", {"round": 1, "max_rounds": 3, "criadas": ["t1"], "recusadas": []},
        [{"tipo": "dispatch_indisponivel"}],
    )
    assert "wk resume" in texto and "ready" in texto


def test_proximo_passo_explica_o_teto_quando_houve_recusa():
    texto = cli._proximo_passo_continuacao(
        "/repo",
        {"round": 3, "max_rounds": 3, "criadas": [],
         "recusadas": [{"objective_id": "obj_1", "motivo": "round 4 excede o máximo de 3 rodadas"}]},
        [],
    )
    assert "decisão humana" in texto
