from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from wiki_ai.ingestion.adapters import documents
from wiki_ai.ingestion.adapters.blocks import BlockBuilder
from wiki_ai.ingestion.source import BlockKind, SourceDocument, SourceKind

__all__ = ["adapt", "HtmlOutline", "HtmlNode"]

_HEADINGS = {f"h{level}": level for level in range(1, 7)}
_TEXT_TAGS = frozenset({"p", "li", "pre", "code", "td", "th", "caption", "title"})
_TABLE_TAGS = frozenset({"table", "tr", "td", "th"})
_SKIP = frozenset({"script", "style"})
_WS = re.compile(r"\s+")


class HtmlNode:
    def __init__(self, tag: str, attributes: dict[str, str], path: str) -> None:
        self.tag = tag
        self.attributes = attributes
        self.path = path
        self.text: list[str] = []
        self.links: list[dict[str, str]] = []
        self.row = 0
        self.col = 0


class HtmlOutline(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[HtmlNode] = []
        self._stack: list[HtmlNode] = []
        self._counts: list[dict[str, int]] = [{}]
        self._skip = 0
        self._table = 0
        self._row = 0
        self._col = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
            return
        counter = self._counts[-1]
        counter[tag] = counter.get(tag, 0) + 1
        parent = self._stack[-1].path if self._stack else ""
        path = f"{parent}/{tag}[{counter[tag]}]"
        node = HtmlNode(tag, {k: (v or "") for k, v in attrs}, path)
        if tag == "table":
            self._table += 1
            self._row = 0
            self._col = 0
        elif tag == "tr":
            self._row += 1
            self._col = 0
        elif tag in {"td", "th"}:
            self._col += 1
            node.row = self._row
            node.col = self._col
        if tag == "a" and self._stack:
            self._stack[-1].links.append(
                {"target": node.attributes.get("href", ""), "text": ""}
            )
        self._stack.append(node)
        self._counts.append({})
        if tag in _HEADINGS or tag in _TEXT_TAGS or tag in _TABLE_TAGS:
            self.nodes.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                del self._counts[index + 1 :]
                break

    def handle_data(self, data: str) -> None:
        if self._skip or not self._stack:
            return
        chunk = _WS.sub(" ", data)
        if not chunk.strip():
            return
        for node in self._stack:
            node.text.append(chunk)
        if self._stack[-1].tag == "a":
            for holder in reversed(self._stack[:-1]):
                if holder.links:
                    holder.links[-1]["text"] = (
                        holder.links[-1]["text"] + chunk
                    ).strip()
                    break


def _text(node: HtmlNode) -> str:
    return _WS.sub(" ", "".join(node.text)).strip()


def adapt(
    path: str | Path, payload: bytes, captured_at: datetime | None = None
) -> SourceDocument:
    target = Path(path)
    outline = HtmlOutline()
    outline.feed(payload.decode("utf-8", errors="replace"))
    outline.close()

    title = next(
        (_text(node) for node in outline.nodes if node.tag == "title" and _text(node)),
        target.name,
    )
    source, metadata_diagnostics = documents.build_source(
        target,
        SourceKind.HTML,
        payload,
        {"source_type": SourceKind.HTML.value, "title": title},
        captured_at,
    )
    builder = BlockBuilder(source.id)
    builder.extend_diagnostics(metadata_diagnostics)

    stack: list[tuple[int, str]] = []
    table_blocks: dict[str, str] = {}
    for index, node in enumerate(outline.nodes, start=1):
        text = _text(node)
        locator: dict[str, Any] = {
            "path": node.path,
            "block": f"{node.tag}:{index}",
            "section": " / ".join(item for _, item in stack) or "document",
            "heading_path": [item for _, item in stack],
        }
        if node.tag in _HEADINGS:
            if not text:
                continue
            level = _HEADINGS[node.tag]
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            locator["section"] = " / ".join(item for _, item in stack)
            locator["heading_path"] = [item for _, item in stack]
            builder.add(BlockKind.HEADING, text, locator, attributes={"level": level})
        elif node.tag == "table":
            block = builder.add(
                BlockKind.TABLE,
                text,
                locator,
                attributes={"tag": node.tag},
            )
            table_blocks[node.path] = block.id
        elif node.tag in {"td", "th"}:
            locator["row"] = node.row
            locator["col"] = node.col
            parent = next(
                (
                    identifier
                    for key, identifier in table_blocks.items()
                    if node.path.startswith(key)
                ),
                None,
            )
            builder.add(
                BlockKind.CELL,
                text,
                locator,
                parent_id=parent,
                attributes={"header": node.tag == "th"},
            )
        elif node.tag == "tr":
            continue
        elif node.tag == "pre":
            builder.add(BlockKind.CODE, text, locator, attributes={"tag": node.tag})
        elif node.tag == "li":
            if text:
                builder.add(BlockKind.LIST_ITEM, text, locator, attributes={"tag": node.tag})
        elif node.tag == "title":
            continue
        elif text:
            attributes: dict[str, Any] = {"tag": node.tag}
            if node.links:
                attributes["hyperlinks"] = node.links
            builder.add(BlockKind.PARAGRAPH, text, locator, attributes=attributes)

    return SourceDocument(
        source=source, blocks=builder.blocks, diagnostics=builder.diagnostics
    )
