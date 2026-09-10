from __future__ import annotations

from pathlib import Path

from tests.investigation.fixtures_snapshots import java_repo

from wiki_ai.knowledge.gaps import GAP_KIND
from wiki_ai.knowledge.identity import contextual_key
from wiki_ai.knowledge.model import Confidence, EntityId
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind
from wiki_ai.repository.evidence import capture
from wiki_ai.investigation.evidence import CaptureRegistry
from wiki_ai.investigation.finding import EvidenceRef, Finding, RelationClaim
from wiki_ai.investigation.normalizer import normalize
from wiki_ai.investigation.target import AMBIGUOUS_GAP_KIND
from wiki_ai.investigation.verifier import verify

SERVICE = "src/main/java/com/acme/order/OrderService.java"
REPOSITORY = "src/main/java/com/acme/order/OrderRepository.java"
NAMESPACE = "acme"

SERVICE_REF = EvidenceRef(path=SERVICE, line_start=10, line_end=12)
REPOSITORY_REF = EvidenceRef(path=REPOSITORY, line_start=3, line_end=4)
WIRING_REF = EvidenceRef(path=SERVICE, line_start=5, line_end=11)


def prepared(tmp_path: Path):
    snapshot = java_repo(tmp_path / "repo")
    registry = CaptureRegistry()
    registry.register(capture(snapshot, SERVICE, 10, 12, symbol="place"))
    registry.register(capture(snapshot, REPOSITORY, 3, 4, symbol="save"))
    registry.register(capture(snapshot, SERVICE, 5, 11, symbol="OrderService"))
    return snapshot, registry


def knowledge_at(tmp_path: Path) -> KnowledgeRepository:
    return KnowledgeRepository.open(str(tmp_path / "state.db"))


def eligibility(owner: str) -> Finding:
    return Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="Eligibility",
        statement="place saves the reference through the repository",
        conditions=("a reference is given",),
        effects=("repository save is called with the reference",),
        owner=owner,
        evidence=(SERVICE_REF,),
    )


def superseding(claim: RelationClaim) -> Finding:
    return Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="Eligibility Revision",
        statement="place saves the reference through the repository",
        conditions=("a reference is given",),
        effects=("repository save is called with the reference",),
        evidence=(SERVICE_REF,),
        relations=(claim,),
    )


def normalized(tmp_path: Path, findings, knowledge: KnowledgeRepository):
    snapshot, registry = prepared(tmp_path)
    report = verify(findings, registry, snapshot, NAMESPACE)
    return normalize(report.verified, knowledge, snapshot, NAMESPACE, "obj_x")


def rule_id(owner: str) -> EntityId:
    return EntityId.derive(
        EntityKind.BUSINESS_RULE.value,
        contextual_key(NAMESPACE, EntityKind.BUSINESS_RULE.value, owner, "Eligibility"),
    )


def test_two_owners_and_a_bare_target_subject_block_the_relation(
    tmp_path: Path,
) -> None:
    claim = RelationClaim(
        kind=RelationKind.SUPERSEDES,
        target_subject="Eligibility",
        target_type=EntityKind.BUSINESS_RULE,
    )
    findings = [eligibility("Ordering"), eligibility("Billing"), superseding(claim)]
    with knowledge_at(tmp_path) as knowledge:
        result = normalized(tmp_path, findings, knowledge)
        assert result.relations_written == 0
        assert knowledge.relation_count() == 0
        assert "relation_target_ambiguous:Eligibility" in result.diagnostics
        assert any(
            AMBIGUOUS_GAP_KIND in question for question in result.gaps_opened
        )
        assert "Eligibility" in result.unresolved_relations
        gaps = knowledge.find_entities(GAP_KIND)
        assert any(AMBIGUOUS_GAP_KIND in gap.name for gap in gaps)


def test_target_owner_resolves_the_ambiguity_to_the_named_owner(
    tmp_path: Path,
) -> None:
    claim = RelationClaim(
        kind=RelationKind.SUPERSEDES,
        target_subject="Eligibility",
        target_type=EntityKind.BUSINESS_RULE,
        target_owner="Billing",
    )
    findings = [eligibility("Ordering"), eligibility("Billing"), superseding(claim)]
    with knowledge_at(tmp_path) as knowledge:
        result = normalized(tmp_path, findings, knowledge)
        assert result.relations_written == 1
        assert not any(
            AMBIGUOUS_GAP_KIND in question for question in result.gaps_opened
        )
        relation = knowledge.find_relations(RelationKind.SUPERSEDES.value)[0]
        assert relation.target_id == rule_id("Billing")
        assert relation.target_id != rule_id("Ordering")


def test_target_id_resolves_the_ambiguity_to_the_explicit_entity(
    tmp_path: Path,
) -> None:
    identified = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="Eligibility",
        statement="place saves the reference through the repository",
        conditions=("a reference is given",),
        effects=("repository save is called with the reference",),
        owner="Ordering",
        explicit_id="RULE-7",
        evidence=(SERVICE_REF,),
    )
    claim = RelationClaim(
        kind=RelationKind.SUPERSEDES,
        target_subject="Eligibility",
        target_type=EntityKind.BUSINESS_RULE,
        target_id="RULE-7",
    )
    findings = [identified, eligibility("Billing"), superseding(claim)]
    with knowledge_at(tmp_path) as knowledge:
        result = normalized(tmp_path, findings, knowledge)
        assert result.relations_written == 1
        relation = knowledge.find_relations(RelationKind.SUPERSEDES.value)[0]
        expected = EntityId.derive(
            EntityKind.BUSINESS_RULE.value, "explicit::RULE-7"
        )
        assert relation.target_id == expected


def calls_repository(claim: RelationClaim) -> Finding:
    return Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService place",
        statement="OrderService place delegates to repository save",
        evidence=(SERVICE_REF,),
        relations=(claim,),
    )


def repository_save() -> Finding:
    return Finding(
        type=EntityKind.OPERATION,
        subject="OrderRepository save",
        statement="OrderRepository save persists the reference",
        attributes={"verb": "save"},
        evidence=(REPOSITORY_REF,),
    )


def relation_of(knowledge: KnowledgeRepository):
    return knowledge.find_relations(RelationKind.CALLS.value)[0]


def test_two_supported_endpoints_without_relation_evidence_stay_inferred(
    tmp_path: Path,
) -> None:
    claim = RelationClaim(
        kind=RelationKind.CALLS,
        target_subject="OrderRepository save",
        target_type=EntityKind.OPERATION,
    )
    findings = [calls_repository(claim), repository_save()]
    with knowledge_at(tmp_path) as knowledge:
        result = normalized(tmp_path, findings, knowledge)
        assert result.relations_written == 1
        source = knowledge.get_entity(relation_of(knowledge).source_id)
        target = knowledge.get_entity(relation_of(knowledge).target_id)
        assert source is not None and source.confidence is Confidence.SUPPORTED
        assert target is not None and target.confidence is Confidence.SUPPORTED
        assert relation_of(knowledge).confidence is Confidence.INFERRED
        assert knowledge.evidence_for_relation(relation_of(knowledge).id) == []


def test_relation_evidence_that_names_both_endpoints_makes_it_supported(
    tmp_path: Path,
) -> None:
    claim = RelationClaim(
        kind=RelationKind.CALLS,
        target_subject="OrderRepository save",
        target_type=EntityKind.OPERATION,
        evidence=(WIRING_REF,),
    )
    findings = [calls_repository(claim), repository_save()]
    with knowledge_at(tmp_path) as knowledge:
        result = normalized(tmp_path, findings, knowledge)
        assert result.relations_written == 1
        relation = relation_of(knowledge)
        assert relation.confidence is Confidence.SUPPORTED
        assert knowledge.evidence_for_relation(relation.id)


def test_relation_evidence_that_never_names_the_target_stays_inferred(
    tmp_path: Path,
) -> None:
    claim = RelationClaim(
        kind=RelationKind.CALLS,
        target_subject="LedgerService post",
        target_type=EntityKind.OPERATION,
        evidence=(SERVICE_REF,),
    )
    ledger = Finding(
        type=EntityKind.OPERATION,
        subject="LedgerService post",
        statement="LedgerService post writes the ledger entry",
        attributes={"verb": "post"},
        evidence=(REPOSITORY_REF,),
    )
    findings = [calls_repository(claim), ledger]
    with knowledge_at(tmp_path) as knowledge:
        result = normalized(tmp_path, findings, knowledge)
        assert result.relations_written == 1
        assert relation_of(knowledge).confidence is Confidence.INFERRED


def test_the_relation_check_reports_why_the_relation_is_not_supported(
    tmp_path: Path,
) -> None:
    snapshot, registry = prepared(tmp_path)
    claim = RelationClaim(
        kind=RelationKind.CALLS,
        target_subject="LedgerService post",
        target_type=EntityKind.OPERATION,
        evidence=(SERVICE_REF,),
    )
    report = verify([calls_repository(claim)], registry, snapshot, NAMESPACE)
    check = report.verified[0].relation_check(0)
    assert check.evidence_valid
    assert not check.claim_supported
    assert check.confidence is Confidence.INFERRED
    assert check.reasons


def test_a_relation_without_evidence_is_reported_as_such(tmp_path: Path) -> None:
    snapshot, registry = prepared(tmp_path)
    claim = RelationClaim(
        kind=RelationKind.CALLS,
        target_subject="OrderRepository save",
        target_type=EntityKind.OPERATION,
    )
    report = verify([calls_repository(claim)], registry, snapshot, NAMESPACE)
    check = report.verified[0].relation_check(0)
    assert not check.evidence_valid
    assert check.confidence is Confidence.INFERRED
    assert any("no evidence of its own" in reason for reason in check.reasons)


def test_a_same_named_entity_from_another_namespace_never_resolves_the_target(
    tmp_path: Path,
) -> None:
    snapshot, registry = prepared(tmp_path)
    foreign = verify([eligibility("Ordering")], registry, snapshot, "other")
    claim = RelationClaim(
        kind=RelationKind.SUPERSEDES,
        target_subject="Eligibility",
        target_type=EntityKind.BUSINESS_RULE,
    )
    with knowledge_at(tmp_path) as knowledge:
        normalize(foreign.verified, knowledge, snapshot, "other", "obj_other")
        result = normalized(tmp_path, [superseding(claim)], knowledge)
        relation = knowledge.find_relations(RelationKind.SUPERSEDES.value)[0]
        target = knowledge.get_entity(relation.target_id)
        assert target is not None
        assert target.confidence is Confidence.UNRESOLVED
        assert relation.confidence is Confidence.UNRESOLVED
        assert "Eligibility" in result.unresolved_relations


def test_a_persisted_entity_in_the_same_namespace_resolves_the_target(
    tmp_path: Path,
) -> None:
    snapshot, registry = prepared(tmp_path)
    earlier = verify([eligibility("Ordering")], registry, snapshot, NAMESPACE)
    claim = RelationClaim(
        kind=RelationKind.SUPERSEDES,
        target_subject="Eligibility",
        target_type=EntityKind.BUSINESS_RULE,
    )
    with knowledge_at(tmp_path) as knowledge:
        normalize(earlier.verified, knowledge, snapshot, NAMESPACE, "obj_first")
        result = normalized(tmp_path, [superseding(claim)], knowledge)
        assert result.relations_written == 1
        relation = knowledge.find_relations(RelationKind.SUPERSEDES.value)[0]
        assert relation.target_id == rule_id("Ordering")
        assert result.unresolved_relations == ()
