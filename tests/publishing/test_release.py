from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from wiki_ai.publishing.manifest import (
    MANIFEST_FILENAME,
    Artifact,
    ArtifactKind,
    Manifest,
    artifact_for_bytes,
    hash_bytes,
)
from wiki_ai.publishing.release import (
    CURRENT_POINTER,
    RELEASES_DIRNAME,
    STAGING_DIRNAME,
    ArtifactMissing,
    HashMismatch,
    PublicationExists,
    PublicationIdInvalid,
    PublicationNotFound,
    current,
    list_publications,
    promote,
    read_manifest,
    release_dir,
    rollback,
    stage,
    staging_dir,
)

CREATED = datetime(2026, 9, 10, 8, 30, 0, tzinfo=timezone.utc)

FILES_V1 = {"wiki.docx": b"docx-v1", "wiki.md": b"# v1"}
FILES_V2 = {"wiki.docx": b"docx-v2", "wiki.md": b"# v2"}


def manifest_for(
    publication_id: str,
    files: dict[str, bytes],
    previous: str | None = None,
) -> Manifest:
    return Manifest(
        publication_id=publication_id,
        revision=publication_id,
        created_at=CREATED,
        source_snapshot_hash="s" * 64,
        artifacts=(
            artifact_for_bytes("wiki.docx", files["wiki.docx"], ArtifactKind.DOCX),
            artifact_for_bytes("wiki.md", files["wiki.md"], ArtifactKind.MARKDOWN),
        ),
        previous_publication_id=previous,
    )


def publish(root: Path, publication_id: str, files: dict[str, bytes], previous=None):
    return promote(stage(root, manifest_for(publication_id, files, previous), files))


def test_stage_writes_files_and_manifest_under_staging(tmp_path: Path) -> None:
    manifest = manifest_for("pub-1", FILES_V1)
    staged = stage(tmp_path, manifest, FILES_V1)

    assert staged.directory == staging_dir(tmp_path, "pub-1")
    assert staged.directory.parent.name == STAGING_DIRNAME
    assert (staged.directory / "wiki.docx").read_bytes() == b"docx-v1"
    assert read_manifest(staged.directory) == manifest
    assert not (tmp_path / RELEASES_DIRNAME).exists()
    assert current(tmp_path) is None


def test_stage_creates_nested_directories(tmp_path: Path) -> None:
    files = {"diagrams/flow.svg": b"<svg/>", "wiki.docx": b"d", "wiki.md": b"m"}
    manifest = Manifest(
        publication_id="pub-nested",
        revision="r1",
        created_at=CREATED,
        source_snapshot_hash="s" * 64,
        artifacts=(
            artifact_for_bytes("diagrams/flow.svg", b"<svg/>", ArtifactKind.DIAGRAM_IMAGE),
        ),
    )
    staged = stage(tmp_path, manifest, files)
    assert (staged.directory / "diagrams" / "flow.svg").read_bytes() == b"<svg/>"


def test_restaging_replaces_the_previous_staging_content(tmp_path: Path) -> None:
    stage(tmp_path, manifest_for("pub-1", FILES_V1), {**FILES_V1, "stray.txt": b"x"})
    staged = stage(tmp_path, manifest_for("pub-1", FILES_V1), FILES_V1)
    assert not (staged.directory / "stray.txt").exists()


def test_promote_publishes_and_points_current_at_it(tmp_path: Path) -> None:
    published = publish(tmp_path, "pub-1", FILES_V1)

    assert published.directory == release_dir(tmp_path, "pub-1")
    assert (published.directory / "wiki.md").read_bytes() == b"# v1"
    assert (published.directory / MANIFEST_FILENAME).is_file()
    assert not staging_dir(tmp_path, "pub-1").exists()
    assert (tmp_path / CURRENT_POINTER).read_text(encoding="utf-8").strip() == "pub-1"

    pointed = current(tmp_path)
    assert pointed is not None
    assert pointed.publication_id == "pub-1"
    assert pointed.manifest == published.manifest


def test_manifest_atomic_promotion(tmp_path: Path) -> None:
    publish(tmp_path, "pub-1", FILES_V1)
    assert current(tmp_path).publication_id == "pub-1"

    broken_manifest = manifest_for("pub-2", FILES_V2, previous="pub-1")
    staged = stage(tmp_path, broken_manifest, FILES_V2)
    (staged.directory / "wiki.md").write_bytes(b"# tampered")

    with pytest.raises(HashMismatch) as raised:
        promote(staged)

    assert raised.value.relative_path == "wiki.md"
    assert not release_dir(tmp_path, "pub-2").exists()
    assert list_publications(tmp_path) == ("pub-1",)
    assert current(tmp_path).publication_id == "pub-1"
    assert (release_dir(tmp_path, "pub-1") / "wiki.md").read_bytes() == b"# v1"
    assert staged.directory.is_dir()

    good = publish(tmp_path, "pub-2", FILES_V2, previous="pub-1")
    assert good.publication_id == "pub-2"
    assert current(tmp_path).publication_id == "pub-2"
    assert list_publications(tmp_path) == ("pub-1", "pub-2")

    restored = rollback(tmp_path, "pub-1")
    assert restored.publication_id == "pub-1"
    assert current(tmp_path).publication_id == "pub-1"
    assert list_publications(tmp_path) == ("pub-1", "pub-2")
    assert (release_dir(tmp_path, "pub-2") / "wiki.md").read_bytes() == b"# v2"


def test_promote_refuses_when_a_required_artifact_is_absent(tmp_path: Path) -> None:
    publish(tmp_path, "pub-1", FILES_V1)
    staged = stage(tmp_path, manifest_for("pub-2", FILES_V2), FILES_V2)
    (staged.directory / "wiki.docx").unlink()

    with pytest.raises(ArtifactMissing) as raised:
        promote(staged)

    assert raised.value.relative_path == "wiki.docx"
    assert not release_dir(tmp_path, "pub-2").exists()
    assert current(tmp_path).publication_id == "pub-1"


def test_promote_tolerates_an_absent_optional_artifact(tmp_path: Path) -> None:
    manifest = Manifest(
        publication_id="pub-opt",
        revision="r1",
        created_at=CREATED,
        source_snapshot_hash="s" * 64,
        artifacts=(
            artifact_for_bytes("wiki.docx", b"d", ArtifactKind.DOCX),
            Artifact("extra.md", hash_bytes(b"never written"), 13, ArtifactKind.MARKDOWN, False),
        ),
    )
    published = promote(stage(tmp_path, manifest, {"wiki.docx": b"d"}))
    assert published.publication_id == "pub-opt"


def test_promoting_an_existing_publication_is_a_typed_error(tmp_path: Path) -> None:
    publish(tmp_path, "pub-1", FILES_V1)

    with pytest.raises(PublicationExists) as raised:
        stage(tmp_path, manifest_for("pub-1", FILES_V2), FILES_V2)

    assert raised.value.publication_id == "pub-1"
    assert (release_dir(tmp_path, "pub-1") / "wiki.md").read_bytes() == b"# v1"


def test_promote_detects_a_publication_created_after_staging(tmp_path: Path) -> None:
    staged = stage(tmp_path, manifest_for("pub-1", FILES_V1), FILES_V1)
    release_dir(tmp_path, "pub-1").mkdir(parents=True)

    with pytest.raises(PublicationExists):
        promote(staged)

    assert staged.directory.is_dir()


def test_rollback_only_moves_the_pointer(tmp_path: Path) -> None:
    publish(tmp_path, "pub-1", FILES_V1)
    publish(tmp_path, "pub-2", FILES_V2, previous="pub-1")

    rollback(tmp_path, "pub-1")
    assert current(tmp_path).publication_id == "pub-1"
    assert release_dir(tmp_path, "pub-2").is_dir()

    rollback(tmp_path, "pub-2")
    assert current(tmp_path).publication_id == "pub-2"
    assert list_publications(tmp_path) == ("pub-1", "pub-2")


def test_rollback_to_an_unknown_publication_leaves_the_pointer(tmp_path: Path) -> None:
    publish(tmp_path, "pub-1", FILES_V1)

    with pytest.raises(PublicationNotFound):
        rollback(tmp_path, "pub-404")

    assert current(tmp_path).publication_id == "pub-1"


def test_current_is_none_before_any_publication(tmp_path: Path) -> None:
    assert current(tmp_path) is None
    assert list_publications(tmp_path) == ()


def test_current_reports_a_dangling_pointer(tmp_path: Path) -> None:
    publish(tmp_path, "pub-1", FILES_V1)
    (tmp_path / CURRENT_POINTER).write_text("pub-gone\n", encoding="utf-8")

    with pytest.raises(PublicationNotFound):
        current(tmp_path)


@pytest.mark.parametrize("bad", ["../escape", "with/slash", "", ".hidden"])
def test_publication_id_must_be_a_safe_segment(tmp_path: Path, bad: str) -> None:
    with pytest.raises(PublicationIdInvalid):
        release_dir(tmp_path, bad)


def test_read_manifest_requires_the_manifest_file(tmp_path: Path) -> None:
    with pytest.raises(ArtifactMissing):
        read_manifest(tmp_path)
