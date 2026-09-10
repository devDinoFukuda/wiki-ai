from __future__ import annotations

import pytest

from tests.knowledge.graph_fixture import Graph, build
from tests.publishing.grounding_fixture import GroundingGraph
from tests.publishing.grounding_fixture import build as build_grounding
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.publishing.query_harness import KnowledgeQueryHarness


@pytest.fixture
def graph(tmp_path) -> Graph:
    return build(tmp_path / "knowledge")


@pytest.fixture
def query(graph: Graph) -> KnowledgeQuery:
    return KnowledgeQuery(graph.repository)


@pytest.fixture
def grounded(tmp_path) -> GroundingGraph:
    built = build_grounding(tmp_path / "grounding")
    yield built
    built.repository.close()


@pytest.fixture
def grounded_harness(grounded: GroundingGraph) -> KnowledgeQueryHarness:
    return KnowledgeQueryHarness(grounded.repository, "ns")


@pytest.fixture
def publications_dir(tmp_path):
    return tmp_path / "publications"


@pytest.fixture
def silent_narrative(monkeypatch):
    from wiki_ai.publishing import narrative as narrative_module

    monkeypatch.setattr(
        narrative_module.NarrativeBuilder,
        "_gap_assertions",
        lambda self, gaps: (),
    )
    monkeypatch.setattr(
        narrative_module.NarrativeBuilder,
        "_reserved_assertions",
        lambda self, entities: (),
    )
    return narrative_module
