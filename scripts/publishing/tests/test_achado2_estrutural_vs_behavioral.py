"""Regressao do achado #2 (BLOQUEANTE) da 2a auditoria externa — Onda10-B.

O defeito auditado: o Word de CONTRATO declarou "completo: todas as unidades
publicadas tem condicao, comportamento ou excecao sustentada por evidencia"
quando o unico fato do contrato era ESTRUTURAL (`implemented = true`, com
evidencia executavel real emitida por `wk code`). 404/422/409 e mudanca de
estado estavam ausentes. No MESMO conjunto, a capacidade que expoe esse
contrato saiu `parcial`.

Aceites cobertos aqui:

1. conjunto com capacidade `parcial` -> o documento do contrato relacionado
   NUNCA sai `completo`, e a frase de completude nao afirma comportamento
   sustentado quando so ha `implemented = true`;
2. unidade com fato estrutural evidenciado -> `content_grade` structural;
3. unidade com regra real (condicao + efeito + evidencia) -> behavioral, e o
   documento pode ser completo quando as obrigacoes fecham.

Sem rede, sem LLM, sem commit: knowledge.db em tempfile.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unicodedata
import unittest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "..")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from knowledge import evidence as ev_mod
from knowledge.models import (
    Alias,
    AliasOrigin,
    ContentKind,
    EntityDraft,
    EntityType,
    EpistemicStatus,
    FactDraft,
    FactNature,
    LifecycleStatus,
    RelationDraft,
    RelationType,
    SourceKind,
)
from knowledge.repository import Repository

from publishing.document import (
    AnalysisState,
    ContentGrade,
    DocKind,
    Statement,
    UnitState,
    has_behavioral_content,
    is_behavioral_predicate,
    is_behavioral_statement,
    is_structural_predicate,
)
from publishing.planner import plan as plan_fn


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _statement(predicate: str, value: str, evidence=("EV-1",)) -> Statement:
    return Statement(
        fact_id="F-1",
        predicate=predicate,
        value=value,
        scope="src/api/rules.py",
        state=UnitState.IMPLEMENTED,
        nature=FactNature.IMPLEMENTED,
        epistemic_status=EpistemicStatus.SUPPORTED,
        lifecycle_status=LifecycleStatus.CURRENT,
        revision_id="rev-1",
        evidence_ids=tuple(evidence),
    )


# ---------------------------------------------------------------------------
# Aceite 2 e 3, no nivel do Statement (a raiz do achado)
# ---------------------------------------------------------------------------


class TestPredicadoEstruturalNaoEComportamento(unittest.TestCase):
    def test_implemented_true_com_evidencia_nao_e_behavioral(self):
        st = _statement("implemented", "true")
        self.assertTrue(st.evidence_ids, "cenario invalido: o fato precisa TER evidencia")
        self.assertTrue(is_structural_predicate("implemented"))
        self.assertFalse(has_behavioral_content("true"))
        self.assertFalse(is_behavioral_statement(st))

    def test_predicado_qualificado_implemented_e_estrutural(self):
        for pred in ("capability.implemented", "contract.implemented", "cap.exists"):
            with self.subTest(pred=pred):
                self.assertTrue(is_structural_predicate(pred))
                self.assertFalse(is_behavioral_statement(_statement(pred, "true")))

    def test_investigacao_identidade_gatilho_dependencias_sao_estruturais(self):
        for pred in (
            "investigacao.identidade",
            "investigacao.gatilho",
            "investigacao.dependencias",
        ):
            with self.subTest(pred=pred):
                self.assertTrue(is_structural_predicate(pred))
                self.assertFalse(is_behavioral_predicate(pred))

    def test_espelho_de_relacao_e_estrutural(self):
        for pred in ("contains", "calls", "depends_on", "consumes"):
            with self.subTest(pred=pred):
                self.assertTrue(is_structural_predicate(pred))

    def test_flag_booleana_nunca_tem_conteudo_comportamental(self):
        for value in ("true", "false", "sim", "nao", "1", "0"):
            with self.subTest(value=value):
                self.assertFalse(has_behavioral_content(value))

    def test_regra_real_com_condicao_efeito_e_evidencia_e_behavioral(self):
        st = _statement("rule", "quando total > 1000 entao retorna 409 e nao grava o pedido")
        self.assertTrue(is_behavioral_predicate("rule"))
        self.assertTrue(has_behavioral_content(st.value))
        self.assertTrue(is_behavioral_statement(st))

    def test_regra_real_sem_evidencia_nao_e_behavioral(self):
        st = _statement(
            "rule", "quando total > 1000 entao retorna 409 e nao grava o pedido", evidence=()
        )
        self.assertFalse(is_behavioral_statement(st))


# ---------------------------------------------------------------------------
# Aceite 1 e 3, no nivel do plano: capacidade parcial + contrato
# ---------------------------------------------------------------------------

NS_PARCIAL = "acme/achado2-parcial"
NS_COMPLETO = "acme/achado2-completo"


def _seed_parcial(repo):
    """Capacidade com UMA regra real + contrato so com `implemented = true`."""
    src = repo.register_source(NS_PARCIAL, SourceKind.CODE, "git://app")
    ver = repo.register_source_version(src, "commit:achado2", "cd" * 32)
    with repo.revision(author="pipeline:codescan", reason="cenario achado #2") as rev:
        def evidence(path, start, end, digest):
            return rev.add_evidence(
                ev_mod.make_evidence(
                    NS_PARCIAL,
                    SourceKind.CODE,
                    ContentKind.EXECUTABLE,
                    ver.source_version_id,
                    {
                        "repo": "app",
                        "commit": "achado2",
                        "path": path,
                        "start_line": start,
                        "end_line": end,
                        "snippet_hash": digest,
                    },
                )
            )

        ev_entry = evidence("src/api/orders.py", 10, 14, "hA")
        ev_rule = evidence("src/api/rules.py", 40, 52, "hB")

        def ent(etype, key, title):
            return rev.put_entity(
                EntityDraft(
                    NS_PARCIAL,
                    etype,
                    key,
                    title,
                    source_version_id=ver.source_version_id,
                    aliases=(Alias(key, AliasOrigin.METADATA_ID),),
                    evidence_refs=(ev_entry,),
                )
            ).target_id

        cap = ent(EntityType.CAPABILITY, "CAP-900", "CAP-900 Aprovacao de pedido")
        rn = ent(EntityType.BUSINESS_RULE, "RN-900", "RN-900 Limite de valor para aprovacao")
        ctr = ent(EntityType.CONTRACT, "CTR-900", "POST /orders/{id}/approve")

        for source, rtype, target in (
            (cap, RelationType.CONTAINS, rn),
            (cap, RelationType.PUBLISHES, ctr),
        ):
            rev.put_relation(
                RelationDraft(
                    NS_PARCIAL,
                    source,
                    rtype,
                    target,
                    scope="src/api",
                    epistemic_status=EpistemicStatus.SUPPORTED,
                    lifecycle_status=LifecycleStatus.CURRENT,
                    asserted_by="extractor:codescan",
                    evidence_refs=(ev_entry,),
                    support_recorded_by="pipeline:verify-static",
                    source_version_id=ver.source_version_id,
                )
            )

        def fact(subject, pred, value, evid, scope):
            rev.put_fact(
                FactDraft(
                    NS_PARCIAL,
                    subject,
                    pred,
                    value,
                    scope,
                    FactNature.IMPLEMENTED,
                    EpistemicStatus.SUPPORTED,
                    LifecycleStatus.CURRENT,
                    asserted_by="extractor:codescan",
                    evidence_refs=(evid,),
                    source_version_id=ver.source_version_id,
                    support_recorded_by="pipeline:verify-static",
                )
            )

        # Exatamente o que `wk code` emite (scripts/wk/cli.py:5269 e :5278).
        fact(ctr, "implemented", "true", ev_entry, "orders.approve")
        fact(cap, "implemented", "true", ev_entry, "CAP-900")
        # A unica regra REAL do conjunto vive na capacidade.
        fact(
            rn,
            "rule",
            "quando total > 1000 entao retorna 409 e nao grava o pedido",
            ev_rule,
            "src/api/rules.py",
        )


def _seed_completo(repo):
    """Capacidade e contrato ambos com regra real (condicao + efeito + evidencia)."""
    src = repo.register_source(NS_COMPLETO, SourceKind.CODE, "git://app2")
    ver = repo.register_source_version(src, "commit:ok", "ef" * 32)
    with repo.revision(author="pipeline:codescan", reason="cenario completo") as rev:
        ev = rev.add_evidence(
            ev_mod.make_evidence(
                NS_COMPLETO,
                SourceKind.CODE,
                ContentKind.EXECUTABLE,
                ver.source_version_id,
                {
                    "repo": "app2",
                    "commit": "ok",
                    "path": "src/pay.py",
                    "start_line": 1,
                    "end_line": 20,
                    "snippet_hash": "hZ",
                },
            )
        )

        def ent(etype, key, title):
            return rev.put_entity(
                EntityDraft(
                    NS_COMPLETO,
                    etype,
                    key,
                    title,
                    source_version_id=ver.source_version_id,
                    aliases=(Alias(key, AliasOrigin.METADATA_ID),),
                    evidence_refs=(ev,),
                )
            ).target_id

        cap = ent(EntityType.CAPABILITY, "CAP-901", "CAP-901 Captura de pagamento")
        ctr = ent(EntityType.CONTRACT, "CTR-901", "POST /payments/capture")
        rev.put_relation(
            RelationDraft(
                NS_COMPLETO,
                cap,
                RelationType.PUBLISHES,
                ctr,
                scope="src/pay.py",
                epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by="extractor:codescan",
                evidence_refs=(ev,),
                support_recorded_by="pipeline:verify-static",
                source_version_id=ver.source_version_id,
            )
        )

        def fact(subject, pred, value):
            rev.put_fact(
                FactDraft(
                    NS_COMPLETO,
                    subject,
                    pred,
                    value,
                    "src/pay.py",
                    FactNature.IMPLEMENTED,
                    EpistemicStatus.SUPPORTED,
                    LifecycleStatus.CURRENT,
                    asserted_by="extractor:codescan",
                    evidence_refs=(ev,),
                    source_version_id=ver.source_version_id,
                    support_recorded_by="pipeline:verify-static",
                )
            )

        fact(
            cap,
            "rule",
            "quando o saldo e menor que o valor entao retorna 422 e nao grava a captura",
        )
        fact(
            ctr,
            "exception",
            "quando o id ja foi capturado entao retorna 409 e nao grava nada",
        )


def _last_revision(repo):
    return repo.conn.execute(
        "SELECT revision_id FROM revisions ORDER BY created_at DESC, revision_id DESC LIMIT 1"
    ).fetchone()[0]


class TestContratoNaoHerdaCompletudeQueACapacidadeNaoTem(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="achado2-")
        self.repo = Repository.open(os.path.join(self.tmpdir, "knowledge.db"))
        _seed_parcial(self.repo)
        self.plan = plan_fn(self.repo, _last_revision(self.repo), namespace=NS_PARCIAL)
        self.docs = {d.doc_kind: d for d in self.plan.documents}

    def tearDown(self):
        self.repo.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_capacidade_sai_parcial(self):
        cap_doc = self.docs[DocKind.CAPACIDADE]
        self.assertIs(cap_doc.effective_analysis_state(), AnalysisState.PARCIAL)

    def test_contrato_relacionado_nunca_sai_completo(self):
        ctr_doc = self.docs[DocKind.CONTRATO_DEPENDENCIA]
        self.assertIsNot(ctr_doc.effective_analysis_state(), AnalysisState.COMPLETO)
        self.assertIs(ctr_doc.effective_analysis_state(), AnalysisState.ESTRUTURAL)

    def test_unidade_do_contrato_e_structural_apesar_da_evidencia(self):
        ctr_doc = self.docs[DocKind.CONTRATO_DEPENDENCIA]
        own = ctr_doc.own_units()
        self.assertTrue(own)
        for unit in own:
            self.assertIs(unit.content_grade, ContentGrade.STRUCTURAL)
            # O criterio ANTIGO (so evidencia) daria behavioral — e essa
            # diferenca e literalmente o achado #2.
            self.assertTrue(unit.sustained_statements())
            self.assertFalse(unit.behavioral_statements())

    def test_frase_de_completude_nao_afirma_comportamento_sustentado(self):
        ctr_doc = self.docs[DocKind.CONTRATO_DEPENDENCIA]
        texto = _fold(" | ".join(ctr_doc.analysis_gaps))
        self.assertNotIn("todas as unidades publicadas tem condicao", texto)
        self.assertIn("apenas estrutura e contratos confirmados", texto)
        self.assertIn("comportamento nao analisado", texto)

    def test_completo_declarado_pela_investigacao_e_rebaixado(self):
        plan2 = plan_fn(
            self.repo,
            _last_revision(self.repo),
            namespace=NS_PARCIAL,
            investigation_states={"CTR-900": "completo", "chave-extra-sem-doc": "parcial"},
        )
        ctr_doc = [d for d in plan2.documents if d.doc_kind is DocKind.CONTRATO_DEPENDENCIA][0]
        self.assertIsNot(ctr_doc.effective_analysis_state(), AnalysisState.COMPLETO)
        texto = _fold(" | ".join(ctr_doc.analysis_gaps))
        self.assertIn("rebaixado", texto)

    def test_titulo_do_contrato_deixa_de_prometer_comportamento(self):
        ctr_doc = self.docs[DocKind.CONTRATO_DEPENDENCIA]
        self.assertFalse(ctr_doc.title_promises_behavior(), ctr_doc.title)


class TestContratoComRegraRealPodeSerCompleto(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="achado2-ok-")
        self.repo = Repository.open(os.path.join(self.tmpdir, "knowledge.db"))
        _seed_completo(self.repo)
        self.plan = plan_fn(self.repo, _last_revision(self.repo), namespace=NS_COMPLETO)
        self.docs = {d.doc_kind: d for d in self.plan.documents}

    def tearDown(self):
        self.repo.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_contrato_com_condicao_efeito_e_evidencia_sai_completo(self):
        ctr_doc = self.docs[DocKind.CONTRATO_DEPENDENCIA]
        self.assertIs(ctr_doc.effective_analysis_state(), AnalysisState.COMPLETO)
        self.assertTrue(ctr_doc.behavioral_units())

    def test_capacidade_com_regra_real_sai_completa(self):
        cap_doc = self.docs[DocKind.CAPACIDADE]
        self.assertIs(cap_doc.effective_analysis_state(), AnalysisState.COMPLETO)


if __name__ == "__main__":
    unittest.main()
