from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WHEEL_BUILD_UNAVAILABLE = "wheel_build_unavailable"
_BUILD_TIMEOUT = 900
_RUN_TIMEOUT = 300

_PROBE = """
import json
import wiki_ai
from wiki_ai.knowledge.grounding import relation_predicate_terms
from wiki_ai.quality import vocabulary

terms = relation_predicate_terms("calls")
assert terms, "relation_predicate_terms('calls') is empty"
loaded = vocabulary.load()
assert loaded, "vocabulary.load() is empty"
print(
    json.dumps(
        {
            "version": wiki_ai.__version__,
            "package_dir": wiki_ai.__file__,
            "calls_terms": len(terms),
            "vocabulary_keys": len(loaded),
        }
    )
)
"""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run(command: list[str], cwd: Path | None = None, timeout: int = _RUN_TIMEOUT):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    return subprocess.run(
        command,
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )


def _detail(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stdout + completed.stderr).strip()[-4000:]


def _build_wheel(destination: Path) -> Path:
    try:
        completed = _run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                ".",
                "--no-deps",
                "-w",
                str(destination),
            ],
            cwd=_project_root(),
            timeout=_BUILD_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"{WHEEL_BUILD_UNAVAILABLE}: {exc}")
    if completed.returncode != 0:
        pytest.skip(f"{WHEEL_BUILD_UNAVAILABLE}: {_detail(completed)}")
    wheels = sorted(destination.glob("wiki_ai-*.whl"))
    if not wheels:
        pytest.skip(
            f"{WHEEL_BUILD_UNAVAILABLE}: no wheel produced; {_detail(completed)}"
        )
    return wheels[0]


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _console_script(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "wiki-ai.exe"
    return venv / "bin" / "wiki-ai"


def _install(venv: Path, wheel: Path) -> Path:
    completed = _run([sys.executable, "-m", "venv", str(venv)], timeout=_BUILD_TIMEOUT)
    if completed.returncode != 0:
        pytest.skip(f"{WHEEL_BUILD_UNAVAILABLE}: venv failed; {_detail(completed)}")
    python = _venv_python(venv)
    if not python.is_file():
        pytest.skip(f"{WHEEL_BUILD_UNAVAILABLE}: no interpreter at {python}")
    completed = _run(
        [str(python), "-m", "pip", "install", "--no-index", str(wheel)],
        timeout=_BUILD_TIMEOUT,
    )
    if completed.returncode != 0:
        pytest.skip(f"{WHEEL_BUILD_UNAVAILABLE}: install failed; {_detail(completed)}")
    return python


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    base = tmp_path_factory.mktemp("wheel")
    wheel = _build_wheel(base / "dist")
    venv = base / "venv"
    python = _install(venv, wheel)
    return wheel, python, _console_script(venv)


def test_the_wheel_carries_the_runtime_data_files(
    installed: tuple[Path, Path, Path]
) -> None:
    import zipfile

    wheel, _, _ = installed
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "wiki_ai/knowledge/relation_predicates.json" in names
    assert "wiki_ai/quality/vocabulary.json" in names


def test_the_installed_package_loads_its_data_files(
    installed: tuple[Path, Path, Path]
) -> None:
    _, python, _ = installed
    completed = _run([str(python), "-c", _PROBE])
    assert completed.returncode == 0, _detail(completed)
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["calls_terms"] > 0
    assert payload["vocabulary_keys"] > 0
    assert "site-packages" in payload["package_dir"].replace("\\", "/")


def test_the_console_script_reports_the_version(
    installed: tuple[Path, Path, Path]
) -> None:
    _, _, script = installed
    assert script.is_file(), f"console script missing at {script}"
    completed = _run([str(script), "version"])
    assert completed.returncode == 0, _detail(completed)
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["status"] == "ok"
    assert payload["command"] == "version"
    assert payload["version"]


def test_the_console_script_reports_status_of_a_minimal_repository(
    installed: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, _, script = installed
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("title\n", encoding="utf-8")
    completed = _run([str(script), "status", "--repo", str(repo)])
    assert completed.returncode == 0, _detail(completed)
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["status"] == "ok"
    assert payload["command"] == "status"
    assert payload["entities"] == 0
    assert payload["analysis_status"] == "never"
    assert len(payload["observed_digest"]) == 64
