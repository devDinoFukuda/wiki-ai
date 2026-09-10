from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from wiki_ai.ingestion import pipeline
from wiki_ai.ingestion.harness import DocumentHarness
from wiki_ai.ingestion.integration import (
    PROVIDER_UNAVAILABLE,
    VERSION_MISMATCH,
    IngestionEngine,
    IngestionStatus,
)
from wiki_ai.knowledge.model import KnowledgeState
from wiki_ai.knowledge.repository import KnowledgeRepository

from tests.ingestion import fixtures
from tests.ingestion.fake_provider import FakeProvider, Payloads, Script, evidence_of


@pytest.fixture()
def knowledge(tmp_path: Path) -> KnowledgeRepository:
    repository = KnowledgeRepository.open(str(tmp_path / "knowledge.sqlite3"))
    yield repository
    repository.close()


def _transcript(tmp_path: Path) -> Path:
    target = tmp_path / "reuniao.vtt"
    target.write_text(fixtures.VTT, encoding="utf-8")
    return target


def _decision_provider(source: Path) -> FakeProvider:
    harness = DocumentHarness(pipeline.ingest(source).document)
    block = harness.invoke("doc.blocks", {"speaker": "Ana"})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "decision_record",
                "subject": "corte no dia 5",
                "statement": "Decidimos manter o corte no dia 5",
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


def test_without_a_provider_the_outcome_is_honest(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = _transcript(tmp_path)
    engine = IngestionEngine(provider_resolver=lambda: None)
    outcome = engine.run(source, "", knowledge, "reuniao")
    assert outcome.entities_written == 0
    assert outcome.blocks == 2
    assert PROVIDER_UNAVAILABLE in outcome.diagnostics
    assert knowledge.find_entities() == []
    versions = knowledge.source_versions(outcome.source_id)
    assert [item.version_hash for item in versions] == [outcome.version_hash]


def test_a_provider_turns_blocks_into_entities(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = _transcript(tmp_path)
    provider = _decision_provider(source)
    engine = IngestionEngine(provider_resolver=lambda: provider)
    outcome = engine.run(source, "", knowledge, "reuniao")
    assert outcome.entities_written == 1
    assert PROVIDER_UNAVAILABLE not in outcome.diagnostics
    decision = knowledge.find_entities("decision_record")[0]
    assert decision.state is KnowledgeState.DECLARED


def test_reingesting_the_same_source_does_not_duplicate(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = _transcript(tmp_path)
    engine = IngestionEngine(provider_resolver=lambda: _decision_provider(source))
    first = engine.run(source, "", knowledge, "reuniao")
    second = engine.run(source, first.version_hash, knowledge, "reuniao")
    assert first.source_id == second.source_id
    assert first.version_hash == second.version_hash
    assert len(knowledge.source_versions(first.source_id)) == 1
    assert len(knowledge.find_entities("decision_record")) == 1
    assert VERSION_MISMATCH not in second.diagnostics


def test_a_declared_version_hash_that_disagrees_is_reported(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = _transcript(tmp_path)
    engine = IngestionEngine(provider_resolver=lambda: None)
    outcome = engine.run(source, "0" * 64, knowledge, "reuniao")
    assert VERSION_MISMATCH in outcome.diagnostics


def test_an_image_only_pdf_is_partial_with_an_explicit_gap(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    target = tmp_path / "digitalizado.pdf"
    target.write_bytes(fixtures.image_only_pdf())
    engine = IngestionEngine(provider_resolver=lambda: None)
    outcome = engine.run(target, "", knowledge, "digitalizado")
    assert "warning:image_content_not_interpreted" in outcome.diagnostics
    assert "source_partially_interpreted" in outcome.diagnostics
    assert outcome.entities_written == 0


def test_structure_alone_never_becomes_knowledge(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = fixtures.write_xlsx(tmp_path / "planilha.xlsx")
    engine = IngestionEngine(provider_resolver=lambda: None)
    outcome = engine.run(source, "", knowledge, "planilha")
    assert outcome.blocks > 0
    assert outcome.entities_written == 0
    assert knowledge.entity_count() == 0


def test_a_silent_provider_writes_nothing_but_keeps_the_source(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = _transcript(tmp_path)
    engine = IngestionEngine(provider_resolver=lambda: FakeProvider(scripts=[Script()]))
    outcome = engine.run(source, "", knowledge, "reuniao")
    assert outcome.entities_written == 0
    assert knowledge.source_versions(outcome.source_id)


def test_the_outcome_shape_matches_the_application_port(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    from wiki_ai.app.ports import IngestionOutcome, OutcomeStatus

    source = _transcript(tmp_path)
    engine = IngestionEngine(provider_resolver=lambda: None)
    outcome = engine.run(source, "", knowledge, "reuniao")
    port = IngestionOutcome(
        source_id=outcome.source_id,
        version_hash=outcome.version_hash,
        blocks=outcome.blocks,
        entities_written=outcome.entities_written,
        status=OutcomeStatus(outcome.status.value),
        reason=outcome.reason,
        diagnostics=outcome.diagnostics,
    )
    assert port.to_dict() == outcome.to_dict()
    assert {item.value for item in OutcomeStatus} == {
        item.value for item in IngestionStatus
    }
