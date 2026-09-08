"""Onda10-D — fiação final da 2ª remediação de auditoria em `wk/cli.py`.

Duas costuras, e só o que é da COSTURA está aqui (a regra em si mora nos
vizinhos, que esta onda não edita):

* achado nº5 — escopo na integração: `objectives` correntes + o
  `expected_input_versions_hash` do snapshot corrente, e o descarte VISÍVEL do
  que ficou de fora (`integracao.descartados` / `integracao.rejeitados`);
* achado nº3 — leituras satisfeitas/pendentes (o dado que `wk status` mostra).

O que NÃO é reproduzido aqui: o ponta a ponta com `knowledge.integrate` e os
renderizadores reais (mesma razão de `test_fix_r6`: falharia por mudança do
vizinho, não por regressão desta fiação). A validação ponta a ponta foi feita
fora do teste, com dois mini-repos num store único.

A 3ª costura original (nº3 — laço de continuação BOUNDED por invocação,
`_plan_continuation_round`/`_continuation_cycle`/`_unmet_needs_of`/
`_proximo_passo_continuacao`/`_dispatch_disponivel`/`_EMPTY_CONTINUACAO`) foi
REMOVIDA de `cli.py` numa limpeza de código morto posterior: o laço inteiro
de continuação passou a rodar em `_run_investigation_chain` (§7.3, UMA
chamada por invocação até conclusão ou motivo material de parada) — ver a
tabela de destino teste-a-teste no lugar onde esses testes viviam (git log
deste arquivo) e a suíte que os substitui, `scripts/wk/tests/
test_run_chain_cli.py`.
"""

from __future__ import annotations

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
# nº3 — unmet_needs / continuação por rodada: MECÂNICA ANTIGA REMOVIDA
#
# `_unmet_needs_of`/`_plan_continuation_round`/`_continuation_cycle`/
# `_proximo_passo_continuacao`/`_dispatch_disponivel`/`_EMPTY_CONTINUACAO`
# foram removidos de `cli.py` nesta limpeza de código morto: sem chamador de
# produção desde que o laço de continuação inteiro (despacho -> integra ->
# planeja -> repete) passou a rodar em `_run_investigation_chain`, UMA
# chamada por invocação de `analyze`/`update`/`resume`, até conclusão ou
# motivo material de parada (§7.3) — nunca mais uma rodada por invocação
# desta CLI orquestrando `_plan_continuation_round` manualmente.
#
# Onde a MESMA garantia comportamental sobrevive, testada contra o alvo novo:
#
# | comportamento                                    | teste antigo (aqui, removido)                          | teste novo                                                                 |
# |---------------------------------------------------|---------------------------------------------------------|-----------------------------------------------------------------------------|
# | leituras abertas viram pedido de continuação       | test_unmet_needs_traz_leituras_abertas_com_alvo          | `knowledge.integrate` publica `reading_needs` nativamente no outcome; `_run_investigation_chain` espelha (ver `_outcomes_fn_for` em cli.py) — T06 (test_run_chain_cli.py) exercita ponta a ponta |
# | leitura fechada sai da lista de pendências         | test_unmet_needs_desconta_o_que_a_integracao_fechou      | T06MultiRoundaTest (test_run_chain_cli.py): 2ª rodada não repete a 1ª leitura já satisfeita |
# | objetivo sem alvo concreto não gera continuação    | test_unmet_needs_de_objetivo_sem_leitura_aberta_e_vazio  | NoProgressTest (test_run_chain_cli.py): sem alvo novo -> `stop_reason=no_progress`, nenhuma 2ª continuação |
# | teto de rodadas respeitado, recusa com motivo      | test_plan_continuation_round_cria_uma_rodada_e_respeita_o_teto | MaxRoundsTest (test_run_chain_cli.py): `budget_exhausted`/`rounds_used` através de invocações |
# | reaplicar não duplica tarefa                       | test_plan_continuation_round_reaplicado_nao_duplica      | NoProgressTest (test_run_chain_cli.py): "pacote idêntico não pode gerar 3ª tarefa" |
# | objetivo `complete` não gera continuação           | test_objetivo_complete_nao_gera_continuacao              | `runtime.coordinator.run_chain`/`STOP_COMPLETED` — coberto em `runtime/tests/test_run_chain.py` (dono é `runtime`, não `cli.py`) |
# | objetivo de outro repo é excluído do escopo        | test_objetivo_fora_do_plano_corrente_e_recusado_com_motivo | ESTRUTURAL agora: `_run_investigation_chain` chama `run_chain(objective_ids=[oid])` POR objetivo — um objetivo de outro escopo nunca entra no laço para começo de conversa (não precisa mais recusa explícita em tempo de planejamento) |
# | bloqueio de despacho reconhecido (tipo)            | test_dispatch_disponivel_le_o_bloqueio_ja_emitido        | ExecutorUnavailableTest (test_run_chain_cli.py) exercita o mesmo vocabulário de bloqueio via `_connect_chain_binding` |
# | próximo passo orienta `wk resume`/reconectar        | test_proximo_passo_orienta_*                             | `_chain_next_action`/`_chain_envelope` (cli.py) — ExecutorUnavailableTest.test_next_action_de_executor_indisponivel_e_concreto (test_run_chain_cli.py) |
#
# `_round_relatado_e_o_criado_agora_nao_o_historico` não tem equivalente: o
# par `round`/`round_historico` era um conceito EXCLUSIVO do desenho antigo
# (uma tarefa de continuação por invocação); o `chain_status`
# (`rounds_used`/`max_rounds`/`stop_reason`) que o substitui é persistido
# pelo PRÓPRIO `runtime.coordinator`/`runtime.tasks` (`TaskStore.
# chain_status`), já coberto pelos testes de `runtime/tests/` e por
# MaxRoundsTest aqui.
# --------------------------------------------------------------------------


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
