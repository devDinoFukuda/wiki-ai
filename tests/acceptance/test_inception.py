from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from tests.acceptance.scenarios import (
    CAPABILITY,
    MIGRATION_DOCX_BODY,
    NAMESPACE_OBJECTIVE,
    java_acceptance_repo,
    java_scripts,
    scripted_registry,
)
from tests.ingestion import fixtures
from tests.ingestion.fake_provider import FakeProvider as DocumentProvider
from tests.ingestion.fake_provider import Payloads
from tests.ingestion.fake_provider import Script as DocumentScript
from tests.ingestion.fake_provider import evidence_of
from wiki_ai.agent.protocol import AgentCapabilities
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.agent.session import AgentRun, AgentSession
from wiki_ai.app import api
from wiki_ai.app.session import Session
from wiki_ai.ingestion import pipeline
from wiki_ai.ingestion.harness import DocumentHarness
from wiki_ai.ingestion.source import SourceKind
from wiki_ai.knowledge.evidence import (
    DiagramLocator,
    DocumentLocator,
    SpreadsheetLocator,
    TranscriptLocator,
)
from wiki_ai.knowledge.model import EpistemicStatus
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind

QUESTION = "Quais sistemas, regras e integrações tornam a migração complexa?"

PROPOSAL = {
    "type": "proposal",
    "subject": "migrar place order para eventos",
    "statement": (
        "Propomos migrar place order para uma arquitetura orientada a eventos "
        "mantendo orders kafka producer"
    ),
    "attributes": {
        "statement": (
            "Propomos migrar place order para uma arquitetura orientada a eventos"
        ),
        "aliases": ["place order"],
    },
}

DECISION = {
    "type": "decision_record",
    "subject": "place order",
    "statement": "Decidimos manter place order como a capacidade central de pedidos",
    "attributes": {"decision": "place order continua sendo a capacidade central"},
}

SHEET_RULE = {
    "type": "business_rule",
    "subject": "every placed order is saved",
    "statement": "A planilha registra que cada pedido colocado precisa ser gravado",
    "conditions": ["um pedido é colocado"],
    "effects": ["o pedido é gravado"],
}

DIAGRAM_INTEGRATION = {
    "type": "integration",
    "subject": "orders kafka producer",
    "statement": "O diagrama mostra a API de cobranca gravando no banco pelo produtor",
    "attributes": {"direction": "outbound", "protocol": "kafka"},
}


class _DocumentBackedProvider(DocumentProvider):
    def connect(self) -> None:
        return None

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(tools=("doc.blocks", "evidence.capture"))

    def cancel(self) -> None:
        return None

    def run(self, session: AgentSession) -> AgentRun:
        return super().run(session)


def _document_registry(
    source: Path, findings: Sequence[Mapping[str, Any]]
) -> ProviderRegistry:
    harness = DocumentHarness(pipeline.ingest(source).document)
    identifier = harness.invoke("doc.blocks", {})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[dict[str, Any]]:
        evidence = evidence_of(payloads)
        return [{**dict(item), "evidence": evidence} for item in findings]

    provider = _DocumentBackedProvider(
        scripts=[
            DocumentScript(
                steps=(("evidence.capture", {"block_ids": [identifier]}),),
                build_findings=build,
            )
        ]
    )
    registry = ProviderRegistry()
    registry.register("scripted", lambda: provider)
    return registry


@pytest.fixture()
def sources(tmp_path: Path) -> dict[str, Path]:
    transcript = tmp_path / "inception.vtt"
    transcript.write_text(fixtures.VTT, encoding="utf-8")
    sheet = fixtures.write_xlsx(tmp_path / "regras.xlsx")
    diagram = tmp_path / "arquitetura.drawio"
    diagram.write_text(fixtures.drawio_document(), encoding="utf-8")
    document = fixtures.write_docx(tmp_path / "proposta.docx", MIGRATION_DOCX_BODY)
    return {
        "transcript": transcript,
        "sheet": sheet,
        "diagram": diagram,
        "document": document,
    }


@pytest.fixture()
def inception(tmp_path: Path, sources: dict[str, Path]) -> Path:
    repo = java_acceptance_repo(tmp_path / "repo")
    analyzed = api.analyze(repo, NAMESPACE_OBJECTIVE, registry=scripted_registry(java_scripts()))
    assert analyzed.status == "ok", analyzed.to_dict()
    for key, findings in (
        ("transcript", [DECISION]),
        ("sheet", [SHEET_RULE]),
        ("diagram", [DIAGRAM_INTEGRATION]),
        ("document", [PROPOSAL]),
    ):
        report = api.ingest(
            sources[key], repo, registry=_document_registry(sources[key], findings)
        )
        assert report.status == "ok", report.to_dict()
        assert report.details["entities_written"] == len(findings), key
    return repo


def test_all_five_source_kinds_are_registered(
    inception: Path, sources: dict[str, Path]
) -> None:
    report = api.status(inception)
    by_kind = dict(report.sources_by_kind)
    assert by_kind[SourceKind.CODEBASE.value] >= 1
    assert by_kind[SourceKind.TRANSCRIPT.value] == 1
    assert by_kind[SourceKind.XLSX.value] == 1
    assert by_kind[SourceKind.DRAWIO.value] == 1
    assert by_kind[SourceKind.DOCX.value] == 1


def test_the_ingested_entities_stay_declared_and_carry_their_own_provenance(
    inception: Path,
) -> None:
    expected = (
        (EntityKind.PROPOSAL.value, DocumentLocator, EpistemicStatus.PROPOSED),
        (
            EntityKind.DECISION_RECORD.value,
            TranscriptLocator,
            EpistemicStatus.DECLARED,
        ),
    )
    with Session.open(inception).open_knowledge() as knowledge:
        for kind, locator_type, epistemic in expected:
            entities = knowledge.find_entities(kind)
            assert entities, kind
            entity = entities[0]
            assert entity.epistemic is epistemic
            assert entity.epistemic is not EpistemicStatus.IMPLEMENTED
            evidence = knowledge.evidence_for(entity.id)
            assert evidence, kind
            assert isinstance(evidence[0].locator, locator_type)


def test_the_spreadsheet_and_the_diagram_reach_the_knowledge_with_their_locators(
    inception: Path,
) -> None:
    with Session.open(inception).open_knowledge() as knowledge:
        locators = [
            evidence.locator
            for entity in knowledge.find_entities()
            for evidence in knowledge.evidence_for(entity.id)
        ]
        assert any(isinstance(item, SpreadsheetLocator) for item in locators)
        assert any(isinstance(item, DiagramLocator) for item in locators)


def test_the_correlation_binds_the_documents_to_the_implemented_code(
    inception: Path,
) -> None:
    with Session.open(inception).open_knowledge() as knowledge:
        kinds = {relation.kind for relation in knowledge.find_relations()}
        assert RelationKind.DECLARES.value in kinds
        assert RelationKind.PROPOSES_CHANGE_TO.value in kinds
        assert RelationKind.AFFECTS.value in kinds


def test_the_inception_question_correlates_every_source_with_its_provenance(
    inception: Path,
) -> None:
    report = api.ask(QUESTION, inception)
    assert report.status == "ok"
    assert report.evidence_ids
    answer = report.answer
    assert "migrar place order para eventos" in answer
    assert CAPABILITY in answer
    assert "código" in answer
    assert "transcrição" in answer
    assert "planilha" in answer
    assert "diagrama" in answer
    assert "documento" in answer


def test_the_answer_evidence_covers_the_five_source_kinds(inception: Path) -> None:
    report = api.ask(QUESTION, inception)
    with Session.open(inception).open_knowledge() as knowledge:
        found = {
            knowledge.get_evidence(identifier).locator.kind
            for identifier in report.evidence_ids
        }
    assert {"code", "transcript", "spreadsheet", "diagram", "document"} <= found


def test_the_answer_lists_what_has_no_evidence(inception: Path) -> None:
    report = api.ask(QUESTION, inception)
    assert "Sem evidência:" in report.answer
    assert report.unresolved
