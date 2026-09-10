from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from wiki_ai.knowledge.evidence import (
    CodeLocator,
    DiagramLocator,
    DocumentLocator,
    SpreadsheetLocator,
    TranscriptLocator,
)
from wiki_ai.knowledge.model import Confidence, Entity, KnowledgeState, Locator
from wiki_ai.knowledge.query import CapabilityProfile, KnowledgeQuery
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind

from .model import (
    Assertion,
    AssertionStance,
    DiagramSpec,
    DocumentKind,
    NarrativeBlock,
    NarrativeKind,
    TraceEntry,
)

__all__ = [
    "NARRATIVE_LIMIT",
    "EMPTY_NOTE",
    "NarrativeEnricher",
    "state_phrase",
    "stance",
    "statement_of",
    "describe",
    "trace_entries",
    "evidence_ids",
    "section",
    "paragraph",
    "bullets",
    "table",
    "diagram_block",
    "listing",
    "gap_section",
    "trace_table",
    "edge_cases",
    "flow_block",
    "members",
    "of_kind",
    "unique",
    "all_profile_entities",
    "empty_profile",
    "keywords",
    "usable",
]

NARRATIVE_LIMIT = 500

_STATE_PHRASES: dict[tuple[str, str], str] = {
    (KnowledgeState.IMPLEMENTED.value, Confidence.SUPPORTED.value): (
        "implementado e verificado no código"
    ),
    (KnowledgeState.IMPLEMENTED.value, Confidence.INFERRED.value): (
        "implementado, inferido a partir do código sem verificação direta"
    ),
    (KnowledgeState.IMPLEMENTED.value, Confidence.UNRESOLVED.value): (
        "não resolvido"
    ),
    (KnowledgeState.IMPLEMENTED.value, Confidence.CONTRADICTED.value): (
        "contraditório entre fontes"
    ),
    (KnowledgeState.DECLARED.value, Confidence.SUPPORTED.value): (
        "declarado em documento e confirmado pela fonte"
    ),
    (KnowledgeState.DECLARED.value, Confidence.INFERRED.value): (
        "declarado em documento, sem implementação encontrada"
    ),
    (KnowledgeState.DECLARED.value, Confidence.UNRESOLVED.value): "não resolvido",
    (KnowledgeState.DECLARED.value, Confidence.CONTRADICTED.value): (
        "contraditório entre fontes"
    ),
    (KnowledgeState.PROPOSED.value, Confidence.SUPPORTED.value): "proposto",
    (KnowledgeState.PROPOSED.value, Confidence.INFERRED.value): "proposto",
    (KnowledgeState.PROPOSED.value, Confidence.UNRESOLVED.value): "não resolvido",
    (KnowledgeState.PROPOSED.value, Confidence.CONTRADICTED.value): (
        "contraditório entre fontes"
    ),
    (KnowledgeState.HISTORICAL.value, Confidence.SUPPORTED.value): (
        "registro histórico, substituído por decisão posterior"
    ),
    (KnowledgeState.HISTORICAL.value, Confidence.INFERRED.value): (
        "registro histórico, substituído por decisão posterior"
    ),
    (KnowledgeState.HISTORICAL.value, Confidence.UNRESOLVED.value): "não resolvido",
    (KnowledgeState.HISTORICAL.value, Confidence.CONTRADICTED.value): (
        "contraditório entre fontes"
    ),
}

_RESERVED_CONFIDENCE = frozenset(
    {Confidence.UNRESOLVED.value, Confidence.CONTRADICTED.value}
)

_STATEMENT_ATTRIBUTES: tuple[str, ...] = (
    "statement",
    "decision",
    "behaviour",
    "criteria",
    "rule",
    "question",
)


@runtime_checkable
class NarrativeEnricher(Protocol):
    def enrich(
        self, document_kind: DocumentKind, section_title: str, assertion: Assertion
    ) -> Assertion: ...


def state_phrase(entity: Entity) -> str:
    return _STATE_PHRASES[(entity.state.value, entity.confidence.value)]


def stance(entity: Entity) -> AssertionStance:
    if entity.confidence.value in _RESERVED_CONFIDENCE:
        return AssertionStance.RESERVED
    return AssertionStance.FACT


def statement_of(entity: Entity) -> str:
    for name in _STATEMENT_ATTRIBUTES:
        value = entity.attributes.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return entity.name


def describe(entity: Entity, prefix: str = "") -> Assertion:
    body = statement_of(entity)
    if prefix:
        text = f"{prefix} {body}"
    else:
        text = body
    return Assertion(
        text=text, stance=stance(entity), qualifier=state_phrase(entity)
    )


def _locator_columns(locator: Locator) -> tuple[str, str, str]:
    if isinstance(locator, CodeLocator):
        symbol = f" ({locator.symbol})" if locator.symbol else ""
        return (
            "código",
            f"{locator.path}{symbol}",
            f"linhas {locator.line_start}-{locator.line_end}",
        )
    if isinstance(locator, SpreadsheetLocator):
        return ("planilha", locator.workbook, f"{locator.worksheet}!{locator.cell_range}")
    if isinstance(locator, TranscriptLocator):
        return (
            "transcrição",
            locator.speaker,
            f"{locator.time_start:.1f}s-{locator.time_end:.1f}s",
        )
    if isinstance(locator, DiagramLocator):
        return ("diagrama", locator.diagram, f"{locator.page}/{locator.node}")
    if isinstance(locator, DocumentLocator):
        heading = " > ".join(locator.heading_path)
        return ("documento", locator.block_id or heading, heading or locator.block_id or "")
    return (locator.kind, "", "")


def trace_entries(
    query: KnowledgeQuery, entities: Sequence[Entity]
) -> tuple[TraceEntry, ...]:
    found: list[TraceEntry] = []
    seen: set[str] = set()
    for entity in entities:
        for evidence in query.evidence_of(entity.id, limit=NARRATIVE_LIMIT):
            key = f"{entity.id.value}:{evidence.id}"
            if key in seen:
                continue
            seen.add(key)
            kind, primary, detail = _locator_columns(evidence.locator)
            found.append(
                TraceEntry(
                    finding=f"{entity.name} — {state_phrase(entity)}",
                    evidence=evidence.id,
                    source_version=f"{evidence.source_id}@{evidence.version_hash}",
                    locator_kind=kind,
                    locator_primary=primary,
                    locator_detail=detail,
                )
            )
    return tuple(sorted(found, key=lambda item: (item.finding, item.evidence)))


def evidence_ids(query: KnowledgeQuery, entities: Sequence[Entity]) -> tuple[str, ...]:
    found: list[str] = []
    for entity in entities:
        for evidence in query.evidence_of(entity.id, limit=NARRATIVE_LIMIT):
            if evidence.id not in found:
                found.append(evidence.id)
    return tuple(sorted(found))


def section(number: int, title: str) -> NarrativeBlock:
    return NarrativeBlock(kind=NarrativeKind.SECTION, section_number=number, title=title)


def paragraph(number: int, assertions: Sequence[Assertion]) -> NarrativeBlock:
    return NarrativeBlock(
        kind=NarrativeKind.PARAGRAPH, section_number=number, assertions=tuple(assertions)
    )


def bullets(number: int, assertions: Sequence[Assertion]) -> NarrativeBlock:
    return NarrativeBlock(
        kind=NarrativeKind.BULLETS, section_number=number, assertions=tuple(assertions)
    )


def table(number: int, rows: Sequence[Sequence[str]]) -> NarrativeBlock:
    return NarrativeBlock(
        kind=NarrativeKind.TABLE,
        section_number=number,
        rows=tuple(tuple(row) for row in rows),
    )


def diagram_block(number: int, index: int) -> NarrativeBlock:
    return NarrativeBlock(
        kind=NarrativeKind.DIAGRAM, section_number=number, diagram_index=index
    )


EMPTY_NOTE = "Nenhum item com evidência registrada para este tópico"


def listing(number: int, entities: Sequence[Entity], prefix: str = "") -> NarrativeBlock:
    facts = [
        describe(entity, prefix)
        for entity in entities
        if stance(entity) is AssertionStance.FACT
    ]
    if not facts:
        return paragraph(
            number, (Assertion(text=EMPTY_NOTE, stance=AssertionStance.OPEN),)
        )
    return bullets(number, facts)



def usable(candidate: Assertion, fallback: Assertion) -> bool:
    return (
        isinstance(candidate, Assertion)
        and bool(str(candidate.text).strip())
        and candidate.stance is fallback.stance
        and candidate.qualifier == fallback.qualifier
    )


def gap_section(items: Sequence[Assertion], number: int) -> NarrativeBlock:
    if items:
        return bullets(number, items)
    return paragraph(
        number,
        (
            Assertion(
                text="Nenhuma lacuna aberta registrada sobre este assunto",
                stance=AssertionStance.OPEN,
            ),
        ),
    )


def trace_table(
    query: KnowledgeQuery, entities: Sequence[Entity], number: int
) -> NarrativeBlock:
    entries = trace_entries(query, entities)
    rows: list[tuple[str, ...]] = [
        ("Finding", "Evidence", "Source version", "Path", "Lines")
    ]
    for entry in entries:
        rows.append(entry.as_row())
    if len(rows) == 1:
        return paragraph(
            number,
            (
                Assertion(
                    text="Nenhuma evidência foi registrada para este documento",
                    stance=AssertionStance.OPEN,
                ),
            ),
        )
    return table(number, rows)


def edge_cases(profile: CapabilityProfile, number: int) -> NarrativeBlock:
    facts: list[Assertion] = []
    for entity in profile.edge_cases:
        if stance(entity) is not AssertionStance.FACT:
            continue
        condition = str(entity.attributes.get("condition", "")).strip()
        expected = str(entity.attributes.get("expected", "")).strip()
        if condition and expected:
            text = f"Quando {condition}, o comportamento esperado é {expected}"
        else:
            text = entity.name
        facts.append(
            Assertion(
                text=text, stance=AssertionStance.FACT, qualifier=state_phrase(entity)
            )
        )
    if not facts:
        return paragraph(
            number, (Assertion(text=EMPTY_NOTE, stance=AssertionStance.OPEN),)
        )
    return bullets(number, facts)


def flow_block(query: KnowledgeQuery, capability: Entity, number: int) -> NarrativeBlock:
    flows = [
        node
        for _relation, node in query.neighbors(
            capability.id, RelationKind.BELONGS_TO, "in", limit=NARRATIVE_LIMIT
        )
        if node.kind == EntityKind.FLOW.value
    ]
    facts: list[Assertion] = []
    for flow in sorted(flows, key=lambda item: (item.name, item.id.value)):
        for step in query.flow(flow.id, limit=NARRATIVE_LIMIT):
            entity = step.entity
            if stance(entity) is not AssertionStance.FACT:
                continue
            facts.append(
                Assertion(
                    text=f"Passo {step.ordinal + 1}: {entity.name}",
                    stance=AssertionStance.FACT,
                    qualifier=state_phrase(entity),
                )
            )
    if not facts:
        return paragraph(
            number,
            (
                Assertion(
                    text="Não há fluxo com passos registrados para esta capacidade",
                    stance=AssertionStance.OPEN,
                ),
            ),
        )
    return bullets(number, facts)


def members(query: KnowledgeQuery, container: Entity) -> tuple[Entity, ...]:
    return tuple(
        node
        for _relation, node in query.neighbors(
            container.id, RelationKind.BELONGS_TO, "in", limit=NARRATIVE_LIMIT
        )
    )


def of_kind(entities: Sequence[Entity], kind: EntityKind) -> tuple[Entity, ...]:
    return tuple(
        sorted(
            (item for item in entities if item.kind == kind.value),
            key=lambda item: (item.name, item.id.value),
        )
    )


def unique(entities: Sequence[Entity]) -> tuple[Entity, ...]:
    seen: set[str] = set()
    found: list[Entity] = []
    for entity in entities:
        if entity.id.value in seen:
            continue
        seen.add(entity.id.value)
        found.append(entity)
    return tuple(sorted(found, key=lambda item: (item.name, item.id.value)))


def all_profile_entities(profile: CapabilityProfile) -> tuple[Entity, ...]:
    every: list[Entity] = []
    for group in profile.sections().values():
        every.extend(group)
    return unique(tuple(every))


def empty_profile(capability: Entity) -> CapabilityProfile:
    return CapabilityProfile(capability=capability)


def keywords(capability: Entity, profile: CapabilityProfile) -> tuple[str, ...]:
    names = [capability.kind, capability.name]
    for group in (profile.integrations, profile.rules, profile.persistence):
        for entity in group[:4]:
            names.append(entity.name)
    return tuple(dict.fromkeys(names))


def _diagrams(query: KnowledgeQuery, subject: Entity) -> tuple[DiagramSpec, ...]:
    return diagrams_for(query, subject)
