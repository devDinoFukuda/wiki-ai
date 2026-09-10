from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from tests.investigation.fake_provider import FakeProvider, Script
from tests.repository.fixtures_repos import java_repo, mainframe_repo, snapshot_of, write

from wiki_ai.knowledge.evidence import CodeContent, CodeLocator
from wiki_ai.knowledge.gaps import GAP_KIND
from wiki_ai.knowledge.model import Confidence, EpistemicStatus
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import ENTITY_KIND_VALUES, EntityKind
from wiki_ai.repository.harness import RepositoryHarness, ScopeFocus
from wiki_ai.investigation.orchestrator import ABORT_SNAPSHOT_CHANGED, Investigator

CONTROLLER = "src/main/java/com/acme/order/OrderController.java"
SERVICE = "src/main/java/com/acme/order/OrderService.java"
REPOSITORY = "src/main/java/com/acme/order/OrderRepository.java"
PRODUCER = "src/main/java/com/acme/order/OrderProducer.java"
CONFIG = "src/main/resources/application.yml"
OBJECTIVE = "analyze repository"


def knowledge_at(tmp_path: Path) -> KnowledgeRepository:
    return KnowledgeRepository.open(str(tmp_path / "knowledge" / "state.db"))


def capture_step(path: str, start: int, end: int, symbol: str | None = None):
    arguments: dict[str, Any] = {"path": path, "line_start": start, "line_end": end}
    if symbol is not None:
        arguments["symbol"] = symbol
    return ("evidence.capture", arguments)


def ref(path: str, start: int, end: int) -> dict[str, Any]:
    return {"path": path, "line_start": start, "line_end": end}


def java_round_one_script() -> Script:
    return Script(
        steps=(
            ("repo.inventory", {}),
            ("repo.search", {"pattern": "place"}),
            ("repo.read", {"path": SERVICE}),
            capture_step(CONTROLLER, 15, 17, "place"),
            capture_step(SERVICE, 10, 12, "place"),
            capture_step(REPOSITORY, 3, 4, "save"),
            capture_step(PRODUCER, 10, 12, "emit"),
            capture_step(CONFIG, 4, 6),
        ),
        findings=[
            {
                "type": "capability",
                "subject": "place order",
                "statement": "OrderController place delegates to OrderService place",
                "evidence": [ref(CONTROLLER, 15, 17)],
                "confidence": "supported",
                "relations": [
                    {
                        "kind": "belongs_to",
                        "target_subject": "order module",
                        "target_type": "module",
                    }
                ],
            },
            {
                "type": "entry_point",
                "subject": "OrderController place",
                "statement": "OrderController place is the entry to service place",
                "attributes": {
                    "mechanism": "http",
                    "location": "OrderController.place",
                },
                "evidence": [ref(CONTROLLER, 15, 17)],
                "confidence": "supported",
            },
            {
                "type": "business_rule",
                "subject": "order reference is persisted",
                "statement": "OrderService place saves the reference in the repository",
                "conditions": ["a reference is supplied"],
                "effects": ["the reference reaches OrderRepository save"],
                "evidence": [ref(SERVICE, 10, 12)],
                "confidence": "supported",
                "relations": [
                    {
                        "kind": "validates",
                        "target_subject": "order reference",
                        "target_type": "input",
                    }
                ],
            },
            {
                "type": "persistence",
                "subject": "OrderRepository save",
                "statement": "OrderRepository save stores the order reference",
                "evidence": [ref(REPOSITORY, 3, 4)],
                "confidence": "supported",
            },
            {
                "type": "integration",
                "subject": "orders kafka producer",
                "statement": "OrderProducer emit sends the payload to the producer",
                "attributes": {"direction": "outbound", "protocol": "kafka"},
                "evidence": [ref(PRODUCER, 10, 12)],
                "confidence": "supported",
            },
            {
                "type": "configuration",
                "subject": "order topic",
                "statement": "the order topic is configured as orders-v1",
                "attributes": {"key": "order.topic"},
                "evidence": [ref(CONFIG, 4, 6)],
                "confidence": "supported",
            },
        ],
    )


def test_java_loop_writes_supported_knowledge_with_executable_evidence(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(scripts=[java_round_one_script()])
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
        assert outcome.entities_written >= 6
        assert outcome.evidence_written >= 5
        kinds = {
            entity.kind
            for entity in knowledge.find_entities()
            if entity.confidence is Confidence.SUPPORTED
        }
        assert {
            EntityKind.CAPABILITY.value,
            EntityKind.ENTRY_POINT.value,
            EntityKind.BUSINESS_RULE.value,
            EntityKind.INTEGRATION.value,
            EntityKind.PERSISTENCE.value,
        } <= kinds
        capability = [
            entity
            for entity in knowledge.find_entities(EntityKind.CAPABILITY.value)
            if entity.name == "place order"
        ][0]
        assert capability.epistemic is EpistemicStatus.IMPLEMENTED
        evidence = knowledge.evidence_for(capability.id)
        assert isinstance(evidence[0].locator, CodeLocator)
        assert evidence[0].locator.content is CodeContent.EXECUTABLE
        assert evidence[0].version_hash == snapshot.digest
        assert evidence[0].source_id == "acme"


def test_the_session_receives_the_briefing_and_the_taxonomy_schema(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(scripts=[java_round_one_script()])
    with knowledge_at(tmp_path) as knowledge:
        Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
    briefing_text = provider.seen_objectives[0]
    assert "Objective: reconstruct implemented behavior" in briefing_text
    assert "Method:" in briefing_text
    assert "business_rule (requires statement, conditions, effects)" in briefing_text
    assert "repo.inventory" in briefing_text
    schema = provider.seen_schemas[0]
    assert {entry["type"] for entry in schema["types"]} == ENTITY_KIND_VALUES
    assert len(schema["properties"]) != 13


def test_the_session_never_receives_the_whole_repository(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(scripts=[java_round_one_script()])
    with knowledge_at(tmp_path) as knowledge:
        Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
    briefing_text = provider.seen_objectives[0]
    assert "public String place(String reference)" not in briefing_text
    assert "spring-boot-starter-web" not in briefing_text


def test_forged_evidence_hash_is_rejected(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(
        scripts=[
            Script(
                steps=(capture_step(SERVICE, 10, 12),),
                findings=[
                    {
                        "type": "business_rule",
                        "subject": "order reference is persisted",
                        "statement": "OrderService place saves the reference",
                        "conditions": ["a reference is supplied"],
                        "effects": ["the reference is saved"],
                        "evidence": [ref(SERVICE, 1, 2)],
                        "confidence": "supported",
                    }
                ],
            )
        ]
    )
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
        rules = knowledge.find_entities(EntityKind.BUSINESS_RULE.value)
        assert [rule.confidence for rule in rules] == [Confidence.UNRESOLVED]
        assert any("order reference" in item for item in outcome.unresolved)


def test_comment_evidence_never_reaches_supported(tmp_path: Path) -> None:
    write(
        tmp_path / "repo",
        "src/Remark.java",
        "// the renewal rule renews the contract yearly\n",
    )
    snapshot = snapshot_of(tmp_path / "repo")
    provider = FakeProvider(
        scripts=[
            Script(
                steps=(capture_step("src/Remark.java", 1, 1),),
                findings=[
                    {
                        "type": "business_rule",
                        "subject": "renewal rule",
                        "statement": "the renewal rule renews the contract yearly",
                        "conditions": ["the year ends"],
                        "effects": ["the contract renews"],
                        "evidence": [ref("src/Remark.java", 1, 1)],
                        "confidence": "supported",
                    }
                ],
            )
        ]
    )
    with knowledge_at(tmp_path) as knowledge:
        Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
        rule = knowledge.find_entities(EntityKind.BUSINESS_RULE.value)[0]
        assert rule.confidence is Confidence.INFERRED
        assert rule.attributes["verification_notes"]


def test_finding_without_evidence_is_unresolved_and_opens_a_gap(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(
        scripts=[
            Script(
                steps=(("repo.inventory", {}),),
                findings=[
                    {
                        "type": "capability",
                        "subject": "cancel order",
                        "statement": "an order can be cancelled",
                    }
                ],
            )
        ]
    )
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
        capability = knowledge.find_entities(EntityKind.CAPABILITY.value)[0]
        assert capability.confidence is Confidence.UNRESOLVED
        gaps = knowledge.find_entities(GAP_KIND)
        assert any("cancel order" in gap.name for gap in gaps)
        assert outcome.details["gaps_opened"]


def test_contradiction_is_marked_and_opens_a_gap(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path / "repo")
    common: Mapping[str, Any] = {
        "type": "business_rule",
        "subject": "order reference handling",
        "conditions": ["a reference is supplied"],
        "evidence": [ref(SERVICE, 10, 12)],
        "confidence": "supported",
    }
    provider = FakeProvider(
        scripts=[
            Script(
                steps=(capture_step(SERVICE, 10, 12),),
                findings=[
                    {
                        **common,
                        "statement": "OrderService place accepts the order reference",
                        "effects": ["accept the order reference"],
                    },
                    {
                        **common,
                        "statement": "OrderService place rejects the order reference",
                        "effects": ["reject the order reference"],
                    },
                ],
            )
        ]
    )
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
        rules = knowledge.find_entities(EntityKind.BUSINESS_RULE.value)
        assert all(rule.confidence is Confidence.CONTRADICTED for rule in rules)
        assert any("contradictory" in item for item in outcome.unresolved)
        assert knowledge.find_entities(GAP_KIND)


def test_frontier_drives_a_second_round_that_resolves_it(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(
        scripts=[
            Script(
                steps=(capture_step(SERVICE, 10, 12),),
                findings=[
                    {
                        "type": "capability",
                        "subject": "place order",
                        "statement": "OrderService place delegates to OrderRepository save",
                        "evidence": [ref(SERVICE, 10, 12)],
                        "confidence": "supported",
                        "relations": [
                            {
                                "kind": "calls",
                                "target_subject": "OrderRepository save",
                                "target_type": "operation",
                            }
                        ],
                    }
                ],
            ),
            Script(
                steps=(capture_step(REPOSITORY, 3, 4),),
                findings=[
                    {
                        "type": "operation",
                        "subject": "OrderRepository save",
                        "statement": "OrderRepository save stores the order reference",
                        "attributes": {"verb": "save"},
                        "evidence": [ref(REPOSITORY, 3, 4)],
                        "confidence": "supported",
                    }
                ],
            ),
        ]
    )
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
        assert provider.calls >= 2
        assert outcome.details["round_count"] >= 2
        assert not outcome.details["coverage"]["unresolved_calls"]
        operation = [
            entity
            for entity in knowledge.find_entities(EntityKind.OPERATION.value)
            if entity.name == "OrderRepository save"
        ][0]
        assert operation.confidence is Confidence.SUPPORTED


def test_the_second_round_receives_the_compacted_state_not_the_first_transcript(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(
        scripts=[
            Script(
                steps=(capture_step(SERVICE, 10, 12),),
                findings=[
                    {
                        "type": "capability",
                        "subject": "place order",
                        "statement": "OrderService place delegates to OrderRepository save",
                        "evidence": [ref(SERVICE, 10, 12)],
                        "confidence": "supported",
                        "relations": [
                            {
                                "kind": "calls",
                                "target_subject": "OrderRepository save",
                                "target_type": "operation",
                            }
                        ],
                    }
                ],
            ),
            Script(steps=(("repo.inventory", {}),)),
        ]
    )
    with knowledge_at(tmp_path) as knowledge:
        Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
    second = provider.seen_objectives[1]
    assert "Round: 2" in second
    assert "Accepted findings:" in second
    assert "capability | place order | supported" in second
    assert "OrderRepository save" in second
    assert "return repository.save(reference)" not in second


def test_exhausted_budget_ends_the_run_and_lists_the_unresolved(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(
        scripts=[
            Script(
                steps=(capture_step(SERVICE, 10, 12),),
                findings=[
                    {
                        "type": "capability",
                        "subject": "place order",
                        "statement": "OrderService place delegates to OrderRepository save",
                        "evidence": [ref(SERVICE, 10, 12)],
                        "confidence": "supported",
                        "relations": [
                            {
                                "kind": "calls",
                                "target_subject": "OrderRepository save",
                                "target_type": "operation",
                            }
                        ],
                    }
                ],
            )
        ]
    )
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator(total_tool_calls=1).run(
            OBJECTIVE, snapshot, knowledge, provider, "acme"
        )
    assert provider.calls == 1
    assert any("OrderRepository save" in item for item in outcome.unresolved)
    assert outcome.details["coverage"]["unresolved_calls"] == ["OrderRepository save"]


def test_reexecution_over_the_same_snapshot_is_idempotent(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path / "repo")
    with knowledge_at(tmp_path) as knowledge:
        first = Investigator().run(
            OBJECTIVE, snapshot, knowledge, FakeProvider(scripts=[java_round_one_script()]), "acme"
        )
        entities = knowledge.entity_count()
        relations = knowledge.relation_count()
        evidence = len(knowledge.all_evidence_keys())
        second = Investigator().run(
            OBJECTIVE, snapshot, knowledge, FakeProvider(scripts=[java_round_one_script()]), "acme"
        )
        assert second.entities_written == first.entities_written
        assert second.relations_written == first.relations_written
        assert second.evidence_written == first.evidence_written
        assert knowledge.entity_count() == entities
        assert knowledge.relation_count() == relations
        assert len(knowledge.all_evidence_keys()) == evidence


def test_a_snapshot_changed_mid_run_aborts_with_a_reason(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    snapshot = java_repo(root)
    moving = {"snapshot": snapshot}

    def harness_factory(
        _: object, focus: ScopeFocus | None = None
    ) -> RepositoryHarness:
        return RepositoryHarness(moving["snapshot"], None, focus)

    def mutate(index: int) -> None:
        if index == 0:
            write(root, "src/main/java/com/acme/order/OrderExtra.java", "class Extra {}\n")
            moving["snapshot"] = snapshot_of(root)

    provider = FakeProvider(
        scripts=[Script(steps=(("repo.inventory", {}),))], on_round=mutate
    )
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator(harness_factory=harness_factory).run(
            OBJECTIVE, snapshot, knowledge, provider, "acme"
        )
    assert outcome.details["aborted"] == ABORT_SNAPSHOT_CHANGED
    assert outcome.details["task_state"] == "failed"


def test_a_provider_failure_is_recovered_and_retried(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(
        scripts=[
            Script(steps=(("repo.inventory", {}),), fail_with="agent lost the connection"),
            java_round_one_script(),
        ]
    )
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
    assert provider.calls >= 2
    assert "transient:agent_run_failed" in outcome.details["failures"]
    assert outcome.entities_written >= 6


def test_a_repeated_provider_failure_gives_up(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(
        scripts=[
            Script(steps=(("repo.inventory", {}),), fail_with="agent lost the connection"),
            Script(steps=(("repo.inventory", {}),), fail_with="agent lost the connection"),
        ]
    )
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
    assert outcome.details["aborted"] == "agent_run_failed"
    assert outcome.details["task_state"] == "failed"


def test_cobol_reaches_supported_without_any_parser(tmp_path: Path) -> None:
    snapshot = mainframe_repo(tmp_path / "repo")
    provider = FakeProvider(
        scripts=[
            Script(
                steps=(
                    ("repo.search", {"pattern": "PERFORM"}),
                    ("repo.symbol", {"path": "cobol/PAYRUN.cbl"}),
                    ("repo.read", {"path": "cobol/PAYRUN.cbl"}),
                    capture_step("cobol/PAYRUN.cbl", 1, 2, "PAYRUN"),
                    capture_step("cobol/PAYRUN.cbl", 9, 9, "TAXCALC"),
                    capture_step("cobol/PAYRUN.cbl", 8, 8, "CALC-TOTAL"),
                    capture_step("copybook/PAYREC.cpy", 1, 3, "PAY-RECORD"),
                ),
                findings=[
                    {
                        "type": "module",
                        "subject": "PAYRUN",
                        "statement": "PAYRUN is the payroll program identified in the source",
                        "evidence": [ref("cobol/PAYRUN.cbl", 1, 2)],
                        "confidence": "supported",
                    },
                    {
                        "type": "operation",
                        "subject": "TAXCALC call",
                        "statement": "PAYRUN issues a CALL to TAXCALC with WS-GROSS and WS-NET",
                        "attributes": {"verb": "call"},
                        "evidence": [ref("cobol/PAYRUN.cbl", 9, 9)],
                        "confidence": "supported",
                    },
                    {
                        "type": "flow_step",
                        "subject": "CALC-TOTAL perform",
                        "statement": "PAYRUN performs CALC-TOTAL before the CALL",
                        "attributes": {"ordinal": 1},
                        "evidence": [ref("cobol/PAYRUN.cbl", 8, 8)],
                        "confidence": "supported",
                    },
                    {
                        "type": "data_contract",
                        "subject": "PAYREC copybook",
                        "statement": "PAY-RECORD declares WS-HOURS and WS-RATE",
                        "evidence": [ref("copybook/PAYREC.cpy", 1, 3)],
                        "confidence": "supported",
                    },
                ],
            )
        ]
    )
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator().run(
            "analyze repository", snapshot, knowledge, provider, "payroll"
        )
        assert outcome.entities_written == 4
        supported = {
            entity.kind
            for entity in knowledge.find_entities()
            if entity.confidence is Confidence.SUPPORTED
        }
        assert supported == {
            EntityKind.MODULE.value,
            EntityKind.OPERATION.value,
            EntityKind.FLOW_STEP.value,
            EntityKind.DATA_CONTRACT.value,
        }


def test_outcome_details_carry_rounds_coverage_and_verification(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path / "repo")
    provider = FakeProvider(scripts=[java_round_one_script()])
    with knowledge_at(tmp_path) as knowledge:
        outcome = Investigator().run(OBJECTIVE, snapshot, knowledge, provider, "acme")
    details = outcome.details
    assert details["objective_hash"].startswith("obj_")
    assert details["snapshot_id"] == snapshot.digest
    assert details["rounds"][0]["tool_calls"] == 8
    assert details["rounds"][0]["findings_received"] == 6
    assert details["tool_calls"] == 8
    assert set(details["coverage"]) >= {
        "discovered_entrypoints",
        "discovered_capabilities",
        "unresolved_calls",
        "unresolved_effects",
        "unresolved_integrations",
        "unresolved_branches",
        "missing_evidence",
        "explicit_gaps",
        "files_covered",
    }
    assert details["verification"]["counts"]["supported"] == 6
    assert details["captures"]
    assert details["task_state"] == "succeeded"
