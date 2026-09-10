from __future__ import annotations

import tomllib
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _source_root() -> Path:
    return _project_root() / "src"


def _data_files() -> tuple[Path, ...]:
    root = _source_root() / "wiki_ai"
    found = [
        item
        for item in root.rglob("*")
        if item.is_file()
        and item.suffix != ".py"
        and "__pycache__" not in item.parts
    ]
    return tuple(sorted(found))


def _package_data() -> dict[str, list[str]]:
    payload = tomllib.loads(
        (_project_root() / "pyproject.toml").read_text(encoding="utf-8")
    )
    section = payload["tool"]["setuptools"]["package-data"]
    return {str(key): [str(item) for item in value] for key, value in section.items()}


def _package_of(path: Path) -> str:
    relative = path.relative_to(_source_root())
    return ".".join(relative.parent.parts)


def test_every_non_python_file_under_the_source_tree_is_declared_package_data() -> None:
    declared = _package_data()
    files = _data_files()
    assert files
    for item in files:
        package = _package_of(item)
        patterns = declared.get(package, []) + declared.get("*", [])
        assert patterns, f"{item} has no package-data entry for {package}"
        assert any(
            Path(item.name).match(pattern) for pattern in patterns
        ), f"{item.name} is not matched by {patterns}"


def test_every_package_data_entry_points_at_a_file_that_exists() -> None:
    root = _source_root()
    for package, patterns in _package_data().items():
        if package == "*":
            continue
        directory = root / Path(*package.split("."))
        assert directory.is_dir(), f"{package} is not a package directory"
        for pattern in patterns:
            assert list(directory.glob(pattern)), f"{package}:{pattern} matches nothing"


def test_the_declared_data_files_are_the_ones_opened_at_runtime() -> None:
    names = {item.relative_to(_source_root()).as_posix() for item in _data_files()}
    assert "wiki_ai/knowledge/relation_predicates.json" in names
    assert "wiki_ai/quality/vocabulary.json" in names


def test_every_declared_package_is_found_by_the_package_discovery() -> None:
    payload = tomllib.loads(
        (_project_root() / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert payload["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]
    root = _source_root()
    for package in _package_data():
        if package == "*":
            continue
        assert (root / Path(*package.split(".")) / "__init__.py").is_file()
