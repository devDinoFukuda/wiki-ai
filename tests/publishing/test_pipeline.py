from __future__ import annotations

import re

import pytest

from wiki_ai.knowledge.repository import DATABASE_FILENAME, KnowledgeRepository
from wiki_ai.publishing import release, render_docx as render_docx_module
from wiki_ai.publishing.docx import package as docx_package
from wiki_ai.publishing.docx.inspect import read_package
from wiki_ai.publishing.manifest import ArtifactKind
from wiki_ai.publishing.model import CAPABILITY_SECTIONS
from wiki_ai.publishing.pipeline import (
    BlockReason,
    PublicationBlocked,
    Publisher,
    docx_structure_problems,
)

_INTERNAL_ID = re.compile(r"(ent_|rel_|srcv_)[0-9a-f]{8,}")


def _publish(graph, publications_dir):
    return Publisher().run(graph.repository, publications_dir, "ns")


def test_publish_emits_one_docx_per_planned_document(graph, publications_dir):
    outcome = _publish(graph, publications_dir)
    assert len(outcome.artifacts) == 8
    assert all(name.endswith(".docx") for name in outcome.artifacts)


def test_every_docx_opens_as_a_valid_package(graph, publications_dir):
    _publish(graph, publications_dir)
    published = release.current(publications_dir)
    for artifact in published.manifest.artifacts:
        summary = read_package(published.directory / artifact.relative_path)
        assert summary.heading_count >= 1


def test_capability_docx_carries_the_fifteen_headings(graph, publications_dir):
    _publish(graph, publications_dir)
    published = release.current(publications_dir)
    path = published.directory / "capacidade-renovacao-cc9b6d1f.docx"
    summary = read_package(path)
    for number, title in enumerate(CAPABILITY_SECTIONS, start=1):
        assert f"{number}. {title}" in summary.text


def test_docx_body_hides_internal_identifiers(graph, publications_dir):
    _publish(graph, publications_dir)
    published = release.current(publications_dir)
    for artifact in published.manifest.artifacts:
        summary = read_package(published.directory / artifact.relative_path)
        head = summary.text.split("Rastreabilidade técnica")[0]
        assert not _INTERNAL_ID.search(head)


def test_manifest_lists_every_docx_as_required(graph, publications_dir):
    outcome = _publish(graph, publications_dir)
    published = release.current(publications_dir)
    assert set(published.manifest.relative_paths) == set(outcome.artifacts)
    docx = published.manifest.of_kind(ArtifactKind.DOCX)
    assert len(docx) == len(outcome.artifacts)
    assert all(item.required for item in docx)


def test_manifest_records_the_knowledge_snapshot(graph, publications_dir):
    _publish(graph, publications_dir)
    published = release.current(publications_dir)
    assert published.manifest.source_snapshot_hash
    assert published.manifest.revision == graph.repository.head_revision_id()


def test_current_pointer_targets_the_publication(graph, publications_dir):
    outcome = _publish(graph, publications_dir)
    assert release.current(publications_dir).publication_id == outcome.publication_id


def test_second_run_reuses_the_same_release(graph, publications_dir):
    first = _publish(graph, publications_dir)
    second = _publish(graph, publications_dir)
    assert first.publication_id == second.publication_id
    assert first.manifest_hash == second.manifest_hash
    assert release.list_publications(publications_dir) == (first.publication_id,)


def test_docx_bytes_are_identical_across_runs(graph, publications_dir, tmp_path):
    _publish(graph, publications_dir)
    published = release.current(publications_dir)
    before = {
        artifact.relative_path: (published.directory / artifact.relative_path).read_bytes()
        for artifact in published.manifest.artifacts
    }
    _publish(graph, publications_dir)
    after = {
        artifact.relative_path: (published.directory / artifact.relative_path).read_bytes()
        for artifact in published.manifest.artifacts
    }
    assert before == after


def test_empty_knowledge_is_blocked(tmp_path, publications_dir):
    repository = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    with pytest.raises(PublicationBlocked) as caught:
        Publisher().run(repository, publications_dir, "ns")
    assert BlockReason.EMPTY_PLAN.value in caught.value.reasons
    repository.close()


def test_invalid_docx_blocks_and_promotes_nothing(
    graph, publications_dir, monkeypatch
):
    def broken(blocks, properties, out_path, media=None):
        from pathlib import Path

        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"nao e um zip")
        return target

    monkeypatch.setattr(docx_package, "build_package", broken)
    monkeypatch.setattr(render_docx_module, "build_package", broken)
    with pytest.raises(PublicationBlocked) as caught:
        Publisher().run(graph.repository, publications_dir, "ns")
    assert any(
        BlockReason.DOCX_INVALID.value in reason for reason in caught.value.reasons
    )
    assert release.list_publications(publications_dir) == ()
    assert release.current(publications_dir) is None


def test_docx_structure_problems_reports_a_broken_file(tmp_path):
    broken = tmp_path / "quebrado.docx"
    broken.write_bytes(b"nao e um zip")
    assert docx_structure_problems(broken)


def test_docx_structure_problems_accepts_a_good_file(graph, publications_dir):
    _publish(graph, publications_dir)
    published = release.current(publications_dir)
    path = published.directory / published.manifest.artifacts[0].relative_path
    assert docx_structure_problems(path) == []
