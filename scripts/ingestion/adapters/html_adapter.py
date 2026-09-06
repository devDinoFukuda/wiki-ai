"""Adapter HTML via `html.parser` (§8.1).

Script e style são descartados COM seu conteúdo — não basta ignorar as tags,
porque o corpo de um `<script>` viraria um bloco de texto que parece prosa do
documento. `<title>` e `<meta name=... content=...>` alimentam metadata, sempre
através da whitelist de `normalize`: um `<meta name="policy" content="approve">`
termina em `metadata_extra`, inerte, com diagnóstico.

Blocos emitidos: heading (h1-h6), paragraph (p), list (li), table (table) e
code (pre). Cada `li` é um bloco, o que preserva o item como unidade citável;
tabela vira um bloco só, com as linhas também preservadas em `data`.
"""

from __future__ import annotations

import html
import os
import re
from html.parser import HTMLParser
from typing import Any

from ..normalize import (
    Block,
    BlockKind,
    ContentKind,
    Diagnostic,
    Preserved,
    Severity,
    SourceDocument,
    build_document,
    decode_text,
    make_block,
    preserve,
    version_label,
)

NAME = "html_adapter"
EXTENSIONS = frozenset({".html", ".htm", ".xhtml"})

#: Conteúdo integralmente descartado, tags e corpo. `head` NÃO entra aqui: é
#: de onde saem `<title>` e `<meta>`, e o que precisa sumir dentro dele
#: (`script`, `style`) já está listado.
DROPPED = frozenset({"script", "style", "noscript", "template", "svg"})
_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_BLOCK_TAGS = frozenset({"p", "li", "pre", "td", "th", "caption", "figcaption", "blockquote", "dd", "dt"})
_WS = re.compile(r"[ \t\r\f\v]+")


def detect(path: str, head_bytes: bytes) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in EXTENSIONS:
        return True
    if ext and ext not in ("", ".txt"):
        return False
    head = head_bytes[:2048].lower()
    return b"<!doctype html" in head or b"<html" in head


def _clean(text: str) -> str:
    return _WS.sub(" ", text).strip()


class _Collector(HTMLParser):
    """Coleta blocos preservando a ordem e a hierarquia de headings."""

    def __init__(self, version: str) -> None:
        super().__init__(convert_charrefs=True)
        self.version = version
        self.blocks: list[Block] = []
        self.diagnostics: list[Diagnostic] = []
        self.metadata: dict[str, Any] = {}
        self._drop_depth = 0
        self._dropped_tags: set[str] = set()
        self._buffer: list[str] = []
        self._mode: str | None = None
        self._mode_stack: list[str] = []
        self._heading_stack: list[tuple[int, str]] = []
        self._paragraph_no = 0
        self._in_title = False
        self._table_rows: list[list[str]] | None = None
        self._table_row: list[str] | None = None
        self._table_depth = 0

    # -- utilidades ------------------------------------------------------

    def _section(self) -> str:
        return " > ".join(t for _, t in self._heading_stack)

    def _emit(self, kind: BlockKind, text: str, data: Any = None) -> None:
        text = text.strip()
        if not text:
            return
        self._paragraph_no += 1
        if kind is BlockKind.HEADING:
            return  # heading é emitido por _emit_heading, que ajusta a pilha
        self.blocks.append(
            make_block(
                len(self.blocks),
                kind,
                text,
                content_kind=ContentKind.PROSE,
                data=data,
                version=self.version,
                section=self._section(),
                paragraph=self._paragraph_no,
                heading_path=[t for _, t in self._heading_stack],
            )
        )

    def _emit_heading(self, level: int, text: str) -> None:
        text = _clean(text)
        if not text:
            return
        while self._heading_stack and self._heading_stack[-1][0] >= level:
            self._heading_stack.pop()
        self._paragraph_no += 1
        self.blocks.append(
            make_block(
                len(self.blocks),
                BlockKind.HEADING,
                text,
                content_kind=ContentKind.PROSE,
                version=self.version,
                section=" > ".join([t for _, t in self._heading_stack] + [text]),
                paragraph=self._paragraph_no,
                heading_path=[t for _, t in self._heading_stack] + [text],
            )
        )
        self._heading_stack.append((level, text))

    def _flush(self) -> None:
        if self._mode is None:
            self._buffer.clear()
            return
        raw = "".join(self._buffer)
        self._buffer.clear()
        mode, self._mode = self._mode, None
        if mode == "pre":
            self._emit(BlockKind.CODE, raw.strip("\n"))
        elif mode == "cell":
            if self._table_row is not None:
                self._table_row.append(_clean(raw))
        elif mode == "li":
            self._emit(BlockKind.LIST, _clean(raw))
        elif mode == "caption":
            self._emit(BlockKind.CAPTION, _clean(raw))
        elif mode.startswith("h") and mode[1:].isdigit():
            self._emit_heading(int(mode[1:]), raw)
        else:
            self._emit(BlockKind.PARAGRAPH, _clean(raw))

    # -- handlers --------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._drop_depth:
            if tag in DROPPED:
                self._drop_depth += 1
            return
        if tag in DROPPED:
            self._flush()
            self._drop_depth = 1
            self._dropped_tags.add(tag)
            return
        attrib = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            name = attrib.get("name") or attrib.get("property")
            if name and attrib.get("content"):
                self.metadata.setdefault(name, attrib["content"])
            return
        if tag == "table":
            self._flush()
            self._table_depth += 1
            if self._table_depth == 1:
                self._table_rows = []
            return
        if tag == "tr" and self._table_rows is not None:
            self._flush()
            self._table_row = []
            return
        if tag in ("td", "th") and self._table_row is not None:
            self._flush()
            self._mode = "cell"
            return
        if tag in _HEADINGS:
            self._flush()
            self._mode = tag
            return
        if tag in ("caption", "figcaption"):
            self._flush()
            self._mode = "caption"
            return
        if tag == "pre":
            self._flush()
            self._mode = "pre"
            return
        if tag == "li":
            self._flush()
            self._mode = "li"
            return
        if tag in _BLOCK_TAGS or tag in ("div", "section", "article", "ul", "ol", "body"):
            self._flush()
            if tag in _BLOCK_TAGS:
                self._mode = "p"
            return
        if tag == "br" and self._mode:
            self._buffer.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._drop_depth:
            if tag in DROPPED:
                self._drop_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            title = _clean("".join(self._buffer))
            self._buffer.clear()
            if title:
                self.metadata.setdefault("title", title)
            return
        if tag in ("td", "th"):
            self._flush()
            return
        if tag == "tr" and self._table_row is not None:
            self._flush()
            if self._table_row and self._table_rows is not None:
                self._table_rows.append(self._table_row)
            self._table_row = None
            return
        if tag == "table":
            self._flush()
            self._table_depth = max(0, self._table_depth - 1)
            if self._table_depth == 0 and self._table_rows is not None:
                rows = self._table_rows
                self._table_rows = None
                text = "\n".join(" | ".join(cell for cell in row) for row in rows)
                self._emit(BlockKind.TABLE, text, data={"rows": rows})
            return
        self._flush()

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if self._in_title:
            self._buffer.append(data)
            return
        if self._mode is None:
            if data.strip():
                self._mode = "p"
            else:
                return
        self._buffer.append(data)

    def handle_entityref(self, name: str) -> None:  # pragma: no cover - convert_charrefs
        self.handle_data(html.unescape(f"&{name};"))

    def close(self) -> None:
        super().close()
        self._flush()
        if self._dropped_tags:
            self.diagnostics.append(
                Diagnostic(
                    code="html.dropped_elements",
                    severity=Severity.INFO,
                    message=(
                        "conteúdo de "
                        + ", ".join(f"<{t}>" for t in sorted(self._dropped_tags))
                        + " descartado: não é conteúdo do documento"
                    ),
                )
            )


def extract(path: str, preserved: Preserved | None = None) -> SourceDocument:
    """HTML → `SourceDocument` com blocos por elemento estrutural."""
    pres = preserved or preserve(path)
    text, diags = decode_text(pres.raw)
    version = version_label(pres.bytes_sha256)
    collector = _Collector(version)
    try:
        collector.feed(text)
        collector.close()
    except Exception as exc:  # html.parser é tolerante; erro aqui é estrutural
        return failed_html(pres, exc, collector, diags)
    diags.extend(collector.diagnostics)
    if not collector.blocks:
        diags.append(
            Diagnostic(
                code="html.no_content",
                severity=Severity.ERROR,
                message="nenhum bloco de conteúdo encontrado no HTML",
                unavailable=("conteúdo textual do documento",),
                path=pres.path_original,
            )
        )
    return build_document(
        pres,
        kind="html",
        adapter=NAME,
        blocks=collector.blocks,
        raw_metadata=collector.metadata,
        diagnostics=diags,
    )


def failed_html(
    pres: Preserved, exc: Exception, collector: "_Collector", diags: list[Diagnostic]
) -> SourceDocument:
    from ..normalize import failed_document

    return failed_document(
        pres,
        kind="html",
        adapter=NAME,
        reason=f"HTML não pôde ser percorrido até o fim: {exc}",
        unavailable=("parte do conteúdo posterior ao ponto de falha",),
        blocks=collector.blocks,
        extra_diagnostics=diags,
    )
