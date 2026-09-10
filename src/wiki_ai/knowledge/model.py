from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Sequence

from .errors import InvalidKind, PayloadInvalid
from .identity import (
    canonical_name,
    contextual_key,
    entity_id,
    relation_id,
    source_version_id,
)

KIND_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def validate_kind(kind: str) -> str:
    value = (kind or "").strip()
    if not KIND_PATTERN.fullmatch(value):
        raise InvalidKind(
            f"kind {kind!r} fora de snake_case; esperado {KIND_PATTERN.pattern}"
        )
    return value


def _required_text(value: str, label: str) -> str:
    text = (value or "").strip()
    if not text:
        raise PayloadInvalid(f"{label} vazio")
    return text


class KnowledgeState(str, Enum):
    IMPLEMENTED = "implemented"
    DECLARED = "declared"
    PROPOSED = "proposed"
    HISTORICAL = "historical"


class Confidence(str, Enum):
    SUPPORTED = "supported"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"
    CONTRADICTED = "contradicted"


@dataclass(frozen=True)
class EntityId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _required_text(self.value, "EntityId"))

    def __str__(self) -> str:
        return self.value

    @classmethod
    def derive(cls, kind: str, stable_key: str) -> "EntityId":
        return cls(entity_id(validate_kind(kind), stable_key))


class Locator(ABC):
    kind: ClassVar[str]

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Locator":
        raise NotImplementedError


@dataclass(frozen=True)
class Entity:
    id: EntityId
    kind: str
    name: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    state: KnowledgeState = KnowledgeState.DECLARED
    confidence: Confidence = Confidence.UNRESOLVED
    source_versions: tuple[str, ...] = ()
    owner_id: EntityId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", validate_kind(self.kind))
        object.__setattr__(self, "name", _required_text(self.name, "Entity.name"))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))
        object.__setattr__(self, "source_versions", tuple(self.source_versions))

    @property
    def canonical_name(self) -> str:
        return canonical_name(self.name)

    @classmethod
    def create(
        cls,
        kind: str,
        name: str,
        stable_key: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        state: KnowledgeState = KnowledgeState.DECLARED,
        confidence: Confidence = Confidence.UNRESOLVED,
        source_versions: Sequence[str] = (),
        owner_id: EntityId | None = None,
    ) -> "Entity":
        return cls(
            id=EntityId.derive(kind, stable_key or _required_text(name, "Entity.name")),
            kind=kind,
            name=name,
            attributes=dict(attributes or {}),
            state=state,
            confidence=confidence,
            source_versions=tuple(source_versions),
            owner_id=owner_id,
        )

    @classmethod
    def owned(
        cls,
        namespace: str,
        kind: str,
        name: str,
        owner: "Entity | None" = None,
        explicit_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        state: KnowledgeState = KnowledgeState.DECLARED,
        confidence: Confidence = Confidence.UNRESOLVED,
        source_versions: Sequence[str] = (),
    ) -> "Entity":
        return cls.create(
            kind=kind,
            name=name,
            stable_key=contextual_key(
                namespace,
                validate_kind(kind),
                owner.name if owner is not None else None,
                name,
                explicit_id,
            ),
            attributes=attributes,
            state=state,
            confidence=confidence,
            source_versions=source_versions,
            owner_id=owner.id if owner is not None else None,
        )

    def with_confidence(self, confidence: Confidence) -> "Entity":
        return replace(self, confidence=confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id.value,
            "kind": self.kind,
            "name": self.name,
            "attributes": dict(self.attributes),
            "state": self.state.value,
            "confidence": self.confidence.value,
            "source_versions": list(self.source_versions),
            "owner_id": self.owner_id.value if self.owner_id is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Entity":
        owner = data.get("owner_id")
        return cls(
            id=EntityId(str(data["id"])),
            kind=str(data["kind"]),
            name=str(data["name"]),
            attributes=dict(data.get("attributes") or {}),
            state=KnowledgeState(str(data.get("state", KnowledgeState.DECLARED.value))),
            confidence=Confidence(
                str(data.get("confidence", Confidence.UNRESOLVED.value))
            ),
            source_versions=tuple(str(key) for key in data.get("source_versions") or ()),
            owner_id=EntityId(str(owner)) if owner else None,
        )


@dataclass(frozen=True)
class Relation:
    id: str
    kind: str
    source_id: EntityId
    target_id: EntityId
    attributes: Mapping[str, Any] = field(default_factory=dict)
    confidence: Confidence = Confidence.UNRESOLVED

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", validate_kind(self.kind))
        object.__setattr__(self, "id", _required_text(self.id, "Relation.id"))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    @classmethod
    def create(
        cls,
        kind: str,
        source_id: EntityId,
        target_id: EntityId,
        attributes: Mapping[str, Any] | None = None,
        confidence: Confidence = Confidence.UNRESOLVED,
    ) -> "Relation":
        validated = validate_kind(kind)
        return cls(
            id=relation_id(validated, source_id.value, target_id.value),
            kind=validated,
            source_id=source_id,
            target_id=target_id,
            attributes=dict(attributes or {}),
            confidence=confidence,
        )

    def with_confidence(self, confidence: Confidence) -> "Relation":
        return replace(self, confidence=confidence)


@dataclass(frozen=True)
class SourceVersion:
    source_id: str
    version_hash: str
    locator_root: str
    captured_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_id", _required_text(self.source_id, "SourceVersion.source_id")
        )
        object.__setattr__(
            self,
            "version_hash",
            _required_text(self.version_hash, "SourceVersion.version_hash"),
        )
        object.__setattr__(
            self,
            "locator_root",
            _required_text(self.locator_root, "SourceVersion.locator_root"),
        )

    @property
    def key(self) -> str:
        return source_version_id(self.source_id, self.version_hash)


@dataclass(frozen=True)
class Evidence:
    id: str
    source_id: str
    version_hash: str
    locator: Locator
    excerpt_hash: str
    captured_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_text(self.id, "Evidence.id"))
        object.__setattr__(
            self, "source_id", _required_text(self.source_id, "Evidence.source_id")
        )
        object.__setattr__(
            self,
            "version_hash",
            _required_text(self.version_hash, "Evidence.version_hash"),
        )
        if not isinstance(self.locator, Locator):
            raise PayloadInvalid(
                f"Evidence.locator deve ser Locator, recebido {type(self.locator).__name__}"
            )

    @property
    def source_version_key(self) -> str:
        return source_version_id(self.source_id, self.version_hash)


@dataclass(frozen=True)
class Revision:
    id: str
    parent_id: str | None
    author: str
    created_at: str
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_text(self.id, "Revision.id"))
        object.__setattr__(
            self, "author", _required_text(self.author, "Revision.author")
        )
