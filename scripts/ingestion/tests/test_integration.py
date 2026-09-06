"""Testes de integracao — pipeline real: ingest → extract_candidates → correlate.

Sem mocks: `ingestion.ingest()` le um arquivo `.md` REAL de um tempdir,
`knowledge.Repository` grava em um `knowledge.db` REAL (tempfile) e
`ingestion.correlate.correlate` roda a revisão de verdade contra ele.
"""

import os
import shutil
import tempfile
import unittest

from ingestion import extract as extr
from ingestion import normalize as norm
from ingestion import ingest as ingestion_ingest
from ingestion.adapters import markdown_txt
from ingestion.correlate import correlate

from knowledge.models import EntityType, EpistemicStatus
from knowledge.repository import Repository

NS = "acme/integration-test"

RF042_MD = """---
initiative_id: INI-008
phase: refinement
title: RF-042
---
Refinamento RF-042 detalha a decisao DEC-017 para a proxima entrega.

O sistema deve registrar o motivo do bloqueio.
"""


class TestIntegrationPipeline(unittest.TestCase):
    """Pipeline simples: md → preserve → extract → candidates (sem knowledge.db)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_md_preserve_extract_flow(self):
        """Pipeline: md com frontmatter → preserve → extract → candidates."""
        md_content = """---
initiative_id: INI-008
phase: design
title: Decisao
---
# Decisao

Decidimos usar PostgreSQL.

## Requisitos

O sistema deve ser escalavel.
""".encode("utf-8")

        md_path = os.path.join(self.tmpdir, "doc.md")
        with open(md_path, "wb") as f:
            f.write(md_content)

        preserved = norm.preserve(md_path)
        self.assertEqual(len(preserved.bytes_sha256), 64)
        self.assertEqual(preserved.mime_guess, "text/markdown")

        doc = markdown_txt.extract(md_path, preserved)
        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.blocks), 0)
        self.assertIn("initiative_id", doc.metadata)
        self.assertEqual(doc.metadata["initiative_id"], "INI-008")

        candidates = extr.extract_candidates(doc)
        self.assertGreater(len(candidates.decisions), 0)
        self.assertGreater(len(candidates.requirements), 0)

        for c in candidates.all():
            self.assertGreater(len(c.block_refs), 0)

    def test_reingestao_identica_ids_estaveis(self):
        """Reingestao de arquivo identico → mesmos IDs e candidatos."""
        md_content = """---
initiative_id: INI-008
---
# Decisao

Decidimos usar PostgreSQL.

DEC-017 foi rejeitada.
""".encode("utf-8")

        md_path = os.path.join(self.tmpdir, "doc.md")
        with open(md_path, "wb") as f:
            f.write(md_content)

        preserved1 = norm.preserve(md_path)
        doc1 = markdown_txt.extract(md_path, preserved1)
        candidates1 = extr.extract_candidates(doc1)

        preserved2 = norm.preserve(md_path)
        doc2 = markdown_txt.extract(md_path, preserved2)
        candidates2 = extr.extract_candidates(doc2)

        self.assertEqual(preserved1.bytes_sha256, preserved2.bytes_sha256)
        self.assertEqual(len(candidates1.all()), len(candidates2.all()))

        ids1 = {i.value for i in candidates1.explicit_ids()}
        ids2 = {i.value for i in candidates2.explicit_ids()}
        self.assertEqual(ids1, ids2)


class TestIntegrationEndToEndKnowledgeDb(unittest.TestCase):
    """ingest() real de .md → extract_candidates → correlate → knowledge.db real.

    `knowledge.db` é um tempfile por teste (setUp/tearDown); zero mocks.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.md_path = os.path.join(self.tmpdir, "refinamento-rf042.md")
        with open(self.md_path, "wb") as f:
            f.write(RF042_MD.encode("utf-8"))
        self.db_path = os.path.join(self.tmpdir, "knowledge.db")
        self.repo = Repository.open(self.db_path)
        self.ns = NS

    def tearDown(self):
        self.repo.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ingest_extract_correlate_writes_revision_with_valid_evidence(self):
        """ingest() real → extract_candidates → correlate: revisão com evidence_refs válidos."""
        doc = ingestion_ingest(self.md_path)
        self.assertTrue(doc.complete, msg=f"status inesperado: {doc.status}")
        self.assertEqual(doc.metadata.get("initiative_id"), "INI-008")

        candidates = extr.extract_candidates(doc)
        res = correlate(candidates, doc, self.repo, self.ns)

        self.assertIsNone(res.error)
        self.assertFalse(res.duplicate)
        self.assertIsNotNone(res.revision_id)
        self.assertTrue(res.facts_written or res.relations_written)

        rf_id = None
        for eid in res.entities_created:
            if self.repo.entity_type_of(eid) is EntityType.REFINEMENT:
                rf_id = eid
        self.assertIsNotNone(rf_id, "RF-042 deveria ter sido criada por id explicito")

        checked_refs = 0
        for fid in res.facts_written:
            fact = self.repo.get_fact(fid, lifecycle=None)
            self.assertIsNotNone(fact)
            self.assertTrue(fact.evidence_refs, f"fato {fid} sem evidence_refs")
            for ref in fact.evidence_refs:
                self.assertIsNotNone(self.repo.get_evidence(ref), f"evidence_ref invalido: {ref}")
                checked_refs += 1

        for written in res.relations_written:
            relation = self.repo.get_relation(written.relation_id, lifecycle=None)
            self.assertIsNotNone(relation)
            if relation.epistemic_status is EpistemicStatus.SUPPORTED:
                self.assertTrue(relation.evidence_refs, f"relacao supported sem evidencia: {written}")
            for ref in relation.evidence_refs:
                self.assertIsNotNone(self.repo.get_evidence(ref), f"evidence_ref invalido: {ref}")
                checked_refs += 1

        self.assertGreater(checked_refs, 0)

    def test_reingestion_of_same_file_is_duplicate(self):
        """Reingestao do MESMO arquivo (bytes identicos) → duplicate, 0 fatos/arestas novos."""
        doc1 = ingestion_ingest(self.md_path)
        first = correlate(extr.extract_candidates(doc1), doc1, self.repo, self.ns)
        self.assertFalse(first.duplicate)
        self.assertTrue(first.correlated)

        doc2 = ingestion_ingest(self.md_path)  # mesmo arquivo: mesmo bytes_sha256
        self.assertEqual(doc1.bytes_sha256, doc2.bytes_sha256)
        second = correlate(extr.extract_candidates(doc2), doc2, self.repo, self.ns)

        self.assertTrue(second.duplicate)
        self.assertEqual(second.facts_written, ())
        self.assertEqual(second.relations_written, ())


if __name__ == "__main__":
    unittest.main()
