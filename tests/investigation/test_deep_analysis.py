from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.investigation.fake_provider import FakeProvider, Script
from tests.repository.fixtures_repos import java_repo, mainframe_repo

from wiki_ai.knowledge.evidence import CodeContent, CodeLocator
from wiki_ai.knowledge.gaps import GAP_KIND, is_blocking, is_open
from wiki_ai.knowledge.model import Confidence, EpistemicStatus
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind
from wiki_ai.investigation.orchestrator import Investigator
from wiki_ai.investigation.profile_gaps import SECTION_ATTRIBUTE
from wiki_ai.investigation.strategy import Phase

CONTROLLER = "src/main/java/com/acme/order/OrderController.java"
SERVICE = "src/main/java/com/acme/order/OrderService.java"
REPOSITORY = "src/main/java/com/acme/order/OrderRepository.java"
PRODUCER = "src/main/java/com/acme/order/OrderProducer.java"
JAVA_TEST = "src/test/java/com/acme/order/OrderServiceTest.java"
CONFIG = "src/main/resources/application.yml"

COBOL = "cobol/PAYRUN.cbl"
COPYBOOK = "copybook/PAYREC.cpy"
JCL = "jcl/PAYJOB.jcl"

OBJECTIVE = "analyze repository"
NAMESPACE = "acme"
CAPABILITY = "place order"
PAYROLL = "payroll run"


def knowledge_at(tmp_path: Path) -> KnowledgeRepository:
    return KnowledgeRepository.open(str(tmp_path / "knowledge" / "state.db"))


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
                "statement": "OrderController place delegates the reference to OrderService place",
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
            ("repo.tests", {}),
            ("repo.config", {}),
            capture(SERVICE, 10, 12, "place"),
            capture(REPOSITORY, 3, 4, "save"),
            capture(PRODUCER, 10, 12, "emit"),
            capture(JAVA_TEST, 4, 7, "placesOrder"),
            capture(CONFIG, 4, 6),
            capture(CONTROLLER, 15, 17, "place"),
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
                "statement": "OrderService place hands the reference to OrderRepository save",
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
                "statement": "OrderProducer emit sends the payload through KafkaProducer send",
                "attributes": {"direction": "outbound", "protocol": "kafka"},
                "evidence": [ref(PRODUCER, 10, 12)],
                "confidence": "supported",
                "relations": [rel("belongs_to", CAPABILITY, "capability")],
            },
            {
                "type": "event",
                "subject": "orders-v1 message",
                "statement": "the payload reaches the orders-v1 topic configured for the producer",
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
                "statement": "OrderService place is the service operation behind the entry point",
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


def java_provider() -> FakeProvider:
    return FakeProvider(
        scripts=[java_discovery(), java_capability(), java_link(), Script()]
    )


def run_java(tmp_path: Path, knowledge: KnowledgeRepository) -> tuple[Any, FakeProvider]:
    snapshot = java_repo(tmp_path / "repo")
    provider = java_provider()
    outcome = Investigator(total_tool_calls=120).run(
        OBJECTIVE, snapshot, knowledge, provider, NAMESPACE
    )
    return outcome, provider


def capability_of(knowledge: KnowledgeRepository, name: str):
    return [
        entity
        for entity in knowledge.find_entities(EntityKind.CAPABILITY.value)
        if entity.name == name
    ][0]


def test_java_the_strategy_runs_discovery_then_one_step_per_capability(
    tmp_path: Path,
) -> None:
    with knowledge_at(tmp_path) as knowledge:
        outcome, _ = run_java(tmp_path, knowledge)
    phases = [step["phase"] for step in outcome.details["steps"]]
    assert phases[0] == Phase.DISCOVERY.value
    assert Phase.CAPABILITY.value in phases
    assert phases[-1] == Phase.CONSOLIDATION.value
    subjects = [
        step["subject"]
        for step in outcome.details["steps"]
        if step["phase"] == Phase.CAPABILITY.value
    ]
    assert subjects == [CAPABILITY]


def test_java_the_capability_briefing_asks_the_empty_sections_as_questions(
    tmp_path: Path,
) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        capability_briefing = [
            text
            for text in _provider_briefings(tmp_path, knowledge)
            if "Kind: capability_analysis" in text
        ]
    assert capability_briefing
    text = capability_briefing[0]
    assert "not a form to fill" in text
    assert "persistence: what does it read or write and where" in text
    assert "events: which events does it publish or consume" in text
    assert "declare a gap" in text


def _provider_briefings(tmp_path: Path, knowledge: KnowledgeRepository) -> list[str]:
    snapshot = java_repo(tmp_path / "repo2")
    provider = java_provider()
    Investigator(total_tool_calls=120).run(
        OBJECTIVE, snapshot, knowledge, provider, NAMESPACE
    )
    return list(provider.seen_objectives)


def test_java_identifies_the_entrypoint_with_executable_evidence(
    tmp_path: Path,
) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        profile = KnowledgeQuery(knowledge).capability_profile(
            capability_of(knowledge, CAPABILITY).id
        )
        assert profile is not None
        assert [item.name for item in profile.entrypoints] == ["OrderController place"]
        entry = profile.entrypoints[0]
        assert entry.attributes["mechanism"] == "http"
        assert entry.confidence is Confidence.SUPPORTED
        evidence = knowledge.evidence_for(entry.id)
        assert isinstance(evidence[0].locator, CodeLocator)
        assert evidence[0].locator.content is CodeContent.EXECUTABLE


def test_java_fills_the_section_profile_of_ten_two(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        profile = KnowledgeQuery(knowledge).capability_profile(
            capability_of(knowledge, CAPABILITY).id
        )
        assert profile is not None
        sections = profile.sections()
    for name in (
        "entrypoints",
        "inputs",
        "outputs",
        "preconditions",
        "rules",
        "invariants",
        "persistence",
        "integrations",
        "events",
        "failures",
        "edge_cases",
        "tests",
    ):
        assert sections[name], name


def test_java_reconstructs_the_rule_with_conditions_and_effects(
    tmp_path: Path,
) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        profile = KnowledgeQuery(knowledge).capability_profile(
            capability_of(knowledge, CAPABILITY).id
        )
        assert profile is not None
        rule = [
            item for item in profile.rules if item.name == "every placed order is saved"
        ][0]
        assert rule.attributes["conditions"] == ["place is called with a reference"]
        assert rule.attributes["effects"] == [
            "OrderRepository save receives the reference"
        ]
        assert rule.epistemic is EpistemicStatus.IMPLEMENTED
        assert knowledge.evidence_for(rule.id)


def test_java_identifies_persistence_and_kafka_events(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        profile = KnowledgeQuery(knowledge).capability_profile(
            capability_of(knowledge, CAPABILITY).id
        )
        assert profile is not None
        assert "order repository" in {item.name for item in profile.persistence}
        integrations = {item.name for item in profile.integrations}
        assert "orders kafka producer" in integrations
        kafka = [
            item for item in profile.integrations if item.name == "orders kafka producer"
        ][0]
        assert kafka.attributes["protocol"] == "kafka"
        assert "orders-v1 message" in {item.name for item in profile.events}


def test_java_identifies_failures_and_edge_cases(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        profile = KnowledgeQuery(knowledge).capability_profile(
            capability_of(knowledge, CAPABILITY).id
        )
        assert profile is not None
        assert "producer send receives null" in {item.name for item in profile.failures}
        edge = [
            item
            for item in profile.edge_cases
            if item.name == "place with a null reference"
        ][0]
        assert edge.attributes["condition"] == "reference is null"


def test_java_generates_a_flow_with_ordered_steps_from_the_entrypoint(
    tmp_path: Path,
) -> None:
    with knowledge_at(tmp_path) as knowledge:
        outcome, _ = run_java(tmp_path, knowledge)
        flows = knowledge.find_entities(EntityKind.FLOW.value)
        assert flows
        steps = knowledge.find_entities(EntityKind.FLOW_STEP.value)
        ordered = KnowledgeQuery(knowledge).flow(flows[0].id)
        ordinals = [view.entity.attributes["ordinal"] for view in ordered]
        assert ordinals == sorted(ordinals)
        assert len(ordered) == len(steps)
        subjects = [view.entity.attributes["subject"] for view in ordered]
        assert subjects[0] == "OrderController place"
        assert "OrderService place" in subjects
        assert "order repository" in subjects
    assert outcome.details["flows"]
    assert outcome.details["flows"][0]["steps"][0]["ordinal"] == 0


def test_java_every_flow_step_presents_the_evidence_it_inherited(
    tmp_path: Path,
) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        steps = knowledge.find_entities(EntityKind.FLOW_STEP.value)
        supported = [
            step for step in steps if step.confidence is Confidence.SUPPORTED
        ]
        assert supported
        for step in supported:
            evidence = knowledge.evidence_for(step.id)
            assert evidence
            assert isinstance(evidence[0].locator, CodeLocator)
        unresolved = [
            step for step in steps if step.confidence is Confidence.UNRESOLVED
        ]
        for step in unresolved:
            assert not knowledge.evidence_for(step.id)


def test_java_a_decision_is_derived_from_the_rule_conditions(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        decisions = knowledge.find_entities(EntityKind.DECISION.value)
        assert decisions
        assert decisions[0].attributes["criteria"] == [
            "place is called with a reference"
        ]


def test_java_the_open_sections_become_typed_gaps_about_the_capability(
    tmp_path: Path,
) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        gaps = [gap for gap in knowledge.find_entities(GAP_KIND) if is_open(gap)]
        sections = {
            str(gap.attributes[SECTION_ATTRIBUTE])
            for gap in gaps
            if SECTION_ATTRIBUTE in gap.attributes
        }
        assert "retries" in sections
        assert "idempotency" in sections
        assert "rules" not in sections
        assert "persistence" not in sections
        assert "events" not in sections
        capability = capability_of(knowledge, CAPABILITY)
        about = KnowledgeQuery(knowledge).capability_profile(capability.id)
        assert about is not None
        assert about.gaps


def test_java_a_section_without_any_signal_is_never_a_gap(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        sections = {
            str(gap.attributes[SECTION_ATTRIBUTE])
            for gap in knowledge.find_entities(GAP_KIND)
            if SECTION_ATTRIBUTE in gap.attributes
        }
        assert "states" not in sections


def test_java_the_gaps_that_block_the_profile_are_flagged_blocking(
    tmp_path: Path,
) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        blocking = {
            str(gap.attributes[SECTION_ATTRIBUTE])
            for gap in knowledge.find_entities(GAP_KIND)
            if is_blocking(gap) and SECTION_ATTRIBUTE in gap.attributes
        }
        assert "decisions" in blocking
        assert "retries" not in blocking


def test_java_the_outcome_reports_the_open_sections_as_unresolved(
    tmp_path: Path,
) -> None:
    with knowledge_at(tmp_path) as knowledge:
        outcome, _ = run_java(tmp_path, knowledge)
    assert any("retries" in item for item in outcome.unresolved)
    assert outcome.details["coverage"]["unresolved_sections"]


def cobol_discovery() -> Script:
    return Script(
        steps=(
            ("repo.inventory", {}),
            ("repo.search", {"pattern": "PROGRAM-ID"}),
            ("repo.search", {"pattern": "EXEC PGM"}),
            ("repo.read", {"path": COBOL}),
            capture(COBOL, 2, 2, "PAYRUN"),
            capture(JCL, 2, 2, "STEP01"),
        ),
        findings=[
            {
                "type": "capability",
                "subject": PAYROLL,
                "statement": "PAYRUN computes the payroll gross and calls TAXCALC",
                "evidence": [ref(COBOL, 2, 2)],
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
                "evidence": [ref(JCL, 2, 2)],
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
            ("repo.read", {"path": COPYBOOK}),
            ("repo.read", {"path": JCL}),
            capture(COBOL, 8, 8, "MAIN-LOGIC"),
            capture(COBOL, 9, 9, "MAIN-LOGIC"),
            capture(COBOL, 5, 5, "PAYRUN"),
            capture(COBOL, 12, 12, "CALC-TOTAL"),
            capture(COPYBOOK, 2, 5),
            capture(JCL, 5, 5, "STEP02"),
            capture(JCL, 4, 4, "INFILE"),
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
        ],
    )


def cobol_provider() -> FakeProvider:
    return FakeProvider(scripts=[cobol_discovery(), cobol_capability(), Script()])


def run_cobol(tmp_path: Path, knowledge: KnowledgeRepository):
    snapshot = mainframe_repo(tmp_path / "repo")
    provider = cobol_provider()
    outcome = Investigator(total_tool_calls=120).run(
        OBJECTIVE, snapshot, knowledge, provider, NAMESPACE
    )
    return outcome, provider


def test_cobol_never_blocks_for_a_missing_parser(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        outcome, provider = run_cobol(tmp_path, knowledge)
    assert "aborted" not in outcome.details
    assert outcome.entities_written > 0
    joined = " ".join(provider.seen_objectives).lower()
    assert "not supported" not in joined
    assert "unsupported language" not in joined


def test_cobol_finds_the_programs_by_investigation(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_cobol(tmp_path, knowledge)
        names = {
            entity.name
            for entity in knowledge.find_entities(EntityKind.PROCEDURE.value)
        }
        assert "PAYRUN" in names


def test_cobol_follows_call_and_perform_through_search_and_read(
    tmp_path: Path,
) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_cobol(tmp_path, knowledge)
        operations = {
            entity.name
            for entity in knowledge.find_entities(EntityKind.OPERATION.value)
        }
        assert "TAXCALC" in operations
        procedures = {
            entity.name
            for entity in knowledge.find_entities(EntityKind.PROCEDURE.value)
        }
        assert "CALC-TOTAL" in procedures
        _, provider = run_cobol(tmp_path, knowledge)
    tools = {name for script in provider.scripts for name, _ in script.steps}
    assert "repo.search" in tools
    assert "repo.read" in tools


def test_cobol_associates_the_copybook_with_the_program(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_cobol(tmp_path, knowledge)
        contracts = knowledge.find_entities(EntityKind.DATA_CONTRACT.value)
        assert [item.name for item in contracts] == ["PAYREC copybook"]
        paths = {
            item.locator.to_dict()["path"]
            for item in knowledge.evidence_for(contracts[0].id)
        }
        assert COPYBOOK in paths
        assert COBOL in paths


def test_cobol_identifies_the_jcl_steps_as_entrypoints(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_cobol(tmp_path, knowledge)
        entries = {
            entity.name
            for entity in knowledge.find_entities(EntityKind.ENTRY_POINT.value)
        }
        assert {"PAYJOB STEP01", "PAYJOB STEP02"} <= entries
        step = [
            entity
            for entity in knowledge.find_entities(EntityKind.ENTRY_POINT.value)
            if entity.name == "PAYJOB STEP01"
        ][0]
        assert step.attributes["mechanism"] == "jcl"


def test_cobol_every_finding_carries_its_evidence(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_cobol(tmp_path, knowledge)
        supported = [
            entity
            for entity in knowledge.find_entities()
            if entity.confidence is Confidence.SUPPORTED
        ]
        assert supported
        for entity in supported:
            evidence = knowledge.evidence_for(entity.id)
            assert evidence, entity.name
            assert evidence[0].version_hash


def test_cobol_builds_a_flow_from_the_jcl_entrypoint(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        outcome, _ = run_cobol(tmp_path, knowledge)
        flows = knowledge.find_entities(EntityKind.FLOW.value)
        assert flows
    names = {flow["entrypoint"] for flow in outcome.details["flows"]}
    assert "PAYJOB STEP01" in names


def test_a_budget_spent_mid_analysis_leaves_explicit_gaps_and_an_honest_outcome(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = java_provider()
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator(total_tool_calls=6).run(
            OBJECTIVE, snapshot, knowledge, provider, NAMESPACE
        )
        capabilities = knowledge.find_entities(EntityKind.CAPABILITY.value)
        assert capabilities
        profile = KnowledgeQuery(knowledge).capability_profile(capabilities[0].id)
    assert outcome.details["tool_calls"] <= 6
    assert outcome.unresolved
    assert profile is not None
    assert not profile.rules
    assert outcome.details["coverage"]["score"] < 1.0


def test_the_relations_needed_by_the_diagrams_reach_the_graph(tmp_path: Path) -> None:
    with knowledge_at(tmp_path) as knowledge:
        run_java(tmp_path, knowledge)
        kinds = {relation.kind for relation in knowledge.find_relations()}
        assert RelationKind.BELONGS_TO.value in kinds
        assert RelationKind.TRIGGERS.value in kinds
        assert RelationKind.PERSISTS_TO.value in kinds
        assert RelationKind.PUBLISHES.value in kinds
        assert knowledge.find_entities(EntityKind.FLOW.value)
        assert knowledge.find_entities(EntityKind.FLOW_STEP.value)
