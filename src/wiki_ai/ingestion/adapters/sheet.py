from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from wiki_ai.ingestion.adapters.blocks import BlockBuilder
from wiki_ai.ingestion.adapters.cellref import cell_reference, split_reference
from wiki_ai.ingestion.adapters.ooxml import (
    OoxmlPackage,
    XML_REJECTED,
    attribute,
    children,
    collapse,
    descendants,
    find_child,
)
from wiki_ai.ingestion.adapters.xmlsafe import XmlRejected

__all__ = [
    "CellValue",
    "SheetGrid",
    "SHARED_STRINGS",
    "STYLES",
    "shared_strings",
    "number_formats",
    "read_sheet",
    "regions",
    "header_row",
    "parse_range",
    "region_text",
    "covers",
    "covers_any",
]

SHARED_STRINGS = "xl/sharedStrings.xml"
STYLES = "xl/styles.xml"
_DATE_FORMATS = frozenset(range(14, 23)) | frozenset({27, 30, 36, 45, 46, 47, 50, 57})
_DATE_TOKENS = re.compile(r"[dmyhs]", re.IGNORECASE)
_TEXT_FORMAT = "@"


@dataclass(frozen=True)
class CellValue:
    row: int
    column: int
    reference: str
    text: str
    value_type: str
    formula: str
    style: str


@dataclass(frozen=True)
class SheetGrid:
    name: str
    visible: bool
    cells: tuple[CellValue, ...]
    merged: tuple[str, ...]
    validations: tuple[dict[str, str], ...]


def shared_strings(package: OoxmlPackage, builder: BlockBuilder) -> list[str]:
    if not package.has(SHARED_STRINGS):
        return []
    try:
        root = package.xml(SHARED_STRINGS)
    except XmlRejected as exc:
        builder.fail(XML_REJECTED, f"{SHARED_STRINGS}: {exc}", {"part": "sharedStrings"})
        return []
    if root is None:
        return []
    found: list[str] = []
    for item in children(root, "si"):
        found.append(
            "".join(node.text or "" for node in descendants(item, "t"))
        )
    return found


def number_formats(package: OoxmlPackage) -> dict[int, str]:
    if not package.has(STYLES):
        return {}
    try:
        root = package.xml(STYLES)
    except XmlRejected:
        return {}
    if root is None:
        return {}
    custom: dict[int, str] = {}
    formats = find_child(root, "numFmts")
    if formats is not None:
        for node in children(formats, "numFmt"):
            try:
                custom[int(attribute(node, "numFmtId"))] = attribute(node, "formatCode")
            except ValueError:
                continue
    styles = find_child(root, "cellXfs")
    resolved: dict[int, str] = {}
    if styles is None:
        return resolved
    for index, node in enumerate(children(styles, "xf")):
        raw = attribute(node, "numFmtId", "0")
        try:
            fmt_id = int(raw)
        except ValueError:
            continue
        code = custom.get(fmt_id, "")
        if fmt_id in _DATE_FORMATS or (code and _DATE_TOKENS.search(code) and code != _TEXT_FORMAT):
            resolved[index] = "date"
        elif code == _TEXT_FORMAT:
            resolved[index] = "text"
        else:
            resolved[index] = "number"
    return resolved


def _cell_text(cell: Any, strings: list[str], cell_type: str) -> str:
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in descendants(cell, "t"))
    value = find_child(cell, "v")
    raw = (value.text or "") if value is not None else ""
    if cell_type == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "TRUE" if raw.strip() == "1" else "FALSE"
    return raw


def _value_type(cell_type: str, style_kind: str, text: str) -> str:
    if cell_type == "s" or cell_type == "inlineStr" or cell_type == "str":
        return "text"
    if cell_type == "b":
        return "boolean"
    if cell_type == "e":
        return "error"
    if style_kind == "date":
        return "date"
    if style_kind == "text":
        return "text"
    try:
        float(text)
    except ValueError:
        return "text"
    return "number"


def read_sheet(
    root: Any, name: str, visible: bool, strings: list[str], formats: dict[int, str]
) -> SheetGrid:
    cells: list[CellValue] = []
    data = find_child(root, "sheetData")
    if data is not None:
        for row in children(data, "row"):
            for cell in children(row, "c"):
                reference = attribute(cell, "r")
                position = split_reference(reference)
                if position is None:
                    continue
                cell_type = attribute(cell, "t")
                style_raw = attribute(cell, "s", "0")
                try:
                    style_kind = formats.get(int(style_raw), "number")
                except ValueError:
                    style_kind = "number"
                text = collapse(_cell_text(cell, strings, cell_type))
                formula_node = find_child(cell, "f")
                formula = (formula_node.text or "") if formula_node is not None else ""
                if not text and not formula:
                    continue
                cells.append(
                    CellValue(
                        row=position[0],
                        column=position[1],
                        reference=reference.replace("$", "").upper(),
                        text=text,
                        value_type=_value_type(cell_type, style_kind, text),
                        formula=formula,
                        style=style_kind,
                    )
                )
    merged: list[str] = []
    merge_root = find_child(root, "mergeCells")
    if merge_root is not None:
        merged = [attribute(node, "ref") for node in children(merge_root, "mergeCell")]
    validations: list[dict[str, str]] = []
    for holder in descendants(root, "dataValidations"):
        for node in children(holder, "dataValidation"):
            formula1 = find_child(node, "formula1")
            validations.append(
                {
                    "range": attribute(node, "sqref"),
                    "type": attribute(node, "type"),
                    "operator": attribute(node, "operator"),
                    "formula": (formula1.text or "") if formula1 is not None else "",
                    "prompt": attribute(node, "prompt"),
                }
            )
    return SheetGrid(
        name=name,
        visible=visible,
        cells=tuple(sorted(cells, key=lambda c: (c.row, c.column))),
        merged=tuple(m for m in merged if m),
        validations=tuple(validations),
    )


def regions(cells: tuple[CellValue, ...]) -> list[tuple[int, int, int, int]]:
    occupied = {(cell.row, cell.column) for cell in cells}
    seen: set[tuple[int, int]] = set()
    found: list[tuple[int, int, int, int]] = []
    for start in sorted(occupied):
        if start in seen:
            continue
        stack = [start]
        component: set[tuple[int, int]] = set()
        seen.add(start)
        while stack:
            row, column = stack.pop()
            component.add((row, column))
            for delta_row, delta_column in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbour = (row + delta_row, column + delta_column)
                if neighbour in occupied and neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        rows = [r for r, _ in component]
        columns = [c for _, c in component]
        found.append((min(rows), min(columns), max(rows), max(columns)))
    return sorted(found)


def header_row(cells: tuple[CellValue, ...], region: tuple[int, int, int, int]) -> list[str]:
    top, left, bottom, right = region
    if bottom <= top:
        return []
    index = {(cell.row, cell.column): cell for cell in cells}
    header = [index.get((top, column)) for column in range(left, right + 1)]
    if not all(cell is not None and cell.text for cell in header):
        return []
    if not all(cell.value_type == "text" for cell in header if cell is not None):
        return []
    below = [
        index.get((row, column))
        for row in range(top + 1, bottom + 1)
        for column in range(left, right + 1)
    ]
    if not any(cell is not None and cell.text for cell in below):
        return []
    return [cell.text for cell in header if cell is not None]


def parse_range(reference: str) -> tuple[int, int, int, int]:
    start, _, end = reference.partition(":")
    first = split_reference(start) or (1, 1)
    last = split_reference(end or start) or first
    return first[0], first[1], last[0], last[1]


def region_text(cells: tuple[CellValue, ...], region: tuple[int, int, int, int]) -> str:
    top, left, bottom, right = region
    index = {(cell.row, cell.column): cell.text for cell in cells}
    lines: list[str] = []
    for row in range(top, bottom + 1):
        lines.append(
            "\t".join(index.get((row, column), "") for column in range(left, right + 1))
        )
    return "\n".join(lines)


def covers(reference: str, row: int, column: int) -> bool:
    if not reference or ":" not in reference:
        return reference.replace("$", "").upper() == cell_reference(row, column)
    top, left, bottom, right = parse_range(reference.replace("$", "").upper())
    return top <= row <= bottom and left <= column <= right


def covers_any(references: str, row: int, column: int) -> bool:
    return any(covers(part, row, column) for part in references.split() if part)
