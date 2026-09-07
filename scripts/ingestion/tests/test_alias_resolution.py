"""Testes permanentes de audit finding #4 ingestion (Onda10-E).

Contrato: RN-023 com stable_key composto + alias -> ingestao de "propomos alterar RN-023"
cria proposes_change_to proposed. Sem alias -> referencias_orfas + complete=False + pendencia,
lote nao trava.
"""

import hashlib
import os
import tempfile
import unittest
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from knowledge.models import (
    Alias,
    AliasOrigin,
    EntityDraft,
    EntityType,
    LifecycleStatus,
)
from knowledge.repository import Repository

from ingestion import correlate as corr
from ingestion.extract import extract_candidates

NS = "test/acme-onda10e"


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


def mblock(i, text, section="corpo"):
    return Block(block_id=f"p{i}", kind="paragraph", text=text, locator={"section": section})


def seed_rn023_composite_stable_key(repo, ns, with_alias: bool):
    """Simula analisador de codigo: BusinessRule com stable_key COMPOSTO.

    Parametros:
    - with_alias: se True, registra alias "RN-023" em entity_aliases
    """
    composite_key = "businessrule:cap-009:rn-023"
    aliases = (Alias("RN-023", AliasOrigin.METADATA_ID),) if with_alias else ()
    with repo.revision(author="pipeline:codescan", reason="regra de negocio detectada em codigo") as rev:
        result = rev.put_entity(
            EntityDraft(
                namespace=ns,
                entity_type=EntityType.BUSINESS_RULE,
                stable_key=composite_key,
                title="RN-023 Limite de tentativas (CAP-009)",
                aliases=aliases,
                attributes={"capability_id": "CAP-009", "origin": "code-analysis"},
                lifecycle_status=LifecycleStatus.CURRENT,
            )
        )
    return result.target_id


def make_refinamento_doc():
    return make_doc(
        "inbox/refinamento-rf042.md",
        "md",
        {"initiative_id": "INI-008", "phase": "refinement", "title": "RF-042"},
        [
            mblock(1, "Refinamento RF-042 detalha o novo limite de tentativas.", "resumo"),
            mblock(2, "Propomos alterar RN-023 para o limite de 2000.", "proposta"),
        ],
    )


class AliasResolutionWithAliasTest(unittest.TestCase):
    """CENARIO 1: RN-023 com stable_key composto + alias registrado."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onda10e-with-alias-")
        self.db_path = os.path.join(self.tmpdir, "knowledge.db")
        self.repo = Repository.open(self.db_path)

    def tearDown(self):
        self.repo.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rn023_with_composite_key_and_alias_creates_proposes_change_to(self):
        """Ingestao de RF-042 'propomos alterar RN-023' cria proposes_change_to."""
        # Seed RN-023 com alias
        rn_entity_id = seed_rn023_composite_stable_key(self.repo, NS, with_alias=True)

        # Verificar que stable_key literal nao resolve (camada a falha)
        from knowledge import identity as identity_mod
        literal = identity_mod.resolve_identity(self.repo.conn, NS, EntityType.BUSINESS_RULE, "RN-023")
        self.assertIsNone(
            literal,
            "stable_key literal 'RN-023' NAO deveria resolver (é o cenário esperado)"
        )

        # Mas find_by_alias deve resolver
        via_alias = identity_mod.find_by_alias(
            self.repo.conn, NS, "RN-023", entity_type=EntityType.BUSINESS_RULE
        )
        self.assertEqual(
            via_alias,
            rn_entity_id,
            f"find_by_alias deve resolver; expected {rn_entity_id}, got {via_alias}"
        )

        # Correlate o documento de refinamento
        doc = make_refinamento_doc()
        candidates = extract_candidates(doc)
        result = corr.correlate(candidates, doc, self.repo, NS)

        # Aceite: sem erro
        self.assertIsNone(
            result.error,
            f"correlate nao deveria produzir erro, got {result.error}"
        )

        # Aceite: proposes_change_to criada
        proposes = [r for r in result.relations_written if r.relation_type == "proposes_change_to"]
        self.assertEqual(
            len(proposes),
            1,
            f"Deve criar 1 proposes_change_to, got {len(proposes)}"
        )
        self.assertEqual(
            proposes[0].target_entity_id,
            rn_entity_id,
            "proposes_change_to deve apontar para RN-023"
        )
        self.assertEqual(
            proposes[0].lifecycle,
            "proposed",
            "proposes_change_to deve ter lifecycle proposed"
        )
        self.assertEqual(
            proposes[0].epistemic,
            "supported",
            "afirmativa deve ter epistemic supported (regra Onda 9)"
        )

        # Aceite: referencias_orfas vazia (RN-023 resolveu)
        self.assertEqual(
            result.referencias_orfas,
            (),
            "RN-023 resolveu via alias, nao deveria ficar orfa"
        )

        # Aceite: completa=True
        self.assertTrue(
            result.completa,
            "resultado deve estar completo"
        )


class AliasResolutionWithoutAliasTest(unittest.TestCase):
    """CENARIO 2: RN-023 com stable_key composto, SEM alias resolvivel."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onda10e-no-alias-")
        self.db_path = os.path.join(self.tmpdir, "knowledge.db")
        self.repo = Repository.open(self.db_path)

    def tearDown(self):
        self.repo.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rn023_without_alias_creates_orphan_reference(self):
        """Sem alias, RN-023 fica orfa; lote NAO trava."""
        # Seed RN-023 SEM alias
        rn_entity_id = seed_rn023_composite_stable_key(self.repo, NS, with_alias=False)

        # Correlate o documento de refinamento
        doc = make_refinamento_doc()
        candidates = extract_candidates(doc)
        result = corr.correlate(candidates, doc, self.repo, NS)

        # Aceite W5: lote NAO deve travar
        self.assertIsNone(
            result.error,
            f"correlate NAO deve produzir erro (lote nao trava), got {result.error}"
        )

        # Aceite: nenhuma proposes_change_to criada (nao resolveu)
        proposes = [r for r in result.relations_written if r.relation_type == "proposes_change_to"]
        self.assertEqual(
            len(proposes),
            0,
            f"Sem alias, nao deveria criar proposes_change_to, got {len(proposes)}"
        )

        # Aceite: RN-023 aparece em referencias_orfas
        self.assertGreater(
            len(result.referencias_orfas),
            0,
            "Deve haver referencias_orfas"
        )
        self.assertEqual(
            result.referencias_orfas[0]["id"],
            "RN-023",
            "referencias_orfas deve conter RN-023"
        )
        self.assertIn(
            "p2",
            result.referencias_orfas[0]["blocos"],
            "Deve apontar para bloco p2 (onde esta 'Propomos alterar RN-023')"
        )

        # Aceite: completa=False
        self.assertFalse(
            result.completa,
            "resultado deve estar incompleto (referencia nao resolvida)"
        )

        # Aceite: motivo_incompleta menciona RN-023
        self.assertIn(
            "RN-023",
            result.motivo_incompleta,
            f"motivo_incompleta deve mencionar RN-023, got {result.motivo_incompleta}"
        )

        # Aceite: pending_decisions deve trazer a pendencia
        pend_keys = {p.key for p in result.pending_decisions}
        self.assertIn(
            "referencia_orfa",
            pend_keys,
            f"pending_decisions deve conter 'referencia_orfa', got {pend_keys}"
        )


if __name__ == "__main__":
    unittest.main()
