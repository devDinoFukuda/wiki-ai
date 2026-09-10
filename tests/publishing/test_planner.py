from __future__ import annotations

from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import DATABASE_FILENAME, KnowledgeRepository
from wiki_ai.publishing.model import CAPABILITY_SECTIONS, DocumentKind
from wiki_ai.publishing.planner import (
    GAPS_DOCUMENT_ID,
    INTEGRATION_CATALOG_ID,
    RULES_CATALOG_ID,
    plan,
)


def test_plan_has_one_document_per_system(query, graph):
    documents = plan(query, "ns").of_kind(DocumentKind.SYSTEM_OVERVIEW)
    assert len(documents) == 1
    assert documents[0].subject_entity_id == graph.id("system").value


def test_plan_has_one_document_per_capability(query):
    documents = plan(query, "ns").of_kind(DocumentKind.CAPABILITY)
    assert len(documents) == 2


def test_capability_document_uses_the_fifteen_sections(query):
    documents = plan(query, "ns").of_kind(DocumentKind.CAPABILITY)
    for document in documents:
        assert document.section_titles() == CAPABILITY_SECTIONS
        assert [s.number for s in document.sections] == list(range(1, 16))


def test_plan_emits_catalogs_and_gaps_report(query):
    identifiers = {document.document_id for document in plan(query, "ns").documents}
    assert INTEGRATION_CATALOG_ID in identifiers
    assert RULES_CATALOG_ID in identifiers
    assert GAPS_DOCUMENT_ID in identifiers


def test_plan_emits_change_impact_per_proposal(query):
    documents = plan(query, "ns").of_kind(DocumentKind.CHANGE_IMPACT)
    assert len(documents) == 2


def test_plan_is_stable_across_calls(query):
    first = [d.document_id for d in plan(query, "ns").documents]
    second = [d.document_id for d in plan(query, "ns").documents]
    assert first == second


def test_plan_is_ordered_by_kind_then_id(query):
    documents = plan(query, "ns").documents
    keys = [(d.kind.value, d.document_id) for d in documents]
    assert keys == sorted(keys)


def test_empty_knowledge_yields_empty_plan(tmp_path):
    repository = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    result = plan(KnowledgeQuery(repository), "ns")
    assert result.is_empty
    assert len(result) == 0
    repository.close()
