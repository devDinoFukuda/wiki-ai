from __future__ import annotations

import ast
import io
import json
import os
import sys
import tokenize

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = os.path.join(ROOT, "scripts")
OUT = HERE
EXCLUDED_DIRS = ("__pycache__", ".git", ".mypy_cache", ".pytest_cache")

STDLIB = set(getattr(sys, "stdlib_module_names", ()))


def iter_source_files():
    found = []
    for dirpath, dirnames, filenames in os.walk(SCRIPTS):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                found.append(os.path.join(dirpath, fn))
    return sorted(found)


def module_id(path):
    rel = os.path.relpath(path, SCRIPTS).replace("\\", "/")
    rel = rel[: -len(".py")]
    parts = rel.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def rel_path(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def read_source(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    return raw.decode("utf-8", errors="replace")


def docstring_lines(tree):
    lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            start = first.lineno
            end = getattr(first, "end_lineno", start) or start
            for n in range(start, end + 1):
                lines.add(n)
    return lines


def count_loc(source, tree):
    total = len(source.splitlines())
    skip = docstring_lines(tree)
    code_lines = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type in (
                tokenize.COMMENT,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENDMARKER,
                tokenize.ENCODING,
            ):
                continue
            if not tok.string.strip():
                continue
            for n in range(tok.start[0], tok.end[0] + 1):
                code_lines.add(n)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                code_lines.add(i)
    return total, len(code_lines - skip)


def is_test_module(mid, path):
    parts = mid.split(".")
    if "tests" in parts:
        return True
    return os.path.basename(path).startswith("test_")


def package_of(mid):
    return mid.split(".")[0]


class ScopeMap(ast.NodeVisitor):
    def __init__(self):
        self.scope = ["module"]
        self.node_scope = {}

    def generic_visit(self, node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.scope.append("function")
            super().generic_visit(node)
            self.scope.pop()
            return
        if isinstance(node, ast.ClassDef):
            self.scope.append("class")
            super().generic_visit(node)
            self.scope.pop()
            return
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Call)):
            self.node_scope[id(node)] = "function" if "function" in self.scope else self.scope[-1]
        super().generic_visit(node)


def build_module_table(files):
    modules = {}
    trees = {}
    parse_errors = []
    for path in files:
        mid = module_id(path)
        source = read_source(path)
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            parse_errors.append({"module": mid, "path": rel_path(path), "error": str(exc)})
            continue
        total, code = count_loc(source, tree)
        modules[mid] = {
            "module": mid,
            "path": rel_path(path),
            "package": package_of(mid),
            "is_package_init": os.path.basename(path) == "__init__.py",
            "loc_total": total,
            "loc_code": code,
            "is_test": is_test_module(mid, path),
        }
        trees[mid] = tree
    return modules, trees, parse_errors


def internal_roots(modules):
    return {m.split(".")[0] for m in modules}


def resolve_target(dotted, modules):
    hits = []
    if dotted.startswith("scripts.") and "scripts" not in modules:
        dotted = dotted[len("scripts.") :]
    if dotted in modules:
        hits.append(dotted)
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        prefix = ".".join(parts[:i])
        if prefix in modules:
            hits.append(prefix)
    return hits


def absolute_from_relative(mid, is_pkg_init, level, module_part):
    base = mid.split(".") if mid else []
    if not is_pkg_init:
        base = base[:-1]
    if level > 1:
        drop = level - 1
        if drop > len(base):
            return None
        base = base[: len(base) - drop]
    if module_part:
        base = base + module_part.split(".")
    if not base:
        return None
    return ".".join(base)


def literal_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def collect_dynamic_targets(tree, scopes):
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = None
        if isinstance(fn, ast.Attribute) and fn.attr == "import_module":
            base = fn.value
            if isinstance(base, ast.Name) and base.id in ("importlib",):
                name = "importlib.import_module"
            elif isinstance(base, ast.Attribute) and base.attr == "importlib":
                name = "importlib.import_module"
            else:
                name = "import_module"
        elif isinstance(fn, ast.Name) and fn.id in ("import_module", "__import__"):
            name = fn.id
        if name is None:
            continue
        if not node.args:
            continue
        value = literal_str(node.args[0])
        out.append(
            {
                "call": name,
                "value": value,
                "lineno": node.lineno,
                "scope": scopes.get(id(node), "module"),
            }
        )
    return out


def build_import_graph(modules, trees):
    edges = {mid: set() for mid in modules}
    details = {mid: [] for mid in modules}
    unresolved = []
    external = {}
    roots = internal_roots(modules)
    for mid, tree in trees.items():
        info = modules[mid]
        mapper = ScopeMap()
        mapper.visit(tree)
        scopes = mapper.node_scope
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                scope = scopes.get(id(node), "module")
                for alias in node.names:
                    record_target(
                        mid, alias.name, "import", scope, node.lineno, modules, roots, edges, details, unresolved, external
                    )
            elif isinstance(node, ast.ImportFrom):
                scope = scopes.get(id(node), "module")
                if node.level:
                    base = absolute_from_relative(mid, info["is_package_init"], node.level, node.module or "")
                    if base is None:
                        unresolved.append(
                            {
                                "module": mid,
                                "kind": "relative",
                                "target": "." * node.level + (node.module or ""),
                                "lineno": node.lineno,
                                "scope": scope,
                                "why": "nivel relativo fora da arvore",
                            }
                        )
                        continue
                    targets = [base] + [base + "." + a.name for a in node.names]
                    for t in targets:
                        record_target(
                            mid, t, "from-relative", scope, node.lineno, modules, roots, edges, details, unresolved, external
                        )
                else:
                    base = node.module or ""
                    targets = [base] + [base + "." + a.name for a in node.names if base]
                    for t in targets:
                        record_target(
                            mid, t, "from", scope, node.lineno, modules, roots, edges, details, unresolved, external
                        )
        for dyn in collect_dynamic_targets(tree, scopes):
            if dyn["value"] is None:
                unresolved.append(
                    {
                        "module": mid,
                        "kind": "dynamic",
                        "target": None,
                        "lineno": dyn["lineno"],
                        "scope": dyn["scope"],
                        "why": "argumento nao literal em " + dyn["call"],
                    }
                )
                continue
            record_target(
                mid,
                dyn["value"],
                "dynamic:" + dyn["call"],
                dyn["scope"],
                dyn["lineno"],
                modules,
                roots,
                edges,
                details,
                unresolved,
                external,
            )
    edges.pop(None, None)
    return edges, details, unresolved, external


def record_target(mid, dotted, kind, scope, lineno, modules, roots, edges, details, unresolved, external):
    if not dotted:
        return
    hits = resolve_target(dotted, modules)
    if hits:
        for h in hits:
            if h == mid:
                continue
            edges[mid].add(h)
        details[mid].append(
            {"target": dotted, "resolved": sorted(set(h for h in hits if h != mid)), "kind": kind, "scope": scope, "lineno": lineno}
        )
        return
    head = dotted.split(".")[0]
    if head in roots:
        unresolved.append(
            {
                "module": mid,
                "kind": kind,
                "target": dotted,
                "lineno": lineno,
                "scope": scope,
                "why": "prefixo interno sem modulo correspondente",
            }
        )
        return
    if head in STDLIB or head == "__future__":
        bucket = "stdlib"
    else:
        bucket = "third_party"
    key = bucket + ":" + head
    external.setdefault(key, {"bucket": bucket, "name": head, "count": 0, "importers": set()})
    external[key]["count"] += 1
    external[key]["importers"].add(mid)


def find_main_blocks(trees):
    out = []
    for mid, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
                continue
            left = test.left
            right = test.comparators[0]
            if isinstance(left, ast.Name) and left.id == "__name__" and literal_str(right) == "__main__":
                out.append({"module": mid, "lineno": node.lineno})
    return sorted(out, key=lambda x: x["module"])


def handler_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = handler_name(node.value)
        return (base + "." if base else "") + node.attr
    if isinstance(node, ast.Lambda):
        return "<lambda>"
    if isinstance(node, ast.Call):
        return (handler_name(node.func) or "?") + "(...)"
    return None


def loop_value_bindings(tree):
    bindings = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        try:
            values = ast.literal_eval(node.iter)
        except (ValueError, SyntaxError, TypeError):
            continue
        if not isinstance(values, (list, tuple, set)):
            continue
        names = {}
        target = node.target
        if isinstance(target, ast.Name):
            names[target.id] = [v for v in values if isinstance(v, str)]
        elif isinstance(target, ast.Tuple):
            for pos, elt in enumerate(target.elts):
                if not isinstance(elt, ast.Name):
                    continue
                got = []
                for v in values:
                    if isinstance(v, (list, tuple)) and len(v) > pos and isinstance(v[pos], str):
                        got.append(v[pos])
                names[elt.id] = got
        if not names:
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start) or start
        for name, got in names.items():
            if got:
                bindings.setdefault(name, []).append((start, end, got))
    return bindings


def resolve_parser_name(call, bindings):
    if not call.args:
        return None, []
    literal = literal_str(call.args[0])
    if literal is not None:
        return literal, [literal]
    arg = call.args[0]
    if isinstance(arg, ast.Name):
        for start, end, values in bindings.get(arg.id, ()):
            if start <= call.lineno <= end:
                return "|".join(values), list(values)
    return "<dinamico:" + (handler_name(arg) or "?") + ">", []


def helper_defaults(tree):
    helpers = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
        if node.args.posonlyargs:
            params |= {a.arg for a in node.args.posonlyargs}
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            fn = inner.func
            if not isinstance(fn, ast.Attribute) or fn.attr != "set_defaults":
                continue
            if not isinstance(fn.value, ast.Name) or fn.value.id not in params:
                continue
            kw = {}
            for k in inner.keywords:
                if k.arg is None:
                    continue
                kw[k.arg] = literal_str(k.value) if literal_str(k.value) is not None else handler_name(k.value)
            helpers[node.name] = {"kwargs": kw, "param": fn.value.id, "lineno": inner.lineno}
    return helpers


def extract_argparse(trees, modules):
    commands = []
    for mid, tree in trees.items():
        if modules[mid]["is_test"]:
            continue
        bindings = loop_value_bindings(tree)
        helpers = helper_defaults(tree)
        adds = []
        defaults = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Attribute) and call.func.attr == "add_parser":
                    target = node.targets[0]
                    var = target.id if isinstance(target, ast.Name) else None
                    name, variants = resolve_parser_name(call, bindings)
                    parent = handler_name(call.func.value)
                    adds.append(
                        {"var": var, "name": name, "variants": variants, "lineno": node.lineno, "parent": parent}
                    )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in helpers and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Name):
                    entry = helpers[fn.id]
                    kw = dict(entry["kwargs"])
                    kw["_via_helper"] = fn.id
                    defaults.append({"var": arg.id, "kwargs": kw, "lineno": node.lineno})
                continue
            if not isinstance(fn, ast.Attribute) or fn.attr != "set_defaults":
                continue
            var = fn.value.id if isinstance(fn.value, ast.Name) else None
            if var is not None and any(var == h["param"] for h in helpers.values()):
                inside_helper = False
                for h in helpers.values():
                    if h["lineno"] == node.lineno:
                        inside_helper = True
                if inside_helper:
                    continue
            kw = {}
            for k in node.keywords:
                if k.arg is None:
                    continue
                kw[k.arg] = literal_str(k.value) if literal_str(k.value) is not None else handler_name(k.value)
            defaults.append({"var": var, "kwargs": kw, "lineno": node.lineno})
        used = set()
        for d in defaults:
            best = None
            for a in adds:
                if a["var"] is None or a["var"] != d["var"]:
                    continue
                if a["lineno"] > d["lineno"]:
                    continue
                if best is None or a["lineno"] > best["lineno"]:
                    best = a
            handler = d["kwargs"].get("fn") or d["kwargs"].get("func")
            if best is None and handler is None:
                continue
            if best is not None:
                used.add(id(best))
            commands.append(
                {
                    "module": mid,
                    "command": (best or {}).get("name"),
                    "command_variants": (best or {}).get("variants") or [],
                    "command_label": d["kwargs"].get("cmd"),
                    "registered_via_helper": d["kwargs"].get("_via_helper"),
                    "handler": handler,
                    "handler_module": mid if handler and "." not in handler else handler,
                    "parser_var": d["var"],
                    "parser_parent": (best or {}).get("parent"),
                    "add_parser_line": (best or {}).get("lineno"),
                    "set_defaults_line": d["lineno"],
                    "extra_defaults": {
                        k: v for k, v in d["kwargs"].items() if k not in ("fn", "func", "_via_helper")
                    },
                }
            )
        for a in adds:
            if id(a) in used:
                continue
            commands.append(
                {
                    "module": mid,
                    "command": a["name"],
                    "command_variants": a.get("variants") or [],
                    "command_label": None,
                    "registered_via_helper": None,
                    "handler": None,
                    "handler_module": None,
                    "parser_var": a["var"],
                    "parser_parent": a["parent"],
                    "add_parser_line": a["lineno"],
                    "set_defaults_line": None,
                    "extra_defaults": {},
                }
            )
    return sorted(commands, key=lambda c: (c["module"], c["add_parser_line"] or 0, c["set_defaults_line"] or 0))


def extract_packages_const(trees, mid, const):
    tree = trees.get(mid)
    if tree is None:
        return []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == const:
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return []
                if isinstance(value, (list, tuple)):
                    return [str(v) for v in value]
    return []


def closure(roots, edges, allowed):
    seen = set()
    stack = [r for r in roots if r in allowed]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for nxt in edges.get(cur, ()):
            if nxt in allowed and nxt not in seen:
                stack.append(nxt)
    return seen


def tarjan(nodes, edges):
    index = {}
    low = {}
    on_stack = {}
    stack = []
    result = []
    counter = [0]
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in nodes:
                    continue
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack[nxt] = True
                    work.append((nxt, iter(sorted(edges.get(nxt, ())))))
                    advanced = True
                    break
                if on_stack.get(nxt):
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    result.append(sorted(comp))
    return sorted(result, key=lambda c: (-len(c), c[0]))


RULES = [
    ("codescan", "prefix", "DELETE", "§5.4.1 remover scripts/codescan/ integralmente"),
    ("sbindex", "prefix", "DELETE", "§5.4.2 remover scripts/sbindex/ integralmente"),
    ("wk.cli", "exact", "DELETE", "§5.4.3 wk/cli.py nao deve ser refatorado; deve ser removido"),
    ("wk.__main__", "exact", "DELETE", "§5.4.3 entrypoint do CLI antigo"),
    ("wk", "exact", "DELETE", "§5.4.3/§5.4.16 nenhum renderer novo pode depender de wk"),
    ("wk.docx_md", "exact", "EXTRACT", "§5.4.16 primitivas OOXML extraidas e depois wk/docx_*.py removido"),
    ("wk.docx_meta", "exact", "EXTRACT", "§5.4.16 primitivas OOXML extraidas e depois wk/docx_*.py removido"),
    ("wk.docx_ooxml", "exact", "EXTRACT", "§5.4.16 primitivas OOXML extraidas e depois wk/docx_*.py removido"),
    ("wk.docxgen", "exact", "EXTRACT", "§5.4.16 primitivas OOXML extraidas e depois wk/docxgen.py removido"),
    ("build_pyz", "exact", "DELETE", "§5.4.1/§5.4.2 empacota codescan e sbindex; packaging do runtime antigo"),
    ("analysis.snapshot", "exact", "EXTRACT", "§5.4.8 snapshot e o unico kernel elegivel a extracao"),
    ("analysis.inventory", "exact", "EXTRACT", "§5.4.8 inventory e o unico kernel elegivel a extracao"),
    ("analysis.investigation", "exact", "DELETE", "§5.4.7 planner de investigacao com objetivo fixo e contrato de 13 campos"),
    ("analysis.capabilities", "exact", "DELETE", "§5.4.7 reading needs pre-selecionadas / formulario fixo"),
    ("analysis.verification", "exact", "DELETE", "§5.4.7 cobertura de arquivos como proxy de completude"),
    ("analysis.extractors", "prefix", "DELETE", "§5.4.8 extractors atuais fora do kernel; voltam como hints se necessario"),
    ("analysis.profile", "exact", "REBUILD", "§5.4.8 resto de analysis; §6.3 sem configuracao operacional manual"),
    ("analysis", "exact", "REBUILD", "§5.4.8 fachada do package sera reescrita na nova arvore"),
    ("runtime.executors", "prefix", "DELETE", "§5.4.9/§5.4.6 executores e agent adapters atuais removidos"),
    ("runtime.coordinator", "exact", "DELETE", "§5.4.9 coordinator preso ao planner antigo"),
    ("runtime.agents", "exact", "DELETE", "§5.4.9/§5.4.4 provider-specific behavior e compat de engines"),
    ("runtime.bindings", "exact", "DELETE", "§5.4.9/§5.4.4 binding de agente com selecao de engine"),
    ("runtime.tasks", "exact", "EXTRACT", "§5.4.9 task state e lease sao candidatos a extracao"),
    ("runtime.envelopes", "exact", "EXTRACT", "§5.4.9 envelope identity e candidato a extracao"),
    ("runtime.recovery", "exact", "EXTRACT", "§5.4.9 retry/recovery primitives e stale-result detection"),
    ("runtime.state", "exact", "REBUILD", "§5.4.9 schema de resultado preso ao formulario antigo (contrato de campos)"),
    ("runtime.context", "exact", "REBUILD", "§5.4.9 orchestration contaminada; empacotamento de contexto sera reconstruido"),
    ("runtime", "exact", "REBUILD", "§5.4.9 fachada do package sera reescrita na nova arvore"),
    ("knowledge.identity", "exact", "EXTRACT", "§5.4.10 identity e candidato a extracao"),
    ("knowledge.evidence", "exact", "EXTRACT", "§5.4.10 evidence e candidato a extracao"),
    ("knowledge.repository", "exact", "EXTRACT", "§5.4.10 repository persistence e revision transaction"),
    ("knowledge.relations", "exact", "EXTRACT", "§5.4.10 relation persistence e candidato a extracao"),
    ("knowledge.schema", "exact", "EXTRACT", "§5.4.10 storage kernel (schema de persistencia)"),
    ("knowledge.invalidate", "exact", "EXTRACT", "§5.4.10 source versions / invalidacao por versao de fonte"),
    ("knowledge.integrate", "exact", "REBUILD", "§5.4.10 integrate.py DEVE ser reconstruido"),
    ("knowledge.query", "exact", "REBUILD", "§5.4.2 busca e recuperacao nascem sobre o modelo novo"),
    ("knowledge.migrate", "exact", "DELETE", "§5.4.14 artefatos antigos do store nao serao migrados"),
    ("knowledge.models", "exact", "REBUILD", "§5.4.10 taxonomias insuficientes nao sao preservadas automaticamente"),
    ("knowledge", "exact", "REBUILD", "§5.4.10 fachada do package sera reescrita na nova arvore"),
    ("ingestion.extract", "exact", "REBUILD", "§5.4.11 semantic extractor regex removido e reconstruido"),
    ("ingestion.correlate", "exact", "DELETE", "§5.4.12 correlacao atual removida"),
    ("ingestion.normalize", "exact", "EXTRACT", "§5.4.12 normalize.py extraivel se source/block/hash/locator/diagnostics aderirem"),
    ("ingestion.adapters.pdf_adapter", "exact", "REBUILD", "§5.4.13 adapter pdf insuficiente"),
    ("ingestion.adapters.docx_adapter", "exact", "REBUILD", "§5.4.13 adapter docx insuficiente"),
    ("ingestion.adapters.json_xml", "exact", "REBUILD", "§5.4.13 xml generico como substituto de draw.io"),
    ("ingestion.adapters.transcript", "exact", "REBUILD", "§5.4.13 transcript semantic path insuficiente"),
    ("ingestion.adapters.registry", "exact", "REBUILD", "§5.4.13 registry de adapters segue os adapters reconstruidos"),
    ("ingestion.adapters", "exact", "REBUILD", "§5.4.13 fachada de adapters segue a reconstrucao do pipeline"),
    ("ingestion", "exact", "REBUILD", "§5.4.13 novo pipeline de ingestao na Wave especifica"),
    ("publishing.release", "exact", "EXTRACT", "§5.4.15 staging, atomic promotion, manifest, hash e rollback"),
    ("publishing.validate", "exact", "EXTRACT", "§5.4.15 validacao estrutural reutilizavel"),
    ("publishing.planner", "exact", "REBUILD", "§5.4.15 planner documental deve ser reconstruido"),
    ("publishing.document", "exact", "REBUILD", "§5.4.15 semantic unit orientada ao modelo atual"),
    ("publishing.markdown", "exact", "REBUILD", "§5.4.15 conteudo Markdown deve ser reconstruido"),
    ("publishing.word", "exact", "REBUILD", "§5.4.15 conteudo Word deve ser reconstruido"),
    ("publishing.delivery", "exact", "REBUILD", "§5.4.15 delivery atual deve ser reconstruido"),
    ("publishing.sharepoint", "exact", "REBUILD", "§5.4.15 sharepoint abstraction atual deve ser reconstruida"),
    ("publishing", "exact", "REBUILD", "§5.4.15 fachada do package sera reescrita na nova arvore"),
]

ORDER = {"KEEP": 0, "EXTRACT": 1, "REBUILD": 2, "DELETE": 3}


def apply_rules(mid):
    for pattern, mode, verdict, reason in RULES:
        if mode == "exact" and mid == pattern:
            return verdict, reason
    for pattern, mode, verdict, reason in RULES:
        if mode == "prefix" and (mid == pattern or mid.startswith(pattern + ".")):
            return verdict, reason
    return None, None


def classify(modules, edges, reach):
    verdicts = {}
    reasons = {}
    for mid in sorted(modules):
        info = modules[mid]
        if info["is_test"]:
            verdicts[mid] = "DELETE"
            reasons[mid] = "§5.8 testes do fluxo antigo sao descartados; testes novos sao escritos contra o comportamento novo"
            continue
        verdict, reason = apply_rules(mid)
        if verdict is not None:
            verdicts[mid] = verdict
            reasons[mid] = reason
            continue
        state = reach[mid]
        if not state["reachable_runtime"] and not state["reachable_tests"]:
            verdicts[mid] = "DELETE"
            reasons[mid] = "derivado: inalcancavel a partir de qualquer entrypoint de runtime ou teste"
            continue
        if not state["reachable_runtime"]:
            verdicts[mid] = "DELETE"
            reasons[mid] = "derivado: alcancavel somente por testes do fluxo antigo"
            continue
        verdicts[mid] = "KEEP"
        reasons[mid] = "derivado: alcancavel em runtime e sem regra explicita do §5.4"
    changed = True
    while changed:
        changed = False
        for mid in sorted(modules):
            if verdicts[mid] != "KEEP":
                continue
            blocking = [d for d in sorted(edges.get(mid, ())) if verdicts.get(d) in ("DELETE", "REBUILD")]
            if blocking:
                verdicts[mid] = "REBUILD"
                reasons[mid] = "derivado: depende de modulo condenado (" + ", ".join(blocking[:5]) + ")"
                changed = True
    return verdicts, reasons


def main():
    files = iter_source_files()
    modules, trees, parse_errors = build_module_table(files)
    edges, details, unresolved, external = build_import_graph(modules, trees)

    prod = {m for m in modules if not modules[m]["is_test"]}
    tests = {m for m in modules if modules[m]["is_test"]}
    allowed = set(modules)

    main_blocks = find_main_blocks(trees)
    commands = extract_argparse(trees, modules)
    packaged = extract_packages_const(trees, "build_pyz", "PACKAGES")

    entry_groups = {
        "wk": [m for m in ("wk.__main__", "wk.cli", "wk") if m in modules],
        "codescan": [m for m in ("codescan.cli", "codescan") if m in modules],
        "sbindex": [m for m in ("sbindex.cli", "sbindex") if m in modules],
        "build_pyz": [m for m in ("build_pyz",) if m in modules],
    }
    known = set()
    for v in entry_groups.values():
        known.update(v)
    other_mains = [b["module"] for b in main_blocks if b["module"] not in known and b["module"] in prod]
    for m in sorted(other_mains):
        entry_groups["main:" + m] = [m]

    reach_sets = {}
    for group, roots in entry_groups.items():
        reach_sets[group] = closure(roots, edges, allowed)

    runtime_groups = [g for g in entry_groups if g != "build_pyz"]
    reachable_runtime = set()
    for g in runtime_groups:
        reachable_runtime |= reach_sets[g]

    test_imports = {m: set() for m in modules}
    for t in sorted(tests):
        for target in closure([t], edges, allowed):
            if target != t:
                test_imports[target].add(t)

    direct_test_importers = {m: set() for m in modules}
    for t in sorted(tests):
        for target in edges.get(t, ()):
            direct_test_importers[target].add(t)

    reachable_tests = {m for m in modules if test_imports[m]}

    reach = {}
    for mid in sorted(modules):
        groups = sorted(g for g in entry_groups if mid in reach_sets[g])
        reach[mid] = {
            "module": mid,
            "package": modules[mid]["package"],
            "is_test": modules[mid]["is_test"],
            "loc_total": modules[mid]["loc_total"],
            "loc_code": modules[mid]["loc_code"],
            "entrypoint_groups": groups,
            "reachable_from_wk": mid in reach_sets["wk"],
            "reachable_from_codescan": mid in reach_sets["codescan"],
            "reachable_from_sbindex": mid in reach_sets["sbindex"],
            "reachable_from_build_pyz": mid in reach_sets["build_pyz"],
            "reachable_runtime": mid in reachable_runtime,
            "reachable_tests": mid in reachable_tests or modules[mid]["is_test"],
            "test_only": (mid not in reachable_runtime) and (mid in reachable_tests) and not modules[mid]["is_test"],
            "unreachable": (mid not in reachable_runtime) and (mid not in reachable_tests) and not modules[mid]["is_test"],
            "imported_by_tests": sorted(direct_test_importers[mid]),
            "imported_by_tests_transitive": sorted(test_imports[mid]),
            "packaged_by_build_pyz": modules[mid]["package"] in packaged,
        }

    verdicts, reasons = classify(modules, edges, reach)

    cycles = tarjan(prod, {k: {d for d in v if d in prod} for k, v in edges.items()})

    entrypoints_doc = {
        "root": rel_path(SCRIPTS),
        "entrypoint_groups": {g: sorted(r) for g, r in entry_groups.items()},
        "main_blocks": main_blocks,
        "build_pyz_packages": packaged,
        "argparse_commands": commands,
        "argparse_command_count_by_module": count_by(commands, "module"),
    }

    graph_doc = {
        "modules": [modules[m] for m in sorted(modules)],
        "edges": {m: sorted(edges[m]) for m in sorted(edges)},
        "edge_details": {m: details[m] for m in sorted(details) if details[m]},
        "late_imports": [
            {"module": m, **d}
            for m in sorted(details)
            for d in details[m]
            if d["scope"] == "function"
        ],
        "dynamic_imports": [
            {"module": m, **d}
            for m in sorted(details)
            for d in details[m]
            if d["kind"].startswith("dynamic")
        ],
        "unresolved_imports": sorted(unresolved, key=lambda u: (u["module"], u["lineno"])),
        "external_imports": sorted(
            (
                {"bucket": v["bucket"], "name": v["name"], "count": v["count"], "importers": len(v["importers"])}
                for v in external.values()
            ),
            key=lambda v: (v["bucket"], v["name"]),
        ),
        "cycles": [{"size": len(c), "modules": c} for c in cycles],
        "parse_errors": parse_errors,
    }

    reach_doc = {
        "entrypoint_groups": sorted(entry_groups),
        "modules": [reach[m] for m in sorted(reach)],
        "counts": {
            "total": len(modules),
            "prod": len(prod),
            "tests": len(tests),
            "reachable_runtime": len([m for m in prod if reach[m]["reachable_runtime"]]),
            "test_only": len([m for m in prod if reach[m]["test_only"]]),
            "unreachable": len([m for m in prod if reach[m]["unreachable"]]),
        },
        "unreachable_modules": [m for m in sorted(prod) if reach[m]["unreachable"]],
        "test_only_modules": [m for m in sorted(prod) if reach[m]["test_only"]],
    }

    rows = []
    for mid in sorted(modules):
        blocking = sorted(
            d for d in edges.get(mid, ()) if verdicts.get(d) in ("DELETE", "REBUILD") and d != mid
        )
        rows.append(
            {
                "module": mid,
                "path": modules[mid]["path"],
                "package": modules[mid]["package"],
                "is_test": modules[mid]["is_test"],
                "loc_total": modules[mid]["loc_total"],
                "loc_code": modules[mid]["loc_code"],
                "classification": verdicts[mid],
                "reason": reasons[mid],
                "blocking_dependencies": [
                    {"module": d, "classification": verdicts[d]} for d in blocking
                ],
                "reachable_from_wk": reach[mid]["reachable_from_wk"],
                "reachable_from_codescan": reach[mid]["reachable_from_codescan"],
                "test_only": reach[mid]["test_only"],
                "unreachable": reach[mid]["unreachable"],
                "imported_by_tests": reach[mid]["imported_by_tests"],
            }
        )

    by_class = {}
    for r in rows:
        b = by_class.setdefault(r["classification"], {"modules": 0, "loc_total": 0, "loc_code": 0})
        b["modules"] += 1
        b["loc_total"] += r["loc_total"]
        b["loc_code"] += r["loc_code"]

    by_class_prod = {}
    for r in rows:
        if r["is_test"]:
            continue
        b = by_class_prod.setdefault(r["classification"], {"modules": 0, "loc_total": 0, "loc_code": 0})
        b["modules"] += 1
        b["loc_total"] += r["loc_total"]
        b["loc_code"] += r["loc_code"]

    by_package = {}
    for r in rows:
        p = by_package.setdefault(r["package"], {})
        c = p.setdefault(r["classification"], {"modules": 0, "loc_total": 0})
        c["modules"] += 1
        c["loc_total"] += r["loc_total"]

    extract_rows = [r for r in rows if r["classification"] == "EXTRACT"]
    extract_blocking = [
        {
            "module": r["module"],
            "path": r["path"],
            "loc_total": r["loc_total"],
            "loc_code": r["loc_code"],
            "reason": r["reason"],
            "blocking_dependencies": r["blocking_dependencies"],
            "blocking_count": len(r["blocking_dependencies"]),
        }
        for r in extract_rows
    ]
    extract_blocking.sort(key=lambda x: (-x["blocking_count"], x["module"]))

    deletion_doc = {
        "summary": {
            "totals": {
                "modules": len(rows),
                "loc_total": sum(r["loc_total"] for r in rows),
                "loc_code": sum(r["loc_code"] for r in rows),
            },
            "by_classification": {
                k: by_class[k] for k in sorted(by_class, key=lambda x: ORDER.get(x, 9))
            },
            "by_classification_prod_only": {
                k: by_class_prod[k] for k in sorted(by_class_prod, key=lambda x: ORDER.get(x, 9))
            },
            "by_package": {p: by_package[p] for p in sorted(by_package)},
            "extract_modules": [x["module"] for x in extract_blocking],
            "extract_blocking_dependencies": extract_blocking,
            "unreachable_modules": reach_doc["unreachable_modules"],
            "test_only_modules": reach_doc["test_only_modules"],
            "cycle_count": len(cycles),
            "unresolved_import_count": len(unresolved),
        },
        "modules": rows,
    }

    write(os.path.join(OUT, "runtime-entrypoints.json"), entrypoints_doc)
    write(os.path.join(OUT, "import-graph.json"), graph_doc)
    write(os.path.join(OUT, "reachability.json"), reach_doc)
    write(os.path.join(OUT, "deletion-report.json"), deletion_doc)

    print(json.dumps(deletion_doc["summary"]["by_classification"], indent=2, ensure_ascii=False))
    print("modulos:", len(rows), "prod:", len(prod), "tests:", len(tests))
    print("ciclos:", len(cycles), "imports internos nao resolvidos:", len(unresolved))


def count_by(items, key):
    out = {}
    for it in items:
        out[it[key]] = out.get(it[key], 0) + 1
    return {k: out[k] for k in sorted(out)}


def write(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")


if __name__ == "__main__":
    main()
