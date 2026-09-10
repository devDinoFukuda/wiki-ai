from __future__ import annotations

import builtins
from pathlib import Path
from typing import Sequence

import pytest

from tests.ingestion import fixtures
from wiki_ai.ingestion.adapters import pdf, registry
from wiki_ai.ingestion.outcome import Gap, StructuralFault
from wiki_ai.ingestion.source import (
    BlockKind,
    DiagnosticLevel,
    SourceDocument,
    SourceKind,
    locator_violations,
)

class ScriptedExtractor:
    def __init__(
        self, pages: Sequence[pdf.PageText], properties: dict[str, str] | None = None
    ) -> None:
        self.readout = pdf.PdfReadout(pages=tuple(pages), properties=properties or {})
        self.calls: list[str] = []

    def extract(self, path: str | Path, payload: bytes) -> pdf.PdfReadout:
        self.calls.append(str(path))
        return self.readout


class MissingLibraryExtractor:
    def extract(self, path: str | Path, payload: bytes) -> pdf.PdfReadout:
        raise pdf.LibraryUnavailable(pdf.LIBRARY)


@pytest.fixture()
def library() -> object:
    return pytest.importorskip(pdf.LIBRARY)


@pytest.fixture()
def document(library: object, tmp_path: Path) -> SourceDocument:
    target = tmp_path / "politica.pdf"
    target.write_bytes(fixtures.minimal_pdf())
    return registry.adapt(target)


def _properties(document: SourceDocument):
    return next(
        block
        for block in document.blocks
        if block.locator["block"] == "document_properties"
    )


def test_metadata_comes_from_the_library_info_dictionary(
    document: SourceDocument,
) -> None:
    assert _properties(document).attributes["properties"]["Title"] == "Politica de Credito"
    assert _properties(document).attributes["properties"]["Author"] == "Comite"
    assert document.source.metadata["title"] == "Politica de Credito"


def test_page_count_is_reported(document: SourceDocument) -> None:
    assert _properties(document).attributes["pages"] == 1


def test_page_text_becomes_a_paragraph_block(document: SourceDocument) -> None:
    paragraph = next(
        block for block in document.blocks if block.kind is BlockKind.PARAGRAPH
    )
    assert paragraph.text == "Politica de credito aprovada"
    assert paragraph.locator["page"] == 1


def test_text_read_by_the_library_is_high_confidence(document: SourceDocument) -> None:
    paragraph = next(
        block for block in document.blocks if block.kind is BlockKind.PARAGRAPH
    )
    assert paragraph.attributes["confidence"] == pdf.Confidence.HIGH.value


def test_a_readable_pdf_reports_no_gap(document: SourceDocument) -> None:
    assert document.diagnostics == ()


def test_a_page_without_text_yields_the_image_and_ocr_gaps(library: object, tmp_path: Path) -> None:
    target = tmp_path / "digitalizado.pdf"
    target.write_bytes(fixtures.image_only_pdf())
    result = registry.adapt(target)
    codes = [d.code for d in result.diagnostics]
    assert Gap.IMAGE_CONTENT_NOT_INTERPRETED.value in codes
    assert Gap.OCR_UNAVAILABLE.value in codes
    assert all(d.level is DiagnosticLevel.WARNING for d in result.diagnostics)
    image = next(block for block in result.blocks if block.kind is BlockKind.IMAGE)
    assert image.attributes["gap"] is True
    assert image.locator["page"] == 1


def test_a_page_without_text_is_a_gap_not_an_error(library: object, tmp_path: Path) -> None:
    target = tmp_path / "digitalizado.pdf"
    target.write_bytes(fixtures.image_only_pdf())
    assert registry.adapt(target).errors == ()


def test_a_payload_without_the_pdf_header_is_malformed(tmp_path: Path) -> None:
    target = tmp_path / "falso.pdf"
    target.write_bytes(b"nao sou um pdf")
    result = pdf.adapt(target, target.read_bytes())
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == [
        StructuralFault.MALFORMED_DOCUMENT.value
    ]


def test_a_truncated_pdf_the_library_rejects_is_malformed(library: object, tmp_path: Path) -> None:
    target = tmp_path / "cortado.pdf"
    target.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog")
    result = pdf.adapt(target, target.read_bytes())
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == [
        StructuralFault.MALFORMED_DOCUMENT.value
    ]


def test_a_missing_library_blocks_with_a_typed_diagnostic(tmp_path: Path) -> None:
    target = tmp_path / "politica.pdf"
    target.write_bytes(fixtures.minimal_pdf())
    result = pdf.adapt(
        target, target.read_bytes(), extractor=MissingLibraryExtractor()
    )
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == [
        StructuralFault.PDF_LIBRARY_UNAVAILABLE.value
    ]
    assert result.diagnostics[0].level is DiagnosticLevel.ERROR


def test_the_real_extractor_reports_the_library_as_unavailable_when_import_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_import = builtins.__import__

    def blocked(name: str, *args, **kwargs):
        if name == pdf.LIBRARY:
            raise ModuleNotFoundError(f"No module named {pdf.LIBRARY!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(__import__("sys").modules, pdf.LIBRARY, raising=False)
    monkeypatch.setattr(builtins, "__import__", blocked)
    target = tmp_path / "politica.pdf"
    payload = fixtures.minimal_pdf()
    target.write_bytes(payload)
    with pytest.raises(pdf.LibraryUnavailable):
        pdf.LibraryTextExtractor().extract(target, payload)
    result = pdf.adapt(target, payload)
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == [
        StructuralFault.PDF_LIBRARY_UNAVAILABLE.value
    ]


def test_an_injected_extractor_replaces_the_library_reader(tmp_path: Path) -> None:
    target = tmp_path / "politica.pdf"
    target.write_bytes(fixtures.minimal_pdf())
    engine = ScriptedExtractor(
        [
            pdf.PageText(page=1, text="pagina um", confidence=pdf.Confidence.HIGH),
            pdf.PageText(page=2, text="pagina dois", confidence=pdf.Confidence.HIGH),
        ],
        properties={"Title": "Digitalizado a mao"},
    )
    result = pdf.adapt(target, target.read_bytes(), extractor=engine)
    paragraphs = [
        block for block in result.blocks if block.kind is BlockKind.PARAGRAPH
    ]
    assert [block.text for block in paragraphs] == ["pagina um", "pagina dois"]
    assert {block.attributes["confidence"] for block in paragraphs} == {
        pdf.Confidence.HIGH.value
    }
    assert engine.calls == [str(target)]
    assert result.source.metadata["title"] == "Digitalizado a mao"


def test_an_injected_extractor_satisfies_the_protocol() -> None:
    assert isinstance(ScriptedExtractor([]), pdf.PdfTextExtractor)
    assert isinstance(pdf.LibraryTextExtractor(), pdf.PdfTextExtractor)


def test_an_extractor_returning_no_page_reports_no_text_layer(tmp_path: Path) -> None:
    target = tmp_path / "politica.pdf"
    target.write_bytes(fixtures.minimal_pdf())
    result = pdf.adapt(target, target.read_bytes(), extractor=ScriptedExtractor([]))
    assert [d.code for d in result.diagnostics] == [Gap.NO_TEXT_LAYER.value]


def test_every_block_satisfies_the_kernel_locator_contract(
    document: SourceDocument,
) -> None:
    assert all(
        locator_violations(SourceKind.PDF, block.locator) == ()
        for block in document.blocks
    )
