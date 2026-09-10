from __future__ import annotations

import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from wiki_ai.ingestion.adapters.documents import empty_document
from wiki_ai.ingestion.outcome import StructuralFault
from wiki_ai.ingestion.source import SourceDocument, SourceKind

__all__ = [
    "EXTENSION_KINDS",
    "detect",
    "detect_bytes",
    "adapt",
    "adapt_many",
    "read_bytes",
    "kinds",
    "UNSUPPORTED_FORMAT",
    "UNREADABLE_SOURCE",
]

UNSUPPORTED_FORMAT = StructuralFault.UNSUPPORTED_FORMAT.value
UNREADABLE_SOURCE = StructuralFault.UNREADABLE_SOURCE.value

EXTENSION_KINDS: dict[str, SourceKind] = {
    ".docx": SourceKind.DOCX,
    ".docm": SourceKind.DOCX,
    ".xlsx": SourceKind.XLSX,
    ".xlsm": SourceKind.XLSX,
    ".drawio": SourceKind.DRAWIO,
    ".pdf": SourceKind.PDF,
    ".vtt": SourceKind.TRANSCRIPT,
    ".srt": SourceKind.TRANSCRIPT,
    ".md": SourceKind.MARKDOWN,
    ".markdown": SourceKind.MARKDOWN,
    ".html": SourceKind.HTML,
    ".htm": SourceKind.HTML,
    ".json": SourceKind.JSON,
    ".jsonl": SourceKind.JSON,
    ".ndjson": SourceKind.JSON,
    ".xml": SourceKind.XML,
    ".txt": SourceKind.TRANSCRIPT,
}

_SPEAKER_LINE = re.compile(
    r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*[^:]{1,80}:"
    r"|[^()\n]{1,80}\(\d{1,2}:\d{2}(?::\d{2})?\)\s*:"
    r"|\d{1,2}:\d{2}(?::\d{2})?\s+[^:\n]{1,80}:)",
    re.MULTILINE,
)
_ZIP_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_SNIFF = 8192


def _zip_kind(path: Path) -> SourceKind | None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    if any(name.startswith("word/") for name in names):
        return SourceKind.DOCX
    if any(name.startswith("xl/") for name in names):
        return SourceKind.XLSX
    return None


def _looks_srt(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    return lines[0].isdigit() and "-->" in lines[1]


def _looks_transcript(text: str) -> bool:
    if text.lstrip("\ufeff").startswith("WEBVTT"):
        return True
    if _looks_srt(text):
        return True
    return len(_SPEAKER_LINE.findall(text)) >= 2


def _text_head(head: bytes) -> str:
    return head.decode("utf-8", errors="replace")


def detect_bytes(head: bytes, suffix: str = "") -> SourceKind | None:
    if head.startswith(_PDF_MAGIC):
        return SourceKind.PDF
    text = _text_head(head)
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("<mxfile") or stripped.startswith("<mxGraphModel"):
        return SourceKind.DRAWIO
    if _looks_transcript(text):
        return SourceKind.TRANSCRIPT
    if suffix and suffix in EXTENSION_KINDS:
        return EXTENSION_KINDS[suffix]
    if stripped.startswith("<?xml") or stripped.startswith("<"):
        return SourceKind.XML
    return None


def _drawio_embedded(path: Path, suffix: str) -> bool:
    name = path.name.lower()
    if name.endswith(".drawio.png") or name.endswith(".drawio.svg"):
        return True
    return suffix in {".png", ".svg"} and ".drawio." in name


def detect(path: str | Path) -> SourceKind | None:
    target = Path(path)
    suffix = target.suffix.lower()
    if _drawio_embedded(target, suffix):
        return SourceKind.DRAWIO
    try:
        with target.open("rb") as handle:
            head = handle.read(_MAX_SNIFF)
    except OSError:
        return None
    if head.startswith(_ZIP_MAGIC):
        found = _zip_kind(target)
        if found is not None:
            return found
        return EXTENSION_KINDS.get(suffix)
    if head.startswith(_PNG_MAGIC) and suffix == ".png":
        return None
    sniffed = detect_bytes(head, suffix)
    if sniffed is not None:
        return sniffed
    return EXTENSION_KINDS.get(suffix)


def read_bytes(path: str | Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


Adapter = Callable[[Path, bytes, datetime | None], SourceDocument]


def _adapters() -> dict[SourceKind, Adapter]:
    from wiki_ai.ingestion.adapters import (
        docx,
        drawio,
        html,
        json as json_adapter,
        markdown,
        pdf,
        transcript,
        xlsx,
        xml as xml_adapter,
    )

    return {
        SourceKind.DOCX: docx.adapt,
        SourceKind.XLSX: xlsx.adapt,
        SourceKind.DRAWIO: drawio.adapt,
        SourceKind.PDF: pdf.adapt,
        SourceKind.TRANSCRIPT: transcript.adapt,
        SourceKind.MARKDOWN: markdown.adapt,
        SourceKind.HTML: html.adapt,
        SourceKind.JSON: json_adapter.adapt,
        SourceKind.XML: xml_adapter.adapt,
    }


def adapt(path: str | Path, captured_at: datetime | None = None) -> SourceDocument:
    target = Path(path)
    payload = read_bytes(target)
    if payload is None:
        return empty_document(
            target,
            SourceKind.JSON,
            b"",
            UNREADABLE_SOURCE,
            f"{target.name} could not be read from disk",
            captured_at=captured_at,
        )
    kind = detect(target)
    if kind is None:
        return empty_document(
            target,
            SourceKind.JSON,
            payload,
            UNSUPPORTED_FORMAT,
            f"{target.name} matches no known source format",
            captured_at=captured_at,
        )
    adapter = _adapters().get(kind)
    if adapter is None:
        return empty_document(
            target,
            kind,
            payload,
            UNSUPPORTED_FORMAT,
            f"{kind.value} has no structural adapter",
            captured_at=captured_at,
        )
    return adapter(target, payload, captured_at)


def adapt_many(
    root: str | Path,
    captured_at: datetime | None = None,
    suffixes: Iterable[str] | None = None,
) -> tuple[SourceDocument, ...]:
    base = Path(root)
    if base.is_file():
        return (adapt(base, captured_at),)
    allowed = {s.lower() for s in suffixes} if suffixes else None
    found: list[SourceDocument] = []
    for entry in sorted(p for p in base.rglob("*") if p.is_file()):
        if allowed is not None and entry.suffix.lower() not in allowed:
            continue
        found.append(adapt(entry, captured_at))
    return tuple(found)


def kinds() -> Sequence[SourceKind]:
    return tuple(sorted(set(EXTENSION_KINDS.values()), key=lambda k: k.value))
