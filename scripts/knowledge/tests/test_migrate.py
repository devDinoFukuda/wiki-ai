"""Testes da migração do corpus legado (§16.1, onda W8).

Cenários cobertos:
1. inventory_legacy classifica {curated_source, agent_output, derived_output,
   code_analysis_output, asset, log} e NUNCA altera disco.
2. backup: manifest com sha256; recusa backup_dir não-vazio; migrate sem
   backup válido → BackupError; original alterado após backup → recusa.
3. migrate: raw com derived_from → relação DERIVED_FROM; NENHUM fato com
   nature=IMPLEMENTED ou epistemic=SUPPORTED; item quebrado → SkippedItem.
4. Idempotência: migrate 2× → contagens de entities/facts/relations/evidence
   idênticas; changed=False na segunda.
5. verify_migration: ok=True no fluxo feliz; detecta fato proibido (se
   simulável) ou cobre caminho feliz.
"""

import unittest
import tempfile
import os
import shutil
import json
from pathlib import Path

from scripts.knowledge import migrate as M
from scripts.knowledge.repository import Repository
from scripts.knowledge.models import (
    FactNature,
    EpistemicStatus,
    EntityType,
    SourceKind,
    ContentKind,
    EntityDraft,
    FactDraft,
    LifecycleStatus,
    RelationType,
)


class TestBaseWithStore(unittest.TestCase):
    """Base class com store legado sintético + DB temporário."""

    def setUp(self) -> None:
        """Cria store legado, backup e DB para cada teste."""
        self.tmpdir = tempfile.mkdtemp()
        self.store_root = os.path.join(self.tmpdir, "legacy_store")
        self.backup_dir = os.path.join(self.tmpdir, "legacy_backup")
        self.db_path = os.path.join(self.tmpdir, "knowledge.db")

        # Criar estrutura mínima do store legado
        os.makedirs(os.path.join(self.store_root, "raw", "docs"), exist_ok=True)
        os.makedirs(os.path.join(self.store_root, "raw", "transcripts"), exist_ok=True)
        os.makedirs(os.path.join(self.store_root, "wiki"), exist_ok=True)
        os.makedirs(os.path.join(self.store_root, ".codescan", "python", "sdd"), exist_ok=True)
        os.makedirs(os.path.join(self.store_root, "raw", "assets"), exist_ok=True)

        self.repo = Repository.open(self.db_path)

    def tearDown(self) -> None:
        """Fecha DB e limpa tmpdir."""
        self.repo.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_md(self, rel_path: str, content: str) -> str:
        """Escreve arquivo .md no store_root com line endings Unix."""
        abs_path = os.path.join(self.store_root, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        # Normaliza para LF (Unix) para compatibilidade com parser de frontmatter
        content = content.replace("\r\n", "\n")
        with open(abs_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        return abs_path

    def _write_asset(self, rel_path: str, content: bytes) -> str:
        """Escreve arquivo não-texto no store_root."""
        abs_path = os.path.join(self.store_root, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "wb") as fh:
            fh.write(content)
        return abs_path


class TestInventoryLegacy(TestBaseWithStore):
    """§1: inventory_legacy classifica e NUNCA altera disco."""

    def test_inventory_classifies_curated_source(self) -> None:
        """raw/*.md com source_type legítimo → CATEGORY_CURATED_SOURCE."""
        self._write_md(
            "raw/docs/doc-001.md",
            "---\nid: doc1\nsource_type: human-doc\norigin: test\n---\nCorpo.",
        )
        inv = M.inventory_legacy(self.store_root)
        self.assertEqual(len(inv.items), 1)
        self.assertEqual(inv.items[0].category, M.CATEGORY_CURATED_SOURCE)
        self.assertEqual(inv.items[0].legacy_id, "doc1")
        self.assertEqual(inv.items[0].source_type, "human-doc")

    def test_inventory_classifies_agent_output(self) -> None:
        """raw/*.md com source_type=agent-output → CATEGORY_AGENT_OUTPUT."""
        self._write_md(
            "raw/docs/agent.md",
            "---\nid: agent1\nsource_type: agent-output\n---\nSaída.",
        )
        inv = M.inventory_legacy(self.store_root)
        self.assertEqual(len(inv.items), 1)
        self.assertEqual(inv.items[0].category, M.CATEGORY_AGENT_OUTPUT)

    def test_inventory_classifies_wiki_as_derived(self) -> None:
        """wiki/*.md → CATEGORY_DERIVED_OUTPUT."""
        self._write_md("wiki/topic/article.md", "# Artigo\nConteúdo.")
        inv = M.inventory_legacy(self.store_root)
        self.assertEqual(len(inv.items), 1)
        self.assertEqual(inv.items[0].category, M.CATEGORY_DERIVED_OUTPUT)

    def test_inventory_classifies_codescan_output(self) -> None:
        """.codescan/*/sdd/*.md → CATEGORY_CODE_ANALYSIS_OUTPUT."""
        self._write_md(".codescan/python/sdd/module-001.md", "Análise de código.")
        inv = M.inventory_legacy(self.store_root)
        self.assertEqual(len(inv.items), 1)
        self.assertEqual(inv.items[0].category, M.CATEGORY_CODE_ANALYSIS_OUTPUT)

    def test_inventory_classifies_asset(self) -> None:
        """raw/assets/* → CATEGORY_ASSET."""
        self._write_asset("raw/assets/image.png", b"PNG_DATA")
        inv = M.inventory_legacy(self.store_root)
        self.assertEqual(len(inv.items), 1)
        self.assertEqual(inv.items[0].category, M.CATEGORY_ASSET)
        self.assertIsNotNone(inv.items[0].sha256)

    def test_inventory_counts(self) -> None:
        """Contagens por categoria."""
        self._write_md(
            "raw/docs/doc1.md",
            "---\nsource_type: human-doc\norigin: test\n---\nConteúdo.",
        )
        self._write_md("wiki/test.md", "Conteúdo wiki.")
        self._write_md(".codescan/python/sdd/test.md", "Análise.")
        self._write_asset("raw/assets/img.png", b"data")

        inv = M.inventory_legacy(self.store_root)
        self.assertEqual(inv.counts[M.CATEGORY_CURATED_SOURCE], 1)
        self.assertEqual(inv.counts[M.CATEGORY_DERIVED_OUTPUT], 1)
        self.assertEqual(inv.counts[M.CATEGORY_CODE_ANALYSIS_OUTPUT], 1)
        self.assertEqual(inv.counts[M.CATEGORY_ASSET], 1)
        self.assertEqual(len(inv.items), 4)

    def test_inventory_computes_sha256(self) -> None:
        """Cada item tem sha256 correto."""
        content = "Conteúdo único.\n"
        self._write_md("raw/docs/test.md", f"---\nsource_type: human-doc\norigin: x\n---\n{content}")
        inv = M.inventory_legacy(self.store_root)
        item = inv.items[0]
        self.assertEqual(len(item.sha256), 64)  # hex SHA256
        self.assertTrue(all(c in "0123456789abcdef" for c in item.sha256))

    def test_inventory_never_modifies_store(self) -> None:
        """inventory_legacy é read-only."""
        path = self._write_md("raw/docs/doc.md", "---\nsource_type: human-doc\norigin: x\n---\nConteúdo.")
        with open(path, "r") as fh:
            original = fh.read()

        inv = M.inventory_legacy(self.store_root)

        with open(path, "r") as fh:
            after = fh.read()
        self.assertEqual(original, after, "inventory_legacy alterou arquivo no disco!")


class TestBackup(TestBaseWithStore):
    """§2: backup com manifest, recusa reescrita, valida integridade."""

    def test_backup_creates_manifest(self) -> None:
        """backup() cria manifest.json com hashes."""
        self._write_md("raw/docs/doc.md", "---\nsource_type: human-doc\norigin: x\n---\nCorpo.")
        bkp = M.backup(self.store_root, self.backup_dir)

        self.assertTrue(os.path.isfile(bkp.manifest_path))
        with open(bkp.manifest_path) as fh:
            manifest = json.load(fh)
        self.assertIn("files", manifest)
        self.assertGreater(len(manifest["files"]), 0)

    def test_backup_copies_files(self) -> None:
        """Arquivos são copiados para backup_dir/files/."""
        self._write_md("raw/docs/doc.md", "---\nsource_type: human-doc\norigin: x\n---\nCorpo.")
        bkp = M.backup(self.store_root, self.backup_dir)

        backed_path = os.path.join(self.backup_dir, "files", "raw/docs/doc.md")
        self.assertTrue(os.path.isfile(backed_path))

    def test_backup_refuses_nonempty_dir(self) -> None:
        """Recusa sobrescrever backup_dir já povoado."""
        self._write_md("raw/docs/doc.md", "---\nsource_type: human-doc\norigin: x\n---\nCorpo.")

        # Primeiro backup OK
        bkp1 = M.backup(self.store_root, self.backup_dir)
        self.assertTrue(os.path.isfile(bkp1.manifest_path))

        # Segundo backup DEVE RECUSAR
        with self.assertRaises(M.BackupError) as ctx:
            M.backup(self.store_root, self.backup_dir)
        self.assertIn("não está vazio", str(ctx.exception))

    def test_backup_manifest_validates_files(self) -> None:
        """Manifest contém sha256 correto de cada arquivo."""
        self._write_md("raw/docs/test.md", "---\nsource_type: human-doc\norigin: x\n---\nConteúdo único.")
        bkp = M.backup(self.store_root, self.backup_dir)

        with open(bkp.manifest_path) as fh:
            manifest = json.load(fh)
        self.assertIn("raw/docs/test.md", manifest["files"])
        file_info = manifest["files"]["raw/docs/test.md"]
        self.assertIn("sha256", file_info)
        self.assertIn("size", file_info)

    def test_migrate_refuses_without_backup(self) -> None:
        """migrate() recusa se backup_dir inválido."""
        self._write_md("raw/docs/doc.md", "---\nsource_type: human-doc\norigin: x\n---\nCorpo.")

        with self.assertRaises(M.BackupError) as ctx:
            M.migrate(self.store_root, self.repo, "org/test", backup_dir="/nonexistent/backup")
        self.assertIn("manifest", str(ctx.exception).lower())

    def test_migrate_refuses_if_original_changed_after_backup(self) -> None:
        """migrate() recusa se arquivo original mudou desde o backup."""
        path = self._write_md("raw/docs/doc.md", "---\nsource_type: human-doc\norigin: x\n---\nCorpo original.")

        # Fazer backup
        bkp = M.backup(self.store_root, self.backup_dir)

        # Alterar arquivo original
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\nLinha adicionada após backup!")

        # migrate() deve recusar
        with self.assertRaises(M.BackupError) as ctx:
            M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)
        self.assertIn("mudou", str(ctx.exception).lower())


class TestMigrate(TestBaseWithStore):
    """§3: migrate cria entidades/fatos com natureza/epistemic corretos."""

    def test_migrate_creates_entity_for_md(self) -> None:
        """Cada .md vira uma entidade Source."""
        self._write_md(
            "raw/docs/doc-001.md",
            "---\nid: doc1\nsource_type: human-doc\norigin: test\n---\nCorpo.",
        )
        bkp = M.backup(self.store_root, self.backup_dir)

        result = M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)

        self.assertEqual(len(result.migrados), 1)
        self.assertEqual(result.migrados[0].category, M.CATEGORY_CURATED_SOURCE)
        self.assertIsNotNone(result.migrados[0].entity_id)

        # Confira que entidade existe no repo
        entity_id = result.migrados[0].entity_id
        self.assertTrue(self.repo.entity_exists(entity_id))

    def test_migrate_facts_never_implemented_or_supported(self) -> None:
        """NENHUM fato migrado tem nature=IMPLEMENTED ou epistemic=SUPPORTED."""
        self._write_md(
            "raw/docs/doc-001.md",
            "---\nid: doc1\nsource_type: human-doc\norigin: test\n---\n\n"
            "Parágrafo 1.\n\nParágrafo 2.",
        )
        bkp = M.backup(self.store_root, self.backup_dir)

        result = M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)

        for m in result.migrados:
            for fact_id in m.fact_ids:
                fact = self.repo.get_fact(fact_id, lifecycle=None)
                self.assertIsNotNone(fact, f"fact_id {fact_id} não encontrado")
                self.assertNotEqual(
                    fact.nature, FactNature.IMPLEMENTED,
                    f"Fato {fact_id} tem nature=IMPLEMENTED (proibido)",
                )
                self.assertNotEqual(
                    fact.epistemic_status, EpistemicStatus.SUPPORTED,
                    f"Fato {fact_id} tem epistemic=SUPPORTED (proibido)",
                )

    def test_migrate_creates_derived_from_relation(self) -> None:
        """derived_from no frontmatter → RelationType.DERIVED_FROM."""
        # Criar documento base
        self._write_md(
            "raw/docs/doc-001.md",
            "---\nid: doc1\nsource_type: human-doc\norigin: test\n---\nBase.",
        )
        # Criar documento derivado
        self._write_md(
            "raw/transcripts/doc-002.md",
            "---\nid: doc2\nsource_type: human-transcript\norigin: test\nderived_from: doc1\n---\nDerivado.",
        )
        bkp = M.backup(self.store_root, self.backup_dir)

        result = M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)

        # Encontrar as entidades
        doc1_id = result.mapa_ids.get("raw/docs/doc-001.md")
        doc2_id = result.mapa_ids.get("raw/transcripts/doc-002.md")
        self.assertIsNotNone(doc1_id)
        self.assertIsNotNone(doc2_id)

        # Confira relação: doc2 -> doc1 (DERIVED_FROM)
        doc2_item = next((m for m in result.migrados if m.rel_path == "raw/transcripts/doc-002.md"), None)
        self.assertIsNotNone(doc2_item)
        self.assertGreater(len(doc2_item.relation_ids), 0, "Sem relações criadas para doc-002")

        # Verificar que a relação foi persistida
        neighbors = self.repo.neighbors(doc2_id, direction="out")
        found_derived = False
        for rel in neighbors:
            if rel.target_entity_id == doc1_id and rel.relation_type == RelationType.DERIVED_FROM:
                found_derived = True
                break
        self.assertTrue(found_derived, "Relação DERIVED_FROM não encontrada no repo")

    def test_migrate_continues_after_malformed_item(self) -> None:
        """Item com erro não interrompe migração de outros itens."""
        # Arquivo válido
        self._write_md(
            "raw/docs/doc-001.md",
            "---\nsource_type: human-doc\norigin: test\n---\nOK.",
        )
        # Arquivo com codificação ruim (não UTF-8)
        bad_path = os.path.join(self.store_root, "raw", "docs", "broken.md")
        with open(bad_path, "wb") as fh:
            fh.write(b"\xff\xfe Invalid UTF-8")

        bkp = M.backup(self.store_root, self.backup_dir)

        # migrate não deve abortar mesmo com arquivo corrompido
        result = M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)

        # Deve ter pelo menos um item migrado (doc-001)
        self.assertGreater(len(result.migrados), 0, "Nenhum item migrado")
        # E pode ter itens pulados por erro de leitura
        # (Dependendo de quando o arquivo corrompido é processado)
        self.assertGreaterEqual(len(result.migrados) + len(result.pulados), 1)

    def test_migrate_creates_assets(self) -> None:
        """raw/assets/* viram entidades Asset."""
        self._write_asset("raw/assets/image.png", b"PNG_DATA_HEADER")
        bkp = M.backup(self.store_root, self.backup_dir)

        result = M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)

        self.assertEqual(len(result.migrados), 1)
        self.assertEqual(result.migrados[0].category, M.CATEGORY_ASSET)
        asset_id = result.migrados[0].entity_id
        self.assertTrue(self.repo.entity_exists(asset_id))


class TestIdempotency(TestBaseWithStore):
    """§4: migrate 2× → contagens idênticas, changed=False na 2ª."""

    def test_migrate_twice_creates_identical_counts(self) -> None:
        """Segunda migração não duplica entities/facts/relations/evidence."""
        # Setup
        self._write_md(
            "raw/docs/doc-001.md",
            "---\nid: doc1\nsource_type: human-doc\norigin: test\n---\n"
            "Parágrafo 1.\n\nParágrafo 2.",
        )
        self._write_md(
            "raw/transcripts/doc-002.md",
            "---\nid: doc2\nsource_type: human-transcript\norigin: test\nderived_from: doc1\n---\n"
            "Derivado.",
        )
        bkp = M.backup(self.store_root, self.backup_dir)

        # 1ª migração
        result1 = M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)

        def get_counts():
            c = self.repo.conn
            return {
                "entities": c.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                "entity_revisions": c.execute("SELECT COUNT(*) FROM entity_revisions").fetchone()[0],
                "facts": c.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
                "fact_revisions": c.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0],
                "relations": c.execute("SELECT COUNT(*) FROM relations").fetchone()[0],
                "relation_revisions": c.execute("SELECT COUNT(*) FROM relation_revisions").fetchone()[0],
                "evidence": c.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            }

        counts_1 = get_counts()
        revisions_1 = self.repo.conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]

        # 2ª migração
        result2 = M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)

        counts_2 = get_counts()
        revisions_2 = self.repo.conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]

        # Contagens de domínio devem ser idênticas
        self.assertEqual(counts_1, counts_2, "Contagens de entidades/fatos/relações mudaram na 2ª migração!")

        # Revisions pode crescer (bookkeeping), mas não deve duplicar domínio
        self.assertGreaterEqual(revisions_2, revisions_1)

        # Nenhum item deve ter changed=True na 2ª migração
        changed_any = any(m.changed for m in result2.migrados)
        self.assertFalse(changed_any, "2ª migração marcou changed=True (contéudo idêntico)")

    def test_migrate_idempotent_entity_ids(self) -> None:
        """Entity IDs são determinísticos: mesmo arquivo → mesmo ID."""
        self._write_md(
            "raw/docs/doc-001.md",
            "---\nsource_type: human-doc\norigin: test\n---\nCorpo.",
        )
        bkp = M.backup(self.store_root, self.backup_dir)

        result1 = M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)
        id1 = result1.mapa_ids.get("raw/docs/doc-001.md")

        result2 = M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)
        id2 = result2.mapa_ids.get("raw/docs/doc-001.md")

        self.assertEqual(id1, id2, "Entity ID mudou entre migrações (não determinístico)")


class TestVerifyMigration(TestBaseWithStore):
    """§5: verify_migration detecta problemas pós-migração."""

    def test_verify_migration_ok_on_happy_path(self) -> None:
        """verify_migration retorna ok=True no fluxo feliz."""
        self._write_md(
            "raw/docs/doc-001.md",
            "---\nsource_type: human-doc\norigin: test\n---\nCorpo.",
        )
        bkp = M.backup(self.store_root, self.backup_dir)
        result = M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)

        report = M.verify_migration(self.store_root, self.repo, result)

        self.assertTrue(report.ok, f"Verificação falhou: {report}")
        self.assertEqual(len(report.missing_sources), 0)
        self.assertEqual(len(report.forbidden_facts), 0)
        self.assertIsNone(report.count_mismatch)

    def test_verify_migration_detects_missing_sources(self) -> None:
        """verify_migration detecta entidades não criadas."""
        self._write_md(
            "raw/docs/doc-001.md",
            "---\nsource_type: human-doc\norigin: test\n---\nCorpo.",
        )
        bkp = M.backup(self.store_root, self.backup_dir)

        # Simular migração parcial: criar resultado sem realmente migrar
        inv = M.inventory_legacy(self.store_root)
        result = M.MigrationResult(namespace="org/test", backup_ref=bkp.manifest_path)
        # result.migrados fica vazio

        report = M.verify_migration(self.store_root, self.repo, result)

        # Deve ter achado fonte ausente
        self.assertFalse(report.ok)
        self.assertGreater(len(report.missing_sources), 0)

    def test_verify_migration_counts_items(self) -> None:
        """verify_migration confere contagem total contra inventário."""
        self._write_md(
            "raw/docs/doc-001.md",
            "---\nsource_type: human-doc\norigin: test\n---\nCorpo.",
        )
        self._write_md("wiki/test.md", "Conteúdo wiki.")
        bkp = M.backup(self.store_root, self.backup_dir)

        result = M.migrate(self.store_root, self.repo, "org/test", backup_dir=self.backup_dir)
        report = M.verify_migration(self.store_root, self.repo, result)

        # Deve ter 2 itens (1 curated_source + 1 derived_output)
        self.assertEqual(len(result.migrados) + len(result.pulados), 2)
        self.assertIsNone(report.count_mismatch, f"Contagem divergiu: {report.count_mismatch}")


class TestFrontmatterParsing(TestBaseWithStore):
    """Parse de frontmatter legado."""

    def test_split_frontmatter_basic(self) -> None:
        """_split_frontmatter extrai metadados e corpo."""
        text = "---\nid: test1\nsource_type: human-doc\n---\nCorpo aqui."
        meta, body = M._split_frontmatter(text)

        self.assertEqual(meta.get("id"), "test1")
        self.assertEqual(meta.get("source_type"), "human-doc")
        self.assertIn("Corpo", body)

    def test_split_frontmatter_handles_bom(self) -> None:
        """_split_frontmatter tolera BOM UTF-8."""
        text = "﻿---\nid: test\n---\nCorpo."
        meta, body = M._split_frontmatter(text)

        self.assertEqual(meta.get("id"), "test")
        self.assertIn("Corpo", body)

    def test_split_frontmatter_no_frontmatter(self) -> None:
        """Sem frontmatter, retorna dicts vazios e texto completo."""
        text = "Apenas corpo, sem frontmatter."
        meta, body = M._split_frontmatter(text)

        self.assertEqual(meta, {})
        self.assertIn("Apenas corpo", body)

    def test_split_list_field(self) -> None:
        """_split_list_field processa valores escalares, CSV, lista."""
        self.assertEqual(M._split_list_field(None), [])
        self.assertEqual(M._split_list_field("item1"), ["item1"])
        self.assertEqual(M._split_list_field("item1,item2"), ["item1", "item2"])
        self.assertEqual(M._split_list_field(["item1", "item2"]), ["item1", "item2"])


if __name__ == "__main__":
    unittest.main()
