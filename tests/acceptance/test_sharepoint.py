from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.acceptance.scenarios import (
    CAPABILITY,
    NAMESPACE_OBJECTIVE,
    java_acceptance_repo,
    java_scripts,
    scripted_registry,
)
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app import api
from wiki_ai.app.commands import COMMANDS, EXIT_OK, main
from wiki_ai.app.session import Session
from wiki_ai.publishing import gate
from wiki_ai.publishing.docx.inspect import read_package
from wiki_ai.publishing.manifest import MANIFEST_FILENAME, ArtifactKind
from wiki_ai.publishing.model import (
    CAPABILITY_SECTIONS,
    GAPS_SECTION_TITLE,
    TRACEABILITY_SECTION_TITLE,
)
from wiki_ai.publishing.release import (
    CURRENT_POINTER,
    RELEASES_DIRNAME,
    STAGING_DIRNAME,
    read_manifest,
)

RULE = "every placed order is saved"
MANUAL_COMMANDS: tuple[str, ...] = ("promote", "compile", "docx", "handoff", "merge")


@pytest.fixture()
def registry() -> ProviderRegistry:
    return scripted_registry(java_scripts())


@pytest.fixture()
def analyzed(tmp_path: Path, registry: ProviderRegistry) -> Path:
    repo = java_acceptance_repo(tmp_path / "repo")
    report = api.analyze(repo, NAMESPACE_OBJECTIVE, registry=registry)
    assert report.status == "ok", report.to_dict()
    return repo


def _publish(repo: Path, registry: ProviderRegistry) -> tuple[int, dict]:
    stream = io.StringIO()
    code = main(["publish", "--repo", str(repo)], stream=stream, registry=registry)
    return code, json.loads(stream.getvalue())


@pytest.fixture()
def published(analyzed: Path, registry: ProviderRegistry) -> tuple[Path, dict]:
    code, payload = _publish(analyzed, registry)
    assert code == EXIT_OK, payload
    assert payload["status"] == "ok"
    return analyzed, payload


def _release_dir(repo: Path, publication_id: str) -> Path:
    return Session.open(repo).publications_dir / RELEASES_DIRNAME / publication_id


def _capability_docx(repo: Path, publication_id: str) -> Path:
    release = _release_dir(repo, publication_id)
    manifest = read_manifest(release)
    for artifact in manifest.of_kind(ArtifactKind.DOCX):
        summary = read_package(release / artifact.relative_path)
        if CAPABILITY.lower() in summary.text.lower():
            return release / artifact.relative_path
    raise AssertionError(f"no docx describes the capability {CAPABILITY!r}")


def test_a_single_command_delivers_the_publication(
    published: tuple[Path, dict],
) -> None:
    repo, payload = published
    assert payload["publication_id"]
    assert payload["artifacts"]
    assert payload["manifest_hash"]
    pointer = (
        Session.open(repo).publications_dir / CURRENT_POINTER
    ).read_text(encoding="utf-8").strip()
    assert pointer == payload["publication_id"]


def test_the_docx_exists(published: tuple[Path, dict]) -> None:
    repo, payload = published
    release = _release_dir(repo, payload["publication_id"])
    produced = sorted(item.name for item in release.glob("*.docx"))
    assert produced
    assert set(payload["artifacts"]) >= set(produced)


def test_the_docx_content_is_searchable(published: tuple[Path, dict]) -> None:
    repo, payload = published
    summary = read_package(_capability_docx(repo, payload["publication_id"]))
    assert summary.paragraph_count > 0
    assert CAPABILITY in summary.text
    assert RULE in summary.text


def test_the_structure_is_domain_oriented(published: tuple[Path, dict]) -> None:
    repo, payload = published
    summary = read_package(_capability_docx(repo, payload["publication_id"]))
    assert summary.heading_count >= len(CAPABILITY_SECTIONS)
    for title in CAPABILITY_SECTIONS:
        assert title in summary.text, title
    assert GAPS_SECTION_TITLE in summary.text
    assert TRACEABILITY_SECTION_TITLE in summary.text


def test_no_manual_intermediate_file_is_required(
    published: tuple[Path, dict],
) -> None:
    repo, payload = published
    publications = Session.open(repo).publications_dir
    release = _release_dir(repo, payload["publication_id"])
    declared = set(read_manifest(release).relative_paths) | {MANIFEST_FILENAME}
    produced = {
        item.relative_to(release).as_posix()
        for item in release.rglob("*")
        if item.is_file()
    }
    assert produced == declared
    staging = publications / STAGING_DIRNAME
    assert not staging.exists() or not any(staging.iterdir())


def test_the_manifest_carries_the_hash_and_the_revision(
    published: tuple[Path, dict],
) -> None:
    repo, payload = published
    release = _release_dir(repo, payload["publication_id"])
    manifest = read_manifest(release)
    assert manifest.revision
    assert manifest.source_snapshot_hash
    assert manifest.manifest_hash == payload["manifest_hash"]
    for artifact in manifest.artifacts:
        assert artifact.sha256
        assert (release / artifact.relative_path).is_file()


def test_the_delivery_needs_no_promote_compile_or_manual_docx(
    analyzed: Path, registry: ProviderRegistry
) -> None:
    for word in MANUAL_COMMANDS:
        assert word not in COMMANDS
    code, payload = _publish(analyzed, registry)
    assert code == EXIT_OK
    release = _release_dir(analyzed, payload["publication_id"])
    assert list(release.glob("*.docx"))


def test_the_publishing_gate_is_clean(published: tuple[Path, dict]) -> None:
    repo, payload = published
    release = _release_dir(repo, payload["publication_id"])
    with Session.open(repo).open_knowledge() as knowledge:
        violations = gate.check(release, knowledge)
    assert [item.to_dict() for item in violations] == []


def test_every_diagram_image_has_a_textual_equivalent(
    published: tuple[Path, dict],
) -> None:
    repo, payload = published
    release = _release_dir(repo, payload["publication_id"])
    manifest = read_manifest(release)
    for artifact in manifest.of_kind(ArtifactKind.DOCX):
        summary = read_package(release / artifact.relative_path)
        if summary.image_count:
            assert summary.text.strip()


def test_a_second_publish_keeps_the_same_delivery(
    analyzed: Path, registry: ProviderRegistry
) -> None:
    first_code, first = _publish(analyzed, registry)
    second_code, second = _publish(analyzed, registry)
    assert first_code == second_code == EXIT_OK
    assert first["publication_id"] == second["publication_id"]
    assert first["manifest_hash"] == second["manifest_hash"]
