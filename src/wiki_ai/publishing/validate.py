from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from wiki_ai.publishing.manifest import (
    MANIFEST_FILENAME,
    Artifact,
    ArtifactKind,
    Manifest,
    hash_file,
)

__all__ = [
    "ViolationCode",
    "PackageViolation",
    "DocxValidator",
    "package_files",
    "validate_package",
]

DocxValidator = Callable[[Path], list[str]]


class ViolationCode(str, enum.Enum):
    MISSING_REQUIRED_ARTIFACT = "missing_required_artifact"
    MISSING_OPTIONAL_ARTIFACT = "missing_optional_artifact"
    HASH_MISMATCH = "hash_mismatch"
    SIZE_MISMATCH = "size_mismatch"
    UNLISTED_FILE = "unlisted_file"
    MISSING_REQUIRED_DOCX = "missing_required_docx"
    DIAGRAM_IMAGE_WITHOUT_TEXT = "diagram_image_without_text"
    DOCX_STRUCTURE = "docx_structure"


@dataclass(frozen=True)
class PackageViolation:
    code: ViolationCode
    relative_path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code.value,
            "relative_path": self.relative_path,
            "message": self.message,
        }


def package_files(package_dir: Path | str) -> tuple[str, ...]:
    root = Path(package_dir)
    if not root.is_dir():
        return ()
    found = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    ]
    return tuple(sorted(found))


def _artifact_violations(
    package_dir: Path, artifact: Artifact
) -> list[PackageViolation]:
    path = package_dir / artifact.relative_path
    if not path.is_file():
        code = (
            ViolationCode.MISSING_REQUIRED_ARTIFACT
            if artifact.required
            else ViolationCode.MISSING_OPTIONAL_ARTIFACT
        )
        return [
            PackageViolation(
                code=code,
                relative_path=artifact.relative_path,
                message=f"artifact listed in the manifest is absent from {package_dir}",
            )
        ]
    found: list[PackageViolation] = []
    size = path.stat().st_size
    if size != artifact.size:
        found.append(
            PackageViolation(
                code=ViolationCode.SIZE_MISMATCH,
                relative_path=artifact.relative_path,
                message=f"file holds {size} bytes, manifest declares {artifact.size}",
            )
        )
    digest = hash_file(path)
    if digest != artifact.sha256:
        found.append(
            PackageViolation(
                code=ViolationCode.HASH_MISMATCH,
                relative_path=artifact.relative_path,
                message=f"file hashes to {digest}, manifest declares {artifact.sha256}",
            )
        )
    return found


def _unlisted_violations(
    package_dir: Path, listed: Iterable[str]
) -> list[PackageViolation]:
    allowed = set(listed) | {MANIFEST_FILENAME}
    return [
        PackageViolation(
            code=ViolationCode.UNLISTED_FILE,
            relative_path=relative,
            message="file present in the package but absent from the manifest",
        )
        for relative in package_files(package_dir)
        if relative not in allowed
    ]


def _docx_violations(manifest: Manifest) -> list[PackageViolation]:
    required = [a for a in manifest.of_kind(ArtifactKind.DOCX) if a.required]
    if required:
        return []
    return [
        PackageViolation(
            code=ViolationCode.MISSING_REQUIRED_DOCX,
            relative_path=MANIFEST_FILENAME,
            message=(
                f"publication {manifest.publication_id} declares no required DOCX "
                "artifact; DOCX is a first-class output of the delivery package"
            ),
        )
    ]


def _diagram_violations(manifest: Manifest) -> list[PackageViolation]:
    textual = {a.stem for a in manifest.of_kind(ArtifactKind.DIAGRAM_TEXT)}
    return [
        PackageViolation(
            code=ViolationCode.DIAGRAM_IMAGE_WITHOUT_TEXT,
            relative_path=image.relative_path,
            message=(
                f"diagram image has no textual counterpart named {image.stem!r} "
                "among the diagram_text artifacts"
            ),
        )
        for image in manifest.of_kind(ArtifactKind.DIAGRAM_IMAGE)
        if image.stem not in textual
    ]


def _injected_docx_violations(
    package_dir: Path, manifest: Manifest, docx_validator: DocxValidator
) -> list[PackageViolation]:
    found: list[PackageViolation] = []
    for artifact in manifest.of_kind(ArtifactKind.DOCX):
        path = package_dir / artifact.relative_path
        if not path.is_file():
            continue
        for problem in docx_validator(path):
            found.append(
                PackageViolation(
                    code=ViolationCode.DOCX_STRUCTURE,
                    relative_path=artifact.relative_path,
                    message=str(problem),
                )
            )
    return found


def validate_package(
    package_dir: Path | str,
    manifest: Manifest,
    *,
    required_docx: bool = True,
    docx_validator: DocxValidator | None = None,
) -> list[PackageViolation]:
    root = Path(package_dir)
    found: list[PackageViolation] = []
    for artifact in manifest.artifacts:
        found.extend(_artifact_violations(root, artifact))
    found.extend(_unlisted_violations(root, manifest.relative_paths))
    if required_docx:
        found.extend(_docx_violations(manifest))
    found.extend(_diagram_violations(manifest))
    if docx_validator is not None:
        found.extend(_injected_docx_violations(root, manifest, docx_validator))
    return sorted(found, key=lambda v: (v.relative_path, v.code.value, v.message))
