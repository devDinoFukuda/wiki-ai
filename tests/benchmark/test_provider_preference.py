from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from tests.benchmark import conformance, metrics, runner, thresholds

REQUESTED = "claude"
RESOLVED = "codex"


class _StubReport:
    def __init__(self, status: str, provider: str) -> None:
        self._status = status
        self._provider = provider

    @property
    def status(self) -> str:
        return self._status

    @property
    def reason(self) -> str:
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {"status": self._status, "provider": self._provider}


class _Corpus:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.documents: tuple[Path, ...] = ()
        self.truth = None


def _report() -> metrics.BenchmarkReport:
    return metrics.BenchmarkReport(
        repository="java",
        scores=(
            metrics.MetricScore("rule_recall", 0.9, 9, 10),
            metrics.MetricScore("unsupported_claim_rate", 0.1, 1, 10),
        ),
        items=(),
        entity_total=10,
        supported_total=9,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, resolved: str
) -> None:
    monkeypatch.setattr(runner, "_registry", lambda provider: object())
    monkeypatch.setattr(runner, "Wiring", lambda registry, provider=None: object())
    monkeypatch.setattr(runner, "_prepare", lambda name, workspace: _Corpus(tmp_path))
    monkeypatch.setattr(
        runner.api, "analyze", lambda *args, **kwargs: _StubReport("ok", resolved)
    )
    monkeypatch.setattr(
        runner.api, "ask", lambda *args, **kwargs: _StubReport("ok", resolved)
    )
    monkeypatch.setattr(
        runner.api, "publish", lambda *args, **kwargs: _StubReport("ok", resolved)
    )
    monkeypatch.setattr(runner.metrics, "evaluate", lambda knowledge, truth: _report())

    class _Session:
        @staticmethod
        def open(root: Path) -> "_Session":
            return _Session()

        def open_knowledge(self) -> Any:
            return _Knowledge()

    class _Knowledge:
        def __enter__(self) -> "_Knowledge":
            return self

        def __exit__(self, *exception: Any) -> bool:
            return False

    monkeypatch.setattr(runner, "Session", _Session)


def test_stage_providers_reads_every_reported_name() -> None:
    stages: Mapping[str, Any] = {
        "analyze": {"provider": REQUESTED},
        "ingest": [{"provider": REQUESTED}, {"provider": RESOLVED}],
        "publish": {"status": "ok"},
    }
    assert runner.stage_providers(stages) == (REQUESTED, REQUESTED, RESOLVED)


def test_a_divergent_provider_marks_the_run_as_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, RESOLVED)
    result = runner.run_repository("java", REQUESTED, tmp_path)
    assert result.status == runner.STATUS_PROVIDER_MISMATCH
    assert result.reason == runner.PROVIDER_MISMATCH_REASON
    assert result.actual_provider == RESOLVED
    assert not result.provider_matches()
    assert result.report is None


def test_the_mismatch_report_names_both_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, RESOLVED)
    payload = runner.run_repository("java", REQUESTED, tmp_path).to_dict()
    assert payload["requested_provider"] == REQUESTED
    assert payload["actual_provider"] == RESOLVED
    assert payload["status"] == runner.STATUS_PROVIDER_MISMATCH
    assert "metrics" not in payload


def test_a_matching_provider_produces_an_ok_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, REQUESTED)
    result = runner.run_repository("java", REQUESTED, tmp_path)
    assert result.status == "ok"
    assert result.actual_provider == REQUESTED
    assert result.provider_matches()
    assert result.report is not None


def test_the_preference_reaches_the_wiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[str | None] = []

    def _record(registry: Any, provider: str | None = None) -> object:
        seen.append(provider)
        return object()

    _install(monkeypatch, tmp_path, REQUESTED)
    monkeypatch.setattr(runner, "Wiring", _record)
    runner.run_repository("java", REQUESTED, tmp_path)
    assert seen == [REQUESTED]


def test_conformance_refuses_to_compare_a_mismatched_pair() -> None:
    matched = runner.RunResult(
        provider=REQUESTED,
        repository="java",
        status="ok",
        actual_provider=REQUESTED,
    )
    diverged = runner.RunResult(
        provider=RESOLVED,
        repository="java",
        status=runner.STATUS_PROVIDER_MISMATCH,
        actual_provider=REQUESTED,
    )
    assert conformance.comparable(matched, matched)
    assert not conformance.comparable(matched, diverged)


def test_conformance_run_reports_mismatch_instead_of_a_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "missing_binaries", lambda providers: ())

    def _diverged(repository: str, provider: str, workspace: Path) -> runner.RunResult:
        return runner.RunResult(
            provider=provider,
            repository=repository,
            status=runner.STATUS_PROVIDER_MISMATCH,
            reason=runner.PROVIDER_MISMATCH_REASON,
            actual_provider=REQUESTED,
        )

    monkeypatch.setattr(runner, "run_repository", _diverged)
    payload = conformance.run("java", tmp_path)
    assert payload["status"] == conformance.STATUS_PROVIDER_MISMATCH
    assert payload["reason"] == conformance.MISMATCH_REASON
    assert payload["mismatched"][0]["repository"] == "java"
    entry = payload["comparisons"][0]
    assert entry["comparable"] is False
    assert "knowledge_diff" not in entry
    assert "metric_diff" not in entry


def test_conformance_main_exits_with_its_own_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "missing_binaries", lambda providers: ())
    monkeypatch.setattr(
        runner,
        "run_repository",
        lambda repository, provider, workspace: runner.RunResult(
            provider=provider,
            repository=repository,
            status=runner.STATUS_PROVIDER_MISMATCH,
            reason=runner.PROVIDER_MISMATCH_REASON,
            actual_provider=REQUESTED,
        ),
    )
    stream = io.StringIO()
    target = tmp_path / "conformance.json"
    code = conformance.main(
        ["--repo", "java", "--workspace", str(tmp_path), "--out", str(target)], stream
    )
    assert code == conformance.EXIT_PROVIDER_MISMATCH
    assert code not in {runner.EXIT_OK, runner.EXIT_SKIPPED}
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["status"] == conformance.STATUS_PROVIDER_MISMATCH
    assert conformance.MISMATCH_REASON in stream.getvalue()


def test_conformance_compares_when_both_runs_honour_the_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "missing_binaries", lambda providers: ())

    def _matched(repository: str, provider: str, workspace: Path) -> runner.RunResult:
        return runner.RunResult(
            provider=provider,
            repository=repository,
            status="ok",
            report=_report(),
            actual_provider=provider,
        )

    shapes = {
        name: conformance.KnowledgeShape(
            provider=name,
            entities_by_kind={"business_rule": 1},
            relations_by_kind={},
            evidence_paths=("a/One.java",),
            entity_names_by_kind={"business_rule": ("rule one",)},
        )
        for name in conformance.PAIR
    }
    monkeypatch.setattr(runner, "run_repository", _matched)
    monkeypatch.setattr(
        conformance,
        "_run_one",
        lambda repository, provider, workspace: (
            _matched(repository, provider, workspace),
            shapes[provider],
        ),
    )
    payload = conformance.run("java", tmp_path)
    assert payload["status"] == "ok"
    assert payload["comparisons"][0]["comparable"] is True
    assert payload["comparisons"][0]["knowledge_diff"]["identical"] is True
    assert "metric_diff" in payload["comparisons"][0]


def test_target_is_strictly_harder_than_bootstrap_on_every_metric() -> None:
    bootstrap = thresholds.THRESHOLDS[thresholds.BOOTSTRAP]
    target = thresholds.THRESHOLDS[thresholds.TARGET]
    assert target.stricter_than(bootstrap)
    assert not bootstrap.stricter_than(target)
    for name in thresholds.METRIC_NAMES:
        low = bootstrap.threshold(name)
        high = target.threshold(name)
        if name in thresholds.LOWER_IS_BETTER:
            assert high < low, name
        else:
            assert high > low, name


def test_every_metric_of_the_runner_table_has_a_threshold() -> None:
    assert set(runner._COLUMNS) == set(thresholds.METRIC_NAMES)


def test_the_contract_values_are_pinned() -> None:
    target = thresholds.THRESHOLDS[thresholds.TARGET]
    assert target.rule_recall == 0.85
    assert target.unsupported_claim_rate == 0.05
    assert target.edge_case_recall == 0.7
    assert target.invariant_recall == 0.7
    assert target.integration_recall == 0.8
    assert target.evidence_precision == 0.95
    bootstrap = thresholds.THRESHOLDS[thresholds.BOOTSTRAP]
    assert bootstrap.rule_recall == 0.5
    assert bootstrap.unsupported_claim_rate == 0.5
    assert thresholds.DEFAULT_LEVEL == thresholds.BOOTSTRAP


def test_lower_is_better_metrics_pass_below_their_ceiling() -> None:
    target = thresholds.THRESHOLDS[thresholds.TARGET]
    assert target.passed("unsupported_claim_rate", 0.04)
    assert not target.passed("unsupported_claim_rate", 0.06)
    assert target.passed("rule_recall", 0.9)
    assert not target.passed("rule_recall", 0.5)


def test_the_report_carries_level_threshold_and_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, REQUESTED)
    payload = runner.run_repository(
        "java", REQUESTED, tmp_path, thresholds.TARGET
    ).to_dict()
    assert payload["level"] == thresholds.TARGET
    rule = payload["metrics"]["rule_recall"]
    assert rule == {"value": 0.9, "threshold": 0.85, "passed": True}
    unsupported = payload["metrics"]["unsupported_claim_rate"]
    assert unsupported == {"value": 0.1, "threshold": 0.05, "passed": False}


def test_the_same_numbers_pass_at_bootstrap_and_fail_at_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, REQUESTED)
    lenient = runner.run_repository(
        "java", REQUESTED, tmp_path, thresholds.BOOTSTRAP
    ).to_dict()
    strict = runner.run_repository(
        "java", REQUESTED, tmp_path, thresholds.TARGET
    ).to_dict()
    assert lenient["metrics"]["unsupported_claim_rate"]["passed"] is True
    assert strict["metrics"]["unsupported_claim_rate"]["passed"] is False


def test_the_runner_defaults_to_bootstrap_and_accepts_the_level_flag() -> None:
    parser = runner.build_parser()
    assert parser.parse_args([]).level == thresholds.BOOTSTRAP
    assert parser.parse_args(["--level", "target"]).level == thresholds.TARGET
    assert runner.LEVEL_CHOICES == ("bootstrap", "target")


def test_the_run_payload_records_the_level_and_its_curve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "missing_binaries", lambda providers: ())
    monkeypatch.setattr(
        runner,
        "run_repository",
        lambda repo, name, workspace, level: runner.RunResult(
            provider=name,
            repository=repo,
            status="ok",
            report=_report(),
            actual_provider=name,
            level=level,
        ),
    )
    payload = runner.run("claude", "java", tmp_path, thresholds.TARGET)
    assert payload["level"] == thresholds.TARGET
    assert payload["thresholds"]["level"] == thresholds.TARGET
    assert payload["thresholds"]["rule_recall"] == 0.85
    assert payload["results"][0]["metrics"]["rule_recall"]["threshold"] == 0.85


def test_evaluate_metrics_ignores_unknown_entries() -> None:
    evaluated = thresholds.evaluate_metrics(
        {"rule_recall": {"value": 0.6}, "unknown": {"value": 1.0}},
        thresholds.BOOTSTRAP,
    )
    assert set(evaluated) == {"rule_recall"}
    assert evaluated["rule_recall"]["passed"] is True


def test_failed_metrics_lists_only_the_names_that_did_not_pass() -> None:
    evaluated = thresholds.evaluate_metrics(
        {
            "rule_recall": {"value": 0.9},
            "unsupported_claim_rate": {"value": 0.1},
        },
        thresholds.TARGET,
    )
    assert runner.failed_metrics(evaluated) == ("unsupported_claim_rate",)


def test_failed_metrics_is_empty_when_every_metric_passes() -> None:
    evaluated = thresholds.evaluate_metrics(
        {"rule_recall": {"value": 0.9}}, thresholds.BOOTSTRAP
    )
    assert runner.failed_metrics(evaluated) == ()


def test_a_metric_below_threshold_marks_the_run_below_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, REQUESTED)
    result = runner.run_repository("java", REQUESTED, tmp_path, thresholds.TARGET)
    assert result.status == runner.STATUS_BELOW_THRESHOLD
    assert result.reason == runner.BELOW_THRESHOLD_REASON
    assert result.failed_metrics == ("unsupported_claim_rate",)
    payload = result.to_dict()
    assert payload["status"] == runner.STATUS_BELOW_THRESHOLD
    assert payload["failed_metrics"] == ["unsupported_claim_rate"]


def test_every_metric_above_threshold_keeps_the_run_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, REQUESTED)
    result = runner.run_repository("java", REQUESTED, tmp_path, thresholds.BOOTSTRAP)
    assert result.status == "ok"
    assert result.failed_metrics == ()
    assert "failed_metrics" not in result.to_dict()


def test_main_exits_five_when_a_run_is_below_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "missing_binaries", lambda providers: ())
    monkeypatch.setattr(
        runner,
        "run_repository",
        lambda repo, name, workspace, level: runner.RunResult(
            provider=name,
            repository=repo,
            status=runner.STATUS_BELOW_THRESHOLD,
            report=_report(),
            reason=runner.BELOW_THRESHOLD_REASON,
            actual_provider=name,
            level=level,
            failed_metrics=("unsupported_claim_rate",),
        ),
    )
    stream = io.StringIO()
    code = runner.main(["--provider", "claude", "--repo", "java", "--level", "target"], stream)
    assert code == runner.EXIT_BELOW_THRESHOLD
    assert code not in {runner.EXIT_OK, runner.EXIT_SKIPPED, runner.EXIT_ERROR}


def test_main_stays_ok_when_every_metric_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "missing_binaries", lambda providers: ())
    monkeypatch.setattr(
        runner,
        "run_repository",
        lambda repo, name, workspace, level: runner.RunResult(
            provider=name,
            repository=repo,
            status="ok",
            report=_report(),
            actual_provider=name,
            level=level,
        ),
    )
    stream = io.StringIO()
    code = runner.main(["--provider", "claude", "--repo", "java"], stream)
    assert code == runner.EXIT_OK


def test_conformance_reports_below_threshold_distinct_from_provider_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "missing_binaries", lambda providers: ())

    def _below(repository: str, provider: str, workspace: Path) -> runner.RunResult:
        return runner.RunResult(
            provider=provider,
            repository=repository,
            status=runner.STATUS_BELOW_THRESHOLD,
            report=_report(),
            reason=runner.BELOW_THRESHOLD_REASON,
            actual_provider=provider,
            failed_metrics=("unsupported_claim_rate",),
        )

    monkeypatch.setattr(runner, "run_repository", _below)
    payload = conformance.run("java", tmp_path)
    assert payload["status"] == conformance.STATUS_BELOW_THRESHOLD
    assert payload["status"] != conformance.STATUS_PROVIDER_MISMATCH
    assert payload["below_threshold"][0]["repository"] == "java"
    entry = payload["comparisons"][0]
    assert entry["comparable"] is False
    assert "knowledge_diff" not in entry


def test_conformance_main_exits_five_on_below_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "missing_binaries", lambda providers: ())
    monkeypatch.setattr(
        runner,
        "run_repository",
        lambda repository, provider, workspace: runner.RunResult(
            provider=provider,
            repository=repository,
            status=runner.STATUS_BELOW_THRESHOLD,
            report=_report(),
            reason=runner.BELOW_THRESHOLD_REASON,
            actual_provider=provider,
            failed_metrics=("unsupported_claim_rate",),
        ),
    )
    stream = io.StringIO()
    code = conformance.main(["--repo", "java", "--workspace", str(tmp_path)], stream)
    assert code == conformance.EXIT_BELOW_THRESHOLD
    assert code != conformance.EXIT_PROVIDER_MISMATCH
    assert code not in {runner.EXIT_OK, runner.EXIT_SKIPPED, runner.EXIT_ERROR}
