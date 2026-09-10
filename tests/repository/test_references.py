from __future__ import annotations

from pathlib import Path

import pytest

from tests.repository.fixtures_repos import (
    java_repo,
    mainframe_repo,
    python_repo,
    web_repo,
)
from wiki_ai.repository.references import SymbolNameInvalid, find_references


def test_references_exclude_definitions_by_default(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    references = find_references(snapshot, "InvoiceService")
    assert references
    assert all(not item.is_definition for item in references)
    paths = {item.path for item in references}
    assert "tests/test_service.py" in paths


def test_references_can_include_definitions(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    references = find_references(snapshot, "InvoiceService", exclude_definition=False)
    assert any(item.is_definition for item in references)


def test_references_flag_tests(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    references = find_references(snapshot, "OrderService")
    flags = {item.path: item.is_test for item in references}
    assert flags["src/test/java/com/acme/order/OrderServiceTest.java"] is True
    assert flags["src/main/java/com/acme/order/OrderController.java"] is False


def test_references_rank_same_directory_and_language_first(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    references = find_references(
        snapshot,
        "OrderService",
        origin_path="src/main/java/com/acme/order/OrderService.java",
    )
    assert references[0].path.startswith("src/main/java/com/acme/order/")
    assert references[0].rank < references[-1].rank


def test_references_use_word_boundaries(tmp_path: Path) -> None:
    snapshot = web_repo(tmp_path)
    references = find_references(snapshot, "Cart")
    assert references
    for item in references:
        column = item.column - 1
        assert item.excerpt[column : column + 4] == "Cart"
        before = item.excerpt[column - 1] if column > 0 else " "
        assert not (before.isalnum() or before == "_")


def test_references_work_for_cobol_without_parser(tmp_path: Path) -> None:
    snapshot = mainframe_repo(tmp_path)
    references = find_references(snapshot, "PAYRUN")
    paths = {item.path for item in references}
    assert "jcl/PAYJOB.jcl" in paths


def test_references_honour_limit(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    assert len(find_references(snapshot, "OrderService", limit=2)) <= 2


def test_references_reject_unusable_names(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    with pytest.raises(SymbolNameInvalid):
        find_references(snapshot, "")
    with pytest.raises(SymbolNameInvalid):
        find_references(snapshot, "not a name")
    with pytest.raises(SymbolNameInvalid):
        find_references(snapshot, "InvoiceService", limit=0)
