from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from wiki_ai.ingestion import pipeline
from wiki_ai.ingestion.harness import DocumentHarness
from wiki_ai.ingestion.semantic import (
    DEFAULT_ROUND_BUDGET,
    Rejection,
    SemanticInvestigator,
    document_finding_schema,
)
from wiki_ai.ingestion.source import SourceKind
from wiki_ai.knowledge.evidence import DiagramLocator, SpreadsheetLocator, TranscriptLocator
from wiki_ai.knowledge.gaps import GAP_KIND, questions
from wiki_ai.knowledge.model import Confidence, EpistemicStatus
from wiki_ai.knowledge.repository import KnowledgeRepository

from tests.ingestion import fixtures
from tests.ingestion.fake_provider import FakeProvider, Payloads, Script, evidence_of


@pytest.fixture()
def knowledge(tmp_path: Path) -> KnowledgeRepository:
    repository = KnowledgeRepository.open(str(tmp_path / "knowledge.sqlite3"))
    yield repository
    repository.close()


def _investigator() -> SemanticInvestigator:
    return SemanticInvestigator(round_budget=DEFAULT_ROUND_BUDGET)


def _transcript(tmp_path: Path) -> Path:
    target = tmp_path / "reuniao.vtt"
    target.write_text(fixtures.VTT, encoding="utf-8")
    return target


def _entities(repository: KnowledgeRepository, kind: str) -> list[Any]:
    return [item for item in repository.find_entities(kind)]


def test_transcript_yields_declared_and_supported_findings(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    harness = DocumentHarness(ingested.document)
    ana = harness.invoke("doc.blocks", {"speaker": "Ana"})["blocks"][0]["block_id"]
    bruno = harness.invoke("doc.blocks", {"speaker": "Bruno"})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "decision_record",
                "subject": "corte no dia 5",
                "statement": "Decidimos manter o corte no dia 5",
                "evidence": evidence_of(payloads, 0),
            },
            {
                "type": "requirement",
                "subject": "limite de 500 documentado",
                "statement": "Preciso do limite de 500 documentado",
                "evidence": evidence_of(payloads, 1),
            },
            {
                "type": "proposal",
                "subject": "confirmacao do time financeiro",
                "statement": "O time financeiro confirma amanha",
                "proposed": True,
                "evidence": evidence_of(payloads, 0),
            },
        ]

    provider = FakeProvider(
        scripts=[
            Script(
                steps=(
                    ("doc.outline", {}),
                    ("doc.blocks", {"speaker": "Ana"}),
                    ("evidence.capture", {"block_ids": [ana]}),
                    ("doc.blocks", {"speaker": "Bruno"}),
                    ("evidence.capture", {"block_ids": [bruno]}),
                ),
                build_findings=build,
            )
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "reuniao")
    assert outcome.entities_written == 3
    assert outcome.evidence_written == 2

    decision = _entities(knowledge, "decision_record")[0]
    assert decision.epistemic is EpistemicStatus.DECLARED
    assert decision.confidence is Confidence.SUPPORTED
    requirement = _entities(knowledge, "requirement")[0]
    assert requirement.epistemic is EpistemicStatus.DECLARED
    proposal = _entities(knowledge, "proposal")[0]
    assert proposal.epistemic is EpistemicStatus.PROPOSED

    evidence = knowledge.evidence_for(decision.id)
    assert len(evidence) == 1
    locator = evidence[0].locator
    assert isinstance(locator, TranscriptLocator)
    assert locator.speaker == "Ana"
    assert locator.time_start == 12.0


def test_spreadsheet_rule_matrix_becomes_a_business_rule(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(fixtures.write_xlsx(tmp_path / "planilha.xlsx"))

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        table = payloads["doc.table"][0]
        row = table["rows"][0]
        return [
            {
                "type": "business_rule",
                "subject": row[0],
                "statement": f"{row[0]}: {row[1]} entao {row[3]}",
                "conditions": [row[1], f"limite {row[2]}"],
                "effects": [row[3]],
                "attributes": {"owner": row[4]},
                "evidence": evidence_of(payloads),
            }
        ]

    harness = DocumentHarness(ingested.document)
    region = harness.invoke("doc.table", {"worksheet": "Regras", "cell_range": "B13:F13"})
    provider = FakeProvider(
        scripts=[
            Script(
                steps=(
                    ("doc.outline", {}),
                    ("doc.table", {"worksheet": "Regras", "cell_range": "B12:F27"}),
                    ("evidence.capture", {"block_ids": region["block_ids"]}),
                ),
                build_findings=build,
            )
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "planilha")
    assert outcome.entities_written == 1

    rule = _entities(knowledge, "business_rule")[0]
    assert rule.name == "Desconto A"
    assert rule.confidence is Confidence.SUPPORTED
    assert rule.epistemic is EpistemicStatus.DECLARED
    assert list(rule.attributes["conditions"]) == ["valor > 100", "limite 100"]
    assert list(rule.attributes["effects"]) == ["aplicar"]

    locator = knowledge.evidence_for(rule.id)[0].locator
    assert isinstance(locator, SpreadsheetLocator)
    assert locator.worksheet == "Regras"
    assert locator.cell_range == "B13:F13"


def test_diagram_yields_modules_and_a_calls_relation(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    target = tmp_path / "arq.drawio"
    target.write_text(fixtures.drawio_document(), encoding="utf-8")
    ingested = pipeline.ingest(target)
    harness = DocumentHarness(ingested.document)
    graph = harness.invoke("doc.graph", {"page": "1"})
    nodes = {item["node"]: item["block_id"] for item in graph["nodes"]}

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "module",
                "subject": "API Cobranca",
                "statement": "API Cobranca desenhada na pagina 1",
                "evidence": evidence_of(payloads, 0),
                "relations": [
                    {
                        "kind": "calls",
                        "target_subject": "Banco",
                        "target_type": "persistence",
                    }
                ],
            },
            {
                "type": "integration",
                "subject": "Banco",
                "statement": "Banco recebe grava da API Cobranca",
                "attributes": {"direction": "outbound", "protocol": "jdbc"},
                "evidence": evidence_of(payloads, 1),
            },
        ]

    provider = FakeProvider(
        scripts=[
            Script(
                steps=(
                    ("doc.graph", {"page": "1"}),
                    ("evidence.capture", {"block_ids": [nodes["n1"]]}),
                    ("evidence.capture", {"block_ids": [nodes["n2"]]}),
                ),
                build_findings=build,
            )
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "arquitetura")
    assert outcome.entities_written >= 2
    assert outcome.relations_written == 1

    module = _entities(knowledge, "module")[0]
    assert module.epistemic is EpistemicStatus.DECLARED
    relation = knowledge.find_relations("calls")[0]
    assert relation.source_id == module.id

    locator = knowledge.evidence_for(module.id)[0].locator
    assert isinstance(locator, DiagramLocator)
    assert locator.page == "1"
    assert locator.node == "n1"


def test_document_yields_a_requirement_with_a_document_locator(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(fixtures.write_docx(tmp_path / "regras.docx"))
    harness = DocumentHarness(ingested.document)
    block = harness.invoke("doc.blocks", {"kinds": ["paragraph"]})["blocks"][0]

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "requirement",
                "subject": "faturamento no dia 5",
                "statement": "O faturamento roda no dia 5",
                "evidence": evidence_of(payloads),
            }
        ]

    provider = FakeProvider(
        scripts=[
            Script(
                steps=(
                    ("doc.outline", {}),
                    ("doc.read", {"block_id": block["block_id"]}),
                    ("evidence.capture", {"block_ids": [block["block_id"]]}),
                ),
                build_findings=build,
            )
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "regras")
    assert outcome.entities_written == 1
    requirement = _entities(knowledge, "requirement")[0]
    assert requirement.epistemic is EpistemicStatus.DECLARED
    assert requirement.confidence is Confidence.SUPPORTED
    locator = knowledge.evidence_for(requirement.id)[0].locator
    assert locator.kind == "document"
    assert locator.heading_path == ("Regras de Faturamento", "Escopo")


def test_forged_evidence_is_rejected(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    provider = FakeProvider(
        scripts=[
            Script(
                findings=[
                    {
                        "type": "decision_record",
                        "subject": "corte no dia 5",
                        "statement": "Decidimos manter o corte",
                        "evidence": [{"capture_id": "cap-000000000000000000000000"}],
                    }
                ]
            )
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "reuniao")
    assert outcome.verified[0].confidence is Confidence.UNRESOLVED
    assert Rejection.EVIDENCE_UNRESOLVED in outcome.verified[0].rejections
    assert _entities(knowledge, "decision_record")[0].confidence is Confidence.UNRESOLVED


def test_a_tampered_excerpt_hash_is_rejected_and_not_persisted(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    harness = DocumentHarness(ingested.document)
    block = harness.invoke("doc.blocks", {"speaker": "Ana"})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        capture = payloads["captures"][0]
        return [
            {
                "type": "decision_record",
                "subject": "corte no dia 5",
                "statement": "Decidimos manter o corte no dia 5",
                "evidence": [
                    {"capture_id": capture["capture_id"], "excerpt_hash": "0" * 64}
                ],
            }
        ]

    provider = FakeProvider(
        scripts=[
            Script(
                steps=(("evidence.capture", {"block_ids": [block]}),),
                build_findings=build,
            )
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "reuniao")
    assert Rejection.EVIDENCE_TAMPERED in outcome.verified[0].rejections
    assert outcome.entities_written == 0
    assert _entities(knowledge, "decision_record") == []


def test_a_finding_without_evidence_opens_a_gap(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    provider = FakeProvider(
        scripts=[
            Script(
                findings=[
                    {
                        "type": "requirement",
                        "subject": "limite de 500",
                        "statement": "Preciso do limite de 500 documentado",
                    }
                ]
            )
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "reuniao")
    assert outcome.verified[0].confidence is Confidence.UNRESOLVED
    assert Rejection.NO_EVIDENCE in outcome.verified[0].rejections
    opened = questions(tuple(_entities(knowledge, GAP_KIND)))
    assert any("limite de 500" in item for item in opened)


def test_a_declared_gap_question_reaches_knowledge(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    harness = DocumentHarness(ingested.document)
    block = harness.invoke("doc.blocks", {"speaker": "Bruno"})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "requirement",
                "subject": "limite de 500 documentado",
                "statement": "Preciso do limite de 500 documentado",
                "evidence": evidence_of(payloads),
                "gap_question": "quem aprova o limite de 500?",
            }
        ]

    provider = FakeProvider(
        scripts=[
            Script(
                steps=(("evidence.capture", {"block_ids": [block]}),),
                build_findings=build,
            )
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "reuniao")
    assert "quem aprova o limite de 500?" in outcome.gaps_opened


def test_a_document_never_produces_implemented_knowledge(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(fixtures.write_docx(tmp_path / "regras.docx"))
    harness = DocumentHarness(ingested.document)
    block = harness.invoke("doc.blocks", {"kinds": ["paragraph"]})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "business_rule",
                "subject": "faturamento no dia 5",
                "statement": "O faturamento roda no dia 5",
                "conditions": ["dia 5"],
                "effects": ["faturamento roda"],
                "attributes": {"epistemic": "implemented"},
                "evidence": evidence_of(payloads),
            }
        ]

    provider = FakeProvider(
        scripts=[
            Script(
                steps=(("evidence.capture", {"block_ids": [block]}),),
                build_findings=build,
            )
        ]
    )
    _investigator().run(ingested, knowledge, provider, "regras")
    for entity in knowledge.find_entities():
        assert entity.epistemic is not EpistemicStatus.IMPLEMENTED


def test_a_type_outside_the_source_kind_is_rejected(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    harness = DocumentHarness(ingested.document)
    block = harness.invoke("doc.blocks", {"speaker": "Ana"})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "endpoint",
                "subject": "POST /faturas",
                "statement": "Decidimos manter o corte no dia 5",
                "attributes": {"address": "POST /faturas"},
                "evidence": evidence_of(payloads),
            }
        ]

    provider = FakeProvider(
        scripts=[
            Script(
                steps=(("evidence.capture", {"block_ids": [block]}),),
                build_findings=build,
            )
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "reuniao")
    assert Rejection.TYPE_OUTSIDE_SOURCE_KIND in outcome.verified[0].rejections
    assert outcome.verified[0].confidence is Confidence.UNRESOLVED


def test_a_statement_absent_from_the_block_stays_inferred(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    harness = DocumentHarness(ingested.document)
    block = harness.invoke("doc.blocks", {"speaker": "Ana"})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "requirement",
                "subject": "criptografia obrigatoria",
                "statement": "tudo precisa trafegar cifrado",
                "evidence": evidence_of(payloads),
            }
        ]

    provider = FakeProvider(
        scripts=[
            Script(
                steps=(("evidence.capture", {"block_ids": [block]}),),
                build_findings=build,
            )
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "reuniao")
    assert outcome.verified[0].confidence is Confidence.INFERRED
    assert Rejection.STATEMENT_UNSUPPORTED_BY_BLOCK in outcome.verified[0].rejections


def test_a_hint_without_a_finding_writes_nothing(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    provider = FakeProvider(scripts=[Script(steps=(("doc.hints", {}),), findings=())])
    outcome = _investigator().run(ingested, knowledge, provider, "reuniao")
    assert outcome.entities_written == 0
    assert knowledge.find_entities() == []


def test_a_finding_contradicting_the_hint_prevails(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    harness = DocumentHarness(ingested.document)
    hinted = harness.invoke("doc.hints", {"candidate_kinds": ["decision"]})["hints"][0]
    assert hinted["candidate_kind"] == "decision"

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "proposal",
                "subject": "corte no dia 5",
                "statement": "Decidimos manter o corte no dia 5",
                "proposed": True,
                "evidence": evidence_of(payloads),
            }
        ]

    provider = FakeProvider(
        scripts=[
            Script(
                steps=(
                    ("doc.hints", {"candidate_kinds": ["decision"]}),
                    ("evidence.capture", {"block_ids": [hinted["block_id"]]}),
                ),
                build_findings=build,
            )
        ]
    )
    _investigator().run(ingested, knowledge, provider, "reuniao")
    assert _entities(knowledge, "decision_record") == []
    proposal = _entities(knowledge, "proposal")[0]
    assert proposal.epistemic is EpistemicStatus.PROPOSED


def test_the_briefing_and_schema_are_specific_to_the_source_kind(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(fixtures.write_xlsx(tmp_path / "planilha.xlsx"))
    provider = FakeProvider(scripts=[Script()])
    _investigator().run(ingested, knowledge, provider, "planilha")
    objective = provider.seen_objectives[0]
    assert "xlsx" in objective
    assert "worksheet:Regras" in objective
    assert "doc.table" in objective
    allowed = provider.seen_schemas[0]["properties"]["type"]["enum"]
    assert "business_rule" in allowed
    assert "endpoint" not in allowed
    assert provider.seen_tools[0] == tuple(sorted(DocumentHarness(ingested.document).names()))


def test_the_briefing_is_deterministic(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    first = FakeProvider(scripts=[Script()])
    second = FakeProvider(scripts=[Script()])
    _investigator().run(ingested, knowledge, first, "reuniao")
    _investigator().run(ingested, knowledge, second, "reuniao")
    assert first.seen_objectives[0] == second.seen_objectives[0]


def test_the_frontier_drives_further_rounds(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(fixtures.write_xlsx(tmp_path / "planilha.xlsx"))
    provider = FakeProvider(
        scripts=[
            Script(steps=(("doc.blocks", {"worksheet": "Regras"}),)),
            Script(steps=(("doc.blocks", {"worksheet": "Resumo"}),)),
            Script(steps=(("doc.blocks", {"worksheet": "Rascunho"}),)),
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "planilha")
    assert outcome.rounds == 3
    pending = provider.seen_objectives[2].split("Not covered yet:")[1].split("Already covered:")[0]
    assert "worksheet:Rascunho" in pending
    assert "worksheet:Regras" not in pending
    assert "worksheet:Resumo" not in pending


def test_the_total_budget_stops_the_loop(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(fixtures.write_xlsx(tmp_path / "planilha.xlsx"))
    provider = FakeProvider(
        scripts=[
            Script(steps=(("doc.blocks", {"worksheet": "Regras"}),)),
            Script(steps=(("doc.blocks", {"worksheet": "Resumo"}),)),
            Script(steps=(("doc.blocks", {"worksheet": "Rascunho"}),)),
        ]
    )
    investigator = SemanticInvestigator(round_budget=1, total_tool_calls=2)
    outcome = investigator.run(ingested, knowledge, provider, "planilha")
    assert outcome.tool_calls <= 2
    assert "semantic_investigation_budget_exhausted" in outcome.diagnostics


def test_a_failed_round_is_reported_and_stops_the_loop(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    provider = FakeProvider(scripts=[Script(fail_with="provider_lost_connection")])
    outcome = _investigator().run(ingested, knowledge, provider, "reuniao")
    assert outcome.rounds == 1
    assert any("provider_lost_connection" in item for item in outcome.diagnostics)


def test_the_finding_schema_defaults_to_the_full_taxonomy() -> None:
    everything = document_finding_schema()
    assert "endpoint" in everything["properties"]["type"]["enum"]
    restricted = document_finding_schema(SourceKind.DRAWIO)
    assert "endpoint" not in restricted["properties"]["type"]["enum"]
    assert "module" in restricted["properties"]["type"]["enum"]


def test_stable_keys_are_normalized_for_correlation(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    ingested = pipeline.ingest(_transcript(tmp_path))
    harness = DocumentHarness(ingested.document)
    block = harness.invoke("doc.blocks", {"speaker": "Ana"})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[Mapping[str, Any]]:
        return [
            {
                "type": "decision_record",
                "subject": "  Corte No Dia 5  ",
                "statement": "Decidimos manter o corte no dia 5",
                "evidence": evidence_of(payloads),
            }
        ]

    provider = FakeProvider(
        scripts=[
            Script(
                steps=(("evidence.capture", {"block_ids": [block]}),),
                build_findings=build,
            )
        ]
    )
    outcome = _investigator().run(ingested, knowledge, provider, "reuniao")
    assert outcome.verified[0].stable_key == "decision_record::corte no dia 5"
