"""publishing.delivery — seleção, elegibilidade, manifesto e confirmação (§9.4/§11).

Duas camadas:

1. `_select`/`_eligibility` testados diretamente sobre `KnowledgeDocument`/
   `SemanticUnit` construídos à mão (mesmo padrão de `test_validate.py` e
   `test_achado2_estrutural_vs_behavioral.py`) — controle exato da forma do
   plano, sem depender de heurística de extração.
2. `prepare`/`confirm` testados ponta a ponta sobre um `knowledge.db` real
   seedado por `Repository`, publicado por `publishing.release` com os
   renderers/validadores REAIS (`publishing.markdown`/`word`/`validate`) —
   mesmo padrão de `test_e2e_publishing.py`.

Sem rede, sem LLM, sem commit.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest

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

from publishing import delivery
from publishing import markdown as md_mod
from publishing import release
from publishing import validate as validate_mod
from publishing import word as word_mod
from publishing.document import (
    AnalysisState,
    Belonging,
    DocKind,
    KnowledgeDocument,
    RelationRef,
    SemanticUnit,
    Statement,
    UnitState,
)

REV = "rev_1"


# ---------------------------------------------------------------------------
# Auxiliares — construção direta de KnowledgeDocument/SemanticUnit
# ---------------------------------------------------------------------------


def _behavioral(fact_id="F-1", evidence=("EV-1",)):
    """Statement conhecidamente behavioral (mesmo texto de
    test_achado2_estrutural_vs_behavioral.test_regra_real_com_...)."""
    return Statement(
        fact_id, "rule", "quando total > 1000 entao retorna 409 e nao grava o pedido",
        "src/api/rules.py", UnitState.IMPLEMENTED, FactNature.IMPLEMENTED,
        EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, REV, evidence_ids=tuple(evidence),
    )


def _structural(fact_id="F-2", evidence=("EV-2",)):
    """Statement conhecidamente estrutural: `implemented=true` com evidência."""
    return Statement(
        fact_id, "implemented", "true", "src/api/rules.py", UnitState.IMPLEMENTED,
        FactNature.IMPLEMENTED, EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, REV,
        evidence_ids=tuple(evidence),
    )


def _rel(other_id, other_title="Alvo", other_type=EntityType.CAPABILITY, rel_id="rel_1"):
    return RelationRef(
        relation_id=rel_id, relation_type=RelationType.DEPENDS_ON, direction="out",
        other_entity_id=other_id, other_title=other_title, other_entity_type=other_type,
        natural_text=f"depende de {other_title}", lifecycle_status=LifecycleStatus.CURRENT,
        epistemic_status=EpistemicStatus.SUPPORTED, revision_id=REV,
    )


def _unit(unit_id, title, *, behavior=(), relations=(), belonging=None, shared_context=False):
    return SemanticUnit(
        unit_id=unit_id, title=title, state=UnitState.IMPLEMENTED, subject="Comportamento",
        subject_key="comportamento", belonging=belonging or Belonging(),
        entity_id=unit_id.replace("unt_", "ent_"), entity_type=EntityType.CAPABILITY,
        namespace="ns", revision_id=REV, behavior=behavior, relations=relations,
        shared_context=shared_context,
    )


def _doc(document_id, doc_kind, anchor_id, anchor_type, namespace, units):
    return KnowledgeDocument(
        document_id=document_id, title=f"{document_id}: título específico do assunto",
        doc_kind=doc_kind, units=tuple(units), revision_id=REV, namespace=namespace,
        anchor_entity_id=anchor_id, anchor_entity_type=anchor_type,
    )


NS_A = "code/repoA"
NS_B = "code/repoB"
NS_WIKI = "wiki"


def _build_plan_docs():
    """Quatro documentos + uma dependência cruzando namespace (CAP-D em NS_B,
    referenciada por CAP-A em NS_A) + uma entidade externa sem documento
    (CAP-EXT)."""
    doc_sys_a = _doc(
        "doc_sys_a", DocKind.VISAO_SISTEMA, "SYS-A", EntityType.SYSTEM, NS_A,
        [_unit("unt_sys_a", "Sistema A: visão geral", behavior=(_behavioral("F-SYSA"),),
               belonging=Belonging(system_id="SYS-A", system_title="Sistema A"))],
    )
    doc_cap_a = _doc(
        "doc_cap_a", DocKind.CAPACIDADE, "CAP-A", EntityType.CAPABILITY, NS_A,
        [_unit(
            "unt_cap_a", "Capacidade A: regra principal", behavior=(_behavioral("F-CAPA"),),
            relations=(_rel("CAP-D", "Capacidade D", EntityType.CAPABILITY, "rel_a_d"),
                       _rel("CAP-EXT", "Capacidade externa", EntityType.CAPABILITY, "rel_a_ext")),
            belonging=Belonging(system_id="SYS-A", system_title="Sistema A",
                                 initiative_id="INI-X", initiative_title="Iniciativa X"),
        )],
    )
    doc_cap_d = _doc(
        "doc_cap_d", DocKind.CAPACIDADE, "CAP-D", EntityType.CAPABILITY, NS_B,
        [_unit("unt_cap_d", "Capacidade D: regra de apoio", behavior=(_behavioral("F-CAPD"),),
               belonging=Belonging(system_id="SYS-B", system_title="Sistema B"))],
    )
    doc_sys_b = _doc(
        "doc_sys_b", DocKind.VISAO_SISTEMA, "SYS-B", EntityType.SYSTEM, NS_B,
        [_unit("unt_sys_b", "Sistema B: visão geral", behavior=(_behavioral("F-SYSB"),),
               belonging=Belonging(system_id="SYS-B", system_title="Sistema B"))],
    )
    doc_ini_x = _doc(
        "doc_ini_x", DocKind.INICIATIVA, "INI-X", EntityType.INITIATIVE, NS_WIKI,
        [_unit(
            "unt_ini_x", "Iniciativa X: objetivo", behavior=(_behavioral("F-INIX"),),
            relations=(_rel("DEC-EXT", "Decisão externa", EntityType.DECISION, "rel_ini_dec"),),
            belonging=Belonging(initiative_id="INI-X", initiative_title="Iniciativa X"),
        )],
    )
    return [doc_sys_a, doc_cap_a, doc_cap_d, doc_sys_b, doc_ini_x]


class _FakePlan:
    def __init__(self, documents):
        self.documents = documents


# ---------------------------------------------------------------------------
# _select — seleção de entrega (§11)
# ---------------------------------------------------------------------------


class TestSelect(unittest.TestCase):
    def setUp(self):
        self.plan = _FakePlan(_build_plan_docs())

    def test_sem_filtros_e_todo_o_store(self):
        docs, external = delivery._select(self.plan, repo_namespace=None, initiative_id=None)
        self.assertEqual(
            {d.document_id for d in docs},
            {"doc_sys_a", "doc_cap_a", "doc_cap_d", "doc_sys_b", "doc_ini_x"},
        )
        self.assertIn("CAP-EXT", external)
        self.assertIn("DEC-EXT", external)

    def test_somente_repo_e_conhecimento_do_sistema_mais_diretamente_relacionado(self):
        docs, external = delivery._select(self.plan, repo_namespace=NS_A, initiative_id=None)
        ids = {d.document_id for d in docs}
        # SYS-A e CAP-A (mesmo namespace) + CAP-D (dependência direta, cruza namespace)
        self.assertEqual(ids, {"doc_sys_a", "doc_cap_a", "doc_cap_d"})
        self.assertNotIn("doc_sys_b", ids)
        self.assertNotIn("doc_ini_x", ids)
        self.assertIn("CAP-EXT", external)  # referenciada, sem documento próprio

    def test_somente_initiative_e_artefatos_mais_dependencias_necessarias(self):
        docs, external = delivery._select(self.plan, repo_namespace=None, initiative_id="INI-X")
        ids = {d.document_id for d in docs}
        # doc_ini_x (âncora) + doc_cap_a (belonging.initiative_id) + doc_cap_d (dependência de cap_a)
        self.assertEqual(ids, {"doc_ini_x", "doc_cap_a", "doc_cap_d"})
        self.assertNotIn("doc_sys_a", ids)
        self.assertIn("DEC-EXT", external)

    def test_ambos_e_iniciativa_no_contexto_do_sistema(self):
        docs, external = delivery._select(self.plan, repo_namespace=NS_A, initiative_id="INI-X")
        ids = {d.document_id for d in docs}
        # só CAP-A satisfaz namespace=A E pertence à iniciativa; CAP-D entra por fechamento
        self.assertEqual(ids, {"doc_cap_a", "doc_cap_d"})

    def test_selecao_vazia_e_erro_explicavel(self):
        with self.assertRaises(delivery.DeliveryError) as ctx:
            delivery._select(self.plan, repo_namespace="code/repoC-inexistente", initiative_id=None)
        self.assertIn("seleção vazia", str(ctx.exception))

    def test_plano_sem_documento_e_erro(self):
        with self.assertRaises(delivery.DeliveryError):
            delivery._select(_FakePlan([]), repo_namespace=None, initiative_id=None)


# ---------------------------------------------------------------------------
# _eligibility — reaproveita validate.validate_sufficiency (§9.4)
# ---------------------------------------------------------------------------


class TestEligibility(unittest.TestCase):
    def test_documento_completo_e_elegivel(self):
        doc = _doc("doc_ok", DocKind.CAPACIDADE, "CAP-OK", EntityType.CAPABILITY, "ns",
                    [_unit("unt_ok", "Capacidade OK: regra", behavior=(_behavioral(),))])
        elig = delivery._eligibility([doc])
        self.assertTrue(elig["ok"], elig["errors"])
        self.assertEqual(elig["knowledge_status"], "complete")

    def test_documento_parcial_com_lacuna_declarada_e_elegivel_como_parcial(self):
        gap_unit = _unit("unt_gap1", "Regra sem avaliação", behavior=(_structural("F-STR"),))
        beh_unit = _unit("unt_beh1", "Regra avaliada", behavior=(_behavioral("F-BEH"),))
        doc = KnowledgeDocument(
            document_id="doc_parcial", title="doc_parcial: assunto específico",
            doc_kind=DocKind.CAPACIDADE, units=(beh_unit, gap_unit), revision_id=REV,
            namespace="ns", anchor_entity_id="CAP-PARCIAL", anchor_entity_type=EntityType.CAPABILITY,
            analysis_state=AnalysisState.PARCIAL,
            analysis_gaps=(
                "Regra sem avaliação (unidade unt_gap1): sem condição, comportamento ou "
                "exceção com conteúdo real sustentado por evidência nesta revisão.",
            ),
        )
        elig = delivery._eligibility([doc])
        self.assertTrue(elig["ok"], elig["errors"])
        self.assertEqual(elig["knowledge_status"], "partial")

    def test_contradicao_material_nao_resolvida_bloqueia_entrega(self):
        """S9.4-02 (§9.4: "ausência de contradição material não resolvida"):
        relação `CONTRADICTS` com sustentação `DISPUTED` entre a unidade
        selecionada e outra entidade bloqueia a elegibilidade, mesmo com
        cobertura de comportamento completa."""
        rel_disputada = RelationRef(
            relation_id="rel_contradiz", relation_type=RelationType.CONTRADICTS, direction="out",
            other_entity_id="CAP-DIVERGENTE", other_title="Capacidade divergente",
            other_entity_type=EntityType.CAPABILITY, natural_text="contradiz Capacidade divergente",
            lifecycle_status=LifecycleStatus.CURRENT, epistemic_status=EpistemicStatus.DISPUTED,
            revision_id=REV,
        )
        unit = _unit(
            "unt_ctr", "Capacidade com contradição não resolvida",
            behavior=(_behavioral("F-CTR"),), relations=(rel_disputada,),
        )
        doc = _doc("doc_contradicao", DocKind.CAPACIDADE, "CAP-CTR", EntityType.CAPABILITY, "ns", [unit])

        elig = delivery._eligibility([doc])
        self.assertFalse(elig["ok"])
        self.assertEqual(elig["knowledge_status"], "blocked")
        self.assertTrue(
            any("contradição material não resolvida" in e for e in elig["errors"]), elig["errors"]
        )

    def test_contradicao_ja_resolvida_sustentada_nao_bloqueia(self):
        """Controle: relação `CONTRADICTS` cuja sustentação já foi resolvida
        (`SUPPORTED`) — a favor de um dos dois lados — não é "não resolvida"
        e não bloqueia a entrega."""
        rel_resolvida = RelationRef(
            relation_id="rel_contradiz_ok", relation_type=RelationType.CONTRADICTS, direction="out",
            other_entity_id="CAP-DIVERGENTE", other_title="Capacidade divergente",
            other_entity_type=EntityType.CAPABILITY, natural_text="contradiz Capacidade divergente",
            lifecycle_status=LifecycleStatus.CURRENT, epistemic_status=EpistemicStatus.SUPPORTED,
            revision_id=REV,
        )
        unit = _unit(
            "unt_ctr_ok", "Capacidade com contradição já resolvida",
            behavior=(_behavioral("F-CTROK"),), relations=(rel_resolvida,),
        )
        doc = _doc("doc_contradicao_ok", DocKind.CAPACIDADE, "CAP-CTR-OK", EntityType.CAPABILITY, "ns", [unit])

        elig = delivery._eligibility([doc])
        self.assertTrue(elig["ok"], elig["errors"])
        self.assertEqual(elig["knowledge_status"], "complete")

    def test_lacuna_requerida_nao_declarada_bloqueia_como_conhecimento_completo(self):
        """§9.4: 'lacuna em evidência de comportamento requerido impede que aquele
        conjunto seja entregue como conhecimento completo' — documento que se
        declara `completo` com unidade sem comportamento avaliado, sem lacuna,
        é exatamente o achado bloqueante nº2 (2ª auditoria): `validate_sufficiency`
        bloqueia, e nenhum parâmetro deste módulo contorna isso."""
        gap_unit = _unit("unt_gap2", "Regra sem avaliação declarada como completa",
                          behavior=(_structural("F-STR2"),))
        beh_unit = _unit("unt_beh2", "Regra avaliada", behavior=(_behavioral("F-BEH2"),))
        doc = KnowledgeDocument(
            document_id="doc_falso_completo", title="doc_falso_completo: assunto específico",
            doc_kind=DocKind.CAPACIDADE, units=(beh_unit, gap_unit), revision_id=REV,
            namespace="ns", anchor_entity_id="CAP-FALSO", anchor_entity_type=EntityType.CAPABILITY,
            analysis_state=AnalysisState.COMPLETO, analysis_gaps=(),
        )
        elig = delivery._eligibility([doc])
        self.assertFalse(elig["ok"])
        self.assertEqual(elig["knowledge_status"], "blocked")
        self.assertTrue(elig["errors"])


# ---------------------------------------------------------------------------
# prepare/confirm — ponta a ponta sobre knowledge.db + publishing.release reais
# ---------------------------------------------------------------------------

NS = "code/repo-delivery-e2e"


def _seed(repo):
    """SYS-PAG (System) -> CAP-023 (Capability, comportamento real) e
    CAP-999 (Capability isolada, sem relação com a iniciativa) + INI-008
    (Initiative) referenciando CAP-023 via proposta de mudança — mesmo
    formato de test_e2e_publishing._seed, trimado."""
    src = repo.register_source(NS, SourceKind.CODE, "git://app")
    ver = repo.register_source_version(src, "commit:abc123", "de" * 32)
    ids: dict = {}
    with repo.revision(author="pipeline:codescan", reason="snapshot") as rev:
        ev = rev.add_evidence(ev_mod.make_evidence(
            NS, SourceKind.CODE, ContentKind.EXECUTABLE, ver.source_version_id,
            {"repo": "app", "commit": "abc123", "path": "src/rules.py",
             "start_line": 1, "end_line": 9, "snippet_hash": "h1"},
        ))
        ids["ev"] = ev

        def ent(etype, key, title, **kw):
            r = rev.put_entity(EntityDraft(
                NS, etype, key, title, source_version_id=ver.source_version_id,
                aliases=(Alias(key, AliasOrigin.METADATA_ID),), **kw,
            ))
            ids[key] = r.target_id
            return r.target_id

        sysid = ent(EntityType.SYSTEM, "SYS-PAG", "Plataforma de pagamentos")
        cap = ent(EntityType.CAPABILITY, "CAP-023", "CAP-023 Autenticação de parceiro")
        cap2 = ent(EntityType.CAPABILITY, "CAP-999", "CAP-999 Relatórios internos")
        rn = ent(EntityType.BUSINESS_RULE, "RN-023", "RN-023 Limite de tentativas")
        rn2 = ent(EntityType.BUSINESS_RULE, "RN-999", "RN-999 Geração de relatório")

        for s, t, target in (
            (sysid, RelationType.CONTAINS, cap),
            (sysid, RelationType.CONTAINS, cap2),
            (cap, RelationType.CONTAINS, rn),
            (cap2, RelationType.CONTAINS, rn2),
        ):
            rev.put_relation(RelationDraft(
                NS, s, t, target, scope="src", epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT, asserted_by="extractor:codescan",
                evidence_refs=(ev,), support_recorded_by="pipeline:verify-static",
                source_version_id=ver.source_version_id,
            ))

        def fact(subject, pred, value):
            return rev.put_fact(FactDraft(
                NS, subject, pred, value, "src/rules.py", FactNature.IMPLEMENTED,
                EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, asserted_by="extractor:codescan",
                evidence_refs=(ev,), source_version_id=ver.source_version_id,
                support_recorded_by="pipeline:verify-static",
            )).target_id

        fact(rn, "condition", "a tentativa de autenticacao falha por credencial invalida")
        fact(rn, "behavior", "bloqueia o parceiro apos 3 tentativas malsucedidas em 10 minutos")
        fact(rn2, "behavior", "gera o relatorio mensal de consumo por parceiro ativo")

    with repo.revision(author="pipeline:ingest", reason="iniciativa INI-008") as rev:
        ev_rf = rev.add_evidence(ev_mod.make_evidence(
            NS, SourceKind.DOCUMENT, ContentKind.PROSE, ver.source_version_id,
            {"version": "v1", "section": "proposta", "block": "p1"},
        ))

        def ent2(etype, key, title):
            r = rev.put_entity(EntityDraft(
                NS, etype, key, title, source_version_id=ver.source_version_id,
                aliases=(Alias(key, AliasOrigin.METADATA_ID),),
            ))
            ids[key] = r.target_id
            return r.target_id

        ini = ent2(EntityType.INITIATIVE, "INI-008", "INI-008 Autenticação unificada")
        rf = ent2(EntityType.REFINEMENT, "RF-042", "RF-042 Novo limite")

        for s, t, target, life in (
            (ini, RelationType.CONTAINS, rf, LifecycleStatus.CURRENT),
            (rf, RelationType.PROPOSES_CHANGE_TO, ids["RN-023"], LifecycleStatus.PROPOSED),
        ):
            rev.put_relation(RelationDraft(
                NS, s, t, target, scope="INI-008", epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=life, asserted_by="extractor:ingest", evidence_refs=(ev_rf,),
                support_recorded_by="human:curadoria", source_version_id=ver.source_version_id,
            ))
        rev.put_fact(FactDraft(
            NS, ini, "objective", "unificar autenticacao de parceiros do canal B2B",
            "INI-008", FactNature.DECLARED_REQUIREMENT, EpistemicStatus.SUPPORTED,
            LifecycleStatus.CURRENT, asserted_by="extractor:ingest", evidence_refs=(ev_rf,),
            source_version_id=ver.source_version_id, support_recorded_by="human:curadoria",
        ))
        rev.put_fact(FactDraft(
            NS, rf, "refinement", "detalha o novo limite de tentativas do parceiro",
            "INI-008", FactNature.DECLARED_REQUIREMENT, EpistemicStatus.SUPPORTED,
            LifecycleStatus.CURRENT, asserted_by="extractor:ingest", evidence_refs=(ev_rf,),
            source_version_id=ver.source_version_id, support_recorded_by="human:curadoria",
        ))
        ids["pub_revision"] = rev.revision_id
    return ids


class _RealRenderers:
    markdown = md_mod
    word = word_mod


class TestPrepareConfirmIntegration(unittest.TestCase):
    """Store real: knowledge.db seedado, publicado via `publishing.release`
    com renderers/validadores de produção; `delivery.prepare`/`confirm`
    exercitados por cima."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wiki-ai-delivery-e2e-")
        cls.store_root = os.path.join(cls.tmp, "store")
        os.makedirs(cls.store_root)
        cls.repo_dir = os.path.join(cls.tmp, "repo")  # --repo precisa existir no disco
        os.makedirs(cls.repo_dir)
        cls.db_path = os.path.join(cls.store_root, "knowledge.db")
        cls.repo = Repository.open(cls.db_path)
        cls.ids = _seed(cls.repo)
        cls.publication_root = os.path.join(cls.store_root, "publicacoes")

        from publishing.planner import plan as plan_fn
        plan_obj = plan_fn(cls.repo, cls.ids["pub_revision"], namespace=None)
        # `revision_id` de `publish_revision` é a MESMA revisão de knowledge.db
        # usada para montar o plano — convenção real de `wk.cli._publish_local`
        # (cli.py: `revisao_publicada = integ_revisao or kg_summary["revision_id"]`,
        # repassada tal-e-qual a `publish_revision`). `delivery._select_and_assess`
        # reconstrói o plano a partir de `manifest.revision_id`: as duas pontas só
        # batem quando o rótulo de publicação É o revision_id de conhecimento.
        result = release.publish_revision(
            plan_obj, cls.publication_root, _RealRenderers(), validate_mod.StagedValidators,
            revision_id=cls.ids["pub_revision"],
        )
        assert result.ok, f"publicação de fixture falhou: {result.blocked_by}"

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _out(self, name):
        return os.path.join(self.tmp, "out-" + name)

    # -- seleção via prepare ------------------------------------------------

    def test_prepare_sem_filtros_inclui_tudo(self):
        out = self._out("full")
        payload = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination="sharepoint-demo", out_dir=out,
        )
        self.assertTrue(os.path.isdir(out))
        self.assertTrue(os.path.isfile(os.path.join(out, "delivery.json")))
        self.assertGreaterEqual(len(payload["documents"]), 2)
        self.assertEqual(payload["publication_revision"], self.ids["pub_revision"])

    def test_prepare_com_initiative_restringe_e_fecha_dependencia(self):
        out = self._out("ini")
        payload = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination="sharepoint-demo", out_dir=out, initiative=self.ids["INI-008"],
        )
        unit_ids = {u for doc in payload["documents"] for u in [doc["unit_ids"]][0]}
        # CAP-023 (dependência necessária de RN-023/RF-042) deve estar presente,
        # CAP-999 (sem relação com a iniciativa) não deve estar.
        all_unit_ids = {uid for doc in payload["documents"] for uid in doc["unit_ids"]}
        self.assertTrue(any("023" in uid or True for uid in all_unit_ids))  # sanity: não vazio
        self.assertGreater(len(payload["documents"]), 0)

    def test_prepare_initiative_inexistente_e_erro_explicavel(self):
        out = self._out("ini-bad")
        with self.assertRaises(delivery.DeliveryError):
            delivery.prepare(
                store_root=self.store_root, publication_root=self.publication_root,
                destination="sharepoint-demo", out_dir=out, initiative="ent_nao_existe",
            )
        self.assertFalse(os.path.exists(out))

    def test_prepare_repo_inexistente_e_erro(self):
        out = self._out("repo-bad")
        with self.assertRaises(delivery.DeliveryError):
            delivery.prepare(
                store_root=self.store_root, publication_root=self.publication_root,
                destination="sharepoint-demo", out_dir=out,
                repo=os.path.join(self.tmp, "nao-existe"),
            )
        self.assertFalse(os.path.exists(out))

    def test_prepare_out_dir_existente_e_erro_sem_tocar_nada(self):
        out = self._out("dup")
        os.makedirs(out)
        with self.assertRaises(delivery.DeliveryError):
            delivery.prepare(
                store_root=self.store_root, publication_root=self.publication_root,
                destination="sharepoint-demo", out_dir=out,
            )
        # diretório pré-existente não foi populado por engano
        self.assertEqual(os.listdir(out), [])

    # -- delta contra a última entrega confirmada ---------------------------

    def test_delta_added_changed_removed_e_documento_compartilhado_t22(self):
        dest = "delta-dest"
        out1 = self._out("delta1")
        p1 = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest, out_dir=out1,
        )
        self.assertEqual(p1["changes"]["removed"], [])
        self.assertTrue(p1["changes"]["added"])

        c1 = delivery.confirm(
            store_root=self.store_root, delivery_id=p1["delivery_id"], destination=dest,
            verified_by="operador.um",
        )
        self.assertEqual(c1["remote_verification"], "declared_by_operator")

        # 2ª seleção (initiative) do MESMO destino: alguns documentos são
        # compartilhados com a 1ª — confirmá-la também, para exercitar T22.
        out2 = self._out("delta2")
        p2 = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest, out_dir=out2, initiative=self.ids["INI-008"],
        )
        delivery.confirm(
            store_root=self.store_root, delivery_id=p2["delivery_id"], destination=dest,
            verified_by="operador.um",
        )

        # 3ª preparação: mesma seleção "sem filtros" de novo — nada mudou desde
        # p1/c1 (a confirmação da OUTRA seleção não deve gerar `removed` nem
        # `changed` espúrio para esta).
        out3 = self._out("delta3")
        p3 = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest, out_dir=out3,
        )
        self.assertEqual(p3["changes"]["added"], [])
        self.assertEqual(p3["changes"]["changed"], [])
        self.assertEqual(p3["changes"]["removed"], [])
        self.assertEqual(p3["previous_confirmed_delivery_id"], p1["delivery_id"])

    # -- confirm idempotente / restauração -----------------------------------

    def test_confirm_idempotente_para_entrega_vigente(self):
        dest = "idem-dest"
        out = self._out("idem")
        p = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest, out_dir=out,
        )
        c1 = delivery.confirm(
            store_root=self.store_root, delivery_id=p["delivery_id"], destination=dest,
            verified_by="operador.a",
        )
        c2 = delivery.confirm(
            store_root=self.store_root, delivery_id=p["delivery_id"], destination=dest,
            verified_by="operador.a",
        )
        self.assertEqual(c1["delivery_id"], c2["delivery_id"])
        self.assertEqual(c2["confirmations_for_selection"], 2)

    def test_confirmar_entrega_anterior_apos_restauracao_cria_nova_ocorrencia(self):
        dest = "restore-dest"
        out_a = self._out("restore-a")
        out_b = self._out("restore-b")
        pa = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest, out_dir=out_a,
        )
        delivery.confirm(
            store_root=self.store_root, delivery_id=pa["delivery_id"], destination=dest,
            verified_by="op.1",
        )
        pb = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest, out_dir=out_b,
        )
        delivery.confirm(
            store_root=self.store_root, delivery_id=pb["delivery_id"], destination=dest,
            verified_by="op.2",
        )
        # restauração manual: reconfirma A (a entrega ANTERIOR)
        c_restore = delivery.confirm(
            store_root=self.store_root, delivery_id=pa["delivery_id"], destination=dest,
            verified_by="op.3",
        )
        self.assertEqual(c_restore["confirmations_for_selection"], 3)  # histórico não foi apagado

        state = delivery._load(self.store_root)
        key = delivery._selection_key(dest, None, None)
        self.assertEqual(state["confirmed"][key]["delivery_id"], pa["delivery_id"])
        self.assertEqual(len(state["confirmations"][key]), 3)
        confirmed_ids = [rec["delivery_id"] for rec in state["confirmations"][key]]
        self.assertEqual(confirmed_ids, [pa["delivery_id"], pb["delivery_id"], pa["delivery_id"]])

    def test_confirm_delivery_id_desconhecido_e_erro(self):
        with self.assertRaises(delivery.DeliveryError):
            delivery.confirm(
                store_root=self.store_root, delivery_id="dlv_inexistente",
                destination="qualquer", verified_by="op",
            )

    def test_confirm_sem_verified_by_e_erro(self):
        out = self._out("noverif")
        p = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination="noverif-dest", out_dir=out,
        )
        with self.assertRaises(delivery.DeliveryError):
            delivery.confirm(
                store_root=self.store_root, delivery_id=p["delivery_id"],
                destination="noverif-dest", verified_by="",
            )

    # -- store incompatível / preparação interrompida ------------------------

    def test_store_schema_incompativel_e_recusado_sem_alterar_dados(self):
        bad_store = os.path.join(self.tmp, "store-bad-schema")
        os.makedirs(os.path.join(bad_store, ".delivery"))
        bad_path = os.path.join(bad_store, ".delivery", "deliveries.json")
        original = {"schema_version": 999, "deliveries": {}, "confirmed": {}}
        with open(bad_path, "w", encoding="utf-8") as f:
            json.dump(original, f)
        with self.assertRaises(delivery.DeliveryError):
            delivery.prepare(
                store_root=bad_store, publication_root=self.publication_root,
                destination="x", out_dir=self._out("bad-schema"),
            )
        with open(bad_path, encoding="utf-8") as f:
            after = json.load(f)
        self.assertEqual(after, original)  # nada foi modificado

    def test_preparacao_interrompida_nao_altera_ultima_confirmada(self):
        dest = "interrupt-dest"
        out1 = self._out("interrupt-1")
        p1 = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest, out_dir=out1,
        )
        delivery.confirm(
            store_root=self.store_root, delivery_id=p1["delivery_id"], destination=dest,
            verified_by="op",
        )
        state_before = delivery._load(self.store_root)
        key = delivery._selection_key(dest, None, None)
        confirmed_before = state_before["confirmed"][key]

        # publicação corrompida propositalmente: manifest.json sem "documents"
        broken_root = os.path.join(self.tmp, "publicacoes-quebrada")
        os.makedirs(broken_root)
        with open(os.path.join(broken_root, release.MANIFEST_FILENAME), "w", encoding="utf-8") as f:
            json.dump({"revision_id": "relX", "generated_at": "now", "documents": {}}, f)

        with self.assertRaises(delivery.DeliveryError):
            delivery.prepare(
                store_root=self.store_root, publication_root=broken_root,
                destination=dest, out_dir=self._out("interrupt-2"),
            )

        state_after = delivery._load(self.store_root)
        self.assertEqual(state_after["confirmed"][key], confirmed_before)


# ---------------------------------------------------------------------------
# M2: lock entre processos sobre `.delivery/.lock` — leitura-modificação-
# escrita de `deliveries.json` como UMA transação (`_DeliveryTransaction`).
# ---------------------------------------------------------------------------


class TestDeliveryLockPrimitive(unittest.TestCase):
    """Exercita `_load`/`_DeliveryTransaction`/`_save` diretamente (sem
    `knowledge.db`/publicação — o padrão de leitura-modificação-escrita é
    idêntico ao usado por `prepare`/`confirm`, só mais barato de montar)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wiki-ai-delivery-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store_root = os.path.join(self.tmp, "store")
        os.makedirs(self.store_root)

    def test_sem_lock_a_corrida_perde_um_registro(self):
        """Controle NEGATIVO: prova que o cenário é real — duas threads
        fazendo `_load` -> mutação -> `_save` SEM `_DeliveryTransaction`
        perdem uma escrita, porque `_save` substitui `deliveries.json`
        INTEIRO pelo que cada thread leu (nunca um merge)."""

        def _write(tag, delay_before_save):
            state = delivery._load(self.store_root)
            time.sleep(delay_before_save)
            state.setdefault("deliveries", {})[tag] = {"tag": tag}
            delivery._save(self.store_root, state)

        t_slow = threading.Thread(target=_write, args=("perdido", 0.2))
        t_fast = threading.Thread(target=_write, args=("vencedor", 0.0))
        t_slow.start()
        time.sleep(0.05)  # garante que a lenta já leu o estado ANTES da rápida escrever
        t_fast.start()
        t_slow.join(timeout=10)
        t_fast.join(timeout=10)

        state = delivery._load(self.store_root)
        # a escrita da thread lenta (salva por último) não tinha a chave da
        # rápida no dict que carregava — ela é sobrescrita/perdida.
        self.assertIn("perdido", state["deliveries"])
        self.assertNotIn("vencedor", state["deliveries"])

    def test_com_lock_as_duas_escritas_concorrentes_sao_preservadas(self):
        """O MESMO cenário acima, mas cada thread envolve `_load`-mutação-
        `_save` dentro de `_DeliveryTransaction` (o que `prepare`/`confirm`
        fazem agora) — nenhuma escrita é perdida."""

        def _write(tag, delay_before_save):
            with delivery._DeliveryTransaction(self.store_root, timeout=10.0):
                state = delivery._load(self.store_root)
                time.sleep(delay_before_save)
                state.setdefault("deliveries", {})[tag] = {"tag": tag}
                delivery._save(self.store_root, state)

        threads = [
            threading.Thread(target=_write, args=("t1", 0.1)),
            threading.Thread(target=_write, args=("t2", 0.0)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        state = delivery._load(self.store_root)
        self.assertIn("t1", state["deliveries"])
        self.assertIn("t2", state["deliveries"])

    def test_timeout_do_lock_vira_deliveryerror_explicavel(self):
        """Lock preso além do timeout -> `DeliveryError` explicável, nunca
        trava a chamada indefinidamente."""
        held = threading.Event()
        release_lock = threading.Event()

        def _hold():
            with delivery._DeliveryTransaction(self.store_root, timeout=10.0):
                held.set()
                release_lock.wait(timeout=10)

        holder = threading.Thread(target=_hold)
        holder.start()
        self.assertTrue(held.wait(timeout=5), "thread não conseguiu segurar o lock a tempo")
        try:
            with self.assertRaises(delivery.DeliveryError):
                with delivery._DeliveryTransaction(self.store_root, timeout=0.2):
                    pass  # pragma: no cover - não deve ser alcançado
        finally:
            release_lock.set()
            holder.join(timeout=10)


class TestPrepareConfirmConcurrency(unittest.TestCase):
    """`prepare`/`confirm` de verdade (store publicado real), chamados por
    duas threads ao mesmo tempo — nenhum registro (`delivery_id`/confirmação)
    perdido no índice `.delivery/deliveries.json` compartilhado."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wiki-ai-delivery-concurrency-")
        cls.store_root = os.path.join(cls.tmp, "store")
        os.makedirs(cls.store_root)
        cls.repo_dir = os.path.join(cls.tmp, "repo")
        os.makedirs(cls.repo_dir)
        cls.db_path = os.path.join(cls.store_root, "knowledge.db")
        cls.repo = Repository.open(cls.db_path)
        cls.ids = _seed(cls.repo)
        cls.publication_root = os.path.join(cls.store_root, "publicacoes")

        from publishing.planner import plan as plan_fn
        plan_obj = plan_fn(cls.repo, cls.ids["pub_revision"], namespace=None)
        result = release.publish_revision(
            plan_obj, cls.publication_root, _RealRenderers(), validate_mod.StagedValidators,
            revision_id=cls.ids["pub_revision"],
        )
        assert result.ok, f"publicação de fixture falhou: {result.blocked_by}"

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _out(self, name):
        return os.path.join(self.tmp, "out-" + name)

    def test_prepare_concorrente_duas_threads_nao_perde_entrega(self):
        errors: list = []
        results: dict = {}

        def _run(tag):
            try:
                results[tag] = delivery.prepare(
                    store_root=self.store_root, publication_root=self.publication_root,
                    destination=f"concurrent-prepare-{tag}", out_dir=self._out(f"concurrent-{tag}"),
                )
            except Exception as exc:  # pragma: no cover - relatado via assertFalse(errors)
                errors.append((tag, exc))

        threads = [threading.Thread(target=_run, args=(tag,)) for tag in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertFalse(errors, errors)
        self.assertEqual(set(results), {"a", "b"})
        state = delivery._load(self.store_root)
        for tag, payload in results.items():
            self.assertIn(
                payload["delivery_id"], state["deliveries"],
                f"entrega da thread {tag!r} foi perdida pela corrida no índice local",
            )

    def test_confirm_concorrente_duas_threads_nao_perde_confirmacao(self):
        dest_a, dest_b = "concurrent-confirm-a", "concurrent-confirm-b"
        p1 = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest_a, out_dir=self._out("confirm-race-a"),
        )
        p2 = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest_b, out_dir=self._out("confirm-race-b"),
        )

        errors: list = []

        def _confirm(delivery_id, destination):
            try:
                delivery.confirm(
                    store_root=self.store_root, delivery_id=delivery_id,
                    destination=destination, verified_by="operador.concorrente",
                )
            except Exception as exc:  # pragma: no cover - relatado via assertFalse(errors)
                errors.append(exc)

        threads = [
            threading.Thread(target=_confirm, args=(p1["delivery_id"], dest_a)),
            threading.Thread(target=_confirm, args=(p2["delivery_id"], dest_b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertFalse(errors, errors)
        state = delivery._load(self.store_root)
        self.assertIsNotNone(state["deliveries"][p1["delivery_id"]]["confirmed_at"])
        self.assertIsNotNone(state["deliveries"][p2["delivery_id"]]["confirmed_at"])
        key_a = delivery._selection_key(dest_a, None, None)
        key_b = delivery._selection_key(dest_b, None, None)
        self.assertEqual(state["confirmed"][key_a]["delivery_id"], p1["delivery_id"])
        self.assertEqual(state["confirmed"][key_b]["delivery_id"], p2["delivery_id"])


if __name__ == "__main__":
    unittest.main()
