from __future__ import annotations

from pathlib import Path

import pytest

from tests.ingestion import fixtures
from wiki_ai.ingestion.adapters import drawio, registry
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
    target = tmp_path / "arquitetura.drawio"
    target.write_text(fixtures.drawio_document(), encoding="utf-8")
    return registry.adapt(target)


def node(document: SourceDocument, identifier: str) -> Block:
    return next(
        block for block in document.blocks if block.locator.get("node") == identifier
    )


def test_compressed_page_is_inflated_and_urldecoded(document: SourceDocument) -> None:
    assert node(document, "n1").text == "API Cobranca"


def test_plain_xml_page_is_read_without_decompression(document: SourceDocument) -> None:
    assert node(document, "p2n1").text == "Fila"


def test_pages_are_numbered_and_named(document: SourceDocument) -> None:
    assert node(document, "n1").locator["diagram"] == "Arquitetura"
    assert node(document, "n1").locator["page"] == 1
    assert node(document, "p2n1").locator["diagram"] == "Integracoes"
    assert node(document, "p2n1").locator["page"] == 2


def test_vertices_become_node_blocks_with_geometry(document: SourceDocument) -> None:
    block = node(document, "n1")
    assert block.kind is BlockKind.NODE
    assert block.attributes["geometry"] == {
        "x": 20.0,
        "y": 30.0,
        "width": 160.0,
        "height": 60.0,
    }


def test_html_markup_is_stripped_from_labels(document: SourceDocument) -> None:
    assert "<b>" not in node(document, "n1").text
    assert node(document, "n1").text == "API Cobranca"


def test_edges_become_edge_blocks_with_source_and_target(document: SourceDocument) -> None:
    edge = next(block for block in document.blocks if block.kind is BlockKind.EDGE)
    assert edge.text == "grava"
    assert edge.locator["edge"] == "e1"
    assert edge.attributes["source"] == "n1"
    assert edge.attributes["target"] == "n2"


def test_layers_are_not_emitted_as_nodes(document: SourceDocument) -> None:
    identifiers = {block.locator["node"] for block in document.blocks}
    assert "0" not in identifiers
    assert "1" not in identifiers


def test_container_and_contained_nodes_are_distinguished(document: SourceDocument) -> None:
    group = node(document, "grp1")
    inner = node(document, "n2")
    assert group.attributes["container"] is True
    assert group.attributes["in_container"] is False
    assert inner.attributes["container"] is False
    assert inner.attributes["in_container"] is True
    assert inner.attributes["parent"] == "grp1"


def test_style_is_parsed_into_key_value_pairs(document: SourceDocument) -> None:
    assert node(document, "n2").attributes["style_parsed"] == {
        "shape": "cylinder",
        "fillColor": "#d5e8d4",
    }
    assert node(document, "n1").attributes["style_parsed"]["rounded"] == "1"


def test_bare_mxgraphmodel_is_accepted(tmp_path: Path) -> None:
    target = tmp_path / "modelo.xml"
    target.write_text(fixtures.MX_MODEL, encoding="utf-8")
    result = registry.adapt(target)
    assert result.source.kind is SourceKind.DRAWIO
    assert {block.locator["node"] for block in result.blocks} == {
        "grp1",
        "n1",
        "n2",
        "e1",
    }


def test_every_block_satisfies_the_kernel_locator_contract(document: SourceDocument) -> None:
    assert all(
        locator_violations(SourceKind.DRAWIO, block.locator) == ()
        for block in document.blocks
    )


def test_embedded_png_payload_is_reported_without_exception(tmp_path: Path) -> None:
    target = tmp_path / "arquitetura.drawio.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload" * 10)
    result = registry.adapt(target)
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == ["embedded_payload_not_supported"]
    assert result.diagnostics[0].level is DiagnosticLevel.WARNING


def test_embedded_svg_payload_is_reported_without_exception(tmp_path: Path) -> None:
    target = tmp_path / "arquitetura.drawio.svg"
    target.write_text("<svg content='mxfile'></svg>", encoding="utf-8")
    result = registry.adapt(target)
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == ["embedded_payload_not_supported"]


def test_unreadable_page_payload_is_reported_per_page(tmp_path: Path) -> None:
    target = tmp_path / "ruim.drawio"
    target.write_text(
        '<mxfile><diagram id="d1" name="Quebrada">%%%nao-e-base64%%%</diagram></mxfile>',
        encoding="utf-8",
    )
    result = registry.adapt(target)
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == ["malformed_diagram"]


def test_mxfile_without_pages_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "vazio.drawio"
    target.write_text("<mxfile></mxfile>", encoding="utf-8")
    result = registry.adapt(target)
    assert [d.code for d in result.diagnostics] == ["malformed_diagram"]


def test_xxe_in_a_diagram_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "ataque.drawio"
    target.write_text(
        fixtures.XXE.replace("<root>", "<mxfile>").replace("</root>", "</mxfile>"),
        encoding="utf-8",
    )
    result = registry.adapt(target)
    assert result.blocks == ()
    assert [d.code for d in result.diagnostics] == ["xml_rejected"]


def test_decode_diagram_rejects_a_payload_that_is_not_deflate() -> None:
    assert drawio.decode_diagram("bm90LWRlZmxhdGU=") is None
    assert drawio.decode_diagram("") is None


def test_parse_style_keeps_a_bare_shape_token() -> None:
    assert drawio.parse_style("mxgraph.aws4.lambda;fillColor=#fff") == {
        "shape": "mxgraph.aws4.lambda",
        "fillColor": "#fff",
    }
