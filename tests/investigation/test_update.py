from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.investigation.fake_provider import FakeProvider, Script
from tests.investigation.fixtures_snapshots import java_repo, snapshot_of, write

from wiki_ai.agent.protocol import ToolCall
from wiki_ai.knowledge.gaps import GAP_KIND
from wiki_ai.knowledge.gate import KnowledgeRule
from wiki_ai.knowledge.gate import check as knowledge_gate
from wiki_ai.knowledge.model import Confidence, KnowledgeState
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind
from wiki_ai.investigation.orchestrator import Investigator
from wiki_ai.investigation.orchestrator import (
    ABORT_SNAPSHOT_NOT_MATERIALIZED,
    InvestigationStatus,
)
from wiki_ai.repository.snapshot import SnapshotSpec, take_snapshot
from wiki_ai.investigation.update import (
    SKIPPED_NO_DIFF,
    SKIPPED_NO_PROVIDER,
    UpdateEngine,
    dependency_key,
    impacted_evidence,
    plan_reinvestigation,
)
from wiki_ai.investigation.objective import Objective, ObjectiveKind
from wiki_ai.repository.snapshot import diff

BASE = "src/main/java/com/acme/order"
CONTROLLER = f"{BASE}/OrderController.java"
SERVICE = f"{BASE}/OrderService.java"
REPOSITORY = f"{BASE}/OrderRepository.java"
PRODUCER = f"{BASE}/OrderProducer.java"
NAMESPACE = "acme"
BLOCKING_RULES = frozenset(
    {
        KnowledgeRule.OBSOLETE_WITHOUT_INVALIDATION,
        KnowledgeRule.SUPPORTED_WITHOUT_EVIDENCE,
        KnowledgeRule.EVIDENCE_DOES_NOT_RESOLVE,
    }
)
OBJECTIVE = "analyze repository"

_SERVICE_V2 = """package com.acme.order;

import com.acme.order.OrderRepository;

public class OrderService {
    private final OrderRepository repository;
    public OrderService(OrderRepository repository) {
        this.repository = repository;
    }
    public String place(String reference) {
        if (reference == null) {
            return "rejected";
        }
        return repository.save(reference);
    }
}
"""


def knowledge_at(tmp_path: Path) -> KnowledgeRepository:
    return KnowledgeRepository.open(str(tmp_path / "knowledge" / "state.db"))


def capture_step(path: str, start: int, end: int, symbol: str | None = None):
    arguments: dict[str, Any] = {"path": path, "line_start": start, "line_end": end}
    if symbol is not None:
        arguments["symbol"] = symbol
    return ("evidence.capture", arguments)


def ref(path: str, start: int, end: int) -> dict[str, Any]:
    return {"path": path, "line_start": start, "line_end": end}


def baseline_script() -> Script:
    return Script(
        steps=(
            capture_step(SERVICE, 10, 12, "place"),
            capture_step(REPOSITORY, 3, 4, "save"),
            capture_step(PRODUCER, 10, 12, "emit"),
        ),
        findings=[
            {
                "type": "business_rule",
                "subject": "order reference is persisted",
                "statement": "OrderService place saves the reference in the repository",
                "conditions": ["a reference is supplied"],
                "effects": ["the reference reaches OrderRepository save"],
                "evidence": [ref(SERVICE, 10, 12)],
                "confidence": "supported",
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
        ],
    )


def reinvestigation_script(start: int, end: int) -> Script:
    return Script(
        steps=(capture_step(SERVICE, start, end, "place"),),
        findings=[
            {
                "type": "business_rule",
                "subject": "order reference is persisted",
                "statement": "OrderService place saves the reference in the repository",
                "conditions": ["a reference is supplied"],
                "effects": ["the reference reaches OrderRepository save"],
                "evidence": [ref(SERVICE, start, end)],
                "confidence": "supported",
            }
        ],
    )


def entity_named(knowledge: KnowledgeRepository, kind: str, name: str):
    for entity in knowledge.find_entities(kind):
        if entity.name == name:
            return entity
    raise AssertionError(f"{kind} {name} not stored")


@pytest.fixture()
def analysed(tmp_path: Path):
    root = tmp_path / "repo"
    previous = java_repo(root)
    knowledge = knowledge_at(tmp_path)
    Investigator().run(
        OBJECTIVE, previous, knowledge, FakeProvider(scripts=[baseline_script()]), NAMESPACE
    )
    yield root, previous, knowledge
    knowledge.close()


def change_service(root: Path) -> Any:
    write(root, f"{BASE}/OrderService.java", _SERVICE_V2)
    return snapshot_of(root)


def remove_producer(root: Path) -> Any:
    (root / BASE / "OrderProducer.java").unlink()
    return snapshot_of(root)


def test_dependency_key_is_namespace_path_and_file_hash(analysed) -> None:
    _root, previous, _knowledge = analysed
    key = dependency_key(NAMESPACE, SERVICE, previous)
    assert key is not None
    assert key.namespace == NAMESPACE
    assert key.path == SERVICE
    assert key.file_sha256 == previous.file_map()[SERVICE].sha256
    assert dependency_key(NAMESPACE, "src/absent.java", previous) is None


def test_impacted_evidence_only_covers_changed_and_removed_files(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    impacted = impacted_evidence(knowledge, previous, current, NAMESPACE)
    assert [item.locator.path for item in impacted] == [SERVICE]


def test_impacted_evidence_ignores_a_foreign_namespace(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    assert impacted_evidence(knowledge, previous, current, "other") == ()


def test_only_the_changed_file_entities_lose_supported(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    rule = entity_named(
        knowledge, EntityKind.BUSINESS_RULE.value, "order reference is persisted"
    )
    assert rule.confidence is Confidence.UNRESOLVED
    for name, kind in (
        ("OrderRepository save", EntityKind.PERSISTENCE.value),
        ("orders kafka producer", EntityKind.INTEGRATION.value),
    ):
        assert entity_named(knowledge, kind, name).confidence is Confidence.SUPPORTED


def test_invalidation_keeps_the_state_axis_and_deletes_nothing(analysed) -> None:
    root, previous, knowledge = analysed
    before_entities = knowledge.entity_count()
    current = change_service(root)
    UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    rule = entity_named(
        knowledge, EntityKind.BUSINESS_RULE.value, "order reference is persisted"
    )
    assert rule.state is KnowledgeState.IMPLEMENTED
    assert knowledge.entity_count() >= before_entities
    assert knowledge.evidence_for(rule.id)


def test_a_gap_is_opened_for_each_invalidated_entity(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    outcome = UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    assert outcome.invalidated.gaps == (f"behavior may have changed in {SERVICE}",)
    stored = [gap.name for gap in knowledge.find_entities(GAP_KIND)]
    assert f"behavior may have changed in {SERVICE}" in stored


def test_the_whole_invalidation_lands_in_a_single_update_revision(analysed) -> None:
    root, previous, knowledge = analysed
    before = knowledge.revision_count()
    current = change_service(root)
    outcome = UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    assert knowledge.revision_count() == before + 1
    revision = knowledge.get_revision(outcome.invalidated.revision_id)
    assert revision is not None
    assert revision.author == "update"


def test_relations_of_an_invalidated_entity_become_unresolved(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    outcome = UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    for relation_id in outcome.invalidated.relations:
        assert knowledge.get_relation(relation_id).confidence is Confidence.UNRESOLVED


def test_removing_a_file_turns_its_entity_historical(analysed) -> None:
    root, previous, knowledge = analysed
    current = remove_producer(root)
    UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    integration = entity_named(
        knowledge, EntityKind.INTEGRATION.value, "orders kafka producer"
    )
    assert integration.state is KnowledgeState.HISTORICAL
    assert integration.confidence is Confidence.UNRESOLVED


def test_a_changed_file_never_turns_its_entity_historical(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    rule = entity_named(
        knowledge, EntityKind.BUSINESS_RULE.value, "order reference is persisted"
    )
    assert rule.state is KnowledgeState.IMPLEMENTED


def test_plan_reinvestigation_is_targeted_and_scoped(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    outcome = UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    objective = plan_reinvestigation(outcome.invalidated, diff(previous, current))
    assert objective.kind is ObjectiveKind.TARGETED_REINVESTIGATION
    assert objective.scope.paths == (SERVICE,)
    assert objective.scope.entities == outcome.invalidated.entities
    assert not objective.scope.is_repository_wide


def test_an_empty_diff_is_an_idempotent_noop(analysed) -> None:
    _root, previous, knowledge = analysed
    before = (
        knowledge.entity_count(),
        knowledge.relation_count(),
        knowledge.revision_count(),
    )
    outcome = UpdateEngine().run(previous, previous, knowledge, None, NAMESPACE)
    assert outcome.skipped_reason == SKIPPED_NO_DIFF
    assert outcome.invalidated.total == 0
    assert outcome.reinvestigation is None
    assert (
        knowledge.entity_count(),
        knowledge.relation_count(),
        knowledge.revision_count(),
    ) == before


def test_without_a_provider_the_knowledge_stays_honest(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    outcome = UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    assert outcome.skipped_reason == SKIPPED_NO_PROVIDER
    assert outcome.reinvestigation is None
    rule = entity_named(
        knowledge, EntityKind.BUSINESS_RULE.value, "order reference is persisted"
    )
    assert rule.confidence is Confidence.UNRESOLVED


def test_the_reinvestigation_restores_supported_for_the_changed_file(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    provider = FakeProvider(scripts=[reinvestigation_script(10, 15)])
    outcome = UpdateEngine().run(previous, current, knowledge, provider, NAMESPACE)
    assert outcome.skipped_reason == ""
    assert outcome.reinvestigation is not None
    rule = entity_named(
        knowledge, EntityKind.BUSINESS_RULE.value, "order reference is persisted"
    )
    assert rule.confidence is Confidence.SUPPORTED
    hashes = {item.version_hash for item in knowledge.evidence_for(rule.id)}
    assert current.digest in hashes


def test_the_reinvestigation_never_reaches_outside_the_scope(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    provider = FakeProvider(scripts=[reinvestigation_script(10, 15)])
    UpdateEngine().run(previous, current, knowledge, provider, NAMESPACE)
    touched = {payload["path"] for payload in provider.captures}
    assert touched == {SERVICE}
    briefing_text = provider.seen_objectives[0]
    assert f"Scope: paths={SERVICE}" in briefing_text
    assert CONTROLLER not in briefing_text


def test_nothing_stays_obsolete_without_invalidation_after_the_update(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    provider = FakeProvider(scripts=[reinvestigation_script(10, 15)])
    UpdateEngine().run(previous, current, knowledge, provider, NAMESPACE)
    found = knowledge_gate(knowledge, {NAMESPACE: current.digest})
    assert BLOCKING_RULES.isdisjoint({item.rule for item in found})


def test_nothing_stays_obsolete_without_invalidation_without_a_provider(
    analysed,
) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    found = knowledge_gate(knowledge, {NAMESPACE: current.digest})
    assert BLOCKING_RULES.isdisjoint({item.rule for item in found})


def test_before_the_update_the_gate_reports_uninvalidated_dependents(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    found = knowledge_gate(knowledge, {NAMESPACE: current.digest})
    assert KnowledgeRule.OBSOLETE_WITHOUT_INVALIDATION in {item.rule for item in found}


def test_the_gate_is_clean_when_the_previous_version_is_the_current_one(
    analysed,
) -> None:
    _root, previous, knowledge = analysed
    assert knowledge_gate(knowledge, {NAMESPACE: previous.digest}) == ()


def test_rerunning_the_same_update_duplicates_nothing(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    first = FakeProvider(scripts=[reinvestigation_script(10, 15)])
    UpdateEngine().run(previous, current, knowledge, first, NAMESPACE)
    before = (
        knowledge.entity_count(),
        knowledge.relation_count(),
        int(knowledge.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]),
        len(knowledge.source_versions()),
    )
    second = FakeProvider(scripts=[reinvestigation_script(10, 15)])
    UpdateEngine().run(previous, current, knowledge, second, NAMESPACE)
    after = (
        knowledge.entity_count(),
        knowledge.relation_count(),
        int(knowledge.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]),
        len(knowledge.source_versions()),
    )
    assert after == before


def test_a_new_source_version_is_registered_and_the_previous_stays_stored(
    analysed,
) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    hashes = {item.version_hash for item in knowledge.source_versions(NAMESPACE)}
    assert {previous.digest, current.digest} <= hashes


def test_a_repository_without_prior_knowledge_invalidates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    previous = java_repo(root)
    current = change_service(root)
    with knowledge_at(tmp_path) as knowledge:
        outcome = UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
        assert outcome.invalidated.entities == ()
        assert outcome.skipped_reason == SKIPPED_NO_PROVIDER
        found = knowledge_gate(knowledge, {NAMESPACE: current.digest})
        assert BLOCKING_RULES.isdisjoint({item.rule for item in found})


def test_the_update_hands_the_ready_objective_to_the_investigator(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    received: list[Any] = []

    class _Recording:
        def run(self, objective, snapshot, store, provider, namespace):
            received.append(objective)
            return Investigator().run(objective, snapshot, store, provider, namespace)

    provider = FakeProvider(scripts=[reinvestigation_script(10, 15)])
    outcome = UpdateEngine(_Recording()).run(
        previous, current, knowledge, provider, NAMESPACE
    )
    assert len(received) == 1
    planned = plan_reinvestigation(outcome.invalidated, diff(previous, current))
    assert isinstance(received[0], Objective)
    assert received[0].hash == planned.hash
    assert received[0].scope.paths == (SERVICE,)


def test_the_reinvestigation_reports_the_focus_it_ran_under(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    provider = FakeProvider(scripts=[reinvestigation_script(10, 15)])
    outcome = UpdateEngine().run(previous, current, knowledge, provider, NAMESPACE)
    assert outcome.reinvestigation is not None
    details = outcome.reinvestigation.details
    assert details["focus_paths"] == [SERVICE]
    assert details["outside_focus_reads"] == 0
    assert details["files_read"] == [SERVICE]


class _SearchingProvider(FakeProvider):
    searched: list[dict[str, Any]]

    def __init__(self, scripts: list[Script]) -> None:
        super().__init__(scripts=scripts)
        self.searched = []

    def run(self, session):
        for arguments in (
            {"pattern": "place"},
            {"pattern": "place", "scope": "repository"},
        ):
            result = session.invoke(
                ToolCall(name="repo.search", arguments=arguments)
            )
            self.searched.append(dict(result.payload))
        return super().run(session)


def test_an_unscoped_search_during_the_update_sees_only_the_changed_file(
    analysed,
) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    provider = _SearchingProvider(scripts=[reinvestigation_script(10, 15)])
    UpdateEngine().run(previous, current, knowledge, provider, NAMESPACE)
    focused, widened = provider.searched[0], provider.searched[1]
    assert set(focused["paths"]) == {SERVICE}
    assert CONTROLLER in widened["paths"]
    assert "Focus:" + chr(10) + "- " + SERVICE in provider.seen_objectives[0]


def test_update_without_provider_is_blocked_not_ok(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    outcome = UpdateEngine().run(previous, current, knowledge, None, NAMESPACE)
    assert outcome.status is InvestigationStatus.BLOCKED
    assert outcome.reason == SKIPPED_NO_PROVIDER
    assert outcome.to_dict()["status"] == "blocked"


def test_update_over_a_non_materialized_snapshot_fails(analysed) -> None:
    root, previous, knowledge = analysed
    write(root, f"{BASE}/OrderService.java", _SERVICE_V2)
    bare = take_snapshot(SnapshotSpec(root=root))
    provider = FakeProvider(scripts=[reinvestigation_script(10, 15)])
    outcome = UpdateEngine().run(previous, bare, knowledge, provider, NAMESPACE)
    assert outcome.status is InvestigationStatus.FAILED
    assert outcome.reason == ABORT_SNAPSHOT_NOT_MATERIALIZED


def test_update_carries_the_reinvestigation_status(analysed) -> None:
    root, previous, knowledge = analysed
    current = change_service(root)
    provider = FakeProvider(scripts=[reinvestigation_script(10, 15)])
    outcome = UpdateEngine().run(previous, current, knowledge, provider, NAMESPACE)
    assert outcome.reinvestigation is not None
    assert outcome.status is outcome.reinvestigation.status
    assert outcome.to_dict()["reinvestigation"]["status"] == outcome.status.value
