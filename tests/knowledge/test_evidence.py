from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    CodeContent,
    CodeLocator,
    DiagramLocator,
    DocumentLocator,
    LocatorInvalid,
    PayloadInvalid,
    SpreadsheetLocator,
    TranscriptLocator,
    UnsupportedEvidence,
    content_hash,
    locator_from_dict,
    make_evidence,
)
from wiki_ai.knowledge.evidence import assert_supports_implemented, supports_implemented
from wiki_ai.knowledge.model import Evidence

CAPTURED = "2026-01-01T00:00:00+00:00"

ALL_LOCATORS = [
    CodeLocator(path="src/app/order.py", line_start=10, line_end=42, symbol="place_order"),
    CodeLocator(path="src/app/order.py", line_start=10, line_end=10),
    CodeLocator(
        path="config/app.yaml", line_start=3, line_end=3, content=CodeContent.CONFIG_VALUE
    ),
    DocumentLocator(block_id="blk-17", heading_path=("Capítulo 2", "Regras")),
    DocumentLocator(heading_path=("Anexo",)),
    DocumentLocator(block_id="blk-1"),
    SpreadsheetLocator(workbook="tarifas.xlsx", worksheet="2026", cell_range="B2:D40"),
    DiagramLocator(diagram="fluxo.drawio", page="Pagina-1", node="node-88"),
    TranscriptLocator(speaker="Ana", time_start=12.5, time_end=48.0),
]


@pytest.mark.parametrize("locator", ALL_LOCATORS, ids=lambda loc: f"{loc.kind}")
def test_evidence_locator_roundtrip(locator):
    payload = locator.to_dict()
    restored = locator_from_dict(payload)
    assert restored == locator
    assert restored.to_dict() == payload
    assert type(restored) is type(locator)


def test_locator_kinds_cover_every_source_shape():
    kinds = {locator.kind for locator in ALL_LOCATORS}
    assert kinds == {"code", "document", "spreadsheet", "diagram", "transcript"}


def test_code_locator_keeps_symbol_and_range():
    locator = CodeLocator(path="a.py", line_start=1, line_end=9, symbol="f")
    payload = locator.to_dict()
    assert payload["path"] == "a.py"
    assert payload["line_start"] == 1
    assert payload["line_end"] == 9
    assert payload["symbol"] == "f"


def test_code_locator_rejects_inverted_range():
    with pytest.raises(LocatorInvalid):
        CodeLocator(path="a.py", line_start=9, line_end=2)


def test_code_locator_rejects_empty_path():
    with pytest.raises(LocatorInvalid):
        CodeLocator(path="  ", line_start=1, line_end=2)


def test_document_locator_requires_block_or_heading():
    with pytest.raises(LocatorInvalid):
        DocumentLocator()


def test_transcript_locator_rejects_inverted_interval():
    with pytest.raises(LocatorInvalid):
        TranscriptLocator(speaker="Ana", time_start=10.0, time_end=1.0)


def test_locator_from_dict_rejects_unknown_kind():
    with pytest.raises(LocatorInvalid):
        locator_from_dict({"kind": "telepathy"})


def test_locator_from_dict_rejects_malformed_fields():
    with pytest.raises(LocatorInvalid):
        locator_from_dict({"kind": "code", "path": "a.py", "line_start": 0, "line_end": 3})


def test_locator_from_dict_rejects_non_mapping():
    with pytest.raises(LocatorInvalid):
        locator_from_dict(["kind", "code"])


def test_evidence_always_carries_source_and_version():
    evidence = make_evidence(
        "src_repo", "commit-abc", ALL_LOCATORS[0], "def place_order():", CAPTURED
    )
    assert evidence.source_id == "src_repo"
    assert evidence.version_hash == "commit-abc"
    assert evidence.source_version_key.startswith("srv_")


def test_evidence_rejects_missing_version():
    with pytest.raises(PayloadInvalid):
        Evidence(
            id="evd_1",
            source_id="src_repo",
            version_hash="  ",
            locator=ALL_LOCATORS[0],
            excerpt_hash=content_hash("x"),
            captured_at=CAPTURED,
        )


def test_evidence_rejects_locator_of_wrong_type():
    with pytest.raises(PayloadInvalid):
        Evidence(
            id="evd_1",
            source_id="src_repo",
            version_hash="v1",
            locator={"kind": "code"},
            excerpt_hash=content_hash("x"),
            captured_at=CAPTURED,
        )


def test_evidence_id_is_deterministic_for_same_citation():
    first = make_evidence("src_repo", "v1", ALL_LOCATORS[0], "trecho", CAPTURED)
    second = make_evidence("src_repo", "v1", ALL_LOCATORS[0], "trecho", CAPTURED)
    assert first.id == second.id


def test_evidence_excerpt_hash_detects_changed_excerpt():
    first = make_evidence("src_repo", "v1", ALL_LOCATORS[0], "trecho", CAPTURED)
    second = make_evidence("src_repo", "v1", ALL_LOCATORS[0], "outro", CAPTURED)
    assert first.excerpt_hash != second.excerpt_hash
    assert first.id == second.id


def test_executable_code_supports_implemented():
    evidence = make_evidence("src_repo", "v1", ALL_LOCATORS[0], "codigo", CAPTURED)
    assert supports_implemented(evidence) is True


def test_config_value_supports_implemented():
    locator = CodeLocator(
        path="app.yaml", line_start=1, line_end=1, content=CodeContent.CONFIG_VALUE
    )
    assert supports_implemented(make_evidence("s", "v", locator, "x", CAPTURED)) is True


@pytest.mark.parametrize(
    "locator",
    [
        CodeLocator(path="a.py", line_start=1, line_end=2, content=CodeContent.COMMENT),
        CodeLocator(path="a.py", line_start=1, line_end=2, content=CodeContent.DOCSTRING),
        CodeLocator(path="README.md", line_start=1, line_end=2, content=CodeContent.MARKUP),
        DocumentLocator(block_id="blk-1"),
        TranscriptLocator(speaker="Ana", time_start=0.0, time_end=1.0),
        DiagramLocator(diagram="d", page="p", node="n"),
        SpreadsheetLocator(workbook="w", worksheet="s", cell_range="A1"),
    ],
)
def test_prose_and_annotation_do_not_support_implemented(locator):
    evidence = make_evidence("src", "v1", locator, "texto", CAPTURED)
    assert supports_implemented(evidence) is False
    with pytest.raises(UnsupportedEvidence):
        assert_supports_implemented([evidence])


def test_assert_supports_implemented_accepts_mixed_set_with_one_executable():
    prose = make_evidence("src", "v1", DocumentLocator(block_id="b"), "texto", CAPTURED)
    code = make_evidence("src", "v1", ALL_LOCATORS[0], "codigo", CAPTURED)
    assert_supports_implemented([prose, code])


def test_assert_supports_implemented_rejects_empty_set():
    with pytest.raises(UnsupportedEvidence):
        assert_supports_implemented([])
