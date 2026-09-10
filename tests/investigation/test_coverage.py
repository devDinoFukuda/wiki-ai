from __future__ import annotations

from pathlib import Path

from tests.investigation.fixtures_snapshots import java_repo

from wiki_ai.knowledge.model import Confidence
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind
from wiki_ai.repository.evidence import capture
from wiki_ai.investigation.coverage import (
    GLOBAL_OWNER,
    CoverageState,
    FrontierItem,
    entity_identities,
    initial_state,
    update,
)
from wiki_ai.investigation.evidence import CaptureRegistry
from wiki_ai.investigation.finding import EvidenceRef, Finding, RelationClaim
from wiki_ai.investigation.verifier import verify

SERVICE = "src/main/java/com/acme/order/OrderService.java"


def verified_report(findings: list[Finding], tmp_path: Path):
    snapshot = java_repo(tmp_path)
    registry = CaptureRegistry()
    registry.register(capture(snapshot, SERVICE, 10, 12, symbol="place"))
    return verify(findings, registry, snapshot)


def test_initial_state_seeds_frontier_from_entrypoints_and_integrations() -> None:
    state = initial_state(7, entrypoints=("PAYRUN",), integrations=("kafka in pom.xml",))
    assert state.files_total == 7
    assert state.entrypoints == ("PAYRUN",)
    assert {item.kind for item in state.frontier} == {"entrypoint", "integration"}


def test_resolved_subject_leaves_the_frontier(tmp_path: Path) -> None:
    state = initial_state(7, entrypoints=("OrderService place",))
    finding = Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService place",
        statement="OrderService place delegates to repository save",
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
    )
    report = verified_report([finding], tmp_path)
    updated = update(state, report)
    assert not updated.frontier
    assert updated.rounds == 1
    assert "OrderService place" in updated.capabilities


def test_relation_to_an_unknown_target_creates_frontier(tmp_path: Path) -> None:
    finding = Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService place",
        statement="OrderService place delegates to repository save",
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
        relations=(
            RelationClaim(kind=RelationKind.CALLS, target_subject="OrderRepository save"),
        ),
    )
    updated = update(CoverageState(files_total=7), verified_report([finding], tmp_path))
    assert [item.subject for item in updated.frontier] == ["OrderRepository save"]
    assert updated.completeness().unresolved_calls == ("OrderRepository save",)


def test_condition_without_effect_becomes_an_unresolved_effect(tmp_path: Path) -> None:
    finding = Finding(
        type=EntityKind.DECISION,
        subject="OrderService place branch",
        statement="OrderService place branches on the reference",
        attributes={"criteria": "reference is blank"},
        conditions=("reference is blank",),
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
    )
    updated = update(CoverageState(files_total=7), verified_report([finding], tmp_path))
    assert updated.completeness().unresolved_effects == ("OrderService place branch",)


def test_more_conditions_than_effects_becomes_an_unresolved_branch(
    tmp_path: Path,
) -> None:
    finding = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="OrderService place rule",
        statement="OrderService place saves depending on the reference",
        conditions=("reference is blank", "reference is present"),
        effects=("order saved",),
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
    )
    updated = update(CoverageState(files_total=7), verified_report([finding], tmp_path))
    assert updated.completeness().unresolved_branches == ("OrderService place rule",)


def test_finding_without_evidence_becomes_missing_evidence_and_gap(
    tmp_path: Path,
) -> None:
    finding = Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService cancel",
        statement="OrderService cancel voids the order",
    )
    updated = update(CoverageState(files_total=7), verified_report([finding], tmp_path))
    report = updated.completeness()
    assert report.missing_evidence == ("capability: OrderService cancel",)
    assert report.explicit_gaps


def test_files_covered_is_auxiliary_and_never_a_gate(tmp_path: Path) -> None:
    finding = Finding(
        type=EntityKind.CAPABILITY,
        subject="OrderService place",
        statement="OrderService place delegates to repository save",
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
    )
    updated = update(
        CoverageState(files_total=7), verified_report([finding], tmp_path), ("pom.xml",)
    )
    report = updated.completeness()
    assert report.files_covered == 2
    assert report.files_total == 7
    assert report.score == 1.0
    assert updated.is_done(remaining_budget=10)


def test_score_never_reaches_one_while_something_is_outstanding() -> None:
    state = CoverageState(
        resolved_subjects=("a",),
        frontier=(FrontierItem("call", "b", "unknown"),),
    )
    assert state.completeness().score < 1.0
    assert not state.is_done(remaining_budget=10)


def test_exhausted_budget_ends_the_loop_even_with_frontier() -> None:
    state = CoverageState(frontier=(FrontierItem("call", "b", "unknown"),))
    assert state.is_done(remaining_budget=0)


def test_entry_like_entities_are_listed_as_entrypoints(tmp_path: Path) -> None:
    finding = Finding(
        type=EntityKind.ENTRY_POINT,
        subject="OrderController place",
        statement="OrderController place is the http entry to OrderService place",
        attributes={"mechanism": "http", "location": "OrderController.place"},
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
    )
    updated = update(CoverageState(files_total=7), verified_report([finding], tmp_path))
    assert updated.completeness().discovered_entrypoints == ("OrderController place",)


def test_contradicted_finding_is_not_counted_as_resolved(tmp_path: Path) -> None:
    ref = EvidenceRef(path=SERVICE, line_start=10, line_end=12)
    common = {
        "type": EntityKind.BUSINESS_RULE,
        "subject": "order place",
        "conditions": ("reference given",),
        "evidence": (ref,),
    }
    first = Finding(
        statement="OrderService place accepts the order", effects=("accept order",), **common
    )
    second = Finding(
        statement="OrderService place rejects the order", effects=("reject order",), **common
    )
    updated = update(
        CoverageState(files_total=7), verified_report([first, second], tmp_path)
    )
    assert Confidence.CONTRADICTED.value not in updated.resolved_subjects
    assert "order place" not in updated.resolved_subjects


def owned_rule(owner: str, relations=()) -> Finding:
    return Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="Eligibility",
        statement="place saves the reference through the repository",
        conditions=("a reference is given",),
        effects=("repository save is called with the reference",),
        owner=owner,
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
        relations=relations,
    )


def test_a_target_under_another_owner_stays_on_the_frontier(tmp_path: Path) -> None:
    claim = RelationClaim(
        kind=RelationKind.SUPERSEDES,
        target_subject="Eligibility",
        target_type=EntityKind.BUSINESS_RULE,
        target_owner="Billing",
    )
    report = verified_report([owned_rule("Ordering", (claim,))], tmp_path)
    updated = update(CoverageState(files_total=7), report)
    assert [item.subject for item in updated.frontier] == ["Eligibility"]


def test_a_target_under_the_same_owner_never_reaches_the_frontier(
    tmp_path: Path,
) -> None:
    claim = RelationClaim(
        kind=RelationKind.SUPERSEDES,
        target_subject="Eligibility",
        target_type=EntityKind.BUSINESS_RULE,
        target_owner="Ordering",
    )
    report = verified_report([owned_rule("Ordering", (claim,))], tmp_path)
    updated = update(CoverageState(files_total=7), report)
    assert not updated.frontier


def test_a_target_named_by_id_is_matched_by_the_explicit_id(tmp_path: Path) -> None:
    claim = RelationClaim(kind=RelationKind.SUPERSEDES, target_id="RULE-7")
    identified = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="Eligibility",
        statement="place saves the reference through the repository",
        conditions=("a reference is given",),
        effects=("repository save is called with the reference",),
        explicit_id="RULE-7",
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
    )
    report = verified_report(
        [identified, owned_rule("Ordering", (claim,))], tmp_path
    )
    updated = update(CoverageState(files_total=7), report)
    assert not updated.frontier


def test_two_owners_of_one_name_never_alias_into_the_global_owner(
    tmp_path: Path,
) -> None:
    report = verified_report(
        [owned_rule("Ordering"), owned_rule("Billing")], tmp_path
    )
    identities = set(entity_identities(report.verified[0]))
    assert "business_rule::ordering::eligibility" in identities
    assert f"business_rule::{GLOBAL_OWNER}::eligibility" not in identities
    assert f"::{GLOBAL_OWNER}::eligibility" not in identities


def test_an_unowned_finding_still_carries_the_global_alias(tmp_path: Path) -> None:
    unowned = Finding(
        type=EntityKind.BUSINESS_RULE,
        subject="Eligibility",
        statement="place saves the reference through the repository",
        conditions=("a reference is given",),
        effects=("repository save is called with the reference",),
        evidence=(EvidenceRef(path=SERVICE, line_start=10, line_end=12),),
    )
    report = verified_report([unowned], tmp_path)
    identities = set(entity_identities(report.verified[0]))
    assert f"business_rule::{GLOBAL_OWNER}::eligibility" in identities


def test_an_ambiguous_relation_keeps_the_frontier_open(tmp_path: Path) -> None:
    claim = RelationClaim(
        kind=RelationKind.SUPERSEDES,
        target_subject="Eligibility",
        target_type=EntityKind.BUSINESS_RULE,
        statement="Ordering Eligibility supersedes the other Eligibility",
    )
    report = verified_report(
        [owned_rule("Ordering", (claim,)), owned_rule("Billing")], tmp_path
    )
    updated = update(
        CoverageState(files_total=7),
        report,
        unresolved_relations=("Eligibility",),
    )
    completeness = updated.completeness()
    assert completeness.unresolved_relations == ("Eligibility",)
    assert "Eligibility" in completeness.outstanding
    assert completeness.score < 1.0


def test_an_ambiguous_relation_survives_a_later_round_that_names_the_subject(
    tmp_path: Path,
) -> None:
    report = verified_report([owned_rule("Ordering")], tmp_path)
    first = update(
        CoverageState(files_total=7),
        report,
        unresolved_relations=("Eligibility",),
    )
    second = update(first, report)
    assert second.completeness().unresolved_relations == ("Eligibility",)


def test_gaps_from_the_normalizer_reach_the_completeness_report(
    tmp_path: Path,
) -> None:
    report = verified_report([owned_rule("Ordering")], tmp_path)
    updated = update(
        CoverageState(files_total=7),
        report,
        gaps=("ambiguous_relation_target: relation supersedes names Eligibility",),
    )
    completeness = updated.completeness()
    assert completeness.explicit_gaps == (
        "ambiguous_relation_target: relation supersedes names Eligibility",
    )
    assert completeness.outstanding
