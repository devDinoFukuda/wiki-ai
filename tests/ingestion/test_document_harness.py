from __future__ import annotations

from pathlib import Path

import pytest

from wiki_ai.ingestion import pipeline
from wiki_ai.ingestion.harness import (
    InvalidDocumentArguments,
    UnknownDocumentTool,
    DocumentHarness,
    TOOL_NAMES,
    excerpt_hash,
)

from tests.ingestion import fixtures


def _harness(path: Path) -> DocumentHarness:
    return DocumentHarness(pipeline.ingest(path).document)


@pytest.fixture()
def workbook(tmp_path: Path) -> DocumentHarness:
    return _harness(fixtures.write_xlsx(tmp_path / "planilha.xlsx"))


@pytest.fixture()
def diagram(tmp_path: Path) -> DocumentHarness:
    target = tmp_path / "arq.drawio"
    target.write_text(fixtures.drawio_document(), encoding="utf-8")
    return _harness(target)


@pytest.fixture()
def transcript(tmp_path: Path) -> DocumentHarness:
    target = tmp_path / "reuniao.vtt"
    target.write_text(fixtures.VTT, encoding="utf-8")
    return _harness(target)


@pytest.fixture()
def document(tmp_path: Path) -> DocumentHarness:
    return _harness(fixtures.write_docx(tmp_path / "regras.docx"))


def test_specs_cover_every_declared_tool(workbook: DocumentHarness) -> None:
    assert tuple(spec.name for spec in workbook.specs()) == TOOL_NAMES
    for spec in workbook.specs():
        assert spec.description.strip()
        assert spec.input_schema["type"] == "object"
        assert spec.output_schema["type"] == "object"


def test_unknown_tool_is_rejected(workbook: DocumentHarness) -> None:
    with pytest.raises(UnknownDocumentTool):
        workbook.invoke("doc.everything")


def test_unknown_argument_is_rejected(workbook: DocumentHarness) -> None:
    with pytest.raises(InvalidDocumentArguments):
        workbook.invoke("doc.blocks", {"sheet": "Regras"})


def test_outline_lists_worksheets(workbook: DocumentHarness) -> None:
    entries = workbook.invoke("doc.outline")["entries"]
    labels = [entry["label"] for entry in entries]
    assert labels == ["Regras", "Resumo", "Rascunho"]
    assert all(entry["scope"] == "worksheet" for entry in entries)


def test_outline_lists_speakers(transcript: DocumentHarness) -> None:
    entries = transcript.invoke("doc.outline")["entries"]
    assert [entry["label"] for entry in entries] == ["Ana", "Bruno"]


def test_outline_lists_diagram_pages(diagram: DocumentHarness) -> None:
    entries = diagram.invoke("doc.outline")["entries"]
    assert [entry["label"] for entry in entries] == ["1", "2"]


def test_outline_exposes_headings(document: DocumentHarness) -> None:
    headings = document.invoke("doc.outline")["headings"]
    assert [item["text"] for item in headings] == [
        "Regras de Faturamento",
        "Escopo",
        "Excecoes",
    ]


def test_blocks_paginate_and_filter(workbook: DocumentHarness) -> None:
    first = workbook.invoke("doc.blocks", {"worksheet": "Regras", "limit": 5})
    assert len(first["blocks"]) == 5
    assert first["truncated"] is True
    second = workbook.invoke(
        "doc.blocks", {"worksheet": "Regras", "limit": 5, "offset": 5}
    )
    assert first["blocks"][0]["block_id"] != second["blocks"][0]["block_id"]
    assert first["total"] == second["total"]


def test_blocks_filter_by_speaker(transcript: DocumentHarness) -> None:
    assert transcript.invoke("doc.blocks", {"speaker": "Bruno"})["total"] == 1


def test_read_by_block_id_and_range(document: DocumentHarness) -> None:
    listing = document.invoke("doc.blocks", {"kinds": ["paragraph"]})
    identifier = listing["blocks"][0]["block_id"]
    single = document.invoke("doc.read", {"block_id": identifier})
    assert single["total"] == 1
    assert single["blocks"][0]["text"] == "O faturamento roda no dia 5."
    window = document.invoke("doc.read", {"order_start": 1, "order_end": 3})
    assert [item["order"] for item in window["blocks"]] == [1, 2, 3]


def test_read_rejects_foreign_block(document: DocumentHarness) -> None:
    with pytest.raises(InvalidDocumentArguments):
        document.invoke("doc.read", {"block_id": "blk-99999-deadbeef"})


def test_search_finds_literal_and_regex(document: DocumentHarness) -> None:
    literal = document.invoke("doc.search", {"pattern": "faturamento", "ignore_case": True})
    assert literal["total"] >= 1
    regex = document.invoke("doc.search", {"pattern": r"dia\s+\d", "regex": True})
    assert regex["total"] == 1


def test_search_rejects_broken_regex(document: DocumentHarness) -> None:
    with pytest.raises(InvalidDocumentArguments):
        document.invoke("doc.search", {"pattern": "([", "regex": True})


def test_table_reads_a_spreadsheet_region(workbook: DocumentHarness) -> None:
    payload = workbook.invoke(
        "doc.table", {"worksheet": "Regras", "cell_range": "B12:F27"}
    )
    assert payload["header"] == ["Regra", "Condicao", "Limite", "Acao", "Owner"]
    assert payload["rows"][0] == ["Desconto A", "valor > 100", "100", "aplicar", "Financeiro"]
    assert payload["rows"][-1] == ["Desconto B", "valor > 500", "500", "escalar", "Diretoria"]
    assert payload["range"] == "Regras!B12:F27"


def test_table_rejects_a_region_without_cells(workbook: DocumentHarness) -> None:
    with pytest.raises(InvalidDocumentArguments):
        workbook.invoke("doc.table", {"worksheet": "Regras", "cell_range": "Z90:Z99"})


def test_table_reads_a_document_table(document: DocumentHarness) -> None:
    listing = document.invoke("doc.blocks", {"kinds": ["table"]})
    payload = document.invoke("doc.table", {"block_id": listing["blocks"][0]["block_id"]})
    assert payload["header"] == ["Regra", "Valor"]
    assert payload["rows"] == [["Desconto", "10%"]]


def test_graph_returns_nodes_and_edges(diagram: DocumentHarness) -> None:
    payload = diagram.invoke("doc.graph", {"page": "1"})
    labels = {item["label"] for item in payload["nodes"]}
    assert {"API Cobranca", "Banco"} <= labels
    assert payload["edges"][0]["label"] == "grava"
    assert payload["edges"][0]["source"] == "n1"
    assert payload["edges"][0]["target"] == "n2"


def test_graph_restricts_to_a_neighbourhood(diagram: DocumentHarness) -> None:
    payload = diagram.invoke("doc.graph", {"page": "1", "node": "n1", "depth": 1})
    assert {item["node"] for item in payload["nodes"]} == {"n1", "n2"}


def test_hints_are_never_authoritative(transcript: DocumentHarness) -> None:
    payload = transcript.invoke("doc.hints")
    assert payload["authoritative"] is False
    assert all(item["authoritative"] is False for item in payload["hints"])
    kinds = {item["candidate_kind"] for item in payload["hints"]}
    assert "decision" in kinds


def test_capture_produces_a_spreadsheet_locator(workbook: DocumentHarness) -> None:
    table = workbook.invoke("doc.table", {"worksheet": "Regras", "cell_range": "B13:F13"})
    capture = workbook.invoke("evidence.capture", {"block_ids": table["block_ids"]})
    assert capture["locator"] == {
        "kind": "spreadsheet",
        "workbook": "planilha.xlsx",
        "worksheet": "Regras",
        "cell_range": "B13:F13",
    }
    assert capture["excerpt_hash"] == excerpt_hash(capture["excerpt"])


def test_capture_produces_a_diagram_locator(diagram: DocumentHarness) -> None:
    payload = diagram.invoke("doc.graph", {"page": "1"})
    node = [item for item in payload["nodes"] if item["node"] == "n1"][0]
    capture = diagram.invoke("evidence.capture", {"block_ids": [node["block_id"]]})
    assert capture["locator"] == {
        "kind": "diagram",
        "diagram": "Arquitetura",
        "page": "1",
        "node": "n1",
    }


def test_capture_produces_a_transcript_locator(transcript: DocumentHarness) -> None:
    listing = transcript.invoke("doc.blocks", {"speaker": "Ana"})
    capture = transcript.invoke(
        "evidence.capture", {"block_ids": [listing["blocks"][0]["block_id"]]}
    )
    assert capture["locator"] == {
        "kind": "transcript",
        "speaker": "Ana",
        "time_start": 12.0,
        "time_end": 24.0,
    }


def test_capture_produces_a_document_locator(document: DocumentHarness) -> None:
    listing = document.invoke("doc.blocks", {"kinds": ["paragraph"]})
    identifier = listing["blocks"][0]["block_id"]
    capture = document.invoke("evidence.capture", {"block_ids": [identifier]})
    assert capture["locator"]["kind"] == "document"
    assert capture["locator"]["block_id"] == identifier
    assert capture["locator"]["heading_path"] == ["Regras de Faturamento", "Escopo"]


def test_capture_rejects_a_foreign_block(document: DocumentHarness) -> None:
    with pytest.raises(InvalidDocumentArguments):
        document.invoke("evidence.capture", {"block_ids": ["blk-00000-000000000000"]})


def test_capture_is_stable_for_the_same_blocks(document: DocumentHarness) -> None:
    listing = document.invoke("doc.blocks", {"kinds": ["paragraph"]})
    identifier = listing["blocks"][0]["block_id"]
    first = document.invoke("evidence.capture", {"block_ids": [identifier]})
    second = document.invoke("evidence.capture", {"block_ids": [identifier]})
    assert first["capture_id"] == second["capture_id"]
    assert len(document.captures()) == 1


def test_harness_never_mutates_the_document(document: DocumentHarness) -> None:
    before = document.document.to_dict()
    document.invoke("doc.outline")
    document.invoke("doc.search", {"pattern": "a"})
    assert document.document.to_dict() == before
