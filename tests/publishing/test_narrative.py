from __future__ import annotations

import re

from wiki_ai.publishing.model import (
    Assertion,
    AssertionStance,
    DocumentKind,
    GAPS_SECTION_TITLE,
    NarrativeKind,
    TRACEABILITY_SECTION_TITLE,
)
from wiki_ai.publishing.narrative import NarrativeBuilder
from wiki_ai.publishing.planner import plan

_INTERNAL_ID = re.compile(r"(ent_|rel_|evd_|srcv_)[0-9a-f]{8,}")


def _document(query, kind, index=0):
    entries = plan(query, "ns").of_kind(kind)
    return NarrativeBuilder(query).build(entries[index])


def _body_text(document) -> str:
    parts: list[str] = []
    for block in document.body:
        if block.kind is NarrativeKind.SECTION:
            if block.title == TRACEABILITY_SECTION_TITLE:
                break
            parts.append(block.title)
        parts.extend(item.rendered() for item in block.assertions)
        parts.extend(" ".join(row) for row in block.rows)
    return "\n".join(parts)


def _capability_named(query, name):
    for entry in plan(query, "ns").of_kind(DocumentKind.CAPABILITY):
        document = NarrativeBuilder(query).build(entry)
        if document.properties is not None and document.properties.subject == name:
            return document
    raise AssertionError(f"capacidade {name} ausente do plano")


def test_capability_document_carries_fifteen_sections(query):
    document = _capability_named(query, "Renovacao")
    assert len(document.section_titles()) == 15
    assert document.section_titles()[0] == "Objetivo"
    assert document.section_titles()[-1] == TRACEABILITY_SECTION_TITLE


def test_body_has_no_internal_identifiers(query):
    for kind in DocumentKind:
        entries = plan(query, "ns").of_kind(kind)
        for entry in entries:
            document = NarrativeBuilder(query).build(entry)
            assert not _INTERNAL_ID.search(_body_text(document))


def test_assertions_carry_epistemic_language(query):
    document = _capability_named(query, "Renovacao")
    rendered = _body_text(document)
    assert "implementado" in rendered


def test_declared_capability_is_marked_as_declared(query):
    document = _capability_named(query, "Renovacao automatica")
    assert "declarado em documento, sem implementação encontrada" in _body_text(document)


def test_open_gaps_land_in_the_gaps_section(query):
    document = _capability_named(query, "Renovacao")
    numbers = {
        block.section_number
        for block in document.body
        if block.kind is NarrativeKind.SECTION and block.title == GAPS_SECTION_TITLE
    }
    assert numbers
    number = numbers.pop()
    texts = [
        item.rendered()
        for block in document.body
        if block.section_number == number
        for item in block.assertions
    ]
    assert any("Qual a janela de renovacao?" in text for text in texts)


def test_unresolved_assertions_never_appear_as_fact(query):
    document = _capability_named(query, "Renovacao")
    for block in document.body:
        for item in block.assertions:
            if item.stance is AssertionStance.FACT:
                assert "não resolvido" not in item.qualifier
                assert "contraditório" not in item.qualifier


def test_traceability_lists_paths_and_lines(query):
    document = _capability_named(query, "Renovacao")
    assert document.traceability
    entry = document.traceability[0]
    assert entry.locator_kind == "código"
    assert entry.locator_primary.endswith(")")
    assert entry.locator_detail.startswith("linhas ")
    assert "@" in entry.source_version


def test_properties_describe_the_document(query):
    document = _capability_named(query, "Renovacao")
    assert document.properties is not None
    assert document.properties.category == DocumentKind.CAPABILITY.value
    assert document.properties.subject == "Renovacao"
    assert document.properties.description.startswith("Objetivo:")
    assert "Renovacao" in document.properties.keywords


def test_gaps_report_separates_blocking_from_soft(query):
    document = _document(query, DocumentKind.GAPS_REPORT)
    titles = document.section_titles()
    assert "Lacunas bloqueantes" in titles
    assert "Lacunas não bloqueantes" in titles


def test_change_impact_reports_comparison(query):
    document = _document(query, DocumentKind.CHANGE_IMPACT)
    assert "Comportamento implementado versus decisão registrada" in (
        document.section_titles()
    )


def test_enricher_fallback_keeps_deterministic_text(query):
    class Broken:
        def enrich(self, document_kind, section_title, assertion):
            return Assertion(text="", stance=assertion.stance, qualifier="outro")

    entry = plan(query, "ns").of_kind(DocumentKind.CAPABILITY)[0]
    plain = NarrativeBuilder(query).build(entry)
    enriched = NarrativeBuilder(query, Broken()).build(entry)
    assert _body_text(plain) == _body_text(enriched)


def test_enricher_may_rewrite_prose(query):
    class Louder:
        def enrich(self, document_kind, section_title, assertion):
            return Assertion(
                text=assertion.text.upper(),
                stance=assertion.stance,
                qualifier=assertion.qualifier,
            )

    entry = plan(query, "ns").of_kind(DocumentKind.CAPABILITY)[0]
    enriched = NarrativeBuilder(query, Louder()).build(entry)
    assert "ESTA CAPACIDADE" in _body_text(enriched).upper()
