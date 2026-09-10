from __future__ import annotations

from pathlib import Path

import pytest

from tests.acceptance.reachability import (
    PUBLIC_API,
    modules_of,
    reachable,
    source_root,
    unreachable,
)
from tests.acceptance.scenarios import (
    NAMESPACE_OBJECTIVE,
    assert_settled,
    cobol_scripts,
    java_acceptance_repo,
    java_scripts,
    mainframe_acceptance_repo,
    scripted_registry,
)
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app import api
from wiki_ai.app.session import Session
from wiki_ai.knowledge import gate as knowledge_gate
from wiki_ai.publishing import gate as publishing_gate
from wiki_ai.publishing.release import RELEASES_DIRNAME
from wiki_ai.quality import architecture, dead_code, source_hygiene


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture()
def java(tmp_path: Path) -> tuple[Path, ProviderRegistry]:
    repo = java_acceptance_repo(tmp_path / "java")
    registry = scripted_registry(java_scripts())
    report = api.analyze(repo, NAMESPACE_OBJECTIVE, registry=registry)
    assert_settled(report)
    return repo, registry


@pytest.fixture()
def mainframe(tmp_path: Path) -> Path:
    repo = mainframe_acceptance_repo(tmp_path / "mainframe")
    report = api.analyze(
        repo, NAMESPACE_OBJECTIVE, registry=scripted_registry(cobol_scripts())
    )
    assert_settled(report)
    return repo


def test_the_source_hygiene_gate_is_empty() -> None:
    root = _project_root()
    violations = source_hygiene.scan([root / "src", root / "tests"])
    assert [str(item) for item in violations] == []


def test_the_architecture_gate_is_empty() -> None:
    violations = architecture.check(_project_root() / "src")
    assert [str(item) for item in violations] == []


def test_the_dead_code_gate_finds_no_unreachable_production_module() -> None:
    assert unreachable(source_root()) == ()


def test_dead_code_gate() -> None:
    root = _project_root()
    found = dead_code.check(root / "src", root / "tests")
    assert [str(item) for item in found] == []


def test_every_declared_public_api_module_exists_and_is_out_of_the_call_graph() -> None:
    root = source_root()
    known = modules_of(root)
    reached = reachable(root)
    for name in PUBLIC_API:
        assert name in known, name
        assert name not in reached, name


def test_the_knowledge_gate_is_empty_for_the_java_scenario(
    java: tuple[Path, ProviderRegistry]
) -> None:
    repo, _ = java
    with Session.open(repo).open_knowledge() as knowledge:
        violations = knowledge_gate.check(knowledge)
    assert [item.detail for item in violations] == []


def test_the_knowledge_gate_is_empty_for_the_mainframe_scenario(
    mainframe: Path,
) -> None:
    with Session.open(mainframe).open_knowledge() as knowledge:
        violations = knowledge_gate.check(knowledge)
    assert [item.detail for item in violations] == []


def test_the_knowledge_gate_sees_the_current_source_version_as_matching(
    java: tuple[Path, ProviderRegistry]
) -> None:
    repo, _ = java
    session = Session.open(repo)
    with session.open_knowledge() as knowledge:
        current = {
            record.source_id: record.version_hash
            for record in knowledge.source_versions()
        }
        violations = knowledge_gate.check(knowledge, current)
    assert [item.detail for item in violations] == []


def test_the_publishing_gate_is_empty_for_the_released_package(
    java: tuple[Path, ProviderRegistry]
) -> None:
    repo, registry = java
    report = api.publish(repo, registry=registry)
    assert report.status == "ok", report.to_dict()
    release = (
        Session.open(repo).publications_dir / RELEASES_DIRNAME / report.publication_id
    )
    with Session.open(repo).open_knowledge() as knowledge:
        violations = publishing_gate.check(release, knowledge)
    assert [item.to_dict() for item in violations] == []


def test_no_production_module_exceeds_the_size_limit() -> None:
    oversized = {
        name: len(path.read_text(encoding="utf-8").splitlines())
        for name, path in modules_of(source_root()).items()
        if len(path.read_text(encoding="utf-8").splitlines()) > 1000
    }
    assert oversized == {}
