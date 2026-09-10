from __future__ import annotations

from wiki_ai.ingestion.finding import (
    DocumentEvidenceRef,
    DocumentFinding,
    DocumentRelationClaim,
    VerifiedDocumentFinding,
    document_finding_schema,
)
from wiki_ai.ingestion.harness import DocumentEvidenceCapture
from wiki_ai.ingestion.relations import (
    TargetOutcome,
    TargetResolution,
    assess_relation,
)
from wiki_ai.ingestion.source import SourceKind
from wiki_ai.knowledge.evidence import excerpt_digest
from wiki_ai.knowledge.model import Confidence, KnowledgeState
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind

CAPTURE_ID = "cap-111111111111111111111111"


def _capture(excerpt: str) -> DocumentEvidenceCapture:
    return DocumentEvidenceCapture(
        capture_id=CAPTURE_ID,
        source_id="src-1",
        version_hash="a" * 64,
        source_kind=SourceKind.MARKDOWN,
        block_ids=("b1",),
        locator={"kind": "document", "section": "1"},
        excerpt=excerpt,
        excerpt_hash=excerpt_digest(excerpt),
    )


def _source(subject: str, claim: DocumentRelationClaim) -> VerifiedDocumentFinding:
    finding = DocumentFinding(
        type=EntityKind.MODULE,
        subject=subject,
        statement=f"{subject} esta descrito no documento",
        relations=(claim,),
    )
    return VerifiedDocumentFinding(
        finding=finding,
        confidence=Confidence.SUPPORTED,
        state=KnowledgeState.DECLARED,
    )


def _claim(
    kind: RelationKind, target: str, statement: str
) -> DocumentRelationClaim:
    return DocumentRelationClaim(
        kind=kind,
        target_subject=target,
        target_type=EntityKind.MODULE,
        statement=statement,
        evidence=(DocumentEvidenceRef(capture_id=CAPTURE_ID),),
    )


def _resolution() -> TargetResolution:
    return TargetResolution(
        outcome=TargetOutcome.CONTEXTUAL, key="doc::module::Cobranca", kind="module"
    )


def _verdict(
    kind: RelationKind, target: str, statement: str, excerpt: str
) -> Confidence:
    claim = _claim(kind, target, statement)
    return assess_relation(
        claim,
        _source("Faturamento", claim),
        _resolution(),
        target,
        (_capture(excerpt),),
        False,
    ).confidence


def test_a_configuring_sentence_never_supports_a_calls_relation() -> None:
    verdict = _verdict(
        RelationKind.CALLS,
        "Cobranca",
        "Faturamento chama Cobranca",
        "o Faturamento configura a Cobranca no arranque",
    )
    assert verdict is Confidence.INFERRED


def test_a_writing_sentence_supports_a_writes_relation() -> None:
    verdict = _verdict(
        RelationKind.WRITES,
        "Cobranca",
        "Faturamento grava na Cobranca",
        "o Faturamento grava na Cobranca a cada lote",
    )
    assert verdict is Confidence.SUPPORTED


def test_a_documental_declares_relation_needs_its_predicate_in_the_text() -> None:
    present = _verdict(
        RelationKind.DECLARES,
        "Cobranca",
        "Faturamento define Cobranca",
        "o Faturamento define a Cobranca como obrigatoria",
    )
    absent = _verdict(
        RelationKind.DECLARES,
        "Cobranca",
        "Faturamento define Cobranca",
        "o Faturamento aparece ao lado da Cobranca no anexo",
    )
    assert present is Confidence.SUPPORTED
    assert absent is Confidence.INFERRED


def test_naming_both_ends_without_any_predicate_stays_inferred() -> None:
    verdict = _verdict(
        RelationKind.PUBLISHES,
        "Cobranca",
        "Faturamento publica para Cobranca",
        "Faturamento e Cobranca estao na mesma tabela",
    )
    assert verdict is Confidence.INFERRED


def test_the_grounding_reason_names_the_action_that_is_missing() -> None:
    claim = _claim(
        RelationKind.CALLS, "Cobranca", "Faturamento chama Cobranca"
    )
    verdict = assess_relation(
        claim,
        _source("Faturamento", claim),
        _resolution(),
        "Cobranca",
        (_capture("o Faturamento configura a Cobranca no arranque"),),
        False,
    )
    assert verdict.confidence is Confidence.INFERRED
    assert any("acting on" in reason for reason in verdict.reasons)
    assert verdict.captures


def test_a_relation_statement_reaches_the_provider_schema() -> None:
    schema = document_finding_schema(SourceKind.TRANSCRIPT)
    relation = schema["properties"]["relations"]["items"]
    assert "statement" in relation["properties"]
    assert relation["properties"]["statement"] == {"type": "string"}
    assert "statement" in relation["required"]


def test_a_relation_statement_survives_the_round_trip() -> None:
    claim = _claim(RelationKind.CALLS, "Cobranca", "  Faturamento chama Cobranca  ")
    assert claim.statement == "Faturamento chama Cobranca"
    assert claim.to_dict()["statement"] == "Faturamento chama Cobranca"
