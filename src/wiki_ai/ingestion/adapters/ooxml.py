from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from typing import Iterator, Mapping
from xml.etree.ElementTree import Element

from wiki_ai.ingestion.adapters.xmlsafe import XmlRejected, local_name, parse_defused

__all__ = [
    "PackageUnreadable",
    "OoxmlPackage",
    "open_package",
    "attribute",
    "children",
    "descendants",
    "find_child",
    "relationships",
    "CORRUPT_ARCHIVE",
    "XML_REJECTED",
]

CORRUPT_ARCHIVE = "corrupt_archive"
XML_REJECTED = "xml_rejected"

_RELATIONSHIP_TAG = "Relationship"


class PackageUnreadable(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class OoxmlPackage:
    archive: zipfile.ZipFile

    def names(self) -> tuple[str, ...]:
        return tuple(self.archive.namelist())

    def has(self, name: str) -> bool:
        return name in self.archive.namelist()

    def raw(self, name: str) -> bytes | None:
        try:
            return self.archive.read(name)
        except (KeyError, zipfile.BadZipFile, OSError, RuntimeError):
            return None

    def size(self, name: str) -> int:
        try:
            return self.archive.getinfo(name).file_size
        except (KeyError, zipfile.BadZipFile):
            return 0

    def xml(self, name: str) -> Element | None:
        payload = self.raw(name)
        if payload is None:
            return None
        return parse_defused(payload)

    def close(self) -> None:
        self.archive.close()


def open_package(payload: bytes) -> OoxmlPackage:
    try:
        archive = zipfile.ZipFile(BytesIO(payload))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise PackageUnreadable(str(exc)) from exc
    broken = archive.testzip()
    if broken is not None:
        raise PackageUnreadable(f"entry {broken} is corrupt")
    return OoxmlPackage(archive=archive)


def attribute(element: Element, name: str, default: str = "") -> str:
    for key, value in element.attrib.items():
        if local_name(key) == name:
            return value
    return default


def children(element: Element, name: str) -> list[Element]:
    return [child for child in element if local_name(child.tag) == name]


def find_child(element: Element, name: str) -> Element | None:
    found = children(element, name)
    return found[0] if found else None


def descendants(element: Element, name: str) -> Iterator[Element]:
    for node in element.iter():
        if local_name(node.tag) == name:
            yield node


def relationships(package: OoxmlPackage, part: str) -> Mapping[str, tuple[str, str]]:
    folder, _, filename = part.rpartition("/")
    rels_name = f"{folder}/_rels/{filename}.rels" if folder else f"_rels/{filename}.rels"
    found: dict[str, tuple[str, str]] = {}
    try:
        root = package.xml(rels_name)
    except XmlRejected:
        return found
    if root is None:
        return found
    for node in root:
        if local_name(node.tag) != _RELATIONSHIP_TAG:
            continue
        found[attribute(node, "Id")] = (
            attribute(node, "Type").rsplit("/", 1)[-1],
            attribute(node, "Target"),
        )
    return found


_WS = re.compile(r"[ \t\r\f\v]+")


def collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()
