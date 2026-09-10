from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping

from wiki_ai.repository.inventory import language_hint_for
from wiki_ai.repository.reader import snapshot_text
from wiki_ai.repository.snapshot import RepositorySnapshot

__all__ = [
    "ConfigHitKind",
    "ConfigHit",
    "DEFAULT_MAX_CONFIG_HITS",
    "DEFAULT_MAX_FILE_BYTES",
    "find_config",
]

DEFAULT_MAX_CONFIG_HITS = 200
DEFAULT_MAX_FILE_BYTES = 1048576


class ConfigHitKind(Enum):
    DECLARATION = "declaration"
    USAGE = "usage"


@dataclass(frozen=True)
class ConfigHit:
    path: str
    line: int
    column: int
    key: str
    value: str | None
    kind: ConfigHitKind
    mechanism: str
    language_hint: str
    excerpt: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "key": self.key,
            "value": self.value,
            "kind": self.kind.value,
            "mechanism": self.mechanism,
            "language_hint": self.language_hint,
            "excerpt": self.excerpt,
        }


_CONFIG_SUFFIXES = (
    ".env", ".properties", ".yaml", ".yml", ".json", ".toml", ".ini",
    ".xml", ".cfg", ".conf",
)

_CONFIG_NAMES = frozenset({".env", ".env.example", ".env.local", ".env.sample"})

_KEY_VALUE = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][\w.\-]*)\s*[:=]\s*(?P<value>.*)$")
_YAML_KEY = re.compile(r"^(?P<indent>\s*)(?:-\s+)?(?P<key>[A-Za-z_][\w.\-]*)\s*:(?:\s+(?P<value>.*))?$")
_JSON_KEY = re.compile(r'"(?P<key>[^"]+)"\s*:\s*(?P<value>"[^"]*"|[^,\}\s]+)')
_XML_PROPERTY = re.compile(
    r"""<(?P<tag>[\w.\-]+)(?P<attributes>(?:\s+[\w.\-:]+\s*=\s*["'][^"']*["'])*)\s*>(?P<value>[^<]*)(?=<)""",
)
_XML_NAME_ATTRIBUTE = re.compile(r"""\b(?:name|key|id)\s*=\s*["'](?P<value>[^"']+)["']""")

_USAGE_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "os.environ": re.compile(r"""os\.environ(?:\.get)?\s*(?:\[|\()\s*['"](?P<key>[^'"]+)['"]"""),
    "getenv": re.compile(r"""(?<![\w.])(?:os\.)?getenv\s*\(\s*['"](?P<key>[^'"]+)['"]""", re.IGNORECASE),
    "value_annotation": re.compile(r"""@Value\s*\(\s*["']\$?\{?(?P<key>[\w.\-]+)"""),
    "configuration_properties": re.compile(
        r"""@ConfigurationProperties\s*\(\s*(?:prefix\s*=\s*)?["'](?P<key>[^"']+)["']"""
    ),
    "process.env": re.compile(r"""process\.env(?:\.(?P<key>\w+)|\[\s*['"](?P<key2>[^'"]+)['"]\s*\])"""),
    "system_get_property": re.compile(
        r"""System\.get(?:Property|env)\s*\(\s*["'](?P<key>[^"']+)["']"""
    ),
    "configuration_manager": re.compile(
        r"""ConfigurationManager\.(?:AppSettings|ConnectionStrings)\s*\[\s*["'](?P<key>[^"']+)["']"""
    ),
    "config_get": re.compile(
        r"""(?<![\w.])config(?:uration)?\.get(?:String|Int|Bool|Boolean)?\s*\(\s*['"](?P<key>[^'"]+)['"]""",
        re.IGNORECASE,
    ),
}


def _is_config_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if name in _CONFIG_NAMES or name.startswith(".env"):
        return True
    return name.endswith(_CONFIG_SUFFIXES)


def _matches_query(key: str, query: str) -> bool:
    lowered = key.lower()
    target = query.lower()
    if target in lowered:
        return True
    normalized_key = re.sub(r"[^a-z0-9]", "", lowered)
    normalized_query = re.sub(r"[^a-z0-9]", "", target)
    return bool(normalized_query) and normalized_query in normalized_key


def _clean_value(raw: str) -> str | None:
    value = raw.strip().split("#", 1)[0].strip().strip(",")
    value = value.strip("\"'")
    return value or None


def _declarations(path: str, text: str, query: str) -> list[ConfigHit]:
    name = path.rsplit("/", 1)[-1].lower()
    language = language_hint_for(path)
    is_yaml = name.endswith((".yaml", ".yml"))
    stack: list[tuple[int, str]] = []
    found: list[ConfigHit] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "//")):
            continue
        excerpt = stripped[:400]
        if name.endswith((".json",)):
            for match in _JSON_KEY.finditer(line):
                key = match.group("key")
                if not _matches_query(key, query):
                    continue
                found.append(
                    ConfigHit(
                        path=path,
                        line=number,
                        column=match.start("key") + 1,
                        key=key,
                        value=_clean_value(match.group("value")),
                        kind=ConfigHitKind.DECLARATION,
                        mechanism="json",
                        language_hint=language,
                        excerpt=excerpt,
                    )
                )
            continue
        if name.endswith((".xml",)):
            for match in _XML_PROPERTY.finditer(line):
                attribute = _XML_NAME_ATTRIBUTE.search(match.group("attributes") or "")
                key = attribute.group("value") if attribute else match.group("tag")
                if not _matches_query(key, query):
                    continue
                found.append(
                    ConfigHit(
                        path=path,
                        line=number,
                        column=match.start() + 1,
                        key=key,
                        value=_clean_value(match.group("value")),
                        kind=ConfigHitKind.DECLARATION,
                        mechanism="xml",
                        language_hint=language,
                        excerpt=excerpt,
                    )
                )
            continue
        if is_yaml:
            match = _YAML_KEY.match(line)
            if match is None:
                continue
            indent = len(match.group("indent"))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            key = ".".join([item[1] for item in stack] + [match.group("key")])
            stack.append((indent, match.group("key")))
            if not _matches_query(key, query):
                continue
            found.append(
                ConfigHit(
                    path=path,
                    line=number,
                    column=match.start("key") + 1,
                    key=key,
                    value=_clean_value(match.group("value") or ""),
                    kind=ConfigHitKind.DECLARATION,
                    mechanism="yaml",
                    language_hint=language,
                    excerpt=excerpt,
                )
            )
            continue
        match = _KEY_VALUE.match(line)
        if match is None:
            continue
        key = match.group("key")
        if not _matches_query(key, query):
            continue
        found.append(
            ConfigHit(
                path=path,
                line=number,
                column=match.start("key") + 1,
                key=key,
                value=_clean_value(match.group("value")),
                kind=ConfigHitKind.DECLARATION,
                mechanism="key_value",
                language_hint=language,
                excerpt=excerpt,
            )
        )
    return found


def _usages(path: str, text: str, query: str) -> list[ConfigHit]:
    language = language_hint_for(path)
    found: list[ConfigHit] = []
    for number, line in enumerate(text.splitlines(), start=1):
        excerpt = line.strip()[:400]
        for mechanism, pattern in _USAGE_PATTERNS.items():
            for match in pattern.finditer(line):
                groups = match.groupdict()
                key = groups.get("key") or groups.get("key2")
                if not key or not _matches_query(key, query):
                    continue
                found.append(
                    ConfigHit(
                        path=path,
                        line=number,
                        column=match.start() + 1,
                        key=key,
                        value=None,
                        kind=ConfigHitKind.USAGE,
                        mechanism=mechanism,
                        language_hint=language,
                        excerpt=excerpt,
                    )
                )
    return found


def find_config(
    snapshot: RepositorySnapshot,
    key_or_usage: str,
    *,
    limit: int = DEFAULT_MAX_CONFIG_HITS,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> tuple[ConfigHit, ...]:
    query = (key_or_usage or "").strip()
    if not query or limit <= 0:
        return ()
    collected: list[ConfigHit] = []
    for record in snapshot.files:
        text = snapshot_text(snapshot, record.path, max_file_bytes)
        if text is None:
            continue
        if _is_config_file(record.path):
            collected.extend(_declarations(record.path, text, query))
        collected.extend(_usages(record.path, text, query))
    collected.sort(
        key=lambda hit: (hit.kind is ConfigHitKind.USAGE, hit.path, hit.line, hit.column)
    )
    return tuple(collected[:limit])
