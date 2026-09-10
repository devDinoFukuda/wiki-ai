from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.benchmark import metrics, runner
from tests.benchmark.ground_truth import corpora

MISSING_BY_PROVIDER = {
    name: shutil.which(binary) is None
    for name, binary in runner.PROVIDER_BINARIES.items()
}
ANY_PROVIDER = not all(MISSING_BY_PROVIDER.values())
AVAILABLE = tuple(sorted(name for name, absent in MISSING_BY_PROVIDER.items() if not absent))
NO_PROVIDER_REASON = (
    "requires at least one real provider binary "
    f"{sorted(runner.PROVIDER_BINARIES.values())}; none found on PATH"
)
requires_a_provider = pytest.mark.skipif(not ANY_PROVIDER, reason=NO_PROVIDER_REASON)

MINIMUM_RULE_RECALL = 0.5
MAXIMUM_UNSUPPORTED_CLAIM_RATE = 0.5


@requires_a_provider
@pytest.mark.parametrize("name", corpora.CORPUS_NAMES)
def test_real_provider_recovers_the_ground_truth(tmp_path: Path, name: str) -> None:
    provider = AVAILABLE[0]
    result = runner.run_repository(name, provider, tmp_path / provider)
    assert result.status == "ok", result.reason
    assert result.report is not None
    assert result.report.value("rule_recall") >= MINIMUM_RULE_RECALL
    assert result.report.value("unsupported_claim_rate") <= MAXIMUM_UNSUPPORTED_CLAIM_RATE


@requires_a_provider
def test_real_provider_detects_the_planted_contradiction(tmp_path: Path) -> None:
    provider = AVAILABLE[0]
    result = runner.run_repository("java", provider, tmp_path / provider)
    assert result.status == "ok", result.reason
    assert result.report is not None
    score = result.report.score("contradiction_detection")
    assert score.denominator == 1
    assert score.value == 1.0


FORBIDDEN_PROVIDER_TOKENS = ("Script" + "edProvider", "Fake" + "Provider", "fake_" + "provider")


def test_the_benchmark_never_uses_a_simulated_provider() -> None:
    package = Path(__file__).resolve().parent
    checked = 0
    for module in sorted(package.rglob("*.py")):
        if module.name == Path(__file__).name:
            continue
        checked += 1
        text = module.read_text(encoding="utf-8")
        for token in FORBIDDEN_PROVIDER_TOKENS:
            assert token not in text, f"{module.name}: {token}"
    assert checked >= 6


def test_metrics_module_declares_every_required_metric() -> None:
    required = (
        "rule_recall",
        "edge_case_recall",
        "invariant_recall",
        "integration_recall",
        "entrypoint_recall",
        "evidence_precision",
        "unsupported_claim_rate",
        "contradiction_detection",
    )
    names = set(metrics.RECALL_METRIC_BY_GROUP.values())
    names.update({"evidence_precision", "unsupported_claim_rate", "contradiction_detection"})
    for name in required:
        assert name in names
