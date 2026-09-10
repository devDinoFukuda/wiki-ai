from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from tests.benchmark import metrics, runner
from tests.benchmark.ground_truth import corpora
from wiki_ai.app.session import Session
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind

__all__ = [
    "PROGRAM_NAME",
    "PAIR",
    "KnowledgeShape",
    "ShapeDiff",
    "shape_of",
    "compare_shapes",
    "compare_metrics",
    "run",
    "render_table",
    "main",
]

PROGRAM_NAME = "tests.benchmark.conformance"
PAIR: tuple[str, str] = ("claude", "codex")


@dataclass(frozen=True)
class KnowledgeShape:
    provider: str
    entities_by_kind: Mapping[str, int]
    relations_by_kind: Mapping[str, int]
    evidence_paths: tuple[str, ...]
    entity_names_by_kind: Mapping[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "entities_by_kind": dict(self.entities_by_kind),
            "relations_by_kind": dict(self.relations_by_kind),
            "evidence_paths": list(self.evidence_paths),
        }


@dataclass(frozen=True)
class ShapeDiff:
    entities_by_kind: Mapping[str, tuple[int, int]]
    relations_by_kind: Mapping[str, tuple[int, int]]
    evidence_paths_only_first: tuple[str, ...]
    evidence_paths_only_second: tuple[str, ...]
    names_only_first: Mapping[str, tuple[str, ...]]
    names_only_second: Mapping[str, tuple[str, ...]]

    def identical(self) -> bool:
        return not (
            self.entities_by_kind
            or self.relations_by_kind
            or self.evidence_paths_only_first
            or self.evidence_paths_only_second
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identical": self.identical(),
            "entities_by_kind": {
                kind: list(pair) for kind, pair in self.entities_by_kind.items()
            },
            "relations_by_kind": {
                kind: list(pair) for kind, pair in self.relations_by_kind.items()
            },
            "evidence_paths_only_first": list(self.evidence_paths_only_first),
            "evidence_paths_only_second": list(self.evidence_paths_only_second),
            "names_only_first": {
                kind: list(names) for kind, names in self.names_only_first.items()
            },
            "names_only_second": {
                kind: list(names) for kind, names in self.names_only_second.items()
            },
        }


def shape_of(knowledge: KnowledgeRepository, provider: str) -> KnowledgeShape:
    entities: dict[str, int] = {}
    names: dict[str, tuple[str, ...]] = {}
    paths: set[str] = set()
    for kind in EntityKind:
        found = knowledge.find_entities(kind.value)
        if not found:
            continue
        entities[kind.value] = len(found)
        names[kind.value] = tuple(sorted(entity.name for entity in found))
        for entity in found:
            for evidence in knowledge.evidence_for(entity.id):
                located = evidence.locator.to_dict().get("path")
                if isinstance(located, str) and located:
                    paths.add(located.replace("\\", "/"))
    relations: dict[str, int] = {}
    for relation in knowledge.find_relations():
        relations[relation.kind] = relations.get(relation.kind, 0) + 1
    return KnowledgeShape(
        provider=provider,
        entities_by_kind=entities,
        relations_by_kind=relations,
        evidence_paths=tuple(sorted(paths)),
        entity_names_by_kind=names,
    )


def _count_diff(
    first: Mapping[str, int], second: Mapping[str, int]
) -> dict[str, tuple[int, int]]:
    diverging: dict[str, tuple[int, int]] = {}
    for key in sorted(set(first) | set(second)):
        left = first.get(key, 0)
        right = second.get(key, 0)
        if left != right:
            diverging[key] = (left, right)
    return diverging


def _name_diff(
    first: Mapping[str, tuple[str, ...]], second: Mapping[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    exclusive: dict[str, tuple[str, ...]] = {}
    for key in sorted(set(first) | set(second)):
        only = tuple(sorted(set(first.get(key, ())) - set(second.get(key, ()))))
        if only:
            exclusive[key] = only
    return exclusive


def compare_shapes(first: KnowledgeShape, second: KnowledgeShape) -> ShapeDiff:
    left_paths = set(first.evidence_paths)
    right_paths = set(second.evidence_paths)
    return ShapeDiff(
        entities_by_kind=_count_diff(first.entities_by_kind, second.entities_by_kind),
        relations_by_kind=_count_diff(first.relations_by_kind, second.relations_by_kind),
        evidence_paths_only_first=tuple(sorted(left_paths - right_paths)),
        evidence_paths_only_second=tuple(sorted(right_paths - left_paths)),
        names_only_first=_name_diff(first.entity_names_by_kind, second.entity_names_by_kind),
        names_only_second=_name_diff(second.entity_names_by_kind, first.entity_names_by_kind),
    )


def compare_metrics(
    first: metrics.BenchmarkReport, second: metrics.BenchmarkReport
) -> dict[str, dict[str, float]]:
    names = sorted({entry.name for entry in first.scores} | {entry.name for entry in second.scores})
    comparison: dict[str, dict[str, float]] = {}
    for name in names:
        left = _safe(first, name)
        right = _safe(second, name)
        comparison[name] = {
            PAIR[0]: round(left, 4),
            PAIR[1]: round(right, 4),
            "delta": round(right - left, 4),
        }
    return comparison


def _safe(report: metrics.BenchmarkReport, name: str) -> float:
    try:
        return report.value(name)
    except KeyError:
        return 0.0


def _run_one(repository: str, provider: str, workspace: Path) -> tuple[
    runner.RunResult, KnowledgeShape | None
]:
    result = runner.run_repository(repository, provider, workspace / provider)
    if result.status != "ok":
        return result, None
    corpus_root = workspace / provider / repository
    session = Session.open(corpus_root)
    with session.open_knowledge() as knowledge:
        shape = shape_of(knowledge, provider)
    return result, shape


def run(repository: str, workspace: Path | None = None) -> dict[str, Any]:
    absent = runner.missing_binaries(PAIR)
    if absent:
        return {
            "status": "skipped",
            "reason": runner.SKIP_REASON,
            "providers": list(PAIR),
            "missing": list(absent),
        }
    repositories = (
        corpora.CORPUS_NAMES if repository == runner.ALL else (repository,)
    )
    holder = (
        Path(tempfile.mkdtemp(prefix="wiki-ai-conformance-"))
        if workspace is None
        else Path(workspace)
    )
    holder.mkdir(parents=True, exist_ok=True)
    comparisons: list[dict[str, Any]] = []
    for name in repositories:
        first_result, first_shape = _run_one(name, PAIR[0], holder / name)
        second_result, second_shape = _run_one(name, PAIR[1], holder / name)
        entry: dict[str, Any] = {
            "repository": name,
            PAIR[0]: first_result.to_dict(),
            PAIR[1]: second_result.to_dict(),
        }
        if first_shape is not None and second_shape is not None:
            entry["knowledge_diff"] = compare_shapes(first_shape, second_shape).to_dict()
        if first_result.report is not None and second_result.report is not None:
            entry["metric_diff"] = compare_metrics(
                first_result.report, second_result.report
            )
        comparisons.append(entry)
    failed = [entry for entry in comparisons if "knowledge_diff" not in entry]
    return {
        "status": "ok" if not failed else "partial",
        "providers": list(PAIR),
        "workspace": holder.as_posix(),
        "comparisons": comparisons,
    }


def render_table(payload: Mapping[str, Any]) -> str:
    if payload.get("status") == "skipped":
        return f"skipped: {payload.get('reason')} ({', '.join(payload.get('missing', []))})"
    rows = [["repository", "identical", "entity_kind_diffs", "relation_kind_diffs"]]
    for entry in payload.get("comparisons", ()):
        diff = entry.get("knowledge_diff")
        if not isinstance(diff, Mapping):
            rows.append([str(entry.get("repository", "")), "-", "-", "-"])
            continue
        rows.append(
            [
                str(entry.get("repository", "")),
                "yes" if diff.get("identical") else "no",
                str(len(diff.get("entities_by_kind", {}))),
                str(len(diff.get("relations_by_kind", {}))),
            ]
        )
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return "\n".join(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM_NAME, add_help=True)
    parser.add_argument("--repo", choices=runner.REPO_CHOICES, default=runner.ALL)
    parser.add_argument("--out", default=None)
    parser.add_argument("--workspace", default=None)
    return parser


def main(argv: Sequence[str] | None = None, stream: TextIO | None = None) -> int:
    arguments = build_parser().parse_args(list(argv) if argv is not None else None)
    output = stream if stream is not None else sys.stdout
    workspace = Path(arguments.workspace) if arguments.workspace else None
    try:
        payload = run(arguments.repo, workspace)
    except (LookupError, OSError) as failure:
        payload = {
            "status": "error",
            "reason": type(failure).__name__,
            "detail": str(failure),
        }
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    if arguments.out:
        target = Path(arguments.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    else:
        print(text, file=output)
    print(render_table(payload), file=output)
    if payload.get("status") == "skipped":
        return runner.EXIT_SKIPPED
    if payload.get("status") == "error":
        return runner.EXIT_ERROR
    return runner.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
