from __future__ import annotations

from pathlib import Path

from tests.repository.fixtures_repos import java_repo

from wiki_ai.knowledge.evidence import CodeLocator
from wiki_ai.knowledge.gaps import GAP_KIND, open_gaps
from wiki_ai.knowledge.model import Confidence, EntityId, KnowledgeState
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind
from wiki_ai.repository.evidence import capture
from wiki_ai.investigation.evidence import CaptureRegistry
from wiki_ai.investigation.finding import EvidenceRef, Finding, RelationClaim
from wiki_ai.investigation.normalizer import AUTHOR, normalize
from wiki_ai.investigation.verifier import verify

SERVICE = "src/main/java/com/acme/order/OrderService.java"
REPOSITORY = "src/main/java/com/acme/order/OrderRepository.java"


def knowledge_at(tmp_path: Path) -> KnowledgeRepository:
    return KnowledgeRepository.open(str(tmp_path / "state.db"))


def prepared(tmp_path: Path):
    snapshot = java_repo(tmp_path / "repo")
    registry = CaptureRegistry()
    registry.register(capture(snapshot, SERVICE, 10, 12, symbol="place"))
    registry.register(capture(snapshot, REPOSITORY, 3, 4, symbol="save"))
    return snapshot, registry


def place_and_save() -> list[Finding]:
    return [
        Finding(
            type=EntityKind.CAPABILITY,
            subject="OrderService place",
            statement="OrderService place delegates to repository save",
            evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
            relations=(
                RelationClaim(
                    kind=RelationKind.CALLS, target_subject="OrderRepository save"
                ),
            ),
        ),
        Finding(
            type=EntityKind.OPERATION,
            subject="OrderRepository save",
            statement="OrderRepository save persists the reference",
            attributes={"verb": "save"},
            evidence=(EvidenceRef(path=REPOSITORY, line_start=3, line_end=4),),
        ),
    ]


def test_findings_become_entities_relations_and_evidence(tmp_path: Path) -> None:
    snapshot, registry = prepared(tmp_path)
    report = verify(place_and_save(), registry, snapshot)
    with knowledge_at(tmp_path) as knowledge:
        result = normalize(report.verified, knowledge, snapshot, "acme", "obj_x")
        assert result.entities_written == 2
        assert result.relations_written == 1
        assert result.evidence_written == 2
        capability = knowledge.find_entities(EntityKind.CAPABILITY.value)[0]
        assert capability.name == "OrderService place"
        assert capability.confidence is Confidence.SUPPORTED
        assert capability.state is KnowledgeState.IMPLEMENTED
        evidence = knowledge.evidence_for(capability.id)
        assert len(evidence) == 1
        assert isinstance(evidence[0].locator, CodeLocator)
        assert evidence[0].source_id == "acme"
        assert evidence[0].version_hash == snapshot.digest


def test_everything_lands_in_a_single_revision(tmp_path: Path) -> None:
    snapshot, registry = prepared(tmp_path)
    report = verify(place_and_save(), registry, snapshot)
    with knowledge_at(tmp_path) as knowledge:
        normalize(report.verified, knowledge, snapshot, "acme", "obj_x")
        assert knowledge.revision_count() == 1
        revision = knowledge.get_revision(knowledge.head_revision_id() or "")
        assert revision is not None
        assert revision.author == AUTHOR
        assert revision.summary == "obj_x"


def test_reexecution_is_idempotent(tmp_path: Path) -> None:
    snapshot, registry = prepared(tmp_path)
    report = verify(place_and_save(), registry, snapshot)
    with knowledge_at(tmp_path) as knowledge:
        first = normalize(report.verified, knowledge, snapshot, "acme", "obj_x")
        entities = knowledge.entity_count()
        relations = knowledge.relation_count()
        evidence = len(knowledge.all_evidence_keys())
        second = normalize(report.verified, knowledge, snapshot, "acme", "obj_x")
        assert (second.entities_written, second.relations_written) == (
            first.entities_written,
            first.relations_written,
        )
        assert knowledge.entity_count() == entities
        assert knowledge.relation_count() == relations
        assert len(knowledge.all_evidence_keys()) == evidence


def test_entity_ids_are_deterministic_from_kind_and_subject(tmp_path: Path) -> None:
    snapshot, registry = prepared(tmp_path)
    report = verify(place_and_save(), registry, snapshot)
    with knowledge_at(tmp_path) as knowledge:
        result = normalize(report.verified, knowledge, snapshot, "acme", "obj_x")
    expected = EntityId.derive(
        EntityKind.CAPABILITY.value, "capability::orderservice place"
    ).value
    assert result.entity_ids["capability::orderservice place"] == expected


def test_relation_to_an_unknown_target_creates_a_placeholder_and_a_gap(
    tmp_path: Path,
) -> None:
    snapshot, registry = prepared(tmp_path)
    finding = Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService place",
        statement="OrderService place delegates to repository save",
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
        relations=(
            RelationClaim(kind=RelationKind.CALLS, target_subject="LedgerService post"),
        ),
    )
    report = verify([finding], registry, snapshot)
    with knowledge_at(tmp_path) as knowledge:
        result = normalize(report.verified, knowledge, snapshot, "acme", "obj_x")
        assert result.unresolved_relations == ("LedgerService post",)
        assert any("LedgerService post" in question for question in result.gaps_opened)
        assert open_gaps(knowledge)
        placeholder = [
            entity
            for entity in knowledge.find_entities(EntityKind.OPERATION.value)
            if entity.name == "LedgerService post"
        ]
        assert placeholder[0].confidence is Confidence.UNRESOLVED


def test_verifier_gaps_are_opened_in_knowledge(tmp_path: Path) -> None:
    snapshot, registry = prepared(tmp_path)
    finding = Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService cancel",
        statement="OrderService cancel voids the order",
    )
    report = verify([finding], registry, snapshot)
    with knowledge_at(tmp_path) as knowledge:
        normalize(
            report.verified, knowledge, snapshot, "acme", "obj_x", gaps=report.gaps
        )
        gaps = knowledge.find_entities(GAP_KIND)
        assert any("OrderService cancel" in gap.name for gap in gaps)


def test_unresolved_findings_are_stored_as_declared_not_implemented(
    tmp_path: Path,
) -> None:
    snapshot, registry = prepared(tmp_path)
    finding = Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService cancel",
        statement="OrderService cancel voids the order",
    )
    report = verify([finding], registry, snapshot)
    with knowledge_at(tmp_path) as knowledge:
        normalize(report.verified, knowledge, snapshot, "acme", "obj_x")
        entity = [
            item
            for item in knowledge.find_entities(EntityKind.CAPABILITY.value)
            if item.name == "OrderService cancel"
        ][0]
        assert entity.confidence is Confidence.UNRESOLVED
        assert entity.state is KnowledgeState.DECLARED


def test_an_invalid_relation_pair_is_reported_not_written(tmp_path: Path) -> None:
    snapshot, registry = prepared(tmp_path)
    finding = Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService place",
        statement="OrderService place delegates to repository save",
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
        relations=(
            RelationClaim(
                kind=RelationKind.TRANSITIONS_TO,
                target_subject="approved",
                target_type=EntityKind.STATE,
            ),
        ),
    )
    report = verify([finding], registry, snapshot)
    with knowledge_at(tmp_path) as knowledge:
        result = normalize(report.verified, knowledge, snapshot, "acme", "obj_x")
        assert result.relations_written == 0
        assert result.diagnostics
