"""Integração de resultados de objetivo de DESCOBERTA (módulo sem extrator).

Tudo aqui passa pelo caminho REAL (`integrate()` sobre um `Repository` de
verdade e um `Snapshot` capturado de um repositório em disco) — nunca pela
função interna isolada.

| Aceite                                                                  | Teste |
|-------------------------------------------------------------------------|-------|
| Claim com evidência resolvida é aceito e conta progresso                 | `test_claim_com_evidencia_resolvida_*` |
| Claim com evidência inexistente é RECUSADO com diagnóstico tipado        | `test_claim_com_evidencia_inexistente_*` |
| Descoberta nunca chega a `supported`                                     | `test_descoberta_nunca_vira_supported` |
| Entidade de descoberta: lifecycle não confirmado + proveniência do agente | `EntidadeDeDescobertaTest` |
| Sem evidência resolvida ⇒ nenhuma entidade                               | `test_sem_evidencia_resolvida_nao_cria_entidade` |
| Evidência em documento (.md) não sustenta                                | `EvidenciaSoCodigoTest` |
| Faixa só de comentário/docstring não sustenta (por família de linguagem) | `ComentarioNaoEEvidenciaTest` |
| Distinção da regra F10 de `ingestion.correlate`                          | `test_distincao_da_regra_f10_de_correlate` |
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
    ObjectiveKind,
)
from analysis.snapshot import capture
from ingestion import correlate as CORRELATE
from knowledge import integrate as I
from knowledge.models import EntityType, EpistemicStatus, LifecycleStatus
from knowledge.repository import Repository
from runtime import state as rt_state
from runtime import tasks as rt_tasks


#: Módulo Go: o extrator estrutural não o cobre, então a leitura do fonte pelo
#: modelo é a única origem de conhecimento — exatamente o caso `discovery`.
GO_SRC = """package billing

import "errors"

var ErrLimite = errors.New("limite excedido")

// Aprovar decide sobre o pedido.
func Aprovar(total int) (int, error) {
\tif total > 1000 {
\t\treturn 0, ErrLimite
\t}
\treturn 200, nil
}
"""

DOC_SRC = """# Regras de faturamento

Quando o total passa de 1000 o pedido e recusado.
"""

#: Arquivo Go cujo trecho citado e SO comentario.
GO_COMENTADO = """package billing

// Este bloco explica a regra de limite.
// Nada aqui executa.

func Nada() {}
"""

#: Mesmo comportamento em Python — a unica linguagem para a qual
#: `analysis.verification` tem gramatica completa (AST) e, portanto, a unica em
#: que um claim pode chegar a `supported`. Serve de CONTROLE: prova que o teto
#: `inferred` da descoberta vem do KIND do objetivo, nao de uma incapacidade do
#: claim.
PY_SRC = """def aprovar(total):
    if total > 1000:
        return 409
    return 200
"""


def _extraction_of(snapshot):
    registry = default_registry()
    files = []
    for entry in snapshot.files:
        with open(os.path.join(snapshot.repo, entry.path), "r", encoding="utf-8") as fh:
            files.append(SourceFile(path=entry.path, content=fh.read()))
    return registry.extract_all(files)


def _contract_dict():
    return {
        n: ContractField(name=n, label=CONTRACT_LABELS[n]).to_dict() for n in CONTRACT_FIELDS
    }


class _Base(unittest.TestCase):
    """Repositório em disco + snapshot real; `integrate()` de ponta a ponta."""

    ARQUIVOS = {
        "billing.go": GO_SRC,
        "REGRAS.md": DOC_SRC,
        "comentado.go": GO_COMENTADO,
        "regras.py": PY_SRC,
    }
    ASSERTED_BY = "llm:agente-descoberta"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="descoberta-know-")
        self.repo_dir = os.path.join(self.tmpdir, "repo")
        os.makedirs(self.repo_dir, exist_ok=True)
        for name, content in self.ARQUIVOS.items():
            with open(os.path.join(self.repo_dir, name), "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
        self.snapshot = capture(self.repo_dir)
        self.extraction = _extraction_of(self.snapshot)
        self.repo = Repository.open(os.path.join(self.tmpdir, "knowledge.db"))

    def tearDown(self):
        self.repo.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- montagem -----------------------------------------------------------

    def _objective_dict(self, kind=ObjectiveKind.DISCOVERY.value):
        return {
            "objective_id": "discovery:billing",
            "kind": kind,
            "capability_id": "",
            "name": "discovery@billing",
            "contract": _contract_dict(),
            "reading_needs": [],
            "analysis_directives": ["evidencia so em codigo executavel"],
            "state": "partial",
        }

    def _result(self, contract, *, objective_id="discovery:billing"):
        versions = {"snapshot_id": self.snapshot.snapshot_id, "source_version_ids": []}
        return I.AcceptedResult(
            task_id="t-descoberta",
            kind="investigation",
            objective_id=objective_id,
            capability_id="",
            execution_id="exec-descoberta",
            input_versions=versions,
            input_versions_hash=rt_tasks.input_versions_hash(versions),
            output={"objective_id": objective_id, "contract": contract},
            objective_payload={"objective_id": objective_id},
            asserted_by=self.ASSERTED_BY,
            created_at="2026-09-09T00:00:00Z",
        )

    def _integrate(self, contract, *, kind=ObjectiveKind.DISCOVERY.value):
        report = I.integrate(
            self.repo,
            None,
            self.snapshot,
            self.extraction,
            "code/billing",
            objectives=[self._objective_dict(kind)],
            results=[self._result(contract)],
        )
        self.assertEqual(len(report.objetivos), 1, report.to_dict())
        return report, report.objetivos[0]

    @staticmethod
    def _afirmacao(statement, *, regra=None, path=None, start=None, end=None):
        item = {"statement": statement}
        if regra:
            item["regra"] = regra
        if path is not None:
            item["evidence_refs"] = [
                {"path": path, "line_start": start, "line_end": end}
            ]
        return item


class ClaimComEvidenciaTest(_Base):
    """(a) evidência resolvida ⇒ aceita e conta progresso; não resolvida ⇒ recusa."""

    def test_claim_com_evidencia_resolvida_e_aceito_e_conta_progresso(self):
        contract = {
            "decisoes": {
                "status": "filled",
                "afirmacoes": [
                    self._afirmacao(
                        "Quando total > 1000 o pedido e recusado com ErrLimite.",
                        regra="RN-LIMITE Limite de aprovacao",
                        path="billing.go",
                        start=8,
                        end=12,
                    )
                ],
            }
        }
        report, outcome = self._integrate(contract)
        dados = outcome.to_dict()
        self.assertEqual(dados["kind"], "discovery")
        self.assertEqual(dados["claims_com_evidencia_resolvida"], 1)
        self.assertEqual(dados["claims_sem_evidencia"], 0)
        self.assertEqual(dados["rejeitados"], [])
        self.assertEqual(len(outcome.fatos), 1, dados)

        # O mesmo outcome, aplicado como delta, PROGRIDE por EVIDÊNCIA ACEITA —
        # mesmo sem nenhum campo textual novo. Era exatamente esta a rodada que
        # virava `no_progress`: leu o arquivo, citou código real, e a cadeia
        # parava porque o contrato não ganhou texto (§7.3).
        _, progresso = rt_state.apply_result(self._estado(), dados)
        self.assertTrue(progresso.evidence_accepted, progresso.to_dict())
        self.assertTrue(progresso.has_progress)

    def _estado(self):
        return rt_state.new_state(
            repo_id="code/billing", objective_id="discovery:billing", input_revision="r1"
        )

    def test_campo_com_conteudo_progride_por_contract_fields_filled(self):
        """O outro eixo de progresso do §7.3: campo do §6.3 que saiu de vazio."""
        contract = {
            "decisoes": {
                "status": "filled",
                "content": "Quando total > 1000 o pedido e recusado com ErrLimite.",
                "evidence_refs": [
                    {"path": "billing.go", "line_start": 8, "line_end": 12}
                ],
            }
        }
        _, outcome = self._integrate(contract)
        _, progresso = rt_state.apply_result(self._estado(), outcome.to_dict())
        self.assertIn("decisoes", progresso.contract_fields_filled)
        self.assertTrue(progresso.has_progress)

    def test_rodada_sem_evidencia_e_sem_campo_novo_e_no_progress(self):
        """`no_progress` SÓ quando nada disso ocorre — o outro lado da regra."""
        contract = {
            "decisoes": {
                "status": "filled",
                "afirmacoes": [
                    self._afirmacao(
                        "Existe uma regra de limite.", path="billing.go", start=900, end=950
                    )
                ],
            }
        }
        _, outcome = self._integrate(contract)
        dados = outcome.to_dict()
        self.assertEqual(dados["evidence_refs"], [], "citação recusada não é evidência")
        _, progresso = rt_state.apply_result(self._estado(), dados)
        self.assertFalse(progresso.has_progress, progresso.to_dict())

    def test_claim_com_evidencia_inexistente_e_rejeitado_com_diagnostico(self):
        """Faixa além do fim do arquivo: recusa TIPADA, com path e linhas."""
        contract = {
            "decisoes": {
                "status": "filled",
                "afirmacoes": [
                    self._afirmacao(
                        "Quando total > 1000 o pedido e recusado.",
                        path="billing.go",
                        start=900,
                        end=950,
                    )
                ],
            }
        }
        _, outcome = self._integrate(contract)
        self.assertEqual(outcome.fatos, [], "claim recusado nao pode virar fato")
        self.assertEqual(len(outcome.rejeitados), 1, outcome.to_dict())
        rejeicao = outcome.rejeitados[0]
        self.assertEqual(rejeicao["tipo"], I.REJECT_EVIDENCE_UNRESOLVED)
        evidencia = rejeicao["evidencias"][0]
        self.assertEqual(evidencia["path"], "billing.go")
        self.assertEqual((evidencia["line_start"], evidencia["line_end"]), (900, 950))

    def test_citacao_para_arquivo_fora_do_snapshot_tem_tipo_proprio(self):
        contract = {
            "decisoes": {
                "status": "filled",
                "afirmacoes": [
                    self._afirmacao(
                        "Regra vinda de outro repositorio.",
                        path="outro-repo/regra.go",
                        start=1,
                        end=2,
                    )
                ],
            }
        }
        _, outcome = self._integrate(contract)
        self.assertEqual(outcome.fatos, [])
        self.assertEqual(
            outcome.rejeitados[0]["tipo"], I.REJECT_EVIDENCE_OUT_OF_SNAPSHOT
        )

    def test_claim_sem_citacao_em_descoberta_e_recusado(self):
        """Descoberta sem path/linhas é leitura livre do modelo: não entra."""
        contract = {
            "decisoes": {
                "status": "filled",
                "afirmacoes": [self._afirmacao("Existe um limite de aprovacao.")],
            }
        }
        _, outcome = self._integrate(contract)
        self.assertEqual(outcome.fatos, [])
        self.assertEqual(outcome.to_dict()["claims_sem_evidencia"], 1)
        self.assertEqual(outcome.rejeitados[0]["tipo"], I.REJECT_EVIDENCE_UNRESOLVED)

    def test_claim_sem_citacao_em_objetivo_de_capacidade_continua_lacuna(self):
        """Compat: fora da descoberta, ausência de prova é lacuna DECLARADA."""
        contract = {
            "decisoes": {
                "status": "filled",
                "afirmacoes": [self._afirmacao("Existe um limite de aprovacao.")],
            }
        }
        _, outcome = self._integrate(contract, kind=ObjectiveKind.CAPABILITY.value)
        self.assertEqual(len(outcome.fatos), 1, outcome.to_dict())
        self.assertEqual(outcome.fatos[0].epistemic, EpistemicStatus.UNRESOLVED.value)


class DescobertaNuncaSupportedTest(_Base):
    """(b) sem gramática não há verificação mecânica: teto `inferred`."""

    #: Claim VERIFICAVEL MECANICAMENTE (Python, condicao e efeito no mesmo
    #: ramo): em objetivo de capacidade sai `supported`. E o par exato de que
    #: o teto da descoberta precisa para ser provado.
    CONTRATO = {
        "decisoes": {
            "status": "filled",
            "afirmacoes": [
                {
                    "statement": "Quando total > 1000, o pedido retorna HTTP 409.",
                    "regra": "RN-LIMITE Limite de aprovacao",
                    "evidence_refs": [
                        {"path": "regras.py", "line_start": 1, "line_end": 4}
                    ],
                }
            ],
        }
    }

    def test_descoberta_nunca_vira_supported(self):
        _, outcome = self._integrate(self.CONTRATO)
        self.assertEqual(len(outcome.fatos), 1, outcome.to_dict())
        fato = outcome.fatos[0]
        self.assertNotEqual(fato.epistemic, EpistemicStatus.SUPPORTED.value)
        self.assertIn(
            fato.epistemic,
            (EpistemicStatus.INFERRED.value, EpistemicStatus.UNRESOLVED.value),
            "descoberta usa os estados que o modelo de conhecimento ja tem",
        )
        self.assertNotEqual(fato.nature, "implemented")
        self.assertEqual(outcome.to_dict()["supported"], 0)

    def test_o_mesmo_claim_em_objetivo_de_capacidade_pode_ser_supported(self):
        """Controle: o teto é do KIND, não uma incapacidade do claim."""
        _, outcome = self._integrate(self.CONTRATO, kind=ObjectiveKind.CAPABILITY.value)
        self.assertEqual(
            outcome.fatos[0].epistemic,
            EpistemicStatus.SUPPORTED.value,
            "sem o teto de descoberta, o MESMO claim é verificável mecanicamente",
        )


class EntidadeDeDescobertaTest(_Base):
    """(c) entidade descoberta: `proposed` + proveniência do agente + lastro."""

    CONTRATO = {
        "decisoes": {
            "status": "filled",
            "afirmacoes": [
                {
                    "regra": "RN-LIMITE Limite de aprovacao",
                    "statement": "Quando total > 1000 o pedido e recusado com ErrLimite.",
                    "evidence_refs": [
                        {"path": "billing.go", "line_start": 8, "line_end": 12}
                    ],
                }
            ],
        }
    }

    def test_capacidade_descoberta_nasce_proposed_com_asserted_by_do_agente(self):
        _, outcome = self._integrate(self.CONTRATO)
        capacidade = [
            e for e in outcome.entidades if e["tipo"] == EntityType.CAPABILITY.value
        ]
        self.assertEqual(len(capacidade), 1, outcome.entidades)
        self.assertEqual(capacidade[0]["lifecycle"], LifecycleStatus.PROPOSED.value)
        self.assertEqual(capacidade[0]["asserted_by"], self.ASSERTED_BY)
        entidade = self.repo.get_entity(capacidade[0]["entity_id"], lifecycle=None)
        self.assertIs(entidade.lifecycle_status, LifecycleStatus.PROPOSED)
        self.assertEqual(entidade.attributes["asserted_by"], self.ASSERTED_BY)
        self.assertEqual(entidade.attributes["origem"], "descoberta")

    def test_regra_descoberta_nasce_proposed_com_evidencia_resolvida(self):
        _, outcome = self._integrate(self.CONTRATO)
        regras = [
            e for e in outcome.entidades if e["tipo"] == EntityType.BUSINESS_RULE.value
        ]
        self.assertEqual(len(regras), 1, outcome.entidades)
        self.assertEqual(regras[0]["lifecycle"], LifecycleStatus.PROPOSED.value)
        self.assertEqual(regras[0]["asserted_by"], self.ASSERTED_BY)
        entidade = self.repo.get_entity(regras[0]["entity_id"], lifecycle=None)
        self.assertIs(entidade.lifecycle_status, LifecycleStatus.PROPOSED)

    def test_relacao_implements_respeita_check_support(self):
        """Quem afirma (agente) nunca registra a própria sustentação (§5.3)."""
        _, outcome = self._integrate(self.CONTRATO)
        relacoes = [r for r in outcome.relacoes if r["tipo"] == "implements"]
        self.assertEqual(len(relacoes), 1, outcome.relacoes)
        relacao = self.repo.get_relation(relacoes[0]["relation_id"], lifecycle=None)
        self.assertIsNot(relacao.epistemic_status, EpistemicStatus.SUPPORTED)
        self.assertIsNone(relacao.support_recorded_by)
        self.assertEqual(relacao.asserted_by, self.ASSERTED_BY)

    def test_sem_evidencia_resolvida_nao_cria_entidade(self):
        """ACEITE: entidade de descoberta EXIGE citação resolvida."""
        contract = {
            "decisoes": {
                "status": "filled",
                "afirmacoes": [
                    {
                        "regra": "RN-INVENTADA",
                        "statement": "Existe um limite de aprovacao.",
                        "evidence_refs": [
                            {"path": "billing.go", "line_start": 900, "line_end": 950}
                        ],
                    }
                ],
            }
        }
        _, outcome = self._integrate(contract)
        self.assertEqual(outcome.entidades, [], "sem lastro, nada e criado")
        self.assertIsNone(outcome.subject_id)
        self.assertEqual(outcome.fatos, [])
        self.assertTrue(
            any("F10" in b or "§5.4" in b for b in outcome.bloqueios), outcome.bloqueios
        )

    def test_distincao_da_regra_f10_de_correlate(self):
        """A regra de `ingestion.correlate` continua valendo — e é OUTRA.

        Lá a origem é o TEXTO de um documento e `Capability` nunca é criável,
        com ou sem citação. Aqui a origem é análise do próprio código, e o que
        autoriza a criação é a citação RESOLVIDA no snapshot: por isso a
        capacidade nasce (`proposed`), e sem citação não nasce.
        """
        self.assertNotIn(
            EntityType.CAPABILITY,
            CORRELATE.CREATABLE_TYPES,
            "texto de documento nunca cria Capability (F10/§5.4)",
        )
        _, outcome = self._integrate(self.CONTRATO)
        self.assertTrue(
            [e for e in outcome.entidades if e["tipo"] == EntityType.CAPABILITY.value],
            "analise de codigo COM citacao resolvida cria a capacidade",
        )


class EvidenciaSoCodigoTest(_Base):
    """Documento nunca sustenta afirmação sobre o sistema (§5.4, F10)."""

    def test_citacao_para_markdown_e_rejeitada_como_nao_codigo(self):
        contract = {
            "decisoes": {
                "status": "filled",
                "afirmacoes": [
                    self._afirmacao(
                        "Quando total > 1000 o pedido e recusado.",
                        path="REGRAS.md",
                        start=1,
                        end=3,
                    )
                ],
            }
        }
        _, outcome = self._integrate(contract)
        self.assertEqual(outcome.fatos, [])
        self.assertEqual(outcome.rejeitados[0]["tipo"], I.REJECT_EVIDENCE_NOT_CODE)
        self.assertEqual(outcome.rejeitados[0]["evidencias"][0]["path"], "REGRAS.md")

    def test_extensoes_de_documento_cobrem_as_do_inventario(self):
        """A lista local não pode ser MENOR que a que a análise já classifica."""
        from analysis import inventory as INV

        faltando = set(INV._DOC_EXTENSIONS) - set(I.DOC_EXTENSIONS)
        self.assertEqual(faltando, set(), f"extensões de documento não cobertas: {faltando}")


class ComentarioNaoEEvidenciaTest(_Base):
    """Faixa só de comentário/docstring não sustenta — em qualquer linguagem."""

    def test_faixa_so_de_comentario_em_go_e_rejeitada(self):
        contract = {
            "decisoes": {
                "status": "filled",
                "afirmacoes": [
                    self._afirmacao(
                        "Existe uma regra de limite.",
                        path="comentado.go",
                        start=3,
                        end=4,
                    )
                ],
            }
        }
        _, outcome = self._integrate(contract)
        self.assertEqual(outcome.fatos, [])
        self.assertEqual(outcome.rejeitados[0]["tipo"], I.REJECT_EVIDENCE_NOT_CODE)
        self.assertIn("comentario", outcome.rejeitados[0]["motivo"])

    def test_faixa_com_codigo_ao_lado_do_comentario_e_aceita(self):
        """Controle: comentário JUNTO de código não invalida a citação."""
        contract = {
            "decisoes": {
                "status": "filled",
                "afirmacoes": [
                    self._afirmacao(
                        "Quando total > 1000 o pedido e recusado.",
                        path="billing.go",
                        start=7,
                        end=12,
                    )
                ],
            }
        }
        _, outcome = self._integrate(contract)
        self.assertEqual(outcome.rejeitados, [], outcome.to_dict())
        self.assertEqual(len(outcome.fatos), 1)

    def test_is_comment_only_por_familia_de_linguagem(self):
        """Uma família por linha — mainframe, SQL, C-like, Python, shell, XML."""
        casos = [
            ("java", "// so comentario\n/* bloco */\n", True),
            ("java", "// comentario\nint x = 1;\n", False),
            ("go", "/*\n multi\n linha\n*/\n", True),
            ("go", 'url := "http://x" // nao e comentario\n', False),
            ("rs", "// rust\n", True),
            ("rs", "fn main() { let x = 1; }\n", False),
            ("cs", "// c#\n", True),
            ("kt", "// kotlin\n", True),
            ("python", '"""docstring isolada"""\n', True),
            ("python", "# nada\n\n", True),
            ("python", 'x = """valor"""\n', False),
            ("sh", "# shell\n", True),
            ("sh", "echo ok\n", False),
            ("sql", "-- comentario\n/* outro */\n", True),
            ("sql", "SELECT '--' FROM dual;\n", False),
            ("sybase", "-- t-sql\n", True),
            ("cobol", "000100* ESTE E COMENTARIO\n000200*   OUTRO\n", True),
            ("cobol", "000100 MOVE A TO B.\n", False),
            ("cobol", "      *> comentario livre\n", True),
            ("jcl", "//* comentario jcl\n/*\n", True),
            ("jcl", "//STEP1 EXEC PGM=IEFBR14\n", False),
            ("xml", "<!-- so comentario -->\n", True),
            ("desconhecida.zzz", "// x\n# y\n-- z\n", True),
            ("desconhecida.zzz", "FOO BAR\n", False),
            ("java", "   \n\n", True),
        ]
        for language, texto, esperado in casos:
            with self.subTest(language=language, texto=texto[:24]):
                self.assertEqual(
                    I.is_comment_only(texto, language),
                    esperado,
                    f"{language}: {texto!r}",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
