"""Testes de lacunas da especificacao — delivery (sec 9.4, 11).

Cobre requisitos específicos:

- T2/S9.4-06: delivery.json traz sha256 real dos arquivos
- T1/S9.4-08/T22-02: documento ainda referenciado pela última entrega
  CONFIRMADA de OUTRA seleção do MESMO destino nunca entra em `removed`;
  documento sem outra referência entra normalmente.
- T8/S9.4-09: reingestão do MESMO arquivo, após falha de publicação na
  primeira ingestão, tenta publicar de novo (nunca sai cedo por "duplicada").
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

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
from publishing.planner import plan as plan_fn

from wk import cli


class _RealRenderers:
    markdown = md_mod
    word = word_mod


class TestDeliveryJsonSha256(unittest.TestCase):
    """T2/S9.4-06: delivery.json traz sha256 real dos arquivos."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wiki-ai-sha256-test-")
        cls.store_root = os.path.join(cls.tmp, "store")
        os.makedirs(cls.store_root)
        cls.db_path = os.path.join(cls.store_root, "knowledge.db")
        cls.repo = Repository.open(cls.db_path)

        NS_local = "code/repo-sha256-test"
        src = cls.repo.register_source(NS_local, SourceKind.CODE, "git://app")
        ver = cls.repo.register_source_version(src, "commit:abc123", "de" * 32)
        with cls.repo.revision(author="pipeline:codescan", reason="snapshot") as rev:
            ev = rev.add_evidence(ev_mod.make_evidence(
                NS_local, SourceKind.CODE, ContentKind.EXECUTABLE, ver.source_version_id,
                {"repo": "app", "commit": "abc123", "path": "src/rules.py",
                 "start_line": 1, "end_line": 9, "snippet_hash": "h1"},
            ))

            sysid = rev.put_entity(EntityDraft(
                NS_local, EntityType.SYSTEM, "SYS-SHA", "Sistema SHA",
                source_version_id=ver.source_version_id,
                aliases=(Alias("SYS-SHA", AliasOrigin.METADATA_ID),),
            )).target_id
            cap = rev.put_entity(EntityDraft(
                NS_local, EntityType.CAPABILITY, "CAP-SHA", "Cap SHA",
                source_version_id=ver.source_version_id,
                aliases=(Alias("CAP-SHA", AliasOrigin.METADATA_ID),),
            )).target_id
            rn = rev.put_entity(EntityDraft(
                NS_local, EntityType.BUSINESS_RULE, "RN-SHA", "RN SHA",
                source_version_id=ver.source_version_id,
                aliases=(Alias("RN-SHA", AliasOrigin.METADATA_ID),),
            )).target_id

            for s, t, target in (
                (sysid, RelationType.CONTAINS, cap),
                (cap, RelationType.CONTAINS, rn),
            ):
                rev.put_relation(RelationDraft(
                    NS_local, s, t, target, scope="src",
                    epistemic_status=EpistemicStatus.SUPPORTED,
                    lifecycle_status=LifecycleStatus.CURRENT,
                    asserted_by="extractor:codescan",
                    evidence_refs=(ev,),
                    support_recorded_by="pipeline:verify-static",
                    source_version_id=ver.source_version_id,
                ))

            rev.put_fact(FactDraft(
                NS_local, rn, "condition", "condicao",
                "src/rules.py", FactNature.IMPLEMENTED,
                EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT,
                asserted_by="extractor:codescan", evidence_refs=(ev,),
                source_version_id=ver.source_version_id,
                support_recorded_by="pipeline:verify-static",
            ))
            rev.put_fact(FactDraft(
                NS_local, rn, "behavior", "comportamento",
                "src/rules.py", FactNature.IMPLEMENTED,
                EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT,
                asserted_by="extractor:codescan", evidence_refs=(ev,),
                source_version_id=ver.source_version_id,
                support_recorded_by="pipeline:verify-static",
            ))
            cls.revision_id = rev.revision_id

        cls.publication_root = os.path.join(cls.store_root, "publicacoes")
        plan_obj = plan_fn(cls.repo, cls.revision_id, namespace=None)
        result = release.publish_revision(
            plan_obj, cls.publication_root, _RealRenderers(), validate_mod.StagedValidators,
            revision_id=cls.revision_id,
        )
        assert result.ok, f"publicacao falhou: {result.blocked_by}"

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_sha256_recalculado_bate_com_arquivo(self):
        """delivery.json traz sha256; recalcular deve dar o mesmo valor."""
        out = os.path.join(self.tmp, "dlv-sha256")
        delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination="sha256-dest", out_dir=out,
        )
        dlv_json = os.path.join(out, "delivery.json")
        with open(dlv_json, encoding="utf-8") as f:
            dlv = json.load(f)

        for doc in dlv["documents"]:
            for field in ("markdown", "word"):
                if field in doc and field + "_file" in doc:
                    file_info = doc[field + "_file"]
                    file_path = os.path.join(out, field, file_info["path"])
                    self.assertTrue(os.path.isfile(file_path))
                    import hashlib
                    h = hashlib.sha256()
                    with open(file_path, "rb") as f:
                        for chunk in iter(lambda: f.read(1048576), b""):
                            h.update(chunk)
                    actual = h.hexdigest()
                    expected = file_info["sha256"]
                    self.assertEqual(actual, expected,
                                    f"sha256 diverge: {actual} != {expected}")


# ---------------------------------------------------------------------------
# T1/S9.4-08/T22-02: `removed` respeita seleção confirmada de OUTRO destino
# ---------------------------------------------------------------------------


def _seed_t22(repo, ns_x, ns_other, ns_ini):
    """CAP-KEEP (revisão 1, "antiga") + CAP-D1/CAP-D2 (repo X) e CAP-D3
    (outro repo), com INI-Y referenciando CAP-D2 e CAP-D3 (revisão 2, mais
    recente). Revisão 1 é BACKDATED — `RevisionScope.contains` é por
    instante (`publishing.document.RevisionScope`, resolução de segundos) —
    para que republicar em `ids["rev1"]` reproduza de forma determinística
    um plano ANTERIOR onde D1/D2/D3/INI-Y ainda não existiam, sem depender
    de `time.sleep`."""
    ids: dict = {}
    src_x = repo.register_source(ns_x, SourceKind.CODE, "git://repo-t22-x")
    ver_x = repo.register_source_version(src_x, "commit:t22x1", "a1" * 32)
    src_other = repo.register_source(ns_other, SourceKind.CODE, "git://repo-t22-other")
    ver_other = repo.register_source_version(src_other, "commit:t22o1", "b2" * 32)
    src_ini = repo.register_source(ns_ini, SourceKind.DOCUMENT, "doc://t22-ini")
    ver_ini = repo.register_source_version(src_ini, "v1", "c3" * 32)

    with repo.revision(author="pipeline:codescan", reason="t22 baseline") as rev:
        ev1 = rev.add_evidence(ev_mod.make_evidence(
            ns_x, SourceKind.CODE, ContentKind.EXECUTABLE, ver_x.source_version_id,
            {"repo": "repo-t22-x", "commit": "t22x1", "path": "src/keep.py",
             "start_line": 1, "end_line": 5, "snippet_hash": "hkeep"},
        ))

        def ent1(etype, key, title):
            r = rev.put_entity(EntityDraft(
                ns_x, etype, key, title, source_version_id=ver_x.source_version_id,
                aliases=(Alias(key, AliasOrigin.METADATA_ID),),
            ))
            ids[key] = r.target_id
            return r.target_id

        cap_keep = ent1(EntityType.CAPABILITY, "CAP-KEEP", "Capacidade estavel")
        rn_keep = ent1(EntityType.BUSINESS_RULE, "RN-KEEP", "Regra estavel")
        rev.put_relation(RelationDraft(
            ns_x, cap_keep, RelationType.CONTAINS, rn_keep, scope="src",
            epistemic_status=EpistemicStatus.SUPPORTED, lifecycle_status=LifecycleStatus.CURRENT,
            asserted_by="extractor:codescan", evidence_refs=(ev1,),
            support_recorded_by="pipeline:verify-static", source_version_id=ver_x.source_version_id,
        ))
        for pred, value in (("condition", "condicao estavel"), ("behavior", "comportamento estavel")):
            rev.put_fact(FactDraft(
                ns_x, rn_keep, pred, value, "src/keep.py", FactNature.IMPLEMENTED,
                EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, asserted_by="extractor:codescan",
                evidence_refs=(ev1,), source_version_id=ver_x.source_version_id,
                support_recorded_by="pipeline:verify-static",
            ))
        ids["rev1"] = rev.revision_id

    # Backdate deterministico (ver docstring acima).
    backdated = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    repo.conn.execute("UPDATE revisions SET created_at=? WHERE revision_id=?", (backdated, ids["rev1"]))
    repo.conn.commit()

    with repo.revision(author="pipeline:codescan", reason="t22 d1 d2 d3") as rev:
        ev2 = rev.add_evidence(ev_mod.make_evidence(
            ns_x, SourceKind.CODE, ContentKind.EXECUTABLE, ver_x.source_version_id,
            {"repo": "repo-t22-x", "commit": "t22x1", "path": "src/d.py",
             "start_line": 1, "end_line": 9, "snippet_hash": "hd"},
        ))
        ev3 = rev.add_evidence(ev_mod.make_evidence(
            ns_other, SourceKind.CODE, ContentKind.EXECUTABLE, ver_other.source_version_id,
            {"repo": "repo-t22-other", "commit": "t22o1", "path": "src/d3.py",
             "start_line": 1, "end_line": 9, "snippet_hash": "hd3"},
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

        cap_d1 = entx(EntityType.CAPABILITY, "CAP-D1", "Capacidade D1")
        rn_d1 = entx(EntityType.BUSINESS_RULE, "RN-D1", "Regra D1")
        cap_d2 = entx(EntityType.CAPABILITY, "CAP-D2", "Capacidade D2")
        rn_d2 = entx(EntityType.BUSINESS_RULE, "RN-D2", "Regra D2")
        cap_d3 = ento(EntityType.CAPABILITY, "CAP-D3", "Capacidade D3")
        rn_d3 = ento(EntityType.BUSINESS_RULE, "RN-D3", "Regra D3")

        for ns, ev, s, reltype, target, ver in (
            (ns_x, ev2, cap_d1, RelationType.CONTAINS, rn_d1, ver_x),
            (ns_x, ev2, cap_d2, RelationType.CONTAINS, rn_d2, ver_x),
            (ns_other, ev3, cap_d3, RelationType.CONTAINS, rn_d3, ver_other),
        ):
            rev.put_relation(RelationDraft(
                ns, s, reltype, target, scope="src", epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT, asserted_by="extractor:codescan",
                evidence_refs=(ev,), support_recorded_by="pipeline:verify-static",
                source_version_id=ver.source_version_id,
            ))

        for subj, pred, value, path, ev, ns, ver in (
            (rn_d1, "condition", "condicao de D1", "src/d.py", ev2, ns_x, ver_x),
            (rn_d1, "behavior", "comportamento de D1", "src/d.py", ev2, ns_x, ver_x),
            (rn_d2, "condition", "condicao de D2", "src/d.py", ev2, ns_x, ver_x),
            (rn_d2, "behavior", "comportamento de D2", "src/d.py", ev2, ns_x, ver_x),
            (rn_d3, "condition", "condicao de D3", "src/d3.py", ev3, ns_other, ver_other),
            (rn_d3, "behavior", "comportamento de D3", "src/d3.py", ev3, ns_other, ver_other),
        ):
            rev.put_fact(FactDraft(
                ns, subj, pred, value, path, FactNature.IMPLEMENTED,
                EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, asserted_by="extractor:codescan",
                evidence_refs=(ev,), source_version_id=ver.source_version_id,
                support_recorded_by="pipeline:verify-static",
            ))

        ev_ini = rev.add_evidence(ev_mod.make_evidence(
            ns_ini, SourceKind.DOCUMENT, ContentKind.PROSE, ver_ini.source_version_id,
            {"version": "v1", "section": "proposta-t22", "block": "p1"},
        ))

        def enti(etype, key, title):
            r = rev.put_entity(EntityDraft(
                ns_ini, etype, key, title, source_version_id=ver_ini.source_version_id,
                aliases=(Alias(key, AliasOrigin.METADATA_ID),),
            ))
            ids[key] = r.target_id
            return r.target_id

        ini_y = enti(EntityType.INITIATIVE, "INI-Y", "Iniciativa Y")
        rf_d2 = enti(EntityType.REFINEMENT, "RF-Y-D2", "Refinamento Y sobre D2")
        rf_d3 = enti(EntityType.REFINEMENT, "RF-Y-D3", "Refinamento Y sobre D3")

        for s, reltype, target, life in (
            (ini_y, RelationType.CONTAINS, rf_d2, LifecycleStatus.CURRENT),
            (ini_y, RelationType.CONTAINS, rf_d3, LifecycleStatus.CURRENT),
            (rf_d2, RelationType.PROPOSES_CHANGE_TO, rn_d2, LifecycleStatus.PROPOSED),
            (rf_d3, RelationType.PROPOSES_CHANGE_TO, rn_d3, LifecycleStatus.PROPOSED),
        ):
            rev.put_relation(RelationDraft(
                ns_ini, s, reltype, target, scope="ini-y", epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=life, asserted_by="extractor:ingest", evidence_refs=(ev_ini,),
                support_recorded_by="human:curadoria", source_version_id=ver_ini.source_version_id,
            ))
        rev.put_fact(FactDraft(
            ns_ini, ini_y, "objective", "unificar Y", "INI-Y", FactNature.DECLARED_REQUIREMENT,
            EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, asserted_by="extractor:ingest",
            evidence_refs=(ev_ini,), source_version_id=ver_ini.source_version_id,
            support_recorded_by="human:curadoria",
        ))
        for rf, label in ((rf_d2, "D2"), (rf_d3, "D3")):
            rev.put_fact(FactDraft(
                ns_ini, rf, "refinement", f"detalha {label}", "INI-Y", FactNature.DECLARED_REQUIREMENT,
                EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, asserted_by="extractor:ingest",
                evidence_refs=(ev_ini,), source_version_id=ver_ini.source_version_id,
                support_recorded_by="human:curadoria",
            ))
        ids["rev2"] = rev.revision_id
    return ids


class TestDeliveryRemovalRespectsOtherConfirmedSelection(unittest.TestCase):
    """T1/S9.4-08/T22-02 (§9.4/§11): documento que sai do plano de uma
    seleção só entra em `changes.removed` se NENHUMA outra seleção
    CONFIRMADA do mesmo destino ainda o referencia."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wiki-ai-t22-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store_root = os.path.join(self.tmp, "store")
        os.makedirs(self.store_root)
        self.dir_x = os.path.join(self.tmp, "repo-t22-x")
        os.makedirs(self.dir_x)
        self.ns_x = delivery._repo_namespace(os.path.abspath(self.dir_x))
        self.ns_other = "code/repo-t22-other"
        self.ns_ini = "wiki-t22"
        self.db_path = os.path.join(self.store_root, "knowledge.db")
        self.repo = Repository.open(self.db_path)
        self.addCleanup(self.repo.close)
        self.ids = _seed_t22(self.repo, self.ns_x, self.ns_other, self.ns_ini)
        self.publication_root = os.path.join(self.store_root, "publicacoes")

    def _out(self, name):
        return os.path.join(self.tmp, "out-t22-" + name)

    def test_d2_referenciado_por_outra_selecao_nao_e_removido_d1_e(self):
        # 1) publica a revisão COMPLETA (D1, D2, D3, INI-Y existem).
        plan_full = plan_fn(self.repo, self.ids["rev2"], namespace=None)
        result = release.publish_revision(
            plan_full, self.publication_root, _RealRenderers(), validate_mod.StagedValidators,
            revision_id=self.ids["rev2"],
        )
        self.assertTrue(result.ok, result.blocked_by)

        dest = "sp"
        p_a1 = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest, out_dir=self._out("a1"), repo=self.dir_x,
        )
        delivery.confirm(
            store_root=self.store_root, delivery_id=p_a1["delivery_id"], destination=dest,
            verified_by="op.a",
        )
        p_b1 = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest, out_dir=self._out("b1"), initiative=self.ids["INI-Y"],
        )
        delivery.confirm(
            store_root=self.store_root, delivery_id=p_b1["delivery_id"], destination=dest,
            verified_by="op.b",
        )
        # sanity: B (iniciativa Y) trouxe mais de um documento (INI-Y +
        # dependências fechadas em 1 salto — D2 e D3).
        self.assertGreaterEqual(len(p_b1["documents"]), 2, p_b1)

        # 2) republica a revisão ANTERIOR (só CAP-KEEP existia) — o plano de
        # A "perde" D1 e D2 (que nasceram na revisão 2, mais recente).
        plan_partial = plan_fn(self.repo, self.ids["rev1"], namespace=None)
        result2 = release.publish_revision(
            plan_partial, self.publication_root, _RealRenderers(), validate_mod.StagedValidators,
            revision_id=self.ids["rev1"],
        )
        self.assertTrue(result2.ok, result2.blocked_by)

        p_a2 = delivery.prepare(
            store_root=self.store_root, publication_root=self.publication_root,
            destination=dest, out_dir=self._out("a2"), repo=self.dir_x,
        )

        paths_a1 = {(d["markdown"], d["word"]) for d in p_a1["documents"]}
        paths_a2 = {(d["markdown"], d["word"]) for d in p_a2["documents"]}
        paths_b1 = {(d["markdown"], d["word"]) for d in p_b1["documents"]}
        dropped = paths_a1 - paths_a2
        self.assertGreaterEqual(len(dropped), 2, dropped)  # ao menos D1 e D2 saíram do plano de A

        removed = {tuple(x) for x in p_a2["changes"]["removed"]}
        still_referenced_by_b = dropped & paths_b1
        not_referenced_anywhere = dropped - paths_b1

        self.assertTrue(still_referenced_by_b, "esperado ao menos um doc (D2) ainda referenciado por B")
        self.assertTrue(not_referenced_anywhere, "esperado ao menos um doc (D1) sem outra referência")
        self.assertFalse(
            still_referenced_by_b & removed,
            "documento ainda referenciado pela entrega confirmada de outra seleção do mesmo "
            "destino não pode entrar em `changes.removed` (§9.4/T22-02)",
        )
        self.assertLessEqual(
            not_referenced_anywhere, removed,
            "documento sem nenhuma outra referência deveria entrar em `changes.removed`",
        )
        # CAP-KEEP nunca deveria ter sumido nem ter sido marcado como removido.
        self.assertEqual(paths_a1 & paths_a2, paths_a1 - dropped)


# ---------------------------------------------------------------------------
# T8/S9.4-09: reingestão do mesmo arquivo após falha de publicação
# ---------------------------------------------------------------------------


class TestDeliveryReingestAfterFailure(unittest.TestCase):
    """T8/S9.4-09: primeira ingestão falha ao publicar (monkeypatch em
    `publishing.release.publish_revision`); reingestão do MESMO arquivo
    (conteúdo idêntico) precisa tentar publicar de novo — nunca sair cedo
    por "fonte duplicada" sem sequer tentar."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wiki-ai-reingest-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "store")
        self.src = os.path.join(self.tmp, "doc.md")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("# Documento de teste\n\nConteudo qualquer para ingestao.\n")

    def _run(self, argv):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_reingestao_do_mesmo_arquivo_tenta_publicar_de_novo_apos_falha(self):
        import publishing.release as pub_release_mod

        original_publish = pub_release_mod.publish_revision
        calls = {"n": 0}

        def _fake_publish(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("falha simulada de publicacao (S9.4-09)")
            return original_publish(*args, **kwargs)

        with mock.patch("publishing.release.publish_revision", side_effect=_fake_publish):
            code1, out1, err1 = self._run(["ingest", self.src, "--store", self.store, "--json"])
            self.assertEqual(err1, "", err1)
            payload1 = json.loads(out1)
            self.assertEqual(calls["n"], 1, "primeira ingestão deveria ter tentado publicar")
            self.assertIn("publicacoes", payload1["summary"]["detail"], payload1)
            self.assertTrue(
                payload1["summary"]["detail"]["publicacoes"].get("bloqueios"),
                "primeira publicação deveria estar bloqueada (falha simulada registrada)",
            )

            # reingestão do MESMO arquivo, sem nenhuma mudança de conteúdo.
            code2, out2, err2 = self._run(["ingest", self.src, "--store", self.store, "--json"])
            self.assertEqual(err2, "", err2)
            payload2 = json.loads(out2)

        self.assertEqual(
            calls["n"], 2,
            "reingestão do mesmo arquivo, após falha de publicação anterior, precisa tentar "
            "publicar de novo — código atual sai cedo por 'fonte duplicada' antes de chegar em "
            "`_publish_local` (scripts/wk/cli.py: guarda `if result.revision_id:` na linha 8737 "
            "nunca acumula revisão em `revisoes` para uma correlação `duplicate`, e o guard "
            "`if revisoes:` na linha 8790 pula `_publish_local` inteiro na reingestão idêntica)",
        )
        self.assertIn(
            "publicacoes", payload2["summary"]["detail"],
            "reingestão idêntica não gerou nenhuma tentativa de publicação nova — saída "
            "'duplicada sem efeito' silenciosa, nunca reportando nova tentativa (§9.4-09)",
        )


if __name__ == "__main__":
    unittest.main()
