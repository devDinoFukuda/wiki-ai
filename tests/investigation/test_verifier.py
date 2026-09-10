from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tests.investigation.fixtures_snapshots import java_repo, snapshot_of, write

from wiki_ai.knowledge.identity import excerpt_digest
from wiki_ai.knowledge.model import Confidence
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind
from wiki_ai.repository.evidence import capture
from wiki_ai.investigation.evidence import CaptureRegistry, to_knowledge
from wiki_ai.investigation.finding import EvidenceRef, Finding, RelationClaim
from wiki_ai.investigation.verifier import Rejection, verify

SERVICE = "src/main/java/com/acme/order/OrderService.java"


def registry_with(*items: object) -> CaptureRegistry:
    registry = CaptureRegistry()
    for item in items:
        registry.register(item)
    return registry


def place_finding(**overrides: object) -> Finding:
    payload: dict[str, object] = {
        "type": EntityKind.CAPABILITY,
        "subject": "OrderService place",
        "statement": "OrderService place delegates to repository save",
    }
    payload.update(overrides)
    return Finding(**payload)


def test_executable_evidence_yields_supported(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12, symbol="place")
    registry = registry_with(item)
    finding = place_finding(evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),))
    report = verify([finding], registry, snapshot)
    assert report.verified[0].confidence is Confidence.SUPPORTED
    assert report.counts["supported"] == 1


def test_forged_excerpt_hash_is_rejected_as_tampered(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12)
    forged = replace(item, excerpt_sha256="0" * 64)
    registry = registry_with(forged)
    finding = place_finding(evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),))
    report = verify([finding], registry, snapshot)
    assert not report.verified
    assert report.discarded[0].rejections == (Rejection.EVIDENCE_TAMPERED,)
    assert report.discarded[0].confidence is Confidence.UNRESOLVED


def test_evidence_from_another_snapshot_is_rejected(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12)
    other = replace(item, snapshot_id="f" * 64)
    finding = place_finding(evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),))
    report = verify([finding], registry_with(other), snapshot)
    assert report.discarded[0].rejections == (Rejection.EVIDENCE_TAMPERED,)


def test_comment_only_evidence_stays_inferred(tmp_path: Path) -> None:
    write(tmp_path, "src/Remark.java", "// OrderService place renews the order\n")
    snapshot = snapshot_of(tmp_path)
    item = capture(snapshot, "src/Remark.java", 1, 1)
    finding = place_finding(
        evidence=(EvidenceRef(path="src/Remark.java", line_start=1, line_end=1),),
        confidence=Confidence.SUPPORTED,
    )
    report = verify([finding], registry_with(item), snapshot)
    verified = report.verified[0]
    assert verified.confidence is Confidence.INFERRED
    assert Rejection.NO_EXECUTABLE_EVIDENCE in verified.rejections
    assert verified.reasons


def test_finding_without_evidence_is_unresolved_and_becomes_a_gap(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path)
    finding = place_finding(confidence=Confidence.SUPPORTED)
    report = verify([finding], CaptureRegistry(), snapshot)
    verified = report.verified[0]
    assert verified.confidence is Confidence.UNRESOLVED
    assert Rejection.NO_EVIDENCE in verified.rejections
    assert any("OrderService place" in question for question in report.gaps)


def test_evidence_never_captured_is_unresolved(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    finding = place_finding(evidence=(EvidenceRef(capture_id="cap_forged"),))
    report = verify([finding], CaptureRegistry(), snapshot)
    verified = report.verified[0]
    assert verified.confidence is Confidence.UNRESOLVED
    assert Rejection.EVIDENCE_UNRESOLVED in verified.rejections


def test_statement_unrelated_to_the_excerpt_is_downgraded(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12)
    finding = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="pension indexation",
        statement="pension indexation follows quarterly inflation",
        conditions=("quarter ended",),
        effects=("pension indexed",),
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
        confidence=Confidence.SUPPORTED,
    )
    report = verify([finding], registry_with(item), snapshot)
    verified = report.verified[0]
    assert verified.confidence is Confidence.INFERRED
    assert Rejection.STATEMENT_UNSUPPORTED_BY_EXCERPT in verified.rejections


def test_declared_contradiction_marks_both_findings(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12)
    ref = EvidenceRef(path=SERVICE, line_start=10, line_end=12)
    first = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="order place",
        statement="OrderService place always saves the order",
        conditions=("reference given",),
        effects=("order saved",),
        evidence=(ref,),
    )
    second = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="order place",
        statement="OrderService place never saves the order",
        conditions=("reference given",),
        effects=("order saved",),
        evidence=(ref,),
        contradicts=("order place",),
    )
    report = verify([first, second], registry_with(item), snapshot)
    assert {item.confidence for item in report.verified} == {Confidence.CONTRADICTED}
    assert report.gaps


def test_opposite_effects_on_the_same_subject_are_contradictions(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12)
    ref = EvidenceRef(path=SERVICE, line_start=10, line_end=12)
    first = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="order place",
        statement="OrderService place accepts the order",
        conditions=("reference given",),
        effects=("accept the order reference",),
        evidence=(ref,),
    )
    second = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="order place",
        statement="OrderService place rejects the order",
        conditions=("reference given",),
        effects=("reject the order reference",),
        evidence=(ref,),
    )
    report = verify([first, second], registry_with(item), snapshot)
    assert all(item.confidence is Confidence.CONTRADICTED for item in report.verified)


def test_different_subjects_are_not_contradictions(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12)
    ref = EvidenceRef(path=SERVICE, line_start=10, line_end=12)
    first = place_finding(evidence=(ref,))
    second = Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService cancel",
        statement="OrderService cancel never saves the order",
        evidence=(ref,),
    )
    report = verify([first, second], registry_with(item), snapshot)
    assert Confidence.CONTRADICTED not in {item.confidence for item in report.verified}


def test_relation_claims_survive_verification(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12)
    finding = place_finding(
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
        relations=(
            RelationClaim(kind=RelationKind.CALLS, target_subject="OrderRepository save"),
        ),
    )
    report = verify([finding], registry_with(item), snapshot)
    assert report.verified[0].finding.relations[0].target_subject == "OrderRepository save"


def test_reviewer_example_deleting_claim_over_a_find_excerpt_is_inferred(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        "src/OrderService.java",
        "public class OrderService {\n"
        "    public Order load(String id) {\n"
        "        return repository.find(id);\n"
        "    }\n"
        "}\n",
    )
    snapshot = snapshot_of(tmp_path)
    item = capture(snapshot, "src/OrderService.java", 3, 3, symbol="load")
    finding = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="OrderService",
        statement="OrderService deletes the order after three failures",
        conditions=("three consecutive failures happened",),
        effects=("the order is deleted",),
        evidence=(EvidenceRef(path="src/OrderService.java", line_start=3, line_end=3),),
        confidence=Confidence.SUPPORTED,
    )
    report = verify([finding], registry_with(item), snapshot)
    verified = report.verified[0]
    assert verified.confidence is Confidence.INFERRED
    assert verified.evidence_valid
    assert not verified.claim_supported
    assert Rejection.STATEMENT_UNSUPPORTED_BY_EXCERPT in verified.rejections


def test_class_name_alone_does_not_ground_a_business_rule(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12, symbol="place")
    finding = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="OrderService",
        statement="OrderService charges a penalty of 250 when the deadline expires",
        conditions=("the deadline expired",),
        effects=("a penalty of 250 is charged",),
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
        confidence=Confidence.SUPPORTED,
    )
    report = verify([finding], registry_with(item), snapshot)
    verified = report.verified[0]
    assert verified.confidence is Confidence.INFERRED
    assert not verified.claim_supported


def test_business_rule_whose_components_appear_in_the_excerpt_is_supported(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12, symbol="place")
    finding = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="place",
        statement="place saves the reference through the repository",
        conditions=("a reference is given",),
        effects=("repository save is called with the reference",),
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
    )
    report = verify([finding], registry_with(item), snapshot)
    verified = report.verified[0]
    assert verified.confidence is Confidence.SUPPORTED
    assert verified.evidence_valid
    assert verified.claim_supported


def test_grounding_names_the_component_that_failed(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12, symbol="place")
    finding = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="place",
        statement="place saves the reference",
        conditions=("a reference is given",),
        effects=("an invoice is escalated to the auditor",),
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
    )
    report = verify([finding], registry_with(item), snapshot)
    verified = report.verified[0]
    assert verified.confidence is Confidence.INFERRED
    failed = [check.component for check in verified.grounding if not check.ok]
    assert "effects[0]" in failed
    assert any("effects[0]" in reason for reason in verified.reasons)


def test_tampered_evidence_reports_evidence_invalid(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12)
    forged = replace(item, excerpt_sha256="0" * 64)
    finding = place_finding(
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),)
    )
    report = verify([finding], registry_with(forged), snapshot)
    discarded = report.discarded[0]
    assert not discarded.evidence_valid
    assert not discarded.claim_supported


def test_invariant_needs_its_statement_terms_in_executable_code(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12, symbol="place")
    finding = Finding(
        type=EntityKind.INVARIANT,
        subject="place",
        statement="the ledger balance never drops below the reserve floor",
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
        confidence=Confidence.SUPPORTED,
    )
    report = verify([finding], registry_with(item), snapshot)
    assert report.verified[0].confidence is Confidence.INFERRED


def test_the_verifier_grounds_on_the_same_excerpt_the_evidence_persists(
    tmp_path: Path,
) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, SERVICE, 10, 12, symbol="place")
    registry = registry_with(item)
    finding = place_finding(evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),))
    report = verify([finding], registry, snapshot)
    resolved = report.verified[0].evidence[0]
    _version, evidence = to_knowledge(resolved.capture, snapshot, "acme", "2026-01-01T00:00:00Z")
    assert evidence.excerpt == resolved.capture.excerpt
    assert evidence.excerpt
    assert excerpt_digest(evidence.excerpt) == evidence.excerpt_hash
