"""Testes para sharepoint.py (W7-T7.2): perfil, estados, catálogo de agentes.

Regra: identify_profile persiste 1×; mark_published nunca marca retrievable;
confirm_retrievable exige verification_evidence; agent_catalog com 4 agentes,
descrição única (purpose) por agente; upload_package monta pasta com hashes,
sem rede.
"""

import json
import tempfile
import unittest
from pathlib import Path

from publishing import sharepoint as SP
from publishing.release import DocumentState, Manifest, ManifestEntry


def _entry(unit_id, docx_path, md_path=None, state=DocumentState.GENERATED, content_hash="hash"):
    return ManifestEntry(
        unit_id=unit_id,
        md_path=md_path or f"markdown/{unit_id}.md",
        docx_path=docx_path,
        state=state,
        fact_ids=[],
        content_hash=content_hash,
    )


class IdentifyProfileTest(unittest.TestCase):
    """Profile identificado 1× (2ª chamada retorna persistido, não sobrescreve)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store_dir = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_identify_profile_persists_once(self):
        """Chamada 1 cria profile; chamada 2 retorna o criado, ignorando args novos."""
        profile_1 = SP.identify_profile(
            self.store_dir, mode="url_integration", target_hint="https://sharepoint.com/site"
        )
        profile_id_1 = profile_1.profile_id
        mode_1 = profile_1.mode

        # 2ª chamada com argumentos DIFERENTES (file_sync, outro target)
        # Deve devolver o CRIADO anteriormente, não o novo
        profile_2 = SP.identify_profile(
            self.store_dir,
            mode="file_sync",
            target_hint="https://different.com",
        )

        self.assertEqual(profile_id_1, profile_2.profile_id, "Profile ID deve ser o mesmo")
        self.assertEqual(mode_1, profile_2.mode, "Mode deve permanecer o original")
        self.assertEqual(profile_2.mode, "url_integration", "Mode deve ser o da 1ª chamada")

    def test_identify_profile_rejects_invalid_mode(self):
        """Mode inválido levanta UnknownMode."""
        with self.assertRaises(SP.UnknownMode):
            SP.identify_profile(
                self.store_dir, mode="invalid_mode", target_hint="somewhere"
            )

    def test_identify_profile_creates_json_file(self):
        """Profile criado é persistido em sharepoint_profile.json."""
        profile = SP.identify_profile(
            self.store_dir, mode="url_integration", target_hint="https://test.com"
        )

        profile_path = Path(self.store_dir) / SP.PROFILE_FILENAME
        self.assertTrue(profile_path.exists(), "Profile JSON deve ser criado")

        with open(profile_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["profile_id"], profile.profile_id)


class MarkPublishedTest(unittest.TestCase):
    """mark_published marca 'published' mas NUNCA 'retrievable'."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store_dir = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_mark_published_never_sets_retrievable(self):
        """mark_published promove a 'published', nunca a 'retrievable'."""
        profile = SP.identify_profile(
            self.store_dir, mode="url_integration", target_hint="https://test.com"
        )

        # Cria manifesto com 2 documentos (unit_id é a chave, contrato real de Manifest)
        manifest = Manifest(
            revision_id="pub_test1",
            generated_at="2026-09-06T00:00:00+00:00",
            documents={
                "unit_1": _entry("unit_1", "word/doc_1.docx"),
                "unit_2": _entry("unit_2", "word/doc_2.docx"),
            },
        )

        tracking = SP.mark_published(manifest, profile)

        # Ambos devem estar 'published', NENHUM 'retrievable'
        for unit_id in ["unit_1", "unit_2"]:
            doc_state = tracking.get(unit_id)
            self.assertIsNotNone(doc_state)
            self.assertEqual(doc_state.state, DocumentState.PUBLISHED)
            self.assertIsNone(doc_state.verification, "verification deve ser None (não marcou retrievable)")

    def test_mark_published_sets_profile_id(self):
        """mark_published copia profile_id para cada documento."""
        profile = SP.identify_profile(
            self.store_dir, mode="file_sync", target_hint="/local/path"
        )

        manifest = Manifest(
            revision_id="pub_test2",
            generated_at="2026-09-06T00:00:00+00:00",
            documents={
                "unit_a": _entry("unit_a", "word/doc_a.docx"),
            },
        )

        tracking = SP.mark_published(manifest, profile)
        doc_a_state = tracking.get("unit_a")

        self.assertEqual(doc_a_state.profile_id, profile.profile_id)


class ConfirmRetrievableTest(unittest.TestCase):
    """confirm_retrievable exige verification_evidence e transição válida."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store_dir = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_confirm_retrievable_requires_evidence(self):
        """confirm_retrievable sem verification_evidence levanta InvalidStateTransition."""
        profile = SP.identify_profile(
            self.store_dir, mode="url_integration", target_hint="https://test.com"
        )

        manifest = Manifest(
            revision_id="pub_test3",
            generated_at="2026-09-06T00:00:00+00:00",
            documents={
                "unit_1": _entry("unit_1", "word/doc_1.docx"),
            },
        )

        tracking = SP.mark_published(manifest, profile)

        with self.assertRaises(SP.InvalidStateTransition):
            SP.confirm_retrievable(tracking, "unit_1", verification_evidence=None)

    def test_confirm_retrievable_with_evidence(self):
        """confirm_retrievable com verification_evidence muda para 'retrievable'."""
        profile = SP.identify_profile(
            self.store_dir, mode="url_integration", target_hint="https://test.com"
        )

        manifest = Manifest(
            revision_id="pub_test4",
            generated_at="2026-09-06T00:00:00+00:00",
            documents={
                "unit_1": _entry("unit_1", "word/doc_1.docx"),
            },
        )

        tracking = SP.mark_published(manifest, profile)

        evidence = {"url": "https://sharepoint.com/sites/wiki/doc_1", "verified_at": "2026-01-01T00:00:00Z"}
        tracking = SP.confirm_retrievable(tracking, "unit_1", verification_evidence=evidence)

        doc_state = tracking.get("unit_1")
        self.assertEqual(doc_state.state, SP.STATE_RETRIEVABLE)
        self.assertEqual(dict(doc_state.verification), evidence)

    def test_confirm_retrievable_unknown_doc_raises(self):
        """confirm_retrievable com doc_id desconhecido levanta UnknownDocument."""
        tracking = SP.PublicationTracking()

        with self.assertRaises(SP.UnknownDocument):
            SP.confirm_retrievable(
                tracking,
                "unknown_doc",
                verification_evidence={"check": "pass"},
            )


class AgentCatalogTest(unittest.TestCase):
    """agent_catalog retorna dict com 4 agentes; purpose único por agente."""

    def test_agent_catalog_returns_four_agents(self):
        """agent_catalog deve retornar 4 agentes com purpose."""
        manifest = Manifest(revision_id="pub_test5", generated_at="2026-09-06T00:00:00+00:00", documents={})

        catalog = SP.agent_catalog(manifest)

        expected_agents = {"ask", "inception", "historias", "refinamento"}
        actual_agents = set(catalog.keys())
        self.assertEqual(actual_agents, expected_agents, "Deve ter exatamente 4 agentes")

        for agent_name, agent_source in catalog.items():
            self.assertTrue(
                hasattr(agent_source, "purpose"),
                f"Agent {agent_name} deve ter purpose",
            )
            self.assertTrue(
                agent_source.purpose and len(str(agent_source.purpose)) > 0,
                f"Agent {agent_name} purpose não pode ser vazio",
            )

    def test_agent_catalog_purposes_unique(self):
        """Cada agente tem purpose específico (não genérico copiado)."""
        manifest = Manifest(revision_id="pub_test6", generated_at="2026-09-06T00:00:00+00:00", documents={})

        catalog = SP.agent_catalog(manifest)

        purposes = [agent.purpose for agent in catalog.values()]
        unique_purposes = set(purposes)
        self.assertEqual(
            len(unique_purposes), len(purposes),
            "Cada agente deve ter purpose ÚNICO (não copiado)",
        )

    def test_agent_catalog_domain_description_shared(self):
        """domain_description é a MESMA config reaproveitada pelos 4 agentes (§11.1)."""
        manifest = Manifest(revision_id="pub_test7", generated_at="2026-09-06T00:00:00+00:00", documents={})

        catalog = SP.agent_catalog(manifest)

        descriptions = {agent.domain_description for agent in catalog.values()}
        self.assertEqual(len(descriptions), 1, "domain_description deve ser reaproveitada, não repetida por arquivo")


class UploadPackageTest(unittest.TestCase):
    """upload_package monta pasta com arquivos e hashes, sem rede."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.out_root = str(Path(self.tmpdir.name) / "out_root")
        self.target_dir = str(Path(self.tmpdir.name) / "upload_manual")
        word_dir = Path(self.out_root) / "word"
        word_dir.mkdir(parents=True)
        (word_dir / "cap_023.docx").write_bytes(b"fake docx content")
        (word_dir / "ini_001.docx").write_bytes(b"fake docx content 2")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_upload_package_creates_directory_structure(self):
        """upload_package monta pasta com estrutura por sistema/iniciativa."""
        manifest = Manifest(
            revision_id="pub_pkg",
            generated_at="2026-09-06T00:00:00+00:00",
            documents={
                "cap_023": _entry("cap_023", "word/cap_023.docx", content_hash="hash-cap"),
                "ini_001": _entry("ini_001", "word/ini_001.docx", content_hash="hash-ini"),
            },
        )

        package_info = SP.upload_package(
            self.out_root,
            manifest,
            grouping={"cap_023": "SistemaX/INI-008", "ini_001": "SistemaX/INI-008"},
            target_dir=self.target_dir,
        )

        self.assertIsNotNone(package_info)
        self.assertGreater(len(package_info["files"]), 0, "Package deve listar arquivos")
        self.assertEqual(package_info["missing_files"], [])
        self.assertEqual(len(package_info["copied"]), 2)

    def test_upload_package_computes_hashes(self):
        """upload_package calcula hash dos arquivos (para integridade)."""
        manifest = Manifest(
            revision_id="pub_hash",
            generated_at="2026-09-06T00:00:00+00:00",
            documents={
                "cap_023": _entry("cap_023", "word/cap_023.docx", content_hash="hash-cap"),
            },
        )

        package_info = SP.upload_package(self.out_root, manifest, target_dir=self.target_dir)

        self.assertGreater(len(package_info["files"]), 0)
        for file_info in package_info["files"]:
            self.assertIsNotNone(file_info["sha256_on_disk"], "Arquivo deve ter hash")


if __name__ == "__main__":
    unittest.main()
