from __future__ import annotations

import re
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from .errors import PackageValidationError

XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

_INVALID_XML_CHARS = re.compile(
    "[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)
_ATTRIBUTE_ENTITIES = {'"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}
_DOCTYPE = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def strip_invalid(value: str) -> str:
    return _INVALID_XML_CHARS.sub("", value)


def escape_text(value: str) -> str:
    return escape(strip_invalid(value))


def escape_attr(value: str) -> str:
    return escape(strip_invalid(value), _ATTRIBUTE_ENTITIES)


def parse_xml(part_name: str, data: bytes) -> ElementTree.Element:
    if _DOCTYPE.search(data):
        raise PackageValidationError(f"{part_name}: declaracao DOCTYPE/ENTITY recusada")
    parser = ElementTree.XMLParser()
    try:
        parser.feed(data)
        return parser.close()
    except ElementTree.ParseError as exc:
        raise PackageValidationError(f"{part_name}: XML malformado ({exc})") from exc


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
