from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from tests.benchmark.ground_truth.truth import (
    Anchor,
    ContradictionTruth,
    RepositoryTruth,
    TruthItem,
)
from wiki_ai.knowledge.evidence import CodeLocator
from wiki_ai.knowledge.model import Confidence, Entity, Evidence
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind

__all__ = [
    "ItemVerdict",
    "ItemReport",
    "MetricScore",
    "BenchmarkReport",
    "STOPWORDS",
    "MATCH_TERM_RATIO",
    "KINDS_BY_GROUP",
    "RECALL_METRIC_BY_GROUP",
    "normalize",
    "tokens",
    "key_terms_present",
    "statement_matches",
    "evaluate",
]

STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "then",
        "this",
        "to",
        "when",
        "with",
        "without",
    }
)

MATCH_TERM_RATIO = 0.5

_SPLIT = re.compile(r"[^0-9a-z]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class ItemVerdict(str, Enum):
    FOUND = "found"
    MISSED = "missed"
    WRONG_EVIDENCE = "wrong_evidence"


KINDS_BY_GROUP: Mapping[str, tuple[EntityKind, ...]] = {
    "business_rules": (EntityKind.BUSINESS_RULE,),
    "edge_cases": (EntityKind.EDGE_CASE,),
    "invariants": (EntityKind.INVARIANT, EntityKind.PRECONDITION, EntityKind.POSTCONDITION),
    "integrations": (
        EntityKind.INTEGRATION,
        EntityKind.TOPIC,
        EntityKind.QUEUE,
        EntityKind.EVENT,
        EntityKind.PERSISTENCE,
        EntityKind.TABLE,
        EntityKind.ENDPOINT,
        EntityKind.DEPENDENCY,
    ),
    "entrypoints": (EntityKind.ENTRY_POINT,),
    "states": (EntityKind.STATE,),
    "transitions": (EntityKind.STATE_TRANSITION,),
    "failure_modes": (
        EntityKind.FAILURE_MODE,
        EntityKind.RETRY_POLICY,
        EntityKind.FALLBACK,
        EntityKind.TIMEOUT_POLICY,
        EntityKind.IDEMPOTENCY_POLICY,
    ),
}

RECALL_METRIC_BY_GROUP: Mapping[str, str] = {
    "business_rules": "rule_recall",
    "edge_cases": "edge_case_recall",
    "invariants": "invariant_recall",
    "integrations": "integration_recall",
    "entrypoints": "entrypoint_recall",
    "states": "state_recall",
    "transitions": "transition_recall",
    "failure_modes": "failure_mode_recall",
}


def normalize(text: str) -> str:
    expanded = _CAMEL.sub(" ", str(text))
    return _SPLIT.sub(" ", expanded.lower()).strip()


def tokens(text: str) -> frozenset[str]:
    return frozenset(
        word for word in normalize(text).split() if word and word not in STOPWORDS
    )


def _term_tokens(term: str) -> frozenset[str]:
    return frozenset(word for word in normalize(term).split() if word)


def key_terms_present(terms: Sequence[str], haystack: str) -> tuple[str, ...]:
    available = frozenset(normalize(haystack).split())
    found: list[str] = []
    for term in terms:
        wanted = _term_tokens(term)
        if wanted and wanted <= available:
            found.append(term)
    return tuple(found)


def _entity_text(entity: Entity) -> str:
    parts: list[str] = [entity.name]
    for value in entity.attributes.values():
        parts.append(_flatten(value))
    return " ".join(parts)


def _flatten(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(_flatten(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(_flatten(item) for item in value)
    return str(value)


def statement_matches(item: TruthItem, entity: Entity) -> bool:
    haystack = _entity_text(entity)
    present = key_terms_present(item.key_terms, haystack)
    if len(present) * 2 < len(item.key_terms):
        return False
    statement_words = tokens(item.statement)
    shared = statement_words & tokens(haystack)
    if not statement_words:
        return bool(present)
    return len(shared) >= max(1, int(len(statement_words) * MATCH_TERM_RATIO))


def _code_locators(evidence: Iterable[Evidence]) -> tuple[CodeLocator, ...]:
    return tuple(item.locator for item in evidence if isinstance(item.locator, CodeLocator))


def _anchor_hit(anchors: Sequence[Anchor], locator: CodeLocator) -> bool:
    for anchor in anchors:
        if _same_path(anchor.path, locator.path) and anchor.contains(
            locator.line_start, locator.line_end
        ):
            return True
    return False


def _same_path(expected: str, produced: str) -> bool:
    left = expected.replace("\\", "/").lstrip("./")
    right = str(produced).replace("\\", "/").lstrip("./")
    return left == right or right.endswith("/" + left) or left.endswith("/" + right)


@dataclass(frozen=True)
class ItemReport:
    group: str
    key: str
    verdict: ItemVerdict
    entity_id: str = ""
    confidence: str = ""
    evidence_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "key": self.key,
            "verdict": self.verdict.value,
            "entity_id": self.entity_id,
            "confidence": self.confidence,
            "evidence_paths": list(self.evidence_paths),
        }


@dataclass(frozen=True)
class MetricScore:
    name: str
    value: float
    numerator: int
    denominator: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 4),
            "numerator": self.numerator,
            "denominator": self.denominator,
        }


@dataclass(frozen=True)
class BenchmarkReport:
    repository: str
    scores: tuple[MetricScore, ...]
    items: tuple[ItemReport, ...]
    entity_total: int
    supported_total: int

    def score(self, name: str) -> MetricScore:
        for entry in self.scores:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def value(self, name: str) -> float:
        return self.score(name).value

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "entities": self.entity_total,
            "supported_entities": self.supported_total,
            "metrics": {entry.name: entry.to_dict() for entry in self.scores},
            "items": [entry.to_dict() for entry in self.items],
        }


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


@dataclass(frozen=True)
class _Candidate:
    entity: Entity
    evidence: tuple[Evidence, ...]

    def locators(self) -> tuple[CodeLocator, ...]:
        return _code_locators(self.evidence)

    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({locator.path for locator in self.locators()}))


def _load(knowledge: KnowledgeRepository) -> tuple[_Candidate, ...]:
    collected: list[_Candidate] = []
    for kind in EntityKind:
        for entity in knowledge.find_entities(kind.value):
            collected.append(
                _Candidate(entity=entity, evidence=tuple(knowledge.evidence_for(entity.id)))
            )
    return tuple(collected)


def _group_items(truth: RepositoryTruth, group: str) -> tuple[TruthItem, ...]:
    return tuple(getattr(truth, group))


def _match(
    item: TruthItem, group: str, candidates: Sequence[_Candidate]
) -> ItemReport:
    kinds = frozenset(kind.value for kind in KINDS_BY_GROUP[group])
    named: list[_Candidate] = []
    for candidate in candidates:
        if candidate.entity.kind not in kinds:
            continue
        if statement_matches(item, candidate.entity):
            named.append(candidate)
    if not named:
        return ItemReport(group=group, key=item.key, verdict=ItemVerdict.MISSED)
    for candidate in named:
        locators = candidate.locators()
        if locators and any(_anchor_hit(item.anchors, locator) for locator in locators):
            return ItemReport(
                group=group,
                key=item.key,
                verdict=ItemVerdict.FOUND,
                entity_id=candidate.entity.id.value,
                confidence=candidate.entity.confidence.value,
                evidence_paths=candidate.paths(),
            )
    head = named[0]
    return ItemReport(
        group=group,
        key=item.key,
        verdict=ItemVerdict.WRONG_EVIDENCE,
        entity_id=head.entity.id.value,
        confidence=head.entity.confidence.value,
        evidence_paths=head.paths(),
    )


def _evidence_precision(
    truth: RepositoryTruth, candidates: Sequence[_Candidate]
) -> MetricScore:
    anchors_by_key = {item.key: item.anchors for item in truth.all_items()}
    matched = 0
    total = 0
    for candidate in candidates:
        if candidate.entity.confidence is not Confidence.SUPPORTED:
            continue
        locators = candidate.locators()
        if not locators:
            continue
        item = _truth_for(truth, candidate)
        if item is None:
            continue
        total += 1
        anchors = anchors_by_key[item.key]
        if any(_anchor_hit(anchors, locator) for locator in locators):
            matched += 1
    return MetricScore("evidence_precision", _ratio(matched, total), matched, total)


def _truth_for(truth: RepositoryTruth, candidate: _Candidate) -> TruthItem | None:
    for group, kinds in KINDS_BY_GROUP.items():
        if candidate.entity.kind not in frozenset(kind.value for kind in kinds):
            continue
        for item in _group_items(truth, group):
            if statement_matches(item, candidate.entity):
                return item
    return None


def _unsupported_claim_rate(
    truth: RepositoryTruth, candidates: Sequence[_Candidate]
) -> MetricScore:
    claimed = 0
    unmatched = 0
    judged_kinds = frozenset(
        kind.value for kinds in KINDS_BY_GROUP.values() for kind in kinds
    )
    for candidate in candidates:
        if candidate.entity.confidence is not Confidence.SUPPORTED:
            continue
        if candidate.entity.kind not in judged_kinds:
            continue
        claimed += 1
        if _truth_for(truth, candidate) is None:
            unmatched += 1
    return MetricScore(
        "unsupported_claim_rate", _ratio(unmatched, claimed), unmatched, claimed
    )


def _contradiction_detection(
    truth: RepositoryTruth, knowledge: KnowledgeRepository, candidates: Sequence[_Candidate]
) -> MetricScore:
    planted = truth.contradictions
    detected = sum(
        1 for entry in planted if _contradiction_found(entry, knowledge, candidates)
    )
    return MetricScore(
        "contradiction_detection", _ratio(detected, len(planted)), detected, len(planted)
    )


def _contradiction_found(
    entry: ContradictionTruth,
    knowledge: KnowledgeRepository,
    candidates: Sequence[_Candidate],
) -> bool:
    for relation in knowledge.find_relations("contradicts"):
        if _touches_contradiction(entry, relation.source_id.value, relation.target_id.value, candidates):
            return True
    for candidate in candidates:
        if candidate.entity.confidence is not Confidence.CONTRADICTED:
            continue
        if key_terms_present(entry.key_terms, _entity_text(candidate.entity)):
            return True
    return False


def _touches_contradiction(
    entry: ContradictionTruth,
    source_id: str,
    target_id: str,
    candidates: Sequence[_Candidate],
) -> bool:
    involved = {source_id, target_id}
    for candidate in candidates:
        if candidate.entity.id.value not in involved:
            continue
        text = _entity_text(candidate.entity)
        if key_terms_present(entry.key_terms, text):
            return True
        for locator in candidate.locators():
            if _anchor_hit(entry.code_anchors, locator):
                return True
    return False


def evaluate(knowledge: KnowledgeRepository, truth: RepositoryTruth) -> BenchmarkReport:
    candidates = _load(knowledge)
    reports: list[ItemReport] = []
    scores: list[MetricScore] = []
    for group, metric_name in RECALL_METRIC_BY_GROUP.items():
        items = _group_items(truth, group)
        group_reports = tuple(_match(item, group, candidates) for item in items)
        reports.extend(group_reports)
        found = sum(1 for entry in group_reports if entry.verdict is ItemVerdict.FOUND)
        scores.append(MetricScore(metric_name, _ratio(found, len(items)), found, len(items)))
    scores.append(_evidence_precision(truth, candidates))
    scores.append(_unsupported_claim_rate(truth, candidates))
    scores.append(_contradiction_detection(truth, knowledge, candidates))
    supported = sum(
        1
        for candidate in candidates
        if candidate.entity.confidence is Confidence.SUPPORTED
    )
    return BenchmarkReport(
        repository=truth.name,
        scores=tuple(scores),
        items=tuple(reports),
        entity_total=len(candidates),
        supported_total=supported,
    )
