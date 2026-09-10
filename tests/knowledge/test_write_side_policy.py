from __future__ import annotations

import ast
import pathlib

import pytest

from wiki_ai.knowledge import Confidence, GraphPolicy
from wiki_ai.knowledge.gaps import gaps_about, open_gap
from wiki_ai.knowledge.invalidation import (
    relations_of_entities,
    relations_of_evidence,
)

from .graph_fixture import build

KNOWLEDGE_DIR = pathlib.Path(__file__).resolve().parents[2] / "src" / "wiki_ai" / "knowledge"

READ_FUNCTIONS: frozenset[str] = frozenset({"relations_of", "neighborhood", "neighbor_ids"})

POLICY_FREE_MODULES: frozenset[str] = frozenset({"relations.py", "query.py", "repository.py"})


@pytest.fixture()
def graph(tmp_path):
    built = build(tmp_path)
    yield built
    built.repository.close()


def unresolve(graph, kind: str, source: str, target: str) -> str:
    found = ""
    for relation in graph.repository.relations_of(
        graph.id(source), "out", (kind,), policy=GraphPolicy.ALL
    ):
        if relation.target_id == graph.id(target):
            found = relation.id
            graph.repository.conn.execute(
                "UPDATE relations SET confidence=? WHERE relation_id=?",
                (Confidence.UNRESOLVED.value, relation.id),
            )
    assert found
    return found


def policy_keyword(call: ast.Call) -> str:
    for keyword in call.keywords:
        if keyword.arg != "policy":
            continue
        if isinstance(keyword.value, ast.Attribute):
            return keyword.value.attr
    return ""


def graph_read_calls(path: pathlib.Path) -> list[tuple[str, int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name not in READ_FUNCTIONS:
            continue
        found.append((name, node.lineno, policy_keyword(node)))
    return found


def test_every_internal_graph_read_declares_all_explicitly() -> None:
    implicit: list[str] = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.py")):
        if path.name in POLICY_FREE_MODULES:
            continue
        for name, line, policy in graph_read_calls(path):
            if policy != GraphPolicy.ALL.name:
                implicit.append(f"{path.name}:{line} {name} policy={policy or 'default'}")

    assert implicit == []


def test_query_module_threads_the_configured_policy() -> None:
    tree = ast.parse((KNOWLEDGE_DIR / "query.py").read_text(encoding="utf-8"))
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name not in READ_FUNCTIONS:
            continue
        threaded = any(
            keyword.arg == "policy"
            and isinstance(keyword.value, ast.Attribute)
            and keyword.value.attr == "_policy"
            for keyword in node.keywords
        )
        if not threaded:
            missing.append(f"query.py:{node.lineno} {name}")

    assert missing == []


def test_relations_of_entities_sees_unresolved_relations(graph) -> None:
    relation_id = unresolve(graph, "calls", "capability", "integration")

    found = relations_of_entities(graph.repository, [graph.id("capability").value])

    assert relation_id in found


@pytest.mark.parametrize(
    "confidence",
    [Confidence.SUPPORTED, Confidence.INFERRED, Confidence.UNRESOLVED, Confidence.CONTRADICTED],
)
def test_relations_of_entities_reaches_every_confidence(graph, confidence) -> None:
    relation_id = ""
    for relation in graph.repository.relations_of(
        graph.id("capability"), "out", ("calls",), policy=GraphPolicy.ALL
    ):
        relation_id = relation.id
        graph.repository.conn.execute(
            "UPDATE relations SET confidence=? WHERE relation_id=?",
            (confidence.value, relation.id),
        )
    assert relation_id

    found = relations_of_entities(graph.repository, [graph.id("capability").value])

    assert relation_id in found


def test_relations_of_evidence_sees_unresolved_relations(graph) -> None:
    relation_id = unresolve(graph, "calls", "capability", "integration")
    evidence_ids = [
        item.id
        for item in graph.repository.evidence_for_relation(relation_id, active_only=False)
    ]

    assert evidence_ids
    assert relation_id in relations_of_evidence(graph.repository, evidence_ids)


def test_gaps_about_sees_unresolved_affects_relations(graph) -> None:
    unresolve(graph, "affects", "gap", "capability")

    found = gaps_about(graph.repository, graph.id("capability"))

    assert graph.id("gap").value in {gap.id.value for gap in found}


def test_a_gap_opened_now_is_still_reported_on_the_audit_side(graph) -> None:
    repo = graph.repository
    with repo.begin_revision("pipeline", "duvida") as revision:
        gap_id = open_gap(
            revision, "Qual o SLA?", about=graph.id("integration"), blocking=True
        )

    found = gaps_about(repo, graph.id("integration"))

    assert gap_id.value in {gap.id.value for gap in found}
