from __future__ import annotations

import pytest

from wiki_ai.publishing.model import (
    CAPABILITY_SECTIONS,
    Assertion,
    AssertionStance,
    DocumentKind,
    DocumentPlan,
    PublicationPlan,
    SectionPlan,
)


def _sections(count: int) -> tuple[SectionPlan, ...]:
    return tuple(
        SectionPlan(number=index, title=f"Seção {index}", content_ref=f"ref{index}")
        for index in range(1, count + 1)
    )


def _document(document_id: str = "doc") -> DocumentPlan:
    return DocumentPlan(
        document_id=document_id,
        title="Título",
        subject_entity_id="assunto",
        kind=DocumentKind.CAPABILITY,
        sections=_sections(3),
    )


def test_capability_sections_match_the_specification():
    assert len(CAPABILITY_SECTIONS) == 15
    assert CAPABILITY_SECTIONS[0] == "Objetivo"
    assert CAPABILITY_SECTIONS[5] == "Regras de negócio"
    assert CAPABILITY_SECTIONS[13] == "Lacunas conhecidas"
    assert CAPABILITY_SECTIONS[14] == "Rastreabilidade técnica"


def test_section_numbers_must_start_at_one():
    with pytest.raises(ValueError):
        SectionPlan(number=0, title="Objetivo", content_ref="ref")


def test_document_sections_must_be_contiguous():
    with pytest.raises(ValueError):
        DocumentPlan(
            document_id="doc",
            title="Título",
            subject_entity_id="assunto",
            kind=DocumentKind.CAPABILITY,
            sections=(SectionPlan(number=2, title="Objetivo", content_ref="ref"),),
        )


def test_plan_refuses_duplicate_documents():
    with pytest.raises(ValueError):
        PublicationPlan(documents=(_document(), _document()))


def test_assertion_renders_with_the_qualifier():
    assertion = Assertion(
        text="A renovação é bloqueada",
        stance=AssertionStance.FACT,
        qualifier="implementado e verificado no código",
    )
    assert assertion.rendered() == (
        "A renovação é bloqueada (implementado e verificado no código)."
    )


def test_assertion_without_qualifier_ends_with_a_period():
    assert Assertion(text="Texto simples").rendered() == "Texto simples."
