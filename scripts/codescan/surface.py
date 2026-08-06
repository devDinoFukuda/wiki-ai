"""Estágio 1 — Surface. Determinístico, sem LLM.

Varre o repositório e reduz a um mapa. O agente lê o mapa para saber ONDE
cavar, em vez de caminhar às cegas por milhares de arquivos.

Agnóstico por construção: classifica por extensão, não por gramática. Não há
parser, não há AST — então qualquer linguagem entra. O que sai daqui é 100%
verificável (🟢): contagem de arquivo, LOC, dependência declarada em manifest,
metadado de git. Nada é inferido.

Interpretação é dos estágios seguintes, e é do LLM.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field

# Extensão -> linguagem. Agnóstico: adicionar linguagem é uma linha.
LANGUAGES = {
    ".cs": "C#", ".vb": "VB.NET", ".fs": "F#",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala",
    ".groovy": "Groovy",
    ".py": "Python", ".pyi": "Python",
    ".go": "Go", ".rs": "Rust",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".cjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".rb": "Ruby", ".php": "PHP", ".pl": "Perl",
    ".c": "C", ".h": "C/C++ header", ".cpp": "C++", ".cc": "C++",
    ".cxx": "C++", ".hpp": "C++ header",
    ".swift": "Swift", ".m": "Objective-C", ".mm": "Objective-C++",
    ".sql": "SQL", ".psql": "SQL", ".plsql": "PL/SQL", ".pks": "PL/SQL",
    ".pkb": "PL/SQL",
    ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell", ".bat": "Batch",
    ".cbl": "COBOL", ".cob": "COBOL", ".pas": "Pascal", ".dpr": "Delphi",
    ".vue": "Vue", ".svelte": "Svelte",
    ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang",
    ".clj": "Clojure", ".hs": "Haskell", ".lua": "Lua", ".r": "R",
    ".dart": "Dart",
}

# Diretórios que nunca são código-fonte do projeto. Sem isto, um repo .NET
# devolve milhares de arquivos de bin/obj e o mapa vira lixo.
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "vendor", "packages",
    "bin", "obj", "dist", "build", "out", "target", "_build",
    "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", ".gradle", ".idea", ".vs", ".vscode",
    "coverage", "htmlcov", ".next", ".nuxt", "bower_components",
    "Pods", "DerivedData", ".terraform", "site-packages",
}

# Segmentos de caminho que denunciam código de teste. Teste confirma
# comportamento; não é onde a regra de negócio tácita está presa. Por isso o
# `plan` cava `main` primeiro — teste entra como cobertura, não como alvo.
TEST_SEGMENTS = {
    "test", "tests", "spec", "specs", "__tests__", "testing",
    "unittest", "unittests",
}

# Padrões de arquivo gerado. Presença é registrada; conteúdo não interessa.
GENERATED_PATTERNS = (
    re.compile(r"\.designer\.cs$", re.I),
    re.compile(r"\.g\.cs$", re.I),
    re.compile(r"\.generated\.", re.I),
    re.compile(r"_pb2\.py$"),
    re.compile(r"\.pb\.go$"),
    re.compile(r"\.min\.(js|css)$", re.I),
    re.compile(r"-lock\.json$"),
    re.compile(r"\.lock$"),
)

ENTRY_HINTS = (
    ("Program.cs", "entry point .NET"),
    ("Startup.cs", "bootstrap ASP.NET"),
    ("main.go", "entry point Go"),
    ("main.py", "entry point Python"),
    ("__main__.py", "entry point Python"),
    ("app.py", "entry point Flask/genérico"),
    ("manage.py", "entry point Django"),
    ("index.js", "entry point Node"),
    ("index.ts", "entry point Node"),
    ("server.js", "entry point Node"),
    ("main.rb", "entry point Ruby"),
    ("index.php", "entry point PHP"),
    ("main.rs", "entry point Rust"),
)

# Sufixos: convenção Java/Spring nomeia FooApplication.java, BarMain.java.
# Comparação exata perderia todos.
ENTRY_SUFFIXES = (
    ("Application.java", "bootstrap Spring"),
    ("Main.java", "entry point Java"),
    ("Application.kt", "bootstrap Spring/Kotlin"),
)

MAX_FILE_BYTES = 2_000_000  # acima disso: registra, não conta LOC


@dataclass
class Module:
    path: str
    files: int
    loc: int
    languages: list[str]
    role: str = "main"          # 'main' | 'test' — separa alvo de cobertura
    commits: int | None = None
    authors: int | None = None
    last_commit: str | None = None


def _role_of(rel_dir: str) -> str:
    """'test' se qualquer segmento do caminho for de teste, senão 'main'."""
    parts = rel_dir.replace("\\", "/").lower().split("/")
    return "test" if any(p in TEST_SEGMENTS for p in parts) else "main"


@dataclass
class Surface:
    repo: str
    scanned_at: str
    total_files: int = 0
    total_loc: int = 0
    languages: dict = field(default_factory=dict)
    manifests: list = field(default_factory=list)
    entry_points: list = field(default_factory=list)
    modules: list = field(default_factory=list)
    git: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def _loc(path: str) -> int:
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return 0
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _is_generated(name: str) -> bool:
    return any(p.search(name) for p in GENERATED_PATTERNS)


def _is_js_workspace_packages(path: str) -> bool:
    """`packages/` é vendoring NuGet OU raiz de monorepo JS (pnpm/yarn).

    No monorepo, o código-fonte inteiro vive em packages/*/ — podar aqui
    devolveria um mapa vazio em silêncio. O sinal que separa os dois casos:
    workspace tem package.json um nível abaixo; vendoring NuGet não tem.
    """
    try:
        for child in sorted(os.listdir(path))[:50]:
            if os.path.isfile(os.path.join(path, child, "package.json")):
                return True
    except OSError:
        pass
    return False


def _prune_dirs(dirpath: str, dirnames: list[str]) -> tuple[list[str], int]:
    """Decide o que podar da caminhada. Devolve (mantidos, podados)."""
    keep, pruned = [], 0
    for d in dirnames:
        if d.startswith("."):
            pruned += 1
        elif d in SKIP_DIRS:
            if d == "packages" and _is_js_workspace_packages(os.path.join(dirpath, d)):
                keep.append(d)
            else:
                pruned += 1
        else:
            keep.append(d)
    return keep, pruned


# ---------- manifests ----------


def _parse_package_json(path: str) -> list[str]:
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    out = []
    for k in ("dependencies", "devDependencies", "peerDependencies"):
        for name, ver in (d.get(k) or {}).items():
            out.append(f"{name}@{ver}")
    return out


def _parse_csproj(path: str) -> list[str]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(path).getroot()
    except Exception:
        return []
    out = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "PackageReference":
            n = el.get("Include") or el.get("Update")
            v = el.get("Version") or (el.findtext("Version") or "")
            if n:
                out.append(f"{n}@{v}" if v else n)
        elif tag == "ProjectReference":
            inc = el.get("Include")
            if inc:
                out.append(f"proj:{inc}")
    return out


def _parse_pom(path: str) -> list[str]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(path).getroot()
    except Exception:
        return []
    out = []
    for dep in root.iter():
        if dep.tag.split("}")[-1] != "dependency":
            continue
        g = a = v = ""
        for c in dep:
            t = c.tag.split("}")[-1]
            if t == "groupId":
                g = c.text or ""
            elif t == "artifactId":
                a = c.text or ""
            elif t == "version":
                v = c.text or ""
        if a:
            out.append(f"{g}:{a}@{v}" if v else f"{g}:{a}")
    return out


def _parse_requirements(path: str) -> list[str]:
    out = []
    try:
        for ln in open(path, encoding="utf-8", errors="replace"):
            ln = ln.strip()
            if ln and not ln.startswith(("#", "-")):
                out.append(ln)
    except OSError:
        pass
    return out


def _parse_pyproject(path: str) -> list[str]:
    try:
        import tomllib

        d = tomllib.load(open(path, "rb"))
    except Exception:
        return []
    out = list(d.get("project", {}).get("dependencies", []) or [])
    poetry = d.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
    out += [f"{k}@{v}" if isinstance(v, str) else k for k, v in poetry.items()]
    return out


def _parse_gomod(path: str) -> list[str]:
    out = []
    try:
        for ln in open(path, encoding="utf-8", errors="replace"):
            ln = ln.strip()
            m = re.match(r"^(?:require\s+)?([\w./-]+)\s+(v[\w.\-+]+)", ln)
            if m and not ln.startswith(("module", "go ")):
                out.append(f"{m.group(1)}@{m.group(2)}")
    except OSError:
        pass
    return out


def _parse_gradle(path: str) -> list[str]:
    out = []
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    for m in re.finditer(
        r"(?:implementation|api|compile|testImplementation)\s+"
        r"['\"]([^'\"]+)['\"]",
        txt,
    ):
        out.append(m.group(1))
    return out


MANIFESTS = {
    "package.json": ("npm", _parse_package_json),
    "pom.xml": ("maven", _parse_pom),
    "build.gradle": ("gradle", _parse_gradle),
    "build.gradle.kts": ("gradle", _parse_gradle),
    "requirements.txt": ("pip", _parse_requirements),
    "pyproject.toml": ("python", _parse_pyproject),
    "go.mod": ("go", _parse_gomod),
    "Gemfile": ("bundler", _parse_requirements),
    "composer.json": ("composer", _parse_package_json),
}


# ---------- git ----------


def _git(repo: str, *args) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_info(repo: str) -> dict:
    head = _git(repo, "rev-parse", "--short", "HEAD")
    if head is None:
        return {"available": False}
    return {
        "available": True,
        "head": head,
        "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "last_commit": _git(repo, "log", "-1", "--format=%cI"),
        "total_commits": _git(repo, "rev-list", "--count", "HEAD"),
    }


def _git_module_stats(repo: str, rel: str, since: str | None) -> dict:
    """Churn e autores. É a métrica agnóstica que funciona sem gramática:
    não diz complexidade, mas diz onde o time mexe. 100% verificável."""
    args = ["log", "--format=%an"]
    if since:
        args += [f"--since={since}"]
    args += ["--", rel]
    out = _git(repo, *args)
    if out is None:
        return {}
    lines = [x for x in out.splitlines() if x.strip()]
    last = _git(repo, "log", "-1", "--format=%cI", "--", rel)
    return {
        "commits": len(lines),
        "authors": len(set(lines)),
        "last_commit": last or None,
    }


# ---------- varredura ----------


def scan(repo: str, module_min_files: int = 3, since: str | None = None) -> Surface:
    repo = os.path.abspath(repo)
    s = Surface(
        repo=repo,
        scanned_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        git=_git_info(repo),
    )
    skipped = {"vendored_dirs": 0, "generated": 0, "binary_or_unknown": 0, "too_large": 0}
    per_dir: dict[str, dict] = {}

    for dirpath, dirnames, filenames in os.walk(repo):
        kept, pruned_count = _prune_dirs(dirpath, dirnames)
        skipped["vendored_dirs"] += pruned_count
        dirnames[:] = kept

        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, repo)

            if fn in MANIFESTS:
                kind, parser = MANIFESTS[fn]
                s.manifests.append(
                    {"path": rel, "type": kind, "dependencies": parser(full)}
                )
            elif fn.endswith(".csproj") or fn.endswith(".vbproj"):
                s.manifests.append(
                    {"path": rel, "type": "msbuild", "dependencies": _parse_csproj(full)}
                )

            for hint, why in ENTRY_HINTS:
                if fn == hint:
                    s.entry_points.append({"path": rel, "reason": why})
            for suf, why in ENTRY_SUFFIXES:
                if fn.endswith(suf):
                    s.entry_points.append({"path": rel, "reason": why})

            ext = os.path.splitext(fn)[1].lower()
            lang = LANGUAGES.get(ext)
            if not lang:
                skipped["binary_or_unknown"] += 1
                continue
            if _is_generated(fn):
                skipped["generated"] += 1
                continue
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    skipped["too_large"] += 1
                    continue
            except OSError:
                continue

            loc = _loc(full)
            s.total_files += 1
            s.total_loc += loc
            L = s.languages.setdefault(lang, {"files": 0, "loc": 0})
            L["files"] += 1
            L["loc"] += loc

            d = os.path.relpath(dirpath, repo)
            d = "." if d == "." else d
            m = per_dir.setdefault(d, {"files": 0, "loc": 0, "langs": set()})
            m["files"] += 1
            m["loc"] += loc
            m["langs"].add(lang)

    for d, v in sorted(per_dir.items()):
        if v["files"] < module_min_files:
            continue
        mod = Module(
            path=d, files=v["files"], loc=v["loc"],
            languages=sorted(v["langs"]), role=_role_of(d),
        )
        if s.git.get("available"):
            st = _git_module_stats(repo, d, since)
            mod.commits = st.get("commits")
            mod.authors = st.get("authors")
            mod.last_commit = st.get("last_commit")
        s.modules.append(mod)

    # main antes de test; dentro de cada grupo, maior LOC primeiro. Assim o
    # `plan` nunca manda cavar teste antes do código de produção.
    s.modules.sort(key=lambda m: (m.role != "main", -m.loc))
    s.skipped = skipped

    if not s.total_files:
        s.warnings.append("nenhum arquivo de código reconhecido — extensão fora do mapa?")
    if not s.git.get("available"):
        s.warnings.append("sem git: churn e autores indisponíveis")
    if not s.manifests:
        s.warnings.append("nenhum manifest encontrado — dependências desconhecidas")
    if not s.entry_points:
        s.warnings.append("nenhum entry point óbvio — biblioteca, ou convenção própria")
    return s


def to_dict(s: Surface) -> dict:
    d = asdict(s)
    d["modules"] = [asdict(m) if not isinstance(m, dict) else m for m in s.modules]
    return d
