from __future__ import annotations

import pytest

from wiki_ai.knowledge.identity import contextual_key
from wiki_ai.knowledge.model import Confidence
from wiki_ai.knowledge.taxonomy import ENTITY_KIND_VALUES, EntityKind, RelationKind
from wiki_ai.investigation.finding import (
    EvidenceRef,
    Finding,
    MalformedEvidenceRef,
    MissingFindingAttribute,
    RelationClaim,
    UnknownFindingType,
    finding_schema,
    parse_finding,
    parse_findings,
    with_owner,
)


def rule_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "business_rule",
        "subject": "renewal eligibility",
        "statement": "a contract renews when the term ends and the balance is zero",
        "conditions": ["term ended", "balance is zero"],
        "effects": ["contract renews"],
        "evidence": [{"capture_id": "cap_1"}],
        "confidence": "supported",
    }
    payload.update(overrides)
    return payload


def test_finding_parses_the_open_schema_of_section_8_4() -> None:
    parsed = parse_finding(rule_payload())
    assert parsed.type is EntityKind.BUSINESS_RULE
    assert parsed.confidence is Confidence.SUPPORTED
    assert parsed.evidence[0].capture_id == "cap_1"
    assert parsed.stable_key("acme") == contextual_key(
        "acme", "business_rule", None, "renewal eligibility"
    )


def test_unknown_type_is_rejected_with_a_typed_error() -> None:
    with pytest.raises(UnknownFindingType):
        parse_finding(rule_payload(type="renewal_thing"))


def test_missing_required_attribute_is_rejected_with_a_typed_error() -> None:
    with pytest.raises(MissingFindingAttribute):
        parse_finding({"type": "entry_point", "subject": "POST /orders"})


def test_required_attributes_come_from_the_taxonomy() -> None:
    parsed = parse_finding(
        {
            "type": "entry_point",
            "subject": "POST /orders",
            "attributes": {"mechanism": "http", "location": "OrderController.place"},
        }
    )
    assert parsed.attributes["mechanism"] == "http"


def test_evidence_ref_accepts_a_path_range() -> None:
    ref = EvidenceRef(path="a/b.java", line_start=10, line_end=12, symbol="place")
    assert ref.key == "a/b.java:10-12#place"


def test_evidence_ref_without_capture_or_path_is_rejected() -> None:
    with pytest.raises(MalformedEvidenceRef):
        EvidenceRef()


def test_evidence_ref_with_broken_range_is_rejected() -> None:
    with pytest.raises(MalformedEvidenceRef):
        EvidenceRef(path="a/b.java", line_start=5, line_end=2)


def test_relation_claims_are_typed_against_the_taxonomy() -> None:
    parsed = parse_finding(
        rule_payload(
            relations=[{"kind": "validates", "target_subject": "order request"}]
        )
    )
    assert parsed.relations[0].kind is RelationKind.VALIDATES


def test_relation_claim_needs_a_target() -> None:
    with pytest.raises(Exception):
        RelationClaim(kind=RelationKind.CALLS, target_subject="  ")


def test_parse_findings_separates_accepted_from_rejected() -> None:
    accepted, rejected = parse_findings(
        [rule_payload(), {"type": "nonsense", "subject": "x"}]
    )
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert "nonsense" in rejected[0].reason


def test_schema_lists_every_taxonomy_kind_and_is_not_a_fixed_form() -> None:
    schema = finding_schema()
    listed = {entry["type"] for entry in schema["types"]}
    assert listed == ENTITY_KIND_VALUES
    assert set(schema["properties"]["type"]["enum"]) == ENTITY_KIND_VALUES
    assert len(schema["properties"]) != 13


def test_schema_carries_required_attributes_per_type() -> None:
    schema = finding_schema()
    by_type = {entry["type"]: entry["required_attributes"] for entry in schema["types"]}
    assert by_type["business_rule"] == ["statement", "conditions", "effects"]
    assert by_type["integration"] == ["direction", "protocol"]


def test_finding_direct_construction_promotes_statement_into_attributes() -> None:
    parsed = Finding(
        type=EntityKind.INVARIANT,
        subject="totals never negative",
        statement="total is clamped at zero",
    )
    assert parsed.attributes["statement"] == "total is clamped at zero"


def test_with_owner_fills_only_an_absent_owner() -> None:
    bare = parse_finding(rule_payload())
    owned = with_owner(bare, "Ordering")
    assert owned.owner == "Ordering"
    assert with_owner(owned, "Billing").owner == "Ordering"
    assert with_owner(bare, "   ") is bare


def test_owner_changes_the_stable_key_but_explicit_id_wins() -> None:
    bare = parse_finding(rule_payload())
    owned = with_owner(bare, "Ordering")
    assert bare.stable_key("acme") != owned.stable_key("acme")
    explicit = parse_finding(rule_payload(explicit_id="RULE-7"))
    assert explicit.stable_key("acme") == with_owner(explicit, "Ordering").stable_key(
        "acme"
    )


def test_schema_exposes_owner_and_explicit_id() -> None:
    properties = finding_schema()["properties"]
    assert "owner" in properties
    assert "explicit_id" in properties


def relation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "calls",
        "target_subject": "Eligibility",
        "target_type": "business_rule",
        "target_owner": "Billing",
        "target_id": "RULE-7",
        "evidence": [
            {
                "capture_id": "",
                "path": "src/Order.java",
                "line_start": 3,
                "line_end": 5,
                "symbol": "",
            }
        ],
        "attributes": {"note": "wired in the constructor"},
    }
    payload.update(overrides)
    return payload


def test_relation_claim_carries_owner_id_and_its_own_evidence() -> None:
    claim = RelationClaim.from_dict(relation_payload())
    assert claim.target_owner == "Billing"
    assert claim.target_id == "RULE-7"
    assert claim.evidence[0].path == "src/Order.java"
    assert claim.to_dict() == relation_payload()


def test_a_relation_claim_may_be_identified_only_by_target_id() -> None:
    claim = RelationClaim.from_dict(
        {"kind": "calls", "target_id": "RULE-7"}
    )
    assert claim.target_subject == ""
    assert claim.target_id == "RULE-7"
    assert claim.label == "id:RULE-7"


def test_a_relation_claim_without_subject_and_without_id_is_rejected() -> None:
    with pytest.raises(Exception):
        RelationClaim(kind=RelationKind.CALLS, target_subject="  ")


def test_the_schema_offered_to_the_provider_exposes_the_relation_fields() -> None:
    relations = finding_schema()["properties"]["relations"]["items"]
    assert set(relations["properties"]) == {
        "kind",
        "target_subject",
        "target_type",
        "target_owner",
        "target_id",
        "evidence",
        "attributes",
    }
    assert relations["required"] == ["kind"]
    assert {"required": ["target_subject"]} in relations["anyOf"]
    assert {"required": ["target_id"]} in relations["anyOf"]
    assert relations["properties"]["evidence"]["items"]["properties"]["capture_id"]


def test_a_parsed_finding_keeps_the_relation_owner_id_and_evidence() -> None:
    parsed = parse_finding(rule_payload(relations=[relation_payload()]))
    claim = parsed.relations[0]
    assert claim.target_owner == "Billing"
    assert claim.target_id == "RULE-7"
    assert claim.evidence[0].line_start == 3
    assert parsed.to_dict()["relations"][0] == relation_payload()
