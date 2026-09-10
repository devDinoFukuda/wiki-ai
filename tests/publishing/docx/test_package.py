from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree

import pytest

from wiki_ai.publishing.docx import (
    BulletList,
    CodeBlock,
    CoreProperties,
    Heading,
    Image,
    ListItem,
    PageBreak,
    Paragraph,
    Run,
    Table,
    TableOfContents,
    build_package,
    read_package,
    text_cell,
    text_runs,
)
from wiki_ai.publishing.docx.errors import PackageValidationError

REQUIRED_PARTS = (
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
    "word/styles.xml",
    "word/numbering.xml",
    "word/settings.xml",
    "word/_rels/document.xml.rels",
    "docProps/core.xml",
    "docProps/app.xml",
)


def _document_xml(path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def _part(path, name) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name).decode("utf-8")


def _rich_blocks(png: bytes):
    return [
        TableOfContents(),
        Heading(level=1, text="Relatorio"),
        Paragraph(runs=(Run(text="corpo "), Run(text="negrito", bold=True))),
        Heading(level=2, text="Secao"),
        BulletList(
            items=(
                ListItem(runs=text_runs("um")),
                ListItem(runs=text_runs("dois"), level=1),
            )
        ),
        BulletList(items=(ListItem(runs=text_runs("passo")),), ordered=True),
        Table(
            rows=(
                (text_cell("coluna a"), text_cell("coluna b")),
                (text_cell("valor a"), text_cell("valor b")),
            )
        ),
        CodeBlock(text="linha1\nlinha2", language="python"),
        Paragraph(runs=(Run(text="site", hyperlink="https://exemplo.dev/a?x=1&y=2"),)),
        Image(rel_id="rIdImage1", width_emu=914400, height_emu=457200, alt="diagrama"),
        PageBreak(),
    ]


def test_package_contains_all_required_parts(out_path, png_bytes):
    build_package(_rich_blocks(png_bytes), CoreProperties(), out_path, media={"rIdImage1": png_bytes})
    with zipfile.ZipFile(out_path) as archive:
        names = set(archive.namelist())
    assert set(REQUIRED_PARTS) <= names
    assert "word/media/rIdImage1.png" in names


def test_build_package_returns_path_and_creates_parents(tmp_path):
    target = tmp_path / "sub" / "dir" / "a.docx"
    result = build_package([Heading(level=1, text="x")], CoreProperties(), target)
    assert result == target
    assert target.exists()


def test_every_part_is_well_formed_xml(out_path, png_bytes):
    build_package(_rich_blocks(png_bytes), CoreProperties(), out_path, media={"rIdImage1": png_bytes})
    with zipfile.ZipFile(out_path) as archive:
        for name in archive.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                ElementTree.fromstring(archive.read(name))


def test_build_is_deterministic(tmp_path, png_bytes):
    first = tmp_path / "a.docx"
    second = tmp_path / "b.docx"
    properties = CoreProperties(title="T", created=datetime(2026, 1, 1, tzinfo=timezone.utc))
    build_package(_rich_blocks(png_bytes), properties, first, media={"rIdImage1": png_bytes})
    build_package(_rich_blocks(png_bytes), properties, second, media={"rIdImage1": png_bytes})
    assert first.read_bytes() == second.read_bytes()


def test_roundtrip_counts_structure(out_path, png_bytes):
    build_package(_rich_blocks(png_bytes), CoreProperties(), out_path, media={"rIdImage1": png_bytes})
    summary = read_package(out_path)
    assert summary.heading_count == 2
    assert summary.table_count == 1
    assert summary.list_item_count == 3
    assert summary.image_count == 1
    assert summary.hyperlink_targets == ("https://exemplo.dev/a?x=1&y=2",)
    assert summary.media_parts == ("word/media/rIdImage1.png",)
    assert "Relatorio" in summary.text
    assert "valor b" in summary.text
    assert "linha2" in summary.text


def test_roundtrip_preserves_properties(out_path):
    properties = CoreProperties(
        title="Guia de publicacao",
        subject="publicacao",
        creator="wiki",
        keywords=("sharepoint", "docx"),
        description="resumo",
        created=datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
        modified=datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
        revision=3,
        category="guia",
    )
    build_package([Heading(level=1, text="x")], properties, out_path)
    assert read_package(out_path).properties == properties


def test_core_xml_part_carries_title_and_keywords(out_path):
    properties = CoreProperties(title="Titulo & Cia", keywords=("alfa", "beta"))
    build_package([Heading(level=1, text="x")], properties, out_path)
    core = _part(out_path, "docProps/core.xml")
    assert "<dc:title>Titulo &amp; Cia</dc:title>" in core
    assert "<cp:keywords>alfa; beta</cp:keywords>" in core


def test_text_escaping_in_document(out_path):
    blocks = [Paragraph(runs=(Run(text='a & b < c > d "e"'),))]
    build_package(blocks, CoreProperties(), out_path)
    xml = _document_xml(out_path)
    assert "a &amp; b &lt; c &gt; d \"e\"" in xml
    assert read_package(out_path).text == 'a & b < c > d "e"'


def test_invalid_xml_characters_are_removed(out_path):
    blocks = [Paragraph(runs=(Run(text="ini\x00cio\x0bfim\ud800"),))]
    build_package(blocks, CoreProperties(), out_path)
    assert read_package(out_path).text == "iniciofim"


def test_hyperlink_target_is_escaped_in_rels(out_path):
    blocks = [Paragraph(runs=(Run(text="l", hyperlink="https://x.dev/?a=1&b=2"),))]
    build_package(blocks, CoreProperties(), out_path)
    rels = _part(out_path, "word/_rels/document.xml.rels")
    assert "Target=\"https://x.dev/?a=1&amp;b=2\"" in rels
    assert 'TargetMode="External"' in rels


def test_repeated_hyperlink_reuses_relationship(out_path):
    url = "https://x.dev/a"
    blocks = [
        Paragraph(runs=(Run(text="um", hyperlink=url),)),
        Paragraph(runs=(Run(text="dois", hyperlink=url),)),
    ]
    build_package(blocks, CoreProperties(), out_path)
    assert read_package(out_path).hyperlink_targets == (url,)


def test_heading_uses_native_style(out_path):
    build_package([Heading(level=3, text="t")], CoreProperties(), out_path)
    assert '<w:pStyle w:val="Heading3"/>' in _document_xml(out_path)


def test_list_uses_numbering_reference(out_path):
    blocks = [BulletList(items=(ListItem(runs=text_runs("x"), level=2),), ordered=True)]
    build_package(blocks, CoreProperties(), out_path)
    xml = _document_xml(out_path)
    assert '<w:ilvl w:val="2"/><w:numId w:val="2"/>' in xml
    assert '<w:pStyle w:val="ListNumber"/>' in xml


def test_table_header_row_repeats_and_is_bold(out_path):
    blocks = [Table(rows=((text_cell("h"),), (text_cell("v"),)), header=True)]
    build_package(blocks, CoreProperties(), out_path)
    xml = _document_xml(out_path)
    assert "<w:trPr><w:tblHeader/></w:trPr>" in xml
    assert xml.count("<w:b/>") == 1
    assert '<w:tblStyle w:val="TableGrid"/>' in xml


def test_table_without_header_has_no_repeat_row(out_path):
    blocks = [Table(rows=((text_cell("a"),), (text_cell("b"),)), header=False)]
    build_package(blocks, CoreProperties(), out_path)
    assert "<w:tblHeader/>" not in _document_xml(out_path)


def test_table_of_contents_emits_field_and_update_setting(out_path):
    build_package([TableOfContents()], CoreProperties(), out_path)
    assert "TOC" in _document_xml(out_path)
    assert '<w:updateFields w:val="true"/>' in _part(out_path, "word/settings.xml")


def test_settings_omits_update_fields_without_toc(out_path):
    build_package([Heading(level=1, text="x")], CoreProperties(), out_path)
    assert "updateFields" not in _part(out_path, "word/settings.xml")


def test_page_break_emits_break_run(out_path):
    build_package([PageBreak()], CoreProperties(), out_path)
    assert '<w:br w:type="page"/>' in _document_xml(out_path)


def test_image_declares_drawing_and_content_type(out_path, png_bytes):
    blocks = [Image(rel_id="rIdImage1", width_emu=100, height_emu=200, alt="alt")]
    build_package(blocks, CoreProperties(), out_path, media={"rIdImage1": png_bytes})
    xml = _document_xml(out_path)
    assert '<wp:extent cx="100" cy="200"/>' in xml
    assert '<a:blip r:embed="rIdImage1"/>' in xml
    assert 'descr="alt"' in xml
    content_types = _part(out_path, "[Content_Types].xml")
    assert '<Default Extension="png" ContentType="image/png"/>' in content_types
    rels = _part(out_path, "word/_rels/document.xml.rels")
    assert 'Target="media/rIdImage1.png"' in rels


def test_image_without_media_is_rejected(out_path):
    blocks = [Image(rel_id="rIdImage1", width_emu=100, height_emu=200)]
    with pytest.raises(PackageValidationError):
        build_package(blocks, CoreProperties(), out_path)


def test_media_without_image_block_is_rejected(out_path, png_bytes):
    with pytest.raises(PackageValidationError):
        build_package([], CoreProperties(), out_path, media={"rIdImage1": png_bytes})


def test_unknown_media_format_is_rejected(out_path):
    blocks = [Image(rel_id="rIdImage1", width_emu=10, height_emu=10)]
    with pytest.raises(PackageValidationError):
        build_package(blocks, CoreProperties(), out_path, media={"rIdImage1": b"nao-e-imagem"})


def test_image_rel_id_colliding_with_fixed_part_is_rejected(out_path, png_bytes):
    blocks = [Image(rel_id="rId1", width_emu=10, height_emu=10)]
    with pytest.raises(PackageValidationError):
        build_package(blocks, CoreProperties(), out_path, media={"rId1": png_bytes})


def test_duplicated_image_rel_id_is_rejected(out_path, png_bytes):
    blocks = [
        Image(rel_id="rIdImage1", width_emu=10, height_emu=10),
        Image(rel_id="rIdImage1", width_emu=10, height_emu=10),
    ]
    with pytest.raises(PackageValidationError):
        build_package(blocks, CoreProperties(), out_path, media={"rIdImage1": png_bytes})


def test_unknown_block_type_is_rejected(out_path):
    with pytest.raises(PackageValidationError):
        build_package(["nao e um bloco"], CoreProperties(), out_path)


def test_code_block_keeps_lines_and_language(out_path):
    build_package([CodeBlock(text="a\n\nb", language="sql")], CoreProperties(), out_path)
    summary = read_package(out_path)
    assert summary.text.splitlines() == ["sql", "a", "", "b"]
    assert '<w:pStyle w:val="Code"/>' in _document_xml(out_path)


def test_code_run_uses_code_character_style(out_path):
    build_package(
        [Paragraph(runs=(Run(text="x", code=True),))], CoreProperties(), out_path
    )
    assert '<w:rStyle w:val="CodeChar"/>' in _document_xml(out_path)


def test_section_properties_close_the_body(out_path):
    build_package([Heading(level=1, text="x")], CoreProperties(), out_path)
    xml = _document_xml(out_path)
    assert xml.index("<w:sectPr>") > xml.index('<w:pStyle w:val="Heading1"/>')
    assert xml.endswith("</w:sectPr></w:body></w:document>")
