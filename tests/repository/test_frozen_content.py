from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from tests.repository.fixtures_repos import store_for
from wiki_ai.repository.config import find_config
from wiki_ai.repository.dependencies import detect_dependencies
from wiki_ai.repository.evidence import capture, excerpt_digest, verify
from wiki_ai.repository.harness import RepositoryHarness
from wiki_ai.repository.reader import read_range
from wiki_ai.repository.references import find_references
from wiki_ai.repository.search import search
from wiki_ai.repository.snapshot import (
    RepositorySnapshot,
    SnapshotNotMaterialized,
    SnapshotSpec,
    observe,
    take_snapshot,
)
from wiki_ai.repository.store import SnapshotStore
from wiki_ai.repository.symbols import find_symbols
from wiki_ai.repository.tests_discovery import related_tests

_SERVICE_A = b"""import os

INVOICE_TOPIC = os.environ["INVOICE_TOPIC"]


class InvoiceService:
    def charge(self, amount):
        return amount * 2
"""

_SERVICE_B = b"""import os

BILLING_TOPIC = os.environ["BILLING_TOPIC"]


class PaymentService:
    def refund(self, amount):
        return amount * 99
"""

_TEST_FILE = b"""from service import InvoiceService


def test_charge():
    assert InvoiceService().charge(2) == 4
"""

_READING_CALLS = frozenset({"read_bytes", "read_text", "open"})
_CONTENT_MODULES = frozenset({"snapshot.py", "store.py"})
_SNAPSHOT_RECEIVERS = frozenset({"snapshot", "self"})


def _write(root: Path, relative: str, payload: bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _repo(root: Path) -> None:
    _write(root, "service.py", _SERVICE_A)
    _write(root, "test_service.py", _TEST_FILE)


def _snapshot_a(root: Path) -> RepositorySnapshot:
    _repo(root)
    return take_snapshot(SnapshotSpec(root=root), store_for(root))


def test_read_returns_snapshot_content_after_the_working_tree_changed(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_a(tmp_path)
    _write(tmp_path, "service.py", _SERVICE_B)
    result = read_range(snapshot, "service.py", 1, 8)
    assert "InvoiceService" in result.text
    assert "PaymentService" not in result.text


def test_search_returns_snapshot_content_after_the_working_tree_changed(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_a(tmp_path)
    _write(tmp_path, "service.py", _SERVICE_B)
    assert search(snapshot, "InvoiceService").matches
    assert search(snapshot, "PaymentService").matches == ()


def test_symbols_and_references_use_snapshot_content(tmp_path: Path) -> None:
    snapshot = _snapshot_a(tmp_path)
    _write(tmp_path, "service.py", _SERVICE_B)
    names = {item.name for item in find_symbols(snapshot)}
    assert "InvoiceService" in names
    assert "PaymentService" not in names
    references = find_references(snapshot, "InvoiceService")
    assert {item.path for item in references} == {"test_service.py"}


def test_config_and_dependencies_use_snapshot_content(tmp_path: Path) -> None:
    snapshot = _snapshot_a(tmp_path)
    _write(tmp_path, "service.py", _SERVICE_B)
    assert find_config(snapshot, "INVOICE_TOPIC")
    assert find_config(snapshot, "BILLING_TOPIC") == ()
    report = detect_dependencies(snapshot)
    assert {item.path for item in report.imports} >= {"service.py"}


def test_tests_discovery_uses_snapshot_content(tmp_path: Path) -> None:
    snapshot = _snapshot_a(tmp_path)
    _write(tmp_path, "service.py", _SERVICE_B)
    assert {item.path for item in related_tests(snapshot, "InvoiceService")} == {
        "test_service.py"
    }


def test_capture_and_verify_use_snapshot_content(tmp_path: Path) -> None:
    snapshot = _snapshot_a(tmp_path)
    _write(tmp_path, "service.py", _SERVICE_B)
    item = capture(snapshot, "service.py", 6, 6, symbol="InvoiceService")
    assert "InvoiceService" in item.excerpt
    assert verify(snapshot, item) is True


def test_verify_rejects_a_capture_forged_with_other_bytes(tmp_path: Path) -> None:
    snapshot = _snapshot_a(tmp_path)
    other_root = tmp_path.parent / "other"
    _write(other_root, "service.py", _SERVICE_B)
    _write(other_root, "test_service.py", _TEST_FILE)
    other = take_snapshot(SnapshotSpec(root=other_root), store_for(other_root))
    forged_source = capture(other, "service.py", 6, 6)
    forged = dataclasses.replace(
        forged_source,
        snapshot_id=snapshot.digest,
        file_sha256=snapshot.file_map()["service.py"].sha256,
    )
    assert "PaymentService" in forged.excerpt
    assert excerpt_digest(forged.excerpt) == forged.excerpt_sha256
    assert verify(snapshot, forged) is False


def test_verify_rejects_a_capture_whose_blob_was_tampered_with(tmp_path: Path) -> None:
    snapshot = _snapshot_a(tmp_path)
    item = capture(snapshot, "service.py", 6, 6)
    store = store_for(tmp_path)
    sha256 = snapshot.file_map()["service.py"].sha256
    blob = store.root / "blobs" / sha256[:2] / sha256
    blob.write_bytes(_SERVICE_B)
    assert verify(snapshot, item) is False


def test_harness_tools_serve_snapshot_content(tmp_path: Path) -> None:
    snapshot = _snapshot_a(tmp_path)
    _write(tmp_path, "service.py", _SERVICE_B)
    harness = RepositoryHarness(snapshot)
    read = harness.invoke("repo.read", {"path": "service.py"})
    assert "InvoiceService" in read["text"]
    assert "PaymentService" not in read["text"]
    found = harness.invoke("repo.search", {"pattern": "PaymentService"})
    assert found["matches"] == []
    captured = harness.invoke(
        "evidence.capture", {"path": "service.py", "line_start": 6, "line_end": 6}
    )
    assert "InvoiceService" in captured["excerpt"]


def test_observe_detects_the_change_the_snapshot_does_not_see(tmp_path: Path) -> None:
    snapshot = _snapshot_a(tmp_path)
    assert observe(SnapshotSpec(root=tmp_path)).digest == snapshot.digest
    _write(tmp_path, "service.py", _SERVICE_B)
    later = observe(SnapshotSpec(root=tmp_path))
    assert later.digest != snapshot.digest
    assert read_range(snapshot, "service.py", 6, 6).text.strip().endswith(
        "InvoiceService:"
    )


def test_a_reloaded_snapshot_serves_the_same_content(tmp_path: Path) -> None:
    snapshot = _snapshot_a(tmp_path)
    _write(tmp_path, "service.py", _SERVICE_B)
    store = SnapshotStore(store_for(tmp_path).root)
    restored = store.load(snapshot.digest)
    assert read_range(restored, "service.py", 1, 8).text == (
        read_range(snapshot, "service.py", 1, 8).text
    )
    assert verify(restored, capture(snapshot, "service.py", 6, 6)) is True


def _repository_modules() -> tuple[Path, ...]:
    package = Path(__file__).resolve().parents[2] / "src" / "wiki_ai" / "repository"
    return tuple(sorted(package.glob("*.py")))


def _receiver(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _receiver(node.func)
    return None


def _reading_calls(tree: ast.Module) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute) and target.attr in _READING_CALLS:
            if _receiver(target.value) in _SNAPSHOT_RECEIVERS:
                continue
            found.append((f"{_receiver(target.value)}.{target.attr}", node.lineno))
        elif isinstance(target, ast.Name) and target.id == "open":
            found.append((target.id, node.lineno))
    return found


def test_no_module_outside_snapshot_and_store_reads_the_working_tree() -> None:
    offenders: list[str] = []
    for path in _repository_modules():
        if path.name in _CONTENT_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, line in _reading_calls(tree):
            offenders.append(f"{path.name}:{line}: {name}")
    assert offenders == []


def test_snapshot_and_store_are_the_only_holders_of_content_reads() -> None:
    holders = {
        path.name
        for path in _repository_modules()
        if path.name in _CONTENT_MODULES and _reading_calls(ast.parse(
            path.read_text(encoding="utf-8")
        ))
    }
    assert holders == set(_CONTENT_MODULES)


def test_snapshot_read_bytes_is_the_only_content_entry_point_for_tools() -> None:
    receivers: dict[str, set[str]] = {}
    for path in _repository_modules():
        if path.name in _CONTENT_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if isinstance(target, ast.Attribute) and target.attr in _READING_CALLS:
                receivers.setdefault(path.name, set()).add(
                    f"{_receiver(target.value)}.{target.attr}"
                )
    assert receivers == {"reader.py": {"snapshot.read_bytes"}}


def test_tools_refuse_to_work_on_a_snapshot_without_a_store(tmp_path: Path) -> None:
    _repo(tmp_path)
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path))
    with pytest.raises(SnapshotNotMaterialized):
        read_range(snapshot, "service.py", 1, 1)
    with pytest.raises(SnapshotNotMaterialized):
        search(snapshot, "InvoiceService")
    with pytest.raises(SnapshotNotMaterialized):
        capture(snapshot, "service.py", 1, 1)
