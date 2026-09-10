from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from tests.investigation.fake_provider import FakeProvider, Script
from tests.repository.fixtures_repos import java_repo, mainframe_repo, write
from wiki_ai.agent.protocol import AgentCapabilities
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.agent.session import AgentRun, AgentSession
from wiki_ai.knowledge.model import Entity
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind

PROVIDER_NAME = "scripted"
NAMESPACE_OBJECTIVE = "analyze this system deeply"

BASE = "src/main/java/com/acme/order"
CONTROLLER = f"{BASE}/OrderController.java"
SERVICE = f"{BASE}/OrderService.java"
REPOSITORY = f"{BASE}/OrderRepository.java"
PRODUCER = f"{BASE}/OrderProducer.java"
JAVA_TEST = "src/test/java/com/acme/order/OrderServiceTest.java"
CONFIG = "src/main/resources/application.yml"
LEGACY_EIGHT = f"{BASE}/OrderLegacySupport.java"
MODERN_TWENTY_ONE = f"{BASE}/OrderSummary.java"

COBOL = "cobol/PAYRUN.cbl"
COPYBOOK = "copybook/PAYREC.cpy"
JCL = "jcl/PAYJOB.jcl"
EMBEDDED_SQL = "cobol/BILLRUN.cbl"

CAPABILITY = "place order"
PAYROLL = "payroll run"

JAVA_EIGHT_SOURCE = """package com.acme.order;

import java.util.Arrays;
import java.util.List;
import java.util.stream.Collectors;

public class OrderLegacySupport {
    public List<String> normalize(String raw) {
        return Arrays.stream(raw.split(","))
            .map(item -> item.trim())
            .filter(item -> !item.isEmpty())
            .collect(Collectors.toList());
    }
}
"""

JAVA_TWENTY_ONE_SOURCE = """package com.acme.order;

public record OrderSummary(String reference, int quantity) {
    public String describe(Object status) {
        return switch (status) {
            case Integer code when code > 0 -> "accepted %d".formatted(code);
            case String text -> text;
            default -> \"\"\"
                unknown status
                for %s\"\"\".formatted(reference);
        };
    }
}
"""

EMBEDDED_SQL_SOURCE = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. BILLRUN.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       COPY PAYREC.
       PROCEDURE DIVISION.
       BILL-LOGIC SECTION.
           EXEC SQL
             SELECT GROSS INTO :WS-GROSS
             FROM PAYROLL_MASTER
             WHERE EMPLOYEE_ID = :WS-EMP
           END-EXEC.
           CALL 'PAYRUN' USING WS-GROSS
           STOP RUN.
"""


class ScriptedProvider(FakeProvider):
    def connect(self) -> None:
        return None

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(tools=("repo.read", "repo.search", "evidence.capture"))

    def cancel(self) -> None:
        return None

    def run(self, session: AgentSession) -> AgentRun:
        return super().run(session)


SETTLED_STATUSES = ("ok", "partial")


def assert_settled(report: Any) -> Any:
    payload = report.to_dict()
    assert payload["status"] in SETTLED_STATUSES, payload
    assert payload["analysis_status"] in ("complete", "partial"), payload
    return report


def registry_of(provider: FakeProvider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(PROVIDER_NAME, lambda: provider)
    return registry


def scripted_registry(scripts: Sequence[Script]) -> ProviderRegistry:
    return registry_of(ScriptedProvider(scripts=list(scripts)))


def capture(path: str, start: int, end: int, symbol: str | None = None):
    arguments: dict[str, Any] = {"path": path, "line_start": start, "line_end": end}
    if symbol is not None:
        arguments["symbol"] = symbol
    return ("evidence.capture", arguments)


def ref(path: str, start: int, end: int) -> dict[str, Any]:
    return {"path": path, "line_start": start, "line_end": end}


def rel(kind: str, target: str, target_type: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": kind, "target_subject": target}
    if target_type is not None:
        payload["target_type"] = target_type
    return payload


def java_acceptance_repo(root: Path) -> Path:
    java_repo(root)
    write(root, LEGACY_EIGHT, JAVA_EIGHT_SOURCE)
    write(root, MODERN_TWENTY_ONE, JAVA_TWENTY_ONE_SOURCE)
    return root


def mainframe_acceptance_repo(root: Path) -> Path:
    mainframe_repo(root)
    write(root, EMBEDDED_SQL, EMBEDDED_SQL_SOURCE)
    return root


def java_discovery() -> Script:
    return Script(
        steps=(
            ("repo.inventory", {}),
            ("repo.dependencies", {}),
            ("repo.search", {"pattern": "place"}),
            capture(CONTROLLER, 15, 17, "place"),
            capture(SERVICE, 10, 12, "place"),
        ),
        findings=[
            {
                "type": "system",
                "subject": "acme orders",
                "statement": "the repository implements the order placement system",
                "evidence": [ref(CONTROLLER, 15, 17)],
                "confidence": "supported",
            },
            {
                "type": "module",
                "subject": "order module",
                "statement": "com.acme.order groups the order classes",
                "evidence": [ref(SERVICE, 10, 12)],
                "confidence": "supported",
                "relations": [rel("belongs_to", "acme orders", "system")],
            },
            {
                "type": "capability",
                "subject": CAPABILITY,
                "statement": (
                    "OrderController place delegates the reference to OrderService place"
                ),
                "evidence": [ref(CONTROLLER, 15, 17)],
                "confidence": "supported",
                "relations": [rel("belongs_to", "order module", "module")],
            },
            {
                "type": "entry_point",
                "subject": "OrderController place",
                "statement": "OrderController place receives the order reference",
                "attributes": {
                    "mechanism": "http",
                    "location": "OrderController.place",
                },
                "evidence": [ref(CONTROLLER, 15, 17)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
        ],
    )


def java_capability() -> Script:
    return Script(
        steps=(
            ("repo.read", {"path": SERVICE}),
            ("repo.read", {"path": REPOSITORY}),
            ("repo.read", {"path": LEGACY_EIGHT}),
            ("repo.read", {"path": MODERN_TWENTY_ONE}),
            ("repo.tests", {}),
            ("repo.config", {}),
            capture(SERVICE, 10, 12, "place"),
            capture(REPOSITORY, 3, 4, "save"),
            capture(PRODUCER, 10, 12, "emit"),
            capture(JAVA_TEST, 4, 7, "placesOrder"),
            capture(CONFIG, 4, 6),
            capture(CONTROLLER, 15, 17, "place"),
            capture(LEGACY_EIGHT, 8, 13, "normalize"),
            capture(MODERN_TWENTY_ONE, 5, 12, "describe"),
        ),
        findings=[
            {
                "type": "input",
                "subject": "order reference",
                "statement": "place receives a String reference",
                "evidence": [ref(CONTROLLER, 15, 17)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "output",
                "subject": "saved order reference",
                "statement": "place returns the String produced by save",
                "evidence": [ref(SERVICE, 10, 12)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "precondition",
                "subject": "a reference reaches place",
                "statement": "place is only reached with a reference argument",
                "evidence": [ref(CONTROLLER, 15, 17)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "business_rule",
                "subject": "every placed order is saved",
                "statement": (
                    "OrderService place hands the reference to OrderRepository save"
                ),
                "conditions": ["place is called with a reference"],
                "effects": ["OrderRepository save receives the reference"],
                "evidence": [ref(SERVICE, 10, 12)],
                "confidence": "supported",
                "relations": [
                    rel("belongs_to", CAPABILITY, "capability"),
                    rel("validates", "order reference", "input"),
                ],
            },
            {
                "type": "business_rule",
                "subject": "raw references are trimmed and emptied entries dropped",
                "statement": (
                    "OrderLegacySupport normalize splits raw, trims each item and "
                    "filters the empty ones"
                ),
                "conditions": ["normalize receives a raw String"],
                "effects": ["only trimmed non empty items remain"],
                "evidence": [ref(LEGACY_EIGHT, 8, 13)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "business_rule",
                "subject": "an order summary describes its status",
                "statement": (
                    "OrderSummary describe returns accepted for an Integer code above "
                    "zero and the text itself for a String status"
                ),
                "conditions": ["describe receives a status"],
                "effects": ["the status is rendered as text"],
                "evidence": [ref(MODERN_TWENTY_ONE, 5, 12)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "invariant",
                "subject": "place always returns what save returned",
                "statement": "the return of place is the return of save",
                "evidence": [ref(SERVICE, 10, 12)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "persistence",
                "subject": "order repository",
                "statement": "OrderRepository save persists the order reference",
                "evidence": [ref(REPOSITORY, 3, 4)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "integration",
                "subject": "orders kafka producer",
                "statement": (
                    "OrderProducer emit sends the payload through KafkaProducer send"
                ),
                "attributes": {"direction": "outbound", "protocol": "kafka"},
                "evidence": [ref(PRODUCER, 10, 12)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "event",
                "subject": "orders-v1 message",
                "statement": (
                    "the payload reaches the orders-v1 topic configured for the producer"
                ),
                "evidence": [ref(CONFIG, 4, 6)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "failure_mode",
                "subject": "producer send receives null",
                "statement": "OrderProducer emit calls send with a null record",
                "attributes": {
                    "trigger": "emit is called",
                    "effect": "send receives null instead of a record",
                },
                "evidence": [ref(PRODUCER, 10, 12)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "edge_case",
                "subject": "place with a null reference",
                "statement": "place forwards the reference without checking it",
                "attributes": {
                    "condition": "reference is null",
                    "expected": "save receives null",
                },
                "evidence": [ref(SERVICE, 10, 12)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "edge_case",
                "subject": "describe with an unknown status",
                "statement": "describe returns unknown status for any other value",
                "attributes": {
                    "condition": "status is neither Integer nor String",
                    "expected": "unknown status is returned",
                },
                "evidence": [ref(MODERN_TWENTY_ONE, 5, 12)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "test_scenario",
                "subject": "OrderServiceTest placesOrder",
                "statement": "placesOrder calls place with R-1",
                "attributes": {"scenario": "place is called with R-1"},
                "evidence": [ref(JAVA_TEST, 4, 7)],
                "confidence": "supported",
                "relations": [rel("tests", CAPABILITY, "capability")],
            },
            {
                "type": "operation",
                "subject": "OrderService place",
                "statement": (
                    "OrderService place is the service operation behind the entry point"
                ),
                "attributes": {"verb": "place"},
                "evidence": [ref(SERVICE, 10, 12)],
                "confidence": "supported",
                "relations": [
                    rel("persists_to", "order repository", "persistence"),
                    rel("publishes", "orders-v1 message", "event"),
                ],
            },
        ],
    )


def java_link() -> Script:
    return Script(
        steps=(capture(CONTROLLER, 15, 17, "place"),),
        findings=[
            {
                "type": "entry_point",
                "subject": "OrderController place",
                "statement": "OrderController place calls the OrderService place operation",
                "attributes": {
                    "mechanism": "http",
                    "location": "OrderController.place",
                },
                "evidence": [ref(CONTROLLER, 15, 17)],
                "confidence": "supported",
                "relations": [
                    rel("calls", "OrderService place", "operation"),
                    rel("belongs_to", CAPABILITY, "capability"),
                ],
            }
        ],
    )


def java_scripts() -> list[Script]:
    return [java_discovery(), java_capability(), java_link(), Script()]


def cobol_discovery() -> Script:
    return Script(
        steps=(
            ("repo.inventory", {}),
            ("repo.search", {"pattern": "PROGRAM-ID"}),
            ("repo.search", {"pattern": "EXEC PGM"}),
            ("repo.read", {"path": COBOL}),
            capture(COBOL, 2, 2, "PAYRUN"),
            capture(COBOL, 7, 10, "PAYRUN"),
            capture(JCL, 1, 2, "PAYJOB"),
        ),
        findings=[
            {
                "type": "capability",
                "subject": PAYROLL,
                "statement": "PAYRUN performs CALC-TOTAL and calls TAXCALC then stops the run",
                "evidence": [ref(COBOL, 7, 10)],
                "confidence": "supported",
            },
            {
                "type": "procedure",
                "subject": "PAYRUN",
                "statement": "PROGRAM-ID PAYRUN declares the payroll program",
                "evidence": [ref(COBOL, 2, 2)],
                "confidence": "supported",
                "relations": [rel("belongs_to", PAYROLL, "capability")],
            },
            {
                "type": "entry_point",
                "subject": "PAYJOB STEP01",
                "statement": "STEP01 EXEC PGM=PAYRUN starts PAYRUN from the JCL",
                "attributes": {"mechanism": "jcl", "location": "PAYJOB.STEP01"},
                "evidence": [ref(JCL, 1, 2)],
                "confidence": "supported",
                "relations": [rel("belongs_to", PAYROLL, "capability")],
            },
        ],
    )


def cobol_capability() -> Script:
    return Script(
        steps=(
            ("repo.read", {"path": COBOL}),
            ("repo.search", {"pattern": "CALL"}),
            ("repo.search", {"pattern": "PERFORM"}),
            ("repo.search", {"pattern": "COPY"}),
            ("repo.search", {"pattern": "EXEC SQL"}),
            ("repo.read", {"path": COPYBOOK}),
            ("repo.read", {"path": JCL}),
            ("repo.read", {"path": EMBEDDED_SQL}),
            capture(COBOL, 8, 8, "MAIN-LOGIC"),
            capture(COBOL, 9, 9, "MAIN-LOGIC"),
            capture(COBOL, 5, 5, "PAYRUN"),
            capture(COBOL, 12, 12, "CALC-TOTAL"),
            capture(COPYBOOK, 2, 5),
            capture(JCL, 5, 5, "STEP02"),
            capture(JCL, 4, 4, "INFILE"),
            capture(EMBEDDED_SQL, 8, 12, "BILL-LOGIC"),
            capture(EMBEDDED_SQL, 13, 13, "BILL-LOGIC"),
        ),
        findings=[
            {
                "type": "procedure",
                "subject": "CALC-TOTAL",
                "statement": "MAIN-LOGIC performs CALC-TOTAL before calling TAXCALC",
                "evidence": [ref(COBOL, 8, 8)],
                "confidence": "supported",
                "relations": [rel("belongs_to", PAYROLL, "capability")],
            },
            {
                "type": "business_rule",
                "subject": "gross is hours times rate",
                "statement": "CALC-TOTAL computes WS-GROSS as WS-HOURS times WS-RATE",
                "conditions": ["CALC-TOTAL is performed"],
                "effects": ["WS-GROSS receives WS-HOURS multiplied by WS-RATE"],
                "evidence": [ref(COBOL, 12, 12)],
                "confidence": "supported",
                "relations": [rel("belongs_to", PAYROLL, "capability")],
            },
            {
                "type": "operation",
                "subject": "TAXCALC",
                "statement": "MAIN-LOGIC calls TAXCALC using WS-GROSS and WS-NET",
                "attributes": {"verb": "call"},
                "evidence": [ref(COBOL, 9, 9)],
                "confidence": "supported",
                "relations": [rel("belongs_to", PAYROLL, "capability")],
            },
            {
                "type": "data_contract",
                "subject": "PAYREC copybook",
                "statement": "COPY PAYREC brings PAY-RECORD into WORKING-STORAGE",
                "evidence": [ref(COBOL, 5, 5), ref(COPYBOOK, 2, 5)],
                "confidence": "supported",
                "relations": [rel("belongs_to", PAYROLL, "capability")],
            },
            {
                "type": "input",
                "subject": "PROD.PAYROLL.MASTER",
                "statement": "INFILE DD points PAYRUN at PROD.PAYROLL.MASTER",
                "evidence": [ref(JCL, 4, 4)],
                "confidence": "supported",
                "relations": [rel("belongs_to", PAYROLL, "capability")],
            },
            {
                "type": "entry_point",
                "subject": "PAYJOB STEP02",
                "statement": "STEP02 EXEC PGM=TAXCALC runs TAXCALC as its own job step",
                "attributes": {"mechanism": "jcl", "location": "PAYJOB.STEP02"},
                "evidence": [ref(JCL, 5, 5)],
                "confidence": "supported",
                "relations": [rel("belongs_to", PAYROLL, "capability")],
            },
            {
                "type": "query",
                "subject": "BILLRUN gross select",
                "statement": (
                    "BILL-LOGIC runs an EXEC SQL SELECT GROSS INTO WS-GROSS FROM "
                    "PAYROLL_MASTER"
                ),
                "attributes": {
                    "statement": (
                        "SELECT GROSS INTO :WS-GROSS FROM PAYROLL_MASTER WHERE "
                        "EMPLOYEE_ID = :WS-EMP"
                    )
                },
                "evidence": [ref(EMBEDDED_SQL, 8, 12)],
                "confidence": "supported",
                "relations": [rel("belongs_to", PAYROLL, "capability")],
            },
            {
                "type": "procedure",
                "subject": "BILLRUN",
                "statement": "BILLRUN BILL-LOGIC calls PAYRUN using WS-GROSS",
                "evidence": [ref(EMBEDDED_SQL, 13, 13)],
                "confidence": "supported",
                "relations": [
                    rel("belongs_to", PAYROLL, "capability"),
                    rel("calls", "PAYRUN", "procedure"),
                ],
            },
        ],
    )


def cobol_scripts() -> list[Script]:
    return [cobol_discovery(), cobol_capability(), Script()]


def entity_named(
    knowledge: KnowledgeRepository, kind: EntityKind, name: str
) -> Entity:
    wanted = name.strip().lower()
    for entity in knowledge.find_entities(kind.value):
        if entity.name.strip().lower() == wanted:
            return entity
    raise AssertionError(f"{kind.value} {name!r} not written into the knowledge")


def profile_of(knowledge: KnowledgeRepository, name: str):
    capability = entity_named(knowledge, EntityKind.CAPABILITY, name)
    return KnowledgeQuery(knowledge).capability_profile(capability.id)


def names_of(entities: Sequence[Entity]) -> set[str]:
    return {entity.name for entity in entities}


MIGRATION_DOCX_BODY = (
    '<?xml version="1.0"?><w:document '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>'
    '<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r>'
    "<w:t>Proposta de migracao de place order</w:t></w:r></w:p>"
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r>'
    "<w:t>Escopo</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Propomos migrar place order para uma arquitetura orientada a "
    "eventos, mantendo orders kafka producer como canal de saida.</w:t></w:r></w:p>"
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r>'
    "<w:t>Riscos</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>A regra every placed order is saved precisa continuar valendo "
    "durante a transicao.</w:t></w:r></w:p>"
    "</w:body></w:document>"
)
