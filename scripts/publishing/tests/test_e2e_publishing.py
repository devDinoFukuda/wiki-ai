"""Teste de integração ponta a ponta REAL do pipeline de publicação (W6, cenário F).

knowledge.db temporário semeado (INI-008/DEC-017/RF-042/RN-023/CAP-023, sem
depender de ingestion) -> planner.plan(repo, revision_id) ->
release.publish_revision(plan, out_root, renderers, validators) com
renderers/validators de PRODUÇÃO (publishing.markdown/word/validate) — zero
fakes de conteúdo, zero adaptador de assinatura.

`publishing.release.Validators` (Protocol) pede
`validate_equivalence/validate_semantics/validate_docx_structure` como
`(plan, staging_root) -> Report-like`. `publishing.validate` expõe as
validações REAIS numa granularidade diferente (por par de manifesto ou por
documento):

    validate_equivalence(md_manifest, word_manifest, plan)   [validate.py:599]
    validate_semantics(document, rendered_md_text, docx_bytes) [validate.py:425]
    validate_docx_structure(docx_bytes)                        [validate.py:312]

Isso era uma incompatibilidade de assinatura ENTRE OS DOIS MÓDULOS DE
PRODUÇÃO (bug real de costura W6) — corrigida em `publishing.validate`, que
agora também expõe `validate_equivalence_staged`/`validate_semantics_staged`/
`validate_docx_structure_staged` (mesma assinatura `(plan, staging_root)` do
Protocol, implementadas em cima das funções acima) e `StagedValidators`, um
objeto que reúne as três sob os nomes exatos que `release.Validators` espera.
Este teste usa `validate.StagedValidators` DIRETO — o mesmo objeto que
`release.publish_revision` usa como padrão quando nenhum `validators` é
injetado — sem nenhum adaptador local.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import zipfile

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

from publishing import markdown as md_mod
from publishing import release
from publishing import validate as validate_mod
from publishing import word as word_mod
from publishing.document import UnitState, unit_id as make_unit_id
from publishing.planner import plan as plan_fn

NS = "acme/pagamentos-e2e"


# ---------------------------------------------------------------------------
# Seed: INI-008 / DEC-017 / RF-042 / RN-023 / CAP-023, direto em knowledge.db
# (mesmo cenário normativo de scratchpad/demo_w6_pub.py, trimado ao essencial)
# ---------------------------------------------------------------------------


def _seed(repo):
    code_src = repo.register_source(NS, SourceKind.CODE, "git://app")
    code_v = repo.register_source_version(code_src, "commit:abc123", "de" * 32)
    doc_src = repo.register_source(NS, SourceKind.DOCUMENT, "inbox/refinamento-rf042.md")
    doc_v = repo.register_source_version(doc_src, "v3", "ab" * 32)
    ids: dict = {}

    # ---------------------------------------------------------- revisão 1: código
    with repo.revision(author="pipeline:codescan", reason="snapshot de codigo") as rev:
        ev_rule = rev.add_evidence(
            ev_mod.make_evidence(
                NS,
                SourceKind.CODE,
                ContentKind.EXECUTABLE,
                code_v.source_version_id,
                {
                    "repo": "app",
                    "commit": "abc123",
                    "path": "src/auth/rules.py",
                    "start_line": 10,
                    "end_line": 24,
                    "snippet_hash": "h1",
                    "symbol": "check_attempts",
                },
            )
        )
        ev_flow = rev.add_evidence(
            ev_mod.make_evidence(
                NS,
                SourceKind.CODE,
                ContentKind.EXECUTABLE,
                code_v.source_version_id,
                {
                    "repo": "app",
                    "commit": "abc123",
                    "path": "src/auth/flow.py",
                    "start_line": 30,
                    "end_line": 58,
                    "snippet_hash": "h2",
                },
            )
        )
        ids["ev_rule"] = ev_rule
        ids["code_v"] = code_v.source_version_id

        def ent(etype, key, title, **kw):
            r = rev.put_entity(
                EntityDraft(
                    NS,
                    etype,
                    key,
                    title,
                    source_version_id=code_v.source_version_id,
                    aliases=(Alias(key, AliasOrigin.METADATA_ID),),
                    **kw,
                )
            )
            ids[key] = r.target_id
            return r.target_id

        sysid = ent(EntityType.SYSTEM, "SYS-PAG", "Plataforma de pagamentos Acme")
        cap = ent(EntityType.CAPABILITY, "CAP-023", "CAP-023 Autenticacao de parceiro")
        rn = ent(EntityType.BUSINESS_RULE, "RN-023", "RN-023 Limite de tentativas de autenticacao")
        flw = ent(EntityType.FLOW, "FLW-011", "FLW-011 Fluxo de bloqueio por tentativas")

        for src, typ, tgt in (
            (sysid, RelationType.CONTAINS, cap),
            (cap, RelationType.CONTAINS, rn),
            (cap, RelationType.CONTAINS, flw),
        ):
            rev.put_relation(
                RelationDraft(
                    NS,
                    src,
                    typ,
                    tgt,
                    scope="src/auth",
                    epistemic_status=EpistemicStatus.SUPPORTED,
                    lifecycle_status=LifecycleStatus.CURRENT,
                    asserted_by="extractor:codescan",
                    evidence_refs=(ev_flow,),
                    support_recorded_by="pipeline:verify-static",
                    source_version_id=code_v.source_version_id,
                )
            )

        def fact(subject, pred, value, evid=ev_rule, scope="src/auth/rules.py"):
            r = rev.put_fact(
                FactDraft(
                    NS,
                    subject,
                    pred,
                    value,
                    scope,
                    FactNature.IMPLEMENTED,
                    EpistemicStatus.SUPPORTED,
                    LifecycleStatus.CURRENT,
                    asserted_by="extractor:codescan",
                    evidence_refs=(evid,) if evid else (),
                    source_version_id=code_v.source_version_id,
                    support_recorded_by="pipeline:verify-static",
                )
            )
            return r.target_id

        ids["f_rn_cond"] = fact(
            rn, "condition", "a tentativa de autenticacao do parceiro falha por credencial invalida"
        )
        ids["f_rn_beh"] = fact(
            rn, "behavior", "bloqueia o parceiro apos 3 tentativas malsucedidas em 10 minutos"
        )
        ids["f_rn_exc"] = fact(
            rn,
            "exception",
            "quando o parceiro esta na lista de isencao o bloqueio nao se aplica",
        )
        fact(
            flw,
            "behavior",
            "registra o motivo do bloqueio no evento de auditoria",
            evid=ev_flow,
            scope="src/auth/flow.py",
        )

    # ------------------------------------------------------- revisão 2: iniciativa
    with repo.revision(author="pipeline:ingest", reason="inception e refinamento INI-008") as rev:
        ev_rf = rev.add_evidence(
            ev_mod.make_evidence(
                NS,
                SourceKind.DOCUMENT,
                ContentKind.PROSE,
                doc_v.source_version_id,
                {"version": "v3", "section": "proposta", "block": "p3"},
            )
        )

        def ent2(etype, key, title):
            r = rev.put_entity(
                EntityDraft(
                    NS,
                    etype,
                    key,
                    title,
                    source_version_id=doc_v.source_version_id,
                    aliases=(Alias(key, AliasOrigin.METADATA_ID),),
                )
            )
            ids[key] = r.target_id
            return r.target_id

        ini = ent2(EntityType.INITIATIVE, "INI-008", "INI-008 Autenticacao unificada de parceiros")
        dec = ent2(EntityType.DECISION, "DEC-017", "DEC-017 Autenticacao por certificado mutuo")
        rf = ent2(EntityType.REFINEMENT, "RF-042", "RF-042 Novo limite de tentativas")

        for src, typ, tgt, life in (
            (ini, RelationType.CONTAINS, dec, LifecycleStatus.CURRENT),
            (ini, RelationType.CONTAINS, rf, LifecycleStatus.CURRENT),
            (rf, RelationType.REFINES, dec, LifecycleStatus.CURRENT),
            (rf, RelationType.PROPOSES_CHANGE_TO, ids["RN-023"], LifecycleStatus.PROPOSED),
        ):
            rev.put_relation(
                RelationDraft(
                    NS,
                    src,
                    typ,
                    tgt,
                    scope="INI-008",
                    epistemic_status=EpistemicStatus.SUPPORTED,
                    lifecycle_status=life,
                    asserted_by="extractor:ingest",
                    evidence_refs=(ev_rf,),
                    support_recorded_by="human:curadoria",
                    source_version_id=doc_v.source_version_id,
                )
            )

        def dfact(subject, pred, value, life=LifecycleStatus.CURRENT, scope="INI-008"):
            return rev.put_fact(
                FactDraft(
                    NS,
                    subject,
                    pred,
                    value,
                    scope,
                    FactNature.DECLARED_REQUIREMENT,
                    EpistemicStatus.SUPPORTED,
                    life,
                    asserted_by="extractor:ingest",
                    evidence_refs=(ev_rf,),
                    source_version_id=doc_v.source_version_id,
                    support_recorded_by="human:curadoria",
                )
            ).target_id

        ids["f_rn_prop"] = dfact(
            ids["RN-023"],
            "proposed_behavior",
            "o limite passa a ser de 5 tentativas malsucedidas em 10 minutos",
            life=LifecycleStatus.PROPOSED,
            scope="RF-042",
        )
        dfact(ini, "objective", "unificar a autenticacao de parceiros do canal B2B em um unico fluxo")
        dfact(dec, "decision", "usar autenticacao por certificado mutuo em vez de token de curta duracao")
        dfact(rf, "refinement", "detalha DEC-017 elevando o limite de tentativas")
        ids["pub_revision"] = rev.revision_id

    return ids, ids["pub_revision"]


# ---------------------------------------------------------------------------
# Renderers REAIS — markdown.py/word.py já expõem exatamente o contrato que
# release.py pede (render/render_manifest, render/render_manifest_word), como
# atributos de módulo. Nenhum adaptador necessário aqui.
# ---------------------------------------------------------------------------


class _RealRenderers:
    markdown = md_mod
    word = word_mod


class _SabotagedMarkdown:
    """Markdown real, exceto que `render_manifest` derruba 1 unit_id — simula
    manifesto corrompido chegando em `release._render_all` (§10.5)."""

    def __init__(self, victim_unit_id: str):
        self._victim = victim_unit_id

    def render(self, document):
        return md_mod.render(document)

    def render_manifest(self, plan):
        manifest = dict(md_mod.render_manifest(plan))
        manifest.pop(self._victim, None)
        return manifest


class _SabotagedRenderers:
    def __init__(self, victim_unit_id: str):
        self.markdown = _SabotagedMarkdown(victim_unit_id)
        self.word = word_mod


# ---------------------------------------------------------------------------
# Auxiliares de asserção
# ---------------------------------------------------------------------------


def _all_markdown_text(out_root: str) -> str:
    parts = []
    md_dir = os.path.join(out_root, "markdown")
    for dirpath, _dirnames, filenames in os.walk(md_dir):
        for fn in filenames:
            with open(os.path.join(dirpath, fn), encoding="utf-8") as fh:
                parts.append(fh.read())
    return "\n".join(parts)


def _all_docx_text(out_root: str) -> str:
    parts = []
    word_dir = os.path.join(out_root, "word")
    for dirpath, _dirnames, filenames in os.walk(word_dir):
        for fn in filenames:
            if not fn.endswith(".docx"):
                continue
            path = os.path.join(dirpath, fn)
            with zipfile.ZipFile(path) as zf:
                assert zf.testzip() is None, f"{path}: entrada corrompida no zip"
            with open(path, "rb") as fh:
                parts.append(validate_mod.docx_text(fh.read()))
    return "\n".join(parts)


def _expected_unit_ids(plan) -> set:
    return {u.unit_id for d in plan.documents for u in d.units if u.is_publishable()}


# ---------------------------------------------------------------------------
# Teste
# ---------------------------------------------------------------------------


class TestE2EPublishing(unittest.TestCase):
    """Cenário F — pipeline de publicação ponta a ponta, 100% real."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wiki-ai-e2e-pub-")
        cls.db_path = os.path.join(cls.tmp, "knowledge.db")
        cls.repo = Repository.open(cls.db_path)
        cls.ids, cls.pub_rev = _seed(cls.repo)
        cls.out_root = os.path.join(cls.tmp, "out")
        cls.renderers = _RealRenderers()
        # Uso direto (prova da correção do bug de assinatura): mesmo objeto
        # que `release.publish_revision` usa por padrão quando `validators`
        # não é injetado — nenhum adaptador de assinatura neste arquivo.
        cls.validators = validate_mod.StagedValidators

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- 1. pipeline real completo -----------------------------------------

    def test_1_publish_real_pipeline(self):
        plan_v1 = plan_fn(self.repo, self.pub_rev, namespace=NS)
        self.assertGreater(len(plan_v1.documents), 0, "plano sem documentos: seed insuficiente")

        result = release.publish_revision(
            plan_v1, self.out_root, self.renderers, self.validators, revision_id="rel1"
        )
        self.assertTrue(result.ok, f"publish_revision falhou: {result.blocked_by}")
        self.assertEqual(result.revision_id, "rel1")
        self.assertIsNotNone(result.manifest)

        manifest_path = os.path.join(self.out_root, "manifest.json")
        self.assertTrue(os.path.isfile(manifest_path))
        import json

        with open(manifest_path, encoding="utf-8") as fh:
            manifest_json = json.load(fh)
        self.assertEqual(manifest_json["revision_id"], "rel1")
        expected_units = _expected_unit_ids(plan_v1)
        self.assertEqual(set(manifest_json["documents"].keys()), expected_units)
        self.assertGreater(len(expected_units), 0)

        # arquivos .md e .docx existem e batem com o manifesto
        for uid, entry in manifest_json["documents"].items():
            md_path = os.path.join(self.out_root, entry["md_path"])
            docx_path = os.path.join(self.out_root, entry["docx_path"])
            self.assertTrue(os.path.isfile(md_path), f"unidade {uid}: {md_path} ausente")
            self.assertTrue(os.path.isfile(docx_path), f"unidade {uid}: {docx_path} ausente")
            # .docx é zip válido
            with zipfile.ZipFile(docx_path) as zf:
                self.assertIsNone(zf.testzip(), f"{docx_path}: zip corrompido")
                self.assertIn("word/document.xml", zf.namelist())

        # conteúdo verbatim (D12/D13) chega às duas saídas reais
        md_text = _all_markdown_text(self.out_root)
        docx_text = _all_docx_text(self.out_root)
        self.assertIn("bloqueia o parceiro apos 3 tentativas malsucedidas em 10 minutos", md_text)
        self.assertIn("bloqueia o parceiro apos 3 tentativas malsucedidas em 10 minutos", docx_text)

        TestE2EPublishing._plan_v1 = plan_v1
        TestE2EPublishing._rel1_manifest = manifest_json

    # -- 2. republicação da MESMA revisão: nomes estáveis por unit_id -------

    def test_2_republish_same_revision_stable_filenames(self):
        plan_v1 = TestE2EPublishing._plan_v1
        result = release.publish_revision(
            plan_v1, self.out_root, self.renderers, self.validators, revision_id="rel2"
        )
        self.assertTrue(result.ok, f"republicação falhou: {result.blocked_by}")

        rel1_docs = TestE2EPublishing._rel1_manifest["documents"]
        rel2_docs = result.manifest.to_json()["documents"]
        self.assertEqual(set(rel1_docs), set(rel2_docs), "conjunto de unidades mudou sem alteração de fato")
        for uid, entry in rel1_docs.items():
            self.assertEqual(entry["md_path"], rel2_docs[uid]["md_path"], f"{uid}: md_path instável")
            self.assertEqual(entry["docx_path"], rel2_docs[uid]["docx_path"], f"{uid}: docx_path instável")

        TestE2EPublishing._rel2_manifest = result.manifest.to_json()

    # -- 3. segunda revisão com 1 fato alterado ------------------------------

    def test_3_second_revision_with_changed_fact(self):
        with self.repo.revision(
            author="human:curadoria", reason="RN-023: limite revisado apos incidente"
        ) as rev:
            rev.put_fact(
                FactDraft(
                    NS,
                    self.ids["RN-023"],
                    "behavior",
                    "bloqueia o parceiro apos 7 tentativas malsucedidas em 10 minutos",
                    "src/auth/rules.py",
                    FactNature.IMPLEMENTED,
                    EpistemicStatus.SUPPORTED,
                    LifecycleStatus.CURRENT,
                    asserted_by="human:curadoria",
                    evidence_refs=(self.ids["ev_rule"],),
                    source_version_id=self.ids["code_v"],
                    support_recorded_by="pipeline:verify-static",
                )
            )
            rev2_knowledge_id = rev.revision_id

        plan_v2 = plan_fn(self.repo, rev2_knowledge_id, namespace=NS)
        result = release.publish_revision(
            plan_v2, self.out_root, self.renderers, self.validators, revision_id="rel3"
        )
        self.assertTrue(result.ok, f"publicação da revisão com fato alterado falhou: {result.blocked_by}")

        # revisão anterior (rel2) arquivada em .history/
        hist_dir = os.path.join(self.out_root, ".history", "rel2")
        self.assertTrue(os.path.isdir(hist_dir), "rel2 não foi arquivada em .history/")
        archived_md = _all_markdown_text(hist_dir) if os.path.isdir(os.path.join(hist_dir, "markdown")) else ""
        self.assertIn(
            "bloqueia o parceiro apos 3 tentativas malsucedidas em 10 minutos",
            archived_md,
            "conteúdo antigo (rel2) não preservado em .history/rel2/ antes do overwrite",
        )

        # conteúdo NOVO nos arquivos ativos, valor antigo não aparece mais
        md_text = _all_markdown_text(self.out_root)
        docx_text = _all_docx_text(self.out_root)
        self.assertIn("bloqueia o parceiro apos 7 tentativas malsucedidas em 10 minutos", md_text)
        self.assertIn("bloqueia o parceiro apos 7 tentativas malsucedidas em 10 minutos", docx_text)
        self.assertNotIn("bloqueia o parceiro apos 3 tentativas malsucedidas em 10 minutos", md_text)
        self.assertNotIn("bloqueia o parceiro apos 3 tentativas malsucedidas em 10 minutos", docx_text)

        # manifesto novo aponta conjunto COMPLETO de unidades da revisão
        manifest_json = result.manifest.to_json()
        self.assertEqual(manifest_json["revision_id"], "rel3")
        self.assertEqual(set(manifest_json["documents"].keys()), _expected_unit_ids(plan_v2))

        TestE2EPublishing._plan_v2 = plan_v2
        TestE2EPublishing._rel3_manifest = manifest_json

    # -- 4. sabotagem: md_manifest corrompido bloqueia a publicação ---------

    def test_4_corrupted_markdown_manifest_blocks_publish(self):
        plan_v2 = TestE2EPublishing._plan_v2
        victim = next(iter(_expected_unit_ids(plan_v2)))
        sabotaged = _SabotagedRenderers(victim)

        before = release._load_manifest(self.out_root)
        self.assertEqual(before.revision_id, "rel3")

        result = release.publish_revision(
            plan_v2, self.out_root, sabotaged, self.validators, revision_id="rel-sabotage"
        )
        self.assertFalse(result.ok, "publicação com manifesto corrompido deveria ser bloqueada")
        self.assertTrue(
            any("ausente do manifesto markdown" in b for b in result.blocked_by),
            f"bloqueio esperado por equivalência §10.5, obtido: {result.blocked_by}",
        )

        # revisão anterior (rel3) permanece 100% intacta
        after = release._load_manifest(self.out_root)
        self.assertEqual(after.revision_id, "rel3")
        self.assertEqual(after.to_json(), TestE2EPublishing._rel3_manifest)
        md_text = _all_markdown_text(self.out_root)
        self.assertIn("bloqueia o parceiro apos 7 tentativas malsucedidas em 10 minutos", md_text)

        # staging não deixou lixo para trás
        leftovers = [
            d for d in os.listdir(self.tmp) if d.startswith(".out.staging-")
        ]
        self.assertEqual(leftovers, [], f"staging não removido: {leftovers}")

    # -- 5. rollback para a 1ª revisão: histórico incompleto, sem tocar nada ---

    def test_5_rollback_to_first_revision_blocks_on_incomplete_history(self):
        """BUG REAL corrigido (release.py: `_promote`/`rollback`): quando o
        conteúdo de uma unidade fica byte-idêntico através de uma
        republicação (rel1 -> rel2, cenário 2 acima), `_promote` só arquiva
        em `.history/<revisão>/` quando os bytes DIVERGEM — correto por si
        só (não há o que arquivar de um byte que nunca foi sobrescrito).
        Como rel1 -> rel2 não divergiu, nada de rel1 foi arquivado sob
        `.history/rel1/` — só o `manifest.json`. O byte original de rel1 só
        volta a ser arquivado quando finalmente muda, em rel2 -> rel3, mas
        sob `.history/rel2/` (`previous_revision_id` é a revisão IMEDIATAMENTE
        anterior, não a de origem do conteúdo).

        `rollback(out_root, "rel1")` não acha `.history/rel1/<path>`. Antes
        da correção, ausência ali virava "o arquivo ATIVO já é o de rel1"
        SEM checagem — mas o ativo está com o conteúdo de rel3 ("7
        tentativas"): `rollback` devolvia `ok=True` e restaurava o conteúdo
        ERRADO, violando o próprio contrato do módulo ("nunca aplica
        restauração parcial em silêncio").

        Corrigido: antes de aceitar "keep", `rollback` compara o hash do par
        ATIVO (md+docx, mesma função `_compute_doc_hash` usada para gravar
        `ManifestEntry.content_hash`) contra o hash gravado no manifesto de
        rel1. Diverge -> histórico insuficiente -> `rollback` FALHA com
        bloqueio explícito e não toca em nada (menor correção correta:
        restaurar o byte exato de rel1 exigiria reconstruir a cadeia
        completa de `.history/` entre revisões, fora do escopo deste fix).
        """
        before_manifest = release._load_manifest(self.out_root)
        self.assertEqual(before_manifest.revision_id, "rel3")
        before_manifest_json = before_manifest.to_json()
        before_md = _all_markdown_text(self.out_root)
        before_docx = _all_docx_text(self.out_root)

        rb = release.rollback(self.out_root, "rel1")

        self.assertFalse(rb.ok, "rollback deveria falhar: histórico incompleto para restaurar rel1 com integridade")
        self.assertTrue(rb.blocked_by, "rollback sem detalhe de bloqueio")
        self.assertTrue(
            any(
                "diverge do hash" in b or "histórico" in b or "incompleto" in b
                for b in rb.blocked_by
            ),
            f"bloqueio esperado por divergência de hash / histórico incompleto, obtido: {rb.blocked_by}",
        )
        self.assertEqual(rb.revision_id, "rel1")

        # nada foi tocado: manifesto e arquivos ativos continuam sendo os de rel3
        on_disk = release._load_manifest(self.out_root)
        self.assertEqual(on_disk.revision_id, "rel3")
        self.assertEqual(on_disk.to_json(), before_manifest_json)
        self.assertEqual(on_disk.to_json(), TestE2EPublishing._rel3_manifest)
        self.assertEqual(_all_markdown_text(self.out_root), before_md)
        self.assertEqual(_all_docx_text(self.out_root), before_docx)
        self.assertIn("bloqueia o parceiro apos 7 tentativas malsucedidas em 10 minutos", before_md)
        self.assertIn("bloqueia o parceiro apos 7 tentativas malsucedidas em 10 minutos", before_docx)

        # arquivos ativos continuam .docx válidos (rollback não corrompeu nada)
        for entry in on_disk.documents.values():
            docx_path = os.path.join(self.out_root, entry.docx_path)
            self.assertTrue(os.path.isfile(docx_path))
            with zipfile.ZipFile(docx_path) as zf:
                self.assertIsNone(zf.testzip())


if __name__ == "__main__":
    unittest.main()
