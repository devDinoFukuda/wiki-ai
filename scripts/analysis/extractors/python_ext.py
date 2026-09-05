"""Adaptador Python completo, via `ast` da stdlib (§6.2).

É o único adaptador desta primeira leva com `resolution_level="syntactic"`:
existe um parser da gramática real da linguagem, então `resolved=True` aqui
significa **alvo verificado no conjunto analisado**, não palpite.

Como a resolução funciona (e por que o passo `inventory` é obrigatório):

1. `inventory(files)` parseia **todos** os arquivos de uma vez e monta o índice
   global — nome de módulo por arquivo, nomes de topo exportados por módulo e
   métodos por classe.
2. Só depois `references()` consegue dizer se `from .models import Evidence`
   aponta para algo que existe. Sem o passo 1, todo import e toda chamada
   sairiam com `resolved=False`, porque não haveria com o que confrontar.

O que **nunca** vira chamada confirmada (§5.5): atributo de objeto de tipo
desconhecido, `getattr`, despacho por dicionário, método herdado que não está
na própria classe. Esses saem com `resolved=False` e `reason` preenchido —
viram lacuna rastreável, não aresta falsa.

Docstrings: `_docstring_node_ids` marca, antes de qualquer emissão, os
`ast.Expr` que são docstring de módulo/classe/função. Nenhum símbolo,
referência ou `ConfigItem` é emitido a partir deles (§5.4). A tabela em
Markdown dentro da docstring de `knowledge/repository.py` é o caso concreto:
suas palavras não podem virar símbolos.

Literais executáveis são preservados (§5.4): SQL, templates f-string e
constantes de módulo saem como `ConfigItem`.
"""

from __future__ import annotations

import ast
import posixpath
from typing import Any, Iterable, Mapping

from .base import (
    CodeExtractor,
    ConfigItem,
    Diagnostic,
    Entrypoint,
    Reference,
    SourceFile,
    Symbol,
    looks_like_sql,
    normalize_files,
)

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

#: Métodos de argparse que declaram subcomandos (§6.2).
_ARGPARSE_SUBPARSER = "add_parser"
_ARGPARSE_DEFAULTS = "set_defaults"
#: Palavras-chave usadas para ligar o subcomando ao handler. `func` é a do
#: tutorial oficial; `fn`, `handler` e `callback` são as variantes que
#: aparecem em CLIs reais — sem elas o subcomando ficaria sem alvo mesmo com o
#: vínculo declarado no código.
_ARGPARSE_HANDLER_KEYWORDS = ("func", "fn", "handler", "callback", "cmd", "run")


def _visibility(name: str) -> str:
    if name.startswith("__") and name.endswith("__"):
        return "public"
    if name.startswith("__"):
        return "private"
    if name.startswith("_"):
        return "internal"
    return "public"


def _posix(path: str) -> str:
    return str(path).replace("\\", "/")


def _module_candidates(path: str, package_dirs: set[str]) -> list[str]:
    """Nomes de módulo plausíveis para o arquivo.

    Sobe a partir do diretório do arquivo enquanto houver `__init__.py` no
    conjunto analisado; o primeiro diretório sem `__init__.py` é a raiz de
    importação. `scripts/knowledge/repository.py` com `scripts/knowledge/
    __init__.py` presente e `scripts/__init__.py` ausente vira
    `knowledge.repository`.

    Devolve também o nome derivado do caminho inteiro
    (`scripts.knowledge.repository`) como alias, porque a raiz efetiva depende
    de `sys.path` em execução — e resolver por dois nomes é mais honesto que
    escolher um e errar.
    """
    p = _posix(path)
    if p.endswith(".pyi"):
        stem = p[: -len(".pyi")]
    elif p.endswith(".py"):
        stem = p[: -len(".py")]
    else:
        stem = p
    parts = [seg for seg in stem.split("/") if seg not in ("", ".")]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    candidates: list[str] = []
    # Raiz por presença de __init__.py
    dir_parts = parts[:-1] if not p.endswith("__init__.py") else list(parts)
    depth = len(dir_parts)
    while depth > 0:
        d = "/".join(dir_parts[:depth])
        if d in package_dirs:
            depth -= 1
        else:
            break
    rooted = ".".join(parts[depth:])
    if rooted:
        candidates.append(rooted)
    full = ".".join(parts)
    if full and full not in candidates:
        candidates.append(full)
    return candidates


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """Ids dos `ast.Expr` que são docstring (§5.4).

    Cobre módulo, classe, função e função assíncrona. É a barreira que impede
    a prosa de uma docstring de virar `ConfigItem` ou literal executável.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first))
            ids.add(id(first.value))
    return ids


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        try:
            return ast.unparse(node)
        except Exception:  # pragma: no cover - unparse é total em 3.9+
            return None


def _dotted(node: ast.AST) -> str | None:
    """Nome pontilhado de `Name`/`Attribute`, ou `None` se a base for dinâmica."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


class _ParsedModule:
    """AST + índice de um arquivo, reaproveitado entre as chamadas do contrato."""

    __slots__ = ("path", "content", "tree", "module", "aliases", "top_level", "class_methods", "doc_ids")

    def __init__(self, path: str, content: str, tree: ast.Module, module: str, aliases: list[str]):
        self.path = path
        self.content = content
        self.tree = tree
        self.module = module
        self.aliases = aliases
        self.top_level: set[str] = set()
        self.class_methods: dict[str, set[str]] = {}
        self.doc_ids: set[int] = _docstring_node_ids(tree)


class PythonExtractor(CodeExtractor):
    """Extração estrutural de Python com resolução real de nomes."""

    language = "python"
    extensions = (".py", ".pyi")
    resolution_level = "syntactic"

    def __init__(self) -> None:
        super().__init__()
        self._modules: dict[str, _ParsedModule] = {}
        self._by_module: dict[str, _ParsedModule] = {}
        self._failed: dict[str, int] = {}

    # ------------------------------------------------------------------
    def reset(self) -> None:
        super().reset()
        self._modules.clear()
        self._by_module.clear()
        self._failed.clear()

    def inventory(self, files: Iterable[Any]) -> list[SourceFile]:
        """Parseia tudo e monta o índice global de módulos, topos e métodos."""
        sources = normalize_files(files)
        package_dirs = {
            posixpath.dirname(_posix(f.path))
            for f in sources
            if posixpath.basename(_posix(f.path)) == "__init__.py"
        }
        for src in sources:
            self._ingest(src, package_dirs)
        return sources

    def _ingest(self, src: SourceFile, package_dirs: set[str]) -> _ParsedModule | None:
        # O contrato chama symbols/references/entrypoints/configuration para o
        # mesmo arquivo; sem esta marca, um arquivo que não parseia geraria o
        # mesmo diagnóstico uma vez por operação.
        already_failed = self._failed.get(src.path) == hash(src.content)
        try:
            tree = ast.parse(src.content, filename=src.path)
        except SyntaxError as exc:
            self._failed[src.path] = hash(src.content)
            if already_failed:
                return None
            self.add_diagnostic(
                Diagnostic(
                    level="error",
                    code="python_syntax_error",
                    message=(
                        f"arquivo não parseia como Python: {exc.msg} "
                        f"(linha {exc.lineno}, coluna {exc.offset})"
                    ),
                    scope_examined=f"ast.parse sobre {src.path}",
                    path=src.path,
                    paths=(src.path,),
                    language=self.language,
                    impact="símbolos, chamadas e entradas deste arquivo ficam ausentes do grafo",
                )
            )
            return None
        except (ValueError, RecursionError) as exc:  # null bytes, aninhamento extremo
            self._failed[src.path] = hash(src.content)
            if already_failed:
                return None
            self.add_diagnostic(
                Diagnostic(
                    level="error",
                    code="python_parse_failed",
                    message=f"ast.parse falhou: {type(exc).__name__}: {exc}",
                    scope_examined=f"ast.parse sobre {src.path}",
                    path=src.path,
                    paths=(src.path,),
                    language=self.language,
                    impact="arquivo fora do grafo de símbolos e chamadas",
                )
            )
            return None

        candidates = _module_candidates(src.path, package_dirs)
        module = candidates[0] if candidates else posixpath.basename(_posix(src.path))
        parsed = _ParsedModule(src.path, src.content, tree, module, candidates)
        for node in tree.body:
            if isinstance(node, _SCOPE_NODES):
                parsed.top_level.add(node.name)
                if isinstance(node, ast.ClassDef):
                    parsed.class_methods[node.name] = {
                        m.name for m in node.body if isinstance(m, _DEF_NODES)
                    }
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        parsed.top_level.add(tgt.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                parsed.top_level.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    parsed.top_level.add(bound)
        self._modules[src.path] = parsed
        for name in candidates:
            self._by_module.setdefault(name, parsed)
        return parsed

    def _module_for(self, path: str, content: str) -> _ParsedModule | None:
        cached = self._modules.get(path)
        if cached is not None and cached.content == content:
            return cached
        if path in self._failed and cached is None and content == "":
            return None
        return self._ingest(SourceFile(path=path, content=content, language=self.language), set())

    # ------------------------------------------------------------------
    # Resolução de nomes
    # ------------------------------------------------------------------
    def _resolve_module(self, name: str) -> _ParsedModule | None:
        mod = self._by_module.get(name)
        if mod is not None:
            return mod
        # `knowledge.repository` pode estar indexado como `scripts.knowledge.repository`
        for indexed, parsed in self._by_module.items():
            if indexed.endswith("." + name):
                return parsed
        return None

    def _absolute_from(self, parsed: _ParsedModule, node: ast.ImportFrom) -> str | None:
        """Alvo absoluto de um `from ... import`, resolvendo o nível relativo."""
        if not node.level:
            return node.module
        pkg = parsed.module.split(".")
        if _posix(parsed.path).endswith("__init__.py"):
            base = pkg
        else:
            base = pkg[:-1]
        up = node.level - 1
        if up:
            base = base[:-up] if up <= len(base) else []
        target = ".".join([p for p in base if p] + ([node.module] if node.module else []))
        return target or None

    def _name_env(self, parsed: _ParsedModule) -> dict[str, tuple[str, str | None]]:
        """`nome_local -> (alvo_qualificado, módulo_alvo_ou_None)`.

        `módulo_alvo` é o nome do módulo quando o binding é um módulo (permite
        resolver `mod.func`); `None` quando é um símbolo importado ou local.
        """
        env: dict[str, tuple[str, str | None]] = {}
        for node in ast.walk(parsed.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    target = alias.name if alias.asname else alias.name.split(".")[0]
                    env[bound] = (target, target)
            elif isinstance(node, ast.ImportFrom):
                base = self._absolute_from(parsed, node)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    bound = alias.asname or alias.name
                    qualified = f"{base}.{alias.name}" if base else alias.name
                    sub = self._resolve_module(qualified)
                    env[bound] = (qualified, qualified if sub is not None else None)
        for name in parsed.top_level:
            env.setdefault(name, (f"{parsed.module}.{name}", None))
        return env

    def _resolve_call(
        self,
        parsed: "_ParsedModule",
        env: Mapping[str, tuple[str, str | None]],
        func: ast.AST,
        class_stack: list[str],
    ) -> tuple[str, bool, str | None, str]:
        """`(to_name, resolved, target, reason)` para o alvo de uma chamada.

        `resolved=True` exige que o alvo exista no conjunto analisado. Um
        atributo sobre expressão dinâmica (`self.conn.execute(...)`,
        `objs[0].run()`) sai `resolved=False` com motivo — §5.5.
        """
        if isinstance(func, ast.Name):
            entry = env.get(func.id)
            if entry is None:
                return func.id, False, None, "nome não ligado no módulo (builtin ou global externo)"
            qualified, _ = entry
            if func.id in parsed.top_level and qualified.startswith(parsed.module + "."):
                return qualified, True, qualified, ""
            owner, _, attr = qualified.rpartition(".")
            mod = self._resolve_module(owner) if owner else None
            if mod is not None and attr in mod.top_level:
                return qualified, True, f"{mod.module}.{attr}", ""
            return qualified, False, None, "alvo fora do conjunto analisado"

        if isinstance(func, ast.Attribute):
            dotted = _dotted(func)
            base = func.value
            # self.metodo(...) dentro de uma classe conhecida
            if isinstance(base, ast.Name) and base.id in ("self", "cls") and class_stack:
                cls = class_stack[-1]
                methods = parsed.class_methods.get(cls, set())
                qualified = f"{parsed.module}.{cls}.{func.attr}"
                if func.attr in methods:
                    return qualified, True, qualified, ""
                return (
                    qualified,
                    False,
                    None,
                    "método não declarado na própria classe: herança ou atributo dinâmico",
                )
            if isinstance(base, ast.Name):
                entry = env.get(base.id)
                if entry is not None:
                    qualified_base, module_name = entry
                    qualified = f"{qualified_base}.{func.attr}"
                    if module_name:
                        mod = self._resolve_module(module_name)
                        if mod is not None and func.attr in mod.top_level:
                            return qualified, True, f"{mod.module}.{func.attr}", ""
                        if mod is not None:
                            return qualified, False, None, "atributo não é nome de topo do módulo alvo"
                    return qualified, False, None, "atributo sobre valor de tipo não inferido"
            return (
                dotted or f"<expr>.{func.attr}",
                False,
                None,
                "receptor dinâmico: despacho não resolvido estaticamente",
            )

        return "<dynamic>", False, None, "alvo de chamada não é nome nem atributo"

    # ------------------------------------------------------------------
    # Contrato
    # ------------------------------------------------------------------
    def symbols(self, path: str, content: str) -> list[Symbol]:
        parsed = self._module_for(path, content)
        if parsed is None:
            return []
        out: list[Symbol] = []
        last_line = max(1, len(content.splitlines()) or 1)
        out.append(
            Symbol(
                name=parsed.module,
                kind="module",
                path=path,
                line_start=1,
                line_end=last_line,
                visibility=_visibility(parsed.module.rsplit(".", 1)[-1]),
                language=self.language,
                qualname=parsed.module,
                extra={"aliases": tuple(parsed.aliases)},
            )
        )
        self._collect_symbols(parsed, parsed.tree.body, parsed.module, None, out, in_class=False)
        return out

    def _collect_symbols(
        self,
        parsed: _ParsedModule,
        body: list[ast.stmt],
        qual_prefix: str,
        parent: str | None,
        out: list[Symbol],
        *,
        in_class: bool,
    ) -> None:
        for node in body:
            if id(node) in parsed.doc_ids:
                continue  # §5.4: docstring nunca vira símbolo
            if isinstance(node, ast.ClassDef):
                qual = f"{qual_prefix}.{node.name}"
                out.append(
                    Symbol(
                        name=node.name,
                        kind="class",
                        path=parsed.path,
                        line_start=node.lineno,
                        line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                        parent=parent,
                        visibility=_visibility(node.name),
                        language=self.language,
                        qualname=qual,
                        decorators=tuple(_safe_unparse(d) for d in node.decorator_list),
                        extra={"bases": tuple(_safe_unparse(b) for b in node.bases)},
                    )
                )
                self._collect_symbols(parsed, node.body, qual, qual, out, in_class=True)
            elif isinstance(node, _DEF_NODES):
                qual = f"{qual_prefix}.{node.name}"
                out.append(
                    Symbol(
                        name=node.name,
                        kind="method" if in_class else "function",
                        path=parsed.path,
                        line_start=node.decorator_list[0].lineno if node.decorator_list else node.lineno,
                        line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                        parent=parent,
                        visibility=_visibility(node.name),
                        language=self.language,
                        qualname=qual,
                        signature=_signature(node),
                        decorators=tuple(_safe_unparse(d) for d in node.decorator_list),
                        extra={"async": isinstance(node, ast.AsyncFunctionDef)},
                    )
                )
                # Funções aninhadas ainda são comportamento: entram como function.
                self._collect_symbols(parsed, node.body, qual, qual, out, in_class=False)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for tgt in targets:
                    if not isinstance(tgt, ast.Name):
                        continue
                    kind = "const" if tgt.id.isupper() or _is_const_named(tgt.id) else "variable"
                    out.append(
                        Symbol(
                            name=tgt.id,
                            kind=kind,
                            path=parsed.path,
                            line_start=node.lineno,
                            line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                            parent=parent,
                            visibility=_visibility(tgt.id),
                            language=self.language,
                            qualname=f"{qual_prefix}.{tgt.id}",
                        )
                    )

    def references(self, path: str, content: str) -> list[Reference]:
        parsed = self._module_for(path, content)
        if parsed is None:
            return []
        env = self._name_env(parsed)
        out: list[Reference] = []
        walker = _RefWalker(self, parsed, env, out)
        walker.visit(parsed.tree)
        return out

    def entrypoints(self, path: str, content: str) -> list[Entrypoint]:
        parsed = self._module_for(path, content)
        if parsed is None:
            return []
        out: list[Entrypoint] = []
        tree = parsed.tree

        # 1) if __name__ == "__main__"
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _is_main_guard(node.test):
                out.append(
                    Entrypoint(
                        kind="main",
                        name=f"{parsed.module}:__main__",
                        path=path,
                        line=node.lineno,
                        line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                        symbol=parsed.module,
                        language=self.language,
                        detail='guarda `if __name__ == "__main__"`',
                        resolution="syntactic",
                    )
                )

        # 2) argparse: add_parser (+ set_defaults(func=...) ligado pela variável)
        parser_vars: dict[str, tuple[str, int, int]] = {}
        subcommands: list[tuple[str, int, int, str | None]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                name = _call_attr_name(node.value)
                if name == _ARGPARSE_SUBPARSER:
                    label = _first_str_arg(node.value)
                    if label:
                        for tgt in node.targets:
                            if isinstance(tgt, ast.Name):
                                parser_vars[tgt.id] = (
                                    label,
                                    node.lineno,
                                    getattr(node, "end_lineno", node.lineno) or node.lineno,
                                )
        handlers: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_attr_name(node) == _ARGPARSE_DEFAULTS:
                owner = node.func.value.id if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) else None
                for kw in node.keywords:
                    if kw.arg in _ARGPARSE_HANDLER_KEYWORDS and owner:
                        handlers[owner] = _safe_unparse(kw.value)
        for var, (label, ls, le) in parser_vars.items():
            subcommands.append((label, ls, le, handlers.get(var)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_attr_name(node) == _ARGPARSE_SUBPARSER:
                label = _first_str_arg(node)
                if label and not any(s[0] == label for s in subcommands):
                    subcommands.append(
                        (label, node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno, None)
                    )
        for label, ls, le, handler in subcommands:
            out.append(
                Entrypoint(
                    kind="cli",
                    name=label,
                    path=path,
                    line=ls,
                    line_end=le,
                    framework="argparse",
                    symbol=f"{parsed.module}:{label}",
                    language=self.language,
                    detail=(f"subcomando argparse; handler={handler}" if handler else "subcomando argparse"),
                    resolution="syntactic",
                    extra={"handler": handler} if handler else {},
                )
            )

        # 3) API pública do pacote
        exported = _dunder_all(tree)
        is_package_member = "." in parsed.module or _posix(path).endswith("__init__.py")
        for node in tree.body:
            if not isinstance(node, (*_DEF_NODES, ast.ClassDef)):
                continue
            if exported is not None:
                if node.name not in exported:
                    continue
            elif node.name.startswith("_") or not is_package_member:
                continue
            out.append(
                Entrypoint(
                    kind="public_api",
                    name=node.name,
                    path=path,
                    line=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                    framework=None,
                    symbol=f"{parsed.module}.{node.name}",
                    language=self.language,
                    detail=(
                        "listado em __all__" if exported is not None else "nome público de módulo em pacote"
                    ),
                    resolution="syntactic",
                )
            )
        return out

    def configuration(self, path: str, content: str) -> list[ConfigItem]:
        """Constantes de módulo, SQL e templates — literais que o programa usa (§5.4)."""
        parsed = self._module_for(path, content)
        if parsed is None:
            return []
        out: list[ConfigItem] = []
        seen: set[tuple[int, int, str]] = set()

        for node in parsed.tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value_node = node.value
                if value_node is None:
                    continue
                for tgt in targets:
                    if not isinstance(tgt, ast.Name) or not _is_const_named(tgt.id):
                        continue
                    kind = "template" if isinstance(value_node, ast.JoinedStr) else "config"
                    value = _safe_unparse(value_node) if isinstance(value_node, ast.JoinedStr) else _literal(value_node)
                    if isinstance(value, str) and looks_like_sql(value):
                        kind = "sql_literal"
                    out.append(
                        ConfigItem(
                            keypath=f"{parsed.module}.{tgt.id}",
                            value=value,
                            path=path,
                            line_start=node.lineno,
                            line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                            kind=kind,
                            source_format="python",
                            language=self.language,
                            resolution="syntactic",
                            extra={"annotated": isinstance(node, ast.AnnAssign)},
                        )
                    )
                    seen.add((node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno, tgt.id))

        # SQL em qualquer literal executável (docstrings excluídas).
        for node in ast.walk(parsed.tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in parsed.doc_ids:
                continue  # §5.4
            if not looks_like_sql(node.value):
                continue
            key = (node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno)
            out.append(
                ConfigItem(
                    keypath=f"{parsed.module}#sql@{node.lineno}",
                    value=node.value.strip(),
                    path=path,
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                    kind="sql_literal",
                    source_format="python",
                    language=self.language,
                    # Reconhecer SQL dentro de string é heurística: o literal é
                    # sintático, a classificação como SQL não é (§6.2).
                    resolution="heuristic",
                    extra={"detector": "looks_like_sql"},
                )
            )
        return out


class _RefWalker(ast.NodeVisitor):
    """Percorre o módulo mantendo o símbolo delimitador de cada referência."""

    def __init__(
        self,
        ext: PythonExtractor,
        parsed: _ParsedModule,
        env: Mapping[str, tuple[str, str | None]],
        out: list[Reference],
    ) -> None:
        self.ext = ext
        self.parsed = parsed
        self.env = env
        self.out = out
        self.scope: list[str] = [parsed.module]
        self.classes: list[str] = []

    # -- escopos -----------------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        qual = f"{self.scope[-1]}.{node.name}"
        for base in node.bases:
            dotted = _dotted(base)
            to_name = dotted or _safe_unparse(base)
            target, resolved = self._resolve_type(to_name)
            self.out.append(
                Reference(
                    from_symbol=qual,
                    to_name=to_name,
                    kind="inherit",
                    path=self.parsed.path,
                    line=base.lineno,
                    line_end=getattr(base, "end_lineno", base.lineno) or base.lineno,
                    resolved=resolved,
                    target=target,
                    language=self.ext.language,
                    reason="" if resolved else "base fora do conjunto analisado",
                )
            )
        self.scope.append(qual)
        self.classes.append(node.name)
        for child in node.body:
            self.visit(child)
        self.classes.pop()
        self.scope.pop()

    def _visit_def(self, node: ast.AST) -> None:
        qual = f"{self.scope[-1]}.{node.name}"
        for dec in node.decorator_list:
            self.visit(dec)
        self.scope.append(qual)
        for child in node.body:
            self.visit(child)
        self.scope.pop()

    visit_FunctionDef = _visit_def  # noqa: N815
    visit_AsyncFunctionDef = _visit_def  # noqa: N815

    # -- imports -----------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            mod = self.ext._resolve_module(alias.name)
            self.out.append(
                Reference(
                    from_symbol=self.parsed.module,
                    to_name=alias.name,
                    kind="import",
                    path=self.parsed.path,
                    line=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                    resolved=mod is not None,
                    target=mod.module if mod is not None else None,
                    language=self.ext.language,
                    reason="" if mod is not None else "módulo externo ao escopo analisado",
                    extra={"alias": alias.asname or ""},
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        base = self.ext._absolute_from(self.parsed, node) or ""
        base_mod = self.ext._resolve_module(base) if base else None
        for alias in node.names:
            qualified = f"{base}.{alias.name}" if base else alias.name
            sub = self.ext._resolve_module(qualified)
            if sub is not None:
                resolved, target, reason = True, sub.module, ""
            elif base_mod is not None and alias.name in base_mod.top_level:
                resolved, target, reason = True, f"{base_mod.module}.{alias.name}", ""
            elif alias.name == "*":
                resolved, target, reason = False, None, "import estrela: nomes não enumeráveis"
            else:
                resolved, target, reason = False, None, "alvo fora do conjunto analisado"
            self.out.append(
                Reference(
                    from_symbol=self.parsed.module,
                    to_name=qualified,
                    kind="import",
                    path=self.parsed.path,
                    line=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                    resolved=resolved,
                    target=target,
                    language=self.ext.language,
                    reason=reason,
                    extra={"relative_level": node.level, "alias": alias.asname or ""},
                )
            )

    # -- chamadas e atributos ---------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        to_name, resolved, target, reason = self.ext._resolve_call(
            self.parsed, self.env, node.func, self.classes
        )
        self.out.append(
            Reference(
                from_symbol=self.scope[-1],
                to_name=to_name,
                kind="call",
                path=self.parsed.path,
                line=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                resolved=resolved,
                target=target,
                language=self.ext.language,
                reason=reason,
            )
        )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        # Só atributos com base nomeada e ligada: evita ruído e mantém o
        # `resolved` com significado.
        if isinstance(node.value, ast.Name):
            entry = self.env.get(node.value.id)
            if entry is not None:
                qualified_base, module_name = entry
                target = None
                resolved = False
                reason = "atributo sobre valor de tipo não inferido"
                if module_name:
                    mod = self.ext._resolve_module(module_name)
                    if mod is not None and node.attr in mod.top_level:
                        resolved, target, reason = True, f"{mod.module}.{node.attr}", ""
                self.out.append(
                    Reference(
                        from_symbol=self.scope[-1],
                        to_name=f"{qualified_base}.{node.attr}",
                        kind="attribute",
                        path=self.parsed.path,
                        line=node.lineno,
                        line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                        resolved=resolved,
                        target=target,
                        language=self.ext.language,
                        reason=reason,
                    )
                )
        self.generic_visit(node)

    def _resolve_type(self, dotted_name: str) -> tuple[str | None, bool]:
        entry = self.env.get(dotted_name.split(".")[0])
        if entry is None:
            return None, False
        qualified_base, module_name = entry
        rest = dotted_name.split(".")[1:]
        if not rest:
            owner, _, attr = qualified_base.rpartition(".")
            mod = self.ext._resolve_module(owner) if owner else None
            if mod is not None and attr in mod.top_level:
                return f"{mod.module}.{attr}", True
            if dotted_name in self.parsed.top_level:
                return f"{self.parsed.module}.{dotted_name}", True
            return None, False
        if module_name:
            mod = self.ext._resolve_module(module_name)
            if mod is not None and rest[-1] in mod.top_level:
                return f"{mod.module}.{rest[-1]}", True
        return None, False


# ----------------------------------------------------------------------
# Auxiliares
# ----------------------------------------------------------------------


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover
        return "<unparsable>"


def _signature(node: ast.AST) -> str:
    args = node.args
    parts: list[str] = []
    for a in list(getattr(args, "posonlyargs", [])) + list(args.args):
        parts.append(a.arg)
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    for a in args.kwonlyargs:
        parts.append(a.arg)
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    returns = f" -> {_safe_unparse(node.returns)}" if node.returns is not None else ""
    return f"{node.name}({', '.join(parts)}){returns}"


def _is_const_named(name: str) -> bool:
    """Nome de constante: maiúsculas, dígitos e `_`, com ao menos uma letra."""
    core = name.lstrip("_")
    return bool(core) and core.replace("_", "").isalnum() and core.upper() == core and any(c.isalpha() for c in core)


def _is_main_guard(test: ast.AST) -> bool:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    names = {_dotted(left), _dotted(right)}
    consts = [n.value for n in (left, right) if isinstance(n, ast.Constant)]
    return "__name__" in names and "__main__" in consts


def _call_attr_name(call: ast.Call) -> str | None:
    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def _first_str_arg(call: ast.Call) -> str | None:
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        break
    for kw in call.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _dunder_all(tree: ast.Module) -> set[str] | None:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                    value = _literal(node.value)
                    if isinstance(value, (list, tuple, set)):
                        return {str(v) for v in value}
    return None


__all__ = ["PythonExtractor"]
