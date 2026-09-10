from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from wiki_ai.ingestion import pipeline
from wiki_ai.ingestion.finding import (
    DEFAULT_NAMESPACE,
    DocumentEvidenceRef,
    DocumentFinding,
    owner_from_captures,
    verify,
)
from wiki_ai.ingestion.harness import DocumentHarness
from wiki_ai.ingestion.integration import IngestionEngine
from wiki_ai.ingestion.outcome import IngestionStatus, SemanticFault
from wiki_ai.ingestion.semantic import SemanticInvestigator
from wiki_ai.knowledge.gaps import open_gaps
from wiki_ai.knowledge.identity import contextual_key
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind

from tests.ingestion import fixtures
from tests.ingestion.fake_provider import FakeProvider, Payloads, Script, evidence_of

RULE_BODY: dict[str, Any] = {
    "statement": "limite mantido quando o corte cai no dia 5",
    "conditions": ("corte no dia 5",),
    "effects": ("limite mantido",),
}


@pytest.fixture()
def knowledge(tmp_path: Path) -> KnowledgeRepository:
    repository = KnowledgeRepository.open(str(tmp_path / "knowledge.sqlite3"))
    yield repository
    repository.close()


def _transcript(tmp_path: Path, name: str = "reuniao.vtt") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / name
    target.write_text(fixtures.VTT, encoding="utf-8")
    return target


def _rule_provider(source: Path, speaker: str, subject: str) -> FakeProvider:
    harness = DocumentHarness(pipeline.ingest(source).document)
    block = harness.invoke("doc.blocks", {"speaker": speaker})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "business_rule",
                "subject": subject,
                "statement": "Decidimos manter o corte no dia 5",
                "conditions": ["corte no dia 5"],
                "effects": ["limite mantido"],
                "evidence": evidence_of(payloads),
            }
        ]

    return FakeProvider(
        scripts=[
            Script(
                steps=(("evidence.capture", {"block_ids": [block]}),),
                build_findings=build,
            )
        ]
    )


def test_a_finding_without_an_owner_keeps_the_global_owner_slot() -> None:
    finding = DocumentFinding(
        type=EntityKind.BUSINESS_RULE, subject="Eligibility", **RULE_BODY
    )
    assert finding.owner is None
    assert finding.namespace == DEFAULT_NAMESPACE
    assert finding.stable_key == contextual_key(
        DEFAULT_NAMESPACE, EntityKind.BUSINESS_RULE.value, None, "Eligibility"
    )


def test_the_owner_separates_two_rules_with_the_same_name() -> None:
    left = DocumentFinding(
        type=EntityKind.BUSINESS_RULE,
        subject="Eligibility",
        owner="Credito",
        **RULE_BODY,
    )
    right = DocumentFinding(
        type=EntityKind.BUSINESS_RULE,
        subject="Eligibility",
        owner="Cobranca",
        **RULE_BODY,
    )
    assert left.stable_key != right.stable_key


def test_an_explicit_id_wins_over_namespace_kind_and_owner() -> None:
    finding = DocumentFinding(
        type=EntityKind.BUSINESS_RULE,
        subject="Eligibility",
        owner="Credito",
        explicit_id="RN-014",
        **RULE_BODY,
    )
    assert finding.stable_key == contextual_key(
        "outro", EntityKind.BUSINESS_RULE.value, "Cobranca", "Outro", "RN-014"
    )


def test_the_namespace_participates_in_the_key() -> None:
    base = DocumentFinding(
        type=EntityKind.BUSINESS_RULE, subject="Eligibility", **RULE_BODY
    )
    assert base.with_context("credito", None).stable_key != base.with_context(
        "cobranca", None
    ).stable_key


def test_with_context_never_overwrites_an_owner_the_finding_declared() -> None:
    finding = DocumentFinding(
        type=EntityKind.BUSINESS_RULE,
        subject="Eligibility",
        owner="Credito",
        **RULE_BODY,
    )
    assert finding.with_context("reuniao", "Cobranca").owner == "Credito"


def test_the_owner_falls_back_to_the_locator_of_the_captured_block(
    tmp_path: Path,
) -> None:
    source = _transcript(tmp_path)
    ingested = pipeline.ingest(source)
    harness = DocumentHarness(ingested.document)
    block = harness.invoke("doc.blocks", {"speaker": "Ana"})["blocks"][0]["block_id"]
    capture = harness.invoke("evidence.capture", {"block_ids": [block]})
    finding = DocumentFinding(
        type=EntityKind.DECISION_RECORD,
        subject="corte no dia 5",
        statement="Decidimos manter o corte no dia 5",
        evidence=(DocumentEvidenceRef(capture_id=capture["capture_id"]),),
    )
    checked, _ = verify((finding,), harness, "reuniao")
    assert checked[0].finding.owner == "speaker:Ana"
    assert checked[0].stable_key.startswith("reuniao::decision_record::speaker_ana::")


def test_owner_from_captures_is_empty_without_a_usable_locator() -> None:
    assert owner_from_captures(()) == ""


def test_the_owner_entity_is_written_and_referenced_by_owner_id(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = _transcript(tmp_path)
    ingested = pipeline.ingest(source)
    provider = _rule_provider(source, "Ana", "Eligibility")
    SemanticInvestigator().run(ingested, knowledge, provider, "credito")
    rule = knowledge.find_entities("business_rule")[0]
    assert rule.owner_id is not None
    owner = knowledge.get_entity(rule.owner_id)
    assert owner is not None
    assert owner.kind == EntityKind.SOURCE.value
    assert owner.name == rule.attributes["owner"]


def test_two_namespaces_never_overwrite_the_same_named_rule(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    credito = _transcript(tmp_path / "credito", "reuniao.vtt")
    cobranca = _transcript(tmp_path / "cobranca", "reuniao.vtt")
    IngestionEngine(
        provider_resolver=lambda: _rule_provider(credito, "Ana", "Eligibility")
    ).run(credito, "", knowledge, "credito")
    IngestionEngine(
        provider_resolver=lambda: _rule_provider(cobranca, "Bruno", "Eligibility")
    ).run(cobranca, "", knowledge, "cobranca")
    rules = knowledge.find_entities("business_rule")
    assert len(rules) == 2
    assert {item.owner_id for item in rules} != {None}
    assert len({item.id.value for item in rules}) == 2


def test_reingesting_the_same_source_stays_idempotent(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = _transcript(tmp_path)
    engine = IngestionEngine(
        provider_resolver=lambda: _rule_provider(source, "Ana", "Eligibility")
    )
    first = engine.run(source, "", knowledge, "credito")
    second = engine.run(source, first.version_hash, knowledge, "credito")
    assert first.status is IngestionStatus.COMPLETE
    assert second.status is IngestionStatus.COMPLETE
    assert len(knowledge.find_entities("business_rule")) == 1


def _explicit_rule_provider(source: Path, speaker: str, subject: str) -> FakeProvider:
    harness = DocumentHarness(pipeline.ingest(source).document)
    block = harness.invoke("doc.blocks", {"speaker": speaker})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "business_rule",
                "subject": subject,
                "explicit_id": "RN-014",
                "statement": "Decidimos manter o corte no dia 5",
                "conditions": ["corte no dia 5"],
                "effects": ["limite mantido"],
                "evidence": evidence_of(payloads),
            }
        ]

    return FakeProvider(
        scripts=[
            Script(
                steps=(("evidence.capture", {"block_ids": [block]}),),
                build_findings=build,
            )
        ]
    )


def test_two_findings_sharing_an_explicit_id_collide_instead_of_overwriting(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = _transcript(tmp_path)
    ingested = pipeline.ingest(source)
    SemanticInvestigator().run(
        ingested,
        knowledge,
        _explicit_rule_provider(source, "Ana", "Elegibilidade de credito"),
        "credito",
    )
    original = knowledge.find_entities("business_rule")[0]
    outcome = SemanticInvestigator().run(
        ingested,
        knowledge,
        _explicit_rule_provider(source, "Bruno", "Politica de cobranca"),
        "cobranca",
    )
    survivor = knowledge.get_entity(original.id)
    assert survivor is not None
    assert survivor.name == original.name
    assert SemanticFault.IDENTITY_COLLISION in outcome.faults
    assert outcome.gaps_opened
    assert outcome.entities_written == 0


def test_a_collision_is_reported_as_a_blocking_gap_in_the_knowledge_base(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = _transcript(tmp_path)
    ingested = pipeline.ingest(source)
    SemanticInvestigator().run(
        ingested,
        knowledge,
        _explicit_rule_provider(source, "Ana", "Elegibilidade de credito"),
        "credito",
    )
    SemanticInvestigator().run(
        ingested,
        knowledge,
        _explicit_rule_provider(source, "Bruno", "Politica de cobranca"),
        "cobranca",
    )
    questions = [item.name for item in open_gaps(knowledge)]
    assert any("ent_" in item for item in questions)
