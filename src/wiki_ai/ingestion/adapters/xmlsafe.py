from __future__ import annotations

import xml.parsers.expat as expat
from xml.etree.ElementTree import Element, TreeBuilder

__all__ = [
    "XmlRejected",
    "XmlNotAllowed",
    "XmlMalformed",
    "XmlTooLarge",
    "MAX_XML_BYTES",
    "parse_defused",
    "local_name",
]

MAX_XML_BYTES = 32 * 1024 * 1024


class XmlRejected(ValueError):
    pass


class XmlNotAllowed(XmlRejected):
    def __init__(self, construct: str) -> None:
        self.construct = construct
        super().__init__(
            f"{construct} refused: ingested XML declares no DTD and no entity"
        )


class XmlMalformed(XmlRejected):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"invalid XML: {reason}")


class XmlTooLarge(XmlRejected):
    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"XML payload of {size} bytes exceeds the limit of {limit} bytes")


def _as_bytes(data: bytes | str) -> bytes:
    return data.encode("utf-8") if isinstance(data, str) else bytes(data)


def parse_defused(data: bytes | str, *, max_bytes: int = MAX_XML_BYTES) -> Element:
    payload = _as_bytes(data)
    if len(payload) > max_bytes:
        raise XmlTooLarge(len(payload), max_bytes)

    builder = TreeBuilder()
    parser = expat.ParserCreate()
    parser.buffer_text = True

    def _doctype(
        name: str, sysid: str | None, pubid: str | None, has_internal_subset: bool
    ) -> None:
        raise XmlNotAllowed(f"DOCTYPE {name!r}")

    def _entity_decl(*_args: object) -> None:
        raise XmlNotAllowed("entity declaration")

    def _external(*_args: object) -> bool:
        raise XmlNotAllowed("external entity reference")

    parser.StartDoctypeDeclHandler = _doctype
    parser.EntityDeclHandler = _entity_decl
    parser.UnparsedEntityDeclHandler = _entity_decl
    parser.ExternalEntityRefHandler = _external
    parser.StartElementHandler = lambda tag, attrs: builder.start(tag, attrs)
    parser.EndElementHandler = lambda tag: builder.end(tag)
    parser.CharacterDataHandler = builder.data

    try:
        parser.Parse(payload, True)
    except XmlRejected:
        raise
    except expat.ExpatError as exc:
        raise XmlMalformed(str(exc)) from exc
    try:
        return builder.close()
    except Exception as exc:
        raise XmlMalformed(f"incomplete element tree: {exc}") from exc


def local_name(tag: str) -> str:
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag
