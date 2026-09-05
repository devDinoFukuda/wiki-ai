"""Adaptadores de formatos estruturados: JSON, YAML, XML, SQL e manifests (§6.2).

Configuração é fonte de primeira classe no §5.4 (`SourceKind.CONFIG` exige
`file`, `key` e `version`), então cada valor sai como `ConfigItem` com
`keypath` e intervalo de linhas reais — não como um blob do arquivo.

Decisões que estão em código, não em promessa:

- **XML nunca resolve entidade externa.** `_make_expat` instala
  `StartDoctypeDeclHandler`, `EntityDeclHandler` e `UnparsedEntityDeclHandler`
  que levantam erro, e `ExternalEntityRefHandler` que devolve falha. DTD é
  recusada antes mesmo do parse por `_has_dtd`. XXE e billion-laughs param na
  porta, e a recusa vira `Diagnostic`, não exceção.
- **YAML é um subconjunto declarado.** `parse_yaml_min` cobre mapa, lista e
  aninhamento por indentação — o suficiente para manifests. Âncora, alias,
  escalar em bloco, coleção em fluxo e merge key **não** são suportados e cada
  ocorrência emite `Diagnostic("yaml_partial")` com a linha. O arquivo não é
  dado como lido por inteiro quando não foi.
- **`pyproject.toml` depende de `tomllib`** (3.11+). Sem ele não há palpite:
  sai `Diagnostic("toml_unavailable")` e o arquivo conta como não parseado na
  matriz de cobertura.
- **DDL vira candidata a entidade de dados**, não fato. `parse_sql_ddl` é
  reaproveitável por outros adaptadores para o SQL que eles acharem em
  literais (§5.4).
"""

from __future__ import annotations

import json
import os
import re
import xml.parsers.expat as expat
from typing import Any

from .base import (
    CodeExtractor,
    ConfigItem,
    DataEntity,
    Diagnostic,
    Entrypoint,
    Reference,
    Symbol,
    build_line_index,
    line_from_index,
    looks_like_sql,
)

try:  # 3.11+
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - depende da versão do runtime
    _tomllib = None


# ======================================================================
# JSON
# ======================================================================

_JSON_LITERAL = re.compile(r"(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)")


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def scan_json_positions(text: str) -> list[tuple[str, Any, int, int]]:
    """`(keypath, valor, offset_inicial, offset_final)` para cada folha do JSON.

    O módulo `json` da stdlib não devolve posição, e sem posição não existe
    localizador de evidência. Este scanner reaproveita `json.decoder.scanstring`
    (que já trata escapes corretamente) e cuida apenas da estrutura, para que o
    `ConfigItem` cite a linha exata onde a chave está.
    """
    out: list[tuple[str, Any, int, int]] = []
    scanstring = json.decoder.scanstring

    def value(pos: int, prefix: str) -> int:
        pos = _skip_ws(text, pos)
        if pos >= len(text):
            return pos
        ch = text[pos]
        if ch == "{":
            pos += 1
            while True:
                pos = _skip_ws(text, pos)
                if pos >= len(text):
                    return pos
                if text[pos] == "}":
                    return pos + 1
                if text[pos] == ",":
                    pos += 1
                    continue
                if text[pos] != '"':
                    return pos + 1
                key, pos = scanstring(text, pos + 1)
                pos = _skip_ws(text, pos)
                if pos < len(text) and text[pos] == ":":
                    pos += 1
                child = f"{prefix}.{key}" if prefix else key
                pos = value(pos, child)
        elif ch == "[":
            pos += 1
            idx = 0
            while True:
                pos = _skip_ws(text, pos)
                if pos >= len(text):
                    return pos
                if text[pos] == "]":
                    return pos + 1
                if text[pos] == ",":
                    pos += 1
                    continue
                pos = value(pos, f"{prefix}[{idx}]")
                idx += 1
        elif ch == '"':
            start = pos
            s, pos = scanstring(text, pos + 1)
            out.append((prefix, s, start, pos))
        else:
            m = _JSON_LITERAL.match(text, pos)
            if not m:
                return pos + 1
            raw = m.group(1)
            parsed: Any = {"true": True, "false": False, "null": None}.get(raw)
            if raw not in ("true", "false", "null"):
                parsed = float(raw) if ("." in raw or "e" in raw.lower()) else int(raw)
            out.append((prefix, parsed, pos, m.end()))
            pos = m.end()
        return pos

    value(0, "")
    return out


#: Chaves de manifest npm que declaram dependências.
_NPM_DEP_KEYS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


class JsonExtractor(CodeExtractor):
    """JSON via `json` da stdlib, com posição real de cada folha.

    `package.json` recebe tratamento adicional de manifest: as dependências
    declaradas saem como `ConfigItem(kind="dependency")` e como `Reference`
    de import não resolvida (o pacote está fora do escopo analisado).
    """

    language = "json"
    extensions = (".json",)
    resolution_level = "syntactic"

    def _leaves(self, path: str, content: str) -> list[tuple[str, Any, int, int]]:
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            self.add_diagnostic(
                Diagnostic(
                    level="error",
                    code="json_invalid",
                    message=f"JSON inválido: {exc.msg} (linha {exc.lineno}, coluna {exc.colno})",
                    scope_examined=f"json.loads sobre {path}",
                    path=path,
                    paths=(path,),
                    language=self.language,
                    impact="nenhuma configuração deste arquivo entra no conhecimento",
                )
            )
            return []
        return scan_json_positions(content)

    def symbols(self, path: str, content: str) -> list[Symbol]:
        total = max(1, len(content.splitlines()) or 1)
        return [
            Symbol(
                name=os.path.basename(path),
                kind="module",
                path=path,
                line_start=1,
                line_end=total,
                language=self.language,
                qualname=path,
                resolution="syntactic",
            )
        ]

    def references(self, path: str, content: str) -> list[Reference]:
        if os.path.basename(path) != "package.json":
            return []
        starts = build_line_index(content)
        out: list[Reference] = []
        for keypath, value, start, _end in self._leaves(path, content):
            head, _, name = keypath.partition(".")
            if head in _NPM_DEP_KEYS and name:
                out.append(
                    Reference(
                        from_symbol=path,
                        to_name=name,
                        kind="import",
                        path=path,
                        line=line_from_index(starts, start),
                        resolved=False,
                        language=self.language,
                        resolution="syntactic",
                        reason="dependência declarada; pacote fora do conjunto analisado",
                        extra={"version_spec": value, "scope": head},
                    )
                )
        return out

    def entrypoints(self, path: str, content: str) -> list[Entrypoint]:
        if os.path.basename(path) != "package.json":
            return []
        starts = build_line_index(content)
        out: list[Entrypoint] = []
        for keypath, value, start, _end in self._leaves(path, content):
            head, _, name = keypath.partition(".")
            if head == "scripts" and name:
                out.append(
                    Entrypoint(
                        kind="cli",
                        name=f"npm run {name}",
                        path=path,
                        line=line_from_index(starts, start),
                        framework="npm",
                        symbol=f"{path}#scripts.{name}",
                        language=self.language,
                        resolution="syntactic",
                        detail=str(value),
                    )
                )
            elif keypath in ("bin", "main") and isinstance(value, str):
                out.append(
                    Entrypoint(
                        kind="cli" if keypath == "bin" else "public_api",
                        name=value,
                        path=path,
                        line=line_from_index(starts, start),
                        framework="npm",
                        symbol=f"{path}#{keypath}",
                        language=self.language,
                        resolution="syntactic",
                        detail=f"campo `{keypath}` do package.json",
                    )
                )
        return out

    def configuration(self, path: str, content: str) -> list[ConfigItem]:
        starts = build_line_index(content)
        is_npm = os.path.basename(path) == "package.json"
        out: list[ConfigItem] = []
        for keypath, value, start, end in self._leaves(path, content):
            head = keypath.partition(".")[0]
            kind = "dependency" if (is_npm and head in _NPM_DEP_KEYS) else "config"
            if isinstance(value, str) and looks_like_sql(value):
                kind = "sql_literal"
            out.append(
                ConfigItem(
                    keypath=keypath or "(raiz)",
                    value=value,
                    path=path,
                    line_start=line_from_index(starts, start),
                    line_end=line_from_index(starts, max(start, end - 1)),
                    kind=kind,
                    source_format="json",
                    language=self.language,
                    resolution="syntactic",
                )
            )
        return out


# ======================================================================
# YAML (subconjunto declarado)
# ======================================================================

#: Construções fora do subconjunto suportado. Cada ocorrência vira diagnóstico.
_YAML_UNSUPPORTED: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("âncora", re.compile(r":\s*&\S")),
    ("alias", re.compile(r":\s*\*\S")),
    ("escalar em bloco", re.compile(r":\s*[|>][-+\d]*\s*$")),
    ("coleção em fluxo", re.compile(r":\s*[\[{]")),
    ("merge key", re.compile(r"^\s*<<\s*:")),
    ("tag explícita", re.compile(r":\s*!!")),
    ("chave complexa", re.compile(r"^\s*\?\s")),
)


def _strip_yaml_comment(line: str) -> str:
    out: list[str] = []
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote and (i == 0 or line[i - 1] != "\\"):
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out)


def _yaml_scalar(raw: str) -> Any:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def parse_yaml_min(text: str) -> tuple[list[tuple[str, Any, int]], list[tuple[str, int, str]]]:
    """YAML mínimo: `(itens, não_suportados)`.

    `itens` são `(keypath, valor, linha)`; `não_suportados` são
    `(construção, linha, texto)` — o que o subconjunto não cobre é devolvido
    para virar `Diagnostic`, jamais silenciado.
    """
    items: list[tuple[str, Any, int]] = []
    unsupported: list[tuple[str, int, str]] = []
    stack: list[tuple[int, str]] = []
    counters: dict[str, int] = {}
    doc = 0

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_yaml_comment(raw_line)
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped in ("---", "..."):
            doc += 1
            stack.clear()
            continue
        for label, pattern in _YAML_UNSUPPORTED:
            if pattern.search(line):
                unsupported.append((label, lineno, stripped))
                break
        indent = len(line) - len(line.lstrip(" "))
        while stack and indent <= stack[-1][0]:
            stack.pop()
        prefix = ".".join(k for _i, k in stack)
        doc_prefix = f"doc{doc}." if doc > 1 else ""

        if stripped.startswith("- "):
            item = stripped[2:].strip()
            idx = counters.get(prefix, 0)
            counters[prefix] = idx + 1
            base = f"{doc_prefix}{prefix}[{idx}]" if prefix else f"{doc_prefix}[{idx}]"
            if ":" in item and not item.startswith(("'", '"')):
                key, _, val = item.partition(":")
                items.append((f"{base}.{key.strip()}", _yaml_scalar(val), lineno))
                stack.append((indent, f"{prefix}[{idx}]" if prefix else f"[{idx}]"))
            else:
                items.append((base, _yaml_scalar(item), lineno))
            continue

        if ":" not in stripped:
            unsupported.append(("linha sem `chave:`", lineno, stripped))
            continue

        key, _, val = stripped.partition(":")
        key = key.strip().strip("\"'")
        keypath = f"{doc_prefix}{prefix}.{key}" if prefix else f"{doc_prefix}{key}"
        if val.strip() == "":
            stack.append((indent, key))
            counters[".".join(k for _i, k in stack)] = 0
        else:
            items.append((keypath, _yaml_scalar(val), lineno))
    return items, unsupported


class YamlExtractor(CodeExtractor):
    """YAML por parser próprio mínimo, com o que não cobre declarado por linha."""

    language = "yaml"
    extensions = (".yml", ".yaml")
    resolution_level = "syntactic"

    def symbols(self, path: str, content: str) -> list[Symbol]:
        total = max(1, len(content.splitlines()) or 1)
        return [
            Symbol(
                name=os.path.basename(path),
                kind="module",
                path=path,
                line_start=1,
                line_end=total,
                language=self.language,
                qualname=path,
                resolution="syntactic",
            )
        ]

    def references(self, path: str, content: str) -> list[Reference]:
        return []

    def entrypoints(self, path: str, content: str) -> list[Entrypoint]:
        return []

    def configuration(self, path: str, content: str) -> list[ConfigItem]:
        items, unsupported = parse_yaml_min(content)
        if unsupported:
            sample = "; ".join(f"linha {ln}: {label}" for label, ln, _t in unsupported[:5])
            self.add_diagnostic(
                Diagnostic(
                    level="warning",
                    code="yaml_partial",
                    message=(
                        f"{len(unsupported)} construção(ões) YAML fora do subconjunto suportado "
                        f"(mapa, lista e aninhamento por indentação): {sample}"
                    ),
                    scope_examined=f"parse_yaml_min sobre {path} ({len(content.splitlines())} linhas)",
                    path=path,
                    paths=(path,),
                    language=self.language,
                    impact="os valores dessas linhas não entram como configuração; podem esconder chave relevante",
                    extra={"lines": tuple(ln for _l, ln, _t in unsupported)},
                )
            )
        out: list[ConfigItem] = []
        for keypath, value, lineno in items:
            kind = "sql_literal" if isinstance(value, str) and looks_like_sql(value) else "config"
            out.append(
                ConfigItem(
                    keypath=keypath,
                    value=value,
                    path=path,
                    line_start=lineno,
                    line_end=lineno,
                    kind=kind,
                    source_format="yaml",
                    language=self.language,
                    # O subconjunto é sintático para o que cobre; o adaptador
                    # não infere nada além do que a linha diz.
                    resolution="syntactic",
                )
            )
        return out


# ======================================================================
# XML
# ======================================================================

_RE_DTD = re.compile(r"<!DOCTYPE|<!ENTITY", re.I)


def _has_dtd(content: str) -> bool:
    return bool(_RE_DTD.search(content))


class XmlForbidden(ValueError):
    """DTD ou entidade encontrada: o parse é recusado antes de qualquer resolução."""


def _make_expat() -> expat.XMLParserType:
    """Parser expat com resolução de entidade e DTD desligadas.

    Quatro portas fechadas: declaração de DOCTYPE, declaração de entidade,
    entidade não-parseada e referência a entidade externa. É o que impede XXE
    (leitura de arquivo local via `SYSTEM`) e billion-laughs.
    """
    parser = expat.ParserCreate()

    def forbid(*_args: Any) -> None:
        raise XmlForbidden("DTD/entidade em XML é recusada: risco de XXE e expansão de entidade")

    parser.StartDoctypeDeclHandler = forbid
    parser.EntityDeclHandler = forbid
    parser.UnparsedEntityDeclHandler = forbid
    parser.ExternalEntityRefHandler = lambda *_args: 0  # 0 sinaliza falha ao expat
    return parser


def walk_xml(content: str) -> list[tuple[str, str, int, dict[str, str]]]:
    """`(keypath, texto, linha, atributos)` por elemento, sem resolver entidades."""
    parser = _make_expat()
    stack: list[str] = []
    counters: dict[str, int] = {}
    out: list[tuple[str, str, int, dict[str, str]]] = []
    pending: dict[int, list[str]] = {}
    lines: list[int] = []
    keys: list[str] = []
    attrs_stack: list[dict[str, str]] = []

    def start(name: str, attrs: dict[str, str]) -> None:
        parent = ".".join(keys)
        base = f"{parent}.{name}" if parent else name
        idx = counters.get(base, 0)
        counters[base] = idx + 1
        keys.append(name if idx == 0 else f"{name}[{idx}]")
        lines.append(parser.CurrentLineNumber)
        attrs_stack.append(dict(attrs))
        pending[len(keys)] = []

    def chars(data: str) -> None:
        if keys:
            pending.setdefault(len(keys), []).append(data)

    def end(_name: str) -> None:
        keypath = ".".join(keys)
        text = "".join(pending.pop(len(keys), [])).strip()
        out.append((keypath, text, lines[-1], attrs_stack[-1]))
        keys.pop()
        lines.pop()
        attrs_stack.pop()

    parser.StartElementHandler = start
    parser.CharacterDataHandler = chars
    parser.EndElementHandler = end
    parser.Parse(content, True)
    return out


class XmlExtractor(CodeExtractor):
    """XML com entidades externas e DTD proibidas; `pom.xml` lido como manifest."""

    language = "xml"
    extensions = (".xml", ".xsd", ".xsl", ".pom")
    resolution_level = "syntactic"

    def _walk(self, path: str, content: str) -> list[tuple[str, str, int, dict[str, str]]]:
        if _has_dtd(content):
            self.add_diagnostic(
                Diagnostic(
                    level="error",
                    code="xml_dtd_forbidden",
                    message=(
                        "arquivo declara DOCTYPE/ENTITY; parse recusado para não resolver "
                        "entidade externa (XXE) nem expandir entidade recursiva"
                    ),
                    scope_examined=f"varredura de <!DOCTYPE/<!ENTITY em {path}",
                    path=path,
                    paths=(path,),
                    language=self.language,
                    impact="nenhuma configuração deste XML entra no conhecimento",
                )
            )
            return []
        try:
            return walk_xml(content)
        except (expat.ExpatError, XmlForbidden) as exc:
            self.add_diagnostic(
                Diagnostic(
                    level="error",
                    code="xml_invalid",
                    message=f"XML não parseia: {type(exc).__name__}: {exc}",
                    scope_examined=f"expat (DTD/entidades desligadas) sobre {path}",
                    path=path,
                    paths=(path,),
                    language=self.language,
                    impact="nenhuma configuração deste XML entra no conhecimento",
                )
            )
            return []

    def symbols(self, path: str, content: str) -> list[Symbol]:
        total = max(1, len(content.splitlines()) or 1)
        return [
            Symbol(
                name=os.path.basename(path),
                kind="module",
                path=path,
                line_start=1,
                line_end=total,
                language=self.language,
                qualname=path,
                resolution="syntactic",
            )
        ]

    def _maven_dependencies(self, elements) -> list[tuple[str, str, str, int]]:
        """`(groupId, artifactId, version, linha)` de cada `<dependency>` do pom."""
        buckets: dict[str, dict[str, Any]] = {}
        for keypath, text, lineno, _attrs in elements:
            m = re.match(r"^(?P<base>.*\.dependency(?:\[\d+\])?)\.(?P<field>groupId|artifactId|version)$", keypath)
            if not m:
                continue
            slot = buckets.setdefault(m.group("base"), {"line": lineno})
            slot[m.group("field")] = text
            slot["line"] = min(slot["line"], lineno)
        out = []
        for slot in buckets.values():
            out.append(
                (
                    slot.get("groupId", ""),
                    slot.get("artifactId", ""),
                    slot.get("version", ""),
                    slot["line"],
                )
            )
        return out

    def references(self, path: str, content: str) -> list[Reference]:
        if os.path.basename(path) != "pom.xml":
            return []
        out: list[Reference] = []
        for group, artifact, version, lineno in self._maven_dependencies(self._walk(path, content)):
            if not artifact:
                continue
            out.append(
                Reference(
                    from_symbol=path,
                    to_name=f"{group}:{artifact}" if group else artifact,
                    kind="import",
                    path=path,
                    line=lineno,
                    resolved=False,
                    language=self.language,
                    resolution="syntactic",
                    reason="dependência Maven declarada; artefato fora do conjunto analisado",
                    extra={"version": version},
                )
            )
        return out

    def entrypoints(self, path: str, content: str) -> list[Entrypoint]:
        return []

    def configuration(self, path: str, content: str) -> list[ConfigItem]:
        elements = self._walk(path, content)
        is_pom = os.path.basename(path) == "pom.xml"
        out: list[ConfigItem] = []
        for keypath, text, lineno, attrs in elements:
            if text:
                kind = "sql_literal" if looks_like_sql(text) else "config"
                out.append(
                    ConfigItem(
                        keypath=keypath,
                        value=text,
                        path=path,
                        line_start=lineno,
                        line_end=lineno,
                        kind=kind,
                        source_format="xml",
                        language=self.language,
                        resolution="syntactic",
                    )
                )
            for attr, value in attrs.items():
                out.append(
                    ConfigItem(
                        keypath=f"{keypath}@{attr}",
                        value=value,
                        path=path,
                        line_start=lineno,
                        line_end=lineno,
                        kind="config",
                        source_format="xml",
                        language=self.language,
                        resolution="syntactic",
                    )
                )
        if is_pom:
            for group, artifact, version, lineno in self._maven_dependencies(elements):
                out.append(
                    ConfigItem(
                        keypath=f"maven:{group}:{artifact}",
                        value=version or "(herdada de dependencyManagement/parent)",
                        path=path,
                        line_start=lineno,
                        line_end=lineno,
                        kind="dependency",
                        source_format="pom.xml",
                        language=self.language,
                        resolution="syntactic",
                        extra={"groupId": group, "artifactId": artifact},
                    )
                )
        return out


# ======================================================================
# SQL
# ======================================================================

_RE_CREATE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:UNIQUE\s+)?"
    r"(?P<kind>TABLE|INDEX|VIEW|MATERIALIZED\s+VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>[\w.\"`\[\]]+)",
    re.I,
)
#: Termos que abrem restrição de tabela, não coluna.
_CONSTRAINT_HEADS = frozenset(
    {"primary", "foreign", "unique", "check", "constraint", "key", "index", "exclude"}
)


def _unquote_ident(name: str) -> str:
    return name.strip().strip('"').strip("`").strip("[]").strip()


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    quote = ""
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


def parse_sql_ddl(sql: str, path: str, line_offset: int = 0) -> list[DataEntity]:
    """DDL (`CREATE TABLE/INDEX/VIEW`) → candidatas a entidade de dados.

    Reaproveitável pelos demais adaptadores: o SQL que aparece num literal
    Python ou num campo `static final String` Java passa por aqui com o
    `line_offset` do literal, e as linhas continuam apontando para o arquivo
    real (§5.4).
    """
    out: list[DataEntity] = []
    for m in _RE_CREATE.finditer(sql):
        kw = re.sub(r"\s+", " ", m.group("kind").upper())
        kind = {"TABLE": "table", "INDEX": "index", "VIEW": "view", "MATERIALIZED VIEW": "view"}[kw]
        name = _unquote_ident(m.group("name"))
        start_line = line_offset + sql.count("\n", 0, m.start()) + 1
        columns: list[dict[str, Any]] = []
        detail = ""
        end_line = start_line
        if kind == "table":
            open_paren = sql.find("(", m.end())
            if open_paren != -1:
                depth = 0
                close = open_paren
                for i in range(open_paren, len(sql)):
                    if sql[i] == "(":
                        depth += 1
                    elif sql[i] == ")":
                        depth -= 1
                        if depth == 0:
                            close = i
                            break
                body = sql[open_paren + 1 : close]
                end_line = line_offset + sql.count("\n", 0, close) + 1
                for piece in _split_top_level(body):
                    tokens = piece.split()
                    if not tokens:
                        continue
                    if tokens[0].lower() in _CONSTRAINT_HEADS:
                        continue
                    col = _unquote_ident(tokens[0])
                    col_type = tokens[1] if len(tokens) > 1 else ""
                    rest = " ".join(tokens[2:]).upper()
                    columns.append(
                        {
                            "name": col,
                            "type": col_type,
                            "not_null": "NOT NULL" in rest or "NOT NULL" in piece.upper(),
                            "primary_key": "PRIMARY KEY" in piece.upper(),
                            "references": _reference_target(piece),
                        }
                    )
        elif kind == "index":
            on = re.search(r"\bON\s+([\w.\"`\[\]]+)", sql[m.end() :], re.I)
            detail = f"sobre {_unquote_ident(on.group(1))}" if on else ""
            end_line = start_line
        out.append(
            DataEntity(
                name=name,
                kind=kind,
                path=path,
                line_start=start_line,
                line_end=max(start_line, end_line),
                columns=tuple(columns),
                resolution="syntactic",
                detail=detail,
            )
        )
    return out


def _reference_target(piece: str) -> str:
    m = re.search(r"\bREFERENCES\s+([\w.\"`\[\]]+)", piece, re.I)
    return _unquote_ident(m.group(1)) if m else ""


class SqlExtractor(CodeExtractor):
    """Arquivos `.sql`: DDL vira `DataEntity`; cada statement vira `ConfigItem`."""

    language = "sql"
    extensions = (".sql", ".ddl")
    resolution_level = "syntactic"

    def symbols(self, path: str, content: str) -> list[Symbol]:
        out: list[Symbol] = []
        for entity in parse_sql_ddl(content, path):
            out.append(
                Symbol(
                    name=entity.name,
                    kind="const" if entity.kind == "index" else "class",
                    path=path,
                    line_start=entity.line_start,
                    line_end=entity.line_end,
                    language=self.language,
                    qualname=f"{entity.kind}:{entity.name}",
                    signature=f"{entity.kind.upper()} {entity.name}",
                    resolution="syntactic",
                    extra={"ddl_kind": entity.kind, "columns": len(entity.columns)},
                )
            )
        return out

    def references(self, path: str, content: str) -> list[Reference]:
        out: list[Reference] = []
        for entity in parse_sql_ddl(content, path):
            for col in entity.columns:
                if col.get("references"):
                    out.append(
                        Reference(
                            from_symbol=f"table:{entity.name}",
                            to_name=str(col["references"]),
                            kind="attribute",
                            path=path,
                            line=entity.line_start,
                            line_end=entity.line_end,
                            resolved=False,
                            language=self.language,
                            resolution="syntactic",
                            reason="chave estrangeira declarada; tabela alvo não confirmada no escopo",
                            extra={"column": col["name"]},
                        )
                    )
        return out

    def entrypoints(self, path: str, content: str) -> list[Entrypoint]:
        return []

    def configuration(self, path: str, content: str) -> list[ConfigItem]:
        out: list[ConfigItem] = []
        offset = 0
        for raw in content.split(";"):
            statement = raw.strip()
            if statement:
                start = offset + (len(raw) - len(raw.lstrip()))
                line = content.count("\n", 0, start) + 1
                end_line = line + statement.count("\n")
                out.append(
                    ConfigItem(
                        keypath=f"{os.path.basename(path)}#statement@{line}",
                        value=statement,
                        path=path,
                        line_start=line,
                        line_end=end_line,
                        kind="sql_literal",
                        source_format="sql",
                        language=self.language,
                        resolution="syntactic",
                    )
                )
            offset += len(raw) + 1
        return out

    def data_entities(self, path: str, content: str) -> list[DataEntity]:
        return parse_sql_ddl(content, path)


# ======================================================================
# Manifests (não-JSON/XML)
# ======================================================================

_RE_REQUIREMENT = re.compile(r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*(?P<spec>[<>=!~][^;#]*)?")
_RE_GRADLE_DEP = re.compile(
    r"\b(?P<conf>implementation|api|compileOnly|runtimeOnly|testImplementation|testCompileOnly|annotationProcessor|kapt)"
    r"\s*\(?\s*[\"'](?P<coord>[^\"']+)[\"']"
)

MANIFEST_BASENAMES: frozenset[str] = frozenset(
    {"pyproject.toml", "build.gradle", "build.gradle.kts", "settings.gradle", "Pipfile", "poetry.lock"}
)


class ManifestExtractor(CodeExtractor):
    """`requirements*.txt`, `pyproject.toml`, `build.gradle*` e demais `.toml`.

    `package.json` e `pom.xml` ficam com `JsonExtractor`/`XmlExtractor`, que já
    parseiam o formato — evita dois adaptadores disputando o mesmo arquivo.
    """

    language = "manifest"
    extensions = (".toml",)
    resolution_level = "syntactic"

    def detect(self, path: str, content: str = "") -> bool:
        base = os.path.basename(path)
        if base in MANIFEST_BASENAMES:
            return True
        if base.startswith("requirements") and base.endswith(".txt"):
            return True
        return os.path.splitext(path)[1].lower() in self.extensions

    def symbols(self, path: str, content: str) -> list[Symbol]:
        total = max(1, len(content.splitlines()) or 1)
        return [
            Symbol(
                name=os.path.basename(path),
                kind="module",
                path=path,
                line_start=1,
                line_end=total,
                language=self.language,
                qualname=path,
                resolution="syntactic",
            )
        ]

    def _dependencies(self, path: str, content: str) -> list[tuple[str, str, int, str]]:
        """`(nome, especificação, linha, ecossistema)`."""
        base = os.path.basename(path)
        out: list[tuple[str, str, int, str]] = []
        if base.startswith("requirements") and base.endswith(".txt"):
            for lineno, line in enumerate(content.splitlines(), start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", "-")):
                    continue
                m = _RE_REQUIREMENT.match(stripped)
                if m:
                    out.append((m.group("name"), (m.group("spec") or "").strip(), lineno, "pypi"))
            return out
        if base.startswith("build.gradle") or base.startswith("settings.gradle"):
            for m in _RE_GRADLE_DEP.finditer(content):
                lineno = content.count("\n", 0, m.start()) + 1
                coord = m.group("coord")
                parts = coord.split(":")
                name = ":".join(parts[:2]) if len(parts) >= 2 else coord
                version = parts[2] if len(parts) >= 3 else ""
                out.append((name, version, lineno, "maven"))
            return out
        if path.endswith(".toml"):
            if _tomllib is None:
                self.add_diagnostic(
                    Diagnostic(
                        level="warning",
                        code="toml_unavailable",
                        message=(
                            "runtime sem `tomllib` (Python < 3.11): manifest TOML não foi lido. "
                            "Nenhuma dependência foi inferida por regex para não produzir "
                            "resultado marcado como sintático sem parser."
                        ),
                        scope_examined=f"import de tomllib para ler {path}",
                        path=path,
                        paths=(path,),
                        language=self.language,
                        impact="dependências declaradas neste manifest ficam ausentes",
                    )
                )
                return out
            try:
                data = _tomllib.loads(content)
            except Exception as exc:
                self.add_diagnostic(
                    Diagnostic(
                        level="error",
                        code="toml_invalid",
                        message=f"TOML não parseia: {exc}",
                        scope_examined=f"tomllib.loads sobre {path}",
                        path=path,
                        paths=(path,),
                        language=self.language,
                        impact="dependências e configuração deste manifest ficam ausentes",
                    )
                )
                return out
            lines = content.splitlines()

            def find_line(token: str) -> int:
                for i, line in enumerate(lines, start=1):
                    if token in line:
                        return i
                return 1

            project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
            for dep in project.get("dependencies", []) or []:
                m = _RE_REQUIREMENT.match(str(dep))
                name = m.group("name") if m else str(dep)
                out.append((name, str(dep), find_line(str(dep)), "pypi"))
            optional = project.get("optional-dependencies", {}) or {}
            if isinstance(optional, dict):
                for group, deps in optional.items():
                    for dep in deps or []:
                        m = _RE_REQUIREMENT.match(str(dep))
                        name = m.group("name") if m else str(dep)
                        out.append((name, f"[{group}] {dep}", find_line(str(dep)), "pypi"))
            poetry = (data.get("tool", {}) or {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
            for name, spec in (poetry.get("dependencies", {}) or {}).items():
                out.append((str(name), str(spec), find_line(str(name)), "pypi"))
            for req in (data.get("build-system", {}) or {}).get("requires", []) or []:
                m = _RE_REQUIREMENT.match(str(req))
                name = m.group("name") if m else str(req)
                out.append((name, str(req), find_line(str(req)), "pypi-build"))
        return out

    def references(self, path: str, content: str) -> list[Reference]:
        return [
            Reference(
                from_symbol=path,
                to_name=name,
                kind="import",
                path=path,
                line=lineno,
                resolved=False,
                language=self.language,
                resolution="syntactic",
                reason=f"dependência {eco} declarada; artefato fora do conjunto analisado",
                extra={"spec": spec, "ecosystem": eco},
            )
            for name, spec, lineno, eco in self._dependencies(path, content)
        ]

    def entrypoints(self, path: str, content: str) -> list[Entrypoint]:
        """`[project.scripts]` de `pyproject.toml` é console script: entrada CLI."""
        if not path.endswith(".toml") or _tomllib is None:
            return []
        try:
            data = _tomllib.loads(content)
        except Exception:
            return []
        lines = content.splitlines()
        out: list[Entrypoint] = []
        scripts = (data.get("project", {}) or {}).get("scripts", {}) or {}
        for name, target in scripts.items():
            lineno = next((i for i, line in enumerate(lines, start=1) if str(name) in line), 1)
            out.append(
                Entrypoint(
                    kind="cli",
                    name=str(name),
                    path=path,
                    line=lineno,
                    framework="python-console-script",
                    symbol=str(target),
                    language=self.language,
                    resolution="syntactic",
                    detail=f"console script -> {target}",
                )
            )
        return out

    def configuration(self, path: str, content: str) -> list[ConfigItem]:
        out: list[ConfigItem] = [
            ConfigItem(
                keypath=f"{eco}:{name}",
                value=spec,
                path=path,
                line_start=lineno,
                line_end=lineno,
                kind="dependency",
                source_format=os.path.basename(path),
                language=self.language,
                resolution="syntactic" if not path.endswith(("gradle", "gradle.kts")) else "heuristic",
                extra={"ecosystem": eco},
            )
            for name, spec, lineno, eco in self._dependencies(path, content)
        ]
        if path.endswith(("build.gradle", "build.gradle.kts")):
            self.add_diagnostic(
                Diagnostic(
                    level="warning",
                    code="gradle_heuristic",
                    message=(
                        "build.gradle lido por padrão textual de `implementation \"g:a:v\"`. "
                        "Dependências vindas de variável, catálogo de versões, `platform()` ou "
                        "lógica Groovy/Kotlin não são capturadas."
                    ),
                    scope_examined=f"regex de configurações Gradle sobre {path}",
                    path=path,
                    paths=(path,),
                    language=self.language,
                    impact="lista de dependências pode estar incompleta; não é inventário fechado",
                )
            )
        return out


__all__ = [
    "JsonExtractor",
    "ManifestExtractor",
    "SqlExtractor",
    "XmlExtractor",
    "XmlForbidden",
    "YamlExtractor",
    "parse_sql_ddl",
    "parse_yaml_min",
    "scan_json_positions",
    "walk_xml",
]
