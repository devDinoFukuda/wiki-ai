from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from tests.benchmark import metrics
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
    "SKIP_REASON",
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
SKIP_REASON = "provider_binary_unavailable"
STATUS_SKIPPED = "skipped"

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

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "repository": self.repository,
            "status": self.status,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.stages:
            payload["stages"] = dict(self.stages)
        if self.report is not None:
            payload["metrics"] = self.report.to_dict()["metrics"]
            payload["items"] = self.report.to_dict()["items"]
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


def _prepare(name: str, workspace: Path) -> corpora.GeneratedCorpus:
    root = workspace / name
    return corpora.materialize(name, root)


def run_repository(name: str, provider: str, workspace: Path) -> RunResult:
    corpus = _prepare(name, workspace)
    registry = _registry(provider)
    wiring = Wiring(registry)
    stages: dict[str, Any] = {}
    analyzed = api.analyze(corpus.root, OBJECTIVE_BY_REPO[name], wiring=wiring)
    stages["analyze"] = analyzed.to_dict()
    if analyzed.status != "ok":
        return RunResult(
            provider=provider,
            repository=name,
            status=analyzed.status,
            stages=stages,
            reason=analyzed.reason,
        )
    ingested: list[dict[str, Any]] = []
    for document in corpus.documents:
        ingested.append(api.ingest(document, corpus.root, wiring=wiring).to_dict())
    if ingested:
        stages["ingest"] = ingested
    stages["ask"] = api.ask(QUESTION_BY_REPO[name], corpus.root, wiring=wiring).to_dict()
    stages["publish"] = api.publish(corpus.root, wiring=wiring).to_dict()
    session = Session.open(corpus.root)
    with session.open_knowledge() as knowledge:
        report = metrics.evaluate(knowledge, corpus.truth)
    return RunResult(
        provider=provider,
        repository=name,
        status="ok",
        report=report,
        stages=stages,
    )


def run(provider: str, repository: str, workspace: Path | None = None) -> dict[str, Any]:
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
            results.append(run_repository(repo, name, holder / name))
    blocked = [entry for entry in results if entry.status != "ok"]
    return {
        "status": "ok" if not blocked else "partial",
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
    return parser


def main(argv: Sequence[str] | None = None, stream: TextIO | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    output = stream if stream is not None else sys.stdout
    workspace = Path(arguments.workspace) if arguments.workspace else None
    try:
        payload = run(arguments.provider, arguments.repo, workspace)
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
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
