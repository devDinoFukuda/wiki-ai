from __future__ import annotations

import dataclasses

import pytest

from wiki_ai.publishing.docx import blocks
from wiki_ai.publishing.docx.errors import BlockValidationError


def test_run_is_frozen():
    run = blocks.Run(text="a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        run.text = "b"


def test_run_rejects_blank_hyperlink():
    with pytest.raises(BlockValidationError):
        blocks.Run(text="a", hyperlink="  ")


@pytest.mark.parametrize("level", [0, 7, -1])
def test_heading_rejects_level_out_of_range(level):
    with pytest.raises(BlockValidationError):
        blocks.Heading(level=level, text="x")


def test_heading_accepts_levels_one_to_six():
    assert [blocks.Heading(level=n, text="x").level for n in range(1, 7)] == [1, 2, 3, 4, 5, 6]


def test_paragraph_freezes_runs_into_tuple():
    paragraph = blocks.Paragraph(runs=[blocks.Run(text="a")])
    assert isinstance(paragraph.runs, tuple)


def test_list_item_rejects_level_above_maximum():
    with pytest.raises(BlockValidationError):
        blocks.ListItem(runs=blocks.text_runs("x"), level=blocks.MAX_LIST_LEVEL + 1)


def test_bullet_list_rejects_empty_items():
    with pytest.raises(BlockValidationError):
        blocks.BulletList(items=())


def test_table_rejects_ragged_rows():
    with pytest.raises(BlockValidationError):
        blocks.Table(
            rows=(
                (blocks.text_cell("a"), blocks.text_cell("b")),
                (blocks.text_cell("c"),),
            )
        )


def test_table_rejects_empty_rows():
    with pytest.raises(BlockValidationError):
        blocks.Table(rows=())


def test_table_reports_column_count():
    table = blocks.Table(rows=[[blocks.text_cell("a"), blocks.text_cell("b")]])
    assert table.column_count == 2
    assert isinstance(table.rows[0], tuple)


@pytest.mark.parametrize("rel_id", ["", "1img", "img id", "img/1"])
def test_image_rejects_invalid_rel_id(rel_id):
    with pytest.raises(BlockValidationError):
        blocks.Image(rel_id=rel_id, width_emu=10, height_emu=10)


@pytest.mark.parametrize(("width", "height"), [(0, 10), (10, 0), (-1, 10)])
def test_image_rejects_non_positive_dimensions(width, height):
    with pytest.raises(BlockValidationError):
        blocks.Image(rel_id="rIdImage1", width_emu=width, height_emu=height)
