from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.benchmark import conformance, runner


def _no_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)


def _all_binaries(monkeypatch: pytest.MonkeyPatch, fake: str) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda name: fake)


def test_missing_binaries_lists_every_absent_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_binaries(monkeypatch)
    assert runner.missing_binaries(("claude", "codex")) == ("claude", "codex")


def test_missing_binaries_is_empty_when_every_binary_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _all_binaries(monkeypatch, str(tmp_path / "fake"))
    assert runner.missing_binaries(("claude", "codex")) == ()


def test_run_reports_skipped_without_any_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_binaries(monkeypatch)
    payload = runner.run("all", "all")
    assert payload["status"] == "skipped"
    assert payload["reason"] == runner.SKIP_REASON
    assert payload["missing"] == ["claude", "codex"]


def test_main_exits_with_three_and_prints_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_binaries(monkeypatch)
    stream = io.StringIO()
    target = tmp_path / "out" / "benchmark.json"
    code = runner.main(["--provider", "all", "--repo", "java", "--out", str(target)], stream)
    assert code == runner.EXIT_SKIPPED
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {
        "status": "skipped",
        "reason": runner.SKIP_REASON,
        "providers": ["claude", "codex"],
        "missing": ["claude", "codex"],
    }
    assert runner.SKIP_REASON in stream.getvalue()


def test_main_skip_is_explicit_for_a_single_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_binaries(monkeypatch)
    stream = io.StringIO()
    code = runner.main(["--provider", "codex", "--repo", "cobol"], stream)
    assert code == runner.EXIT_SKIPPED
    printed = stream.getvalue()
    payload = json.loads(printed[: printed.index("\nskipped:")])
    assert payload["missing"] == ["codex"]
    assert payload["providers"] == ["codex"]


def test_render_table_states_the_skip_reason() -> None:
    text = runner.render_table(
        {"status": "skipped", "reason": runner.SKIP_REASON, "missing": ["claude"]}
    )
    assert runner.SKIP_REASON in text
    assert "claude" in text


def test_conformance_run_reports_skipped_without_binaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_binaries(monkeypatch)
    payload = conformance.run("java")
    assert payload["status"] == "skipped"
    assert payload["reason"] == runner.SKIP_REASON
    assert payload["missing"] == ["claude", "codex"]


def test_conformance_main_exits_with_three(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_binaries(monkeypatch)
    stream = io.StringIO()
    target = tmp_path / "conformance.json"
    code = conformance.main(["--repo", "all", "--out", str(target)], stream)
    assert code == runner.EXIT_SKIPPED
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["status"] == "skipped"
    assert payload["providers"] == ["claude", "codex"]


def test_conformance_skips_when_only_one_binary_is_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = str(tmp_path / "claude")
    monkeypatch.setattr(
        runner.shutil, "which", lambda name: binary if name == "claude" else None
    )
    payload = conformance.run("java")
    assert payload["status"] == "skipped"
    assert payload["missing"] == ["codex"]


def test_provider_and_repo_choices_are_explicit() -> None:
    assert runner.PROVIDER_CHOICES == ("claude", "codex", "all")
    assert set(runner.REPO_CHOICES) == {"cobol", "java", "python", "all"}
    assert runner.EXIT_SKIPPED == 3


def test_skip_never_uses_the_ok_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_binaries(monkeypatch)
    stream = io.StringIO()
    assert runner.main(["--repo", "python"], stream) != runner.EXIT_OK


def test_which_is_the_only_availability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def _record(name: str) -> None:
        seen.append(name)
        return None

    monkeypatch.setattr(runner.shutil, "which", _record)
    runner.run("all", "java")
    assert seen == ["claude", "codex"]
