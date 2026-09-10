from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.repository.fixtures_repos import java_repo, mixed_repo, python_repo
from wiki_ai.repository.harness import (
    InvalidToolArguments,
    RepositoryHarness,
    ToolLimits,
    UnknownTool,
)
from wiki_ai.repository.schemas import TOOL_NAMES


def _harness(tmp_path: Path) -> RepositoryHarness:
    return RepositoryHarness(mixed_repo(tmp_path))


def test_harness_exposes_the_canonical_tool_names(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    assert harness.names() == TOOL_NAMES
    assert harness.names() == (
        "repo.inventory",
        "repo.search",
        "repo.read",
        "repo.symbol",
        "repo.references",
        "repo.dependencies",
        "repo.tests",
        "repo.config",
        "repo.history",
        "evidence.capture",
    )


def test_specs_declare_input_and_output_schemas(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    for spec in harness.specs():
        assert spec.description
        assert spec.input_schema["type"] == "object"
        assert spec.input_schema["additionalProperties"] is False
        assert isinstance(spec.input_schema["properties"], dict)
        assert spec.output_schema["type"] == "object"
        assert isinstance(spec.output_schema["properties"], dict)
        json.dumps(spec.to_dict())


def test_every_result_is_json_serialisable(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    calls = {
        "repo.inventory": {},
        "repo.search": {"pattern": "Order"},
        "repo.read": {"path": "ledger/post.go", "start": 1, "end": 3},
        "repo.symbol": {"path": "ledger/post.go"},
        "repo.references": {"symbol_name": "Post"},
        "repo.dependencies": {},
        "repo.tests": {"path_or_symbol": "Post"},
        "repo.config": {"key_or_usage": "INVOICE_TOPIC"},
        "repo.history": {},
        "evidence.capture": {"path": "ledger/post.go", "line_start": 1, "line_end": 2},
    }
    for name, arguments in calls.items():
        payload = harness.invoke(name, arguments)
        assert payload["snapshot_id"] == harness.snapshot.digest
        json.dumps(payload)


def test_unknown_tool_is_rejected(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    with pytest.raises(UnknownTool):
        harness.invoke("repo.write", {})
    with pytest.raises(UnknownTool):
        harness.spec_for("shell.exec")


def test_unknown_argument_is_rejected(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.search", {"pattern": "Order", "shell": "rm -rf"})


def test_missing_required_argument_is_rejected(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.search", {})
    with pytest.raises(InvalidToolArguments):
        harness.invoke("evidence.capture", {"path": "ledger/post.go"})


def test_argument_types_are_enforced(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.search", {"pattern": 7})
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.search", {"pattern": "Order", "regex": "yes"})
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.search", {"pattern": "Order", "globs": [1, 2]})
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.read", {"path": "ledger/post.go", "start": 0})


def test_limits_cap_requested_values(tmp_path: Path) -> None:
    harness = RepositoryHarness(mixed_repo(tmp_path), ToolLimits(max_results=2, max_results_ceiling=5))
    assert len(harness.invoke("repo.search", {"pattern": "Order"})["matches"]) <= 2
    assert len(harness.invoke("repo.search", {"pattern": "Order", "max_results": 5})["matches"]) <= 5
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.search", {"pattern": "Order", "max_results": 9999})


def test_enum_like_arguments_are_validated(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.inventory", {"classification": "invented"})
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.symbol", {"kinds": ["invented"]})
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.dependencies", {"scope": "invented"})


def test_inventory_filters_and_counts(tmp_path: Path) -> None:
    harness = RepositoryHarness(java_repo(tmp_path))
    payload = harness.invoke("repo.inventory", {"classification": "test"})
    assert [entry["path"] for entry in payload["entries"]] == [
        "src/test/java/com/acme/order/OrderServiceTest.java"
    ]
    assert payload["by_language"]["java"] == 5
    scoped = harness.invoke("repo.inventory", {"path_prefix": "src/main/resources"})
    assert [entry["path"] for entry in scoped["entries"]] == [
        "src/main/resources/application.yml"
    ]


def test_read_reports_truncation_under_a_byte_budget(tmp_path: Path) -> None:
    harness = RepositoryHarness(python_repo(tmp_path), ToolLimits(max_bytes=8))
    payload = harness.invoke("repo.read", {"path": "billing/service.py"})
    assert payload["truncated"] is True
    assert len(payload["text"].encode("utf-8")) <= 8
    full = harness.invoke("repo.read", {"path": "billing/service.py", "max_bytes": 65536})
    assert full["truncated"] is False


def test_invalid_regex_becomes_a_typed_argument_error(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.search", {"pattern": "(", "regex": True})
    with pytest.raises(InvalidToolArguments):
        harness.invoke("repo.references", {"symbol_name": "not a name"})


def test_history_reports_absence_without_raising(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    payload = harness.invoke("repo.history", {})
    assert payload["available"] is False
    assert payload["reason"] == "not_a_git_repository"
