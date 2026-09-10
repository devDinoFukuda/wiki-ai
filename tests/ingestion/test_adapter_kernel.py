from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.ingestion import fixtures
from wiki_ai.ingestion.adapters import cellref, documents, ooxml, sheet
from wiki_ai.ingestion.adapters.blocks import BlockBuilder
from wiki_ai.ingestion.adapters.xmlsafe import XmlNotAllowed, parse_defused
from wiki_ai.ingestion.source import BlockKind, DiagnosticLevel, SourceKind

MOMENT = datetime(2026, 9, 10, tzinfo=timezone.utc)


def test_version_hash_depends_only_on_the_bytes() -> None:
    assert documents.version_hash(b"abc") == documents.version_hash(b"abc")
    assert documents.version_hash(b"abc") != documents.version_hash(b"abd")


def test_build_source_is_deterministic_for_the_same_inputs(tmp_path: Path) -> None:
    target = tmp_path / "guia.md"
    target.write_text("conteudo", encoding="utf-8")
    first, _ = documents.build_source(
        target, SourceKind.MARKDOWN, b"conteudo", {"title": "Guia"}, MOMENT
    )
    second, _ = documents.build_source(
        target, SourceKind.MARKDOWN, b"conteudo", {"title": "Guia"}, MOMENT
    )
    assert first.id == second.id
    assert first.uri == target.resolve().as_uri()


def test_build_source_reports_metadata_outside_the_whitelist(tmp_path: Path) -> None:
    target = tmp_path / "guia.md"
    target.write_text("conteudo", encoding="utf-8")
    _, found = documents.build_source(
        target, SourceKind.MARKDOWN, b"conteudo", {"policy": "allow"}, MOMENT
    )
    assert [d.code for d in found] == ["metadata.instruction_attempt"]


def test_empty_document_carries_one_diagnostic_and_no_block(tmp_path: Path) -> None:
    target = tmp_path / "opaco.bin"
    target.write_bytes(b"opaco")
    result = documents.empty_document(
        target, SourceKind.JSON, b"opaco", "unsupported_format", "sem adapter", captured_at=MOMENT
    )
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == ["unsupported_format"]
    assert result.diagnostics[0].level is DiagnosticLevel.ERROR


def test_block_builder_numbers_blocks_in_insertion_order() -> None:
    builder = BlockBuilder("src-teste")
    first = builder.add(BlockKind.PARAGRAPH, "um", {"a": 1})
    second = builder.add(BlockKind.PARAGRAPH, "dois", {"a": 2})
    assert (first.order, second.order) == (0, 1)
    assert builder.next_order == 2
    assert first.source_id == "src-teste"


def test_block_builder_separates_levels() -> None:
    builder = BlockBuilder("src-teste")
    builder.inform("i", "informativo")
    builder.warn("w", "aviso")
    builder.fail("e", "erro")
    assert [d.level for d in builder.diagnostics] == [
        DiagnosticLevel.INFO,
        DiagnosticLevel.WARNING,
        DiagnosticLevel.ERROR,
    ]


@pytest.mark.parametrize(
    ("letters", "index"), [("A", 1), ("B", 2), ("Z", 26), ("AA", 27), ("BF", 58)]
)
def test_column_index_and_name_round_trip(letters: str, index: int) -> None:
    assert cellref.column_index(letters) == index
    assert cellref.column_name(index) == letters


def test_split_reference_accepts_absolute_notation() -> None:
    assert cellref.split_reference("$B$12") == (12, 2)
    assert cellref.split_reference("F27") == (27, 6)
    assert cellref.split_reference("nao-e-celula") is None


def test_range_reference_renders_the_plan_example() -> None:
    assert cellref.range_reference(12, 2, 27, 6) == "B12:F27"


def test_covers_handles_single_cells_and_ranges() -> None:
    assert sheet.covers("B12:F27", 20, 4)
    assert not sheet.covers("B12:F27", 28, 4)
    assert sheet.covers("D13", 13, 4)
    assert sheet.covers_any("A1:A2 D13", 13, 4)
    assert not sheet.covers_any("A1:A2 D13", 5, 9)


def test_number_formats_classify_date_text_and_number(tmp_path: Path) -> None:
    target = fixtures.write_xlsx(tmp_path / "formatos.xlsx")
    package = ooxml.open_package(target.read_bytes())
    try:
        formats = sheet.number_formats(package)
    finally:
        package.close()
    assert formats[0] == "number"
    assert formats[1] == "date"
    assert formats[2] == "number"


def test_shared_strings_are_read_in_declaration_order(tmp_path: Path) -> None:
    target = fixtures.write_xlsx(tmp_path / "strings.xlsx")
    package = ooxml.open_package(target.read_bytes())
    builder = BlockBuilder("src-teste")
    try:
        strings = sheet.shared_strings(package, builder)
    finally:
        package.close()
    assert strings[:3] == ["Regra", "Condicao", "Limite"]
    assert builder.diagnostics == ()


def test_regions_group_contiguous_cells_only() -> None:
    cells = tuple(
        sheet.CellValue(
            row=row, column=column, reference="", text="x", value_type="text",
            formula="", style="text",
        )
        for row, column in ((1, 1), (1, 2), (2, 1), (9, 9))
    )
    assert sheet.regions(cells) == [(1, 1, 2, 2), (9, 9, 9, 9)]


def test_open_package_refuses_a_payload_that_is_not_a_zip() -> None:
    with pytest.raises(ooxml.PackageUnreadable):
        ooxml.open_package(b"nao sou um zip")


def test_package_reads_parts_and_reports_missing_ones(tmp_path: Path) -> None:
    target = fixtures.write_docx(tmp_path / "regras.docx")
    package = ooxml.open_package(target.read_bytes())
    try:
        assert package.has("word/document.xml")
        assert package.raw("word/inexistente.xml") is None
        assert package.size("word/media/diagrama.png") == 128
        assert package.size("word/inexistente.xml") == 0
    finally:
        package.close()


def test_relationships_resolve_ids_to_type_and_target(tmp_path: Path) -> None:
    target = fixtures.write_docx(tmp_path / "regras.docx")
    package = ooxml.open_package(target.read_bytes())
    try:
        rels = ooxml.relationships(package, "word/document.xml")
    finally:
        package.close()
    assert rels["rId7"] == ("image", "media/diagrama.png")
    assert rels["rId9"] == ("hyperlink", "https://portal.example")


def test_relationships_of_a_part_without_rels_is_empty(tmp_path: Path) -> None:
    target = tmp_path / "sem_rels.docx"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    package = ooxml.open_package(target.read_bytes())
    try:
        assert ooxml.relationships(package, "word/document.xml") == {}
    finally:
        package.close()


def test_attribute_matches_the_local_name_regardless_of_namespace() -> None:
    root = parse_defused('<r xmlns:w="urn:x" w:val="7" plain="9"/>')
    assert ooxml.attribute(root, "val") == "7"
    assert ooxml.attribute(root, "plain") == "9"
    assert ooxml.attribute(root, "ausente", "padrao") == "padrao"


def test_children_and_descendants_filter_by_local_name() -> None:
    root = parse_defused("<r><a><b/></a><a/><c/></r>")
    assert len(ooxml.children(root, "a")) == 2
    assert len(list(ooxml.descendants(root, "b"))) == 1
    assert ooxml.find_child(root, "c") is not None
    assert ooxml.find_child(root, "ausente") is None


def test_collapse_normalizes_horizontal_whitespace() -> None:
    assert ooxml.collapse("  a \t b  ") == "a b"


def test_the_shared_xml_reader_refuses_a_doctype() -> None:
    with pytest.raises(XmlNotAllowed):
        parse_defused(fixtures.XXE)
