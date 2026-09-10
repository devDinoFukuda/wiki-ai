from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from wiki_ai.knowledge import (
    CodeContent,
    CodeLocator,
    Confidence,
    DiagramLocator,
    DocumentLocator,
    Entity,
    EntityId,
    EpistemicStatus,
    KnowledgeRepository,
    Relation,
    SourceVersion,
    SpreadsheetLocator,
    TranscriptLocator,
    make_evidence,
)
from wiki_ai.knowledge.repository import DATABASE_FILENAME

from .graph_fixture import CAPTURED, entity

CODE_SOURCE = "src_code"
TRANSCRIPT_SOURCE = "src_transcript"
SHEET_SOURCE = "src_xlsx"
DIAGRAM_SOURCE = "src_drawio"
DOCUMENT_SOURCE = "src_docx"

CODE_HASH = "hash_code"
TRANSCRIPT_HASH = "hash_transcript"
SHEET_HASH = "hash_sheet"
DIAGRAM_HASH = "hash_diagram"
DOCUMENT_HASH = "hash_document"

SOURCE_IDS: tuple[str, ...] = (
    CODE_SOURCE,
    DIAGRAM_SOURCE,
    DOCUMENT_SOURCE,
    SHEET_SOURCE,
    TRANSCRIPT_SOURCE,
)


@dataclass(frozen=True)
class Inception:
    repository: KnowledgeRepository
    nodes: Mapping[str, Entity]
    versions: Mapping[str, SourceVersion]
    evidence: Mapping[str, str]

    def id(self, name: str) -> EntityId:
        return self.nodes[name].id


def _version(source_id: str, version_hash: str, root: str) -> SourceVersion:
    return SourceVersion(
        source_id=source_id,
        version_hash=version_hash,
        locator_root=root,
        captured_at=CAPTURED,
    )


def versions() -> dict[str, SourceVersion]:
    return {
        "code": _version(CODE_SOURCE, CODE_HASH, "/repo"),
        "transcript": _version(TRANSCRIPT_SOURCE, TRANSCRIPT_HASH, "/inception.vtt"),
        "sheet": _version(SHEET_SOURCE, SHEET_HASH, "/regras.xlsx"),
        "diagram": _version(DIAGRAM_SOURCE, DIAGRAM_HASH, "/arquitetura.drawio"),
        "document": _version(DOCUMENT_SOURCE, DOCUMENT_HASH, "/escopo.docx"),
    }


def _document(key: str, kind: str, name: str, attributes: Mapping[str, Any], status):
    return entity(
        key,
        kind,
        name,
        dict(attributes),
        epistemic=status,
        confidence=Confidence.INFERRED,
    )


def catalog() -> dict[str, Entity]:
    return {
        "system": entity("sys/renewal", "system", "Plataforma de Renovacao"),
        "capability": entity(
            "capability::renovacao",
            "capability",
            "Renovacao",
            {"aliases": ["Renovação", "Renewal"]},
            confidence=Confidence.SUPPORTED,
        ),
        "rule_code": entity(
            "business_rule::renovacao",
            "business_rule",
            "Renovacao",
            {
                "statement": "renovacao permitida ate 30 dias apos o vencimento",
                "conditions": ["atraso menor que 30 dias"],
                "effects": ["renovacao aprovada"],
                "aliases": ["Renovação"],
            },
            confidence=Confidence.SUPPORTED,
        ),
        "rule_eligibility": entity(
            "business_rule::eligibility",
            "business_rule",
            "Eligibility",
            {
                "statement": "cliente adimplente pode renovar",
                "conditions": ["sem debito"],
                "effects": ["renovacao aprovada"],
            },
            confidence=Confidence.SUPPORTED,
        ),
        "integration_billing": entity(
            "integration::billing",
            "integration",
            "Billing",
            {"direction": "outbound", "protocol": "http"},
            confidence=Confidence.SUPPORTED,
        ),
        "topic_renewal": entity(
            "topic::renewalevent",
            "topic",
            "RenewalEvent",
            confidence=Confidence.SUPPORTED,
        ),
        "capability_cancel": entity(
            "capability::cancelamento",
            "capability",
            "Cancelamento",
            epistemic=EpistemicStatus.DECLARED,
            confidence=Confidence.INFERRED,
        ),
        "proposal": _document(
            "proposal::migrar renovacao para salesforce",
            "proposal",
            "Migrar Renovacao para Salesforce",
            {
                "statement": "migrar renovacao para Salesforce",
                "aliases": ["Renovacao"],
                "date": "2026-02-01",
                "origin": "transcript",
            },
            EpistemicStatus.PROPOSED,
        ),
        "decision": _document(
            "decision_record::migrar renovacao para salesforce",
            "decision_record",
            "Migrar Renovacao para Salesforce",
            {
                "decision": "migrar renovacao para Salesforce",
                "aliases": ["Renovacao"],
                "decided_at": "2026-03-15",
                "origin": "transcript",
            },
            EpistemicStatus.PROPOSED,
        ),
        "rule_sheet": _document(
            "xlsx/business_rule::renovacao",
            "business_rule",
            "Renovação",
            {
                "statement": "renovacao permitida ate 45 dias apos o vencimento",
                "conditions": ["atraso menor que 45 dias"],
                "effects": ["renovacao aprovada"],
                "aliases": ["Renovacao"],
            },
            EpistemicStatus.DECLARED,
        ),
        "integration_billing_declared": _document(
            "drawio/integration::billing",
            "integration",
            "Billing",
            {"direction": "outbound", "protocol": "http"},
            EpistemicStatus.DECLARED,
        ),
        "integration_salesforce": _document(
            "drawio/integration::salesforce",
            "integration",
            "Salesforce",
            {"direction": "outbound", "protocol": "http"},
            EpistemicStatus.DECLARED,
        ),
        "integration_gateway_pay": entity(
            "integration::gateway pagamento",
            "integration",
            "Gateway Pagamento",
            {"direction": "outbound", "protocol": "http"},
            confidence=Confidence.SUPPORTED,
        ),
        "integration_gateway_cobranca": entity(
            "integration::gateway cobranca",
            "integration",
            "Gateway Cobranca",
            {"direction": "outbound", "protocol": "http"},
            confidence=Confidence.SUPPORTED,
        ),
        "requirement_gateway": _document(
            "requirement::gateway",
            "requirement",
            "Gateway",
            {
                "statement": "o gateway deve ser resiliente",
                "aliases": ["Gateway Pagamento", "Gateway Cobranca"],
            },
            EpistemicStatus.DECLARED,
        ),
        "requirement_cancel": _document(
            "requirement::cancelamento",
            "requirement",
            "Cancelamento",
            {"statement": "o sistema deve permitir cancelamento pelo cliente"},
            EpistemicStatus.DECLARED,
        ),
    }


EDGES: tuple[tuple[str, str, str], ...] = (
    ("belongs_to", "capability", "system"),
    ("belongs_to", "rule_code", "capability"),
    ("belongs_to", "rule_eligibility", "capability"),
    ("belongs_to", "integration_billing", "capability"),
    ("calls", "capability", "integration_billing"),
    ("publishes", "integration_billing", "topic_renewal"),
    ("validates", "rule_eligibility", "capability"),
)


def build(tmp_path) -> Inception:
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    nodes = catalog()
    known = versions()
    evidence_ids: dict[str, str] = {}
    with repo.begin_revision("pipeline", "grafo de inception") as revision:
        for version in known.values():
            revision.put_source_version(version)
        for name, node in nodes.items():
            revision.put_entity(_with_version(node, known, name))
        for kind, source, target in EDGES:
            revision.put_relation(
                Relation.create(kind, nodes[source].id, nodes[target].id)
            )
        for name, holder, evidence in _evidence(known, nodes):
            revision.put_evidence(evidence, entity_ids=(holder,))
            evidence_ids[name] = evidence.id
    return Inception(
        repository=repo, nodes=nodes, versions=known, evidence=evidence_ids
    )


_VERSION_OF: Mapping[str, str] = {
    "system": "code",
    "capability": "code",
    "rule_code": "code",
    "rule_eligibility": "code",
    "integration_billing": "code",
    "topic_renewal": "code",
    "capability_cancel": "document",
    "proposal": "transcript",
    "decision": "transcript",
    "rule_sheet": "sheet",
    "integration_billing_declared": "diagram",
    "integration_salesforce": "diagram",
    "requirement_cancel": "document",
    "requirement_gateway": "document",
    "integration_gateway_pay": "code",
    "integration_gateway_cobranca": "code",
}


def _with_version(
    node: Entity, known: Mapping[str, SourceVersion], name: str
) -> Entity:
    version = known[_VERSION_OF[name]]
    return Entity(
        id=node.id,
        kind=node.kind,
        name=node.name,
        attributes=node.attributes,
        epistemic=node.epistemic,
        confidence=node.confidence,
        source_versions=(version.key,),
    )


def _code(version: SourceVersion, path: str, symbol: str):
    return make_evidence(
        version.source_id,
        version.version_hash,
        CodeLocator(
            path=path,
            line_start=10,
            line_end=48,
            symbol=symbol,
            content=CodeContent.EXECUTABLE,
        ),
        f"codigo:{path}:{symbol}",
        CAPTURED,
    )


def _evidence(known: Mapping[str, SourceVersion], nodes: Mapping[str, Entity]):
    code = known["code"]
    transcript = known["transcript"]
    sheet = known["sheet"]
    diagram = known["diagram"]
    document = known["document"]
    return (
        (
            "code_capability",
            nodes["capability"].id,
            _code(code, "src/renewal/service.py", "renew"),
        ),
        (
            "code_rule",
            nodes["rule_code"].id,
            _code(code, "src/renewal/rules.py", "within_window"),
        ),
        (
            "code_eligibility",
            nodes["rule_eligibility"].id,
            _code(code, "src/renewal/rules.py", "eligible"),
        ),
        (
            "code_integration",
            nodes["integration_billing"].id,
            _code(code, "src/billing/client.py", "charge"),
        ),
        (
            "code_topic",
            nodes["topic_renewal"].id,
            _code(code, "src/renewal/events.py", "publish"),
        ),
        (
            "transcript_proposal",
            nodes["proposal"].id,
            make_evidence(
                transcript.source_id,
                transcript.version_hash,
                TranscriptLocator(speaker="Ana", time_start=120.0, time_end=148.5),
                "vamos migrar renovacao para Salesforce",
                CAPTURED,
            ),
        ),
        (
            "transcript_decision",
            nodes["decision"].id,
            make_evidence(
                transcript.source_id,
                transcript.version_hash,
                TranscriptLocator(speaker="Bruno", time_start=900.0, time_end=931.0),
                "ficou decidido migrar renovacao para Salesforce",
                CAPTURED,
            ),
        ),
        (
            "sheet_rule",
            nodes["rule_sheet"].id,
            make_evidence(
                sheet.source_id,
                sheet.version_hash,
                SpreadsheetLocator(
                    workbook="regras.xlsx", worksheet="Renovacao", cell_range="B4:D9"
                ),
                "renovacao permitida ate 45 dias",
                CAPTURED,
            ),
        ),
        (
            "diagram_billing",
            nodes["integration_billing_declared"].id,
            make_evidence(
                diagram.source_id,
                diagram.version_hash,
                DiagramLocator(
                    diagram="arquitetura.drawio", page="Integracoes", node="node-billing"
                ),
                "Billing",
                CAPTURED,
            ),
        ),
        (
            "diagram_salesforce",
            nodes["integration_salesforce"].id,
            make_evidence(
                diagram.source_id,
                diagram.version_hash,
                DiagramLocator(
                    diagram="arquitetura.drawio",
                    page="Integracoes",
                    node="node-salesforce",
                ),
                "Salesforce",
                CAPTURED,
            ),
        ),
        (
            "code_gateway_pay",
            nodes["integration_gateway_pay"].id,
            _code(code, "src/payments/gateway.py", "pay"),
        ),
        (
            "code_gateway_cobranca",
            nodes["integration_gateway_cobranca"].id,
            _code(code, "src/billing/gateway.py", "collect"),
        ),
        (
            "document_requirement",
            nodes["requirement_cancel"].id,
            make_evidence(
                document.source_id,
                document.version_hash,
                DocumentLocator(
                    block_id="b-cancelamento", heading_path=("Escopo", "Cancelamento")
                ),
                "o sistema deve permitir cancelamento pelo cliente",
                CAPTURED,
            ),
        ),
        (
            "document_capability",
            nodes["capability_cancel"].id,
            make_evidence(
                document.source_id,
                document.version_hash,
                DocumentLocator(
                    block_id="b-cancelamento-cap",
                    heading_path=("Escopo", "Cancelamento"),
                ),
                "capacidade de cancelamento",
                CAPTURED,
            ),
        ),
    )
