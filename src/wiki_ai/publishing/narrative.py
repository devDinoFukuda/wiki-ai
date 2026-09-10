from __future__ import annotations

from typing import Sequence

from wiki_ai.knowledge.gaps import GAP_BLOCKING, GAP_QUESTION, is_blocking
from wiki_ai.knowledge.model import Entity, EntityId
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind

from .content import (
    EMPTY_NOTE,
    NARRATIVE_LIMIT,
    Assertion,
    AssertionStance,
    NarrativeEnricher,
    all_profile_entities,
    bullets,
    describe,
    diagram_block,
    edge_cases,
    empty_profile,
    state_phrase,
    evidence_ids,
    flow_block,
    gap_section,
    keywords,
    listing,
    members,
    of_kind,
    paragraph,
    section,
    stance,
    statement_of,
    table,
    trace_entries,
    trace_table,
    unique,
    usable,
)
from .diagrams import diagrams_for
from .model import (
    DiagramSpec,
    DocumentKind,
    DocumentPlan,
    DocumentProperties,
    NarrativeBlock,
    NarrativeDocument,
)

__all__ = ["NarrativeBuilder", "NarrativeEnricher", "state_phrase", "trace_entries"]


class NarrativeBuilder:
    def __init__(
        self, query: KnowledgeQuery, enricher: NarrativeEnricher | None = None
    ) -> None:
        self._query = query
        self._enricher = enricher

    def build(self, document: DocumentPlan) -> NarrativeDocument:
        if document.kind is DocumentKind.CAPABILITY:
            return self._capability(document)
        if document.kind is DocumentKind.SYSTEM_OVERVIEW:
            return self._system(document)
        if document.kind is DocumentKind.INTEGRATION_CATALOG:
            return self._catalog(document, EntityKind.INTEGRATION, "integração")
        if document.kind is DocumentKind.RULES_CATALOG:
            return self._catalog(document, EntityKind.BUSINESS_RULE, "regra de negócio")
        if document.kind is DocumentKind.GAPS_REPORT:
            return self._gaps(document)
        return self._change_impact(document)

    def _finish(
        self,
        document: DocumentPlan,
        body: Sequence[NarrativeBlock],
        subject_name: str,
        purpose: str,
        terms: Sequence[str],
        traced: Sequence[Entity],
        diagrams: Sequence[DiagramSpec] = (),
    ) -> NarrativeDocument:
        enriched = tuple(self._apply(document, block) for block in body)
        return NarrativeDocument(
            document_id=document.document_id,
            title=document.title,
            body=enriched,
            traceability=trace_entries(self._query, traced),
            diagrams=tuple(diagrams),
            properties=DocumentProperties(
                title=document.title,
                subject=subject_name,
                category=document.kind.value,
                description=purpose,
                keywords=tuple(terms),
            ),
            entity_ids=tuple(sorted({entity.id.value for entity in traced})),
            evidence_ids=evidence_ids(self._query, traced),
        )

    def _apply(self, document: DocumentPlan, block: NarrativeBlock) -> NarrativeBlock:
        if self._enricher is None or not block.assertions:
            return block
        title = self._title_for(document, block.section_number)
        rebuilt: list[Assertion] = []
        for assertion in block.assertions:
            candidate = self._enricher.enrich(document.kind, title, assertion)
            rebuilt.append(candidate if usable(candidate, assertion) else assertion)
        return NarrativeBlock(
            kind=block.kind,
            section_number=block.section_number,
            title=block.title,
            assertions=tuple(rebuilt),
            rows=block.rows,
            diagram_index=block.diagram_index,
        )

    def _title_for(self, document: DocumentPlan, number: int) -> str:
        for section in document.sections:
            if section.number == number:
                return section.title
        return ""

    def _gap_assertions(self, gaps: Sequence[Entity]) -> tuple[Assertion, ...]:
        found: list[Assertion] = []
        for gap in sorted(gaps, key=lambda item: (item.name, item.id.value)):
            question = str(gap.attributes.get(GAP_QUESTION, gap.name)).strip()
            severity = (
                "bloqueia conclusões sobre o assunto"
                if bool(gap.attributes.get(GAP_BLOCKING))
                else "não bloqueia conclusões"
            )
            found.append(
                Assertion(
                    text=f"Pergunta em aberto: {question}",
                    stance=AssertionStance.OPEN,
                    qualifier=severity,
                )
            )
        return tuple(found)

    def _reserved_assertions(self, entities: Sequence[Entity]) -> tuple[Assertion, ...]:
        found: list[Assertion] = []
        for entity in sorted(entities, key=lambda item: (item.name, item.id.value)):
            if stance(entity) is not AssertionStance.RESERVED:
                continue
            contradiction = self._contradiction_of(entity)
            if contradiction:
                text = (
                    f"Afirmação não confirmada sobre {entity.name}: "
                    f"{statement_of(entity)}; contradiz {contradiction}"
                )
            else:
                text = (
                    f"Afirmação não confirmada sobre {entity.name}: "
                    f"{statement_of(entity)}"
                )
            found.append(
                Assertion(
                    text=text,
                    stance=AssertionStance.OPEN,
                    qualifier=state_phrase(entity),
                )
            )
        return tuple(found)

    def _gaps_about(self, entities: Sequence[Entity]) -> tuple[Entity, ...]:
        found: list[Entity] = []
        for entity in entities:
            for _relation, gap in self._query.neighbors(
                entity.id, RelationKind.AFFECTS, "in", limit=NARRATIVE_LIMIT
            ):
                if gap.kind == EntityKind.GAP.value:
                    found.append(gap)
        return unique(tuple(found))

    def _contradiction_of(self, entity: Entity) -> str:
        names: list[str] = []
        for direction in ("out", "in"):
            for _relation, other in self._query.neighbors(
                entity.id, RelationKind.CONTRADICTS, direction, limit=NARRATIVE_LIMIT
            ):
                if other.name not in names:
                    names.append(other.name)
        return ", ".join(sorted(names))

    def _capability(self, document: DocumentPlan) -> NarrativeDocument:
        subject_id = EntityId(document.subject_entity_id)
        profile = self._query.capability_profile(subject_id)
        if profile is None:
            return self._finish(document, (), document.title, document.title, (), ())
        capability = profile.capability
        diagrams = diagrams_for(self._query, capability)
        body: list[NarrativeBlock] = []
        body.append(section(1, "Objetivo"))
        body.append(
            paragraph(
                1,
                (
                    Assertion(
                        text=(
                            f"Esta capacidade responde por {capability.name} dentro do "
                            "sistema analisado"
                        ),
                        stance=stance(capability),
                        qualifier=state_phrase(capability),
                    ),
                ),
            )
        )
        body.append(section(2, "Como funciona"))
        body.append(listing(2, profile.entrypoints, "O acionamento acontece por"))
        body.append(section(3, "Fluxo principal"))
        body.append(flow_block(self._query, capability, 3))
        body.append(section(4, "Inputs"))
        body.append(listing(4, profile.inputs, "Recebe"))
        body.append(section(5, "Outputs"))
        body.append(listing(5, profile.outputs, "Produz"))
        body.append(section(6, "Regras de negócio"))
        body.append(listing(6, profile.rules))
        body.append(section(7, "Invariantes"))
        body.append(
            listing(7, profile.invariants + profile.preconditions, "Deve valer que")
        )
        body.append(section(8, "Edge cases"))
        body.append(edge_cases(profile, 8))
        body.append(section(9, "Integrações"))
        body.append(listing(9, profile.integrations, "Depende de"))
        body.append(section(10, "Persistência"))
        body.append(listing(10, profile.persistence, "Grava em"))
        body.append(section(11, "Falhas e recuperação"))
        body.append(
            listing(
                11,
                profile.failures + profile.retries + profile.fallbacks + profile.timeouts,
            )
        )
        body.append(section(12, "Diagramas"))
        for index in range(len(diagrams)):
            body.append(diagram_block(12, index))
        if not diagrams:
            body.append(
                paragraph(
                    12,
                    (
                        Assertion(
                            text=(
                                "Não há dados suficientes para representar este "
                                "assunto graficamente"
                            ),
                            stance=AssertionStance.OPEN,
                        ),
                    ),
                )
            )
        body.append(section(13, "Testes e evidências"))
        body.append(listing(13, profile.tests, "É coberto pelo cenário"))
        body.append(section(14, "Lacunas conhecidas"))
        open_items = self._gap_assertions(profile.gaps) + self._reserved_assertions(
            all_profile_entities(profile)
        )
        body.append(
            bullets(14, open_items)
            if open_items
            else paragraph(
                14,
                (
                    Assertion(
                        text="Nenhuma lacuna aberta registrada sobre este assunto",
                        stance=AssertionStance.OPEN,
                    ),
                ),
            )
        )
        body.append(section(15, "Rastreabilidade técnica"))
        traced = (capability,) + all_profile_entities(profile)
        body.append(trace_table(self._query, traced, 15))
        return self._finish(
            document,
            body,
            capability.name,
            f"Objetivo: descrever a capacidade {capability.name}",
            keywords(capability, profile),
            traced,
            diagrams,
        )

    def _system(self, document: DocumentPlan) -> NarrativeDocument:
        subject_id = EntityId(document.subject_entity_id)
        system = self._query.repository.get_entity(subject_id)
        if system is None:
            return self._finish(document, (), document.title, document.title, (), ())
        owned = members(self._query, system)
        capabilities = of_kind(owned, EntityKind.CAPABILITY)
        modules = of_kind(owned, EntityKind.MODULE)
        for module in modules:
            capabilities = capabilities + of_kind(
                members(self._query, module), EntityKind.CAPABILITY
            )
        integrations = tuple(
            node
            for capability in capabilities
            for node in of_kind(members(self._query, capability), EntityKind.INTEGRATION)
        )
        persistence = tuple(
            node
            for capability in capabilities
            for node in (self._query.capability_profile(capability.id) or empty_profile(capability)).persistence
        )
        diagrams = diagrams_for(self._query, system)
        body: list[NarrativeBlock] = []
        body.append(section(1, "Objetivo"))
        body.append(
            paragraph(
                1,
                (
                    Assertion(
                        text=f"O sistema {system.name} reúne as capacidades descritas a seguir",
                        stance=stance(system),
                        qualifier=state_phrase(system),
                    ),
                ),
            )
        )
        body.append(section(2, "Como funciona"))
        body.append(listing(2, capabilities, "O sistema oferece a capacidade"))
        body.append(section(3, "Capacidades"))
        body.append(listing(3, capabilities))
        body.append(section(4, "Módulos"))
        body.append(listing(4, modules, "O código está organizado no módulo"))
        body.append(section(5, "Integrações"))
        body.append(listing(5, unique(integrations), "Conversa com"))
        body.append(section(6, "Persistência"))
        body.append(listing(6, unique(persistence), "Guarda dados em"))
        body.append(section(7, "Diagramas"))
        for index in range(len(diagrams)):
            body.append(diagram_block(7, index))
        if not diagrams:
            body.append(
                paragraph(
                    7,
                    (
                        Assertion(
                            text=(
                                "Não há dados suficientes para representar este "
                                "assunto graficamente"
                            ),
                            stance=AssertionStance.OPEN,
                        ),
                    ),
                )
            )
        body.append(section(8, "Lacunas conhecidas"))
        gaps = tuple(
            gap
            for capability in capabilities
            for gap in (self._query.capability_profile(capability.id) or empty_profile(capability)).gaps
        )
        open_items = self._gap_assertions(unique(gaps)) + self._reserved_assertions(
            unique((system,) + capabilities + modules + integrations)
        )
        body.append(
            bullets(8, open_items)
            if open_items
            else paragraph(
                8,
                (
                    Assertion(
                        text="Nenhuma lacuna aberta registrada sobre este assunto",
                        stance=AssertionStance.OPEN,
                    ),
                ),
            )
        )
        body.append(section(9, "Rastreabilidade técnica"))
        traced = unique((system,) + modules + capabilities + integrations)
        body.append(trace_table(self._query, traced, 9))
        return self._finish(
            document,
            body,
            system.name,
            f"Objetivo: apresentar a visão geral do sistema {system.name}",
            (system.kind, system.name) + tuple(item.name for item in capabilities[:8]),
            traced,
            diagrams,
        )

    def _catalog(
        self, document: DocumentPlan, kind: EntityKind, label: str
    ) -> NarrativeDocument:
        entries = unique(self._query.entities(kind, limit=NARRATIVE_LIMIT))
        body: list[NarrativeBlock] = []
        body.append(section(1, "Objetivo"))
        body.append(
            paragraph(
                1,
                (
                    Assertion(
                        text=(
                            f"Este catálogo reúne cada {label} conhecida sobre o "
                            "conjunto analisado"
                        ),
                        stance=AssertionStance.FACT,
                    ),
                ),
            )
        )
        body.append(section(2, "Catálogo"))
        rows: list[tuple[str, ...]] = [("Nome", "Situação", "Descrição")]
        for entity in entries:
            if stance(entity) is not AssertionStance.FACT:
                continue
            rows.append((entity.name, state_phrase(entity), statement_of(entity)))
        body.append(
            table(2, rows)
            if len(rows) > 1
            else paragraph(2, (Assertion(text=EMPTY_NOTE, stance=AssertionStance.OPEN),))
        )
        body.append(section(3, "Detalhamento"))
        body.append(listing(3, entries))
        body.append(section(4, "Lacunas conhecidas"))
        open_items = self._gap_assertions(
            self._gaps_about(entries)
        ) + self._reserved_assertions(entries)
        body.append(
            bullets(4, open_items)
            if open_items
            else paragraph(
                4,
                (
                    Assertion(
                        text="Nenhuma lacuna aberta registrada sobre este assunto",
                        stance=AssertionStance.OPEN,
                    ),
                ),
            )
        )
        body.append(section(5, "Rastreabilidade técnica"))
        body.append(trace_table(self._query, entries, 5))
        return self._finish(
            document,
            body,
            document.title,
            f"Objetivo: catalogar cada {label} conhecida",
            (kind.value,) + tuple(item.name for item in entries[:12]),
            entries,
        )

    def _gaps(self, document: DocumentPlan) -> NarrativeDocument:
        every = unique(self._query.gaps(limit=NARRATIVE_LIMIT))
        blocking = tuple(gap for gap in every if is_blocking(gap))
        soft = tuple(gap for gap in every if not is_blocking(gap))
        comparison = self._query.compare(limit=NARRATIVE_LIMIT)
        body: list[NarrativeBlock] = []
        body.append(section(1, "Objetivo"))
        body.append(
            paragraph(
                1,
                (
                    Assertion(
                        text=(
                            "Este relatório lista o que ainda não pôde ser afirmado "
                            "com evidência sobre o conjunto analisado"
                        ),
                        stance=AssertionStance.FACT,
                    ),
                ),
            )
        )
        body.append(section(2, "Lacunas bloqueantes"))
        body.append(gap_section(self._gap_assertions(blocking), 2))
        body.append(section(3, "Lacunas não bloqueantes"))
        body.append(gap_section(self._gap_assertions(soft), 3))
        body.append(section(4, "Contradições entre fontes"))
        contradictions = tuple(
            Assertion(
                text=f"{finding.entity_name}: {finding.detail}",
                stance=AssertionStance.OPEN,
                qualifier=finding.category.replace("_", " "),
            )
            for finding in comparison.source_contradicts_source
            + comparison.proposal_conflicts
            + comparison.declared_not_implemented
        )
        body.append(gap_section(contradictions, 4))
        body.append(section(5, "Lacunas conhecidas"))
        body.append(gap_section(self._gap_assertions(every), 5))
        body.append(section(6, "Rastreabilidade técnica"))
        body.append(trace_table(self._query, every, 6))
        return self._finish(
            document,
            body,
            document.title,
            "Objetivo: registrar o que ainda não possui evidência",
            (EntityKind.GAP.value,) + tuple(gap.name for gap in every[:12]),
            every,
        )

    def _change_impact(self, document: DocumentPlan) -> NarrativeDocument:
        subject_id = EntityId(document.subject_entity_id)
        change = self._query.repository.get_entity(subject_id)
        if change is None:
            return self._finish(document, (), document.title, document.title, (), ())
        targets = tuple(
            node
            for _relation, node in self._query.neighbors(
                change.id, RelationKind.PROPOSES_CHANGE_TO, "out", limit=NARRATIVE_LIMIT
            )
        )
        impacted: list[Entity] = []
        for target in targets:
            impacted.extend(self._query.impact(target.id, limit=NARRATIVE_LIMIT).impacted)
        impacted_unique = unique(tuple(impacted))
        comparison = self._query.compare(limit=NARRATIVE_LIMIT)
        diagrams = diagrams_for(self._query, change)
        body: list[NarrativeBlock] = []
        body.append(section(1, "Objetivo"))
        body.append(
            paragraph(
                1,
                (
                    Assertion(
                        text=(
                            f"Esta mudança propõe {statement_of(change)}"
                        ),
                        stance=stance(change),
                        qualifier=state_phrase(change),
                    ),
                ),
            )
        )
        body.append(section(2, "O que muda"))
        body.append(listing(2, targets, "A mudança incide sobre"))
        body.append(section(3, "O que é afetado"))
        body.append(
            listing(3, impacted_unique, "Muda o comportamento observado em")
            if impacted_unique
            else paragraph(
                3,
                (
                    Assertion(
                        text=(
                            "Nenhum outro elemento com evidência registrada depende "
                            "do que muda"
                        ),
                        stance=AssertionStance.OPEN,
                    ),
                ),
            )
        )
        body.append(section(4, "Comportamento implementado versus decisão registrada"))
        rows: list[tuple[str, ...]] = [("Assunto", "Situação", "Detalhe")]
        for finding in comparison.all_findings():
            rows.append(
                (
                    finding.entity_name,
                    finding.category.replace("_", " "),
                    finding.detail,
                )
            )
        body.append(
            table(4, rows)
            if len(rows) > 1
            else paragraph(
                4,
                (
                    Assertion(
                        text=(
                            "Não há divergência registrada entre o implementado e o "
                            "decidido"
                        ),
                        stance=AssertionStance.OPEN,
                    ),
                ),
            )
        )
        body.append(section(5, "Diagramas"))
        for index in range(len(diagrams)):
            body.append(diagram_block(5, index))
        if not diagrams:
            body.append(
                paragraph(
                    5,
                    (
                        Assertion(
                            text=(
                                "Não há dados suficientes para representar este "
                                "assunto graficamente"
                            ),
                            stance=AssertionStance.OPEN,
                        ),
                    ),
                )
            )
        body.append(section(6, "Lacunas conhecidas"))
        traced = unique((change,) + targets + impacted_unique)
        open_items = self._gap_assertions(
            self._gaps_about(traced)
        ) + self._reserved_assertions(traced)
        body.append(gap_section(open_items, 6))
        body.append(section(7, "Rastreabilidade técnica"))
        body.append(trace_table(self._query, traced, 7))
        return self._finish(
            document,
            body,
            change.name,
            f"Objetivo: medir o impacto de {change.name}",
            (change.kind, change.name) + tuple(item.name for item in targets[:8]),
            traced,
            diagrams,
        )
