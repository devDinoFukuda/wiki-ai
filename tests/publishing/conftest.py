from __future__ import annotations

import pytest

from tests.knowledge.graph_fixture import Graph, build
from wiki_ai.knowledge.query import KnowledgeQuery


@pytest.fixture
def graph(tmp_path) -> Graph:
    return build(tmp_path / "knowledge")


@pytest.fixture
def query(graph: Graph) -> KnowledgeQuery:
    return KnowledgeQuery(graph.repository)


@pytest.fixture
def publications_dir(tmp_path):
    return tmp_path / "publications"
