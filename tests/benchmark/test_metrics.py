from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from tests.benchmark import metrics
from tests.benchmark.ground_truth import corpora
from tests.benchmark.ground_truth.truth import Anchor, RepositoryTruth, TruthItem
from wiki_ai.knowledge.evidence import CodeLocator, make_evidence
from wiki_ai.knowledge.model import Confidence, Entity, KnowledgeState, SourceVersion
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind

NAMESPACE = "repo_benchmark"
VERSION_HASH = "0" * 64
CAPTURED_AT = "2026-09-10T00:00:00+00:00"

_ATTRIBUTES_BY_GROUP: Mapping[str, tuple[str, ...]] = {
    "business_rules": ("statement", "conditions", "effects"),
    "edge_cases": ("condition", "expected"),
    "invariants": ("statement",),
    "integrations": ("direction", "protocol"),
    "entrypoints": ("mechanism", "location"),
    "states": (),
    "transitions": ("from_state", "to_state"),
    "failure_modes": ("trigger", "effect"),
}

_KIND_BY_GROUP: Mapping[str, EntityKind] = {
    group: kinds[0] for group, kinds in metrics.KINDS_BY_GROUP.items()
}


@pytest.fixture(name="java_truth")
def fixture_java_truth(tmp_path: Path) -> RepositoryTruth:
    return corpora.materialize("java", tmp_path / "java").truth


def _attributes(group: str, item: TruthItem) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name in _ATTRIBUTES_BY_GROUP[group]:
        value = getattr(item, name, None)
        if value is None or (isinstance(value, (str, tuple)) and not value):
            payload[name] = item.statement
        else:
            payload[name] = list(value) if isinstance(value, tuple) else value
    payload["detail"] = item.statement + " " + " ".join(item.key_terms)
    return payload


def _entity_for(group: str, item: TruthItem, confidence: Confidence) -> Entity:
    kind = _KIND_BY_GROUP[group]
    return Entity.create(
        kind=kind.value,
        name=item.statement,
        stable_key=f"{NAMESPACE}::{kind.value}::{item.key}",
        attributes=_attributes(group, item),
        state=KnowledgeState.IMPLEMENTED,
        confidence=confidence,
    )


def _open(path: Path) -> KnowledgeRepository:
    return KnowledgeRepository.open(str(path / "state.db"))


def _source_version() -> SourceVersion:
    return SourceVersion(
        source_id=NAMESPACE,
        version_hash=VERSION_HASH,
        locator_root=NAMESPACE,
        captured_at=CAPTURED_AT,
    )


def _write(
    knowledge: KnowledgeRepository,
    entries: Sequence[tuple[str, TruthItem, Confidence, Anchor | None]],
) -> None:
    record = _source_version()
    with knowledge.begin_revision(author=NAMESPACE, summary="benchmark") as tx:
        tx.put_source_version(record)
        for group, item, confidence, anchor in entries:
            entity = _entity_for(group, item, confidence)
            entity_id = tx.put_entity(
                Entity(
                    id=entity.id,
                    kind=entity.kind,
                    name=entity.name,
                    attributes=entity.attributes,
                    state=entity.state,
                    confidence=entity.confidence,
                    source_versions=(record.key,),
                )
            )
            if anchor is None:
                continue
            locator = CodeLocator(
                path=anchor.path,
                line_start=anchor.line_start,
                line_end=anchor.line_end,
            )
            tx.put_evidence(
                make_evidence(NAMESPACE, VERSION_HASH, locator, item.statement, CAPTURED_AT),
                entity_ids=(entity_id,),
            )


def _complete(truth: RepositoryTruth) -> tuple[tuple[str, TruthItem, Confidence, Anchor], ...]:
    entries: list[tuple[str, TruthItem, Confidence, Anchor]] = []
    for group in metrics.KINDS_BY_GROUP:
        for item in getattr(truth, group):
            entries.append((group, item, Confidence.SUPPORTED, item.anchors[0]))
    return tuple(entries)


def test_recall_is_one_when_every_truth_item_is_present(
    tmp_path: Path, java_truth: RepositoryTruth
) -> None:
    with _open(tmp_path) as knowledge:
        _write(knowledge, _complete(java_truth))
        report = metrics.evaluate(knowledge, java_truth)
    assert report.value("rule_recall") == 1.0
    assert report.value("edge_case_recall") == 1.0
    assert report.value("invariant_recall") == 1.0
    assert report.value("integration_recall") == 1.0
    assert report.value("entrypoint_recall") == 1.0
    assert report.value("failure_mode_recall") == 1.0
    assert all(entry.verdict is metrics.ItemVerdict.FOUND for entry in report.items)


def test_recall_is_zero_on_empty_knowledge(
    tmp_path: Path, java_truth: RepositoryTruth
) -> None:
    with _open(tmp_path) as knowledge:
        report = metrics.evaluate(knowledge, java_truth)
    assert report.value("rule_recall") == 0.0
    assert report.value("edge_case_recall") == 0.0
    assert report.value("invariant_recall") == 0.0
    assert report.value("integration_recall") == 0.0
    assert report.value("entrypoint_recall") == 0.0
    assert report.entity_total == 0
    assert all(entry.verdict is metrics.ItemVerdict.MISSED for entry in report.items)


def test_evidence_precision_penalises_a_wrong_line_range(
    tmp_path: Path, java_truth: RepositoryTruth
) -> None:
    rule = java_truth.business_rules[0]
    anchor = rule.anchors[0]
    displaced = Anchor(
        path=anchor.path,
        line_start=anchor.line_end + 40,
        line_end=anchor.line_end + 45,
    )
    with _open(tmp_path) as knowledge:
        _write(knowledge, (("business_rules", rule, Confidence.SUPPORTED, displaced),))
        report = metrics.evaluate(knowledge, java_truth)
    assert report.value("evidence_precision") == 0.0
    verdicts = {entry.key: entry.verdict for entry in report.items}
    assert verdicts[rule.key] is metrics.ItemVerdict.WRONG_EVIDENCE
    assert report.value("rule_recall") == 0.0


def test_evidence_precision_accepts_a_range_inside_the_truth_span(
    tmp_path: Path, java_truth: RepositoryTruth
) -> None:
    rule = java_truth.business_rules[0]
    anchor = rule.anchors[0]
    with _open(tmp_path) as knowledge:
        _write(knowledge, (("business_rules", rule, Confidence.SUPPORTED, anchor),))
        report = metrics.evaluate(knowledge, java_truth)
    assert report.value("evidence_precision") == 1.0
    verdicts = {entry.key: entry.verdict for entry in report.items}
    assert verdicts[rule.key] is metrics.ItemVerdict.FOUND


def test_unsupported_claim_rate_detects_a_fabricated_claim(
    tmp_path: Path, java_truth: RepositoryTruth
) -> None:
    real = java_truth.business_rules[0]
    invented = type(real)(
        key="fabricated",
        statement="renewals are approved automatically for accounts flagged by the fraud engine",
        key_terms=("fraud", "engine", "automatically"),
        conditions=("account flagged by fraud engine",),
        effects=("renewal approved without policy evaluation",),
        anchors=real.anchors,
    )
    with _open(tmp_path) as knowledge:
        _write(
            knowledge,
            (
                ("business_rules", real, Confidence.SUPPORTED, real.anchors[0]),
                ("business_rules", invented, Confidence.SUPPORTED, real.anchors[0]),
            ),
        )
        report = metrics.evaluate(knowledge, java_truth)
    score = report.score("unsupported_claim_rate")
    assert score.numerator == 1
    assert score.denominator == 2
    assert score.value == pytest.approx(0.5)


def test_unsupported_claim_rate_is_zero_when_every_claim_maps_to_truth(
    tmp_path: Path, java_truth: RepositoryTruth
) -> None:
    with _open(tmp_path) as knowledge:
        _write(knowledge, _complete(java_truth))
        report = metrics.evaluate(knowledge, java_truth)
    assert report.value("unsupported_claim_rate") == 0.0


def test_inferred_entities_do_not_count_as_claims(
    tmp_path: Path, java_truth: RepositoryTruth
) -> None:
    rule = java_truth.business_rules[0]
    with _open(tmp_path) as knowledge:
        _write(knowledge, (("business_rules", rule, Confidence.INFERRED, rule.anchors[0]),))
        report = metrics.evaluate(knowledge, java_truth)
    assert report.score("unsupported_claim_rate").denominator == 0
    assert report.score("evidence_precision").denominator == 0


def test_contradiction_detection_reports_zero_when_nothing_is_flagged(
    tmp_path: Path, java_truth: RepositoryTruth
) -> None:
    with _open(tmp_path) as knowledge:
        _write(knowledge, _complete(java_truth))
        report = metrics.evaluate(knowledge, java_truth)
    score = report.score("contradiction_detection")
    assert score.denominator == len(java_truth.contradictions)
    assert score.value == 0.0


def test_contradiction_detection_finds_a_contradicted_entity(
    tmp_path: Path, java_truth: RepositoryTruth
) -> None:
    planted = java_truth.contradictions[0]
    rule = java_truth.business_rules[1]
    contradicted = type(rule)(
        key=rule.key,
        statement="the renewal window is 45 days according to the policy document and 30 days in code",
        key_terms=planted.key_terms,
        conditions=rule.conditions,
        effects=rule.effects,
        anchors=planted.code_anchors,
    )
    with _open(tmp_path) as knowledge:
        _write(
            knowledge,
            (
                (
                    "business_rules",
                    contradicted,
                    Confidence.CONTRADICTED,
                    planted.code_anchors[0],
                ),
            ),
        )
        report = metrics.evaluate(knowledge, java_truth)
    assert report.value("contradiction_detection") == 1.0


def test_normalisation_splits_camel_case_and_drops_stopwords() -> None:
    assert "outstanding" in metrics.tokens("contract.outstandingDebt()")
    assert "debt" in metrics.tokens("contract.outstandingDebt()")
    assert "the" not in metrics.tokens("the contract")


def test_key_terms_require_every_word_of_the_term() -> None:
    assert metrics.key_terms_present(("EXEC SQL",), "the program runs EXEC SQL UPDATE") == (
        "EXEC SQL",
    )
    assert metrics.key_terms_present(("EXEC SQL",), "the program runs EXEC only") == ()


def test_partial_recall_counts_only_matched_items(
    tmp_path: Path, java_truth: RepositoryTruth
) -> None:
    rules = java_truth.business_rules
    with _open(tmp_path) as knowledge:
        _write(
            knowledge,
            tuple(
                ("business_rules", rule, Confidence.SUPPORTED, rule.anchors[0])
                for rule in rules[:1]
            ),
        )
        report = metrics.evaluate(knowledge, java_truth)
    score = report.score("rule_recall")
    assert score.numerator == 1
    assert score.denominator == len(rules)
