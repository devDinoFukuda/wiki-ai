from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from wiki_ai.ingestion.adapters import documents
from wiki_ai.ingestion.adapters.blocks import IMAGE_GAP_CODE, BlockBuilder, image_gap
from wiki_ai.ingestion.outcome import Gap, StructuralFault
from wiki_ai.ingestion.source import BlockKind, SourceDocument, SourceKind

__all__ = [
    "LIBRARY",
    "Confidence",
    "PageText",
    "PdfReadout",
    "PdfTextExtractor",
    "LibraryUnavailable",
    "DocumentUnreadable",
    "LibraryTextExtractor",
    "adapt",
    "OCR_UNAVAILABLE",
    "MALFORMED_DOCUMENT",
    "NO_TEXT_LAYER",
    "PDF_LIBRARY_UNAVAILABLE",
    "IMAGE_GAP_CODE",
]

LIBRARY = "pypdf"
OCR_UNAVAILABLE = Gap.OCR_UNAVAILABLE.value
MALFORMED_DOCUMENT = StructuralFault.MALFORMED_DOCUMENT.value
NO_TEXT_LAYER = Gap.NO_TEXT_LAYER.value
PDF_LIBRARY_UNAVAILABLE = StructuralFault.PDF_LIBRARY_UNAVAILABLE.value

_HEADER = b"%PDF"
_METADATA_FIELDS: tuple[tuple[str, str], ...] = (
    ("Title", "title"),
    ("Author", "author"),
    ("Subject", "subject"),
    ("Creator", "creator"),
    ("Producer", "producer"),
)


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class LibraryUnavailable(Exception):
    def __init__(self, library: str) -> None:
        self.library = library
        super().__init__(
            f"{library} is not installed in this runtime; install the pdf extra"
        )


class DocumentUnreadable(Exception):
    def __init__(self, target: str, reason: str) -> None:
        self.target = target
        self.reason = reason
        super().__init__(f"{target} could not be parsed as a pdf: {reason}")


@dataclass(frozen=True)
class PageText:
    page: int
    text: str
    confidence: Confidence = Confidence.LOW
    has_image: bool = False


@dataclass(frozen=True)
class PdfReadout:
    pages: tuple[PageText, ...] = ()
    properties: dict[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "pages", tuple(self.pages))
        object.__setattr__(self, "properties", dict(self.properties or {}))


@runtime_checkable
class PdfTextExtractor(Protocol):
    def extract(self, path: str | Path, payload: bytes) -> PdfReadout: ...


def _load_library() -> Any:
    try:
        import pypdf
    except ModuleNotFoundError as exc:
        raise LibraryUnavailable(LIBRARY) from exc
    return pypdf


class LibraryTextExtractor:
    def extract(self, path: str | Path, payload: bytes) -> PdfReadout:
        library = _load_library()
        try:
            reader = library.PdfReader(io.BytesIO(payload))
            pages = tuple(
                self._page(index, page)
                for index, page in enumerate(reader.pages, start=1)
            )
            properties = self._properties(reader)
        except LibraryUnavailable:
            raise
        except Exception as exc:
            raise DocumentUnreadable(Path(path).name, str(exc)) from exc
        return PdfReadout(pages=pages, properties=properties)

    def _page(self, index: int, page: Any) -> PageText:
        text = str(page.extract_text() or "").strip()
        return PageText(
            page=index,
            text=" ".join(text.split()),
            confidence=Confidence.HIGH if text else Confidence.LOW,
            has_image=self._has_image(page),
        )

    def _has_image(self, page: Any) -> bool:
        try:
            return len(page.images) > 0
        except Exception:
            return "/XObject" in str(page.get("/Resources") or "")

    def _properties(self, reader: Any) -> dict[str, str]:
        metadata = reader.metadata
        if metadata is None:
            return {}
        found: dict[str, str] = {}
        for key, attribute in _METADATA_FIELDS:
            value = getattr(metadata, attribute, None)
            if value is None:
                value = metadata.get(f"/{key}")
            text = "" if value is None else str(value).strip()
            if text:
                found[key] = text
        return found


def adapt(
    path: str | Path,
    payload: bytes,
    captured_at: datetime | None = None,
    extractor: PdfTextExtractor | None = None,
) -> SourceDocument:
    target = Path(path)
    engine = extractor or LibraryTextExtractor()
    readout: PdfReadout | None = None
    failure: str = ""
    if payload.startswith(_HEADER):
        try:
            readout = engine.extract(target, payload)
        except LibraryUnavailable:
            failure = PDF_LIBRARY_UNAVAILABLE
        except DocumentUnreadable:
            failure = MALFORMED_DOCUMENT
    else:
        failure = MALFORMED_DOCUMENT

    properties = dict(readout.properties or {}) if readout is not None else {}
    metadata: dict[str, Any] = {"source_type": SourceKind.PDF.value}
    if properties.get("Title"):
        metadata["title"] = properties["Title"]
    source, metadata_diagnostics = documents.build_source(
        target, SourceKind.PDF, payload, metadata, captured_at
    )
    builder = BlockBuilder(source.id)
    builder.extend_diagnostics(metadata_diagnostics)

    if failure == PDF_LIBRARY_UNAVAILABLE:
        builder.fail(
            PDF_LIBRARY_UNAVAILABLE,
            f"{target.name} needs {LIBRARY}, which this runtime does not provide",
            {"page": 1, "block": "document"},
        )
        return SourceDocument(source=source, blocks=(), diagnostics=builder.diagnostics)
    if failure == MALFORMED_DOCUMENT or readout is None:
        builder.fail(
            MALFORMED_DOCUMENT,
            f"{target.name} is not a pdf {LIBRARY} can read",
            {"page": 1, "block": "document"},
        )
        return SourceDocument(source=source, blocks=(), diagnostics=builder.diagnostics)

    pages = readout.pages
    builder.add(
        BlockKind.OTHER,
        "\n".join(f"{key}: {value}" for key, value in sorted(properties.items())),
        {"page": 0, "block": "document_properties"},
        attributes={"properties": dict(sorted(properties.items())), "pages": len(pages)},
    )

    if not pages:
        builder.warn(
            NO_TEXT_LAYER,
            f"{target.name} exposes no page {LIBRARY} can read",
            {"page": 1, "block": "document"},
        )

    for page in pages:
        locator = {"page": page.page, "block": f"page:{page.page}"}
        if page.text:
            builder.add(
                BlockKind.PARAGRAPH,
                page.text,
                locator,
                attributes={
                    "confidence": page.confidence.value,
                    "has_image": page.has_image,
                },
            )
            continue
        builder.add(
            BlockKind.IMAGE,
            "",
            locator,
            attributes={
                "gap": True,
                "confidence": page.confidence.value,
                "has_image": True,
            },
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
