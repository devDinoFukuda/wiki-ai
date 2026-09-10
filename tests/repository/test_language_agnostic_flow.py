from __future__ import annotations

from pathlib import Path

import pytest

from tests.repository.fixtures_repos import (
    java_repo,
    mainframe_repo,
    mixed_repo,
    python_repo,
    snapshot_of,
    sql_repo,
    web_repo,
    write,
)
from wiki_ai.repository.harness import RepositoryHarness

_SCENARIOS = {
    "java": ("src/main/java/com/acme/order/OrderService.java", "OrderService", "java"),
    "python": ("billing/service.py", "InvoiceService", "python"),
    "web": ("src/client.ts", "submitCart", "typescript"),
    "cobol": ("cobol/PAYRUN.cbl", "PAYRUN", "cobol"),
    "jcl": ("jcl/PAYJOB.jcl", "STEP01", "jcl"),
    "sql": ("db/schema.sql", "PAYROLL_MASTER", "sql"),
}

_BUILDERS = {
    "java": java_repo,
    "python": python_repo,
    "web": web_repo,
    "cobol": mainframe_repo,
    "jcl": mainframe_repo,
    "sql": sql_repo,
}


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_inventory_search_read_symbol_references_without_a_parser(
    tmp_path: Path, scenario: str
) -> None:
    path, symbol, language = _SCENARIOS[scenario]
    harness = RepositoryHarness(_BUILDERS[scenario](tmp_path))

    inventory = harness.invoke("repo.inventory", {})
    listed = {entry["path"]: entry for entry in inventory["entries"]}
    assert path in listed
    assert listed[path]["language_hint"] == language
    assert listed[path]["hash"]

    found = harness.invoke("repo.search", {"pattern": symbol})
    assert path in found["paths"]
    line = next(match["line"] for match in found["matches"] if match["path"] == path)

    read = harness.invoke("repo.read", {"path": path, "start": line, "end": line})
    assert symbol in read["text"]

    symbols = harness.invoke("repo.symbol", {"path": path})["symbols"]
    assert any(item["name"] == symbol for item in symbols)

    references = harness.invoke("repo.references", {"symbol_name": symbol})
    assert references["total"] >= 0
    for item in references["references"]:
        assert item["path"] in listed or item["path"]

    evidence = harness.invoke(
        "evidence.capture",
        {"path": path, "line_start": line, "line_end": line, "symbol": symbol},
    )
    assert evidence["snapshot_id"] == harness.snapshot.digest
    assert evidence["file_sha256"] == listed[path]["hash"]


def test_additional_reads_can_be_requested_dynamically(tmp_path: Path) -> None:
    harness = RepositoryHarness(mixed_repo(tmp_path))
    entry_points = harness.invoke("repo.search", {"pattern": "PAYRUN"})
    assert entry_points["paths"]
    widened: list[str] = []
    for path in entry_points["paths"]:
        total = harness.invoke("repo.read", {"path": path, "start": 1, "end": 1})["total_lines"]
        payload = harness.invoke("repo.read", {"path": path, "start": 1, "end": total})
        widened.append(payload["text"])
        assert payload["end"] == total
    assert any("PROGRAM-ID" in text for text in widened)
    assert any("EXEC PGM=" in text for text in widened)


def test_unknown_language_never_blocks_the_flow(tmp_path: Path) -> None:
    write(tmp_path, "edge/payroll.zzz", "widget PayrollWidget = {\n  compute_total(hours)\n}\n")
    harness = RepositoryHarness(snapshot_of(tmp_path))
    entry = harness.invoke("repo.inventory", {})["entries"][0]
    assert entry["language_hint"] == "unknown"
    assert harness.invoke("repo.search", {"pattern": "PayrollWidget"})["paths"] == (
        ["edge/payroll.zzz"]
    )
    assert harness.invoke("repo.read", {"path": "edge/payroll.zzz"})["total_lines"] == 3
    symbols = harness.invoke("repo.symbol", {"path": "edge/payroll.zzz"})["symbols"]
    assert {item["confidence"] for item in symbols} == {"heuristic"}
    assert harness.invoke("repo.references", {"symbol_name": "PayrollWidget"})["total"] >= 0
    assert harness.invoke("repo.dependencies", {})["imports"] == []


def test_mixed_repository_serves_every_tool(tmp_path: Path) -> None:
    harness = RepositoryHarness(mixed_repo(tmp_path))
    languages = set(harness.invoke("repo.inventory", {})["by_language"])
    assert {"java", "python", "typescript", "javascript", "cobol", "jcl", "sql", "go"} <= languages
    assert harness.invoke("repo.dependencies", {})["hosts"]
    assert harness.invoke("repo.tests", {"path_or_symbol": "InvoiceService"})["total"] >= 1
    assert harness.invoke("repo.config", {"key_or_usage": "order.topic"})["total"] >= 1
    assert harness.invoke("repo.symbol", {"name": "PAYRUN"})["total"] >= 1
