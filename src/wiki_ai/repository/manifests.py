from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = [
    "ManifestEntry",
    "MANIFEST_SUFFIXES",
    "is_manifest",
    "manifest_entries",
]


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    line: int
    manifest: str
    name: str
    version: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "manifest": self.manifest,
            "name": self.name,
            "version": self.version,
        }



_MANIFEST_NAMES = frozenset(
    {
        "pyproject.toml", "requirements.txt", "requirements-dev.txt", "package.json",
        "pom.xml", "build.gradle", "build.gradle.kts", "go.mod", "gemfile",
        "composer.json", "pipfile", "cargo.toml",
    }
)

MANIFEST_SUFFIXES = (".csproj", ".vbproj", ".fsproj")

_REQUIREMENT_LINE = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._\-\[\]]*)\s*(?P<version>[<>=!~^].*)?$")
_MAVEN_DEPENDENCY = re.compile(
    r"<dependency>(?P<body>.*?)</dependency>", re.IGNORECASE | re.DOTALL
)
_MAVEN_FIELD = re.compile(r"<(?P<tag>groupId|artifactId|version)>(?P<value>[^<]*)</(?P=tag)>", re.IGNORECASE)
_GRADLE_DEPENDENCY = re.compile(
    r"""(?:implementation|api|compile|testImplementation|runtimeOnly|compileOnly|annotationProcessor)"""
    r"""\s*[\(\s]\s*['"](?P<coordinate>[^'"]+)['"]""",
)
_GOMOD_REQUIRE = re.compile(r"^\s*(?P<name>[\w./\-]+)\s+(?P<version>v[\w.\-+]+)")
_GEMFILE_GEM = re.compile(r"""^\s*gem\s+['"](?P<name>[^'"]+)['"](?:\s*,\s*['"](?P<version>[^'"]+)['"])?""")
_CSPROJ_PACKAGE = re.compile(
    r"""<PackageReference\s+Include\s*=\s*"(?P<name>[^"]+)"(?:[^>]*?Version\s*=\s*"(?P<version>[^"]*)")?""",
    re.IGNORECASE,
)


def _line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    return 1


def _pyproject_entries(path: str, text: str) -> list[ManifestEntry]:
    try:
        payload = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    names: list[str] = []
    project = payload.get("project")
    if isinstance(project, Mapping):
        for item in project.get("dependencies") or ():
            if isinstance(item, str):
                names.append(item)
        optional = project.get("optional-dependencies")
        if isinstance(optional, Mapping):
            for group in optional.values():
                for item in group or ():
                    if isinstance(item, str):
                        names.append(item)
    entries: list[ManifestEntry] = []
    for raw in names:
        match = _REQUIREMENT_LINE.match(raw)
        if match is None:
            continue
        entries.append(
            ManifestEntry(
                path=path,
                line=_line_of(text, raw),
                manifest="pyproject.toml",
                name=match.group("name"),
                version=(match.group("version") or "").strip() or None,
            )
        )
    return entries


def _requirements_entries(path: str, text: str) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.split("#", 1)[0].strip()
        if not stripped or stripped.startswith("-"):
            continue
        match = _REQUIREMENT_LINE.match(stripped)
        if match is None:
            continue
        entries.append(
            ManifestEntry(
                path=path,
                line=number,
                manifest="requirements",
                name=match.group("name"),
                version=(match.group("version") or "").strip() or None,
            )
        )
    return entries


def _json_manifest_entries(
    path: str, text: str, manifest: str, sections: Sequence[str]
) -> list[ManifestEntry]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(payload, Mapping):
        return []
    entries: list[ManifestEntry] = []
    for section in sections:
        block = payload.get(section)
        if not isinstance(block, Mapping):
            continue
        for name, version in block.items():
            entries.append(
                ManifestEntry(
                    path=path,
                    line=_line_of(text, f'"{name}"'),
                    manifest=manifest,
                    name=str(name),
                    version=str(version) if version is not None else None,
                )
            )
    return entries


def _maven_entries(path: str, text: str) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    for block in _MAVEN_DEPENDENCY.finditer(text):
        fields = {
            item.group("tag").lower(): item.group("value").strip()
            for item in _MAVEN_FIELD.finditer(block.group("body"))
        }
        artifact = fields.get("artifactid")
        if not artifact:
            continue
        group = fields.get("groupid", "")
        name = f"{group}:{artifact}" if group else artifact
        line = text.count("\n", 0, block.start()) + 1
        entries.append(
            ManifestEntry(
                path=path,
                line=line,
                manifest="pom.xml",
                name=name,
                version=fields.get("version") or None,
            )
        )
    return entries


def _gradle_entries(path: str, text: str) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _GRADLE_DEPENDENCY.finditer(line):
            coordinate = match.group("coordinate")
            parts = coordinate.split(":")
            name = ":".join(parts[:2]) if len(parts) >= 2 else coordinate
            version = parts[2] if len(parts) >= 3 else None
            entries.append(
                ManifestEntry(
                    path=path, line=number, manifest="build.gradle", name=name, version=version
                )
            )
    return entries


def _gomod_entries(path: str, text: str) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("module ", "go ", "require (", ")")) or not stripped:
            continue
        candidate = stripped[len("require ") :] if stripped.startswith("require ") else stripped
        match = _GOMOD_REQUIRE.match(candidate)
        if match is None:
            continue
        entries.append(
            ManifestEntry(
                path=path,
                line=number,
                manifest="go.mod",
                name=match.group("name"),
                version=match.group("version"),
            )
        )
    return entries


def _gemfile_entries(path: str, text: str) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _GEMFILE_GEM.match(line)
        if match is None:
            continue
        entries.append(
            ManifestEntry(
                path=path,
                line=number,
                manifest="Gemfile",
                name=match.group("name"),
                version=match.group("version"),
            )
        )
    return entries


def _csproj_entries(path: str, text: str) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _CSPROJ_PACKAGE.finditer(line):
            entries.append(
                ManifestEntry(
                    path=path,
                    line=number,
                    manifest="csproj",
                    name=match.group("name"),
                    version=match.group("version"),
                )
            )
    return entries


def manifest_entries(path: str, text: str) -> list[ManifestEntry]:
    name = path.rsplit("/", 1)[-1].lower()
    if name == "pyproject.toml":
        return _pyproject_entries(path, text)
    if name.startswith("requirements") and name.endswith(".txt"):
        return _requirements_entries(path, text)
    if name == "package.json":
        return _json_manifest_entries(
            path,
            text,
            "package.json",
            ("dependencies", "devDependencies", "peerDependencies"),
        )
    if name == "composer.json":
        return _json_manifest_entries(
            path, text, "composer.json", ("require", "require-dev")
        )
    if name == "pom.xml":
        return _maven_entries(path, text)
    if name in {"build.gradle", "build.gradle.kts"}:
        return _gradle_entries(path, text)
    if name == "go.mod":
        return _gomod_entries(path, text)
    if name == "gemfile":
        return _gemfile_entries(path, text)
    if name.endswith(MANIFEST_SUFFIXES):
        return _csproj_entries(path, text)
    return []


def is_manifest(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if name in _MANIFEST_NAMES or name.endswith(MANIFEST_SUFFIXES):
        return True
    return name.startswith("requirements") and name.endswith(".txt")
