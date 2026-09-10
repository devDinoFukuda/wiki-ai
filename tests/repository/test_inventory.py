from __future__ import annotations

from pathlib import Path

from wiki_ai.repository.inventory import (
    Classification,
    build_inventory,
    language_hint_for,
)
from wiki_ai.repository.snapshot import SnapshotSpec, take_snapshot


def _write(root: Path, relative: str, content: str = "x\n") -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _inventory(root: Path):
    return build_inventory(take_snapshot(SnapshotSpec(root=root)))


def test_language_hint_covers_many_ecosystems() -> None:
    expected = {
        "a.py": "python",
        "A.java": "java",
        "a.ts": "typescript",
        "a.js": "javascript",
        "PROG.CBL": "cobol",
        "COPY.CPY": "cobol",
        "JOB.JCL": "jcl",
        "a.sql": "sql",
        "a.go": "go",
        "a.cs": "csharp",
        "a.kt": "kotlin",
        "a.rb": "ruby",
        "a.php": "php",
        "a.sh": "shell",
        "a.yaml": "yaml",
        "a.json": "json",
        "a.xml": "xml",
        "a.toml": "toml",
        "a.md": "markdown",
        "Dockerfile": "dockerfile",
    }
    for name, hint in expected.items():
        assert language_hint_for(name) == hint


def test_unknown_extension_is_never_unsupported() -> None:
    assert language_hint_for("a.zzz") == "unknown"


def test_build_inventory_classifies_mixed_repository(tmp_path: Path) -> None:
    _write(tmp_path, "src/app.py", "value = 1\n")
    _write(tmp_path, "src/Service.java", "class Service {}\n")
    _write(tmp_path, "cobol/PROG.CBL", "IDENTIFICATION DIVISION.\n")
    _write(tmp_path, "jcl/JOB.JCL", "//JOB1 JOB\n")
    _write(tmp_path, "db/schema.sql", "select 1;\n")
    _write(tmp_path, "tests/test_app.py", "value = 1\n")
    _write(tmp_path, "README.md", "title\n")
    _write(tmp_path, "config.yaml", "key: value\n")
    _write(tmp_path, "pyproject.toml", "[project]\n")
    _write(tmp_path, "data/rows.csv", "a,b\n")
    _write(tmp_path, "web/app.min.js", "var a=1\n")
    _write(tmp_path, "node_modules/pkg/index.js", "var a=1\n")
    _write(tmp_path, "mystery.zzz", "?\n")

    inventory = _inventory(tmp_path)
    by_path = {entry.path: entry for entry in inventory.entries}

    assert by_path["src/app.py"].classification is Classification.SOURCE
    assert by_path["src/Service.java"].classification is Classification.SOURCE
    assert by_path["cobol/PROG.CBL"].language_hint == "cobol"
    assert by_path["jcl/JOB.JCL"].language_hint == "jcl"
    assert by_path["db/schema.sql"].classification is Classification.SOURCE
    assert by_path["tests/test_app.py"].classification is Classification.TEST
    assert by_path["README.md"].classification is Classification.DOCUMENT
    assert by_path["config.yaml"].classification is Classification.CONFIG
    assert by_path["pyproject.toml"].classification is Classification.BUILD
    assert by_path["data/rows.csv"].classification is Classification.DATA
    assert by_path["web/app.min.js"].generated is True
    assert by_path["node_modules/pkg/index.js"].ignored is True
    assert by_path["mystery.zzz"].classification is Classification.UNKNOWN
    assert by_path["mystery.zzz"].language_hint == "unknown"


def test_inventory_summaries_and_analyzable_subset(tmp_path: Path) -> None:
    _write(tmp_path, "a.py")
    _write(tmp_path, "b.py")
    _write(tmp_path, "node_modules/c.js")
    inventory = _inventory(tmp_path)
    assert inventory.by_language()["python"] == 2
    assert inventory.by_classification()["source"] == 2
    assert [entry.path for entry in inventory.analyzable()] == ["a.py", "b.py"]


def test_inventory_carries_snapshot_identity(tmp_path: Path) -> None:
    _write(tmp_path, "a.py")
    snapshot = take_snapshot(SnapshotSpec(root=tmp_path))
    inventory = build_inventory(snapshot)
    assert inventory.snapshot_digest == snapshot.digest
    assert inventory.entries[0].hash == snapshot.files[0].sha256
