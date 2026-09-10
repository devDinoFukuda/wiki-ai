from __future__ import annotations

import pytest

from wiki_ai.ingestion.adapters.xmlsafe import (
    XmlMalformed,
    XmlNotAllowed,
    XmlRejected,
    XmlTooLarge,
    local_name,
    parse_defused,
)

XXE = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE root [<!ENTITY leak SYSTEM "file:///etc/passwd">]>'
    "<root>&leak;</root>"
)

BILLION_LAUGHS = (
    '<?xml version="1.0"?>'
    "<!DOCTYPE lolz ["
    '<!ENTITY lol "lol">'
    '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
    "]>"
    "<lolz>&lol2;</lolz>"
)

EXTERNAL_DTD = '<!DOCTYPE root SYSTEM "http://example.invalid/evil.dtd"><root/>'


def test_parses_plain_xml_and_keeps_qualified_names() -> None:
    root = parse_defused('<w:body xmlns:w="urn:w"><w:p>text</w:p></w:body>')
    assert root.tag == "w:body"
    child = list(root)[0]
    assert child.tag == "w:p"
    assert child.text == "text"


def test_accepts_bytes_and_str_alike() -> None:
    from_str = parse_defused("<root><a>1</a></root>")
    from_bytes = parse_defused(b"<root><a>1</a></root>")
    assert from_str.tag == from_bytes.tag
    assert list(from_str)[0].text == list(from_bytes)[0].text


@pytest.mark.parametrize("payload", [XXE, BILLION_LAUGHS, EXTERNAL_DTD])
def test_rejects_doctype_and_entity_declarations(payload: str) -> None:
    with pytest.raises(XmlNotAllowed):
        parse_defused(payload)


def test_xxe_never_reaches_the_filesystem() -> None:
    with pytest.raises(XmlNotAllowed) as raised:
        parse_defused(XXE)
    assert "DOCTYPE" in str(raised.value)
    assert "passwd" not in str(raised.value)


def test_undeclared_entity_reference_is_malformed_not_expanded() -> None:
    with pytest.raises(XmlMalformed):
        parse_defused("<root>&leak;</root>")


def test_rejects_payload_larger_than_the_limit() -> None:
    payload = b"<root>" + b"x" * 200 + b"</root>"
    with pytest.raises(XmlTooLarge) as raised:
        parse_defused(payload, max_bytes=64)
    assert raised.value.limit == 64
    assert raised.value.size == len(payload)


def test_malformed_xml_raises_typed_error() -> None:
    with pytest.raises(XmlMalformed):
        parse_defused("<root><unclosed></root>")


def test_every_rejection_shares_one_base_type() -> None:
    for payload, limit in ((XXE, 1 << 20), ("<a>", 1 << 20), ("<a/>", 1)):
        with pytest.raises(XmlRejected):
            parse_defused(payload, max_bytes=limit)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("w:p", "p"), ("{urn:x}p", "p"), ("p", "p"), ("{urn:x}w:p", "p")],
)
def test_local_name_strips_prefix_and_namespace(tag: str, expected: str) -> None:
    assert local_name(tag) == expected
