"""Adaptador JavaScript/TypeScript **estrutural**, sem parser externo (§6.2).

Não existe parser de JS na stdlib e o plano proíbe dependência nativa nova no
`.pyz`. A consequência é assumida em vez de disfarçada: este adaptador é
`resolution_level="heuristic"` e **nunca** emite `resolved=True`. A proibição
não é uma promessa deste docstring — está no `Reference.__post_init__` de
`base.py`, que levanta erro para `resolved=True` sob resolução heurística.

Ordem de trabalho, que é o que torna a regra "comentário não é evidência"
verificável (§5.4):

1. `scrub_c_like(..., template_literals=True, regex_literals=True)` apaga
   comentários de linha e de bloco, trocando cada caractere por espaço. O
   texto resultante tem **o mesmo comprimento**, então todo offset e todo
   número de linha continuam válidos.
2. Toda extração roda sobre esse texto limpo. Um `// function foo() {}` não
   produz símbolo porque, no momento da varredura, as palavras já não existem.
3. As strings capturadas na etapa 1 não são descartadas: SQL e templates saem
   como `ConfigItem` (§5.4 proíbe remover literais indiscriminadamente).

Duas versões do texto limpo são mantidas: uma com o conteúdo das strings
(para ler o path de um `import`) e outra sem (para contar profundidade de
chaves e procurar palavras-chave sem que uma string contendo `class` engane a
varredura).
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable

from .base import (
    CodeExtractor,
    ConfigItem,
    Diagnostic,
    Entrypoint,
    Reference,
    SourceFile,
    Symbol,
    build_line_index,
    line_from_index,
    looks_like_sql,
    match_block_end,
    scrub_c_like,
)

_ID = r"[A-Za-z_$][\w$]*"

_RE_IMPORT = re.compile(
    r"(?:^|[^\w$.])import\s+(?:type\s+)?(?:([\w$*{},\s:]+?)\s+from\s+)?[\"']([^\"']+)[\"']"
)
_RE_IMPORT_DYNAMIC = re.compile(r"(?:^|[^\w$.])import\s*\(\s*[\"']([^\"']+)[\"']\s*\)")
_RE_REQUIRE = re.compile(r"(?:^|[^\w$.])require\s*\(\s*[\"']([^\"']+)[\"']\s*\)")
_RE_EXPORT_FROM = re.compile(r"(?:^|[^\w$.])export\s+\*(?:\s+as\s+" + _ID + r")?\s+from\s*[\"']([^\"']+)[\"']")
_RE_EXPORT_NAMED = re.compile(r"(?:^|[^\w$.])export\s*\{([^}]*)\}")
_RE_MODULE_EXPORTS = re.compile(r"(?:^|[^\w$.])(?:module\.exports|exports\.(" + _ID + r"))\s*=")

_RE_FUNC = re.compile(
    r"(?:^|[^\w$.])(?P<exported>export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*(?P<name>" + _ID + r")\s*\("
)
_RE_CLASS = re.compile(
    r"(?:^|[^\w$.])(?P<exported>export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+(?P<name>" + _ID + r")\b"
)
_RE_INTERFACE = re.compile(r"(?:^|[^\w$.])(?P<exported>export\s+)?interface\s+(?P<name>" + _ID + r")\b")
_RE_TYPE = re.compile(r"(?:^|[^\w$.])(?P<exported>export\s+)?type\s+(?P<name>" + _ID + r")\s*=")
_RE_ENUM = re.compile(r"(?:^|[^\w$.])(?P<exported>export\s+)?(?:const\s+)?enum\s+(?P<name>" + _ID + r")\b")
#: `rhs` fica em lookahead de propósito: sem isso o match consumiria as ~80
#: linhas seguintes e `finditer` (que não sobrepõe) pularia a próxima
#: declaração — duas constantes seguidas viravam uma só.
_RE_BINDING = re.compile(
    r"(?:^|[^\w$.])(?P<exported>export\s+)?(?P<decl>const|let|var)\s+(?P<name>" + _ID + r")\s*(?::[^=;\n]+)?=\s*(?=(?P<rhs>.{0,80}))",
    re.S,
)
#: RHS que caracteriza função: `function`, `(...) =>`, `x =>`, com `async` opcional.
_RE_ARROW_RHS = re.compile(
    r"^\s*(?:async\s+)?(?:function\b|(?:\([^)]*\)|" + _ID + r")\s*(?::[^=]*)?=>)"
)

#: Verbos HTTP de express/fastify/koa-router e afins. É reconhecimento de
#: framework por forma da chamada — camada separada da extração sintática
#: (§6.2), e por isso sempre marcado `heuristic`.
_HTTP_VERBS = ("get", "post", "put", "patch", "delete", "del", "head", "options", "all", "use")
_RE_HTTP = re.compile(
    r"\b(?P<obj>" + _ID + r")\.(?P<verb>" + "|".join(_HTTP_VERBS) + r")\s*\(\s*[\"'`](?P<route>[^\"'`]*)[\"'`]"
)
_RE_LISTEN = re.compile(r"\b(?P<obj>" + _ID + r")\.listen\s*\(")
_RE_EVENT = re.compile(
    r"\b(?P<obj>" + _ID + r")\.(?P<meth>on|once|addEventListener|subscribe)\s*\(\s*[\"'](?P<event>[^\"']+)[\"']"
)
_RE_CALL = re.compile(r"\b(?P<name>" + _ID + r"(?:\." + _ID + r")*)\s*\(")

#: Palavras-chave que casam com `nome(` mas não são chamada.
_CALL_STOPWORDS = frozenset(
    {
        "if", "for", "while", "switch", "catch", "function", "return", "typeof",
        "new", "await", "yield", "do", "else", "try", "throw", "delete", "void",
        "in", "of", "case", "with", "super", "constructor", "import", "export",
    }
)

_CONST_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class JavaScriptExtractor(CodeExtractor):
    """JS/TS por varredura léxica. Descobre estrutura; não resolve tipos."""

    language = "javascript"
    extensions = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts")
    resolution_level = "heuristic"

    def __init__(self) -> None:
        super().__init__()
        self._paths: list[str] = []
        self._cache: dict[str, tuple[str, str, str, list[tuple[int, int, str]], list[int]]] = {}

    def reset(self) -> None:
        super().reset()
        self._paths.clear()
        self._cache.clear()

    # ------------------------------------------------------------------
    def inventory(self, files: Iterable[Any]) -> list[SourceFile]:
        sources = super().inventory(files)
        for src in sources:
            if src.path not in self._paths:
                self._paths.append(src.path)
        return sources

    def diagnostics(self) -> list[Diagnostic]:
        """Diagnósticos acumulados **mais** a limitação estrutural do adaptador.

        A limitação é emitida sempre que houve ao menos um arquivo JS/TS: o
        §6.2 exige que "nunca selo de análise completa por fallback regex" —
        então a ausência de resolução é declarada em toda execução, não apenas
        quando algo falha.
        """
        out = super().diagnostics()
        if self._paths:
            out.append(
                Diagnostic(
                    level="warning",
                    code="heuristic_resolution_only",
                    message=(
                        "JavaScript/TypeScript extraído por varredura léxica (sem parser da "
                        "gramática): declarações, imports e rotas são candidatos. Nenhuma chamada, "
                        "tipo ou herança está resolvida; nenhum item sai com resolved=True."
                    ),
                    scope_examined=f"{len(self._paths)} arquivo(s) .js/.jsx/.ts/.tsx/.mjs/.cjs",
                    paths=tuple(self._paths),
                    language=self.language,
                    impact=(
                        "grafo de chamadas JS/TS é candidato; alvos precisam de investigação "
                        "dirigida antes de virar fato com natureza implemented"
                    ),
                )
            )
        return out

    # ------------------------------------------------------------------
    def _prepare(self, path: str, content: str):
        cached = self._cache.get(path)
        if cached is not None and cached[0] == content:
            return cached
        with_strings, literals = scrub_c_like(
            content, template_literals=True, regex_literals=True, keep_strings=True
        )
        struct, _ = scrub_c_like(
            content, template_literals=True, regex_literals=True, keep_strings=False
        )
        starts = build_line_index(content)
        prepared = (content, with_strings, struct, literals, starts)
        self._cache[path] = prepared
        if path not in self._paths:
            self._paths.append(path)
        return prepared

    @staticmethod
    def _depth_at(struct: str, offset: int) -> int:
        return struct.count("{", 0, offset) - struct.count("}", 0, offset)

    # ------------------------------------------------------------------
    def symbols(self, path: str, content: str) -> list[Symbol]:
        _, text, struct, _literals, starts = self._prepare(path, content)
        module_name = os.path.splitext(os.path.basename(path))[0]
        line = lambda off: line_from_index(starts, off)  # noqa: E731
        total = max(1, len(content.splitlines()) or 1)
        out: list[Symbol] = [
            Symbol(
                name=module_name,
                kind="module",
                path=path,
                line_start=1,
                line_end=total,
                language=self.language,
                qualname=module_name,
                resolution="heuristic",
            )
        ]

        class_ranges: list[tuple[str, int, int]] = []
        for m in _RE_CLASS.finditer(struct):
            name = m.group("name")
            brace = struct.find("{", m.end())
            end_off = match_block_end(struct, brace) if brace != -1 else m.end()
            out.append(
                Symbol(
                    name=name,
                    kind="class",
                    path=path,
                    line_start=line(m.start("name")),
                    line_end=line(end_off),
                    parent=module_name,
                    visibility="public" if m.group("exported") else "internal",
                    language=self.language,
                    qualname=f"{module_name}.{name}",
                    resolution="heuristic",
                    extra={"exported": bool(m.group("exported"))},
                )
            )
            if brace != -1:
                class_ranges.append((f"{module_name}.{name}", brace, end_off))

        for m in _RE_FUNC.finditer(struct):
            name = m.group("name")
            owner = _owner_of(class_ranges, m.start())
            brace = struct.find("{", m.end())
            end_off = match_block_end(struct, brace) if brace != -1 else m.end()
            out.append(
                Symbol(
                    name=name,
                    kind="method" if owner else "function",
                    path=path,
                    line_start=line(m.start("name")),
                    line_end=line(end_off),
                    parent=owner or module_name,
                    visibility="public" if m.group("exported") else "internal",
                    language=self.language,
                    qualname=f"{owner or module_name}.{name}",
                    resolution="heuristic",
                    extra={"exported": bool(m.group("exported"))},
                )
            )

        for regex, kind in ((_RE_INTERFACE, "interface"), (_RE_ENUM, "enum"), (_RE_TYPE, "variable")):
            for m in regex.finditer(struct):
                name = m.group("name")
                brace = struct.find("{", m.end()) if kind != "variable" else -1
                end_off = match_block_end(struct, brace) if brace != -1 else m.end()
                out.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        path=path,
                        line_start=line(m.start("name")),
                        line_end=line(end_off),
                        parent=module_name,
                        visibility="public" if m.group("exported") else "internal",
                        language=self.language,
                        qualname=f"{module_name}.{name}",
                        resolution="heuristic",
                        extra={"typescript": True},
                    )
                )

        for m in _RE_BINDING.finditer(struct):
            name = m.group("name")
            rhs = m.group("rhs") or ""
            is_fn = bool(_RE_ARROW_RHS.match(rhs))
            kind = "function" if is_fn else ("const" if m.group("decl") == "const" else "variable")
            start_off = m.start("name")
            end_off = _statement_end(struct, m.end("name"))
            out.append(
                Symbol(
                    name=name,
                    kind=kind,
                    path=path,
                    line_start=line(start_off),
                    line_end=line(end_off),
                    parent=module_name,
                    visibility="public" if m.group("exported") else "internal",
                    language=self.language,
                    qualname=f"{module_name}.{name}",
                    resolution="heuristic",
                    extra={"declaration": m.group("decl"), "exported": bool(m.group("exported"))},
                )
            )

        # Métodos de classe declarados sem a palavra `function`.
        for owner, brace, end_off in class_ranges:
            body = struct[brace : end_off + 1]
            for m in re.finditer(
                r"(?:^|[\n;{}])\s*(?:(?:static|async|public|private|protected|readonly|get|set)\s+)*"
                r"(?P<name>" + _ID + r")\s*\([^)]*\)\s*(?::[^{;]+)?\{",
                body,
            ):
                name = m.group("name")
                if name in _CALL_STOPWORDS and name != "constructor":
                    continue
                abs_off = brace + m.start("name")
                out.append(
                    Symbol(
                        name=name,
                        kind="method",
                        path=path,
                        line_start=line(abs_off),
                        line_end=line(brace + match_block_end(body, m.end() - 1)),
                        parent=owner,
                        visibility="private" if name.startswith("#") or name.startswith("_") else "public",
                        language=self.language,
                        qualname=f"{owner}.{name}",
                        resolution="heuristic",
                    )
                )
        return _dedup_symbols(out)

    # ------------------------------------------------------------------
    def references(self, path: str, content: str) -> list[Reference]:
        _, text, struct, _literals, starts = self._prepare(path, content)
        module_name = os.path.splitext(os.path.basename(path))[0]
        line = lambda off: line_from_index(starts, off)  # noqa: E731
        out: list[Reference] = []

        def add(to_name: str, kind: str, off: int, reason: str, extra: dict | None = None) -> None:
            out.append(
                Reference(
                    from_symbol=module_name,
                    to_name=to_name,
                    kind=kind,
                    path=path,
                    line=line(off),
                    resolved=False,  # heurística nunca resolve (base.Reference)
                    language=self.language,
                    resolution="heuristic",
                    reason=reason,
                    extra=extra or {},
                )
            )

        for m in _RE_IMPORT.finditer(text):
            add(
                m.group(2),
                "import",
                m.start(2),
                "especificador de módulo não resolvido a arquivo do escopo",
                {"bindings": (m.group(1) or "").strip()},
            )
        for m in _RE_IMPORT_DYNAMIC.finditer(text):
            add(m.group(1), "import", m.start(1), "import() dinâmico", {"dynamic": True})
        for m in _RE_REQUIRE.finditer(text):
            add(m.group(1), "import", m.start(1), "require() CommonJS não resolvido", {"commonjs": True})
        for m in _RE_EXPORT_FROM.finditer(text):
            add(m.group(1), "export", m.start(1), "re-export de módulo não resolvido")
        for m in _RE_EXPORT_NAMED.finditer(struct):
            for piece in m.group(1).split(","):
                name = piece.strip().split(" as ")[0].strip()
                if name:
                    add(name, "export", m.start(1), "nome exportado")
        for m in _RE_MODULE_EXPORTS.finditer(struct):
            add(m.group(1) or "module.exports", "export", m.start(), "export CommonJS")

        for m in _RE_CLASS.finditer(struct):
            tail = struct[m.end() : m.end() + 200]
            ext = re.match(r"\s*extends\s+(" + _ID + r"(?:\." + _ID + r")*)", tail)
            if ext:
                out.append(
                    Reference(
                        from_symbol=f"{module_name}.{m.group('name')}",
                        to_name=ext.group(1),
                        kind="inherit",
                        path=path,
                        line=line(m.end() + ext.start(1)),
                        resolved=False,
                        language=self.language,
                        resolution="heuristic",
                        reason="superclasse não resolvida a declaração do escopo",
                    )
                )
            impl = re.search(r"\bimplements\s+([\w$.,\s]+?)\{", tail)
            if impl:
                for name in impl.group(1).split(","):
                    name = name.strip()
                    if name:
                        out.append(
                            Reference(
                                from_symbol=f"{module_name}.{m.group('name')}",
                                to_name=name,
                                kind="implement",
                                path=path,
                                line=line(m.end() + impl.start(1)),
                                resolved=False,
                                language=self.language,
                                resolution="heuristic",
                                reason="interface não resolvida",
                            )
                        )

        for m in _RE_CALL.finditer(struct):
            name = m.group("name")
            head = name.split(".")[0]
            if head in _CALL_STOPWORDS:
                continue
            depth = self._depth_at(struct, m.start())
            out.append(
                Reference(
                    from_symbol=module_name,
                    to_name=name,
                    kind="call",
                    path=path,
                    line=line(m.start("name")),
                    resolved=False,
                    language=self.language,
                    resolution="heuristic",
                    reason="chamada candidata: receptor e alvo não resolvidos sem parser/tipos",
                    extra={"brace_depth": depth, "top_level": depth == 0},
                )
            )
        return out

    # ------------------------------------------------------------------
    def entrypoints(self, path: str, content: str) -> list[Entrypoint]:
        _, text, struct, _literals, starts = self._prepare(path, content)
        module_name = os.path.splitext(os.path.basename(path))[0]
        line = lambda off: line_from_index(starts, off)  # noqa: E731
        out: list[Entrypoint] = []

        for m in _RE_HTTP.finditer(text):
            obj, verb, route = m.group("obj"), m.group("verb"), m.group("route")
            if verb == "use" and not route.startswith("/"):
                continue
            out.append(
                Entrypoint(
                    kind="http",
                    name=f"{verb.upper()} {route}",
                    path=path,
                    line=line(m.start("obj")),
                    framework="express-like",
                    symbol=f"{module_name}.{obj}",
                    language=self.language,
                    resolution="heuristic",
                    detail=(
                        f"forma `{obj}.{verb}('{route}', ...)`; framework inferido pela forma da "
                        "chamada, não por resolução do import"
                    ),
                    extra={"verb": verb.upper(), "route": route, "receiver": obj},
                )
            )
        for m in _RE_LISTEN.finditer(struct):
            out.append(
                Entrypoint(
                    kind="http",
                    name=f"{m.group('obj')}.listen",
                    path=path,
                    line=line(m.start("obj")),
                    framework="express-like",
                    symbol=f"{module_name}.{m.group('obj')}",
                    language=self.language,
                    resolution="heuristic",
                    detail="abertura de porta HTTP candidata",
                )
            )
        for m in _RE_EVENT.finditer(text):
            out.append(
                Entrypoint(
                    kind="event",
                    name=m.group("event"),
                    path=path,
                    line=line(m.start("obj")),
                    framework=None,
                    symbol=f"{module_name}.{m.group('obj')}",
                    language=self.language,
                    resolution="heuristic",
                    detail=f"`{m.group('obj')}.{m.group('meth')}('{m.group('event')}', ...)`",
                )
            )
        for regex, what in ((_RE_FUNC, "função"), (_RE_CLASS, "classe")):
            for m in regex.finditer(struct):
                if not m.group("exported"):
                    continue
                out.append(
                    Entrypoint(
                        kind="public_api",
                        name=m.group("name"),
                        path=path,
                        line=line(m.start("name")),
                        symbol=f"{module_name}.{m.group('name')}",
                        language=self.language,
                        resolution="heuristic",
                        detail=f"{what} exportada do módulo",
                    )
                )
        for m in _RE_BINDING.finditer(struct):
            if m.group("exported"):
                out.append(
                    Entrypoint(
                        kind="public_api",
                        name=m.group("name"),
                        path=path,
                        line=line(m.start("name")),
                        symbol=f"{module_name}.{m.group('name')}",
                        language=self.language,
                        resolution="heuristic",
                        detail="binding exportado do módulo",
                    )
                )
        return out

    # ------------------------------------------------------------------
    def configuration(self, path: str, content: str) -> list[ConfigItem]:
        _, text, struct, literals, starts = self._prepare(path, content)
        module_name = os.path.splitext(os.path.basename(path))[0]
        line = lambda off: line_from_index(starts, off)  # noqa: E731
        out: list[ConfigItem] = []

        for start, end, value in literals:
            if looks_like_sql(value):
                out.append(
                    ConfigItem(
                        keypath=f"{module_name}#sql@{line(start)}",
                        value=value.strip(),
                        path=path,
                        line_start=line(start),
                        line_end=line(max(start, end - 1)),
                        kind="sql_literal",
                        source_format="javascript",
                        language=self.language,
                        resolution="heuristic",
                        extra={"detector": "looks_like_sql"},
                    )
                )
            elif "${" in value:
                out.append(
                    ConfigItem(
                        keypath=f"{module_name}#template@{line(start)}",
                        value=value,
                        path=path,
                        line_start=line(start),
                        line_end=line(max(start, end - 1)),
                        kind="template",
                        source_format="javascript",
                        language=self.language,
                        resolution="heuristic",
                        extra={"interpolated": True},
                    )
                )

        for m in _RE_BINDING.finditer(struct):
            name = m.group("name")
            if m.group("decl") != "const" or not _CONST_NAME.match(name):
                continue
            end_off = _statement_end(text, m.end("name"))
            out.append(
                ConfigItem(
                    keypath=f"{module_name}.{name}",
                    value=text[m.end("name") : end_off].lstrip(" =:").strip().rstrip(";"),
                    path=path,
                    line_start=line(m.start("name")),
                    line_end=line(end_off),
                    kind="config",
                    source_format="javascript",
                    language=self.language,
                    resolution="heuristic",
                )
            )
        return out


class TypeScriptExtractor(JavaScriptExtractor):
    """Mesma varredura, reportada como linguagem própria na matriz de cobertura."""

    language = "typescript"
    extensions = (".ts", ".tsx", ".mts", ".cts")


# ----------------------------------------------------------------------


def _owner_of(ranges: list[tuple[str, int, int]], offset: int) -> str | None:
    for name, start, end in ranges:
        if start <= offset <= end:
            return name
    return None


def _statement_end(text: str, offset: int) -> int:
    """Fim aproximado da instrução: `;` ou quebra de linha em profundidade zero."""
    depth = 0
    i = offset
    n = len(text)
    while i < n:
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                return i
        elif c == ";" and depth == 0:
            return i
        elif c == "\n" and depth == 0:
            return i
        i += 1
    return n - 1 if n else 0


def _dedup_symbols(symbols: list[Symbol]) -> list[Symbol]:
    seen: set[tuple[str, str, int]] = set()
    out: list[Symbol] = []
    for sym in symbols:
        key = (sym.qualname, sym.kind, sym.line_start)
        if key in seen:
            continue
        seen.add(key)
        out.append(sym)
    return out


__all__ = ["JavaScriptExtractor", "TypeScriptExtractor"]
