from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.benchmark import conformance, metrics, runner
from tests.benchmark.ground_truth import corpora
from wiki_ai.knowledge.evidence import CodeLocator, make_evidence
from wiki_ai.knowledge.model import Confidence, Entity, KnowledgeState, SourceVersion
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import REQUIRED_ATTRIBUTES, EntityKind

NAMESPACE = "repo_conformance"
VERSION_HASH = "1" * 64
CAPTURED_AT = "2026-09-10T00:00:00+00:00"

PROVIDERS_MISSING = tuple(
    name for name in conformance.PAIR if shutil.which(runner.PROVIDER_BINARIES[name]) is None
)
REAL_PROVIDER_REASON = (
    "requires the real provider binaries "
    f"{list(conformance.PAIR)}; missing: {list(PROVIDERS_MISSING)}"
)
requires_both_providers = pytest.mark.skipif(
    bool(PROVIDERS_MISSING), reason=REAL_PROVIDER_REASON
)


def _knowledge(path: Path) -> KnowledgeRepository:
    path.mkdir(parents=True, exist_ok=True)
    return KnowledgeRepository.open(str(path / "state.db"))


def _attributes(kind: str, name: str) -> dict[str, str]:
    required = REQUIRED_ATTRIBUTES.get(EntityKind(kind), ())
    payload = {field: name for field in required}
    payload["detail"] = name
    return payload


def _seed(
    knowledge: KnowledgeRepository,
    entries: tuple[tuple[str, str, str, int, int], ...],
) -> None:
    record = SourceVersion(
        source_id=NAMESPACE,
        version_hash=VERSION_HASH,
        locator_root=NAMESPACE,
        captured_at=CAPTURED_AT,
    )
    with knowledge.begin_revision(author=NAMESPACE, summary="shape") as tx:
        tx.put_source_version(record)
        for kind, name, path, start, end in entries:
            entity = Entity.create(
                kind=kind,
                name=name,
                stable_key=f"{NAMESPACE}::{kind}::{name}",
                attributes=_attributes(kind, name),
                state=KnowledgeState.IMPLEMENTED,
                confidence=Confidence.INFERRED,
                source_versions=(record.key,),
            )
            entity_id = tx.put_entity(entity)
            locator = CodeLocator(path=path, line_start=start, line_end=end)
            tx.put_evidence(
                make_evidence(NAMESPACE, VERSION_HASH, locator, name, CAPTURED_AT),
                entity_ids=(entity_id,),
            )


def test_shape_counts_entities_relations_and_evidence_paths(tmp_path: Path) -> None:
    with _knowledge(tmp_path) as knowledge:
        _seed(
            knowledge,
            (
                ("business_rule", "rule one", "a/One.java", 1, 4),
                ("invariant", "invariant one", "a/One.java", 8, 9),
            ),
        )
        shape = conformance.shape_of(knowledge, "claude")
    assert shape.entities_by_kind == {"business_rule": 1, "invariant": 1}
    assert shape.evidence_paths == ("a/One.java",)
    assert shape.provider == "claude"


def test_identical_shapes_produce_an_empty_diff(tmp_path: Path) -> None:
    entries = (("business_rule", "rule one", "a/One.java", 1, 4),)
    with _knowledge(tmp_path / "left") as left, _knowledge(tmp_path / "right") as right:
        _seed(left, entries)
        _seed(right, entries)
        diff = conformance.compare_shapes(
            conformance.shape_of(left, "claude"), conformance.shape_of(right, "codex")
        )
    assert diff.identical()
    assert diff.to_dict()["identical"] is True


def test_diff_reports_divergent_kinds_and_paths(tmp_path: Path) -> None:
    with _knowledge(tmp_path / "left") as left, _knowledge(tmp_path / "right") as right:
        _seed(left, (("business_rule", "rule one", "a/One.java", 1, 4),))
        _seed(
            right,
            (
                ("business_rule", "rule one", "a/One.java", 1, 4),
                ("edge_case", "edge one", "b/Two.java", 2, 3),
            ),
        )
        diff = conformance.compare_shapes(
            conformance.shape_of(left, "claude"), conformance.shape_of(right, "codex")
        )
    assert not diff.identical()
    assert diff.entities_by_kind["edge_case"] == (0, 1)
    assert diff.evidence_paths_only_second == ("b/Two.java",)
    assert diff.names_only_second["edge_case"] == ("edge one",)


def test_metric_comparison_reports_the_delta_between_providers(tmp_path: Path) -> None:
    truth = corpora.materialize("java", tmp_path / "java").truth
    empty = metrics.BenchmarkReport(
        repository="java",
        scores=(metrics.MetricScore("rule_recall", 0.0, 0, 3),),
        items=(),
        entity_total=0,
        supported_total=0,
    )
    full = metrics.BenchmarkReport(
        repository="java",
        scores=(metrics.MetricScore("rule_recall", 1.0, 3, 3),),
        items=(),
        entity_total=3,
        supported_total=3,
    )
    comparison = conformance.compare_metrics(empty, full)
    assert comparison["rule_recall"] == {"claude": 0.0, "codex": 1.0, "delta": 1.0}
    assert truth.name == "java"


def test_render_table_lists_one_row_per_repository() -> None:
    payload = {
        "status": "ok",
        "comparisons": [
            {
                "repository": "java",
                "knowledge_diff": {
                    "identical": False,
                    "entities_by_kind": {"edge_case": [0, 1]},
                    "relations_by_kind": {},
                },
            }
        ],
    }
    text = conformance.render_table(payload)
    assert "java" in text
    assert "no" in text


@requires_both_providers
def test_conformance_runs_java_with_both_real_providers(tmp_path: Path) -> None:
    payload = conformance.run("java", tmp_path)
    assert payload["status"] in {"ok", "partial"}
    assert payload["providers"] == list(conformance.PAIR)
    assert payload["comparisons"]
    assert payload["comparisons"][0]["repository"] == "java"
