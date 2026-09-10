from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from wiki_ai.ingestion.adapters import documents
from wiki_ai.ingestion.adapters.blocks import BlockBuilder
from wiki_ai.ingestion.source import BlockKind, SourceDocument, SourceKind

__all__ = ["adapt"]

_ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^(?P<marker>```+|~~~+)\s*(?P<language>[\w+.#-]*)\s*$")
_BULLET = re.compile(r"^(?P<indent>\s*)(?P<marker>[-*+]|\d+[.)])\s+(?P<text>.*)$")
_ROW = re.compile(r"^\s*\|.*\|\s*$")
_DIVIDER = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def _cells(line: str) -> list[str]:
    trimmed = line.strip()
    if trimmed.startswith("|"):
        trimmed = trimmed[1:]
    if trimmed.endswith("|"):
        trimmed = trimmed[:-1]
    return [cell.strip() for cell in trimmed.split("|")]


def _section(stack: list[tuple[int, str]]) -> str:
    return " / ".join(text for _, text in stack)


def _push(stack: list[tuple[int, str]], level: int, text: str) -> None:
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, text))


def adapt(
    path: str | Path, payload: bytes, captured_at: datetime | None = None
) -> SourceDocument:
    target = Path(path)
    text = payload.decode("utf-8", errors="replace")
    source, metadata_diagnostics = documents.build_source(
        target,
        SourceKind.MARKDOWN,
        payload,
        {"source_type": SourceKind.MARKDOWN.value, "title": target.name},
        captured_at,
    )
    builder = BlockBuilder(source.id)
    builder.extend_diagnostics(metadata_diagnostics)

    lines = text.splitlines()
    stack: list[tuple[int, str]] = []
    index = 0
    paragraph: list[str] = []
    paragraph_line = 0

    def flush(end_line: int) -> None:
        nonlocal paragraph
        if not paragraph:
            return
        builder.add(
            BlockKind.PARAGRAPH,
            " ".join(paragraph).strip(),
            _locator(stack, paragraph_line, end_line, "paragraph"),
        )
        paragraph = []

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        fence = _FENCE.match(stripped)
        if fence is not None:
            flush(index)
            marker = fence.group("marker")
            language = fence.group("language")
            start = index + 1
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(marker[0] * 3):
                body.append(lines[index])
                index += 1
            builder.add(
                BlockKind.CODE,
                "\n".join(body),
                _locator(stack, start, index, "code"),
                attributes={"language": language},
            )
            index += 1
            continue

        heading = _ATX.match(line)
        if heading is not None:
            flush(index)
            level = len(heading.group(1))
            title = heading.group(2).strip()
            _push(stack, level, title)
            builder.add(
                BlockKind.HEADING,
                title,
                _locator(stack, index + 1, index + 1, "heading"),
                attributes={"level": level},
            )
            index += 1
            continue

        if _ROW.match(line) and index + 1 < len(lines) and _DIVIDER.match(lines[index + 1]):
            flush(index)
            header = _cells(line)
            start = index + 1
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and _ROW.match(lines[index]):
                rows.append(_cells(lines[index]))
                index += 1
            table = builder.add(
                BlockKind.TABLE,
                "\n".join(["\t".join(header)] + ["\t".join(row) for row in rows]),
                _locator(stack, start, index, "table"),
                attributes={"header_row": header, "rows": len(rows) + 1},
            )
            for row_index, row in enumerate(rows, start=1):
                for col_index, cell in enumerate(row, start=1):
                    locator = _locator(stack, start, index, "table")
                    locator["row"] = row_index
                    locator["col"] = col_index
                    locator["block"] = f"table:{start}:r{row_index}c{col_index}"
                    builder.add(
                        BlockKind.CELL,
                        cell,
                        locator,
                        parent_id=table.id,
                        attributes={"column": header[col_index - 1] if col_index <= len(header) else ""},
                    )
            continue

        bullet = _BULLET.match(line)
        if bullet is not None:
            flush(index)
            builder.add(
                BlockKind.LIST_ITEM,
                bullet.group("text").strip(),
                _locator(stack, index + 1, index + 1, "list_item"),
                attributes={
                    "marker": bullet.group("marker"),
                    "ilvl": len(bullet.group("indent")) // 2,
                    "ordered": not bullet.group("marker") in {"-", "*", "+"},
                },
            )
            index += 1
            continue

        if not stripped:
            flush(index)
            index += 1
            continue

        if not paragraph:
            paragraph_line = index + 1
        paragraph.append(stripped)
        index += 1

    flush(len(lines))
    return SourceDocument(
        source=source, blocks=builder.blocks, diagnostics=builder.diagnostics
    )


def _locator(
    stack: list[tuple[int, str]], start: int, end: int, kind: str
) -> dict[str, Any]:
    return {
        "section": _section(stack) or "document",
        "block": f"{kind}:{start}",
        "heading_path": [text for _, text in stack],
        "start_line": start,
        "end_line": end,
    }
