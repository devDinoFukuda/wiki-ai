from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tests.ingestion import fixtures
from wiki_ai.ingestion.adapters import registry, xlsx
from wiki_ai.ingestion.source import (
    Block,
    BlockKind,
    SourceDocument,
    SourceKind,
    locator_violations,
)


@pytest.fixture()
def document(tmp_path: Path) -> SourceDocument:
    return registry.adapt(fixtures.write_xlsx(tmp_path / "regras.xlsx"))


def cell(document: SourceDocument, sheet: str, reference: str) -> Block:
    return next(
        block
        for block in document.blocks
        if block.kind is BlockKind.CELL
        and block.locator.get("worksheet") == sheet
        and block.locator.get("cell") == reference
    )


def tables(document: SourceDocument) -> list[Block]:
    return [block for block in document.blocks if block.kind is BlockKind.TABLE]


def test_workbook_exposes_every_declared_sheet(document: SourceDocument) -> None:
    sheets = {
        block.locator["worksheet"]
        for block in document.blocks
        if block.kind is BlockKind.CELL
    }
    assert sheets == {"Regras", "Resumo", "Rascunho"}


def test_hidden_sheet_cells_are_flagged_not_dropped(document: SourceDocument) -> None:
    hidden = cell(document, "Rascunho", "A1")
    assert hidden.text == "rascunho"
    assert hidden.attributes["visible"] is False
    assert cell(document, "Regras", "B12").attributes["visible"] is True


def test_shared_strings_resolve_into_cell_text(document: SourceDocument) -> None:
    assert cell(document, "Regras", "B12").text == "Regra"
    assert cell(document, "Regras", "B13").text == "Desconto A"


def test_numeric_and_text_cells_get_distinct_value_types(document: SourceDocument) -> None:
    assert cell(document, "Regras", "D13").attributes["value_type"] == "number"
    assert cell(document, "Regras", "C13").attributes["value_type"] == "text"


def test_formula_is_preserved_on_the_cell(document: SourceDocument) -> None:
    block = cell(document, "Resumo", "B1")
    assert block.attributes["formula"] == "Regras!D13+Regras!D27"


def test_cross_sheet_references_are_extracted_from_formulas(document: SourceDocument) -> None:
    block = cell(document, "Resumo", "B1")
    assert block.attributes["cross_sheet_refs"] == ["Regras!D13", "Regras!D27"]


def test_merged_range_is_attached_to_the_covered_cells(document: SourceDocument) -> None:
    assert cell(document, "Regras", "B12").attributes["merged_range"] == "B12:C12"
    assert "merged_range" not in cell(document, "Regras", "D12").attributes


def test_data_validation_is_attached_to_the_covered_cells(document: SourceDocument) -> None:
    validation = cell(document, "Regras", "E13").attributes["data_validation"]
    assert validation["type"] == "list"
    assert validation["range"] == "E13:E27"
    assert validation["formula"] == '"aplicar,escalar"'


def test_comments_reach_the_annotated_cell(document: SourceDocument) -> None:
    block = cell(document, "Regras", "D13")
    assert block.attributes["comment"] == "limite revisado"
    assert block.attributes["comment_author"] == "Bruno"


def test_declared_table_uses_the_required_range_locator(document: SourceDocument) -> None:
    declared = [block for block in tables(document) if block.attributes["declared"]]
    assert len(declared) == 1
    table = declared[0]
    assert table.locator["worksheet"] == "Regras"
    assert table.locator["range"] == "B12:F27"
    assert table.attributes["table_name"] == "TabelaRegras"
    assert table.attributes["header_row"] == [
        "Regra",
        "Condicao",
        "Limite",
        "Acao",
        "Owner",
    ]


def test_the_plan_example_locator_is_reachable(document: SourceDocument) -> None:
    table = next(
        block
        for block in tables(document)
        if f"{block.locator['worksheet']}!{block.locator['range']}" == "Regras!B12:F27"
    )
    assert table.text.startswith("Regra\tCondicao\tLimite\tAcao\tOwner")


def test_named_range_becomes_its_own_block(document: SourceDocument) -> None:
    block = next(
        item for item in document.blocks if item.locator.get("named_range")
    )
    assert block.locator["named_range"] == "FaixaRegras"
    assert block.locator["worksheet"] == "Regras"
    assert block.locator["range"] == "B12:F27"


def test_contiguous_region_without_a_declared_table_is_detected(tmp_path: Path) -> None:
    sheet = (
        f'<?xml version="1.0"?><worksheet {fixtures.S}><sheetData>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>Etapa</t></is></c>'
        '<c r="B2" t="inlineStr"><is><t>Prazo</t></is></c></row>'
        '<row r="3"><c r="A3" t="inlineStr"><is><t>Triagem</t></is></c>'
        '<c r="B3"><v>2</v></c></row>'
        "</sheetData></worksheet>"
    )
    workbook = (
        f'<?xml version="1.0"?><workbook {fixtures.S} {fixtures.SR}><sheets>'
        '<sheet name="Fluxo" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    target = tmp_path / "detectada.xlsx"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<?xml version="1.0"?><Relationships {fixtures.RELS}>'
            '<Relationship Id="rId1" Type="http://x/ws" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    document = registry.adapt(target)
    detected = [block for block in tables(document) if not block.attributes["declared"]]
    assert len(detected) == 1
    assert detected[0].locator["range"] == "A2:B3"
    assert detected[0].attributes["header_row"] == ["Etapa", "Prazo"]


def test_region_without_a_textual_header_is_not_a_table(tmp_path: Path) -> None:
    sheet = (
        f'<?xml version="1.0"?><worksheet {fixtures.S}><sheetData>'
        '<row r="1"><c r="A1"><v>1</v></c><c r="B1"><v>2</v></c></row>'
        '<row r="2"><c r="A2"><v>3</v></c><c r="B2"><v>4</v></c></row>'
        "</sheetData></worksheet>"
    )
    workbook = (
        f'<?xml version="1.0"?><workbook {fixtures.S} {fixtures.SR}><sheets>'
        '<sheet name="Numeros" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    target = tmp_path / "numerica.xlsx"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<?xml version="1.0"?><Relationships {fixtures.RELS}>'
            '<Relationship Id="rId1" Type="http://x/ws" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    assert tables(registry.adapt(target)) == []


def test_every_block_satisfies_the_kernel_locator_contract(document: SourceDocument) -> None:
    assert all(
        locator_violations(SourceKind.XLSX, block.locator) == ()
        for block in document.blocks
    )


def test_corrupt_archive_returns_a_diagnostic_not_an_exception(tmp_path: Path) -> None:
    target = tmp_path / "quebrada.xlsx"
    target.write_bytes(b"PK\x03\x04corrompido")
    result = xlsx.adapt(target, target.read_bytes())
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == ["corrupt_archive"]


def test_missing_workbook_part_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "sem_workbook.xlsx"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("xl/styles.xml", "<styleSheet/>")
    result = xlsx.adapt(target, target.read_bytes())
    assert result.blocks == ()
    assert result.errors


def test_xxe_in_workbook_is_refused(tmp_path: Path) -> None:
    target = fixtures.write_xlsx(tmp_path / "ataque.xlsx", fixtures.XXE)
    result = registry.adapt(target)
    assert [d.code for d in result.diagnostics] == ["xml_rejected"]
    assert result.blocks == ()
