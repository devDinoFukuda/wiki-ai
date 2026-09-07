"""R6 — fiação final: integração no fluxo (nº1), manifesto-união (nº5) e
status honesto (nº6).

Cada teste aqui trava UM comportamento que a auditoria encontrou quebrado e que
só existe na costura do CLI: o mapeamento de estado do objetivo para o estado
publicado, a união de namespaces no plano de publicação e a derivação de
`status_geral`/exit code. O caminho ponta a ponta (analyze -> ingest -> resume)
não é reproduzido aqui de propósito: ele depende dos renderizadores e
validadores reais dos vizinhos, e um teste que os arrasta junto falha por
mudança deles, não por regressão desta fiação.
"""

from __future__ import annotations

import inspect

from wk import cli


# --------------------------------------------------------------------------
# nº6a — status_geral e exit code
# --------------------------------------------------------------------------


def test_status_geral_completo_sem_pendencia():
    assert cli._status_geral(
        objetivos_por_estado={"complete": 2, "partial": 0, "blocked": 0},
        revisao_nova=True,
    ) == ("completo", 0)


def test_status_geral_parcial_com_objetivo_partial_sai_zero():
    """Objetivo `partial` é progresso, não erro: status explícito, exit 0."""
    assert cli._status_geral(
        objetivos_por_estado={"complete": 1, "partial": 2, "blocked": 0},
        revisao_nova=True,
    ) == ("parcial", 0)


def test_status_geral_parcial_com_objetivo_blocked():
    assert cli._status_geral(
        objetivos_por_estado={"complete": 0, "partial": 0, "blocked": 1},
        revisao_nova=True,
    ) == ("parcial", 0)


def test_status_geral_parcial_com_decisao_pendente_ou_bloqueio_de_execucao():
    assert cli._status_geral(decisoes_pendentes=1, revisao_nova=True) == ("parcial", 0)
    assert cli._status_geral(bloqueios_execucao=1, revisao_nova=True) == ("parcial", 0)


def test_status_geral_publicacao_bloqueada_com_revisao_nova_e_parcial():
    """Houve resultado útil (revisão gravada): a publicação bloqueada não
    transforma progresso real em falha."""
    assert cli._status_geral(publicacao_bloqueada=True, revisao_nova=True) == ("parcial", 0)


def test_status_geral_bloqueado_sem_revisao_nova_sai_dois():
    assert cli._status_geral(publicacao_bloqueada=True, revisao_nova=False) == ("bloqueado", 2)


def test_status_geral_erro_material_sai_dois():
    assert cli._status_geral(erro_material=True, revisao_nova=True) == ("bloqueado", 2)


# -- Onda10-D: a tabela-verdade ganhou UMA linha (achado nº4 da 2ª auditoria) --
#
# `fontes_incompletas` é o `CorrelationResult.completa is False` do `wk ingest`
# (hoje: referência explícita a um id de padrão conhecido que não resolveu nem
# por `stable_key` nem por alias). Antes desta onda, uma fonte com órfão
# técnico e sem nenhuma outra pendência saía `completo`/exit 0 — a leitura
# exatamente errada, porque a aresta que dependeria daquela referência não
# nasceu. As demais linhas da tabela seguem idênticas: nada aqui torna a
# incompletude um ERRO (exit 2), porque a fonte É aceita e o lote não trava.


def test_status_geral_parcial_com_fonte_incompleta():
    assert cli._status_geral(fontes_incompletas=1, revisao_nova=True) == ("parcial", 0)


def test_status_geral_fonte_incompleta_nunca_sai_completo():
    """Órfão técnico rebaixa `completo` para `parcial` mesmo sem mais nada aberto."""
    assert cli._status_geral(
        objetivos_por_estado={"complete": 3, "partial": 0, "blocked": 0},
        decisoes_pendentes=0,
        bloqueios_execucao=0,
        fontes_incompletas=1,
        revisao_nova=True,
    ) == ("parcial", 0)


def test_status_geral_sem_fonte_incompleta_continua_completo():
    """A linha nova não sequestra o caso feliz: zero incompletas segue `completo`."""
    assert cli._status_geral(
        objetivos_por_estado={"complete": 3, "partial": 0, "blocked": 0},
        fontes_incompletas=0,
        revisao_nova=True,
    ) == ("completo", 0)


# --------------------------------------------------------------------------
# nº6 — IntegrationReport -> investigation_states / resumo
# --------------------------------------------------------------------------


_REPORT = {
    "revisao": "rev_x",
    "mudancas": 4,
    "fatos_gravados": 3,
    "reread_obligations": [{"claim_id": "c1"}],
    "objetivos": [
        {
            "objective_id": "obj_1",
            "capability_id": "ent_cap1",
            "subject_id": "ent_cap1",
            "state": "partial",
            "supported": 1,
            "disputed": 0,
            "unresolved": 2,
            "unmet": ["campo verificacao pendente"],
            "lacunas": [{"origem": "worker", "detalhe": "limite não confirmado"}],
        },
        {
            "objective_id": "obj_2",
            "capability_id": "ent_cap2",
            "subject_id": "ent_cap2",
            "state": "blocked",
            "supported": 0,
            "disputed": 1,
            "unresolved": 0,
            "unmet": [],
            "lacunas": [],
        },
    ],
}


def test_integration_states_mapeia_estado_do_objetivo_para_analysis_state():
    states = cli._integration_states(_REPORT)
    # `partial` -> `parcial`; `blocked` -> `estrutural` (não `parcial`: objetivo
    # bloqueado não teve comportamento avaliado nesta revisão)
    assert states["ent_cap1"]["state"] == "parcial"
    assert states["ent_cap2"]["state"] == "estrutural"
    # a mesma entrada é alcançável pelo objective_id (chave que o planner também aceita)
    assert states["obj_1"] == states["ent_cap1"]


def test_integration_states_carrega_lacunas_e_unmet_como_gaps():
    gaps = cli._integration_states(_REPORT)["ent_cap1"]["gaps"]
    assert "limite não confirmado" in gaps
    assert "campo verificacao pendente" in gaps


def test_integration_states_sao_aceitos_pelo_planner():
    """O contrato é do vizinho: `plan` aceita `{chave: {state, gaps}}` e o
    `state` precisa ser um `AnalysisState` válido."""
    from publishing.document import AnalysisState

    for entry in cli._integration_states(_REPORT).values():
        assert AnalysisState(entry["state"]) in AnalysisState


def test_integration_summary_conta_estados_fatos_e_lacunas():
    resumo = cli._integration_summary(_REPORT)
    assert resumo["revisao"] == "rev_x"
    assert resumo["mudancas"] == 4
    assert resumo["objetivos_por_estado"] == {"complete": 0, "partial": 1, "blocked": 1}
    assert resumo["fatos"] == {"supported": 1, "disputed": 1, "unresolved": 2}
    assert resumo["lacunas_totais"] == 1
    assert resumo["reread_obligations"] == 1
    # Onda10-D: as chaves novas existem mesmo quando o relatório não as traz —
    # quem lê a saída não precisa distinguir "campo ausente" de "zero".
    assert resumo["descartados"] == {"total": 0, "motivos": []}
    assert resumo["rejeitados"] == {"total": 0, "itens": []}


def test_integration_summary_de_relatorio_vazio_nao_explode():
    resumo = cli._integration_summary({})
    assert resumo["objetivos_por_estado"] == {"complete": 0, "partial": 0, "blocked": 0}
    assert resumo["revisao"] is None


# --------------------------------------------------------------------------
# nº5 — plano da UNIÃO de namespaces
# --------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, namespaces):
        self.namespaces = namespaces

    def execute(self, _sql):
        return _Rows([(ns,) for ns in self.namespaces])


class _Rows(list):
    def fetchall(self):
        return list(self)


class _FakeRepo:
    def __init__(self, namespaces):
        self.conn = _FakeConn(namespaces)


class _FakeDoc:
    def __init__(self, namespace, doc_id):
        self.namespace = namespace
        self.document_id = doc_id
        self.units = ()


def _fake_plan_module(monkeypatch, fn):
    from publishing import planner as planner_mod

    monkeypatch.setattr(planner_mod, "plan", fn)
    return planner_mod


def test_plan_union_usa_namespace_none_e_repassa_investigation_states(monkeypatch):
    chamadas = []

    def fake_plan(repo, revision_id, namespace=None, consumers=(), investigation_states=None):
        chamadas.append({"namespace": namespace, "states": investigation_states})
        from publishing.planner import PublicationPlan

        return PublicationPlan(
            revision_id=revision_id,
            namespace="todos",
            documents=(_FakeDoc("code/x", "d1"), _FakeDoc("wiki", "d2")),
        )

    _fake_plan_module(monkeypatch, fake_plan)
    plan_obj = cli._plan_union(_FakeRepo(["code/x", "wiki"]), "rev1", {"ent": {"state": "parcial"}})

    assert chamadas == [{"namespace": None, "states": {"ent": {"state": "parcial"}}}]
    assert sorted(d.namespace for d in plan_obj.documents) == ["code/x", "wiki"]


def test_plan_union_compoe_por_namespace_quando_none_nao_e_aceito(monkeypatch):
    """Caminho de compatibilidade: assinatura antiga que recusa `namespace=None`
    ainda produz UM manifesto com os documentos dos DOIS universos."""
    vistos = []

    def fake_plan(repo, revision_id, namespace=None, consumers=(), investigation_states=None):
        if namespace is None:
            raise TypeError("namespace obrigatório")
        vistos.append(namespace)
        from publishing.planner import PublicationPlan

        return PublicationPlan(
            revision_id=revision_id,
            namespace=namespace,
            documents=(_FakeDoc(namespace, f"d:{namespace}"),),
        )

    _fake_plan_module(monkeypatch, fake_plan)
    plan_obj = cli._plan_union(_FakeRepo(["code/x", "wiki"]), "rev1", None)

    assert vistos == ["code/x", "wiki"]
    assert sorted(d.namespace for d in plan_obj.documents) == ["code/x", "wiki"]


def test_publish_local_aceita_investigation_states():
    """A assinatura é o contrato entre a integração e a publicação honesta."""
    params = inspect.signature(cli._publish_local).parameters
    assert "investigation_states" in params


def test_publish_local_sem_revisao_reporta_namespaces_vazios(tmp_path):
    out = cli._publish_local(str(tmp_path), "wiki", None)
    assert out["revisao"] is None
    assert out["namespaces_publicados"] == []
    assert out["bloqueios"]
