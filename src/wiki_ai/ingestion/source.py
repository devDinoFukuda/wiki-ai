from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from wiki_ai.ingestion.outcome import StructuralFault

__all__ = [
    "SourceKind",
    "BlockKind",
    "DiagnosticLevel",
    "LocatorInvalid",
    "SourcePayloadInvalid",
    "MetadataNotInert",
    "METADATA_WHITELIST",
    "SUSPICIOUS_METADATA_KEYS",
    "LOCATOR_INCOMPLETE",
    "MAX_METADATA_VALUE",
    "MAX_METADATA_ITEMS",
    "assert_inert",
    "sanitize_metadata",
    "policy_from_metadata",
    "Source",
    "Block",
    "Diagnostic",
    "SourceDocument",
    "REQUIRED_LOCATOR_FIELDS",
    "content_digest",
    "source_hash",
    "block_hash",
    "make_source",
    "make_block",
    "required_locator_fields",
    "locator_violations",
    "validate_locator",
    "locator_diagnostics",
]


class SourceKind(str, enum.Enum):
    DOCX = "docx"
    XLSX = "xlsx"
    DRAWIO = "drawio"
    PDF = "pdf"
    TRANSCRIPT = "transcript"
    MARKDOWN = "markdown"
    HTML = "html"
    JSON = "json"
    XML = "xml"
    CODEBASE = "codebase"


class BlockKind(str, enum.Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    CELL = "cell"
    UTTERANCE = "utterance"
    NODE = "node"
    EDGE = "edge"
    CODE = "code"
    IMAGE = "image"
    OTHER = "other"


class DiagnosticLevel(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


REQUIRED_LOCATOR_FIELDS: Mapping[SourceKind, tuple[str, ...]] = MappingProxyType(
    {
        SourceKind.DOCX: ("section", "block"),
        SourceKind.XLSX: ("workbook", "worksheet", "range"),
        SourceKind.DRAWIO: ("page", "node"),
        SourceKind.PDF: ("page", "block"),
        SourceKind.TRANSCRIPT: ("speaker", "time"),
        SourceKind.MARKDOWN: ("section", "block"),
        SourceKind.HTML: ("path", "block"),
        SourceKind.JSON: ("pointer",),
        SourceKind.XML: ("xpath",),
        SourceKind.CODEBASE: ("path", "start_line", "end_line"),
    }
)


class LocatorInvalid(ValueError):
    def __init__(self, kind: SourceKind, missing: Sequence[str]) -> None:
        self.kind = kind
        self.missing: tuple[str, ...] = tuple(missing)
        super().__init__(
            f"locator for {kind.value} is missing required field(s): {', '.join(self.missing)}"
        )


class SourcePayloadInvalid(ValueError):
    def __init__(self, target: str, reason: str) -> None:
        self.target = target
        self.reason = reason
        super().__init__(f"cannot rebuild {target} from payload: {reason}")


class MetadataNotInert(ValueError):
    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"metadata[{key!r}] is not inert data: {reason}")


METADATA_WHITELIST: tuple[str, ...] = (
    "initiative_id",
    "phase",
    "participants",
    "date",
    "title",
    "source_type",
)

SUSPICIOUS_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "allow",
        "allowed_tools",
        "approve",
        "approved",
        "approved_by",
        "budget",
        "command",
        "commands",
        "knowledge_state",
        "exec",
        "execute",
        "instruction",
        "instructions",
        "nature",
        "permissions",
        "policies",
        "policy",
        "prompt",
        "run",
        "shell",
        "supported",
        "system",
        "system_prompt",
        "tool",
        "tools",
        "trust",
    }
)

LOCATOR_INCOMPLETE = StructuralFault.LOCATOR_INCOMPLETE.value

MAX_METADATA_VALUE = 4000
MAX_METADATA_ITEMS = 200


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def content_digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _frozen_map(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType({str(k): v for k, v in dict(value or {}).items()})


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_moment(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return _as_utc(raw)
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _as_utc(datetime.fromisoformat(text))


def source_hash(
    kind: SourceKind,
    uri: str,
    version_hash: str,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    return content_digest(
        {
            "kind": kind.value,
            "uri": uri,
            "version_hash": version_hash,
            "metadata": dict(metadata or {}),
        }
    )


def block_hash(
    source_id: str,
    order: int,
    kind: BlockKind,
    text: str,
    locator: Mapping[str, Any] | None = None,
    parent_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> str:
    return content_digest(
        {
            "source_id": source_id,
            "order": order,
            "kind": kind.value,
            "text": text,
            "locator": dict(locator or {}),
            "parent_id": parent_id,
            "attributes": dict(attributes or {}),
        }
    )


@dataclass(frozen=True)
class Source:
    id: str
    kind: SourceKind
    uri: str
    version_hash: str
    captured_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_map(self.metadata))
        object.__setattr__(self, "captured_at", _as_utc(self.captured_at))

    @property
    def content_hash(self) -> str:
        return source_hash(self.kind, self.uri, self.version_hash, self.metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "uri": self.uri,
            "version_hash": self.version_hash,
            "captured_at": self.captured_at.isoformat(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Source":
        try:
            return cls(
                id=str(payload["id"]),
                kind=SourceKind(payload["kind"]),
                uri=str(payload["uri"]),
                version_hash=str(payload["version_hash"]),
                captured_at=_parse_moment(payload["captured_at"]),
                metadata=payload.get("metadata") or {},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SourcePayloadInvalid("Source", str(exc)) from exc


@dataclass(frozen=True)
class Block:
    id: str
    source_id: str
    order: int
    kind: BlockKind
    text: str
    locator: Mapping[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "locator", _frozen_map(self.locator))
        object.__setattr__(self, "attributes", _frozen_map(self.attributes))

    @property
    def content_hash(self) -> str:
        return block_hash(
            self.source_id,
            self.order,
            self.kind,
            self.text,
            self.locator,
            self.parent_id,
            self.attributes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "order": self.order,
            "kind": self.kind.value,
            "text": self.text,
            "locator": dict(self.locator),
            "parent_id": self.parent_id,
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Block":
        try:
            parent = payload.get("parent_id")
            return cls(
                id=str(payload["id"]),
                source_id=str(payload["source_id"]),
                order=int(payload["order"]),
                kind=BlockKind(payload["kind"]),
                text=str(payload["text"]),
                locator=payload.get("locator") or {},
                parent_id=None if parent is None else str(parent),
                attributes=payload.get("attributes") or {},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SourcePayloadInvalid("Block", str(exc)) from exc


@dataclass(frozen=True)
class Diagnostic:
    level: DiagnosticLevel
    code: str
    message: str
    locator: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "locator", _frozen_map(self.locator))

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "code": self.code,
            "message": self.message,
            "locator": dict(self.locator),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Diagnostic":
        try:
            return cls(
                level=DiagnosticLevel(payload["level"]),
                code=str(payload["code"]),
                message=str(payload["message"]),
                locator=payload.get("locator") or {},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SourcePayloadInvalid("Diagnostic", str(exc)) from exc


@dataclass(frozen=True)
class SourceDocument:
    source: Source
    blocks: tuple[Block, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.level is DiagnosticLevel.ERROR)

    @property
    def complete(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "blocks": [b.to_dict() for b in self.blocks],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceDocument":
        try:
            source = Source.from_dict(payload["source"])
            blocks = tuple(Block.from_dict(b) for b in payload.get("blocks") or ())
            diagnostics = tuple(
                Diagnostic.from_dict(d) for d in payload.get("diagnostics") or ()
            )
        except (KeyError, TypeError) as exc:
            raise SourcePayloadInvalid("SourceDocument", str(exc)) from exc
        return cls(source=source, blocks=blocks, diagnostics=diagnostics)


def make_source(
    kind: SourceKind,
    uri: str,
    version_hash: str,
    captured_at: datetime,
    metadata: Mapping[str, Any] | None = None,
) -> Source:
    digest = source_hash(kind, uri, version_hash, metadata)
    return Source(
        id=f"src-{digest[:32]}",
        kind=kind,
        uri=uri,
        version_hash=version_hash,
        captured_at=captured_at,
        metadata=metadata or {},
    )


def make_block(
    source_id: str,
    order: int,
    kind: BlockKind,
    text: str,
    locator: Mapping[str, Any] | None = None,
    parent_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Block:
    digest = block_hash(source_id, order, kind, text, locator, parent_id, attributes)
    return Block(
        id=f"blk-{order:05d}-{digest[:16]}",
        source_id=source_id,
        order=order,
        kind=kind,
        text=text,
        locator=locator or {},
        parent_id=parent_id,
        attributes=attributes or {},
    )


def required_locator_fields(kind: SourceKind) -> tuple[str, ...]:
    return REQUIRED_LOCATOR_FIELDS.get(kind, ())


def locator_violations(kind: SourceKind, locator: Mapping[str, Any]) -> tuple[str, ...]:
    present = dict(locator or {})
    missing: list[str] = []
    for name in required_locator_fields(kind):
        value = present.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return tuple(missing)


def validate_locator(kind: SourceKind, locator: Mapping[str, Any]) -> dict[str, Any]:
    missing = locator_violations(kind, locator)
    if missing:
        raise LocatorInvalid(kind, missing)
    return {str(k): v for k, v in dict(locator or {}).items()}


def locator_diagnostics(
    kind: SourceKind, blocks: Iterable[Block]
) -> tuple[Diagnostic, ...]:
    found: list[Diagnostic] = []
    for block in blocks:
        missing = locator_violations(kind, block.locator)
        if not missing:
            continue
        found.append(
            Diagnostic(
                level=DiagnosticLevel.ERROR,
                code=LOCATOR_INCOMPLETE,
                message=(
                    f"block {block.id} has no resolvable locator for {kind.value}: "
                    f"missing {', '.join(missing)}"
                ),
                locator=block.locator,
            )
        )
    return tuple(found)


def _inert_scalar(value: Any) -> Any:
    if isinstance(value, (bool, int, float)):
        return value
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:MAX_METADATA_VALUE]
    return _canonical(value)[:MAX_METADATA_VALUE]


def _inert(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_inert_scalar(item) for item in list(value)[:MAX_METADATA_ITEMS]]
    if isinstance(value, Mapping):
        return _canonical(dict(value))[:MAX_METADATA_VALUE]
    return _inert_scalar(value)


def assert_inert(mapping: Mapping[str, Any]) -> None:
    for key, value in mapping.items():
        if callable(value):
            raise MetadataNotInert(key, "callable values never reach the pipeline")
        if isinstance(value, list):
            for item in value:
                if callable(item) or isinstance(item, (list, dict, tuple, set)):
                    raise MetadataNotInert(key, "list items must be scalars")
        elif not isinstance(value, (str, int, float, bool)):
            raise MetadataNotInert(
                key, f"{type(value).__name__} is neither a scalar nor a list of scalars"
            )


def _normalize_metadata_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


def sanitize_metadata(
    raw: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], tuple[Diagnostic, ...]]:
    metadata: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    found: list[Diagnostic] = []
    for key, value in dict(raw or {}).items():
        normalized = _normalize_metadata_key(key)
        if normalized in METADATA_WHITELIST:
            metadata[normalized] = _inert(value)
            continue
        extra[str(key)[:200]] = _inert(value)
        if normalized in SUSPICIOUS_METADATA_KEYS:
            found.append(
                Diagnostic(
                    level=DiagnosticLevel.WARNING,
                    code="metadata.instruction_attempt",
                    message=(
                        f"metadata key {key!r} looks like an instruction or a policy; "
                        "kept as inert data and never read by the pipeline"
                    ),
                    locator={"key": str(key)[:200]},
                )
            )
        else:
            found.append(
                Diagnostic(
                    level=DiagnosticLevel.INFO,
                    code="metadata.not_whitelisted",
                    message=(
                        f"metadata key {key!r} is outside the whitelist "
                        f"({', '.join(METADATA_WHITELIST)}); kept as inert data"
                    ),
                    locator={"key": str(key)[:200]},
                )
            )
    participants = metadata.get("participants")
    if isinstance(participants, (str, int, float, bool)):
        metadata["participants"] = [
            part.strip() for part in str(participants).split(",") if part.strip()
        ]
    assert_inert(metadata)
    assert_inert(extra)
    return metadata, extra, tuple(found)


def policy_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {}
