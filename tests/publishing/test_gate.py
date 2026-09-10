from __future__ import annotations

from datetime import datetime, timezone

import pytest

from wiki_ai.publishing import release
from wiki_ai.publishing.gate import PublishingRule, check
from wiki_ai.publishing.manifest import (
    MANIFEST_FILENAME,
    Artifact,
    ArtifactKind,
    Manifest,
)
from wiki_ai.publishing.pipeline import PublicationBlocked, Publisher


MOMENT = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _published(graph, publications_dir):
    Publisher().run(graph.repository, publications_dir, "ns")
    return release.current(publications_dir)


def _rules(violations):
    return {item.rule for item in violations}


def _only_staged(publications_dir):
    staging = publications_dir / release.STAGING_DIRNAME
    directories = sorted(entry for entry in staging.iterdir() if entry.is_dir())
    assert len(directories) == 1
    return directories[0]


def test_clean_release_passes(graph, publications_dir):
    published = _published(graph, publications_dir)
    assert check(published.directory, graph.repository) == ()


def test_missing_manifest_is_reported(tmp_path):
    violations = check(tmp_path)
    assert _rules(violations) == {PublishingRule.MANIFEST_UNREADABLE}


def test_absent_docx_is_reported(graph, publications_dir):
    published = _published(graph, publications_dir)
    target = published.directory / published.manifest.artifacts[0].relative_path
    target.unlink()
    assert PublishingRule.REQUIRED_ARTIFACT_MISSING in _rules(
        check(published.directory, graph.repository)
    )


def test_invalid_docx_is_reported(graph, publications_dir):
    published = _published(graph, publications_dir)
    target = published.directory / published.manifest.artifacts[0].relative_path
    target.write_bytes(b"nao e um zip")
    assert PublishingRule.DOCX_INVALID in _rules(
        check(published.directory, graph.repository)
    )


def test_manifest_divergence_is_reported(graph, publications_dir):
    published = _published(graph, publications_dir)
    target = published.directory / published.manifest.artifacts[0].relative_path
    payload = target.read_bytes()
    target.write_bytes(payload + b"\x00")
    assert PublishingRule.MANIFEST_DIVERGES in _rules(
        check(published.directory, graph.repository)
    )


def test_manifest_without_docx_is_reported(tmp_path):
    directory = tmp_path / "release"
    directory.mkdir()
    manifest = Manifest(
        publication_id="pub-vazio",
        revision="r1",
        created_at=MOMENT,
        source_snapshot_hash="abc",
        artifacts=(),
    )
    (directory / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")
    assert PublishingRule.DOCX_MISSING in _rules(check(directory))


def test_diagram_image_without_text_is_reported(tmp_path):
    directory = tmp_path / "release"
    directory.mkdir()
    (directory / "documento.docx").write_bytes(b"")
    (directory / "fluxo.png").write_bytes(b"")
    manifest = Manifest(
        publication_id="pub-imagem",
        revision="r1",
        created_at=MOMENT,
        source_snapshot_hash="abc",
        artifacts=(
            Artifact(
                relative_path="documento.docx",
                sha256="",
                size=0,
                kind=ArtifactKind.DOCX,
            ),
            Artifact(
                relative_path="fluxo.png",
                sha256="",
                size=0,
                kind=ArtifactKind.DIAGRAM_IMAGE,
            ),
        ),
    )
    (directory / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")
    assert PublishingRule.DIAGRAM_IMAGE_WITHOUT_TEXT in _rules(check(directory))


def test_completeness_hiding_blocking_gaps_is_reported(
    graph, publications_dir, monkeypatch, silent_narrative
):
    with pytest.raises(PublicationBlocked):
        Publisher().run(graph.repository, publications_dir, "ns")
    staged = _only_staged(publications_dir)
    assert PublishingRule.COMPLETENESS_HIDES_BLOCKING_GAPS in _rules(
        check(staged, graph.repository)
    )


def test_gate_reads_a_staging_directory(graph, publications_dir):
    published = _published(graph, publications_dir)
    staged = publications_dir / release.STAGING_DIRNAME / published.publication_id
    staged.mkdir(parents=True)
    for artifact in published.manifest.artifacts:
        source = published.directory / artifact.relative_path
        (staged / artifact.relative_path).write_bytes(source.read_bytes())
    (staged / MANIFEST_FILENAME).write_text(
        published.manifest.to_json(), encoding="utf-8"
    )
    assert check(staged, graph.repository) == ()


def test_gate_without_knowledge_skips_the_completeness_rule(graph, publications_dir):
    published = _published(graph, publications_dir)
    violations = check(published.directory)
    assert PublishingRule.COMPLETENESS_HIDES_BLOCKING_GAPS not in _rules(violations)
