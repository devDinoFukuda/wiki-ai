from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Iterable, Mapping, Sequence

from .errors import LocatorInvalid, UnsupportedEvidence
from .identity import evidence_id, excerpt_digest
from .model import Evidence, Locator


class CodeContent(str, Enum):
    EXECUTABLE = "executable"
    CONFIG_VALUE = "config_value"
    COMMENT = "comment"
    DOCSTRING = "docstring"
    MARKUP = "markup"


def _text(data: Mapping[str, Any], name: str, holder: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise LocatorInvalid(f"{holder}.{name} exige texto não vazio, recebido {value!r}")
    return value


def _optional_text(data: Mapping[str, Any], name: str, holder: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise LocatorInvalid(f"{holder}.{name} opcional exige texto não vazio ou None")
    return value


def _positive_int(data: Mapping[str, Any], name: str, holder: str) -> int:
    value = data.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise LocatorInvalid(f"{holder}.{name} exige inteiro >= 1, recebido {value!r}")
    return value


def _seconds(data: Mapping[str, Any], name: str, holder: str) -> float:
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise LocatorInvalid(f"{holder}.{name} exige segundos >= 0, recebido {value!r}")
    return float(value)


@dataclass(frozen=True)
class CodeLocator(Locator):
    kind: ClassVar[str] = "code"
    path: str
    line_start: int
    line_end: int
    symbol: str | None = None
    content: CodeContent = CodeContent.EXECUTABLE

    def __post_init__(self) -> None:
        if not (self.path or "").strip():
            raise LocatorInvalid("CodeLocator.path vazio")
        if self.line_start < 1 or self.line_end < self.line_start:
            raise LocatorInvalid(
                f"intervalo de código inválido: {self.line_start}..{self.line_end}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "symbol": self.symbol,
            "content": self.content.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CodeLocator":
        return cls(
            path=_text(data, "path", cls.kind),
            line_start=_positive_int(data, "line_start", cls.kind),
            line_end=_positive_int(data, "line_end", cls.kind),
            symbol=_optional_text(data, "symbol", cls.kind),
            content=CodeContent(data.get("content", CodeContent.EXECUTABLE.value)),
        )


@dataclass(frozen=True)
class DocumentLocator(Locator):
    kind: ClassVar[str] = "document"
    block_id: str | None = None
    heading_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "heading_path", tuple(self.heading_path))
        if not self.block_id and not self.heading_path:
            raise LocatorInvalid(
                "DocumentLocator exige block_id ou heading_path: página isolada não é estável"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "block_id": self.block_id,
            "heading_path": list(self.heading_path),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DocumentLocator":
        raw = data.get("heading_path") or ()
        if not isinstance(raw, (list, tuple)):
            raise LocatorInvalid("document.heading_path exige lista de títulos")
        return cls(
            block_id=_optional_text(data, "block_id", cls.kind),
            heading_path=tuple(str(part) for part in raw),
        )


@dataclass(frozen=True)
class SpreadsheetLocator(Locator):
    kind: ClassVar[str] = "spreadsheet"
    workbook: str
    worksheet: str
    cell_range: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "workbook": self.workbook,
            "worksheet": self.worksheet,
            "cell_range": self.cell_range,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SpreadsheetLocator":
        return cls(
            workbook=_text(data, "workbook", cls.kind),
            worksheet=_text(data, "worksheet", cls.kind),
            cell_range=_text(data, "cell_range", cls.kind),
        )


@dataclass(frozen=True)
class DiagramLocator(Locator):
    kind: ClassVar[str] = "diagram"
    diagram: str
    page: str
    node: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "diagram": self.diagram,
            "page": self.page,
            "node": self.node,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DiagramLocator":
        return cls(
            diagram=_text(data, "diagram", cls.kind),
            page=_text(data, "page", cls.kind),
            node=_text(data, "node", cls.kind),
        )


@dataclass(frozen=True)
class TranscriptLocator(Locator):
    kind: ClassVar[str] = "transcript"
    speaker: str
    time_start: float
    time_end: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "time_start", float(self.time_start))
        object.__setattr__(self, "time_end", float(self.time_end))
        if self.time_start < 0 or self.time_end < self.time_start:
            raise LocatorInvalid(
                f"intervalo de transcrição inválido: {self.time_start}..{self.time_end}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "speaker": self.speaker,
            "time_start": self.time_start,
            "time_end": self.time_end,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TranscriptLocator":
        return cls(
            speaker=_text(data, "speaker", cls.kind),
            time_start=_seconds(data, "time_start", cls.kind),
            time_end=_seconds(data, "time_end", cls.kind),
        )


LOCATOR_TYPES: dict[str, type[Locator]] = {
    CodeLocator.kind: CodeLocator,
    DocumentLocator.kind: DocumentLocator,
    SpreadsheetLocator.kind: SpreadsheetLocator,
    DiagramLocator.kind: DiagramLocator,
    TranscriptLocator.kind: TranscriptLocator,
}

IMPLEMENTATION_CONTENT: frozenset[CodeContent] = frozenset(
    {CodeContent.EXECUTABLE, CodeContent.CONFIG_VALUE}
)


def locator_from_dict(data: Mapping[str, Any]) -> Locator:
    if not isinstance(data, Mapping):
        raise LocatorInvalid(
            f"localizador deve ser um mapa, recebido {type(data).__name__}"
        )
    kind = data.get("kind")
    locator_type = LOCATOR_TYPES.get(str(kind))
    if locator_type is None:
        raise LocatorInvalid(
            f"localizador de tipo {kind!r} desconhecido; conhecidos: "
            f"{', '.join(sorted(LOCATOR_TYPES))}"
        )
    return locator_type.from_dict(data)


def make_evidence(
    source_id: str,
    version_hash: str,
    locator: Locator,
    excerpt: str,
    captured_at: str,
) -> Evidence:
    payload = locator.to_dict()
    return Evidence(
        id=evidence_id(source_id, version_hash, payload),
        source_id=source_id,
        version_hash=version_hash,
        locator=locator,
        excerpt_hash=excerpt_digest(excerpt),
        captured_at=captured_at,
        excerpt=excerpt,
    )


def supports_implemented(evidence: Evidence) -> bool:
    locator = evidence.locator
    if not isinstance(locator, CodeLocator):
        return False
    return locator.content in IMPLEMENTATION_CONTENT


def assert_supports_implemented(evidences: Iterable[Evidence]) -> None:
    items: Sequence[Evidence] = tuple(evidences)
    if any(supports_implemented(item) for item in items):
        return
    seen = sorted({item.locator.kind for item in items}) or ["(nenhuma)"]
    raise UnsupportedEvidence(
        "estado implemented com confiança supported exige evidência de código executável "
        f"ou valor de configuração; recebido apenas: {', '.join(seen)}"
    )
