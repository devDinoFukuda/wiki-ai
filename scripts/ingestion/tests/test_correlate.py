"""Testes de correlate.py — cenarios normativos do plano (§8.2-§8.4).

Contra `knowledge.db` REAL (tempfile), sem mocks: cada teste abre um banco
proprio em setUp/tearDown e chama `ingestion.correlate.correlate`/`correlate_batch`
de verdade. A API é lida direto de `correlate.py`, `extract.py` e
`knowledge.{models,repository,evidence,identity}` — nenhuma suposição de
contrato fora do que esses módulos realmente expõem.

`SourceDocument`/`Block` aqui são dataclasses de teste (duck-typing: `correlate`
lê tudo via `_attr`, então qualquer objeto com esses atributos serve) — não são
`ingestion.normalize.SourceDocument`. O pipeline com o `SourceDocument` REAL
(via `ingestion.ingest`) é coberto em `test_integration.py`.
"""

import hashlib
import os
import shutil
import tempfile
import unittest
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from knowledge import evidence as ev_mod
from knowledge import identity
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
    SourceKind,
)
from knowledge.repository import Repository

from ingestion import correlate as corr
from ingestion.extract import extract_candidates

NS = "acme/pagamentos-test"


# --------------------------------------------------------------------------
# Contrato mínimo de SourceDocument/Block, só para estes testes (ver docstring)
# --------------------------------------------------------------------------


@dataclass
class Block:
    block_id: str
    kind: str
    text: str
    locator: Mapping[str, Any] = field(default_factory=dict)

    def get(self, key, default=None):
        return self.locator.get(key, default)


@dataclass
class SourceDocument:
    source_id: str
    path_original: str
    kind: str
    bytes_sha256: str
    size: int
    metadata: Mapping[str, Any]
    blocks: Sequence[Block]
    status: str = "ok"
    diagnostics: Sequence[str] = ()


def make_doc(path, kind, metadata, blocks):
    payload = "\n".join(b.text for b in blocks)
    sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return SourceDocument(
        source_id=f"src::{path}",
        path_original=path,
        kind=kind,
        bytes_sha256=sha,
        size=len(payload),
        metadata=metadata,
        blocks=blocks,
    )


def tblock(i, text, speaker=None, t0=None, t1=None):
    loc: dict[str, Any] = {}
    if speaker:
        loc["speaker"] = speaker
    if t0:
        loc["time_start"], loc["time_end"] = t0, t1
    return Block(block_id=f"b{i}", kind="utterance", text=text, locator=loc)


def mblock(i, text, section="corpo"):
    return Block(block_id=f"p{i}", kind="paragraph", text=text, locator={"section": section})


# --------------------------------------------------------------------------
# Cenários normativos (§8.2-§8.4) contra knowledge.db real
# --------------------------------------------------------------------------


class CorrelateScenariosTestCase(unittest.TestCase):
    """Cada teste é independente: banco tempfile próprio em setUp/tearDown."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "knowledge.db")
        self.repo = Repository.open(self.db_path)
        self.ns = NS

    def tearDown(self):
        self.repo.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- helpers de cenário ------------------------------------------------

    def _correlate(self, document):
        candidates = extract_candidates(document)
        return corr.correlate(candidates, document, self.repo, self.ns)

    def _seed_rn023_implemented(self):
        """RN-023 com fato `implemented` via pipeline de código (evidência executável real)."""
        csrc = self.repo.register_source(self.ns, SourceKind.CODE, "git://app")
        csv_ = self.repo.register_source_version(csrc, "commit:abc123", "deadbeef" * 8)
        with self.repo.revision(author="pipeline:codescan", reason="snapshot de codigo") as rev:
            ev = ev_mod.make_evidence(
                self.ns,
                SourceKind.CODE,
                ContentKind.EXECUTABLE,
                csv_.source_version_id,
                {
                    "repo": "app",
                    "commit": "abc123",
                    "path": "src/rules.py",
                    "start_line": 10,
                    "end_line": 24,
                    "snippet_hash": "h1",
                },
            )
            eid = rev.add_evidence(ev)
            rn = rev.put_entity(
                EntityDraft(
                    self.ns,
                    EntityType.BUSINESS_RULE,
                    "RN-023",
                    "RN-023 Limite de tentativas",
                    source_version_id=csv_.source_version_id,
                    aliases=(Alias("RN-023", AliasOrigin.METADATA_ID),),
                    evidence_refs=(eid,),
                )
            )
            f = rev.put_fact(
                FactDraft(
                    self.ns,
                    rn.target_id,
                    "behavior",
                    "bloqueia apos 3 tentativas",
                    "src/rules.py",
                    FactNature.IMPLEMENTED,
                    EpistemicStatus.SUPPORTED,
                    LifecycleStatus.CURRENT,
                    asserted_by="extractor:codescan",
                    evidence_refs=(eid,),
                    source_version_id=csv_.source_version_id,
                    support_recorded_by="pipeline:verify-static",
                )
            )
        return rn.target_id, f.target_id

    def _seed_dec017_current(self):
        """DEC-017 vigente via inception real (marcador de decisão, fase != refinamento)."""
        t1 = make_doc(
            "inbox/inception-t1.vtt",
            "vtt",
            {"initiative_id": "INI-008", "phase": "inception", "title": "Inception INI-008"},
            [
                tblock(
                    1,
                    "Decidimos, em DEC-017, usar autenticacao por token de curta duracao.",
                    "ana",
                    "00:01:02",
                    "00:01:20",
                )
            ],
        )
        self._correlate(t1)
        dec_id = identity.resolve_identity(self.repo.conn, self.ns, EntityType.DECISION, "DEC-017")
        self.assertIsNotNone(dec_id)
        return dec_id

    # -- cenário 1: fonte duplicada não duplica fato/aresta (§8.2.1) -------

    def test_scenario1_duplicate_source_no_new_facts_or_edges(self):
        t1 = make_doc(
            "inbox/inception-t1.vtt",
            "vtt",
            {"initiative_id": "INI-008", "phase": "inception", "title": "Inception INI-008"},
            [
                tblock(
                    1,
                    "Decidimos, em DEC-017, usar autenticacao por token de curta duracao.",
                    "ana",
                    "00:01:02",
                    "00:01:20",
                ),
                tblock(2, "O servico deve recusar tentativas acima do limite definido.", "bruno"),
            ],
        )
        first = self._correlate(t1)
        self.assertFalse(first.duplicate)
        self.assertTrue(first.correlated)

        second = self._correlate(t1)  # mesmo objeto: mesmos bytes/hash
        self.assertTrue(second.duplicate)
        self.assertEqual(second.facts_written, ())
        self.assertEqual(second.relations_written, ())

    # -- cenário 2: refines + proposes_change_to, ambas proposed (§8.3) ----

    def test_scenario2_refinement_creates_refines_and_proposes_change_to_proposed(self):
        self._seed_rn023_implemented()
        self._seed_dec017_current()

        rf = make_doc(
            "inbox/refinamento-rf042.md",
            "md",
            {"initiative_id": "INI-008", "phase": "refinement", "title": "RF-042"},
            [
                mblock(1, "Refinamento RF-042 detalha a decisao DEC-017 para a proxima entrega.", "resumo"),
                mblock(2, "Propomos alterar RN-023: o limite passa a ser de 5 tentativas.", "proposta"),
            ],
        )
        res = self._correlate(rf)
        self.assertIsNone(res.error)

        refines = [r for r in res.relations_written if r.relation_type == "refines"]
        proposes = [r for r in res.relations_written if r.relation_type == "proposes_change_to"]
        self.assertEqual(len(refines), 1)
        self.assertEqual(refines[0].lifecycle, "proposed")
        self.assertEqual(len(proposes), 1)
        self.assertEqual(proposes[0].lifecycle, "proposed")

        dec_id = identity.resolve_identity(self.repo.conn, self.ns, EntityType.DECISION, "DEC-017")
        rn_id = identity.resolve_identity(self.repo.conn, self.ns, EntityType.BUSINESS_RULE, "RN-023")
        self.assertEqual(refines[0].target_entity_id, dec_id)
        self.assertEqual(proposes[0].target_entity_id, rn_id)

    # -- cenário 3: fato implemented não muda de revisão (§8.3, aceite W5) -

    def test_scenario3_implemented_fact_keeps_same_revision_after_refinement(self):
        rn_id, rn_fact_id = self._seed_rn023_implemented()
        self._seed_dec017_current()
        before = self.repo.get_fact(rn_fact_id)
        self.assertEqual(before.nature, FactNature.IMPLEMENTED)
        self.assertEqual(before.support_recorded_by, "pipeline:verify-static")

        rf = make_doc(
            "inbox/refinamento-rf042.md",
            "md",
            {"initiative_id": "INI-008", "phase": "refinement", "title": "RF-042"},
            [
                mblock(1, "Refinamento RF-042 detalha a decisao DEC-017 para a proxima entrega.", "resumo"),
                mblock(2, "Propomos alterar RN-023: o limite passa a ser de 5 tentativas.", "proposta"),
            ],
        )
        self._correlate(rf)

        after = self.repo.get_fact(rn_fact_id)
        self.assertEqual(after.revision_id, before.revision_id)
        self.assertEqual(after.nature, FactNature.IMPLEMENTED)
        self.assertEqual(after.value, before.value)
        self.assertEqual(len(self.repo.fact_history(rn_fact_id)), 1)

    # -- cenário 4: reingestão com conteúdo novo → 2 revisões, ainda proposed

    def test_scenario4_reingestion_with_new_content_creates_second_revision_still_proposed(self):
        rf_blocks = [mblock(1, "Refinamento RF-042 detalha a decisao DEC-017 para a proxima entrega.", "resumo")]
        rf1 = make_doc(
            "inbox/refinamento-rf042.md",
            "md",
            {"initiative_id": "INI-008", "phase": "refinement", "title": "RF-042"},
            rf_blocks,
        )
        self._correlate(rf1)
        rf_id = identity.resolve_identity(self.repo.conn, self.ns, EntityType.REFINEMENT, "RF-042")
        self.assertIsNotNone(rf_id)

        rf2_blocks = rf_blocks + [
            mblock(2, "Revisao: o registro do motivo deve conter o identificador do parceiro.", "requisitos")
        ]
        rf2 = make_doc(
            "inbox/refinamento-rf042.md",
            "md",
            {"initiative_id": "INI-008", "phase": "refinement", "title": "RF-042"},
            rf2_blocks,
        )
        res2 = self._correlate(rf2)
        self.assertFalse(res2.duplicate)

        same_id = identity.resolve_identity(self.repo.conn, self.ns, EntityType.REFINEMENT, "RF-042")
        self.assertEqual(rf_id, same_id)

        history = self.repo.entity_history(rf_id)
        self.assertEqual(len(history), 2)

        entity = self.repo.get_entity(rf_id, lifecycle=None)
        self.assertEqual(entity.lifecycle_status, LifecycleStatus.PROPOSED)

    # -- cenário 5: fonte sem id, 2 iniciativas candidatas, sem fusão ------

    def test_scenario5_ambiguous_source_without_id_yields_candidate_edges_no_merge(self):
        t1 = make_doc(
            "inbox/inception-t1.vtt",
            "vtt",
            {"initiative_id": "INI-008", "phase": "inception", "title": "Inception INI-008"},
            [
                tblock(
                    1,
                    "Contexto: a iniciativa INI-008 trata da autenticacao unificada de parceiros.",
                    "ana",
                    "00:00:10",
                    "00:00:19",
                )
            ],
        )
        self._correlate(t1)
        with self.repo.revision(author="human:curadoria", reason="segunda iniciativa homonima") as rev:
            rev.put_entity(
                EntityDraft(
                    self.ns,
                    EntityType.INITIATIVE,
                    "INI-009",
                    "INI-009 autenticacao unificada de parceiros",
                    aliases=(Alias("INI-009", AliasOrigin.METADATA_ID),),
                )
            )

        amb = make_doc(
            "inbox/ata-sem-id.md",
            "md",
            {"phase": "other", "title": "Ata sem id"},
            [
                mblock(1, "Falamos da autenticacao unificada de parceiros e do proximo passo."),
                mblock(2, "Mantivemos o escopo atual da autenticacao unificada de parceiros."),
            ],
        )
        res = self._correlate(amb)

        init_suggestions = [s for s in res.suggestions if s.entity_type is EntityType.INITIATIVE]
        self.assertEqual(len({s.entity_id for s in init_suggestions}), 2)
        self.assertTrue(all(s.candidate_only for s in init_suggestions))

        self.assertGreaterEqual(len(res.candidate_relations), 2)
        self.assertEqual({r.epistemic for r in res.candidate_relations}, {"inferred"})

        ini8 = identity.resolve_identity(self.repo.conn, self.ns, EntityType.INITIATIVE, "INI-008")
        ini9 = identity.resolve_identity(self.repo.conn, self.ns, EntityType.INITIATIVE, "INI-009")
        self.assertIsNotNone(ini8)
        self.assertIsNotNone(ini9)
        self.assertNotEqual(ini8, ini9)
        # ambiguidade não vira vínculo confirmado (nem belongs_to, nem fusão)
        self.assertTrue(all(r.relation_type != "belongs_to" for r in res.relations_written))

    # -- cenário 6: lote com falha isolada (§7.1, aceite W5) ---------------

    def test_scenario6_correlate_batch_isolates_broken_source(self):
        broken = make_doc("inbox/quebrado.md", "md", {}, [mblock(1, "Texto qualquer sobre INI-008.")])
        broken.bytes_sha256 = ""  # viola pré-condição de §8.1 (bytes/hash preservados)

        valid = make_doc(
            "inbox/ok-lote.md",
            "md",
            {"initiative_id": "INI-008", "phase": "other"},
            [mblock(1, "Ficou decidido em DEC-018 que o lote inicial tera 10 parceiros.")],
        )

        items = [(extract_candidates(d), d) for d in (broken, valid)]
        results = corr.correlate_batch(items, self.repo, self.ns)

        self.assertEqual(len(results), 2)
        self.assertIsNotNone(results[0].error)
        self.assertIsNone(results[1].error)
        self.assertTrue(results[1].correlated)

        dec018 = identity.resolve_identity(self.repo.conn, self.ns, EntityType.DECISION, "DEC-018")
        self.assertIsNotNone(dec018)

    # -- cenário 8 (F10): source_type=code-repo nunca grava implemented ----

    def test_scenario8_f10_code_repo_source_type_never_writes_implemented_fact(self):
        self._seed_rn023_implemented()
        f10 = make_doc(
            "inbox/declarado-code.md",
            "md",
            {"initiative_id": "INI-008", "phase": "other", "source_type": "code-repo", "title": "Dump declarado"},
            [
                mblock(1, "O modulo de RN-023 bloqueia apos 9 tentativas, conforme este dump."),
                mblock(2, "O sistema deve manter o comportamento descrito acima."),
            ],
        )
        res = self._correlate(f10)
        self.assertGreater(len(res.facts_written), 0)
        for fid in res.facts_written:
            fact = self.repo.get_fact(fid, lifecycle=None)
            self.assertIsNotNone(fact)
            self.assertNotEqual(fact.nature, FactNature.IMPLEMENTED)


# --------------------------------------------------------------------------
# Cenário 7: detect_derived — publicação própria não vira evidência
# independente (§8.4). Testes já existentes, mantidos.
# --------------------------------------------------------------------------


class TestDetectDerived(unittest.TestCase):
    """Funcoes auxiliares de correlate."""

    def test_detect_derived_no_marks(self):
        """detect_derived sem publication_id retorna independent_evidence=True."""
        doc = {"metadata": {}, "blocks": []}
        result = corr.detect_derived(doc, None)
        self.assertFalse(result.is_derived)
        self.assertTrue(result.independent_evidence)

    def test_detect_derived_with_publication_id(self):
        """detect_derived com publication_id retorna derived=True."""
        doc = {"metadata": {"publication_id": "pub-001"}, "blocks": []}
        result = corr.detect_derived(doc, None)
        self.assertTrue(result.is_derived)
        self.assertFalse(result.independent_evidence)
        self.assertEqual(result.publication_id, "pub-001")

    def test_detect_derived_agent_source_type(self):
        """detect_derived com source_type=agent-response → authored_by_agent."""
        doc = {"metadata": {"source_type": "agent-response"}, "blocks": []}
        result = corr.detect_derived(doc, None)
        self.assertTrue(result.authored_by_agent)

    def test_correlation_result_structure(self):
        """CorrelationResult tem campos corretos."""
        result = corr.CorrelationResult(
            namespace="test",
            source_id="src-001",
            source_version_id="svid-001",
        )
        self.assertIsNotNone(result)
        self.assertFalse(result.duplicate)
        self.assertFalse(result.correlated)

    def test_correlation_result_summary_duplicate(self):
        """CorrelationResult.summary() para duplicate."""
        result = corr.CorrelationResult(
            namespace="test",
            source_id="src-001",
            source_version_id="svid-001",
            duplicate=True,
        )
        summary = result.summary()
        self.assertIn("duplicada", summary)

    def test_correlation_result_summary_with_facts(self):
        """CorrelationResult.summary() com fatos/arestas."""
        result = corr.CorrelationResult(
            namespace="test",
            source_id="src-001",
            source_version_id="svid-001",
            facts_written=("f1", "f2"),
            relations_written=(
                corr.WrittenRelation(
                    relation_id="r1",
                    source_entity_id="src",
                    relation_type="refines",
                    target_entity_id="tgt",
                    epistemic="inferred",
                    lifecycle="current",
                    changed=False,
                ),
            ),
        )
        summary = result.summary()
        self.assertIn("fatos=2", summary)
        self.assertIn("arestas=1", summary)


if __name__ == "__main__":
    unittest.main()
