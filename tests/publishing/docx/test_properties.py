from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wiki_ai.publishing.docx.properties import (
    CoreProperties,
    app_xml,
    core_xml,
    format_timestamp,
    parse_core_xml,
    parse_timestamp,
    sharepoint_safe_filename,
    split_keywords,
)


def test_core_xml_carries_title_and_keywords():
    xml = core_xml(CoreProperties(title="Guia", keywords=("wiki", "docx")))
    assert "<dc:title>Guia</dc:title>" in xml
    assert "<cp:keywords>wiki; docx</cp:keywords>" in xml


def test_core_xml_omits_empty_fields():
    xml = core_xml(CoreProperties(title="Guia"))
    assert "dc:subject" not in xml
    assert "cp:category" not in xml
    assert "dcterms:created" not in xml


def test_core_xml_escapes_reserved_characters():
    xml = core_xml(CoreProperties(title='A & B < C > "D"'))
    assert "<dc:title>A &amp; B &lt; C &gt; \"D\"</dc:title>" in xml


def test_core_xml_drops_invalid_xml_characters():
    xml = core_xml(CoreProperties(title="a\x00b\x0bc"))
    assert "<dc:title>abc</dc:title>" in xml


def test_core_xml_uses_w3cdtf_for_dates():
    moment = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    xml = core_xml(CoreProperties(created=moment, modified=moment))
    assert '<dcterms:created xsi:type="dcterms:W3CDTF">2026-03-04T05:06:07Z</dcterms:created>' in xml
    assert '<dcterms:modified xsi:type="dcterms:W3CDTF">2026-03-04T05:06:07Z</dcterms:modified>' in xml


def test_core_xml_roundtrips_through_parse():
    original = CoreProperties(
        title="Titulo",
        subject="Assunto",
        creator="Equipe",
        keywords=("a", "b"),
        description="Resumo",
        created=datetime(2026, 1, 1, tzinfo=timezone.utc),
        modified=datetime(2026, 1, 2, tzinfo=timezone.utc),
        revision=7,
        category="capacidade",
    )
    assert parse_core_xml(core_xml(original).encode("utf-8")) == original


def test_format_timestamp_converts_to_utc():
    moment = datetime(2026, 1, 1, 3, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    assert format_timestamp(moment) == "2026-01-01T00:00:00Z"


def test_parse_timestamp_accepts_date_only_and_rejects_garbage():
    assert parse_timestamp("2026-01-01") == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert parse_timestamp("ontem") is None


def test_split_keywords_accepts_comma_and_semicolon():
    assert split_keywords("a; b, c") == ("a", "b", "c")


def test_app_xml_declares_application_and_company():
    xml = app_xml(CoreProperties(creator="Equipe"))
    assert "<Application>wiki-ai</Application>" in xml
    assert "<Company>Equipe</Company>" in xml


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("Guia: Publicacao/2026", "guia-publicacao-2026.docx"),
        ("con", "con-doc.docx"),
        ("~$rascunho", "rascunho.docx"),
        ("  ...  ", "documento.docx"),
        ("pasta_vti_x", "pasta_vti_x-doc.docx"),
    ],
)
def test_sharepoint_safe_filename(stem, expected):
    assert sharepoint_safe_filename(stem) == expected


def test_sharepoint_safe_filename_truncates_long_stem():
    name = sharepoint_safe_filename("a" * 300)
    assert len(name) <= 125
