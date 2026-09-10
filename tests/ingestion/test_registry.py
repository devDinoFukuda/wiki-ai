from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tests.ingestion import fixtures
from wiki_ai.ingestion.adapters import registry
from wiki_ai.ingestion.source import DiagnosticLevel, SourceKind


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    fixtures.write_docx(tmp_path / "regras.docx")
    fixtures.write_xlsx(tmp_path / "regras.xlsx")
    (tmp_path / "arq.drawio").write_text(fixtures.drawio_document(), encoding="utf-8")
    (tmp_path / "politica.pdf").write_bytes(fixtures.minimal_pdf())
    (tmp_path / "reuniao.vtt").write_text(fixtures.VTT, encoding="utf-8")
    (tmp_path / "reuniao.srt").write_text(fixtures.SRT, encoding="utf-8")
    (tmp_path / "reuniao.txt").write_text(fixtures.TXT_TRANSCRIPT, encoding="utf-8")
    (tmp_path / "guia.md").write_text(fixtures.MARKDOWN, encoding="utf-8")
    (tmp_path / "guia.html").write_text(fixtures.HTML, encoding="utf-8")
    (tmp_path / "dados.json").write_text(fixtures.JSON_DOCUMENT, encoding="utf-8")
    (tmp_path / "dados.xml").write_text(fixtures.XML_DOCUMENT, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("regras.docx", SourceKind.DOCX),
        ("regras.xlsx", SourceKind.XLSX),
        ("arq.drawio", SourceKind.DRAWIO),
        ("politica.pdf", SourceKind.PDF),
        ("reuniao.vtt", SourceKind.TRANSCRIPT),
        ("reuniao.srt", SourceKind.TRANSCRIPT),
        ("reuniao.txt", SourceKind.TRANSCRIPT),
        ("guia.md", SourceKind.MARKDOWN),
        ("guia.html", SourceKind.HTML),
        ("dados.json", SourceKind.JSON),
        ("dados.xml", SourceKind.XML),
    ],
)
def test_detect_resolves_every_supported_kind(corpus: Path, name: str, kind: SourceKind) -> None:
    assert registry.detect(corpus / name) is kind


def test_detect_uses_magic_bytes_over_extension(tmp_path: Path) -> None:
    disguised = tmp_path / "planilha.json"
    fixtures.write_xlsx(disguised)
    assert registry.detect(disguised) is SourceKind.XLSX


def test_detect_separates_docx_from_xlsx_inside_zip(tmp_path: Path) -> None:
    word = tmp_path / "sem_extensao_word"
    sheet = tmp_path / "sem_extensao_sheet"
    fixtures.write_docx(word)
    fixtures.write_xlsx(sheet)
    assert registry.detect(word) is SourceKind.DOCX
    assert registry.detect(sheet) is SourceKind.XLSX


def test_detect_reads_pdf_header(tmp_path: Path) -> None:
    target = tmp_path / "documento.bin"
    target.write_bytes(fixtures.minimal_pdf())
    assert registry.detect(target) is SourceKind.PDF


def test_detect_reads_mxgraphmodel_without_mxfile(tmp_path: Path) -> None:
    target = tmp_path / "modelo.xml"
    target.write_text(fixtures.MX_MODEL, encoding="utf-8")
    assert registry.detect(target) is SourceKind.DRAWIO


def test_detect_reads_webvtt_marker(tmp_path: Path) -> None:
    target = tmp_path / "sem_extensao"
    target.write_text(fixtures.VTT, encoding="utf-8")
    assert registry.detect(target) is SourceKind.TRANSCRIPT


def test_detect_reads_srt_index(tmp_path: Path) -> None:
    target = tmp_path / "legenda"
    target.write_text(fixtures.SRT, encoding="utf-8")
    assert registry.detect(target) is SourceKind.TRANSCRIPT


def test_detect_reads_speaker_lines_without_a_transcript_extension(tmp_path: Path) -> None:
    target = tmp_path / "reuniao"
    target.write_text(fixtures.TXT_TRANSCRIPT, encoding="utf-8")
    assert registry.detect(target) is SourceKind.TRANSCRIPT


def test_unknown_format_yields_error_diagnostic_and_no_blocks(tmp_path: Path) -> None:
    target = tmp_path / "binario.dat"
    target.write_bytes(b"\x00\x01\x02\x03opaque payload")
    document = registry.adapt(target)
    assert document.blocks == ()
    assert [d.code for d in document.diagnostics] == [registry.UNSUPPORTED_FORMAT]
    assert document.diagnostics[0].level is DiagnosticLevel.ERROR
    assert not document.complete


def test_unknown_format_never_raises(tmp_path: Path) -> None:
    target = tmp_path / "vazio.bin"
    target.write_bytes(b"")
    document = registry.adapt(target)
    assert document.errors


def test_missing_file_yields_unreadable_diagnostic(tmp_path: Path) -> None:
    document = registry.adapt(tmp_path / "inexistente.docx")
    assert [d.code for d in document.diagnostics] == [registry.UNREADABLE_SOURCE]


def test_corrupt_zip_named_docx_yields_diagnostic_without_exception(tmp_path: Path) -> None:
    target = tmp_path / "quebrado.docx"
    target.write_bytes(b"PK\x03\x04" + b"\xff" * 200)
    document = registry.adapt(target)
    assert document.blocks == ()
    assert document.source.kind is SourceKind.DOCX
    assert [d.code for d in document.diagnostics] == ["corrupt_archive"]


def test_corrupt_zip_named_xlsx_yields_diagnostic_without_exception(tmp_path: Path) -> None:
    target = tmp_path / "quebrado.xlsx"
    target.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    document = registry.adapt(target)
    assert document.blocks == ()
    assert document.source.kind is SourceKind.XLSX
    assert document.errors


def test_zip_without_office_parts_falls_back_to_extension(tmp_path: Path) -> None:
    target = tmp_path / "avulso.docx"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("readme", "sem partes ooxml")
    assert registry.detect(target) is SourceKind.DOCX
    document = registry.adapt(target)
    assert [d.code for d in document.diagnostics] == ["corrupt_archive"]


def test_adapt_many_walks_a_directory(corpus: Path) -> None:
    documents = registry.adapt_many(corpus)
    kinds = {document.source.kind for document in documents}
    assert kinds == {
        SourceKind.DOCX,
        SourceKind.XLSX,
        SourceKind.DRAWIO,
        SourceKind.PDF,
        SourceKind.TRANSCRIPT,
        SourceKind.MARKDOWN,
        SourceKind.HTML,
        SourceKind.JSON,
        SourceKind.XML,
    }


def test_adapt_many_filters_by_suffix(corpus: Path) -> None:
    documents = registry.adapt_many(corpus, suffixes=[".vtt", ".srt"])
    assert len(documents) == 2
    assert all(d.source.kind is SourceKind.TRANSCRIPT for d in documents)


def test_adapt_many_on_a_file_returns_one_document(corpus: Path) -> None:
    documents = registry.adapt_many(corpus / "guia.md")
    assert len(documents) == 1
    assert documents[0].source.kind is SourceKind.MARKDOWN


def test_drawio_png_container_is_detected_by_name(tmp_path: Path) -> None:
    target = tmp_path / "arquitetura.drawio.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    assert registry.detect(target) is SourceKind.DRAWIO
