from __future__ import annotations

from pathlib import Path

from tests.repository.fixtures_repos import (
    java_repo,
    python_repo,
    snapshot_of,
    web_repo,
    write,
)
from wiki_ai.repository.tests_discovery import MatchReason, related_tests


def test_python_mirrored_test_is_found(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    found = related_tests(snapshot, "billing/service.py")
    assert found[0].path == "tests/test_service.py"
    assert found[0].reason is MatchReason.MIRRORED_NAME


def test_java_mirrored_test_is_found(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    found = related_tests(snapshot, "src/main/java/com/acme/order/OrderService.java")
    assert [item.path for item in found] == [
        "src/test/java/com/acme/order/OrderServiceTest.java"
    ]


def test_spec_file_is_found_for_web_module(tmp_path: Path) -> None:
    snapshot = web_repo(tmp_path)
    found = related_tests(snapshot, "src/client.ts")
    assert found[0].path == "src/client.spec.ts"


def test_symbol_reference_inside_test_files(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    found = related_tests(snapshot, "InvoiceService")
    assert found
    assert found[0].path == "tests/test_service.py"
    assert found[0].reason is MatchReason.SYMBOL_REFERENCE
    assert found[0].line is not None


def test_go_underscore_test_naming(tmp_path: Path) -> None:
    write(tmp_path, "ledger/post.go", "package ledger\n\nfunc Post(entry string) string {\n\treturn entry\n}\n")
    write(tmp_path, "ledger/post_test.go", "package ledger\n\nfunc TestPost(t *testing.T) { Post(\"x\") }\n")
    snapshot = snapshot_of(tmp_path)
    found = related_tests(snapshot, "ledger/post.go")
    assert found[0].path == "ledger/post_test.go"
    assert found[0].reason is MatchReason.MIRRORED_NAME


def test_unrelated_subject_returns_nothing(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    assert related_tests(snapshot, "AbsentSymbol") == ()
    assert related_tests(snapshot, "") == ()
    assert related_tests(snapshot, "billing/service.py", limit=0) == ()


def test_results_are_unique_by_path(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    found = related_tests(snapshot, "src/main/java/com/acme/order/OrderService.java")
    assert len({item.path for item in found}) == len(found)
