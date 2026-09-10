from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from wiki_ai.knowledge.evidence import CodeContent, CodeLocator, make_evidence
from wiki_ai.knowledge.model import Evidence, SourceVersion
from wiki_ai.repository.evidence import EvidenceCapture
from wiki_ai.repository.snapshot import RepositorySnapshot

__all__ = [
    "CaptureRegistry",
    "capture_id",
    "detect_content",
    "source_version_of",
    "to_knowledge",
]

_LINE_COMMENT_MARKERS = ("//", "#", "--", "*", "'", ";", "%", "rem ")
_BLOCK_COMMENT_MARKERS = ("/*", "*/", "<!--", "-->", "{-", "-}", "(*", "*)")
_DOCSTRING_MARKERS = ('"""', "'''", "/**", "///", "##", '"""')
_CONFIG_SUFFIXES = (
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".properties",
    ".env",
    ".conf",
)
_MARKUP_SUFFIXES = (".md", ".rst", ".txt", ".adoc", ".html", ".htm")
_COBOL_COMMENT = re.compile(r"^.{6}\*")


def capture_id(capture: EvidenceCapture) -> str:
    blob = "\x1f".join(
        (
            capture.snapshot_id,
            capture.path,
            str(capture.line_start),
            str(capture.line_end),
            capture.symbol or "",
            capture.excerpt_sha256,
        )
    )
    return "cap_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _is_comment_line(path: str, line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if path.lower().endswith((".cbl", ".cpy", ".cob")) and _COBOL_COMMENT.match(line):
        return True
    lowered = stripped.lower()
    if any(lowered.startswith(marker) for marker in _LINE_COMMENT_MARKERS):
        return True
    return any(stripped.startswith(marker) for marker in _BLOCK_COMMENT_MARKERS)


def _is_docstring_line(line: str) -> bool:
    stripped = line.strip()
    return any(stripped.startswith(marker) for marker in _DOCSTRING_MARKERS)


def detect_content(path: str, excerpt: str) -> CodeContent:
    lowered = path.lower()
    if lowered.endswith(_MARKUP_SUFFIXES):
        return CodeContent.MARKUP
    lines = [line for line in excerpt.splitlines() if line.strip()]
    if not lines:
        return CodeContent.MARKUP
    if lowered.endswith(_CONFIG_SUFFIXES):
        return CodeContent.CONFIG_VALUE
    docstring = sum(1 for line in lines if _is_docstring_line(line))
    comment = sum(1 for line in lines if _is_comment_line(path, line))
    if docstring and docstring + comment == len(lines):
        return CodeContent.DOCSTRING
    if comment == len(lines):
        return CodeContent.COMMENT
    return CodeContent.EXECUTABLE


def source_version_of(
    snapshot: RepositorySnapshot, namespace: str, captured_at: str
) -> SourceVersion:
    return SourceVersion(
        source_id=namespace,
        version_hash=snapshot.digest,
        locator_root=snapshot.root,
        captured_at=captured_at,
    )


def to_knowledge(
    capture: EvidenceCapture,
    snapshot: RepositorySnapshot,
    namespace: str,
    captured_at: str,
) -> tuple[SourceVersion, Evidence]:
    version = source_version_of(snapshot, namespace, captured_at)
    locator = CodeLocator(
        path=capture.path,
        line_start=capture.line_start,
        line_end=capture.line_end,
        symbol=capture.symbol,
        content=detect_content(capture.path, capture.excerpt),
    )
    evidence = make_evidence(
        source_id=namespace,
        version_hash=snapshot.digest,
        locator=locator,
        excerpt=capture.excerpt,
        captured_at=captured_at,
    )
    return version, evidence


@dataclass(frozen=True)
class RegisteredCapture:
    identifier: str
    capture: EvidenceCapture

    def to_dict(self) -> dict[str, Any]:
        return {"capture_id": self.identifier, **self.capture.to_dict()}


class CaptureRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, EvidenceCapture] = {}
        self._by_locator: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[RegisteredCapture]:
        for identifier in sorted(self._by_id):
            yield RegisteredCapture(identifier, self._by_id[identifier])

    def register(self, capture: EvidenceCapture) -> RegisteredCapture:
        identifier = capture_id(capture)
        self._by_id[identifier] = capture
        self._by_locator[capture.locator()] = identifier
        span = f"{capture.path}:{capture.line_start}-{capture.line_end}"
        self._by_locator[span] = identifier
        return RegisteredCapture(identifier, capture)

    def register_payload(self, payload: Mapping[str, Any]) -> RegisteredCapture:
        return self.register(EvidenceCapture.from_dict(payload))

    def get(self, identifier: str) -> EvidenceCapture | None:
        found = self._by_id.get(identifier)
        if found is not None:
            return found
        resolved = self._by_locator.get(identifier)
        return self._by_id.get(resolved) if resolved else None

    def find(
        self, path: str, line_start: int, line_end: int, symbol: str = ""
    ) -> EvidenceCapture | None:
        span = f"{path}:{line_start}-{line_end}"
        if symbol:
            found = self.get(f"{span}#{symbol}")
            if found is not None:
                return found
        return self.get(span)

    def identifiers(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_id))
