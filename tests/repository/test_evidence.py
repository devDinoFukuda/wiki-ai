from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tests.repository.fixtures_repos import python_repo, snapshot_of, write
from wiki_ai.repository.evidence import EvidenceCapture, EvidenceError, capture, verify
from wiki_ai.repository.reader import PathOutsideSnapshot, RangeInvalid


def test_capture_carries_snapshot_file_and_excerpt_hashes(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    item = capture(snapshot, "billing/repository.py", 1, 2, symbol="save_invoice")
    assert item.snapshot_id == snapshot.digest
    assert item.file_sha256 == snapshot.file_map()["billing/repository.py"].sha256
    assert item.symbol == "save_invoice"
    assert item.line_start == 1
    assert item.line_end == 2
    assert "save_invoice" in item.excerpt
    assert len(item.excerpt_sha256) == 64
    assert item.locator() == "billing/repository.py:1-2#save_invoice"


def test_capture_is_deterministic(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    first = capture(snapshot, "billing/service.py", 1, 3)
    second = capture(snapshot, "billing/service.py", 1, 3)
    assert first == second
    assert first.locator() == "billing/service.py:1-3"


def test_verify_accepts_unchanged_capture(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    item = capture(snapshot, "billing/repository.py", 1, 2)
    assert verify(snapshot, item) is True


def test_verify_rejects_changed_content(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    item = capture(snapshot, "billing/repository.py", 1, 2)
    write(tmp_path, "billing/repository.py", "def save_invoice(amount):\n    return None\n")
    assert verify(snapshot_of(tmp_path), item) is False


def test_verify_rejects_tampered_excerpt(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    item = capture(snapshot, "billing/repository.py", 1, 2)
    tampered = dataclasses.replace(item, excerpt="def save_invoice(amount):\n    return 0\n")
    assert verify(snapshot, tampered) is False


def test_verify_rejects_other_snapshot(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    item = capture(snapshot, "billing/repository.py", 1, 2)
    other = dataclasses.replace(item, snapshot_id="0" * 64)
    assert verify(snapshot, other) is False


def test_capture_requires_a_path_inside_the_snapshot(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    with pytest.raises(PathOutsideSnapshot):
        capture(snapshot, "../outside.py", 1, 1)
    with pytest.raises(PathOutsideSnapshot):
        capture(snapshot, "billing/absent.py", 1, 1)


def test_capture_rejects_ranges_beyond_the_file(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    with pytest.raises(RangeInvalid):
        capture(snapshot, "billing/repository.py", 1, 999)
    with pytest.raises(RangeInvalid):
        capture(snapshot, "billing/repository.py", 2, 1)


def test_round_trip_through_dict(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    item = capture(snapshot, "billing/repository.py", 1, 2, symbol="save_invoice")
    restored = EvidenceCapture.from_dict(item.to_dict())
    assert restored == item
    assert verify(snapshot, restored) is True
    with pytest.raises(EvidenceError):
        EvidenceCapture.from_dict({"path": "billing/repository.py"})
