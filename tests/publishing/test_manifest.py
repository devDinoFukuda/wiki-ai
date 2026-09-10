from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wiki_ai.publishing.manifest import (
    ArtifactKind,
    Manifest,
    ManifestInvalid,
    artifact_for_bytes,
    artifact_for_file,
    canonical_json,
    hash_bytes,
    hash_file,
)

CREATED = datetime(2026, 9, 10, 8, 30, 0, tzinfo=timezone.utc)


def build_manifest(**overrides: object) -> Manifest:
    payload: dict[str, object] = {
        "publication_id": "pub-2026-09-10",
        "revision": "r7",
        "created_at": CREATED,
        "source_snapshot_hash": "c" * 64,
        "artifacts": (
            artifact_for_bytes("wiki.docx", b"docx-bytes", ArtifactKind.DOCX),
            artifact_for_bytes("wiki.md", b"# wiki", ArtifactKind.MARKDOWN),
        ),
        "previous_publication_id": None,
    }
    payload.update(overrides)
    return Manifest(**payload)


def test_artifact_hash_and_size_come_from_the_bytes() -> None:
    artifact = artifact_for_bytes("a/b.md", b"hello", ArtifactKind.MARKDOWN)
    assert artifact.sha256 == hash_bytes(b"hello")
    assert artifact.size == 5
    assert artifact.required is True
    assert artifact.stem == "b"


def test_artifact_normalizes_windows_separators() -> None:
    artifact = artifact_for_bytes("diagrams\\flow.svg", b"<svg/>")
    assert artifact.relative_path == "diagrams/flow.svg"


@pytest.mark.parametrize("bad", ["", "/absolute.md", "../escape.md", "a/../../b.md"])
def test_artifact_rejects_paths_that_leave_the_package(bad: str) -> None:
    with pytest.raises(ManifestInvalid):
        artifact_for_bytes(bad, b"x")


def test_artifact_for_file_reads_the_file_on_disk(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "wiki.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# title")
    artifact = artifact_for_file(tmp_path, "docs/wiki.md", ArtifactKind.MARKDOWN)
    assert artifact.sha256 == hash_file(target) == hash_bytes(b"# title")
    assert artifact.size == 7


def test_manifest_rejects_duplicate_artifact_paths() -> None:
    with pytest.raises(ManifestInvalid):
        build_manifest(
            artifacts=(
                artifact_for_bytes("wiki.md", b"a", ArtifactKind.MARKDOWN),
                artifact_for_bytes("wiki.md", b"b", ArtifactKind.MARKDOWN),
            )
        )


def test_manifest_rejects_empty_publication_id() -> None:
    with pytest.raises(ManifestInvalid):
        build_manifest(publication_id="  ")


def test_manifest_json_is_canonical_and_key_ordered() -> None:
    text = build_manifest().to_json()
    payload = json.loads(text)
    assert list(payload) == sorted(payload)
    assert list(payload["artifacts"][0]) == sorted(payload["artifacts"][0])
    assert text == canonical_json(payload)


def test_manifest_hash_ignores_artifact_declaration_order() -> None:
    docx = artifact_for_bytes("wiki.docx", b"docx-bytes", ArtifactKind.DOCX)
    markdown = artifact_for_bytes("wiki.md", b"# wiki", ArtifactKind.MARKDOWN)
    one = build_manifest(artifacts=(docx, markdown))
    two = build_manifest(artifacts=(docx, markdown))
    assert one.manifest_hash == two.manifest_hash


def test_manifest_hash_follows_content() -> None:
    base = build_manifest()
    other = build_manifest(
        artifacts=(
            artifact_for_bytes("wiki.docx", b"docx-bytes", ArtifactKind.DOCX),
            artifact_for_bytes("wiki.md", b"# changed", ArtifactKind.MARKDOWN),
        )
    )
    assert base.manifest_hash != other.manifest_hash


def test_manifest_roundtrip_through_json() -> None:
    manifest = build_manifest(previous_publication_id="pub-2026-09-01")
    rebuilt = Manifest.from_json(manifest.to_json())
    assert rebuilt == manifest
    assert rebuilt.manifest_hash == manifest.manifest_hash
    assert rebuilt.previous_publication_id == "pub-2026-09-01"


def test_manifest_from_json_rejects_garbage() -> None:
    with pytest.raises(ManifestInvalid):
        Manifest.from_json("not json")
    with pytest.raises(ManifestInvalid):
        Manifest.from_json("[]")
    with pytest.raises(ManifestInvalid):
        Manifest.from_json(json.dumps({"publication_id": "p"}))


def test_manifest_lookup_helpers() -> None:
    manifest = build_manifest()
    assert manifest.relative_paths == ("wiki.docx", "wiki.md")
    assert manifest.artifact("wiki.docx") is manifest.of_kind(ArtifactKind.DOCX)[0]
    assert manifest.artifact("missing.md") is None
    assert manifest.of_kind(ArtifactKind.DIAGRAM_IMAGE) == ()


def test_manifest_normalizes_naive_creation_time() -> None:
    manifest = build_manifest(created_at=datetime(2026, 9, 10, 8, 30, 0))
    assert manifest.created_at.tzinfo is timezone.utc
