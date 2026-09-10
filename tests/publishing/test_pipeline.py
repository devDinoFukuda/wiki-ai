from __future__ import annotations

import re
from dataclasses import replace

import pytest

from wiki_ai.knowledge.repository import DATABASE_FILENAME, KnowledgeRepository
from wiki_ai.publishing import pipeline as pipeline_module
from wiki_ai.publishing import release, render_docx as render_docx_module
from wiki_ai.publishing.docx import package as docx_package
from wiki_ai.publishing.docx.inspect import read_package
from wiki_ai.publishing.gate import PublishingRule
from wiki_ai.publishing.manifest import MANIFEST_FILENAME, ArtifactKind
from wiki_ai.publishing.model import (
    CAPABILITY_SECTIONS,
    GAPS_SECTION_TITLE,
    NarrativeKind,
)
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


def _staging_entries(publications_dir):
    staging = publications_dir / release.STAGING_DIRNAME
    if not staging.is_dir():
        return ()
    return tuple(sorted(entry.name for entry in staging.iterdir() if entry.is_dir()))


def test_gate_blocks_when_narrative_hides_a_blocking_gap(
    graph, publications_dir, silent_narrative
):
    with pytest.raises(PublicationBlocked) as caught:
        Publisher().run(graph.repository, publications_dir, "ns")
    assert any(
        BlockReason.GATE_REFUSED.value in reason for reason in caught.value.reasons
    )
    assert any(
        PublishingRule.COMPLETENESS_HIDES_BLOCKING_GAPS.value in reason
        for reason in caught.value.reasons
    )


def test_gate_block_promotes_nothing_and_keeps_current_untouched(
    graph, publications_dir, silent_narrative
):
    with pytest.raises(PublicationBlocked):
        Publisher().run(graph.repository, publications_dir, "ns")
    assert release.list_publications(publications_dir) == ()
    assert release.current(publications_dir) is None


def test_gate_block_preserves_the_staging_directory(
    graph, publications_dir, silent_narrative
):
    with pytest.raises(PublicationBlocked):
        Publisher().run(graph.repository, publications_dir, "ns")
    staged = _staging_entries(publications_dir)
    assert len(staged) == 1
    directory = publications_dir / release.STAGING_DIRNAME / staged[0]
    assert (directory / MANIFEST_FILENAME).is_file()
    assert any(directory.glob("*.docx"))


def test_gate_block_names_every_offending_document(
    graph, publications_dir, silent_narrative
):
    with pytest.raises(PublicationBlocked) as caught:
        Publisher().run(graph.repository, publications_dir, "ns")
    assert len(caught.value.reasons) >= 1
    assert all(".docx" in reason for reason in caught.value.reasons)


def test_gate_blocks_when_the_gaps_heading_is_absent_from_the_docx(
    graph, publications_dir, monkeypatch
):
    original = render_docx_module.render_document

    def without_gaps_heading(document, out_path):
        stripped = replace(
            document,
            body=tuple(
                block
                for block in document.body
                if not (
                    block.kind is NarrativeKind.SECTION
                    and block.title == GAPS_SECTION_TITLE
                )
            ),
        )
        return original(stripped, out_path)

    monkeypatch.setattr(pipeline_module, "render_document", without_gaps_heading)
    with pytest.raises(PublicationBlocked) as caught:
        Publisher().run(graph.repository, publications_dir, "ns")
    assert any(
        BlockReason.GATE_REFUSED.value in reason for reason in caught.value.reasons
    )
    assert release.current(publications_dir) is None


def test_a_blocked_run_can_be_retried_after_the_narrative_is_repaired(
    graph, publications_dir, monkeypatch
):
    import wiki_ai.publishing.narrative as narrative_module

    monkeypatch.setattr(
        narrative_module.NarrativeBuilder, "_gap_assertions", lambda self, gaps: ()
    )
    monkeypatch.setattr(
        narrative_module.NarrativeBuilder,
        "_reserved_assertions",
        lambda self, entities: (),
    )
    with pytest.raises(PublicationBlocked):
        Publisher().run(graph.repository, publications_dir, "ns")
    monkeypatch.undo()
    outcome = _publish(graph, publications_dir)
    assert release.current(publications_dir).publication_id == outcome.publication_id


def test_happy_path_leaves_no_staging_directory_behind(graph, publications_dir):
    _publish(graph, publications_dir)
    assert _staging_entries(publications_dir) == ()


def test_a_corrupted_release_blocks_the_rerun_instead_of_being_reused(
    graph, publications_dir
):
    first = _publish(graph, publications_dir)
    published = release.current(publications_dir)
    target = published.directory / published.manifest.artifacts[0].relative_path
    target.write_bytes(b"nao e mais um docx")
    with pytest.raises(PublicationBlocked) as caught:
        Publisher().run(graph.repository, publications_dir, "ns")
    assert any(
        BlockReason.EXISTING_INVALID.value in reason for reason in caught.value.reasons
    )
    assert release.list_publications(publications_dir) == (first.publication_id,)
    assert release.current(publications_dir).publication_id == first.publication_id
    assert target.read_bytes() == b"nao e mais um docx"


def test_a_release_missing_an_artifact_blocks_the_rerun(graph, publications_dir):
    _publish(graph, publications_dir)
    published = release.current(publications_dir)
    (published.directory / published.manifest.artifacts[0].relative_path).unlink()
    with pytest.raises(PublicationBlocked) as caught:
        Publisher().run(graph.repository, publications_dir, "ns")
    assert any(
        BlockReason.EXISTING_INVALID.value in reason for reason in caught.value.reasons
    )


def test_an_intact_release_is_reused_without_being_rewritten(graph, publications_dir):
    first = _publish(graph, publications_dir)
    published = release.current(publications_dir)
    before = {
        artifact.relative_path: (
            published.directory / artifact.relative_path
        ).read_bytes()
        for artifact in published.manifest.artifacts
    }
    second = Publisher().run(graph.repository, publications_dir, "ns")
    after = {
        artifact.relative_path: (
            published.directory / artifact.relative_path
        ).read_bytes()
        for artifact in published.manifest.artifacts
    }
    assert first == second
    assert before == after


def test_idempotent_rerun_still_passes_the_gate(graph, publications_dir):
    first = _publish(graph, publications_dir)
    second = _publish(graph, publications_dir)
    assert first == second
    assert release.list_publications(publications_dir) == (first.publication_id,)
