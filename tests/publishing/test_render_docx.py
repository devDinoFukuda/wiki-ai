from __future__ import annotations

from wiki_ai.publishing.docx.blocks import CodeBlock, Heading, TableOfContents
from wiki_ai.publishing.docx.inspect import read_package
from wiki_ai.publishing.model import CAPABILITY_SECTIONS, DocumentKind
from wiki_ai.publishing.narrative import NarrativeBuilder
from wiki_ai.publishing.planner import plan
from wiki_ai.publishing.render_docx import (
    MERMAID_LANGUAGE,
    docx_filename,
    document_blocks,
    render_document,
)
from wiki_ai.publishing.render_markdown import markdown_for


def _capability_document(query, subject="Renovacao"):
    for entry in plan(query, "ns").of_kind(DocumentKind.CAPABILITY):
        document = NarrativeBuilder(query).build(entry)
        if document.properties is not None and document.properties.subject == subject:
            return document
    raise AssertionError("capacidade ausente do plano")


def test_document_starts_with_title_and_toc(query):
    blocks = document_blocks(_capability_document(query))
    assert isinstance(blocks[0], Heading)
    assert isinstance(blocks[1], TableOfContents)


def test_diagrams_render_as_mermaid_code_blocks(query):
    blocks = document_blocks(_capability_document(query))
    code = [b for b in blocks if isinstance(b, CodeBlock)]
    assert code
    assert all(b.language == MERMAID_LANGUAGE for b in code)


def test_filename_is_sharepoint_safe(query):
    name = docx_filename(_capability_document(query))
    assert name.endswith(".docx")
    assert " " not in name
    assert name == name.lower()


def test_package_opens_and_carries_every_section(query, tmp_path):
    document = _capability_document(query)
    path = render_document(document, tmp_path / docx_filename(document))
    summary = read_package(path)
    for number, title in enumerate(CAPABILITY_SECTIONS, start=1):
        assert f"{number}. {title}" in summary.text


def test_package_properties_reach_sharepoint_fields(query, tmp_path):
    document = _capability_document(query)
    summary = read_package(render_document(document, tmp_path / docx_filename(document)))
    assert summary.properties.title == document.title
    assert summary.properties.subject == "Renovacao"
    assert summary.properties.category == DocumentKind.CAPABILITY.value
    assert summary.properties.keywords
    assert summary.properties.description.startswith("Objetivo:")


def test_render_is_byte_identical_for_the_same_document(query, tmp_path):
    document = _capability_document(query)
    first = render_document(document, tmp_path / "a.docx").read_bytes()
    second = render_document(document, tmp_path / "b.docx").read_bytes()
    assert first == second


def test_markdown_export_mirrors_the_sections(query):
    text = markdown_for(_capability_document(query))
    for number, title in enumerate(CAPABILITY_SECTIONS, start=1):
        assert f"## {number}. {title}" in text
    assert "```mermaid" in text
