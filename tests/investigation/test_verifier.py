from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tests.repository.fixtures_repos import java_repo, snapshot_of, write

from wiki_ai.knowledge.model import Confidence
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind
from wiki_ai.repository.evidence import capture
from wiki_ai.investigation.evidence import CaptureRegistry
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
