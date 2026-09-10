from __future__ import annotations

from pathlib import Path

import pytest

from tests.ingestion import fixtures
from wiki_ai.ingestion.adapters import registry, transcript
from wiki_ai.ingestion.source import (
    BlockKind,
    SourceDocument,
    SourceKind,
    locator_violations,
)


def build(tmp_path: Path, name: str, body: str) -> SourceDocument:
    target = tmp_path / name
    target.write_text(body, encoding="utf-8")
    return registry.adapt(target)


@pytest.fixture()
def vtt(tmp_path: Path) -> SourceDocument:
    return build(tmp_path, "reuniao.vtt", fixtures.VTT)


def test_vtt_voice_tags_become_speakers(vtt: SourceDocument) -> None:
    assert [block.locator["speaker"] for block in vtt.blocks] == ["Ana", "Bruno"]


def test_consecutive_utterances_of_one_speaker_are_grouped(vtt: SourceDocument) -> None:
    assert vtt.blocks[0].text == (
        "Decidimos manter o corte no dia 5. O time financeiro confirma amanha."
    )


def test_grouping_extends_the_time_range(vtt: SourceDocument) -> None:
    assert vtt.blocks[0].locator["time_start"] == "00:00:12.000"
    assert vtt.blocks[0].locator["time_end"] == "00:00:24.000"


def test_every_block_is_an_utterance(vtt: SourceDocument) -> None:
    assert {block.kind for block in vtt.blocks} == {BlockKind.UTTERANCE}


def test_order_is_preserved(vtt: SourceDocument) -> None:
    assert [block.order for block in vtt.blocks] == [0, 1]
    assert [block.locator["index"] for block in vtt.blocks] == [1, 2]


def test_participants_reach_the_source_metadata(vtt: SourceDocument) -> None:
    assert vtt.source.metadata["participants"] == ["Ana", "Bruno"]


def test_srt_index_lines_are_not_treated_as_text(tmp_path: Path) -> None:
    document = build(tmp_path, "reuniao.srt", fixtures.SRT)
    assert [block.text for block in document.blocks] == [
        "A integracao roda em lote.",
        "Qual a janela de retry?",
    ]
    assert [block.locator["speaker"] for block in document.blocks] == ["Carla", "Diego"]


def test_srt_timestamps_are_normalized_to_dot_milliseconds(tmp_path: Path) -> None:
    document = build(tmp_path, "reuniao.srt", fixtures.SRT)
    assert document.blocks[0].locator["time_start"] == "00:00:03.000"
    assert document.blocks[0].locator["time_end"] == "00:00:07.250"


def test_bracket_time_export_is_parsed(tmp_path: Path) -> None:
    document = build(tmp_path, "reuniao.txt", fixtures.TXT_TRANSCRIPT)
    assert document.blocks[0].locator["speaker"] == "Ana"
    assert document.blocks[0].locator["time_start"] == "00:12:03"


def test_a_continuation_line_joins_the_open_utterance(tmp_path: Path) -> None:
    document = build(tmp_path, "reuniao.txt", fixtures.TXT_TRANSCRIPT)
    assert document.blocks[0].text == (
        "O contrato vence em marco. continuidade da mesma fala."
    )


def test_parenthesised_time_export_is_parsed(tmp_path: Path) -> None:
    document = build(tmp_path, "reuniao.txt", fixtures.TXT_TRANSCRIPT)
    bruno = next(
        block for block in document.blocks if block.locator["speaker"] == "Bruno"
    )
    assert bruno.text == "Vou revisar a clausula 7."
    assert bruno.locator["time_start"] == "00:00:13"


def test_a_speaker_returning_later_is_not_merged_across_others(tmp_path: Path) -> None:
    document = build(tmp_path, "reuniao.txt", fixtures.TXT_TRANSCRIPT)
    assert [block.locator["speaker"] for block in document.blocks] == [
        "Ana",
        "Bruno",
        "Ana",
    ]


def test_teams_style_export_with_a_leading_clock_line(tmp_path: Path) -> None:
    body = "00:05:10\nCarla: Subimos o release na terca.\n\n00:06:00\nDiego: Anotado.\n"
    document = build(tmp_path, "teams.txt", body)
    assert [
        (block.locator["speaker"], block.locator["time_start"]) for block in document.blocks
    ] == [("Carla", "00:05:10"), ("Diego", "00:06:00")]


def test_a_cue_without_a_speaker_falls_back_to_unknown(tmp_path: Path) -> None:
    body = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nlinha sem interlocutor\n"
    document = build(tmp_path, "anonima.vtt", body)
    assert document.blocks[0].locator["speaker"] == transcript.UNKNOWN_SPEAKER


def test_no_semantic_classification_is_attached(vtt: SourceDocument) -> None:
    keys = {key for block in vtt.blocks for key in block.attributes}
    assert keys == {"flavour"}


def test_an_empty_transcript_reports_a_warning(tmp_path: Path) -> None:
    document = build(tmp_path, "vazia.vtt", "WEBVTT\n\n")
    assert document.blocks == ()
    assert [d.code for d in document.diagnostics] == [transcript.EMPTY_TRANSCRIPT]


def test_every_block_satisfies_the_kernel_locator_contract(vtt: SourceDocument) -> None:
    assert all(
        locator_violations(SourceKind.TRANSCRIPT, block.locator) == ()
        for block in vtt.blocks
    )
