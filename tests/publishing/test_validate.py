from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from wiki_ai.publishing.manifest import (
    Artifact,
    ArtifactKind,
    Manifest,
    artifact_for_bytes,
    hash_bytes,
)
from wiki_ai.publishing.release import stage
from wiki_ai.publishing.validate import (
    PackageViolation,
    ViolationCode,
    package_files,
    validate_package,
)

CREATED = datetime(2026, 9, 10, 8, 30, 0, tzinfo=timezone.utc)


def build(files: dict[str, bytes], kinds: dict[str, ArtifactKind]) -> Manifest:
    return Manifest(
        publication_id="pub-1",
        revision="r1",
        created_at=CREATED,
        source_snapshot_hash="s" * 64,
        artifacts=tuple(
            artifact_for_bytes(path, data, kinds.get(path, ArtifactKind.OTHER))
            for path, data in files.items()
        ),
    )


def staged_package(tmp_path: Path, files: dict[str, bytes], kinds) -> tuple[Path, Manifest]:
    manifest = build(files, kinds)
    return stage(tmp_path, manifest, files).directory, manifest


def codes(found: list[PackageViolation]) -> list[ViolationCode]:
    return [v.code for v in found]


def test_clean_package_has_no_violations(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path,
        {"wiki.docx": b"docx", "wiki.md": b"# md"},
        {"wiki.docx": ArtifactKind.DOCX, "wiki.md": ArtifactKind.MARKDOWN},
    )
    assert validate_package(directory, manifest) == []


def test_manifest_file_itself_is_never_an_unlisted_file(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path, {"wiki.docx": b"docx"}, {"wiki.docx": ArtifactKind.DOCX}
    )
    assert "manifest.json" in package_files(directory)
    assert validate_package(directory, manifest) == []


def test_missing_required_artifact_is_reported(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path, {"wiki.docx": b"docx"}, {"wiki.docx": ArtifactKind.DOCX}
    )
    (directory / "wiki.docx").unlink()

    found = validate_package(directory, manifest)
    assert codes(found) == [ViolationCode.MISSING_REQUIRED_ARTIFACT]
    assert found[0].relative_path == "wiki.docx"


def test_missing_optional_artifact_is_reported_separately(tmp_path: Path) -> None:
    manifest = Manifest(
        publication_id="pub-1",
        revision="r1",
        created_at=CREATED,
        source_snapshot_hash="s" * 64,
        artifacts=(
            artifact_for_bytes("wiki.docx", b"docx", ArtifactKind.DOCX),
            Artifact("notes.md", hash_bytes(b"absent"), 6, ArtifactKind.MARKDOWN, False),
        ),
    )
    directory = stage(tmp_path, manifest, {"wiki.docx": b"docx"}).directory

    found = validate_package(directory, manifest)
    assert codes(found) == [ViolationCode.MISSING_OPTIONAL_ARTIFACT]


def test_tampered_artifact_reports_hash_and_size(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path, {"wiki.docx": b"docx"}, {"wiki.docx": ArtifactKind.DOCX}
    )
    (directory / "wiki.docx").write_bytes(b"tampered")

    found = validate_package(directory, manifest)
    assert set(codes(found)) == {ViolationCode.HASH_MISMATCH, ViolationCode.SIZE_MISMATCH}


def test_extra_file_not_listed_in_the_manifest_is_reported(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path, {"wiki.docx": b"docx"}, {"wiki.docx": ArtifactKind.DOCX}
    )
    (directory / "sneaky").mkdir()
    (directory / "sneaky" / "extra.md").write_bytes(b"unlisted")

    found = validate_package(directory, manifest)
    assert codes(found) == [ViolationCode.UNLISTED_FILE]
    assert found[0].relative_path == "sneaky/extra.md"


def test_package_without_required_docx_is_blocked_by_default(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path, {"wiki.md": b"# md"}, {"wiki.md": ArtifactKind.MARKDOWN}
    )
    found = validate_package(directory, manifest)
    assert codes(found) == [ViolationCode.MISSING_REQUIRED_DOCX]


def test_docx_requirement_is_parameterized(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path, {"wiki.md": b"# md"}, {"wiki.md": ArtifactKind.MARKDOWN}
    )
    assert validate_package(directory, manifest, required_docx=False) == []


def test_optional_docx_does_not_satisfy_the_docx_requirement(tmp_path: Path) -> None:
    manifest = Manifest(
        publication_id="pub-1",
        revision="r1",
        created_at=CREATED,
        source_snapshot_hash="s" * 64,
        artifacts=(
            Artifact("wiki.docx", hash_bytes(b"docx"), 4, ArtifactKind.DOCX, False),
        ),
    )
    directory = stage(tmp_path, manifest, {"wiki.docx": b"docx"}).directory
    assert codes(validate_package(directory, manifest)) == [
        ViolationCode.MISSING_REQUIRED_DOCX
    ]


def test_diagram_image_without_textual_counterpart(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path,
        {"wiki.docx": b"docx", "diagrams/flow.png": b"png"},
        {
            "wiki.docx": ArtifactKind.DOCX,
            "diagrams/flow.png": ArtifactKind.DIAGRAM_IMAGE,
        },
    )
    found = validate_package(directory, manifest)
    assert codes(found) == [ViolationCode.DIAGRAM_IMAGE_WITHOUT_TEXT]
    assert found[0].relative_path == "diagrams/flow.png"


def test_diagram_image_with_matching_text_stem_is_accepted(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path,
        {
            "wiki.docx": b"docx",
            "diagrams/flow.png": b"png",
            "diagrams/flow.mmd": b"graph TD;",
        },
        {
            "wiki.docx": ArtifactKind.DOCX,
            "diagrams/flow.png": ArtifactKind.DIAGRAM_IMAGE,
            "diagrams/flow.mmd": ArtifactKind.DIAGRAM_TEXT,
        },
    )
    assert validate_package(directory, manifest) == []


def test_injected_docx_validator_contributes_structural_findings(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path, {"wiki.docx": b"docx"}, {"wiki.docx": ArtifactKind.DOCX}
    )
    seen: list[Path] = []

    def validator(path: Path) -> list[str]:
        seen.append(path)
        return ["no native Heading1 style"]

    found = validate_package(directory, manifest, docx_validator=validator)
    assert seen == [directory / "wiki.docx"]
    assert codes(found) == [ViolationCode.DOCX_STRUCTURE]
    assert found[0].message == "no native Heading1 style"


def test_docx_validator_is_not_called_for_an_absent_file(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path, {"wiki.docx": b"docx"}, {"wiki.docx": ArtifactKind.DOCX}
    )
    (directory / "wiki.docx").unlink()

    def validator(path: Path) -> list[str]:
        raise AssertionError("validator must never open a file that is not there")

    found = validate_package(directory, manifest, docx_validator=validator)
    assert codes(found) == [ViolationCode.MISSING_REQUIRED_ARTIFACT]


def test_violations_are_deterministically_ordered(tmp_path: Path) -> None:
    directory, manifest = staged_package(
        tmp_path,
        {"b.md": b"b", "a.docx": b"a"},
        {"a.docx": ArtifactKind.DOCX, "b.md": ArtifactKind.MARKDOWN},
    )
    (directory / "a.docx").write_bytes(b"tampered")
    (directory / "b.md").unlink()
    (directory / "z-extra.txt").write_bytes(b"x")

    found = validate_package(directory, manifest)
    assert found == sorted(found, key=lambda v: (v.relative_path, v.code.value, v.message))
    assert [v.relative_path for v in found] == ["a.docx", "a.docx", "b.md", "z-extra.txt"]


def test_package_files_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert package_files(tmp_path / "nope") == ()


def test_violation_serializes_to_plain_data() -> None:
    violation = PackageViolation(ViolationCode.UNLISTED_FILE, "x.md", "why")
    assert violation.to_dict() == {
        "code": "unlisted_file",
        "relative_path": "x.md",
        "message": "why",
    }


def test_validating_a_missing_package_directory_reports_every_artifact(
    tmp_path: Path,
) -> None:
    manifest = build({"wiki.docx": b"docx"}, {"wiki.docx": ArtifactKind.DOCX})
    found = validate_package(tmp_path / "absent", manifest)
    assert codes(found) == [ViolationCode.MISSING_REQUIRED_ARTIFACT]


@pytest.mark.parametrize("required_docx", [True, False])
def test_hash_check_is_independent_of_the_docx_policy(
    tmp_path: Path, required_docx: bool
) -> None:
    directory, manifest = staged_package(
        tmp_path, {"wiki.docx": b"docx"}, {"wiki.docx": ArtifactKind.DOCX}
    )
    (directory / "wiki.docx").write_bytes(b"docy")
    found = validate_package(directory, manifest, required_docx=required_docx)
    assert ViolationCode.HASH_MISMATCH in codes(found)
