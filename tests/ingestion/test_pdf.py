from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from tests.ingestion import fixtures
from wiki_ai.ingestion.adapters import pdf, registry
from wiki_ai.ingestion.source import (
    BlockKind,
    DiagnosticLevel,
    SourceDocument,
    SourceKind,
    locator_violations,
)


class ScriptedExtractor:
    def __init__(self, pages: Sequence[pdf.PageText]) -> None:
        self.pages = tuple(pages)
        self.calls: list[str] = []

    def extract(self, path: str | Path) -> Sequence[pdf.PageText]:
        self.calls.append(str(path))
        return self.pages


@pytest.fixture()
def document(tmp_path: Path) -> SourceDocument:
    target = tmp_path / "politica.pdf"
    target.write_bytes(fixtures.minimal_pdf())
    return registry.adapt(target)


def test_metadata_is_read_from_the_info_dictionary(document: SourceDocument) -> None:
    properties = next(
        block
        for block in document.blocks
        if block.locator["block"] == "document_properties"
    )
    assert properties.attributes["properties"]["Title"] == "Politica de Credito"
    assert properties.attributes["properties"]["Author"] == "Comite"
    assert document.source.metadata["title"] == "Politica de Credito"


def test_page_count_is_reported(document: SourceDocument) -> None:
    properties = next(
        block
        for block in document.blocks
        if block.locator["block"] == "document_properties"
    )
    assert properties.attributes["pages"] == 1


def test_flate_text_stream_is_decompressed_into_a_paragraph(document: SourceDocument) -> None:
    paragraph = next(
        block for block in document.blocks if block.kind is BlockKind.PARAGRAPH
    )
    assert paragraph.text == "Politica de credito aprovada"
    assert paragraph.locator["page"] == 1


def test_extracted_text_is_marked_low_confidence(document: SourceDocument) -> None:
    paragraph = next(
        block for block in document.blocks if block.kind is BlockKind.PARAGRAPH
    )
    assert paragraph.attributes["confidence"] == pdf.Confidence.LOW


def test_readable_pdf_reports_no_gap(document: SourceDocument) -> None:
    assert document.diagnostics == ()


def test_image_only_page_yields_gap_and_ocr_diagnostics(tmp_path: Path) -> None:
    target = tmp_path / "digitalizado.pdf"
    target.write_bytes(fixtures.image_only_pdf())
    result = registry.adapt(target)
    codes = [d.code for d in result.diagnostics]
    assert "image_content_not_interpreted" in codes
    assert pdf.OCR_UNAVAILABLE in codes
    assert all(d.level is DiagnosticLevel.WARNING for d in result.diagnostics)
    image = next(block for block in result.blocks if block.kind is BlockKind.IMAGE)
    assert image.attributes["gap"] is True
    assert image.locator["page"] == 1


def test_image_only_page_is_partial_not_failed(tmp_path: Path) -> None:
    target = tmp_path / "digitalizado.pdf"
    target.write_bytes(fixtures.image_only_pdf())
    result = registry.adapt(target)
    assert result.complete


def test_a_payload_without_the_pdf_header_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "falso.pdf"
    target.write_bytes(b"nao sou um pdf")
    result = pdf.adapt(target, target.read_bytes())
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == [pdf.MALFORMED_DOCUMENT]


def test_an_injected_extractor_replaces_the_stream_reader(tmp_path: Path) -> None:
    target = tmp_path / "politica.pdf"
    target.write_bytes(fixtures.minimal_pdf())
    engine = ScriptedExtractor(
        [
            pdf.PageText(page=1, text="pagina um", confidence=pdf.Confidence.HIGH),
            pdf.PageText(page=2, text="pagina dois", confidence=pdf.Confidence.HIGH),
        ]
    )
    result = pdf.adapt(target, target.read_bytes(), extractor=engine)
    paragraphs = [
        block for block in result.blocks if block.kind is BlockKind.PARAGRAPH
    ]
    assert [block.text for block in paragraphs] == ["pagina um", "pagina dois"]
    assert {block.attributes["confidence"] for block in paragraphs} == {
        pdf.Confidence.HIGH
    }
    assert engine.calls == [str(target)]


def test_an_injected_extractor_satisfies_the_protocol() -> None:
    assert isinstance(ScriptedExtractor([]), pdf.PdfTextExtractor)
    assert isinstance(pdf.StreamTextExtractor(), pdf.PdfTextExtractor)


def test_an_extractor_returning_no_page_reports_no_text_layer(tmp_path: Path) -> None:
    target = tmp_path / "politica.pdf"
    target.write_bytes(fixtures.minimal_pdf())
    result = pdf.adapt(target, target.read_bytes(), extractor=ScriptedExtractor([]))
    assert [d.code for d in result.diagnostics] == [pdf.NO_TEXT_LAYER]


def test_every_block_satisfies_the_kernel_locator_contract(document: SourceDocument) -> None:
    assert all(
        locator_violations(SourceKind.PDF, block.locator) == ()
        for block in document.blocks
    )
