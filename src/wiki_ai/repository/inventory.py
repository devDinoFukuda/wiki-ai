from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from wiki_ai.repository.snapshot import FileRecord, RepositorySnapshot

__all__ = [
    "Classification",
    "InventoryEntry",
    "Inventory",
    "UNKNOWN_LANGUAGE",
    "language_hint_for",
    "build_inventory",
]


class Classification(Enum):
    SOURCE = "source"
    TEST = "test"
    CONFIG = "config"
    DOCUMENT = "document"
    BUILD = "build"
    DATA = "data"
    GENERATED = "generated"
    UNKNOWN = "unknown"


UNKNOWN_LANGUAGE = "unknown"

_LANGUAGE_BY_EXTENSION: Mapping[str, str] = {
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".java": "java",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".sc": "scala",
    ".groovy": "groovy", ".gvy": "groovy",
    ".sql": "sql", ".ddl": "sql", ".dml": "sql", ".prc": "sql", ".sp": "sql",
    ".trg": "sql", ".vw": "sql", ".viw": "sql", ".fnc": "sql", ".pks": "sql",
    ".pkb": "sql", ".pls": "sql", ".plsql": "sql", ".tsql": "sql",
    ".cbl": "cobol", ".cob": "cobol", ".cobol": "cobol", ".cpy": "cobol", ".ccp": "cobol",
    ".jcl": "jcl", ".proc": "jcl", ".prm": "jcl",
    ".pli": "pli", ".pl1": "pli",
    ".asm": "assembler", ".mac": "assembler",
    ".rexx": "rexx", ".rex": "rexx",
    ".abap": "abap",
    ".nsp": "natural", ".nsn": "natural",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hxx": "cpp",
    ".hh": "cpp", ".ipp": "cpp",
    ".rs": "rust",
    ".go": "go",
    ".cs": "csharp", ".csx": "csharp",
    ".vb": "vbnet", ".bas": "vbnet", ".cls": "vbnet", ".frm": "vbnet", ".vbs": "vbnet",
    ".fs": "fsharp", ".fsx": "fsharp", ".fsi": "fsharp",
    ".pas": "pascal", ".dpr": "pascal", ".dfm": "pascal", ".pp": "pascal",
    ".lpr": "pascal", ".dpk": "pascal",
    ".ada": "ada", ".adb": "ada", ".ads": "ada",
    ".f": "fortran", ".f77": "fortran", ".f90": "fortran", ".f95": "fortran",
    ".for": "fortran",
    ".swift": "swift",
    ".m": "objc", ".mm": "objc",
    ".dart": "dart",
    ".php": "php", ".phtml": "php", ".php5": "php",
    ".rb": "ruby", ".rake": "ruby", ".gemspec": "ruby", ".erb": "ruby",
    ".pl": "perl", ".pm": "perl",
    ".lua": "lua",
    ".r": "r",
    ".jl": "julia",
    ".tcl": "tcl",
    ".hs": "haskell", ".lhs": "haskell",
    ".ex": "elixir", ".exs": "elixir",
    ".erl": "erlang", ".hrl": "erlang",
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure",
    ".ml": "ocaml", ".mli": "ocaml",
    ".lisp": "lisp", ".el": "lisp",
    ".scm": "scheme", ".ss": "scheme",
    ".nim": "nim",
    ".zig": "zig",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ksh": "shell", ".csh": "shell",
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
    ".bat": "batch", ".cmd": "batch",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "css", ".less": "css",
    ".vue": "vue",
    ".svelte": "svelte",
    ".tf": "terraform", ".tfvars": "terraform",
    ".proto": "protobuf",
    ".graphql": "graphql", ".gql": "graphql",
    ".sol": "solidity",
    ".mk": "make",
    ".gradle": "gradle",
    ".json": "json", ".jsonc": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".xml": "xml", ".xsd": "xml", ".xsl": "xml", ".wsdl": "xml",
    ".toml": "toml",
    ".ini": "ini", ".cfg": "ini", ".conf": "ini", ".properties": "ini",
    ".md": "markdown", ".markdown": "markdown",
    ".rst": "restructuredtext",
    ".adoc": "asciidoc",
    ".txt": "text", ".rtf": "text",
    ".csv": "csv", ".tsv": "tsv",
    ".jsonl": "jsonl", ".ndjson": "jsonl",
    ".parquet": "parquet", ".avro": "avro",
    ".db": "sqlite", ".sqlite": "sqlite", ".sqlite3": "sqlite",
}

_LANGUAGE_BY_FILENAME: Mapping[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "make",
    "gnumakefile": "make",
    "rakefile": "ruby",
    "gemfile": "ruby",
}

_DOCUMENT_LANGUAGES = frozenset({"markdown", "restructuredtext", "asciidoc", "text"})
_DATA_LANGUAGES = frozenset({"csv", "tsv", "jsonl", "parquet", "avro", "sqlite"})
_CONFIG_LANGUAGES = frozenset({"yaml", "json", "xml", "toml", "ini"})

_IGNORED_SEGMENTS = frozenset({
    "node_modules", "__pycache__", "dist", "build", "target", "out",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache", ".gradle",
    ".idea", "vendor", "bower_components", "site-packages", ".next",
})

_GENERATED_SUFFIXES = (
    ".min.js", ".min.css", ".map", ".pyc", ".pyo", ".class",
    ".pb.go", ".pb.cc", ".pb.h", ".g.dart", "_pb2.py",
)

_GENERATED_FILENAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "pipfile.lock", "cargo.lock", "composer.lock", "go.sum",
})

_BUILD_FILENAMES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "package.json", "pom.xml",
    "build.gradle", "build.gradle.kts", "settings.gradle", "cargo.toml",
    "go.mod", "makefile", "gnumakefile", "dockerfile", "cmakelists.txt",
    "composer.json", "mix.exs", "requirements.txt", "pipfile", "build.xml",
    "meson.build", "rakefile", "gemfile", "wrangler.toml",
})

_BUILD_SUFFIXES = (".csproj", ".vbproj", ".fsproj", ".sln")

_CONFIG_FILENAMES = frozenset({
    ".gitignore", ".gitattributes", ".dockerignore", ".editorconfig",
    ".npmrc", ".env", ".env.example", ".flake8",
})

_DOCUMENT_FILENAMES = frozenset({
    "license", "licence", "copying", "notice", "authors", "contributors",
    "changelog", "changes", "readme", "codeowners",
})

_TEST_SEGMENTS = frozenset({"test", "tests", "spec", "specs", "testing", "__tests__"})

_TEST_PREFIXES = ("test_", "test-")

_TEST_SUFFIXES = (
    "_test.py", "_test.go", "_test.rb", "test.java", "tests.java",
    ".test.js", ".test.ts", ".test.jsx", ".test.tsx",
    ".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx",
    "_spec.rb", "test.cs", "tests.cs",
)


def _extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def language_hint_for(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if name in _LANGUAGE_BY_FILENAME:
        return _LANGUAGE_BY_FILENAME[name]
    return _LANGUAGE_BY_EXTENSION.get(_extension(name), UNKNOWN_LANGUAGE)


def _is_ignored(path: str) -> bool:
    return any(segment in _IGNORED_SEGMENTS for segment in path.split("/")[:-1])


def _is_generated(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if name in _GENERATED_FILENAMES:
        return True
    return name.endswith(_GENERATED_SUFFIXES)


def _is_build(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name in _BUILD_FILENAMES or name.endswith(_BUILD_SUFFIXES)


def _is_test(path: str) -> bool:
    segments = path.split("/")
    lowered = segments[-1].lower()
    if lowered.startswith(_TEST_PREFIXES) or lowered.endswith(_TEST_SUFFIXES):
        return True
    if lowered == "conftest.py":
        return True
    return any(segment.lower() in _TEST_SEGMENTS for segment in segments[:-1])


def _classify(path: str, language: str) -> Classification:
    if _is_ignored(path) or _is_generated(path):
        return Classification.GENERATED
    if _is_build(path):
        return Classification.BUILD
    if _is_test(path):
        return Classification.TEST
    if language in _DOCUMENT_LANGUAGES:
        return Classification.DOCUMENT
    if language in _DATA_LANGUAGES:
        return Classification.DATA
    name = path.rsplit("/", 1)[-1].lower()
    if name in _CONFIG_FILENAMES or language in _CONFIG_LANGUAGES:
        return Classification.CONFIG
    if language != UNKNOWN_LANGUAGE:
        return Classification.SOURCE
    if name in _DOCUMENT_FILENAMES:
        return Classification.DOCUMENT
    return Classification.UNKNOWN


@dataclass(frozen=True)
class InventoryEntry:
    path: str
    size: int
    language_hint: str
    classification: Classification
    generated: bool
    ignored: bool
    hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "language_hint": self.language_hint,
            "classification": self.classification.value,
            "generated": self.generated,
            "ignored": self.ignored,
            "hash": self.hash,
        }


@dataclass(frozen=True)
class Inventory:
    snapshot_digest: str
    entries: tuple[InventoryEntry, ...]

    def by_classification(self) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            key = entry.classification.value
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    def by_language(self) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.language_hint] = counts.get(entry.language_hint, 0) + 1
        return dict(sorted(counts.items()))

    def analyzable(self) -> tuple[InventoryEntry, ...]:
        return tuple(
            entry for entry in self.entries if not (entry.ignored or entry.generated)
        )


def _entry_for(record: FileRecord) -> InventoryEntry:
    language = language_hint_for(record.path)
    return InventoryEntry(
        path=record.path,
        size=record.size,
        language_hint=language,
        classification=_classify(record.path, language),
        generated=_is_generated(record.path),
        ignored=_is_ignored(record.path),
        hash=record.sha256,
    )


def build_inventory(snapshot: RepositorySnapshot) -> Inventory:
    entries = tuple(_entry_for(record) for record in snapshot.files)
    return Inventory(snapshot_digest=snapshot.digest, entries=entries)
