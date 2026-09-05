"""Adaptador Java **estrutural**, sem parser externo (§6.2).

Mesma disciplina do adaptador JS/TS e pelo mesmo motivo: a stdlib não tem
parser de Java e o `.pyz` não pode embutir dependência nativa. Logo
`resolution_level="heuristic"` e nenhum item sai com `resolved=True` — a
recusa é aplicada por `Reference.__post_init__` em `base.py`, não por
convenção.

Ordem de trabalho:

1. `scrub_c_like` apaga comentários de linha, de bloco e Javadoc, trocando
   cada caractere por espaço. Javadoc é comentário: `/** Cria o pedido. */`
   não gera símbolo nem regra (§5.4). Como o texto limpo tem o mesmo
   comprimento, linhas e offsets seguem válidos.
2. Varredura sobre o texto limpo: `package`, `import`, tipos, métodos, campos.
3. Anotações são lidas na camada de **reconhecimento de framework**, separada
   da extração estrutural (§6.2): `@GetMapping`, `@KafkaListener`,
   `@Scheduled` etc. viram `Entrypoint` com `framework` anotado e resolução
   heurística.
4. Strings capturadas na etapa 1 sobrevivem: SQL em campo constante e queries
   inline saem como `ConfigItem` (§5.4).
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

_MODIFIER = r"public|protected|private|static|final|abstract|synchronized|native|default|strictfp|sealed|non-sealed|transient|volatile"

_RE_PACKAGE = re.compile(r"(?:^|[;\n])\s*package\s+(?P<name>[\w.]+)\s*;")
_RE_IMPORT = re.compile(r"(?:^|[;\n])\s*import\s+(?P<static>static\s+)?(?P<name>[\w.*]+)\s*;")
_RE_TYPE = re.compile(
    r"(?:^|[^\w$@.])(?P<mods>(?:(?:" + _MODIFIER + r")\s+)*)"
    r"(?P<kw>class|interface|enum|record|@interface)\s+(?P<name>\w+)"
    r"(?P<tail>[^{;]*)"
)
_RE_METHOD = re.compile(
    r"(?:^|[\n;{}])\s*(?P<mods>(?:(?:" + _MODIFIER + r")\s+)*)"
    r"(?P<ret>(?:[\w$.]+(?:\s*<[^;{}()]*>)?(?:\s*\[\s*\])*)\s+)?"
    r"(?P<name>\w+)\s*\((?P<params>[^;{}()]*)\)\s*"
    r"(?P<throws>throws\s+[\w.,\s]+)?\s*(?P<body>[{;])"
)
_RE_FIELD = re.compile(
    r"(?:^|[\n;{}])\s*(?P<mods>(?:(?:" + _MODIFIER + r")\s+)+)"
    r"(?P<type>[\w$.]+(?:\s*<[^;{}]*>)?(?:\s*\[\s*\])*)\s+(?P<name>\w+)\s*"
    r"(?:=\s*(?P<value>[^;]*))?;"
)
_RE_ANNOTATION = re.compile(r"@(?P<name>\w+)(?P<args>\s*\((?:[^()]|\([^()]*\))*\))?")
_RE_EXTENDS = re.compile(r"\bextends\s+(?P<name>[\w$.]+(?:\s*<[^{]*>)?)")
_RE_IMPLEMENTS = re.compile(r"\bimplements\s+(?P<names>[\w$.,\s<>]+?)(?:\{|$)")
_RE_NEW = re.compile(r"\bnew\s+(?P<name>[\w$.]+)\s*\(")
_RE_CALL = re.compile(r"(?P<recv>[\w$.]+)\.(?P<name>\w+)\s*\(")

#: Palavras que casam com a forma de método/chamada mas não são declaração.
_JAVA_KEYWORDS = frozenset(
    {
        "if", "for", "while", "switch", "catch", "return", "new", "throw", "do",
        "else", "try", "synchronized", "super", "this", "assert", "instanceof",
        "case", "break", "continue", "yield", "record", "class", "interface", "enum",
    }
)

#: Reconhecimento de framework por anotação (camada separada, §6.2).
#: anotação -> (kind de Entrypoint, framework)
ANNOTATION_ENTRYPOINTS: dict[str, tuple[str, str]] = {
    "RequestMapping": ("http", "spring-web"),
    "GetMapping": ("http", "spring-web"),
    "PostMapping": ("http", "spring-web"),
    "PutMapping": ("http", "spring-web"),
    "DeleteMapping": ("http", "spring-web"),
    "PatchMapping": ("http", "spring-web"),
    "Path": ("http", "jax-rs"),
    "GET": ("http", "jax-rs"),
    "POST": ("http", "jax-rs"),
    "PUT": ("http", "jax-rs"),
    "DELETE": ("http", "jax-rs"),
    "Scheduled": ("job", "spring-scheduling"),
    "SchedulerLock": ("job", "shedlock"),
    "KafkaListener": ("event", "spring-kafka"),
    "RabbitListener": ("event", "spring-amqp"),
    "JmsListener": ("event", "spring-jms"),
    "StreamListener": ("event", "spring-cloud-stream"),
    "SqsListener": ("event", "spring-cloud-aws"),
    "EventListener": ("event", "spring-context"),
    "TransactionalEventListener": ("event", "spring-context"),
    "PostConstruct": ("callback", "jakarta-annotation"),
    "PreDestroy": ("callback", "jakarta-annotation"),
    "GrpcService": ("rpc", "grpc-spring"),
    "WebService": ("rpc", "jax-ws"),
    "MessageMapping": ("rpc", "spring-messaging"),
    "FeignClient": ("rpc", "spring-cloud-openfeign"),
}

#: Anotações que marcam o **tipo** como porta de entrada HTTP.
CONTROLLER_ANNOTATIONS = frozenset({"RestController", "Controller", "RepositoryRestResource", "Path"})


def _visibility(mods: str) -> str:
    tokens = set(mods.split())
    if "public" in tokens:
        return "public"
    if "protected" in tokens:
        return "protected"
    if "private" in tokens:
        return "private"
    return "package"


def _annotation_args(args: str | None) -> str:
    if not args:
        return ""
    return args.strip()[1:-1].strip()


def _route_from_args(args: str) -> str:
    """Primeiro literal de `@GetMapping("/x")` ou `value = "/x"` / `path = "/x"`."""
    m = re.search(r'(?:^|[,(\s])(?:value|path)\s*=\s*"([^"]*)"', args)
    if m:
        return m.group(1)
    m = re.search(r'"([^"]*)"', args)
    return m.group(1) if m else ""


class JavaExtractor(CodeExtractor):
    """Java por varredura léxica. Descobre estrutura e frameworks; não resolve tipos."""

    language = "java"
    extensions = (".java",)
    resolution_level = "heuristic"

    def __init__(self) -> None:
        super().__init__()
        self._paths: list[str] = []
        self._cache: dict[str, tuple[str, str, list[tuple[int, int, str]], list[int], str]] = {}
        self._ann_cache: dict[tuple[str, int], list[tuple[int, int, str, str]]] = {}

    def reset(self) -> None:
        super().reset()
        self._paths.clear()
        self._cache.clear()
        self._ann_cache.clear()

    def inventory(self, files: Iterable[Any]) -> list[SourceFile]:
        sources = super().inventory(files)
        for src in sources:
            if src.path not in self._paths:
                self._paths.append(src.path)
        return sources

    def diagnostics(self) -> list[Diagnostic]:
        out = super().diagnostics()
        if self._paths:
            out.append(
                Diagnostic(
                    level="warning",
                    code="heuristic_resolution_only",
                    message=(
                        "Java extraído por varredura léxica (sem parser da gramática): tipos, "
                        "métodos e anotações são candidatos. Sobrecarga, generics, herança e "
                        "injeção de dependência não estão resolvidos; nenhum item sai resolved=True."
                    ),
                    scope_examined=f"{len(self._paths)} arquivo(s) .java",
                    paths=tuple(self._paths),
                    language=self.language,
                    impact=(
                        "rotas e listeners derivados de anotação são pistas de entrada; o alvo "
                        "efetivo exige investigação dirigida antes de virar fato implemented"
                    ),
                )
            )
        return out

    # ------------------------------------------------------------------
    def _prepare(self, path: str, content: str):
        cached = self._cache.get(path)
        if cached is not None and cached[0] == content:
            return cached
        struct, literals = scrub_c_like(content, template_literals=False, keep_strings=False)
        # Segunda passada preservando o corpo das strings: `struct` apaga o
        # conteúdo (para que uma string com `class` não engane a varredura),
        # mas os argumentos de anotação — `@GetMapping("/{id}")` — precisam do
        # texto real. Os dois têm o mesmo comprimento, então os offsets valem
        # nos dois.
        text, _ = scrub_c_like(content, template_literals=False, keep_strings=True)
        starts = build_line_index(content)
        prepared = (content, struct, literals, starts, text)
        self._cache[path] = prepared
        if path not in self._paths:
            self._paths.append(path)
        return prepared

    def _package(self, struct: str) -> str:
        m = _RE_PACKAGE.search(struct)
        return m.group("name") if m else ""

    def _types(self, struct: str) -> list[tuple[str, str, str, int, int, int, str]]:
        """`(nome, kw, mods, offset_nome, offset_abertura, offset_fim, tail)`."""
        out = []
        for m in _RE_TYPE.finditer(struct):
            brace = struct.find("{", m.end("name"))
            if brace == -1:
                continue
            end = match_block_end(struct, brace)
            out.append(
                (m.group("name"), m.group("kw"), m.group("mods") or "", m.start("name"), brace, end, m.group("tail") or "")
            )
        return out

    def _annotation_spans(self, path: str, struct: str, text: str) -> list[tuple[int, int, str, str]]:
        """Todas as anotações do arquivo: `(início, fim, nome, args)`, ordenadas.

        Posição e nome vêm de `struct` (sem conteúdo de string, para não achar
        anotação dentro de um literal); os **argumentos** vêm de `text`, no
        mesmo intervalo, porque é lá que `"/api/orders"` ainda existe.
        """
        key = (path, len(struct))
        cached = self._ann_cache.get(key)
        if cached is not None:
            return cached
        spans = []
        for m in _RE_ANNOTATION.finditer(struct):
            if m.group("args") is None:
                args = ""
            else:
                args = _annotation_args(text[m.start("args") : m.end("args")])
            spans.append((m.start(), m.end(), m.group("name"), args))
        self._ann_cache[key] = spans
        return spans

    def _annotations_before(self, struct: str, offset: int, spans: list[tuple[int, int, str, str]]) -> list[tuple[str, str, int]]:
        """Anotações contíguas imediatamente antes de `offset`.

        Anda para trás enquanto só houver espaço em branco entre uma anotação e
        a próxima posição: `@Transactional\\n@GetMapping("/x")\\npublic ...`
        fica ligado ao método, e a anotação de outro membro (separada por
        código) não é capturada por engano.
        """
        import bisect

        ends = [s[1] for s in spans]
        found: list[tuple[str, str, int]] = []
        cursor = offset
        while True:
            idx = bisect.bisect_right(ends, cursor) - 1
            if idx < 0:
                break
            start, end, name, args = spans[idx]
            if end > cursor or struct[end:cursor].strip():
                break
            found.append((name, args, start))
            cursor = start
        found.reverse()
        return found

    # ------------------------------------------------------------------
    def symbols(self, path: str, content: str) -> list[Symbol]:
        _, struct, _lit, starts, text = self._prepare(path, content)
        line = lambda off: line_from_index(starts, off)  # noqa: E731
        pkg = self._package(struct)
        base = pkg or os.path.splitext(os.path.basename(path))[0]
        total = max(1, len(content.splitlines()) or 1)
        ann_spans = self._annotation_spans(path, struct, text)
        out: list[Symbol] = []
        if pkg:
            pm = _RE_PACKAGE.search(struct)
            out.append(
                Symbol(
                    name=pkg,
                    kind="package",
                    path=path,
                    line_start=line(pm.start("name")),
                    line_end=total,
                    language=self.language,
                    qualname=pkg,
                    resolution="heuristic",
                )
            )

        types = self._types(struct)
        for name, kw, mods, name_off, brace, end, tail in types:
            kind = {"class": "class", "interface": "interface", "enum": "enum", "record": "record"}.get(kw, "interface")
            annotations = self._annotations_before(struct, _decl_start(struct, name_off, mods), ann_spans)
            out.append(
                Symbol(
                    name=name,
                    kind=kind,
                    path=path,
                    line_start=line(name_off),
                    line_end=line(end),
                    parent=pkg or None,
                    visibility=_visibility(mods),
                    language=self.language,
                    qualname=f"{base}.{name}",
                    signature=f"{mods.strip()} {kw} {name}{tail.strip()}".strip(),
                    decorators=tuple("@" + a for a, _args, _o in annotations),
                    resolution="heuristic",
                    extra={"modifiers": tuple(mods.split()), "keyword": kw},
                )
            )
            body = struct[brace : end + 1]
            for m in _RE_METHOD.finditer(body):
                mname = m.group("name")
                if mname in _JAVA_KEYWORDS:
                    continue
                ret = (m.group("ret") or "").strip()
                if ret in _JAVA_KEYWORDS:
                    continue
                if not ret and mname != name:
                    continue  # sem tipo de retorno só é válido para construtor
                abs_name = brace + m.start("name")
                if m.group("body") == "{":
                    m_end = brace + match_block_end(body, brace_index(body, m.end("body") - 1))
                else:
                    m_end = brace + m.end()
                mmods = m.group("mods") or ""
                mann = self._annotations_before(struct, _decl_start(struct, abs_name, mmods), ann_spans)
                out.append(
                    Symbol(
                        name=mname,
                        kind="method",
                        path=path,
                        line_start=line(abs_name),
                        line_end=line(max(abs_name, m_end)),
                        parent=f"{base}.{name}",
                        visibility=_visibility(mmods),
                        language=self.language,
                        qualname=f"{base}.{name}.{mname}",
                        signature=f"{ret} {mname}({(m.group('params') or '').strip()})".strip(),
                        decorators=tuple("@" + a for a, _args, _o in mann),
                        resolution="heuristic",
                        extra={
                            "modifiers": tuple(mmods.split()),
                            "constructor": mname == name,
                            "abstract": m.group("body") == ";",
                        },
                    )
                )
            for m in _RE_FIELD.finditer(body):
                fname = m.group("name")
                if fname in _JAVA_KEYWORDS or m.group("type") in _JAVA_KEYWORDS:
                    continue
                abs_name = brace + m.start("name")
                mods_f = m.group("mods") or ""
                is_const = "final" in mods_f and "static" in mods_f
                out.append(
                    Symbol(
                        name=fname,
                        kind="const" if is_const else "field",
                        path=path,
                        line_start=line(abs_name),
                        line_end=line(brace + m.end()),
                        parent=f"{base}.{name}",
                        visibility=_visibility(mods_f),
                        language=self.language,
                        qualname=f"{base}.{name}.{fname}",
                        signature=f"{m.group('type')} {fname}",
                        resolution="heuristic",
                        extra={"modifiers": tuple(mods_f.split())},
                    )
                )
        return out

    # ------------------------------------------------------------------
    def references(self, path: str, content: str) -> list[Reference]:
        _, struct, _lit, starts, text = self._prepare(path, content)
        line = lambda off: line_from_index(starts, off)  # noqa: E731
        pkg = self._package(struct)
        base = pkg or os.path.splitext(os.path.basename(path))[0]
        out: list[Reference] = []

        for m in _RE_IMPORT.finditer(struct):
            out.append(
                Reference(
                    from_symbol=base,
                    to_name=m.group("name"),
                    kind="import",
                    path=path,
                    line=line(m.start("name")),
                    resolved=False,
                    language=self.language,
                    resolution="heuristic",
                    reason="import não confrontado com o conjunto analisado (sem índice de classpath)",
                    extra={"static": bool(m.group("static")), "wildcard": m.group("name").endswith(".*")},
                )
            )

        types = self._types(struct)
        for name, _kw, _mods, name_off, brace, end, tail in types:
            qual = f"{base}.{name}"
            ext = _RE_EXTENDS.search(tail)
            if ext:
                out.append(
                    Reference(
                        from_symbol=qual,
                        to_name=ext.group("name").strip(),
                        kind="inherit",
                        path=path,
                        line=line(name_off),
                        resolved=False,
                        language=self.language,
                        resolution="heuristic",
                        reason="superclasse não resolvida",
                    )
                )
            impl = _RE_IMPLEMENTS.search(tail + "{")
            if impl:
                for iface in impl.group("names").split(","):
                    iface = iface.strip()
                    if iface:
                        out.append(
                            Reference(
                                from_symbol=qual,
                                to_name=iface,
                                kind="implement",
                                path=path,
                                line=line(name_off),
                                resolved=False,
                                language=self.language,
                                resolution="heuristic",
                                reason="interface não resolvida",
                            )
                        )
            body = struct[brace : end + 1]
            for m in _RE_CALL.finditer(body):
                out.append(
                    Reference(
                        from_symbol=qual,
                        to_name=f"{m.group('recv')}.{m.group('name')}",
                        kind="call",
                        path=path,
                        line=line(brace + m.start("recv")),
                        resolved=False,
                        language=self.language,
                        resolution="heuristic",
                        reason="chamada candidata: receptor, sobrecarga e herança não resolvidos",
                        extra={"receiver": m.group("recv")},
                    )
                )
            for m in _RE_NEW.finditer(body):
                out.append(
                    Reference(
                        from_symbol=qual,
                        to_name=m.group("name"),
                        kind="call",
                        path=path,
                        line=line(brace + m.start("name")),
                        resolved=False,
                        language=self.language,
                        resolution="heuristic",
                        reason="instanciação candidata",
                        extra={"constructor": True},
                    )
                )

        for start, _end, name, args in self._annotation_spans(path, struct, text):
            out.append(
                Reference(
                    from_symbol=base,
                    to_name=name,
                    kind="annotation",
                    path=path,
                    line=line(start),
                    resolved=False,
                    language=self.language,
                    resolution="heuristic",
                    reason="anotação não resolvida ao tipo declarante",
                    extra={"args": args},
                )
            )
        return out

    # ------------------------------------------------------------------
    def entrypoints(self, path: str, content: str) -> list[Entrypoint]:
        _, struct, _lit, starts, text = self._prepare(path, content)
        line = lambda off: line_from_index(starts, off)  # noqa: E731
        pkg = self._package(struct)
        base = pkg or os.path.splitext(os.path.basename(path))[0]
        ann_spans = self._annotation_spans(path, struct, text)
        out: list[Entrypoint] = []

        for name, kw, mods, name_off, brace, end, _tail in self._types(struct):
            qual = f"{base}.{name}"
            type_ann = self._annotations_before(struct, _decl_start(struct, name_off, mods), ann_spans)
            type_ann_names = {a for a, _args, _o in type_ann}
            base_route = ""
            for ann, args, off in type_ann:
                if ann in ("RequestMapping", "Path"):
                    base_route = _route_from_args(args)
                kind_fw = ANNOTATION_ENTRYPOINTS.get(ann)
                if ann in CONTROLLER_ANNOTATIONS or (kind_fw and kind_fw[0] == "http"):
                    out.append(
                        Entrypoint(
                            kind="http",
                            name=f"{qual}{(' ' + base_route) if base_route else ''}",
                            path=path,
                            line=line(off),
                            framework=(kind_fw[1] if kind_fw else "spring-web"),
                            symbol=qual,
                            language=self.language,
                            resolution="heuristic",
                            detail=f"tipo anotado com @{ann}; rota base={base_route or '(não declarada)'}",
                            extra={"annotation": ann, "base_route": base_route},
                        )
                    )
                elif kind_fw:
                    out.append(
                        Entrypoint(
                            kind=kind_fw[0],
                            name=f"{qual}@{ann}",
                            path=path,
                            line=line(off),
                            framework=kind_fw[1],
                            symbol=qual,
                            language=self.language,
                            resolution="heuristic",
                            detail=f"tipo anotado com @{ann}({args})",
                            extra={"annotation": ann, "args": args},
                        )
                    )

            body = struct[brace : end + 1]
            for m in _RE_METHOD.finditer(body):
                mname = m.group("name")
                if mname in _JAVA_KEYWORDS:
                    continue
                mmods = m.group("mods") or ""
                params = (m.group("params") or "").strip()
                abs_name = brace + m.start("name")
                mann = self._annotations_before(struct, _decl_start(struct, abs_name, mmods), ann_spans)
                mqual = f"{qual}.{mname}"

                if mname == "main" and "static" in mmods and "public" in mmods and "String" in params:
                    out.append(
                        Entrypoint(
                            kind="main",
                            name=mqual,
                            path=path,
                            line=line(abs_name),
                            framework=None,
                            symbol=mqual,
                            language=self.language,
                            resolution="heuristic",
                            detail="public static void main(String[])",
                        )
                    )

                matched = False
                for ann, args, off in mann:
                    kind_fw = ANNOTATION_ENTRYPOINTS.get(ann)
                    if not kind_fw:
                        continue
                    matched = True
                    kind, framework = kind_fw
                    if kind == "http":
                        verb = _VERB_BY_ANNOTATION.get(ann, "ANY")
                        route = _route_from_args(args)
                        if route and base_route:
                            full = base_route.rstrip("/") + "/" + route.lstrip("/")
                        else:
                            full = route or base_route
                        out.append(
                            Entrypoint(
                                kind="http",
                                name=f"{verb} {full or '/'}",
                                path=path,
                                line=line(off),
                                framework=framework,
                                symbol=mqual,
                                language=self.language,
                                resolution="heuristic",
                                detail=f"@{ann}({args}) sobre {mqual}",
                                extra={"annotation": ann, "verb": verb, "route": full, "args": args},
                            )
                        )
                    else:
                        out.append(
                            Entrypoint(
                                kind=kind,
                                name=f"{mqual}@{ann}",
                                path=path,
                                line=line(off),
                                framework=framework,
                                symbol=mqual,
                                language=self.language,
                                resolution="heuristic",
                                detail=f"@{ann}({args}) sobre {mqual}",
                                extra={"annotation": ann, "args": args},
                            )
                        )
                if (
                    not matched
                    and "public" in mmods
                    and "public" in mods
                    and mname != name
                    and not type_ann_names & CONTROLLER_ANNOTATIONS
                ):
                    out.append(
                        Entrypoint(
                            kind="public_api",
                            name=mqual,
                            path=path,
                            line=line(abs_name),
                            framework=None,
                            symbol=mqual,
                            language=self.language,
                            resolution="heuristic",
                            detail=f"método público de tipo público ({kw})",
                        )
                    )
        return out

    # ------------------------------------------------------------------
    def configuration(self, path: str, content: str) -> list[ConfigItem]:
        _, struct, literals, starts, text = self._prepare(path, content)
        line = lambda off: line_from_index(starts, off)  # noqa: E731
        pkg = self._package(struct)
        base = pkg or os.path.splitext(os.path.basename(path))[0]
        out: list[ConfigItem] = []

        # Literais executáveis (§5.4): SQL inline ou em campo constante.
        for start, end, value in literals:
            if looks_like_sql(value):
                out.append(
                    ConfigItem(
                        keypath=f"{base}#sql@{line(start)}",
                        value=value.strip(),
                        path=path,
                        line_start=line(start),
                        line_end=line(max(start, end - 1)),
                        kind="sql_literal",
                        source_format="java",
                        language=self.language,
                        resolution="heuristic",
                        extra={"detector": "looks_like_sql"},
                    )
                )

        for name, _kw, _mods, _off, brace, end, _tail in self._types(struct):
            body_struct = struct[brace : end + 1]
            body_raw = content[brace : end + 1]
            for m in _RE_FIELD.finditer(body_struct):
                mods_f = m.group("mods") or ""
                if not ("final" in mods_f and "static" in mods_f):
                    continue
                raw_value = body_raw[m.start("value") : m.end("value")] if m.group("value") is not None else ""
                joined = " ".join(re.findall(r'"([^"]*)"', raw_value))
                kind = "sql_literal" if looks_like_sql(joined) else "config"
                out.append(
                    ConfigItem(
                        keypath=f"{base}.{name}.{m.group('name')}",
                        value=(joined or raw_value.strip()),
                        path=path,
                        line_start=line(brace + m.start("name")),
                        line_end=line(brace + m.end()),
                        kind=kind,
                        source_format="java",
                        language=self.language,
                        resolution="heuristic",
                        extra={"type": m.group("type"), "modifiers": tuple(mods_f.split())},
                    )
                )
        return out


#: Verbo HTTP implícito na anotação Spring/JAX-RS.
_VERB_BY_ANNOTATION: dict[str, str] = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "ANY",
    "GET": "GET",
    "POST": "POST",
    "PUT": "PUT",
    "DELETE": "DELETE",
    "Path": "ANY",
}


def _decl_start(struct: str, name_offset: int, mods: str) -> int:
    """Offset do início da declaração (antes dos modificadores)."""
    cursor = name_offset
    head = struct.rfind(mods.strip(), max(0, name_offset - len(mods) - 80), name_offset) if mods.strip() else -1
    if head != -1:
        cursor = head
    # Recua também sobre o tipo de retorno / palavra-chave
    while cursor > 0 and struct[cursor - 1] not in "\n;{}@)":
        cursor -= 1
    return cursor


def brace_index(text: str, offset: int) -> int:
    """Offset da `{` em/depois de `offset` (o regex de método já a consumiu)."""
    idx = text.find("{", max(0, offset - 1))
    return idx if idx != -1 else offset


__all__ = ["ANNOTATION_ENTRYPOINTS", "CONTROLLER_ANNOTATIONS", "JavaExtractor"]
