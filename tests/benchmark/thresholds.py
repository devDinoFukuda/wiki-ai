from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "BOOTSTRAP",
    "TARGET",
    "LEVELS",
    "DEFAULT_LEVEL",
    "METRIC_NAMES",
    "LOWER_IS_BETTER",
    "Thresholds",
    "THRESHOLDS",
    "evaluate_metrics",
]

BOOTSTRAP = "bootstrap"
TARGET = "target"
LEVELS: tuple[str, ...] = (BOOTSTRAP, TARGET)
DEFAULT_LEVEL = BOOTSTRAP

METRIC_NAMES: tuple[str, ...] = (
    "rule_recall",
    "edge_case_recall",
    "invariant_recall",
    "integration_recall",
    "entrypoint_recall",
    "evidence_precision",
    "unsupported_claim_rate",
    "contradiction_detection",
)

LOWER_IS_BETTER: frozenset[str] = frozenset({"unsupported_claim_rate"})


@dataclass(frozen=True)
class Thresholds:
    level: str
    rule_recall: float
    edge_case_recall: float
    invariant_recall: float
    integration_recall: float
    entrypoint_recall: float
    evidence_precision: float
    unsupported_claim_rate: float
    contradiction_detection: float

    def threshold(self, name: str) -> float:
        if name not in METRIC_NAMES:
            raise KeyError(name)
        return float(getattr(self, name))

    def passed(self, name: str, value: float) -> bool:
        limit = self.threshold(name)
        if name in LOWER_IS_BETTER:
            return value <= limit
        return value >= limit

    def stricter_than(self, other: "Thresholds") -> bool:
        for name in METRIC_NAMES:
            mine = self.threshold(name)
            theirs = other.threshold(name)
            if name in LOWER_IS_BETTER:
                if mine >= theirs:
                    return False
            elif mine <= theirs:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"level": self.level}
        payload.update({name: self.threshold(name) for name in METRIC_NAMES})
        return payload


THRESHOLDS: Mapping[str, Thresholds] = {
    BOOTSTRAP: Thresholds(
        level=BOOTSTRAP,
        rule_recall=0.5,
        edge_case_recall=0.3,
        invariant_recall=0.3,
        integration_recall=0.4,
        entrypoint_recall=0.4,
        evidence_precision=0.6,
        unsupported_claim_rate=0.5,
        contradiction_detection=0.5,
    ),
    TARGET: Thresholds(
        level=TARGET,
        rule_recall=0.85,
        edge_case_recall=0.7,
        invariant_recall=0.7,
        integration_recall=0.8,
        entrypoint_recall=0.8,
        evidence_precision=0.95,
        unsupported_claim_rate=0.05,
        contradiction_detection=1.0,
    ),
}


def evaluate_metrics(
    scores: Mapping[str, Any], level: str = DEFAULT_LEVEL
) -> dict[str, dict[str, Any]]:
    limits = THRESHOLDS[level]
    evaluated: dict[str, dict[str, Any]] = {}
    for name in METRIC_NAMES:
        entry = scores.get(name)
        if not isinstance(entry, Mapping):
            continue
        value = float(entry.get("value", 0.0))
        evaluated[name] = {
            "value": value,
            "threshold": limits.threshold(name),
            "passed": limits.passed(name, value),
        }
    return evaluated
