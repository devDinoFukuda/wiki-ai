from __future__ import annotations

from pathlib import Path

import pytest

from tests.ingestion import fixtures
from wiki_ai.ingestion.adapters import json as json_adapter, registry
from wiki_ai.ingestion.source import (
    Block,
    BlockKind,
    SourceDocument,
    SourceKind,
    locator_violations,
)


def build(tmp_path: Path, name: str, body: str) -> SourceDocument:
    target = tmp_path / name
    target.write_text(body, encoding="utf-8")
    return registry.adapt(target)


def pick(document: SourceDocument, kind: BlockKind) -> list[Block]:
    return [block for block in document.blocks if block.kind is kind]


@pytest.fixture()
def markdown(tmp_path: Path) -> SourceDocument:
    return build(tmp_path, "guia.md", fixtures.MARKDOWN)


@pytest.fixture()
def html(tmp_path: Path) -> SourceDocument:
    return build(tmp_path, "guia.html", fixtures.HTML)


def test_markdown_headings_build_a_section_path(markdown: SourceDocument) -> None:
    headings = pick(markdown, BlockKind.HEADING)
    assert [block.text for block in headings] == ["Politica", "Regras"]
    assert headings[1].locator["section"] == "Politica / Regras"
    assert headings[1].attributes["level"] == 2


def test_markdown_paragraph_carries_its_line_range(markdown: SourceDocument) -> None:
    paragraph = pick(markdown, BlockKind.PARAGRAPH)[0]
    assert paragraph.text == "Texto introdutorio da politica."
    assert paragraph.locator["start_line"] == 3


def test_markdown_list_items_are_separate_blocks(markdown: SourceDocument) -> None:
    assert [block.text for block in pick(markdown, BlockKind.LIST_ITEM)] == [
        "primeiro",
        "segundo",
    ]


def test_markdown_gfm_table_yields_a_table_and_its_cells(markdown: SourceDocument) -> None:
    table = pick(markdown, BlockKind.TABLE)[0]
    cells = pick(markdown, BlockKind.CELL)
    assert table.attributes["header_row"] == ["Regra", "Limite"]
    assert [(block.text, block.locator["row"], block.locator["col"]) for block in cells] == [
        ("A", 1, 1),
        ("100", 1, 2),
        ("B", 2, 1),
        ("500", 2, 2),
    ]
    assert all(block.parent_id == table.id for block in cells)


def test_markdown_code_fence_keeps_the_language(markdown: SourceDocument) -> None:
    code = pick(markdown, BlockKind.CODE)[0]
    assert code.attributes["language"] == "python"
    assert code.text == "def cobrar(valor):\n    return valor"


def test_markdown_blocks_satisfy_the_kernel_locator_contract(markdown: SourceDocument) -> None:
    assert all(
        locator_violations(SourceKind.MARKDOWN, block.locator) == ()
        for block in markdown.blocks
    )


def test_html_headings_and_paragraphs_carry_an_element_path(html: SourceDocument) -> None:
    heading = pick(html, BlockKind.HEADING)[0]
    paragraph = pick(html, BlockKind.PARAGRAPH)[0]
    assert heading.text == "Manual Operacional"
    assert heading.locator["path"] == "/html[1]/body[1]/h1[1]"
    assert paragraph.text == "Fluxo documentado."
    assert paragraph.locator["path"] == "/html[1]/body[1]/p[1]"


def test_html_anchors_are_kept_as_attributes(html: SourceDocument) -> None:
    paragraph = pick(html, BlockKind.PARAGRAPH)[0]
    assert paragraph.attributes["hyperlinks"] == [
        {"target": "https://wiki.example/fluxo", "text": "documentado"}
    ]


def test_html_list_items_and_pre_blocks(html: SourceDocument) -> None:
    assert [block.text for block in pick(html, BlockKind.LIST_ITEM)] == [
        "abrir chamado",
        "validar",
    ]
    assert pick(html, BlockKind.CODE)[0].text == "curl /api/v1"


def test_html_table_cells_carry_row_and_col(html: SourceDocument) -> None:
    table = pick(html, BlockKind.TABLE)[0]
    cells = pick(html, BlockKind.CELL)
    assert [
        (block.text, block.locator["row"], block.locator["col"], block.attributes["header"])
        for block in cells
    ] == [
        ("Etapa", 1, 1, True),
        ("Prazo", 1, 2, True),
        ("Triagem", 2, 1, False),
        ("2h", 2, 2, False),
    ]
    assert all(block.parent_id == table.id for block in cells)


def test_html_script_content_is_dropped(html: SourceDocument) -> None:
    assert all("ignorado" not in block.text for block in html.blocks)


def test_html_title_reaches_the_metadata(html: SourceDocument) -> None:
    assert html.source.metadata["title"] == "Manual"


def test_html_blocks_satisfy_the_kernel_locator_contract(html: SourceDocument) -> None:
    assert all(
        locator_violations(SourceKind.HTML, block.locator) == ()
        for block in html.blocks
    )


def test_json_object_yields_one_block_per_top_level_key(tmp_path: Path) -> None:
    document = build(tmp_path, "dados.json", fixtures.JSON_DOCUMENT)
    assert [block.locator["json_pointer"] for block in document.blocks] == [
        "/regra_a",
        "/regra_b",
    ]


def test_json_array_uses_index_pointers(tmp_path: Path) -> None:
    document = build(tmp_path, "lista.json", '[{"a": 1}, {"a": 2}]')
    assert [block.locator["json_pointer"] for block in document.blocks] == ["/0", "/1"]


def test_jsonl_yields_one_block_per_line(tmp_path: Path) -> None:
    document = build(tmp_path, "fluxo.jsonl", fixtures.JSONL_DOCUMENT)
    assert [block.locator["json_pointer"] for block in document.blocks] == ["/0", "/1"]
    assert {block.attributes["shape"] for block in document.blocks} == {"jsonl"}


def test_json_pointer_escapes_slashes_in_keys(tmp_path: Path) -> None:
    document = build(tmp_path, "escapes.json", '{"a/b": 1, "c~d": 2}')
    assert [block.locator["json_pointer"] for block in document.blocks] == [
        "/a~1b",
        "/c~0d",
    ]


def test_malformed_json_yields_a_diagnostic_without_exception(tmp_path: Path) -> None:
    document = build(tmp_path, "quebrado.json", "{ isto nao e json")
    assert document.blocks == ()
    assert [d.code for d in document.diagnostics] == [json_adapter.MALFORMED_JSON]


def test_json_blocks_satisfy_the_kernel_locator_contract(tmp_path: Path) -> None:
    document = build(tmp_path, "dados.json", fixtures.JSON_DOCUMENT)
    assert all(
        locator_violations(SourceKind.JSON, block.locator) == ()
        for block in document.blocks
    )


def test_xml_leaves_carry_an_indexed_xpath(tmp_path: Path) -> None:
    document = build(tmp_path, "catalogo.xml", fixtures.XML_DOCUMENT)
    assert [block.locator["xpath"] for block in document.blocks] == [
        "/catalogo/item[1]/nome[1]",
        "/catalogo/item[1]/preco[1]",
        "/catalogo/item[2]/nome[1]",
    ]


def test_xml_leaf_attributes_are_preserved(tmp_path: Path) -> None:
    document = build(
        tmp_path, "atributos.xml", '<r><n unidade="dias">30</n></r>'
    )
    assert document.blocks[0].attributes["attributes"] == {"unidade": "dias"}


def test_xxe_in_a_generic_xml_is_refused(tmp_path: Path) -> None:
    document = build(tmp_path, "ataque.xml", fixtures.XXE)
    assert document.blocks == ()
    assert [d.code for d in document.diagnostics] == ["xml_rejected"]


def test_xml_blocks_satisfy_the_kernel_locator_contract(tmp_path: Path) -> None:
    document = build(tmp_path, "catalogo.xml", fixtures.XML_DOCUMENT)
    assert all(
        locator_violations(SourceKind.XML, block.locator) == ()
        for block in document.blocks
    )
