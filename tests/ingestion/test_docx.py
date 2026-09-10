from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Mapping

import pytest

from tests.ingestion import fixtures
from wiki_ai.ingestion.adapters import docx, registry
from wiki_ai.ingestion.source import (
    Block,
    BlockKind,
    DiagnosticLevel,
    SourceDocument,
    SourceKind,
    locator_violations,
)


@pytest.fixture()
def document(tmp_path: Path) -> SourceDocument:
    return registry.adapt(fixtures.write_docx(tmp_path / "regras.docx"))


def pick(document: SourceDocument, kind: BlockKind) -> list[Block]:
    return [block for block in document.blocks if block.kind is kind]


def by_text(document: SourceDocument, text: str) -> Block:
    return next(block for block in document.blocks if block.text == text)


def test_headings_carry_level_and_heading_path(document: SourceDocument) -> None:
    headings = pick(document, BlockKind.HEADING)
    assert [block.text for block in headings] == [
        "Regras de Faturamento",
        "Escopo",
        "Excecoes",
    ]
    assert headings[2].locator["heading_path"] == [
        "Regras de Faturamento",
        "Escopo",
        "Excecoes",
    ]
    assert headings[1].attributes["level"] == 1


def test_paragraph_inherits_the_open_heading_path(document: SourceDocument) -> None:
    block = by_text(document, "O faturamento roda no dia 5.")
    assert block.kind is BlockKind.PARAGRAPH
    assert block.locator["part"] == "document"
    assert block.locator["paragraph"] == 3
    assert block.locator["heading_path"] == ["Regras de Faturamento", "Escopo"]


def test_list_items_expose_numbering_level(document: SourceDocument) -> None:
    items = pick(document, BlockKind.LIST_ITEM)
    assert [block.text for block in items] == ["Primeiro item", "Subitem"]
    assert [block.attributes["ilvl"] for block in items] == [0, 1]
    assert {block.attributes["num_id"] for block in items} == {"1"}


def test_table_emits_one_cell_per_position_with_row_and_col(document: SourceDocument) -> None:
    table = pick(document, BlockKind.TABLE)[0]
    cells = pick(document, BlockKind.CELL)
    assert table.attributes["rows"] == 2
    assert table.attributes["columns"] == 2
    assert table.attributes["header_row"] == ["Regra", "Valor"]
    assert [
        (block.text, block.locator["row"], block.locator["col"]) for block in cells
    ] == [("Regra", 1, 1), ("Valor", 1, 2), ("Desconto", 2, 1), ("10%", 2, 2)]
    assert all(block.parent_id == table.id for block in cells)
    assert all(block.locator["table"] == 1 for block in cells)


def test_headers_and_footers_become_their_own_part(document: SourceDocument) -> None:
    header = by_text(document, "Cabecalho corporativo")
    footer = by_text(document, "Pagina confidencial")
    assert header.locator["part"] == "header1"
    assert footer.locator["part"] == "footer1"


def test_footnotes_and_endnotes_skip_separators(document: SourceDocument) -> None:
    footnote = by_text(document, "Nota de rodape sobre imposto")
    endnote = by_text(document, "Nota de fim sobre auditoria")
    assert footnote.locator["part"] == "footnote"
    assert footnote.locator["note_id"] == "2"
    assert endnote.locator["part"] == "endnote"
    assert endnote.locator["note_id"] == "3"


def test_comments_keep_author_and_date(document: SourceDocument) -> None:
    comment = by_text(document, "Confirmar com o juridico")
    assert comment.locator["part"] == "comments"
    assert comment.attributes["author"] == "Ana"
    assert comment.attributes["date"] == "2026-01-02T10:00:00Z"


def test_hyperlinks_resolve_through_relationships(document: SourceDocument) -> None:
    block = by_text(document, "portal")
    assert block.attributes["hyperlinks"] == [
        {"text": "portal", "target": "https://portal.example", "rel_id": "rId9"}
    ]


def test_images_become_gap_blocks_with_a_warning(document: SourceDocument) -> None:
    image = pick(document, BlockKind.IMAGE)[0]
    assert image.attributes["gap"] is True
    assert image.attributes["rel_id"] == "rId7"
    assert image.attributes["name"] == "diagrama.png"
    assert image.attributes["bytes"] == 128
    gaps = [
        d for d in document.diagnostics if d.code == "image_content_not_interpreted"
    ]
    assert len(gaps) == 1
    assert gaps[0].level is DiagnosticLevel.WARNING
    assert gaps[0].locator["image"] == "rId7"


def test_document_properties_reach_metadata_and_a_block(document: SourceDocument) -> None:
    properties = next(
        block
        for block in document.blocks
        if block.locator.get("block") == "document_properties"
    )
    values: Mapping[str, Any] = properties.attributes["properties"]
    assert values["title"] == "Regras de Faturamento"
    assert values["creator"] == "Equipe Financeira"
    assert document.source.metadata["title"] == "Regras de Faturamento"


def test_every_block_satisfies_the_kernel_locator_contract(document: SourceDocument) -> None:
    assert all(
        locator_violations(SourceKind.DOCX, block.locator) == ()
        for block in document.blocks
    )


def test_corrupt_archive_returns_a_diagnostic_not_an_exception(tmp_path: Path) -> None:
    target = tmp_path / "quebrado.docx"
    target.write_bytes(b"PK\x03\x04not a real archive")
    result = docx.adapt(target, target.read_bytes())
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == ["corrupt_archive"]


def test_missing_document_part_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "sem_corpo.docx"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("word/settings.xml", "<settings/>")
    result = docx.adapt(target, target.read_bytes())
    assert result.blocks == ()
    assert result.errors


def test_xxe_in_document_part_is_refused(tmp_path: Path) -> None:
    target = fixtures.write_docx(tmp_path / "ataque.docx", fixtures.XXE)
    result = registry.adapt(target)
    assert [d.code for d in result.diagnostics if d.code == "xml_rejected"]
    assert not any(block.locator.get("part") == "document" for block in result.blocks)


def test_xxe_in_comments_part_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "ataque_comentario.docx"
    fixtures.write_docx(target)
    with zipfile.ZipFile(target, "a") as archive:
        archive.writestr("word/comments2.xml", fixtures.XXE)
    replaced = tmp_path / "ataque_comentario2.docx"
    with zipfile.ZipFile(target) as reader, zipfile.ZipFile(replaced, "w") as writer:
        for item in reader.namelist():
            payload = (
                fixtures.XXE.encode("utf-8")
                if item == "word/comments.xml"
                else reader.read(item)
            )
            writer.writestr(item, payload)
    result = registry.adapt(replaced)
    assert any(d.code == "xml_rejected" for d in result.diagnostics)
    assert any(block.text == "Escopo" for block in result.blocks)
