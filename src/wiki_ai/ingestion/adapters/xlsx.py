from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from wiki_ai.ingestion.adapters import documents, ooxml, sheet
from wiki_ai.ingestion.adapters.blocks import BlockBuilder
from wiki_ai.ingestion.adapters.cellref import CROSS_SHEET_PATTERN, range_reference
from wiki_ai.ingestion.adapters.ooxml import (
    CORRUPT_ARCHIVE,
    OoxmlPackage,
    PackageUnreadable,
    XML_REJECTED,
    attribute,
    children,
    collapse,
    descendants,
    find_child,
    relationships,
)
from wiki_ai.ingestion.adapters.sheet import CellValue, SheetGrid
from wiki_ai.ingestion.adapters.xmlsafe import XmlRejected
from wiki_ai.ingestion.source import BlockKind, SourceDocument, SourceKind

__all__ = ["adapt", "WORKBOOK_PART", "CellValue", "SheetGrid"]

WORKBOOK_PART = "xl/workbook.xml"


def _cross_sheet(formula: str) -> list[str]:
    found: list[str] = []
    for match in CROSS_SHEET_PATTERN.finditer(formula):
        sheet = match.group("quoted") or match.group("plain")
        found.append(f"{sheet}!{match.group('col')}{match.group('row')}")
    return found


def _declared_tables(
    package: OoxmlPackage, builder: BlockBuilder
) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for name in sorted(package.names()):
        if not re.fullmatch(r"xl/tables/table\d+\.xml", name):
            continue
        try:
            root = package.xml(name)
        except XmlRejected as exc:
            builder.fail(XML_REJECTED, f"{name}: {exc}", {"part": name})
            continue
        if root is None:
            continue
        columns = [
            attribute(node, "name")
            for holder in descendants(root, "tableColumns")
            for node in children(holder, "tableColumn")
        ]
        found.setdefault(name, []).append(
            {
                "name": attribute(root, "displayName") or attribute(root, "name"),
                "ref": attribute(root, "ref"),
                "header_row_count": attribute(root, "headerRowCount", "1"),
                "columns": columns,
            }
        )
    return found


def _sheet_table_map(
    package: OoxmlPackage, sheet_part: str, declared: Mapping[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for _, target in relationships(package, sheet_part).values():
        resolved = target.replace("../", "xl/")
        for name, entries in declared.items():
            if name.endswith(resolved.rsplit("/", 1)[-1]):
                found.extend(entries)
    return found


def _named_ranges(root: Any) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    holder = find_child(root, "definedNames")
    if holder is None:
        return found
    for node in children(holder, "definedName"):
        found.append(
            {
                "name": attribute(node, "name"),
                "target": collapse(node.text or ""),
                "scope": attribute(node, "localSheetId"),
            }
        )
    return found


def _comments(
    package: OoxmlPackage, sheet_part: str, builder: BlockBuilder
) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for _, target in relationships(package, sheet_part).values():
        if "comments" not in target:
            continue
        name = "xl/" + target.replace("../", "")
        if not package.has(name):
            continue
        try:
            root = package.xml(name)
        except XmlRejected as exc:
            builder.fail(XML_REJECTED, f"{name}: {exc}", {"part": name})
            continue
        if root is None:
            continue
        authors = [
            collapse(node.text or "")
            for holder in descendants(root, "authors")
            for node in children(holder, "author")
        ]
        for holder in descendants(root, "commentList"):
            for node in children(holder, "comment"):
                try:
                    author = authors[int(attribute(node, "authorId", "0"))]
                except (ValueError, IndexError):
                    author = ""
                found.append(
                    {
                        "cell": attribute(node, "ref").replace("$", "").upper(),
                        "author": author,
                        "text": collapse(
                            " ".join(n.text or "" for n in descendants(node, "t"))
                        ),
                    }
                )
    return found


def _sheet_parts(
    package: OoxmlPackage, workbook: Any
) -> list[tuple[str, bool, str]]:
    rels = relationships(package, WORKBOOK_PART)
    found: list[tuple[str, bool, str]] = []
    holder = find_child(workbook, "sheets")
    if holder is None:
        return found
    for index, node in enumerate(children(holder, "sheet"), start=1):
        name = attribute(node, "name")
        state = attribute(node, "state", "visible")
        rel_id = attribute(node, "id")
        target = rels.get(rel_id, ("", ""))[1]
        if target:
            part = "xl/" + target.replace("../", "").lstrip("/")
        else:
            part = f"xl/worksheets/sheet{index}.xml"
        found.append((name, state == "visible", part))
    return found


def adapt(
    path: str | Path, payload: bytes, captured_at: datetime | None = None
) -> SourceDocument:
    target = Path(path)
    try:
        package = ooxml.open_package(payload)
    except PackageUnreadable as exc:
        return documents.empty_document(
            target,
            SourceKind.XLSX,
            payload,
            CORRUPT_ARCHIVE,
            f"{target.name} is not a readable OOXML package: {exc.reason}",
            captured_at=captured_at,
        )
    try:
        return _adapt_package(target, payload, package, captured_at)
    finally:
        package.close()


def _adapt_package(
    target: Path, payload: bytes, package: OoxmlPackage, captured_at: datetime | None
) -> SourceDocument:
    workbook_name = target.name
    source, metadata_diagnostics = documents.build_source(
        target,
        SourceKind.XLSX,
        payload,
        {"source_type": SourceKind.XLSX.value, "title": workbook_name},
        captured_at,
    )
    builder = BlockBuilder(source.id)
    builder.extend_diagnostics(metadata_diagnostics)

    try:
        workbook = package.xml(WORKBOOK_PART)
    except XmlRejected as exc:
        builder.fail(XML_REJECTED, f"{WORKBOOK_PART}: {exc}", {"workbook": workbook_name})
        workbook = None
    if workbook is None:
        if not builder.diagnostics:
            builder.fail(
                CORRUPT_ARCHIVE,
                f"{workbook_name} has no {WORKBOOK_PART}",
                {"workbook": workbook_name},
            )
        return SourceDocument(
            source=source, blocks=builder.blocks, diagnostics=builder.diagnostics
        )

    strings = sheet.shared_strings(package, builder)
    formats = sheet.number_formats(package)
    declared = _declared_tables(package, builder)

    for name, target_ref in (
        (entry["name"], entry["target"]) for entry in _named_ranges(workbook)
    ):
        builder.add(
            BlockKind.OTHER,
            f"{name} = {target_ref}",
            {
                "workbook": workbook_name,
                "worksheet": target_ref.split("!")[0].strip("'") or workbook_name,
                "range": target_ref.split("!")[-1].replace("$", "") or name,
                "named_range": name,
            },
            attributes={"named_range": name, "target": target_ref},
        )

    for sheet_name, visible, part in _sheet_parts(package, workbook):
        _emit_sheet(
            package,
            builder,
            workbook_name,
            sheet_name,
            visible,
            part,
            strings,
            formats,
            declared,
        )

    return SourceDocument(
        source=source, blocks=builder.blocks, diagnostics=builder.diagnostics
    )


def _emit_sheet(
    package: OoxmlPackage,
    builder: BlockBuilder,
    workbook_name: str,
    sheet_name: str,
    visible: bool,
    part: str,
    strings: list[str],
    formats: dict[int, str],
    declared: Mapping[str, list[dict[str, Any]]],
) -> None:
    if not package.has(part):
        builder.warn(
            "worksheet_missing",
            f"{sheet_name} declares part {part} which the package does not contain",
            {"workbook": workbook_name, "worksheet": sheet_name, "range": "A1"},
        )
        return
    try:
        root = package.xml(part)
    except XmlRejected as exc:
        builder.fail(
            XML_REJECTED,
            f"{part}: {exc}",
            {"workbook": workbook_name, "worksheet": sheet_name, "range": "A1"},
        )
        return
    if root is None:
        return

    grid = sheet.read_sheet(root, sheet_name, visible, strings, formats)
    comments = {entry["cell"]: entry for entry in _comments(package, part, builder)}

    for cell in grid.cells:
        attributes: dict[str, Any] = {
            "value_type": cell.value_type,
            "visible": visible,
            "row": cell.row,
            "column": cell.column,
        }
        if cell.formula:
            attributes["formula"] = cell.formula
            references = _cross_sheet(cell.formula)
            if references:
                attributes["cross_sheet_refs"] = references
        annotation = comments.get(cell.reference)
        if annotation is not None:
            attributes["comment"] = annotation["text"]
            attributes["comment_author"] = annotation["author"]
        merged = [ref for ref in grid.merged if sheet.covers(ref, cell.row, cell.column)]
        if merged:
            attributes["merged_range"] = merged[0]
        validations = [
            entry for entry in grid.validations if sheet.covers_any(entry["range"], cell.row, cell.column)
        ]
        if validations:
            attributes["data_validation"] = validations[0]
        builder.add(
            BlockKind.CELL,
            cell.text or cell.formula,
            {
                "workbook": workbook_name,
                "worksheet": sheet_name,
                "cell": cell.reference,
                "range": cell.reference,
            },
            attributes=attributes,
        )

    declared_for_sheet = _sheet_table_map(package, part, declared)
    emitted: set[str] = set()
    for entry in declared_for_sheet:
        reference = entry["ref"].replace("$", "").upper()
        emitted.add(reference)
        builder.add(
            BlockKind.TABLE,
            sheet.region_text(grid.cells, sheet.parse_range(reference)),
            {
                "workbook": workbook_name,
                "worksheet": sheet_name,
                "range": reference,
            },
            attributes={
                "declared": True,
                "table_name": entry["name"],
                "header_row": entry["columns"],
                "visible": visible,
            },
        )

    for region in sheet.regions(grid.cells):
        reference = range_reference(*region)
        if reference in emitted:
            continue
        header = sheet.header_row(grid.cells, region)
        if not header:
            continue
        builder.add(
            BlockKind.TABLE,
            sheet.region_text(grid.cells, region),
            {
                "workbook": workbook_name,
                "worksheet": sheet_name,
                "range": reference,
            },
            attributes={"declared": False, "header_row": header, "visible": visible},
        )
