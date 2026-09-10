from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from tests.benchmark import metrics, thresholds
from tests.benchmark.ground_truth import corpora
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app import api
from wiki_ai.app.session import Session
from wiki_ai.app.wiring import Wiring

__all__ = [
    "PROGRAM_NAME",
    "PROVIDER_BINARIES",
    "PROVIDER_CHOICES",
    "REPO_CHOICES",
    "ALL",
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_SKIPPED",
    "EXIT_BELOW_THRESHOLD",
    "SKIP_REASON",
    "STATUS_PROVIDER_MISMATCH",
    "PROVIDER_MISMATCH_REASON",
    "STATUS_BELOW_THRESHOLD",
    "BELOW_THRESHOLD_REASON",
    "failed_metrics",
    "LEVEL_CHOICES",
    "stage_providers",
    "QUESTION_BY_REPO",
    "OBJECTIVE_BY_REPO",
    "RunResult",
    "missing_binaries",
    "run_repository",
    "run",
    "render_table",
    "main",
]

PROGRAM_NAME = "tests.benchmark.runner"
ALL = "all"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_SKIPPED = 3
EXIT_BELOW_THRESHOLD = 5
SKIP_REASON = "provider_binary_unavailable"
STATUS_SKIPPED = "skipped"
STATUS_PROVIDER_MISMATCH = "provider_mismatch"
PROVIDER_MISMATCH_REASON = "resolved_provider_differs_from_requested"
STATUS_BELOW_THRESHOLD = "below_threshold"
BELOW_THRESHOLD_REASON = "metrics_below_threshold_at_level"
PROVIDER_FIELD = "provider"
LEVEL_CHOICES: tuple[str, ...] = thresholds.LEVELS

PROVIDER_BINARIES: Mapping[str, str] = {"claude": "claude", "codex": "codex"}
PROVIDER_CHOICES: tuple[str, ...] = tuple(sorted(PROVIDER_BINARIES)) + (ALL,)
REPO_CHOICES: tuple[str, ...] = corpora.CORPUS_NAMES + (ALL,)

OBJECTIVE_BY_REPO: Mapping[str, str] = {
    "java": "describe the contract renewal rules, retries, failures and integrations",
    "python": "describe the order lifecycle, validation, idempotency and fallbacks",
    "cobol": "describe the billing batch flow, its rules, sql access and failures",
}

QUESTION_BY_REPO: Mapping[str, str] = {
    "java": "Quais condicoes bloqueiam a renovacao de um contrato?",
    "python": "Quais transicoes de estado um pedido pode sofrer?",
    "cobol": "O que acontece quando o SQLCODE do batch nao e zero?",
}


@dataclass(frozen=True)
class RunResult:
    provider: str
    repository: str
    status: str
    report: metrics.BenchmarkReport | None = None
    stages: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    actual_provider: str = ""
    level: str = thresholds.DEFAULT_LEVEL
    failed_metrics: tuple[str, ...] = ()

    def provider_matches(self) -> bool:
        return self.actual_provider == self.provider

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "requested_provider": self.provider,
            "actual_provider": self.actual_provider,
            "repository": self.repository,
            "status": self.status,
            "level": self.level,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.stages:
            payload["stages"] = dict(self.stages)
        if self.failed_metrics:
            payload["failed_metrics"] = list(self.failed_metrics)
        if self.report is not None:
            rendered = self.report.to_dict()
            payload["metrics"] = thresholds.evaluate_metrics(
                rendered["metrics"], self.level
            )
            payload["raw_metrics"] = rendered["metrics"]
            payload["items"] = rendered["items"]
            payload["entities"] = self.report.entity_total
            payload["supported_entities"] = self.report.supported_total
        return payload


def _selected(value: str, choices: Sequence[str]) -> tuple[str, ...]:
    if value == ALL:
        return tuple(name for name in choices if name != ALL)
    return (value,)


def missing_binaries(providers: Sequence[str]) -> tuple[str, ...]:
    absent: list[str] = []
    for name in providers:
        if shutil.which(PROVIDER_BINARIES[name]) is None:
            absent.append(name)
    return tuple(absent)


def _registry(provider: str) -> ProviderRegistry:
    registry = ProviderRegistry(adapters=True)
    if provider not in registry.registered():
        raise LookupError(provider)
    return registry


def stage_providers(stages: Mapping[str, Any]) -> tuple[str, ...]:
    observed: list[str] = []
    for payload in stages.values():
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            if PROVIDER_FIELD not in entry:
                continue
            observed.append(str(entry[PROVIDER_FIELD]))
    return tuple(observed)


def _observed_provider(stages: Mapping[str, Any], requested: str) -> str:
    reported = [name for name in stage_providers(stages) if name]
    divergent = [name for name in reported if name != requested]
    if divergent:
        return divergent[0]
    return requested


def _prepare(name: str, workspace: Path) -> corpora.GeneratedCorpus:
    root = workspace / name
    return corpora.materialize(name, root)


def _mismatch(
    name: str, provider: str, stages: Mapping[str, Any], level: str
) -> RunResult | None:
    observed = _observed_provider(stages, provider)
    if observed == provider:
        return None
    return RunResult(
        provider=provider,
        repository=name,
        status=STATUS_PROVIDER_MISMATCH,
        stages=dict(stages),
        reason=PROVIDER_MISMATCH_REASON,
        actual_provider=observed,
        level=level,
    )


def failed_metrics(evaluated: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(
        name for name, entry in evaluated.items() if not entry.get("passed", False)
    )


def run_repository(
    name: str,
    provider: str,
    workspace: Path,
    level: str = thresholds.DEFAULT_LEVEL,
) -> RunResult:
    corpus = _prepare(name, workspace)
    registry = _registry(provider)
    wiring = Wiring(registry, provider=provider)
    stages: dict[str, Any] = {}
    analyzed = api.analyze(corpus.root, OBJECTIVE_BY_REPO[name], wiring=wiring)
    stages["analyze"] = analyzed.to_dict()
    diverged = _mismatch(name, provider, stages, level)
    if diverged is not None:
        return diverged
    if analyzed.status != "ok":
        return RunResult(
            provider=provider,
            repository=name,
            status=analyzed.status,
            stages=stages,
            reason=analyzed.reason,
            actual_provider=provider,
            level=level,
        )
    ingested: list[dict[str, Any]] = []
    for document in corpus.documents:
        ingested.append(api.ingest(document, corpus.root, wiring=wiring).to_dict())
    if ingested:
        stages["ingest"] = ingested
        diverged = _mismatch(name, provider, stages, level)
        if diverged is not None:
            return diverged
    stages["ask"] = api.ask(QUESTION_BY_REPO[name], corpus.root, wiring=wiring).to_dict()
    diverged = _mismatch(name, provider, stages, level)
    if diverged is not None:
        return diverged
    stages["publish"] = api.publish(corpus.root, wiring=wiring).to_dict()
    diverged = _mismatch(name, provider, stages, level)
    if diverged is not None:
        return diverged
    session = Session.open(corpus.root)
    with session.open_knowledge() as knowledge:
        report = metrics.evaluate(knowledge, corpus.truth)
    evaluated = thresholds.evaluate_metrics(report.to_dict()["metrics"], level)
    below = failed_metrics(evaluated)
    return RunResult(
        provider=provider,
        repository=name,
        status=STATUS_BELOW_THRESHOLD if below else "ok",
        report=report,
        stages=stages,
        reason=BELOW_THRESHOLD_REASON if below else "",
        actual_provider=provider,
        level=level,
        failed_metrics=below,
    )


def run(
    provider: str,
    repository: str,
    workspace: Path | None = None,
    level: str = thresholds.DEFAULT_LEVEL,
) -> dict[str, Any]:
    providers = _selected(provider, PROVIDER_CHOICES)
    repositories = _selected(repository, REPO_CHOICES)
    absent = missing_binaries(providers)
    if absent:
        return {
            "status": STATUS_SKIPPED,
            "reason": SKIP_REASON,
            "providers": list(providers),
            "missing": list(absent),
        }
    results: list[RunResult] = []
    if workspace is None:
        holder = Path(tempfile.mkdtemp(prefix="wiki-ai-benchmark-"))
    else:
        holder = Path(workspace)
        holder.mkdir(parents=True, exist_ok=True)
    for name in providers:
        for repo in repositories:
            results.append(run_repository(repo, name, holder / name, level))
    blocked = [entry for entry in results if entry.status != "ok"]
    return {
        "status": "ok" if not blocked else "partial",
        "level": level,
        "thresholds": thresholds.THRESHOLDS[level].to_dict(),
        "providers": list(providers),
        "repositories": list(repositories),
        "workspace": holder.as_posix(),
        "results": [entry.to_dict() for entry in results],
    }


_COLUMNS = (
    "rule_recall",
    "edge_case_recall",
    "invariant_recall",
    "integration_recall",
    "entrypoint_recall",
    "evidence_precision",
    "unsupported_claim_rate",
    "contradiction_detection",
)


def render_table(payload: Mapping[str, Any]) -> str:
    if payload.get("status") == STATUS_SKIPPED:
        return f"skipped: {payload.get('reason')} ({', '.join(payload.get('missing', []))})"
    header = ["provider", "repository", "status", *(_short(name) for name in _COLUMNS)]
    rows = [header]
    for entry in payload.get("results", ()):
        scores = entry.get("metrics", {})
        rows.append(
            [
                str(entry.get("provider", "")),
                str(entry.get("repository", "")),
                str(entry.get("status", "")),
                *(_cell(scores, name) for name in _COLUMNS),
            ]
        )
    widths = [max(len(row[index]) for row in rows) for index in range(len(header))]
    lines = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    ]
    return "\n".join(lines)


def _short(name: str) -> str:
    return "".join(part[:4] for part in name.split("_"))


def _cell(scores: Mapping[str, Any], name: str) -> str:
    entry = scores.get(name)
    if not isinstance(entry, Mapping):
        return "-"
    return f"{float(entry.get('value', 0.0)):.2f}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM_NAME, add_help=True)
    parser.add_argument("--provider", choices=PROVIDER_CHOICES, default=ALL)
    parser.add_argument("--repo", choices=REPO_CHOICES, default=ALL)
    parser.add_argument("--out", default=None)
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--level", choices=LEVEL_CHOICES, default=thresholds.DEFAULT_LEVEL
    )
    return parser


def main(argv: Sequence[str] | None = None, stream: TextIO | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    output = stream if stream is not None else sys.stdout
    workspace = Path(arguments.workspace) if arguments.workspace else None
    try:
        payload = run(arguments.provider, arguments.repo, workspace, arguments.level)
    except (LookupError, OSError) as failure:
        payload = {"status": "error", "reason": type(failure).__name__, "detail": str(failure)}
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    if arguments.out:
        target = Path(arguments.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    else:
        print(text, file=output)
    print(render_table(payload), file=output)
    if payload.get("status") == STATUS_SKIPPED:
        return EXIT_SKIPPED
    if payload.get("status") == "error":
        return EXIT_ERROR
    if any(
        entry.get("status") == STATUS_BELOW_THRESHOLD
        for entry in payload.get("results", ())
    ):
        return EXIT_BELOW_THRESHOLD
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
