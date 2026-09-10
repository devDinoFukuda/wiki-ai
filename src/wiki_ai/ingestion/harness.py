from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from wiki_ai.ingestion.hints import CandidateKind, hints_for
from wiki_ai.ingestion.source import (
    Block,
    BlockKind,
    SourceDocument,
    SourceKind,
    content_digest,
)
from wiki_ai.ingestion.toolspec import (
    MAX_BLOCKS_PER_PAGE,
    MAX_EXCERPT_CHARS,
    MAX_SEARCH_MATCHES,
    TOOL_BLOCKS,
    TOOL_EVIDENCE_CAPTURE,
    TOOL_GRAPH,
    TOOL_HINTS,
    TOOL_NAMES,
    TOOL_OUTLINE,
    TOOL_READ,
    TOOL_SEARCH,
    TOOL_TABLE,
    DocumentToolError,
    DocumentToolSpec,
    InvalidDocumentArguments,
    UnknownDocumentTool,
    build_specs,
    validate_arguments,
)

__all__ = [
    "DocumentToolError",
    "UnknownDocumentTool",
    "InvalidDocumentArguments",
    "DocumentToolSpec",
    "DocumentEvidenceCapture",
    "TOOL_NAMES",
    "DocumentHarness",
    "validate_arguments",
    "excerpt_hash",
]


def excerpt_hash(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DocumentEvidenceCapture:
    capture_id: str
    source_id: str
    version_hash: str
    source_kind: SourceKind
    block_ids: tuple[str, ...]
    locator: Mapping[str, Any]
    excerpt: str
    excerpt_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "source_id": self.source_id,
            "version_hash": self.version_hash,
            "source_kind": self.source_kind.value,
            "block_ids": list(self.block_ids),
            "locator": dict(self.locator),
            "excerpt": self.excerpt,
            "excerpt_hash": self.excerpt_hash,
        }


_SPECS: tuple[DocumentToolSpec, ...] = build_specs()

_RANGE_PATTERN = re.compile(r"^([A-Za-z]+)(\d+):([A-Za-z]+)(\d+)$")


def _column_index(letters: str) -> int:
    total = 0
    for char in letters.upper():
        total = total * 26 + (ord(char) - 64)
    return total


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class DocumentHarness:
    def __init__(self, document: SourceDocument) -> None:
        self._document = document
        self._blocks = tuple(document.blocks)
        self._by_id: Mapping[str, Block] = {block.id: block for block in self._blocks}
        self._by_order: Mapping[int, Block] = {
            block.order: block for block in self._blocks
        }
        self._handlers: Mapping[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            TOOL_OUTLINE: self._outline,
            TOOL_BLOCKS: self._blocks_page,
            TOOL_READ: self._read,
            TOOL_SEARCH: self._search,
            TOOL_TABLE: self._table,
            TOOL_GRAPH: self._graph,
            TOOL_HINTS: self._hints,
            TOOL_EVIDENCE_CAPTURE: self._capture,
        }
        self._captures: dict[str, DocumentEvidenceCapture] = {}

    @property
    def document(self) -> SourceDocument:
        return self._document

    @property
    def source_id(self) -> str:
        return self._document.source.id

    @property
    def version_hash(self) -> str:
        return self._document.source.version_hash

    @property
    def kind(self) -> SourceKind:
        return self._document.source.kind

    @property
    def title(self) -> str:
        declared = str(self._document.source.metadata.get("title") or "").strip()
        if declared:
            return declared
        uri = self._document.source.uri
        return uri.rsplit("/", 1)[-1] or uri

    def specs(self) -> tuple[DocumentToolSpec, ...]:
        return _SPECS

    def names(self) -> tuple[str, ...]:
        return TOOL_NAMES

    def spec_for(self, name: str) -> DocumentToolSpec:
        for spec in _SPECS:
            if spec.name == name:
                return spec
        raise UnknownDocumentTool(f"unknown tool: {name}")

    def captures(self) -> Mapping[str, DocumentEvidenceCapture]:
        return dict(self._captures)

    def capture(self, capture_id: str) -> DocumentEvidenceCapture | None:
        return self._captures.get(capture_id)

    def invoke(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        spec = self.spec_for(name)
        cleaned = validate_arguments(spec, arguments or {})
        return self._handlers[name](cleaned)

    def entry_key_of(self, block_id: str) -> str:
        block = self._by_id.get(str(block_id))
        return "" if block is None else self._entry_key(block)

    def frontier(self) -> tuple[str, ...]:
        return tuple(entry["key"] for entry in self._outline_entries())

    def _entry_key(self, block: Block) -> str:
        locator = block.locator
        if self.kind is SourceKind.XLSX:
            return f"worksheet:{_text(locator.get('worksheet'))}"
        if self.kind is SourceKind.TRANSCRIPT:
            return f"speaker:{_text(locator.get('speaker'))}"
        if self.kind is SourceKind.DRAWIO:
            return f"page:{_text(locator.get('page'))}"
        if self.kind is SourceKind.PDF:
            return f"page:{_text(locator.get('page'))}"
        section = _text(locator.get("section")) or _text(locator.get("path"))
        if not section:
            section = _text(locator.get("pointer")) or _text(locator.get("xpath"))
        return f"section:{section}"

    def _outline_entries(self) -> list[dict[str, Any]]:
        counts: dict[str, dict[str, Any]] = {}
        for block in self._blocks:
            key = self._entry_key(block)
            slot = counts.setdefault(
                key,
                {
                    "key": key,
                    "scope": key.split(":", 1)[0],
                    "label": key.split(":", 1)[1],
                    "blocks": 0,
                    "first_order": block.order,
                    "kinds": {},
                },
            )
            slot["blocks"] = int(slot["blocks"]) + 1
            kinds = slot["kinds"]
            kinds[block.kind.value] = kinds.get(block.kind.value, 0) + 1
        return [counts[key] for key in sorted(counts, key=lambda item: counts[item]["first_order"])]

    def _outline(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for block in self._blocks:
            by_kind[block.kind.value] = by_kind.get(block.kind.value, 0) + 1
        headings = [
            {
                "block_id": block.id,
                "order": block.order,
                "text": block.text,
                "locator": dict(block.locator),
                "level": block.attributes.get("level"),
            }
            for block in self._blocks
            if block.kind is BlockKind.HEADING
        ]
        return {
            "source_id": self.source_id,
            "kind": self.kind.value,
            "version_hash": self.version_hash,
            "uri": self._document.source.uri,
            "entries": self._outline_entries(),
            "headings": headings,
            "block_kinds": by_kind,
            "total_blocks": len(self._blocks),
            "gaps": [item.to_dict() for item in self._document.diagnostics],
        }

    def _selected(self, arguments: Mapping[str, Any]) -> list[Block]:
        raw_kinds = arguments.get("kinds") or ()
        kinds: list[BlockKind] = []
        for item in raw_kinds:
            try:
                kinds.append(BlockKind(str(item)))
            except ValueError:
                raise InvalidDocumentArguments(
                    f"kinds must be one of {sorted(k.value for k in BlockKind)}"
                ) from None
        section = arguments.get("section")
        worksheet = arguments.get("worksheet")
        speaker = arguments.get("speaker")
        page = arguments.get("page")
        found: list[Block] = []
        for block in self._blocks:
            if kinds and block.kind not in kinds:
                continue
            if section is not None and str(section) not in _text(
                block.locator.get("section")
            ):
                continue
            if worksheet is not None and _text(block.locator.get("worksheet")) != str(
                worksheet
            ):
                continue
            if speaker is not None and _text(block.locator.get("speaker")) != str(speaker):
                continue
            if page is not None and _text(block.locator.get("page")) != str(page):
                continue
            found.append(block)
        return found

    def _block_payload(self, block: Block, text: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "block_id": block.id,
            "order": block.order,
            "kind": block.kind.value,
            "locator": dict(block.locator),
            "attributes": dict(block.attributes),
        }
        payload["text"] = block.text if text else block.text[:200]
        return payload

    def _blocks_page(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        found = self._selected(arguments)
        offset = int(arguments.get("offset", 0) or 0)
        limit = int(arguments.get("limit", MAX_BLOCKS_PER_PAGE) or MAX_BLOCKS_PER_PAGE)
        window = found[offset : offset + limit]
        return {
            "source_id": self.source_id,
            "blocks": [self._block_payload(block, text=False) for block in window],
            "total": len(found),
            "offset": offset,
            "truncated": offset + limit < len(found),
        }

    def _read(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        block_id = arguments.get("block_id")
        if block_id is not None:
            block = self._by_id.get(str(block_id))
            if block is None:
                raise InvalidDocumentArguments(f"block not part of this source: {block_id}")
            return {"source_id": self.source_id, "blocks": [self._block_payload(block)], "total": 1}
        start = arguments.get("order_start")
        end = arguments.get("order_end")
        if start is None:
            raise InvalidDocumentArguments("doc.read needs a block_id or an order_start")
        first = int(start)
        last = int(end) if end is not None else first
        if last < first:
            raise InvalidDocumentArguments(f"invalid order range: {first}..{last}")
        found = [
            block for block in self._blocks if first <= block.order <= last
        ][:MAX_BLOCKS_PER_PAGE]
        return {
            "source_id": self.source_id,
            "blocks": [self._block_payload(block) for block in found],
            "total": len(found),
        }

    def _search(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        pattern = str(arguments["pattern"])
        flags = re.IGNORECASE if arguments.get("ignore_case") else 0
        if arguments.get("regex"):
            try:
                compiled = re.compile(pattern, flags)
            except re.error as exc:
                raise InvalidDocumentArguments(f"invalid regular expression: {exc}") from exc
        else:
            compiled = re.compile(re.escape(pattern), flags)
        limit = int(arguments.get("limit", MAX_SEARCH_MATCHES) or MAX_SEARCH_MATCHES)
        matches: list[dict[str, Any]] = []
        total = 0
        for block in self._selected(arguments):
            found = compiled.search(block.text)
            if found is None:
                continue
            total += 1
            if len(matches) >= limit:
                continue
            start = max(0, found.start() - 60)
            matches.append(
                {
                    "block_id": block.id,
                    "order": block.order,
                    "kind": block.kind.value,
                    "locator": dict(block.locator),
                    "excerpt": block.text[start : found.end() + 60],
                }
            )
        return {
            "source_id": self.source_id,
            "matches": matches,
            "total": total,
            "truncated": total > len(matches),
        }

    def _region_blocks(
        self, worksheet: str, cell_range: str
    ) -> tuple[tuple[Block, ...], str]:
        parsed = _RANGE_PATTERN.match(cell_range.replace("$", "").strip())
        if parsed is None:
            raise InvalidDocumentArguments(
                f"cell_range must look like B12:F27, got {cell_range!r}"
            )
        first_col = _column_index(parsed.group(1))
        first_row = int(parsed.group(2))
        last_col = _column_index(parsed.group(3))
        last_row = int(parsed.group(4))
        found: list[Block] = []
        for block in self._blocks:
            if _text(block.locator.get("worksheet")) != worksheet:
                continue
            row = block.attributes.get("row")
            column = block.attributes.get("column")
            if not isinstance(row, int) or not isinstance(column, int):
                continue
            if first_row <= row <= last_row and first_col <= column <= last_col:
                found.append(block)
        return tuple(found), f"{worksheet}!{cell_range}"

    def _grid_from_cells(
        self, cells: Sequence[Block], header_row: bool
    ) -> dict[str, Any]:
        rows: dict[int, dict[int, Block]] = {}
        for block in cells:
            row = block.attributes.get("row")
            column = block.attributes.get("column")
            if isinstance(row, int) and isinstance(column, int):
                rows.setdefault(row, {})[column] = block
        if not rows:
            return {"header": [], "rows": [], "block_ids": []}
        columns = sorted({column for line in rows.values() for column in line})
        ordered = sorted(rows)
        grid: list[list[str]] = []
        identifiers: list[str] = []
        for index in ordered:
            line = rows[index]
            grid.append([_text(line[column].text) if column in line else "" for column in columns])
            identifiers.extend(line[column].id for column in columns if column in line)
        header = grid[0] if header_row and grid else []
        body = grid[1:] if header_row and grid else grid
        return {"header": header, "rows": body, "block_ids": identifiers}

    def _table_from_block(self, block: Block, header_row: bool) -> dict[str, Any]:
        children = [
            item
            for item in self._blocks
            if item.parent_id == block.id and item.kind is BlockKind.CELL
        ]
        if children:
            rows: dict[int, dict[int, Block]] = {}
            for cell in children:
                row = cell.locator.get("row")
                column = cell.locator.get("col")
                if isinstance(row, int) and isinstance(column, int):
                    rows.setdefault(row, {})[column] = cell
            columns = sorted({column for line in rows.values() for column in line})
            grid = [
                [_text(rows[index][column].text) if column in rows[index] else "" for column in columns]
                for index in sorted(rows)
            ]
            identifiers = [
                rows[index][column].id for index in sorted(rows) for column in columns if column in rows[index]
            ]
        else:
            grid = [line.split("\t") for line in block.text.splitlines() if line]
            identifiers = []
        declared = block.attributes.get("header_row")
        header: list[str] = []
        body = grid
        if isinstance(declared, (list, tuple)) and declared:
            header = [str(item) for item in declared]
            if grid and [str(item) for item in grid[0]] == header:
                body = grid[1:]
        elif header_row and grid:
            header = grid[0]
            body = grid[1:]
        return {"header": header, "rows": body, "block_ids": [block.id] + identifiers}

    def _table(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        header_row = bool(arguments.get("header_row", True))
        block_id = arguments.get("block_id")
        if block_id is not None:
            block = self._by_id.get(str(block_id))
            if block is None:
                raise InvalidDocumentArguments(f"block not part of this source: {block_id}")
            if block.kind is BlockKind.TABLE:
                payload = self._table_from_block(block, header_row)
                payload["locator"] = dict(block.locator)
                payload["range"] = _text(block.locator.get("range"))
                payload["source_id"] = self.source_id
                return payload
            raise InvalidDocumentArguments(f"block {block_id} is not a table")
        worksheet = arguments.get("worksheet")
        cell_range = arguments.get("cell_range")
        if worksheet is None or cell_range is None:
            raise InvalidDocumentArguments(
                "doc.table needs a table block_id or a worksheet with a cell_range"
            )
        cells, reference = self._region_blocks(str(worksheet), str(cell_range))
        if not cells:
            raise InvalidDocumentArguments(f"no cell of this source lies in {reference}")
        payload = self._grid_from_cells(cells, header_row)
        payload["locator"] = {
            "workbook": _text(cells[0].locator.get("workbook")),
            "worksheet": str(worksheet),
            "range": str(cell_range),
        }
        payload["range"] = reference
        payload["source_id"] = self.source_id
        return payload

    def _graph(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        page = arguments.get("page")
        nodes = [
            block
            for block in self._blocks
            if block.kind is BlockKind.NODE
            and (page is None or _text(block.locator.get("page")) == str(page))
        ]
        edges = [
            block
            for block in self._blocks
            if block.kind is BlockKind.EDGE
            and (page is None or _text(block.locator.get("page")) == str(page))
        ]
        node = arguments.get("node")
        if node is not None:
            depth = int(arguments.get("depth", 1) or 1)
            reachable = {str(node)}
            for _step in range(depth):
                for edge in edges:
                    source = _text(edge.attributes.get("source"))
                    target = _text(edge.attributes.get("target"))
                    if source in reachable or target in reachable:
                        reachable.update({source, target} - {""})
            nodes = [
                block for block in nodes if _text(block.locator.get("node")) in reachable
            ]
            edges = [
                edge
                for edge in edges
                if _text(edge.attributes.get("source")) in reachable
                and _text(edge.attributes.get("target")) in reachable
            ]
        return {
            "source_id": self.source_id,
            "nodes": [
                {
                    "block_id": block.id,
                    "node": _text(block.locator.get("node")),
                    "label": block.text,
                    "page": _text(block.locator.get("page")),
                    "diagram": _text(block.locator.get("diagram")),
                    "style": block.attributes.get("style_parsed") or {},
                    "container": bool(block.attributes.get("container")),
                }
                for block in nodes
            ],
            "edges": [
                {
                    "block_id": block.id,
                    "edge": _text(block.locator.get("node")),
                    "label": block.text,
                    "page": _text(block.locator.get("page")),
                    "source": _text(block.attributes.get("source")),
                    "target": _text(block.attributes.get("target")),
                }
                for block in edges
            ],
            "pages": sorted(
                {
                    _text(block.locator.get("page"))
                    for block in self._blocks
                    if block.kind in (BlockKind.NODE, BlockKind.EDGE)
                }
            ),
        }

    def _hints(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        raw = arguments.get("candidate_kinds") or ()
        wanted: list[CandidateKind] = []
        for item in raw:
            try:
                wanted.append(CandidateKind(str(item)))
            except ValueError:
                raise InvalidDocumentArguments(
                    f"candidate_kinds must be one of "
                    f"{sorted(k.value for k in CandidateKind)}"
                ) from None
        found = hints_for(self._blocks, tuple(wanted))
        limit = int(arguments.get("limit", MAX_SEARCH_MATCHES) or MAX_SEARCH_MATCHES)
        return {
            "source_id": self.source_id,
            "hints": [item.to_dict() for item in found[:limit]],
            "total": len(found),
            "authoritative": False,
        }

    def typed_locator(self, blocks: Sequence[Block]) -> dict[str, Any]:
        first = blocks[0]
        locator = first.locator
        if self.kind is SourceKind.XLSX:
            cells = [
                _text(block.locator.get("cell") or block.locator.get("range"))
                for block in blocks
            ]
            present = [item for item in cells if item]
            reference = (
                f"{present[0]}:{present[-1]}"
                if len(present) > 1 and present[0] != present[-1]
                else (present[0] if present else "")
            )
            return {
                "kind": "spreadsheet",
                "workbook": _text(locator.get("workbook")),
                "worksheet": _text(locator.get("worksheet")),
                "cell_range": reference,
            }
        if self.kind is SourceKind.DRAWIO:
            return {
                "kind": "diagram",
                "diagram": _text(locator.get("diagram")),
                "page": _text(locator.get("page")),
                "node": _text(locator.get("node")),
            }
        if self.kind is SourceKind.TRANSCRIPT:
            return {
                "kind": "transcript",
                "speaker": _text(locator.get("speaker")),
                "time_start": _seconds(locator.get("time_start") or locator.get("time")),
                "time_end": _seconds(
                    blocks[-1].locator.get("time_end")
                    or blocks[-1].locator.get("time_start")
                    or locator.get("time")
                ),
            }
        heading = locator.get("heading_path")
        path = (
            tuple(str(item) for item in heading)
            if isinstance(heading, (list, tuple))
            else ()
        )
        if not path:
            section = _text(locator.get("section"))
            page = _text(locator.get("page"))
            path = tuple(item for item in (section, f"page {page}" if page else "") if item)
        return {"kind": "document", "block_id": first.id, "heading_path": list(path)}

    def _capture(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        raw = arguments["block_ids"]
        if not isinstance(raw, (list, tuple)) or not raw:
            raise InvalidDocumentArguments("evidence.capture needs at least one block id")
        blocks: list[Block] = []
        for item in raw:
            block = self._by_id.get(str(item))
            if block is None:
                raise InvalidDocumentArguments(f"block not part of this source: {item}")
            blocks.append(block)
        blocks.sort(key=lambda block: block.order)
        excerpt = "\n".join(block.text for block in blocks)[:MAX_EXCERPT_CHARS]
        locator = self.typed_locator(blocks)
        digest = content_digest(
            {
                "source_id": self.source_id,
                "version_hash": self.version_hash,
                "blocks": [block.id for block in blocks],
                "locator": locator,
            }
        )
        capture = DocumentEvidenceCapture(
            capture_id=f"cap-{digest[:24]}",
            source_id=self.source_id,
            version_hash=self.version_hash,
            source_kind=self.kind,
            block_ids=tuple(block.id for block in blocks),
            locator=locator,
            excerpt=excerpt,
            excerpt_hash=excerpt_hash(excerpt),
        )
        self._captures[capture.capture_id] = capture
        return capture.to_dict()


def _seconds(raw: Any) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    text = _text(raw).replace(",", ".")
    if not text:
        return 0.0
    parts = text.split(":")
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return 0.0
    total = 0.0
    for value in values:
        total = total * 60.0 + value
    return total
