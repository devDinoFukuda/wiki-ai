from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from wiki_ai.repository.inventory import language_hint_for
from wiki_ai.repository.manifests import ManifestEntry, is_manifest, manifest_entries
from wiki_ai.repository.reader import resolve_within
from wiki_ai.repository.snapshot import RepositorySnapshot

__all__ = [
    "DependencyScope",
    "ImportEdge",
    "ManifestEntry",
    "ExternalEndpoint",
    "MessagingHint",
    "DependencyReport",
    "DEFAULT_MAX_FILE_BYTES",
    "detect_dependencies",
]

DEFAULT_MAX_FILE_BYTES = 1048576


class DependencyScope(Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ImportEdge:
    path: str
    line: int
    target: str
    scope: DependencyScope
    language_hint: str
    mechanism: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "target": self.target,
            "scope": self.scope.value,
            "language_hint": self.language_hint,
            "mechanism": self.mechanism,
        }


@dataclass(frozen=True)
class ExternalEndpoint:
    path: str
    line: int
    url: str
    host: str
    scheme: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "url": self.url,
            "host": self.host,
            "scheme": self.scheme,
        }


@dataclass(frozen=True)
class MessagingHint:
    path: str
    line: int
    technology: str
    excerpt: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "technology": self.technology,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class DependencyReport:
    snapshot_digest: str
    imports: tuple[ImportEdge, ...]
    manifests: tuple[ManifestEntry, ...]
    endpoints: tuple[ExternalEndpoint, ...]
    messaging: tuple[MessagingHint, ...]

    def internal_imports(self) -> tuple[ImportEdge, ...]:
        return tuple(e for e in self.imports if e.scope is DependencyScope.INTERNAL)

    def external_imports(self) -> tuple[ImportEdge, ...]:
        return tuple(e for e in self.imports if e.scope is DependencyScope.EXTERNAL)

    def hosts(self) -> tuple[str, ...]:
        return tuple(sorted({endpoint.host for endpoint in self.endpoints}))

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_digest": self.snapshot_digest,
            "imports": [item.to_dict() for item in self.imports],
            "manifests": [item.to_dict() for item in self.manifests],
            "endpoints": [item.to_dict() for item in self.endpoints],
            "messaging": [item.to_dict() for item in self.messaging],
        }


_PYTHON_IMPORT = re.compile(r"^\s*(?:from\s+(?P<from>[\w.]+)\s+import|import\s+(?P<plain>[\w.,\s]+))")
_JVM_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?(?P<target>[\w.$]+(?:\.\*)?)\s*;")
_KOTLIN_IMPORT = re.compile(r"^\s*import\s+(?P<target>[\w.$]+(?:\.\*)?)\s*$")
_CSHARP_USING = re.compile(r"^\s*using\s+(?:static\s+)?(?P<target>[\w.]+)\s*;")
_JS_IMPORT = re.compile(r"""^\s*(?:import|export)\b[^'"]*?from\s*['"](?P<target>[^'"]+)['"]""")
_JS_BARE_IMPORT = re.compile(r"""^\s*import\s*['"](?P<target>[^'"]+)['"]""")
_JS_REQUIRE = re.compile(r"""require\s*\(\s*['"](?P<target>[^'"]+)['"]\s*\)""")
_GO_IMPORT = re.compile(r"""^\s*(?:import\s+)?(?:[\w.]+\s+)?"(?P<target>[\w./\-]+)"\s*$""")
_RUBY_REQUIRE = re.compile(r"""^\s*require(?:_relative)?\s+['"](?P<target>[^'"]+)['"]""")
_PHP_USE = re.compile(r"^\s*use\s+(?P<target>[\w\\]+)\s*;")
_C_INCLUDE = re.compile(r"""^\s*#\s*include\s*[<"](?P<target>[^>"]+)[>"]""")
_COBOL_COPY = re.compile(r"\bCOPY\s+(?P<target>[A-Za-z0-9][A-Za-z0-9_-]*)", re.IGNORECASE)
_COBOL_CALL = re.compile(r"""\bCALL\s+['"](?P<target>[^'"]+)['"]""", re.IGNORECASE)
_JCL_EXEC = re.compile(r"\bEXEC\s+(?:PGM|PROC)\s*=\s*(?P<target>[A-Za-z0-9@#$][\w@#$-]*)", re.IGNORECASE)
_JCL_DSN = re.compile(r"\bDSN(?:AME)?\s*=\s*(?P<target>[A-Za-z0-9@#$][\w@#$.\-()]*)", re.IGNORECASE)
_SQL_TABLE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(?P<target>[A-Za-z_][\w.$]*)",
    re.IGNORECASE,
)

_SQL_NOISE = frozenset({"select", "where", "set", "values", "dual", "as", "on", "using"})

_URL_PATTERN = re.compile(r"""(?P<scheme>https?)://(?P<host>[A-Za-z0-9_.\-]+(?::\d+)?)(?P<rest>[^\s'"`<>)\]}\\]*)""")

_MESSAGING_TERMS: Mapping[str, tuple[str, ...]] = {
    "kafka": ("kafka",),
    "rabbitmq": ("rabbit", "amqp"),
    "sqs": ("sqs",),
    "sns": ("sns",),
    "jms": ("jms",),
    "mq": ("ibmmq", "websphere-mq", "mqseries"),
    "topic": ("topic",),
    "queue": ("queue",),
    "pubsub": ("pubsub", "pub-sub"),
}

_MESSAGING_PATTERNS: Mapping[str, re.Pattern[str]] = {
    technology: re.compile(
        r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(term) for term in terms) + r")(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    for technology, terms in _MESSAGING_TERMS.items()
}

_INTERNAL_PREFIXES = ("./", "../", ".", "/")


def _read_text(root: Path, path: str, max_file_bytes: int) -> str | None:
    try:
        full_path = resolve_within(root, path)
        raw = full_path.read_bytes()
    except (OSError, ValueError):
        return None
    if len(raw) > max_file_bytes or b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")


def _module_candidates(paths: Iterable[str]) -> frozenset[str]:
    candidates: set[str] = set()
    for path in paths:
        stem = path.rsplit("/", 1)[-1]
        base = stem.rsplit(".", 1)[0]
        candidates.add(base.lower())
        segments = path.split("/")
        for index in range(len(segments)):
            tail = segments[index:]
            tail[-1] = tail[-1].rsplit(".", 1)[0]
            candidates.add(".".join(tail).lower())
    return frozenset(candidates)


def _scope_for(target: str, modules: frozenset[str], relative: bool) -> DependencyScope:
    if relative or target.startswith(_INTERNAL_PREFIXES):
        return DependencyScope.INTERNAL
    lowered = target.lower().rstrip(".*").rstrip(".")
    if lowered in modules:
        return DependencyScope.INTERNAL
    root = lowered.split(".")[0].split("/")[0]
    if root and root in modules:
        return DependencyScope.INTERNAL
    return DependencyScope.EXTERNAL


def _python_imports(path: str, text: str, modules: frozenset[str]) -> list[ImportEdge]:
    edges: list[ImportEdge] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _PYTHON_IMPORT.match(line)
        if match is None:
            continue
        raw_from = match.group("from")
        if raw_from:
            targets = [raw_from]
            relative = line.lstrip().startswith("from .")
        else:
            plain = match.group("plain") or ""
            targets = [
                item.strip().split(" as ")[0].strip()
                for item in plain.split(",")
                if item.strip()
            ]
            relative = False
        for target in targets:
            if not target:
                continue
            edges.append(
                ImportEdge(
                    path=path,
                    line=number,
                    target=target,
                    scope=_scope_for(target, modules, relative),
                    language_hint="python",
                    mechanism="import",
                )
            )
    return edges


def _regex_imports(
    path: str,
    text: str,
    modules: frozenset[str],
    language: str,
    mechanism: str,
    patterns: Sequence[re.Pattern[str]],
    search_mode: bool = False,
) -> list[ImportEdge]:
    edges: list[ImportEdge] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern in patterns:
            iterator = (
                pattern.finditer(line) if search_mode else _first_match(pattern, line)
            )
            for match in iterator:
                target = match.group("target").strip()
                if not target:
                    continue
                relative = target.startswith(_INTERNAL_PREFIXES)
                edges.append(
                    ImportEdge(
                        path=path,
                        line=number,
                        target=target,
                        scope=_scope_for(target, modules, relative),
                        language_hint=language,
                        mechanism=mechanism,
                    )
                )
    return edges


def _first_match(pattern: re.Pattern[str], line: str) -> list[re.Match[str]]:
    match = pattern.match(line)
    return [match] if match else []


def _cobol_imports(path: str, text: str, modules: frozenset[str]) -> list[ImportEdge]:
    edges: list[ImportEdge] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern, mechanism in ((_COBOL_COPY, "copy"), (_COBOL_CALL, "call")):
            for match in pattern.finditer(line):
                target = match.group("target").strip()
                edges.append(
                    ImportEdge(
                        path=path,
                        line=number,
                        target=target,
                        scope=_scope_for(target, modules, False),
                        language_hint="cobol",
                        mechanism=mechanism,
                    )
                )
    return edges


def _jcl_imports(path: str, text: str, modules: frozenset[str]) -> list[ImportEdge]:
    edges: list[ImportEdge] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern, mechanism in ((_JCL_EXEC, "exec_pgm"), (_JCL_DSN, "dd_dsn")):
            for match in pattern.finditer(line):
                target = match.group("target").strip()
                edges.append(
                    ImportEdge(
                        path=path,
                        line=number,
                        target=target,
                        scope=_scope_for(target, modules, False),
                        language_hint="jcl",
                        mechanism=mechanism,
                    )
                )
    return edges


def _sql_imports(path: str, text: str, modules: frozenset[str]) -> list[ImportEdge]:
    edges: list[ImportEdge] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _SQL_TABLE.finditer(line):
            target = match.group("target").strip()
            if not target or target.lower() in _SQL_NOISE:
                continue
            edges.append(
                ImportEdge(
                    path=path,
                    line=number,
                    target=target,
                    scope=DependencyScope.UNRESOLVED,
                    language_hint="sql",
                    mechanism="table",
                )
            )
    return edges


def _imports_for(
    path: str, text: str, language: str, modules: frozenset[str]
) -> list[ImportEdge]:
    if language == "python":
        return _python_imports(path, text, modules)
    if language in {"java", "scala", "groovy"}:
        return _regex_imports(path, text, modules, language, "import", (_JVM_IMPORT,))
    if language == "kotlin":
        return _regex_imports(
            path, text, modules, language, "import", (_JVM_IMPORT, _KOTLIN_IMPORT)
        )
    if language == "csharp":
        return _regex_imports(path, text, modules, language, "using", (_CSHARP_USING,))
    if language in {"javascript", "typescript", "vue", "svelte"}:
        edges = _regex_imports(
            path, text, modules, language, "import", (_JS_IMPORT, _JS_BARE_IMPORT)
        )
        edges.extend(
            _regex_imports(
                path, text, modules, language, "require", (_JS_REQUIRE,), search_mode=True
            )
        )
        return edges
    if language == "go":
        return _regex_imports(path, text, modules, language, "import", (_GO_IMPORT,))
    if language == "ruby":
        return _regex_imports(path, text, modules, language, "require", (_RUBY_REQUIRE,))
    if language == "php":
        return _regex_imports(path, text, modules, language, "use", (_PHP_USE,))
    if language in {"c", "cpp", "objc"}:
        return _regex_imports(path, text, modules, language, "include", (_C_INCLUDE,))
    if language == "cobol":
        return _cobol_imports(path, text, modules)
    if language == "jcl":
        return _jcl_imports(path, text, modules)
    if language == "sql":
        return _sql_imports(path, text, modules)
    return []


def _endpoints_for(path: str, text: str) -> list[ExternalEndpoint]:
    found: list[ExternalEndpoint] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _URL_PATTERN.finditer(line):
            host = match.group("host")
            url = match.group(0).rstrip(".,;:")
            found.append(
                ExternalEndpoint(
                    path=path,
                    line=number,
                    url=url,
                    host=host,
                    scheme=match.group("scheme"),
                )
            )
    return found


def _messaging_for(path: str, text: str) -> list[MessagingHint]:
    found: list[MessagingHint] = []
    for number, line in enumerate(text.splitlines(), start=1):
        excerpt = line.strip()[:400]
        for technology, pattern in _MESSAGING_PATTERNS.items():
            if pattern.search(line):
                found.append(
                    MessagingHint(
                        path=path, line=number, technology=technology, excerpt=excerpt
                    )
                )
    return found


def detect_dependencies(
    snapshot: RepositorySnapshot,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> DependencyReport:
    root = Path(snapshot.root)
    paths = [record.path for record in snapshot.files]
    modules = _module_candidates(paths)
    imports: list[ImportEdge] = []
    manifests: list[ManifestEntry] = []
    endpoints: list[ExternalEndpoint] = []
    messaging: list[MessagingHint] = []
    for path in paths:
        text = _read_text(root, path, max_file_bytes)
        if text is None:
            continue
        language = language_hint_for(path)
        imports.extend(_imports_for(path, text, language, modules))
        if is_manifest(path):
            manifests.extend(manifest_entries(path, text))
        endpoints.extend(_endpoints_for(path, text))
        messaging.extend(_messaging_for(path, text))
    return DependencyReport(
        snapshot_digest=snapshot.digest,
        imports=tuple(imports),
        manifests=tuple(manifests),
        endpoints=tuple(endpoints),
        messaging=tuple(messaging),
    )
