"""Testes de lacunas da especificação — CLI (§10.3, §11).

Cobre requisitos específicos:

- T3/S10.3-02/S10.3-06/S11-08: `delivery prepare --json` com repo
  inexistente ⇒ knowledge_status=="blocked"
- T4/S11-15/16/17: `delivery prepare` com diferentes combinações de filtros
- T9/S10.4.3-05: resolve_transport("auto") considera supports_session
  (testado via CLI quando possível)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
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
from publishing.document import DocKind, document_id
from publishing.planner import plan as plan_fn

from wk import cli

NS = "code/repo-spec-gaps-cli"


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _seed_multi_initiatives(repo):
    """SYS-A contendo CAP-X (ini-X), CAP-Y (ini-Y), CAP-Z (sem iniciativa).
    Permite testar seleção com diferentes filtros."""
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

        sysid = ent(EntityType.SYSTEM, "SYS-A", "Sistema A")
        cap_x = ent(EntityType.CAPABILITY, "CAP-X", "Capacidade X")
        cap_y = ent(EntityType.CAPABILITY, "CAP-Y", "Capacidade Y")
        cap_z = ent(EntityType.CAPABILITY, "CAP-Z", "Capacidade Z")
        rn_x = ent(EntityType.BUSINESS_RULE, "RN-X", "Regra X")
        rn_y = ent(EntityType.BUSINESS_RULE, "RN-Y", "Regra Y")
        rn_z = ent(EntityType.BUSINESS_RULE, "RN-Z", "Regra Z")

        for s, t, target in (
            (sysid, RelationType.CONTAINS, cap_x),
            (sysid, RelationType.CONTAINS, cap_y),
            (sysid, RelationType.CONTAINS, cap_z),
            (cap_x, RelationType.CONTAINS, rn_x),
            (cap_y, RelationType.CONTAINS, rn_y),
            (cap_z, RelationType.CONTAINS, rn_z),
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

        fact(rn_x, "condition", "condicao de X")
        fact(rn_x, "behavior", "comportamento de X")
        fact(rn_y, "condition", "condicao de Y")
        fact(rn_y, "behavior", "comportamento de Y")
        fact(rn_z, "condition", "condicao de Z")
        fact(rn_z, "behavior", "comportamento de Z")
        ids["pub_revision"] = rev.revision_id

    with repo.revision(author="pipeline:ingest", reason="initiatives") as rev:
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

        ini_x = ent2(EntityType.INITIATIVE, "INI-X", "Iniciativa X")
        ini_y = ent2(EntityType.INITIATIVE, "INI-Y", "Iniciativa Y")
        rf_x = ent2(EntityType.REFINEMENT, "RF-X", "Refinamento X")
        rf_y = ent2(EntityType.REFINEMENT, "RF-Y", "Refinamento Y")

        for s, t, target, life in (
            (ini_x, RelationType.CONTAINS, rf_x, LifecycleStatus.CURRENT),
            (ini_y, RelationType.CONTAINS, rf_y, LifecycleStatus.CURRENT),
            (rf_x, RelationType.PROPOSES_CHANGE_TO, ids["RN-X"], LifecycleStatus.PROPOSED),
            (rf_y, RelationType.PROPOSES_CHANGE_TO, ids["RN-Y"], LifecycleStatus.PROPOSED),
        ):
            rev.put_relation(RelationDraft(
                NS, s, t, target, scope="ini", epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=life, asserted_by="extractor:ingest", evidence_refs=(ev_rf,),
                support_recorded_by="human:curadoria", source_version_id=ver.source_version_id,
            ))

    return ids


class _RealRenderers:
    markdown = md_mod
    word = word_mod


class TestDeliveryCliRepoBlockedJson(unittest.TestCase):
    """T3/S10.3-02/S10.3-06/S11-08: `delivery prepare --json` com repo
    inexistente ⇒ operation_status=="blocked" e knowledge_status=="blocked"."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wiki-ai-cli-repo-test-")
        cls.store_root = os.path.join(cls.tmp, "store")
        os.makedirs(cls.store_root)
        cls.db_path = os.path.join(cls.store_root, "knowledge.db")
        cls.repo = Repository.open(cls.db_path)
        cls.ids = _seed_multi_initiatives(cls.repo)
        cls.publication_root = os.path.join(cls.store_root, "publicacoes")

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

    def test_repo_inexistente_bloqueado_json(self):
        """--repo que não existe no disco deve sair com operation_status=blocked
        e knowledge_status=blocked."""
        out = os.path.join(self.tmp, "dlv-bad-repo")
        fake_repo = os.path.join(self.tmp, "repo-nao-existe")
        code, stdout, stderr = _run([
            "delivery", "prepare", "--store", self.store_root,
            "--repo", fake_repo,
            "--destination", "dest-bad-repo", "--out", out, "--json",
        ])
        self.assertEqual(code, 2, f"stderr: {stderr}")
        payload = json.loads(stdout)
        self.assertEqual(payload["operation_status"], "blocked",
                        f"payload: {payload}")
        self.assertEqual(payload["knowledge_status"], "blocked",
                        f"payload: {payload}")
        self.assertFalse(os.path.exists(out),
                        "diretório de saída não deve ser criado quando bloqueado")


def _seed_s11(repo, ns_x, ns_other, ns_ini, ns_unrelated):
    """CAP-A (repo X) depende de CAP-D (outro repo, documento próprio);
    CAP-Z é uma capacidade de repo X isolada, sem relação nenhuma com a
    iniciativa; CAP-U é uma capacidade de um repo TOTALMENTE não
    relacionado (nem repo X, nem iniciativa X); INI-X propõe mudança em
    RN-A (torna CAP-A parte da iniciativa X) através de RF-A — RF-A é do
    tipo Refinement, que nunca vira documento próprio (nenhuma `_plan_*`
    função monta documento para Refinement): é a entidade REFERENCIADA sem
    documento próprio que a spec pede em `external_entities`."""
    ids: dict = {}
    src_x = repo.register_source(ns_x, SourceKind.CODE, "git://repo-s11-x")
    ver_x = repo.register_source_version(src_x, "commit:s11x1", "a5" * 32)
    src_other = repo.register_source(ns_other, SourceKind.CODE, "git://repo-s11-other")
    ver_other = repo.register_source_version(src_other, "commit:s11o1", "b6" * 32)
    src_ini = repo.register_source(ns_ini, SourceKind.DOCUMENT, "doc://s11-ini")
    ver_ini = repo.register_source_version(src_ini, "v1", "c7" * 32)
    src_u = repo.register_source(ns_unrelated, SourceKind.CODE, "git://repo-s11-unrelated")
    ver_u = repo.register_source_version(src_u, "commit:s11u1", "d8" * 32)

    with repo.revision(author="pipeline:codescan", reason="s11 snapshot") as rev:
        ev_x = rev.add_evidence(ev_mod.make_evidence(
            ns_x, SourceKind.CODE, ContentKind.EXECUTABLE, ver_x.source_version_id,
            {"repo": "repo-s11-x", "commit": "s11x1", "path": "src/a.py",
             "start_line": 1, "end_line": 9, "snippet_hash": "hs11a"},
        ))
        ev_other = rev.add_evidence(ev_mod.make_evidence(
            ns_other, SourceKind.CODE, ContentKind.EXECUTABLE, ver_other.source_version_id,
            {"repo": "repo-s11-other", "commit": "s11o1", "path": "src/d.py",
             "start_line": 1, "end_line": 9, "snippet_hash": "hs11d"},
        ))
        ev_u = rev.add_evidence(ev_mod.make_evidence(
            ns_unrelated, SourceKind.CODE, ContentKind.EXECUTABLE, ver_u.source_version_id,
            {"repo": "repo-s11-unrelated", "commit": "s11u1", "path": "src/u.py",
             "start_line": 1, "end_line": 9, "snippet_hash": "hs11u"},
        ))

        def entx(etype, key, title):
            r = rev.put_entity(EntityDraft(
                ns_x, etype, key, title, source_version_id=ver_x.source_version_id,
                aliases=(Alias(key, AliasOrigin.METADATA_ID),),
            ))
            ids[key] = r.target_id
            return r.target_id

        def ento(etype, key, title):
            r = rev.put_entity(EntityDraft(
                ns_other, etype, key, title, source_version_id=ver_other.source_version_id,
                aliases=(Alias(key, AliasOrigin.METADATA_ID),),
            ))
            ids[key] = r.target_id
            return r.target_id

        def entu(etype, key, title):
            r = rev.put_entity(EntityDraft(
                ns_unrelated, etype, key, title, source_version_id=ver_u.source_version_id,
                aliases=(Alias(key, AliasOrigin.METADATA_ID),),
            ))
            ids[key] = r.target_id
            return r.target_id

        cap_a = entx(EntityType.CAPABILITY, "CAP-A", "Capacidade A")
        rn_a = entx(EntityType.BUSINESS_RULE, "RN-A", "Regra A")
        cap_z = entx(EntityType.CAPABILITY, "CAP-Z", "Capacidade Z (isolada)")
        rn_z = entx(EntityType.BUSINESS_RULE, "RN-Z", "Regra Z")
        cap_d = ento(EntityType.CAPABILITY, "CAP-D", "Capacidade D")
        rn_d = ento(EntityType.BUSINESS_RULE, "RN-D", "Regra D")
        cap_u = entu(EntityType.CAPABILITY, "CAP-U", "Capacidade Unrelated")
        rn_u = entu(EntityType.BUSINESS_RULE, "RN-U", "Regra U")

        for ns, ev, s, reltype, target, ver in (
            (ns_x, ev_x, cap_a, RelationType.CONTAINS, rn_a, ver_x),
            (ns_x, ev_x, cap_a, RelationType.DEPENDS_ON, cap_d, ver_x),
            (ns_x, ev_x, cap_z, RelationType.CONTAINS, rn_z, ver_x),
            (ns_other, ev_other, cap_d, RelationType.CONTAINS, rn_d, ver_other),
            (ns_unrelated, ev_u, cap_u, RelationType.CONTAINS, rn_u, ver_u),
        ):
            rev.put_relation(RelationDraft(
                ns, s, reltype, target, scope="src", epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT, asserted_by="extractor:codescan",
                evidence_refs=(ev,), support_recorded_by="pipeline:verify-static",
                source_version_id=ver.source_version_id,
            ))

        for subj, pred, value, path, ev, ns, ver in (
            (rn_a, "condition", "condicao de A", "src/a.py", ev_x, ns_x, ver_x),
            (rn_a, "behavior", "comportamento de A", "src/a.py", ev_x, ns_x, ver_x),
            (rn_z, "condition", "condicao de Z", "src/a.py", ev_x, ns_x, ver_x),
            (rn_z, "behavior", "comportamento de Z", "src/a.py", ev_x, ns_x, ver_x),
            (rn_d, "condition", "condicao de D", "src/d.py", ev_other, ns_other, ver_other),
            (rn_d, "behavior", "comportamento de D", "src/d.py", ev_other, ns_other, ver_other),
            (rn_u, "condition", "condicao de U", "src/u.py", ev_u, ns_unrelated, ver_u),
            (rn_u, "behavior", "comportamento de U", "src/u.py", ev_u, ns_unrelated, ver_u),
        ):
            rev.put_fact(FactDraft(
                ns, subj, pred, value, path, FactNature.IMPLEMENTED,
                EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, asserted_by="extractor:codescan",
                evidence_refs=(ev,), source_version_id=ver.source_version_id,
                support_recorded_by="pipeline:verify-static",
            ))

        ev_ini = rev.add_evidence(ev_mod.make_evidence(
            ns_ini, SourceKind.DOCUMENT, ContentKind.PROSE, ver_ini.source_version_id,
            {"version": "v1", "section": "proposta-s11", "block": "p1"},
        ))

        def enti(etype, key, title):
            r = rev.put_entity(EntityDraft(
                ns_ini, etype, key, title, source_version_id=ver_ini.source_version_id,
                aliases=(Alias(key, AliasOrigin.METADATA_ID),),
            ))
            ids[key] = r.target_id
            return r.target_id

        ini_x = enti(EntityType.INITIATIVE, "INI-X", "Iniciativa X")
        rf_a = enti(EntityType.REFINEMENT, "RF-A", "Refinamento de A")
        for s, reltype, target, life in (
            (ini_x, RelationType.CONTAINS, rf_a, LifecycleStatus.CURRENT),
            (rf_a, RelationType.PROPOSES_CHANGE_TO, rn_a, LifecycleStatus.PROPOSED),
        ):
            rev.put_relation(RelationDraft(
                ns_ini, s, reltype, target, scope="ini-x", epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=life, asserted_by="extractor:ingest", evidence_refs=(ev_ini,),
                support_recorded_by="human:curadoria", source_version_id=ver_ini.source_version_id,
            ))
        rev.put_fact(FactDraft(
            ns_ini, ini_x, "objective", "unificar X", "INI-X", FactNature.DECLARED_REQUIREMENT,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, asserted_by="extractor:ingest",
            evidence_refs=(ev_ini,), source_version_id=ver_ini.source_version_id,
            support_recorded_by="human:curadoria",
        ))
        rev.put_fact(FactDraft(
            ns_ini, rf_a, "refinement", "detalha A", "INI-X", FactNature.DECLARED_REQUIREMENT,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, asserted_by="extractor:ingest",
            evidence_refs=(ev_ini,), source_version_id=ver_ini.source_version_id,
            support_recorded_by="human:curadoria",
        ))
        ids["revision_id"] = rev.revision_id
    return ids


class TestDeliveryCliSelectionFilters(unittest.TestCase):
    """T4/S11-15/16/17 (§11 "Seleção de entrega"): `delivery.eligibility`
    sem filtros, só `--repo`, só `--initiative` e ambos produzem conjuntos
    de documentos DIFERENTES; fechamento de 1 salto inclui dependência
    referenciada (mesmo cruzando repo); entidade referenciada sem documento
    próprio (Refinement — nenhum `_plan_*` monta documento para esse tipo)
    vira `external_entities`, nunca some em silêncio nem é incluída
    escondida."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wiki-ai-s11-")
        cls.store_root = os.path.join(cls.tmp, "store")
        os.makedirs(cls.store_root)
        cls.repo_dir = os.path.join(cls.tmp, "repo-s11-x")
        os.makedirs(cls.repo_dir)
        cls.ns_x = delivery._repo_namespace(os.path.abspath(cls.repo_dir))
        cls.ns_other = "code/repo-s11-other"
        cls.ns_ini = "wiki-s11"
        cls.ns_unrelated = "code/repo-s11-unrelated"
        cls.db_path = os.path.join(cls.store_root, "knowledge.db")
        cls.repo = Repository.open(cls.db_path)
        cls.ids = _seed_s11(cls.repo, cls.ns_x, cls.ns_other, cls.ns_ini, cls.ns_unrelated)
        cls.publication_root = os.path.join(cls.store_root, "publicacoes")

        plan_obj = plan_fn(cls.repo, cls.ids["revision_id"], namespace=None)
        result = release.publish_revision(
            plan_obj, cls.publication_root, _RealRenderers(), validate_mod.StagedValidators,
            revision_id=cls.ids["revision_id"],
        )
        assert result.ok, f"publicação de fixture falhou: {result.blocked_by}"

        cls.doc_a = document_id(cls.ns_x, DocKind.CAPACIDADE, cls.ids["CAP-A"])
        cls.doc_d = document_id(cls.ns_other, DocKind.CAPACIDADE, cls.ids["CAP-D"])
        cls.doc_z = document_id(cls.ns_x, DocKind.CAPACIDADE, cls.ids["CAP-Z"])
        cls.doc_u = document_id(cls.ns_unrelated, DocKind.CAPACIDADE, cls.ids["CAP-U"])
        cls.doc_ini = document_id(cls.ns_ini, DocKind.INICIATIVA, cls.ids["INI-X"])

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _elig(self, **kwargs):
        return delivery.eligibility(
            store_root=self.store_root, publication_root=self.publication_root, **kwargs
        )

    def test_sem_filtros_inclui_todos_os_documentos_publicados(self):
        elig = self._elig()
        docs = set(elig["documents"])
        self.assertIn(self.doc_a, docs)
        self.assertIn(self.doc_d, docs)
        self.assertIn(self.doc_z, docs)
        self.assertIn(self.doc_u, docs)
        self.assertIn(self.doc_ini, docs)
        external_ids = {e["entity_id"] for e in elig["external_entities"]}
        self.assertIn(self.ids["RF-A"], external_ids)

    def test_somente_repo_inclui_a_e_a_dependencia_fechada_exclui_repo_nao_relacionado(self):
        elig = self._elig(repo=self.repo_dir)
        docs = set(elig["documents"])
        self.assertIn(self.doc_a, docs, docs)
        self.assertIn(self.doc_z, docs, docs)  # mesmo namespace, sem filtro de iniciativa
        self.assertIn(self.doc_d, docs, docs)  # dependência de 1 salto, cruza repo
        self.assertNotIn(self.doc_u, docs, docs)  # repo totalmente não relacionado
        external_ids = {e["entity_id"] for e in elig["external_entities"]}
        self.assertIn(self.ids["RF-A"], external_ids)

    def test_somente_initiative_inclui_a_e_dependencia_exclui_z_e_repo_nao_relacionado(self):
        elig = self._elig(initiative=self.ids["INI-X"])
        docs = set(elig["documents"])
        self.assertIn(self.doc_ini, docs, docs)  # âncora da própria iniciativa
        self.assertIn(self.doc_a, docs, docs)  # pertence à iniciativa (proposta sobre RN-A)
        self.assertIn(self.doc_d, docs, docs)  # dependência de 1 salto a partir de A
        self.assertNotIn(self.doc_z, docs, docs)  # não pertence à iniciativa, não é dependência
        self.assertNotIn(self.doc_u, docs, docs)  # nem repo, nem iniciativa
        external_ids = {e["entity_id"] for e in elig["external_entities"]}
        self.assertIn(self.ids["RF-A"], external_ids)

    def test_ambos_e_a_iniciativa_no_contexto_do_repo_exclui_z_e_repo_nao_relacionado(self):
        elig = self._elig(repo=self.repo_dir, initiative=self.ids["INI-X"])
        docs = set(elig["documents"])
        self.assertIn(self.doc_a, docs, docs)  # namespace X E pertence à iniciativa
        self.assertIn(self.doc_d, docs, docs)  # dependência de 1 salto
        self.assertNotIn(self.doc_z, docs, docs)  # namespace X, mas fora da iniciativa
        self.assertNotIn(self.doc_u, docs, docs)  # nem repo X, nem iniciativa X


class TestUpdateExecutadoDuasVezesENoopNaSegunda(unittest.TestCase):
    """T3 estendido: `wk update --json` (comando de nível superior — não
    `delivery update`, que de fato não existe) executado depois de uma
    mudança real e, em seguida, SEM nenhuma mudança adicional, precisa ser
    idempotente: a segunda execução é `noop` explícito, nunca repete efeito
    (nova revisão/publicação) silenciosamente."""

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_update_duas_vezes_sem_mudanca_adicional_e_noop_na_segunda(self):
        tmp = tempfile.mkdtemp(prefix="wk-update-idem-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = os.path.join(tmp, "store")
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo, exist_ok=True)

        code0, out0, err0 = self._run(
            ["analyze", "--repo", repo, "--store", store, "--mode", "structural", "--json"]
        )
        self.assertEqual(err0, "", err0)
        self.assertNotEqual(json.loads(out0)["operation_status"], "blocked", out0)

        with open(os.path.join(repo, "novo.py"), "w", encoding="utf-8") as fh:
            fh.write("def nova():\n    return 1\n")

        code1, out1, err1 = self._run(
            ["update", "--repo", repo, "--store", store, "--mode", "structural", "--json"]
        )
        self.assertEqual(err1, "", err1)
        payload1 = json.loads(out1)
        self.assertNotEqual(payload1["operation_status"], "noop", payload1)

        code2, out2, err2 = self._run(
            ["update", "--repo", repo, "--store", store, "--mode", "structural", "--json"]
        )
        self.assertEqual(err2, "", err2)
        payload2 = json.loads(out2)
        self.assertEqual(payload2["operation_status"], "noop", payload2)
        self.assertFalse(payload2["summary"]["detail"]["mudou"], payload2)
        # T15/#1 (reprodução real, §10.3): "não mudou" preserva o estado
        # AGREGADO do escopo — a mesma leitura de `wk status --repo`
        # (`novo.py` sem executor `deep`/binding disponível nesta fixture
        # deixa o objetivo `partial`, nunca `not_applicable` por
        # "esta invocação não reavaliou nada").
        self.assertEqual(
            payload2["knowledge_status"],
            cli._ingest_repo_scope_status(store, os.path.abspath(repo))["knowledge_status"],
            payload2,
        )
        self.assertEqual(payload2["knowledge_status"], "partial", payload2)


if __name__ == "__main__":
    unittest.main()
