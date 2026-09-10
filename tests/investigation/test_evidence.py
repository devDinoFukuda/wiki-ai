from __future__ import annotations

from pathlib import Path

from tests.repository.fixtures_repos import (
    java_repo,
    mainframe_repo,
    python_repo,
    snapshot_of,
    write,
)

from wiki_ai.knowledge.evidence import CodeContent, CodeLocator
from wiki_ai.repository.evidence import capture
from wiki_ai.investigation.evidence import (
    CaptureRegistry,
    capture_id,
    detect_content,
    to_knowledge,
)

STAMP = "2026-09-10T00:00:00+00:00"


def test_executable_java_lines_are_detected_as_executable(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, "src/main/java/com/acme/order/OrderService.java", 10, 12)
    assert detect_content(item.path, item.excerpt) is CodeContent.EXECUTABLE


def test_yaml_configuration_is_detected_as_config_value(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(snapshot, "src/main/resources/application.yml", 4, 6)
    assert detect_content(item.path, item.excerpt) is CodeContent.CONFIG_VALUE


def test_a_pure_comment_block_is_detected_as_comment(tmp_path: Path) -> None:
    write(tmp_path, "src/remark.java", "// renewal happens yearly\n// unless cancelled\n")
    snapshot = snapshot_of(tmp_path)
    item = capture(snapshot, "src/remark.java", 1, 2)
    assert detect_content(item.path, item.excerpt) is CodeContent.COMMENT


def test_a_python_docstring_is_detected_as_docstring(tmp_path: Path) -> None:
    write(tmp_path, "svc.py", '"""renewal rule\n"""\n')
    snapshot = snapshot_of(tmp_path)
    item = capture(snapshot, "svc.py", 1, 2)
    assert detect_content(item.path, item.excerpt) is CodeContent.DOCSTRING


def test_markdown_prose_is_detected_as_markup(tmp_path: Path) -> None:
    write(tmp_path, "README.md", "# renewal\nthe rule renews yearly\n")
    snapshot = snapshot_of(tmp_path)
    item = capture(snapshot, "README.md", 1, 2)
    assert detect_content(item.path, item.excerpt) is CodeContent.MARKUP


def test_cobol_comment_column_seven_is_detected_as_comment(tmp_path: Path) -> None:
    write(tmp_path, "prog.cbl", "      * PAYRUN COMPUTES GROSS\n      * AND NET\n")
    snapshot = snapshot_of(tmp_path)
    item = capture(snapshot, "prog.cbl", 1, 2)
    assert detect_content(item.path, item.excerpt) is CodeContent.COMMENT


def test_cobol_procedure_lines_stay_executable(tmp_path: Path) -> None:
    snapshot = mainframe_repo(tmp_path)
    item = capture(snapshot, "cobol/PAYRUN.cbl", 7, 10)
    assert detect_content(item.path, item.excerpt) is CodeContent.EXECUTABLE


def test_bridge_produces_source_version_and_evidence_bound_to_the_snapshot(
    tmp_path: Path,
) -> None:
    snapshot = python_repo(tmp_path)
    item = capture(snapshot, "billing/service.py", 9, 11, symbol="issue")
    version, evidence = to_knowledge(item, snapshot, "acme", STAMP)
    assert version.source_id == "acme"
    assert version.version_hash == snapshot.digest
    assert version.locator_root == snapshot.root
    assert evidence.source_id == "acme"
    assert evidence.version_hash == snapshot.digest
    assert isinstance(evidence.locator, CodeLocator)
    assert evidence.locator.path == "billing/service.py"
    assert evidence.locator.symbol == "issue"
    assert evidence.locator.content is CodeContent.EXECUTABLE


def test_bridge_is_deterministic_for_the_same_capture(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    item = capture(snapshot, "billing/service.py", 9, 11)
    first = to_knowledge(item, snapshot, "acme", STAMP)[1]
    second = to_knowledge(item, snapshot, "acme", STAMP)[1]
    assert first.id == second.id


def test_registry_resolves_by_identifier_and_by_locator(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    item = capture(
        snapshot, "src/main/java/com/acme/order/OrderService.java", 10, 12, symbol="place"
    )
    registry = CaptureRegistry()
    registered = registry.register(item)
    assert registered.identifier == capture_id(item)
    assert registry.get(registered.identifier) is item
    assert registry.get(item.locator()) is item
    assert (
        registry.find("src/main/java/com/acme/order/OrderService.java", 10, 12, "place")
        is item
    )
    assert len(registry) == 1
    assert registry.identifiers() == (registered.identifier,)


def test_registry_returns_nothing_for_an_unknown_reference() -> None:
    assert CaptureRegistry().get("cap_forged") is None
