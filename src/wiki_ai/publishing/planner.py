from __future__ import annotations

import hashlib
import re
from typing import Iterable

from wiki_ai.knowledge.model import Entity
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.taxonomy import EntityKind

from .model import (
    CAPABILITY_SECTIONS,
    CATALOG_SECTIONS,
    CHANGE_IMPACT_SECTIONS,
    GAPS_SECTIONS,
    SYSTEM_SECTIONS,
    DocumentKind,
    DocumentPlan,
    PublicationPlan,
    SectionPlan,
)

__all__ = [
    "PLAN_LIMIT",
    "GAPS_DOCUMENT_ID",
    "INTEGRATION_CATALOG_ID",
    "RULES_CATALOG_ID",
    "plan",
    "document_slug",
]

PLAN_LIMIT = 1000
GAPS_DOCUMENT_ID = "gaps-report"
INTEGRATION_CATALOG_ID = "integration-catalog"
RULES_CATALOG_ID = "rules-catalog"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_SLUG_MAX = 48

_SECTION_REFS: dict[DocumentKind, tuple[str, ...]] = {
    DocumentKind.SYSTEM_OVERVIEW: SYSTEM_SECTIONS,
    DocumentKind.CAPABILITY: CAPABILITY_SECTIONS,
    DocumentKind.INTEGRATION_CATALOG: CATALOG_SECTIONS,
    DocumentKind.RULES_CATALOG: CATALOG_SECTIONS,
    DocumentKind.GAPS_REPORT: GAPS_SECTIONS,
    DocumentKind.CHANGE_IMPACT: CHANGE_IMPACT_SECTIONS,
}

_CHANGE_KINDS: tuple[EntityKind, ...] = (EntityKind.PROPOSAL, EntityKind.INITIATIVE)


def document_slug(prefix: str, name: str, entity_id: str) -> str:
    base = _SLUG_STRIP.sub("-", str(name).lower()).strip("-")
    if len(base) > _SLUG_MAX:
        base = base[:_SLUG_MAX].rstrip("-")
    if not base:
        base = "documento"
    suffix = hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{base}-{suffix}"


def _sections(kind: DocumentKind) -> tuple[SectionPlan, ...]:
    titles = _SECTION_REFS[kind]
    return tuple(
        SectionPlan(
            number=index,
            title=title,
            content_ref=f"{kind.value}.{_SLUG_STRIP.sub('_', title.lower()).strip('_')}",
        )
        for index, title in enumerate(titles, start=1)
    )


def _ordered(entities: Iterable[Entity]) -> tuple[Entity, ...]:
    return tuple(sorted(entities, key=lambda item: (item.name, item.id.value)))


def _system_documents(query: KnowledgeQuery) -> tuple[DocumentPlan, ...]:
    found = _ordered(query.entities(EntityKind.SYSTEM, limit=PLAN_LIMIT))
    return tuple(
        DocumentPlan(
            document_id=document_slug("sistema", entity.name, entity.id.value),
            title=f"Visão geral do sistema {entity.name}",
            subject_entity_id=entity.id.value,
            kind=DocumentKind.SYSTEM_OVERVIEW,
            sections=_sections(DocumentKind.SYSTEM_OVERVIEW),
        )
        for entity in found
    )


def _capability_documents(query: KnowledgeQuery) -> tuple[DocumentPlan, ...]:
    found = _ordered(query.entities(EntityKind.CAPABILITY, limit=PLAN_LIMIT))
    return tuple(
        DocumentPlan(
            document_id=document_slug("capacidade", entity.name, entity.id.value),
            title=f"Capacidade {entity.name}",
            subject_entity_id=entity.id.value,
            kind=DocumentKind.CAPABILITY,
            sections=_sections(DocumentKind.CAPABILITY),
        )
        for entity in found
    )


def _catalog_document(
    query: KnowledgeQuery,
    kind: EntityKind,
    document_kind: DocumentKind,
    document_id: str,
    title: str,
) -> tuple[DocumentPlan, ...]:
    if not query.entities(kind, limit=1):
        return ()
    return (
        DocumentPlan(
            document_id=document_id,
            title=title,
            subject_entity_id=kind.value,
            kind=document_kind,
            sections=_sections(document_kind),
        ),
    )


def _gaps_document(query: KnowledgeQuery) -> tuple[DocumentPlan, ...]:
    comparison = query.compare(limit=PLAN_LIMIT)
    if not query.gaps(limit=1) and comparison.total == 0:
        return ()
    return (
        DocumentPlan(
            document_id=GAPS_DOCUMENT_ID,
            title="Relatório de lacunas de conhecimento",
            subject_entity_id=EntityKind.GAP.value,
            kind=DocumentKind.GAPS_REPORT,
            sections=_sections(DocumentKind.GAPS_REPORT),
        ),
    )


def _change_impact_documents(query: KnowledgeQuery) -> tuple[DocumentPlan, ...]:
    found: list[Entity] = []
    for kind in _CHANGE_KINDS:
        found.extend(query.entities(kind, limit=PLAN_LIMIT))
    return tuple(
        DocumentPlan(
            document_id=document_slug("mudanca", entity.name, entity.id.value),
            title=f"Impacto da mudança {entity.name}",
            subject_entity_id=entity.id.value,
            kind=DocumentKind.CHANGE_IMPACT,
            sections=_sections(DocumentKind.CHANGE_IMPACT),
        )
        for entity in _ordered(found)
    )


def plan(query: KnowledgeQuery, namespace: str) -> PublicationPlan:
    documents: list[DocumentPlan] = []
    documents.extend(_system_documents(query))
    documents.extend(_capability_documents(query))
    documents.extend(
        _catalog_document(
            query,
            EntityKind.INTEGRATION,
            DocumentKind.INTEGRATION_CATALOG,
            INTEGRATION_CATALOG_ID,
            "Catálogo de integrações",
        )
    )
    documents.extend(
        _catalog_document(
            query,
            EntityKind.BUSINESS_RULE,
            DocumentKind.RULES_CATALOG,
            RULES_CATALOG_ID,
            "Catálogo de regras de negócio",
        )
    )
    documents.extend(_gaps_document(query))
    documents.extend(_change_impact_documents(query))
    if not documents:
        return PublicationPlan()
    ordered = sorted(documents, key=lambda item: (item.kind.value, item.document_id))
    return PublicationPlan(documents=tuple(ordered))
