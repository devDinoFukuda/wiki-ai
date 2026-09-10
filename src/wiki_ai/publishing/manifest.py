from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

__all__ = [
    "MANIFEST_FILENAME",
    "ArtifactKind",
    "ManifestInvalid",
    "Artifact",
    "Manifest",
    "canonical_json",
    "canonical_hash",
    "hash_bytes",
    "hash_file",
    "normalize_relative_path",
    "artifact_for_bytes",
    "artifact_for_file",
]

MANIFEST_FILENAME = "manifest.json"


class ArtifactKind(str, enum.Enum):
    DOCX = "docx"
    MARKDOWN = "markdown"
    DIAGRAM_TEXT = "diagram_text"
    DIAGRAM_IMAGE = "diagram_image"
    MANIFEST = "manifest"
    OTHER = "other"


class ManifestInvalid(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"invalid manifest: {reason}")


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canonical_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_relative_path(raw: str) -> str:
    text = str(raw).replace("\\", "/").strip()
    if not text:
        raise ManifestInvalid("artifact relative_path is empty")
    candidate = PurePosixPath(text)
    if candidate.is_absolute() or text.startswith("/"):
        raise ManifestInvalid(f"artifact relative_path must be relative: {raw!r}")
    if ".." in candidate.parts:
        raise ManifestInvalid(f"artifact relative_path escapes the package: {raw!r}")
    return candidate.as_posix()


@dataclass(frozen=True)
class Artifact:
    relative_path: str
    sha256: str
    size: int
    kind: ArtifactKind = ArtifactKind.OTHER
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "relative_path", normalize_relative_path(self.relative_path)
        )
        if self.size < 0:
            raise ManifestInvalid(f"artifact {self.relative_path} has negative size")

    @property
    def stem(self) -> str:
        return PurePosixPath(self.relative_path).stem

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size": self.size,
            "kind": self.kind.value,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Artifact":
        try:
            return cls(
                relative_path=str(payload["relative_path"]),
                sha256=str(payload["sha256"]),
                size=int(payload["size"]),
                kind=ArtifactKind(payload.get("kind", ArtifactKind.OTHER.value)),
                required=bool(payload.get("required", True)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestInvalid(f"artifact entry unusable: {exc}") from exc


@dataclass(frozen=True)
class Manifest:
    publication_id: str
    revision: str
    created_at: datetime
    source_snapshot_hash: str
    artifacts: tuple[Artifact, ...] = ()
    previous_publication_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        if not str(self.publication_id).strip():
            raise ManifestInvalid("publication_id is empty")
        object.__setattr__(self, "created_at", _as_utc(self.created_at))
        seen: set[str] = set()
        for artifact in self.artifacts:
            if artifact.relative_path in seen:
                raise ManifestInvalid(
                    f"artifact listed twice: {artifact.relative_path}"
                )
            seen.add(artifact.relative_path)

    def artifact(self, relative_path: str) -> Artifact | None:
        wanted = normalize_relative_path(relative_path)
        for artifact in self.artifacts:
            if artifact.relative_path == wanted:
                return artifact
        return None

    def of_kind(self, kind: ArtifactKind) -> tuple[Artifact, ...]:
        return tuple(a for a in self.artifacts if a.kind is kind)

    @property
    def relative_paths(self) -> tuple[str, ...]:
        return tuple(a.relative_path for a in self.artifacts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "publication_id": self.publication_id,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "source_snapshot_hash": self.source_snapshot_hash,
            "previous_publication_id": self.previous_publication_id,
            "artifacts": [a.to_dict() for a in self.artifacts],
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def manifest_hash(self) -> str:
        return canonical_hash(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Manifest":
        try:
            previous = payload.get("previous_publication_id")
            return cls(
                publication_id=str(payload["publication_id"]),
                revision=str(payload["revision"]),
                created_at=_parse_moment(payload["created_at"]),
                source_snapshot_hash=str(payload["source_snapshot_hash"]),
                artifacts=tuple(
                    Artifact.from_dict(a) for a in payload.get("artifacts") or ()
                ),
                previous_publication_id=None if previous is None else str(previous),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestInvalid(str(exc)) from exc

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestInvalid(f"manifest is not valid JSON: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ManifestInvalid("manifest payload is not an object")
        return cls.from_dict(payload)


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


def artifact_for_bytes(
    relative_path: str,
    data: bytes,
    kind: ArtifactKind = ArtifactKind.OTHER,
    required: bool = True,
) -> Artifact:
    return Artifact(
        relative_path=relative_path,
        sha256=hash_bytes(data),
        size=len(data),
        kind=kind,
        required=required,
    )


def artifact_for_file(
    root: Path,
    relative_path: str,
    kind: ArtifactKind = ArtifactKind.OTHER,
    required: bool = True,
) -> Artifact:
    normalized = normalize_relative_path(relative_path)
    full = Path(root) / normalized
    return Artifact(
        relative_path=normalized,
        sha256=hash_file(full),
        size=full.stat().st_size,
        kind=kind,
        required=required,
    )
