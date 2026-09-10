from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from wiki_ai.ingestion.adapters import documents
from wiki_ai.ingestion.adapters.blocks import IMAGE_GAP_CODE, BlockBuilder, image_gap
from wiki_ai.ingestion.source import BlockKind, SourceDocument, SourceKind

__all__ = [
    "PageText",
    "PdfTextExtractor",
    "StreamTextExtractor",
    "Confidence",
    "adapt",
    "OCR_UNAVAILABLE",
    "MALFORMED_DOCUMENT",
    "NO_TEXT_LAYER",
    "IMAGE_GAP_CODE",
]

OCR_UNAVAILABLE = "ocr_unavailable"
MALFORMED_DOCUMENT = "malformed_document"
NO_TEXT_LAYER = "no_text_layer"

_HEADER = b"%PDF"
_OBJECT = re.compile(rb"(\d+)\s+(\d+)\s+obj(.*?)endobj", re.DOTALL)
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\n?endstream", re.DOTALL)
_INFO_ENTRY = re.compile(rb"/(\w+)\s*\(((?:[^()\\]|\\.)*)\)")
_PAGE_TYPE = re.compile(rb"/Type\s*/Page[^s]")
_COUNT = re.compile(rb"/Count\s+(\d+)")
_FLATE = re.compile(rb"/Filter\s*(?:\[\s*)?/FlateDecode")
_IMAGE_SUBTYPE = re.compile(rb"/Subtype\s*/Image")
_XOBJECT = re.compile(rb"/XObject")
_SHOW_TEXT = re.compile(rb"\((?:[^()\\]|\\.)*\)\s*(?:Tj|TJ|'|\")")
_ARRAY_TEXT = re.compile(rb"\[(.*?)\]\s*TJ", re.DOTALL)
_LITERAL = re.compile(rb"\((?:[^()\\]|\\.)*\)")
_ESCAPES = {
    b"\\n": b"\n",
    b"\\r": b"\r",
    b"\\t": b"\t",
    b"\\b": b"\b",
    b"\\f": b"\f",
    b"\\(": b"(",
    b"\\)": b")",
    b"\\\\": b"\\",
}


class Confidence(str):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class PageText:
    page: int
    text: str
    confidence: str = Confidence.LOW
    has_image: bool = False


@runtime_checkable
class PdfTextExtractor(Protocol):
    def extract(self, path: str | Path) -> Sequence[PageText]: ...


@dataclass(frozen=True)
class PdfObject:
    number: int
    body: bytes
    stream: bytes


def _objects(payload: bytes) -> list[PdfObject]:
    found: list[PdfObject] = []
    for match in _OBJECT.finditer(payload):
        body = match.group(3)
        stream_match = _STREAM.search(body)
        stream = stream_match.group(1) if stream_match else b""
        head = body[: stream_match.start()] if stream_match else body
        found.append(PdfObject(number=int(match.group(1)), body=head, stream=stream))
    return found


def _unescape(literal: bytes) -> bytes:
    text = literal[1:-1]
    for token, replacement in _ESCAPES.items():
        text = text.replace(token, replacement)
    return text


def _decode_stream(obj: PdfObject) -> bytes | None:
    if not obj.stream:
        return None
    if _FLATE.search(obj.body):
        try:
            return zlib.decompress(obj.stream)
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(obj.stream)
            except zlib.error:
                return None
    return obj.stream


def _content_text(content: bytes) -> str:
    parts: list[str] = []
    for match in _SHOW_TEXT.finditer(content):
        literal = _LITERAL.search(match.group(0))
        if literal is not None:
            parts.append(_unescape(literal.group(0)).decode("latin-1"))
    for match in _ARRAY_TEXT.finditer(content):
        chunk = "".join(
            _unescape(item.group(0)).decode("latin-1")
            for item in _LITERAL.finditer(match.group(1))
        )
        if chunk and chunk not in parts:
            parts.append(chunk)
    return " ".join(part for part in parts if part.strip()).strip()


def _info(payload: bytes) -> dict[str, str]:
    found: dict[str, str] = {}
    for obj in _objects(payload):
        if b"/Producer" not in obj.body and b"/Title" not in obj.body and b"/Author" not in obj.body:
            continue
        for match in _INFO_ENTRY.finditer(obj.body):
            key = match.group(1).decode("latin-1")
            found.setdefault(key, _unescape(b"(" + match.group(2) + b")").decode("latin-1"))
    return found


def _page_count(payload: bytes, objects: list[PdfObject]) -> int:
    declared = 0
    for obj in objects:
        if b"/Type" in obj.body and b"/Pages" in obj.body:
            match = _COUNT.search(obj.body)
            if match:
                declared = max(declared, int(match.group(1)))
    counted = sum(1 for obj in objects if _PAGE_TYPE.search(obj.body + b" "))
    return declared or counted or (1 if payload.startswith(_HEADER) else 0)


class StreamTextExtractor:
    def extract(self, path: str | Path) -> Sequence[PageText]:
        payload = Path(path).read_bytes()
        return self.extract_bytes(payload)

    def extract_bytes(self, payload: bytes) -> Sequence[PageText]:
        objects = _objects(payload)
        pages = [obj for obj in objects if _PAGE_TYPE.search(obj.body + b" ")]
        streams = {obj.number: obj for obj in objects if obj.stream}
        images = {
            obj.number for obj in objects if _IMAGE_SUBTYPE.search(obj.body)
        }
        found: list[PageText] = []
        total = len(pages) or _page_count(payload, objects)
        if not pages:
            text = " ".join(
                _content_text(_decode_stream(obj) or b"") for obj in streams.values()
            ).strip()
            has_image = bool(images)
            return (
                PageText(page=1, text=text, confidence=Confidence.LOW, has_image=has_image),
            ) if (text or has_image) else ()
        for index, page in enumerate(pages, start=1):
            references = [int(n) for n in re.findall(rb"(\d+)\s+\d+\s+R", page.body)]
            has_image = bool(_XOBJECT.search(page.body)) or any(
                ref in images for ref in references
            )
            chunks: list[str] = []
            for ref in references:
                obj = streams.get(ref)
                if obj is None:
                    continue
                content = _decode_stream(obj)
                if content is None:
                    continue
                chunk = _content_text(content)
                if chunk:
                    chunks.append(chunk)
            found.append(
                PageText(
                    page=index,
                    text=" ".join(chunks).strip(),
                    confidence=Confidence.LOW,
                    has_image=has_image,
                )
            )
        if total > len(found):
            found.extend(
                PageText(page=index, text="", confidence=Confidence.LOW, has_image=True)
                for index in range(len(found) + 1, total + 1)
            )
        return tuple(found)


def adapt(
    path: str | Path,
    payload: bytes,
    captured_at: datetime | None = None,
    extractor: PdfTextExtractor | None = None,
) -> SourceDocument:
    target = Path(path)
    info = _info(payload) if payload.startswith(_HEADER) else {}
    metadata: dict[str, Any] = {"source_type": SourceKind.PDF.value}
    if info.get("Title"):
        metadata["title"] = info["Title"]
    source, metadata_diagnostics = documents.build_source(
        target, SourceKind.PDF, payload, metadata, captured_at
    )
    builder = BlockBuilder(source.id)
    builder.extend_diagnostics(metadata_diagnostics)

    if not payload.startswith(_HEADER):
        builder.fail(
            MALFORMED_DOCUMENT,
            f"{target.name} carries no %PDF header",
            {"page": 1, "block": "document"},
        )
        return SourceDocument(source=source, blocks=(), diagnostics=builder.diagnostics)

    objects = _objects(payload)
    total = _page_count(payload, objects)
    builder.add(
        BlockKind.OTHER,
        "\n".join(f"{key}: {value}" for key, value in sorted(info.items())),
        {"page": 0, "block": "document_properties"},
        attributes={"properties": dict(sorted(info.items())), "pages": total},
    )

    engine = extractor or StreamTextExtractor()
    if isinstance(engine, StreamTextExtractor):
        pages = engine.extract_bytes(payload)
    else:
        pages = engine.extract(target)

    if not pages:
        builder.warn(
            NO_TEXT_LAYER,
            f"{target.name} exposes no page object the structural adapter can read",
            {"page": 1, "block": "document"},
        )

    for page in pages:
        locator = {"page": page.page, "block": f"page:{page.page}"}
        if page.text:
            builder.add(
                BlockKind.PARAGRAPH,
                page.text,
                locator,
                attributes={"confidence": page.confidence, "has_image": page.has_image},
            )
        else:
            builder.add(
                BlockKind.IMAGE,
                "",
                locator,
                attributes={"gap": True, "confidence": page.confidence, "has_image": True},
            )
            image_gap(builder, f"{target.name} page {page.page}", locator)
            builder.warn(
                OCR_UNAVAILABLE,
                f"page {page.page} needs an optical capability that this runtime "
                "does not provide",
                locator,
            )

    return SourceDocument(
        source=source, blocks=builder.blocks, diagnostics=builder.diagnostics
    )
