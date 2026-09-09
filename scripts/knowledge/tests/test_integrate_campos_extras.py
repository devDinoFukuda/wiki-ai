"""Nada que o worker respondeu e o schema aceitou pode sumir na integração.

Dois buracos de PERDA SILENCIOSA fechados aqui, pelo caminho real
(`integrate()`, nunca a função interna isolada):

1. Chave dentro de `contract` fora do §6.3 — `results_to_claims` iterava só
   `CONTRACT_FIELDS`: o campo respondido não virava claim, não gerava fato e
   não aparecia em relatório nenhum. Agora vira Claim DEGRADADO (sem
   `statement_fields`) e entra em `campos_nao_previstos`.
2. Chave de TOPO autorizada pelo operador (`ResultSchema.with_extra`) — era
   aceita pelo coordenador e descartada aqui. Agora atravessa até
   `ObjectiveOutcome.to_dict()["extra"]`, que é o payload do CLI.

`CONTRACT_FIELDS` não é alterada por nenhum dos dois: campo novo não vira
vocabulário do §6.3 por ter aparecido num resultado.
"""

import os
import shutil
import tempfile
import unittest

from analysis.extractors.base import SourceFile
from analysis.extractors.registry import default_registry
from analysis.investigation import (
    CONTRACT_FIELDS,
    CONTRACT_LABELS,
    ContractField,
    FailureEdgeMatrix,
    InvestigationObjective,
    MatrixIntegrityError,
)
from analysis.snapshot import capture
from knowledge import integrate as I
from knowledge.repository import Repository
from runtime import coordinator as C
from runtime import tasks as rt_tasks


FONTE = '''\
def approve(total):
    if total > 1000:
        return {"status": 409}
    return {"status": 200}
'''


def _extraction_of(snapshot):
    registry = default_registry()
    arquivos = []
    for entry in snapshot.files:
        with open(os.path.join(snapshot.repo, entry.path), "r", encoding="utf-8") as fh:
            arquivos.append(SourceFile(path=entry.path, content=fh.read()))
    return registry.extract_all(arquivos)


def _objective_dict(objective_id="obj-1", capability_id="cap-1"):
    return {
        "objective_id": objective_id,
        "kind": "capability",
        "capability_id": capability_id,
        "name": "Aprovacao",
        "contract": {
            n: ContractField(name=n, label=CONTRACT_LABELS[n]).to_dict()
            for n in CONTRACT_FIELDS
        },
        "reading_needs": [],
        "state": "partial",
    }


class _IntegraCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="campos-extras-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.repo_dir = os.path.join(self.tmpdir, "repo")
        os.makedirs(self.repo_dir, exist_ok=True)
        with open(os.path.join(self.repo_dir, "rules.py"), "w", encoding="utf-8") as fh:
            fh.write(FONTE)
        self.snapshot = capture(self.repo_dir)
        self.extraction = _extraction_of(self.snapshot)
        self.versions = {
            "snapshot_id": self.snapshot.snapshot_id,
            "source_version_ids": [],
        }

    def _result(self, output, task_id="t1"):
        return I.AcceptedResult(
            task_id=task_id,
            kind="investigation",
            objective_id="obj-1",
            capability_id="cap-1",
            execution_id="exec-1",
            input_versions=self.versions,
            input_versions_hash=rt_tasks.input_versions_hash(self.versions),
            output=output,
            objective_payload={"objective_id": "obj-1", "capability_id": "cap-1"},
            asserted_by="llm:worker",
            created_at="2026-09-08T00:00:00Z",
        )

    def _integrate(self, output, **kwargs):
        repo = Repository.open(os.path.join(self.repo_dir, "knowledge.db"))
        self.addCleanup(repo.close)
        return I.integrate(
            repo,
            None,
            self.snapshot,
            self.extraction,
            "code/repro",
            objectives=[_objective_dict()],
            results=[self._result(output)],
            **kwargs,
        )


# --------------------------------------------------------------------------
# 2. Chave desconhecida DENTRO de `contract`
# --------------------------------------------------------------------------


class CampoNaoPrevistoNoContratoTest(_IntegraCase):
    def _output(self):
        return {
            "objective_id": "obj-1",
            "capability_id": "cap-1",
            "contract": {
                "decisoes": {
                    "status": "filled",
                    "afirmacoes": [
                        {
                            "regra": "RN-900",
                            "statement": "Quando total > 1000, o pedido retorna HTTP 409.",
                            "evidence_refs": [
                                {"path": "rules.py", "line_start": 1, "line_end": 4}
                            ],
                        }
                    ],
                },
                # Campo que NÃO existe no §6.3 — antes desaparecia inteiro.
                "risco_operacional": {
                    "status": "filled",
                    "content": "A rota de aprovacao nao tem rollback automatico.",
                    "evidence_refs": [
                        {"path": "rules.py", "line_start": 1, "line_end": 4}
                    ],
                },
            },
        }

    def test_campo_desconhecido_vira_claim_degradado(self):
        claims = I.results_to_claims(self._result(self._output()), _objective_dict())
        novos = [c for c in claims if c.scope == "obj-1:risco_operacional"]
        self.assertEqual(len(novos), 1, [c.claim_id for c in claims])
        claim = novos[0]
        self.assertEqual(
            claim.statement, "A rota de aprovacao nao tem rollback automatico."
        )
        # Degradado: sem estrutura mecânica inventada, com predicado genérico.
        self.assertEqual(claim.statement_fields, {})
        self.assertEqual(claim.predicate_kind, I.UNFORESEEN_PREDICATE_KIND)
        # A citação do worker é preservada — é o que permite verificá-la.
        self.assertEqual([e.path for e in claim.evidence_refs], ["rules.py"])

    def test_campo_desconhecido_e_listado_no_relatorio(self):
        relatorio = self._integrate(self._output())
        payload = relatorio.to_dict()
        self.assertEqual(
            [(d["objective_id"], d["campo"]) for d in payload["campos_nao_previstos"]],
            [("obj-1", "risco_operacional")],
        )
        item = payload["campos_nao_previstos"][0]
        self.assertEqual(item["task_ids"], ["t1"])
        self.assertTrue(item["claims"])
        # Também no outcome do objetivo, que é o que o CLI imprime por objetivo.
        objetivo = payload["objetivos"][0]
        self.assertEqual(
            [d["campo"] for d in objetivo["campos_nao_previstos"]], ["risco_operacional"]
        )

    def test_campo_desconhecido_vira_fato_e_nao_silencio(self):
        relatorio = self._integrate(self._output())
        campos = {f["campo"] for f in relatorio.to_dict()["objetivos"][0]["fatos"]}
        self.assertIn("risco_operacional", campos)

    def test_vocabulario_do_contrato_nao_e_alterado(self):
        """A garantia que fica: campo novo não entra no §6.3."""
        self._integrate(self._output())
        self.assertNotIn("risco_operacional", CONTRACT_FIELDS)
        self.assertNotIn("risco_operacional", I.FIELD_PREDICATE_KIND)

    def test_contrato_so_com_campos_conhecidos_nao_gera_diagnostico(self):
        """Compatibilidade: nada muda para o resultado que já respeitava o §6.3."""
        output = self._output()
        output["contract"].pop("risco_operacional")
        relatorio = self._integrate(output)
        self.assertEqual(relatorio.to_dict()["campos_nao_previstos"], [])
        self.assertEqual(
            I.unforeseen_contract_fields(self._result(output)), []
        )


# --------------------------------------------------------------------------
# 1. Chaves EXTRAS de topo, autorizadas por `ResultSchema.with_extra`
# --------------------------------------------------------------------------


class ExtraDeTopoPreservadoTest(_IntegraCase):
    def _output(self):
        return {
            "objective_id": "obj-1",
            "capability_id": "cap-1",
            "contract": {"decisoes": {"status": "filled", "content": "Aprova por limite."}},
            # Chave que só existe porque o operador a declarou em
            # `run(result_extra_keys=...)`; o coordenador a aceitou.
            "risco_operacional": {"nivel": "alto", "motivo": "sem rollback"},
        }

    def test_chave_extra_atravessa_ate_o_payload_do_cli(self):
        relatorio = self._integrate(self._output())
        objetivo = relatorio.to_dict()["objetivos"][0]
        self.assertEqual(
            objetivo["extra"], {"risco_operacional": {"nivel": "alto", "motivo": "sem rollback"}}
        )

    def test_campo_conhecido_nunca_e_confundido_com_extra(self):
        relatorio = self._integrate(self._output())
        objetivo = relatorio.to_dict()["objetivos"][0]
        self.assertNotIn("contract", objetivo["extra"])
        self.assertNotIn("objective_id", objetivo["extra"])

    def test_resultado_sem_chave_extra_tem_extra_vazio(self):
        """Compatibilidade: quem não usa a capacidade não vê campo novo com lixo."""
        output = self._output()
        output.pop("risco_operacional")
        relatorio = self._integrate(output)
        self.assertEqual(relatorio.to_dict()["objetivos"][0]["extra"], {})

    def test_vocabulario_conhecido_acompanha_o_schema_do_coordenador(self):
        """Se `DEFAULT_SCHEMA` ganhar campo, este módulo NÃO pode ficar para trás.

        Sem esta comparação, um campo novo do runtime passaria a ser tratado
        como "extra de perfil" aqui — silenciosamente, e para sempre.
        """
        self.assertEqual(I.KNOWN_OUTPUT_FIELDS, C.DEFAULT_SCHEMA.declared)


# --------------------------------------------------------------------------
# Matriz §6.5 recusada pelo `from_dict`: rejeição TIPADA, nunca crash
# --------------------------------------------------------------------------


#: Célula fora de `FAILURE_FAMILIES`, em estado afirmativo, sem justificativa
#: de código e sem `justification_source`: exatamente o payload que
#: `FailureEdgeMatrix.from_dict` passou a recusar.
MATRIZ_SEM_JUSTIFICATIVA = {
    "cells": [
        {
            "family": "resiliencia_regional",
            "item": "failover_cross_region",
            "state": "not_applicable",
        }
    ]
}


class MatrizRecusadaTest(_IntegraCase):
    def _output(self, matrix):
        return {
            "objective_id": "obj-1",
            "capability_id": "cap-1",
            "contract": {
                "decisoes": {"status": "filled", "content": "Aprova por limite."}
            },
            "matrix": matrix,
        }

    def test_o_payload_realmente_e_recusado_pela_matriz(self):
        """Premissa do resto da classe, verificada e não assumida."""
        with self.assertRaises(MatrixIntegrityError):
            FailureEdgeMatrix.from_dict(MATRIZ_SEM_JUSTIFICATIVA)

    def test_matriz_invalida_nao_derruba_a_integracao(self):
        relatorio = self._integrate(self._output(MATRIZ_SEM_JUSTIFICATIVA))
        payload = relatorio.to_dict()
        # A revisão saiu: os fatos do MESMO objetivo continuam gravados.
        self.assertTrue(payload["revisao"])
        self.assertEqual(len(payload["objetivos"]), 1)

    def test_recusa_e_tipada_com_objective_id_e_task_id(self):
        relatorio = self._integrate(self._output(MATRIZ_SEM_JUSTIFICATIVA))
        payload = relatorio.to_dict()
        recusas = [
            r for r in payload["resultados_rejeitados"] if r["origem"] == "resultado"
        ]
        self.assertTrue(recusas, payload["resultados_rejeitados"])
        recusa = recusas[0]
        self.assertEqual(recusa["erro"], "MatrixIntegrityError")
        self.assertTrue(recusa["contrato"])
        self.assertEqual(recusa["objective_id"], "obj-1")
        self.assertEqual(recusa["task_id"], "t1")
        self.assertIn("resiliencia_regional", recusa["motivo"])
        # Também no outcome, que é o recorte por objetivo que o CLI imprime.
        self.assertEqual(
            payload["objetivos"][0]["resultados_rejeitados"], payload["resultados_rejeitados"]
        )

    def test_so_a_matriz_e_descartada_o_resto_da_rodada_fica(self):
        """Recusar uma célula não pode custar o contrato apurado na rodada.

        A matriz do payload é a parte recusada; o objetivo fica com a matriz
        ANTERIOR (já validada) e com tudo o mais que a rodada trouxe. Descartar
        a rodada inteira seria trocar uma perda silenciosa por outra.
        """
        sem_matriz = self._output(MATRIZ_SEM_JUSTIFICATIVA)
        sem_matriz.pop("matrix")
        base = self._integrate(sem_matriz).to_dict()["objetivos"][0]
        com_matriz = self._integrate(self._output(MATRIZ_SEM_JUSTIFICATIVA)).to_dict()[
            "objetivos"
        ][0]
        for campo in ("state", "contract_state", "unmet", "reading_needs"):
            with self.subTest(campo=campo):
                self.assertEqual(com_matriz[campo], base[campo])
        # O contrato da rodada SOBREVIVEU à recusa da matriz.
        self.assertIn("decisoes", com_matriz["contract_state"])
        # A ÚNICA diferença é o diagnóstico, e ele diz o que custou.
        self.assertEqual(base["resultados_rejeitados"], [])
        self.assertEqual(
            [r["descartado"] for r in com_matriz["resultados_rejeitados"]], ["matrix"]
        )
        self.assertNotIn("resiliencia_regional", str(com_matriz["contract_state"]))

    def test_matriz_valida_do_agente_continua_passando(self):
        """Compatibilidade: o que o agente PODE dizer sobre a matriz passa.

        Marcar uma célula como `unresolved` com o impacto é a única coisa que
        um resultado faz sozinho na matriz — exclusão é decisão de perfil, e
        cobertura exige evidência de código.
        """
        valida = {
            "cells": [
                {
                    "family": "entrada",
                    "item": "ausente",
                    "state": "unresolved",
                    "marked": True,
                    "note": "nao foi possivel determinar o comportamento sem entrada",
                }
            ]
        }
        relatorio = self._integrate(self._output(valida))
        self.assertEqual(relatorio.to_dict()["resultados_rejeitados"], [])

    def test_sem_matriz_nenhuma_recusa_e_registrada(self):
        output = self._output(MATRIZ_SEM_JUSTIFICATIVA)
        output.pop("matrix")
        relatorio = self._integrate(output)
        self.assertEqual(relatorio.to_dict()["resultados_rejeitados"], [])
        self.assertEqual(
            relatorio.to_dict()["objetivos"][0]["resultados_rejeitados"], []
        )

    def test_objetivo_do_escopo_com_matriz_invalida_tambem_e_diagnosticado(self):
        """A recusa cobre `_objectives_index`, não só o resultado do agente."""
        objetivo = _objective_dict()
        objetivo["matrix"] = MATRIZ_SEM_JUSTIFICATIVA
        repo = Repository.open(os.path.join(self.repo_dir, "knowledge.db"))
        self.addCleanup(repo.close)
        relatorio = I.integrate(
            repo,
            None,
            self.snapshot,
            self.extraction,
            "code/repro",
            objectives=[objetivo],
            results=[self._result(self._output({}))],
        )
        origens = {r["origem"] for r in relatorio.to_dict()["resultados_rejeitados"]}
        self.assertIn("escopo", origens)


# --------------------------------------------------------------------------
# F3 — exclusão de PERFIL forjada pelo próprio investigado
# --------------------------------------------------------------------------


#: O repro exato da auditoria: o agente escreve `justification_source` no
#: formato do perfil e uma `note` plausível. Sem `justification` de código,
#: sem nada que ligue a célula ao sistema — e a obrigação sumia.
CELULA_FORJADA = {
    "cells": [
        {
            "family": "entrada",
            "item": "ausente",
            "state": "not_applicable",
            "justification": None,
            "justification_source": "profile:cli",
            "note": "cli nao recebe entrada externa",
            "marked": True,
        }
    ]
}


def _matriz_com_exclusao_de_perfil():
    """Matriz do PLANO com uma exclusão legítima, escrita pelo pipeline."""
    matriz = FailureEdgeMatrix()
    matriz.exclude_family(
        "concorrencia", "sistema single-threaded por decisao de arquitetura",
        source="profile:cli",
    )
    return matriz


class ExclusaoDePerfilForjadaTest(_IntegraCase):
    """O worker não cria nem revoga exclusão de perfil (§6.5)."""

    def _output(self, matrix):
        return {
            "objective_id": "obj-1",
            "capability_id": "cap-1",
            "contract": {
                "decisoes": {"status": "filled", "content": "Aprova por limite."}
            },
            "matrix": matrix,
        }

    def _pendente(self, objetivo, celula):
        return any(celula in pendencia for pendencia in objetivo.unmet_obligations())

    def test_repro_da_auditoria_celula_continua_pendente(self):
        """ACEITE 1: payload forjado NÃO dispensa a célula, e deixa rastro."""
        base = InvestigationObjective.from_dict(_objective_dict())
        self.assertTrue(self._pendente(base, "entrada/ausente"))

        rejeicoes = []
        depois, _ = I._objective_after_result(
            base, self._output(CELULA_FORJADA), None, task_id="t1", rejeicoes=rejeicoes
        )
        self.assertTrue(
            self._pendente(depois, "entrada/ausente"),
            "a célula forjada dispensou a obrigação — a brecha continua aberta",
        )
        self.assertTrue(rejeicoes, "recusa silenciosa: nada foi registrado")
        # A recusa é PONTUAL: só a célula forjada cai, não a matriz inteira.
        self.assertEqual(rejeicoes[0]["descartado"], "celula")
        self.assertEqual(rejeicoes[0]["celulas"], ["entrada/ausente"])
        self.assertEqual(rejeicoes[0]["task_id"], "t1")

    def test_repro_da_auditoria_pelo_caminho_real_de_integracao(self):
        """O mesmo, por `integrate()`: o payload do CLI vê o diagnóstico."""
        relatorio = self._integrate(self._output(CELULA_FORJADA))
        payload = relatorio.to_dict()
        recusas = payload["resultados_rejeitados"]
        self.assertTrue(recusas, payload)
        self.assertEqual([r["descartado"] for r in recusas], ["celula"])
        self.assertEqual(recusas[0]["objective_id"], "obj-1")
        self.assertIn("entrada/ausente", recusas[0]["motivo"])
        # O resto da rodada sobreviveu: só a matriz foi recusada.
        self.assertIn("decisoes", payload["objetivos"][0]["contract_state"])

    def test_exclusao_legitima_do_perfil_sobrevive_a_rodada_do_agente(self):
        """ACEITE 2: o agente não consegue APAGAR o que o perfil excluiu."""
        data = _objective_dict()
        data["matrix"] = _matriz_com_exclusao_de_perfil().to_dict()
        base = InvestigationObjective.from_dict(data, trust_profile_cells=True)
        excluidas = base.matrix.profile_cells()
        self.assertTrue(excluidas, "o cenário exige exclusão de perfil no plano")

        # O agente devolve uma matriz que simplesmente NÃO tem aquelas células.
        rejeicoes = []
        depois, _ = I._objective_after_result(
            base,
            self._output({"cells": [
                {"family": "entrada", "item": "ausente", "state": "unresolved",
                 "marked": True, "note": "ainda nao verificado"}
            ]}),
            None,
            task_id="t2",
            rejeicoes=rejeicoes,
        )
        self.assertEqual(depois.matrix.profile_cells(), excluidas)
        for familia, item in sorted(excluidas):
            with self.subTest(celula=f"{familia}/{item}"):
                self.assertFalse(self._pendente(depois, f"{familia}/{item}"))
        # A tentativa de remoção é CONTADA, não silenciada.
        self.assertTrue(rejeicoes)
        self.assertEqual(rejeicoes[0]["erro"], "MatrixProfileCellsReimposed")
        self.assertTrue(rejeicoes[0]["reimposicao"]["restored"])

    def test_caminho_do_store_com_celulas_de_perfil_continua_aceito(self):
        """ACEITE 3: escopo e estado acumulado NÃO são recusados pela regra."""
        data = _objective_dict()
        data["matrix"] = _matriz_com_exclusao_de_perfil().to_dict()
        rejeicoes = []
        index = I._objectives_index([data], rejeicoes=rejeicoes)
        self.assertEqual(rejeicoes, [], "objetivo do plano recusado por engano")
        self.assertIn("obj-1", index)
        self.assertTrue(index["obj-1"].matrix.profile_cells())

        # Estado acumulado (runtime.state) semeando o mesmo objetivo.
        semeado = I._seed_with_accumulated(
            index["obj-1"],
            {"contract": {"decisoes": {"content": "apurado na rodada anterior"}}},
            rejeicoes=rejeicoes,
        )
        self.assertEqual(rejeicoes, [])
        self.assertTrue(semeado.matrix.profile_cells())

    def test_objective_payload_da_tarefa_tambem_e_confiavel(self):
        data = _objective_dict()
        data["matrix"] = _matriz_com_exclusao_de_perfil().to_dict()
        rejeicoes = []
        objetivo = I._objective_of(
            data, {"objective_id": "obj-1"}, "obj-1", task_id="t1", rejeicoes=rejeicoes
        )
        self.assertEqual(rejeicoes, [])
        self.assertTrue(objetivo.matrix.profile_cells())


# --------------------------------------------------------------------------
# F4 — payload MISTO: uma linha copiada não pode custar a rodada inteira
# --------------------------------------------------------------------------


class MatrizMistaTest(_IntegraCase):
    """Recusa por CÉLULA, não por matriz, quando a falha é atribuível a uma."""

    def _base(self):
        data = _objective_dict()
        data["matrix"] = _matriz_com_exclusao_de_perfil().to_dict()
        return InvestigationObjective.from_dict(data, trust_profile_cells=True)

    @staticmethod
    def _eco_de_perfil(objetivo):
        """Uma célula de perfil da matriz anterior, copiada IDÊNTICA."""
        familia, item = sorted(objetivo.matrix.profile_cells())[0]
        return objetivo.matrix.cells[(familia, item)].to_dict(), (familia, item)

    @staticmethod
    def _covered_legitima():
        return {
            "family": "entrada",
            "item": "ausente",
            "state": "covered",
            "justification": {"path": "rules.py", "line_start": 1, "line_end": 4},
            "note": "entrada ausente cai no ramo de 409",
            "marked": True,
        }

    def _aplicar(self, celulas, base=None):
        base = base if base is not None else self._base()
        rejeicoes = []
        depois, _ = I._objective_after_result(
            base, {"matrix": {"cells": celulas}}, None, task_id="t7", rejeicoes=rejeicoes
        )
        return base, depois, rejeicoes

    def test_payload_misto_preserva_a_covered_nova_e_a_celula_de_perfil(self):
        """ACEITE 1: eco inócuo + célula nova legítima ⇒ as duas sobrevivem."""
        base = self._base()
        eco, chave = self._eco_de_perfil(base)
        _, depois, rejeicoes = self._aplicar([eco, self._covered_legitima()], base)

        nova = depois.matrix.cells.get(("entrada", "ausente"))
        self.assertIsNotNone(nova, "a célula nova sumiu com o fallback de matriz")
        self.assertEqual(nova.state.value, "covered")
        # A célula de perfil ecoada continua valendo, idêntica.
        self.assertIn(chave, depois.matrix.profile_cells())
        self.assertEqual(
            depois.matrix.cells[chave].to_dict(), base.matrix.cells[chave].to_dict()
        )
        # Eco idêntico não é tentativa de nada: não vira recusa de célula.
        rotulo = f"{chave[0]}/{chave[1]}"
        for recusa in rejeicoes:
            with self.subTest(erro=recusa["erro"]):
                self.assertNotIn(rotulo, recusa.get("celulas", ()))
                self.assertNotIn(
                    rotulo, recusa.get("reimposicao", {}).get("restored", ())
                )
                self.assertNotEqual(recusa["descartado"], "matrix")

    def test_forja_isolada_e_revertida_e_o_resto_da_matriz_e_aceito(self):
        """ACEITE 2: a célula forjada cai sozinha; a legítima da mesma rodada fica."""
        forjada = {
            "family": "protocolo",
            "item": "timeout",
            "state": "not_applicable",
            "justification": None,
            "justification_source": "profile:inventado",
            "note": "nao se aplica",
            "marked": True,
        }
        base, depois, rejeicoes = self._aplicar([forjada, self._covered_legitima()])

        self.assertEqual(depois.matrix.cells[("entrada", "ausente")].state.value, "covered")
        revertida = depois.matrix.cells.get(("protocolo", "timeout"))
        if revertida is not None:
            self.assertEqual(revertida.justification_source, "")
            self.assertEqual(revertida.state.value, "unresolved")
            self.assertFalse(revertida.marked)
        # Toda recusa é de CÉLULA — a matriz do agente não caiu.
        self.assertEqual({r["descartado"] for r in rejeicoes}, {"celula"}, rejeicoes)
        forja = [r for r in rejeicoes if r["erro"] == "MatrixIntegrityError"]
        self.assertEqual(len(forja), 1, rejeicoes)
        self.assertEqual(forja[0]["celulas"], ["protocolo/timeout"])

    def test_matriz_estruturalmente_invalida_ainda_cai_como_matrix(self):
        """ACEITE 3: erro NÃO atribuível a célula continua derrubando a matriz.

        Família inventada, sem proveniência e sem evidência: o saneamento não
        tem o que reverter (não há `justification_source`), então a recusa é do
        schema — e o custo é a matriz inteira, com o fallback da anterior.
        """
        base, depois, rejeicoes = self._aplicar(
            [
                {
                    "family": "resiliencia_regional",
                    "item": "failover_cross_region",
                    "state": "not_applicable",
                },
                self._covered_legitima(),
            ]
        )
        self.assertEqual([r["descartado"] for r in rejeicoes], ["matrix"])
        self.assertEqual(rejeicoes[0]["erro"], "MatrixIntegrityError")
        # Fallback: a matriz é a ANTERIOR, então a `covered` da rodada não entrou.
        self.assertEqual(
            depois.matrix.cells.get(("entrada", "ausente")),
            base.matrix.cells.get(("entrada", "ausente")),
        )
        self.assertTrue(depois.matrix.profile_cells())


if __name__ == "__main__":
    unittest.main()
