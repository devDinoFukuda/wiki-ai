"""Zonas de design — métricas de componente de Martin, determinísticas.

Ce (eferente), Ca (aferente), Instabilidade I = Ce/(Ce+Ca), Abstração A,
Distância D = |A + I − 1| e a Zona resultante.

Ce/Ca/I são 🟢: derivam do grafo de imports internos, reprodutível. Import é
extraído por regex por família de linguagem — aproxima (perde import dinâmico,
DI, reflection), como o parse de manifest do surface aproxima dependências.

Abstração A é 🟡: "interface/abstract" é heurística por linguagem. Logo as
Zonas (que dependem de A) também são 🟡 e vêm rotuladas assim.
"""

from __future__ import annotations

import os
import re

from .surface import LANGUAGES, SKIP_DIRS, _is_generated, MAX_FILE_BYTES

# --- extração de import (o alvo bruto do import) -----------------------------

# Ordem importa: padrões com aspas primeiro (o alvo real é a string), senão o
# bare `import X` do Java captura o identificador errado em `import x from '...'`.
_IMPORT_RES = [
    # JS/TS: import ... from '...' | require('...') | import '...'
    re.compile(r"""(?:from|require\s*\(|import)\s*['"]([^'"]+)['"]"""),
    # Ruby: require(_relative) '...'
    re.compile(r"""require(?:_relative)?\s*\(?\s*['"]([^'"]+)['"]"""),
    # C/C++: #include "path"
    re.compile(r'^\s*#include\s+"([^"]+)"'),
    # Go (dentro de import ( ... )): "path"
    re.compile(r'^\s*"([^"]+)"\s*$'),
    # Python: from a.b import c
    re.compile(r"^\s*from\s+([.\w]+)\s+import\b"),
    # Java/Kotlin/Scala/Python: import [static] a.b.C
    re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)"),
    # C#: using [static] A.B
    re.compile(r"^\s*using\s+(?:static\s+)?([\w.]+)\s*;"),
    # PHP: use A\B
    re.compile(r"^\s*use\s+([\w\\]+)\s*;"),
]

# Só marcadores de layout de build (Maven/Gradle/Python). NÃO incluir nomes
# ambíguos como app/lib/pkg — são pacotes reais e apareceriam no import.
_SRC_ROOTS = {"java", "kotlin", "scala", "src", "main", "resources"}


def _extract_targets(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or len(ln) > 300:
            continue
        for rx in _IMPORT_RES:
            m = rx.search(ln)
            if m:
                out.append(m.group(1))
                break
    return out


def _segs(path: str) -> list[str]:
    """Normaliza qualquer separador (. / \\ ::) em segmentos."""
    norm = re.sub(r"[.\\/]+|::", "/", path.strip())
    return [s for s in norm.split("/") if s and s not in (".", "..")]


def _pkg_suffix(module_path: str) -> list[str]:
    """Segmentos do módulo após a última raiz de fonte (o 'pacote')."""
    segs = _segs(module_path)
    last = -1
    for i, s in enumerate(segs):
        if s.lower() in _SRC_ROOTS:
            last = i
    return segs[last + 1:] if last >= 0 else segs


# --- construção do grafo -----------------------------------------------------


def _module_of(rel_dir: str, module_dirs: list[str]) -> str | None:
    """Módulo dono de um diretório: o mais longo que é prefixo dele."""
    rd = rel_dir.replace("\\", "/")
    best = None
    for m in module_dirs:
        mm = m.replace("\\", "/")
        if rd == mm or rd.startswith(mm + "/"):
            if best is None or len(mm) > len(best):
                best = m
    return best


def _resolve_target(target: str, importer_dir: str, modules: dict) -> str | None:
    """Resolve um import a um módulo interno. Devolve o path do módulo ou None."""
    segs = _segs(target)
    if not segs:
        return None

    # import relativo (JS/TS/Ruby/C): resolve pelo caminho do arquivo importador
    if target.startswith(".") or target.startswith("./") or target.startswith("../"):
        base = importer_dir.replace("\\", "/").split("/")
        for s in _segs(target):
            base.append(s)
        resolved = "/".join([p for p in _normalize_rel(base)])
        return _module_of(os.path.dirname(resolved) or resolved,
                          list(modules["dirs"]))

    # import por pacote (Java/C#/Python/Go): casa o pkg-suffix do módulo como
    # prefixo dos segmentos do import (ignorando o símbolo final).
    best, best_len = None, 0
    for mpath, suffix in modules["suffix"].items():
        if suffix and _is_prefix(suffix, segs) and len(suffix) > best_len:
            best, best_len = mpath, len(suffix)
    return best


def _looks_internal_target(target: str, modules: dict) -> bool:
    segs = _segs(target)
    if not segs:
        return False
    suffixes = [s for s in modules["suffix"].values() if s]
    roots = {s[0] for s in suffixes if s}
    if roots and segs[0] in roots:
        return True
    return any(any(part in segs for part in suffix) for suffix in suffixes)


def _normalize_rel(parts: list[str]) -> list[str]:
    out: list[str] = []
    for p in parts:
        if p == "..":
            if out:
                out.pop()
        elif p and p != ".":
            out.append(p)
    return out


def _is_prefix(prefix: list[str], seq: list[str]) -> bool:
    return len(prefix) <= len(seq) and seq[: len(prefix)] == prefix


# --- abstração (heurística, 🟡) ----------------------------------------------

_ABSTRACT_RE = re.compile(
    r"\b(interface|abstract\s+class|abstract\s+fun|trait|protocol)\b"
    r"|@abstractmethod|\bABC\b", re.I)
_CONCRETE_RE = re.compile(
    r"\b(class|struct|enum|record|object|type\s+\w+\s+struct)\b", re.I)


def _abstractness(files_text: list[str]) -> float | None:
    absn = conc = 0
    for txt in files_text:
        for ln in txt.splitlines():
            if _ABSTRACT_RE.search(ln):
                absn += 1
            elif _CONCRETE_RE.search(ln):
                conc += 1
    total = absn + conc
    return (absn / total) if total else None


# --- zonas -------------------------------------------------------------------


def _zone(i: float, a: float | None) -> str:
    if a is None:
        return "indeterminada (sem abstração)"
    if i <= 0.3 and a <= 0.3:
        return "dor"
    if i >= 0.7 and a >= 0.7:
        return "inutilidade"
    d = abs(a + i - 1)
    return "saudável" if d <= 0.3 else "transição"


# --- API ---------------------------------------------------------------------


def analyze(repo: str, surface: dict) -> dict:
    """Calcula Ce/Ca/I/A/D/zona por módulo main do surface."""
    repo = os.path.abspath(repo)
    mods = [m for m in surface.get("modules", []) if m.get("role", "main") == "main"]
    module_dirs = [m["path"] for m in mods]
    modules = {
        "dirs": module_dirs,
        "suffix": {p: _pkg_suffix(p) for p in module_dirs},
    }

    edges: set[tuple[str, str]] = set()
    unresolved_internal_imports = 0
    file_texts: dict[str, list[str]] = {m: [] for m in module_dirs}

    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, repo)
        owner = _module_of(rel_dir, module_dirs)
        if owner is None:
            continue
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in LANGUAGES or _is_generated(fn):
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, encoding="utf-8-sig", errors="replace") as f:
                    txt = f.read()
            except OSError:
                continue
            file_texts[owner].append(txt)
            imp_dir = os.path.relpath(dirpath, repo)
            for tgt in _extract_targets(txt):
                dest = _resolve_target(tgt, imp_dir, modules)
                if dest and dest != owner:
                    edges.add((owner, dest))
                elif not dest and _looks_internal_target(tgt, modules):
                    unresolved_internal_imports += 1

    ce = {m: 0 for m in module_dirs}
    ca = {m: 0 for m in module_dirs}
    for src, dst in edges:
        ce[src] += 1
        ca[dst] += 1

    out = []
    for m in module_dirs:
        c_e, c_a = ce[m], ca[m]
        inst = (c_e / (c_e + c_a)) if (c_e + c_a) else None
        absn = _abstractness(file_texts[m])
        dist = abs((absn or 0) + inst - 1) if inst is not None and absn is not None else None
        out.append({
            "module": m,
            "ce": c_e, "ca": c_a,
            "instability": None if inst is None else round(inst, 2),
            "abstractness": None if absn is None else round(absn, 2),
            "distance": None if dist is None else round(dist, 2),
            "zone": _zone(inst, absn) if inst is not None else "isolado",
        })
    out.sort(key=lambda r: (r["distance"] is None, -(r["distance"] or 0)))
    return {
        "modules": out,
        "edges": sorted(edges),
        "unresolved_internal_imports": unresolved_internal_imports,
    }


# --- render ------------------------------------------------------------------


def _leaf(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").split("/")[-1] or path


_MERMAID_POINT_RE = re.compile(r"^P[0-9]+$")


def _mermaid_point_name(index: int) -> str:
    """Identificador Mermaid ASCII, sem depender do nome do modulo."""
    if index < 1:
        raise ValueError("indice de ponto Mermaid deve iniciar em 1")
    return f"P{index:03d}"


def _mermaid_point_line(index: int, instability: float, abstractness: float) -> str:
    name = _mermaid_point_name(index)
    if not _MERMAID_POINT_RE.match(name):
        raise ValueError(f"identificador Mermaid invalido: {name!r}")
    return f"  {name}: [{instability:.2f}, {abstractness:.2f}]"


def _mermaid_node_label(row: dict) -> str:
    leaf = _leaf(row["module"])
    leaf = re.sub(r"[^A-Za-z0-9_.-]+", " ", leaf).strip() or "modulo"
    if len(leaf) > 36:
        leaf = leaf[:33] + "..."
    instability = row.get("instability")
    abstractness = row.get("abstractness")
    i = "NA" if instability is None else f"{instability:.2f}"
    a = "NA" if abstractness is None else f"{abstractness:.2f}"
    return f"{leaf} | I={i} A={a}"


def _mermaid_label(text: str, limit: int = 64) -> str:
    label = re.sub(r"[\r\n\t]+", " ", str(text))
    label = re.sub(r"\s+", " ", label).strip() or "modulo"
    label = label.replace("\\", "/").replace('"', "'")
    if len(label) > limit:
        label = label[: limit - 3].rstrip() + "..."
    return label


def _zone_key(zone: str) -> str:
    mapped = {
        "dor": "dor",
        "transição": "transicao",
        "saudável": "saudavel",
        "concreto-instável": "instavel",
        "inutilidade": "inutilidade",
    }
    if zone in mapped:
        return mapped[zone]
    return re.sub(r"[^A-Za-z0-9_]+", "_", _ascii(zone).lower()).strip("_") or "zona"


def _ascii(text: str) -> str:
    import unicodedata

    raw = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")


def _module_ids(mods: list[dict]) -> dict[str, str]:
    return {r["module"]: f"M{idx:03d}" for idx, r in enumerate(mods, start=1)}


def render(surface: dict, analysis: dict, topic: str | None, now: str) -> str:
    from .export import _repo_name, _origin

    repo = _repo_name(surface)
    mods = analysis["modules"]
    fm = [
        "---",
        f"id: sb-codescan-{repo}-coupling",
        "source_type: code-repo",
        f'origin: "{_origin(surface)} — coupling (grafo de imports)"',
        f"captured_at: {now}",
        "promoted: false",
        "promoted_by:",
        "promoted_at:",
        "confidence: reviewed",
    ]
    if topic:
        fm.append(f"topic: {topic}")
    fm += ["supersedes:", "source_link:", "---", ""]

    out = fm + [
        f"# Zonas de design — {repo}",
        "",
        f"Grafo: {len(analysis['edges'])} arestas, {len(mods)} módulos. "
        "🟢 Ce/Ca/I (imports, aproximado) · 🟡 A/zona (heurística).",
        "",
        "## Métricas por módulo",
        "",
        "| Módulo | Ce 🟢 | Ca 🟢 | I 🟢 | A 🟡 | D | Zona 🟡 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in mods:
        def _f(v):
            return "—" if v is None else f"{v:.2f}"
        out.append(
            f"| `{r['module']}` | {r['ce']} | {r['ca']} | {_f(r['instability'])} "
            f"| {_f(r['abstractness'])} | {_f(r['distance'])} | {r['zone']} |"
        )
    out.append("")

    pts = [r for r in mods if r["instability"] is not None and r["abstractness"] is not None]
    if pts:
        zone_order = {"dor": 0, "transição": 1, "saudável": 2, "concreto-instável": 3, "inutilidade": 4}
        diagram_pts = sorted(
            pts,
            key=lambda r: (
                zone_order.get(r.get("zone", ""), 99),
                -(r.get("distance") or 0),
                r.get("module", ""),
            ),
        )[:24]
        out += [
            "## Plano Abstração × Instabilidade",
            "",
            "Diagrama determinístico por zona; a tabela acima mantém todos os módulos.",
            "",
            "```mermaid",
            "flowchart LR",
            "  classDef dor fill:#fee2e2,stroke:#991b1b,color:#111827",
            "  classDef transicao fill:#fef3c7,stroke:#92400e,color:#111827",
            "  classDef saudavel fill:#dcfce7,stroke:#166534,color:#111827",
            "  classDef instavel fill:#dbeafe,stroke:#1d4ed8,color:#111827",
            "  classDef inutilidade fill:#f3e8ff,stroke:#7e22ce,color:#111827",
        ]
        grouped: dict[str, list[tuple[int, dict]]] = {}
        for idx, r in enumerate(diagram_pts, start=1):
            grouped.setdefault(_zone_key(r["zone"]), []).append((idx, r))
        for zid, title in (
            ("dor", "Dor"),
            ("transicao", "Transicao"),
            ("saudavel", "Saudavel"),
            ("instavel", "Concreto Instavel"),
            ("inutilidade", "Inutilidade"),
        ):
            rows = grouped.get(zid) or []
            if not rows:
                continue
            out.append(f'  subgraph zone_{zid}["{title}"]')
            hub = f"Z{zid.upper()}"
            out.append(f'    {hub}["{title}"]')
            for idx, r in rows:
                node = _mermaid_point_name(idx)
                out.append(f'    {node}["{_mermaid_node_label(r)}"]')
                out.append(f"    {hub} --> {node}")
            out.append("  end")
            nodes = ",".join([hub] + [_mermaid_point_name(idx) for idx, _r in rows])
            out.append(f"  class {nodes} {zid}")
        out += ["```", ""]

    if len(mods) >= 2:
        ids = _module_ids(mods)
        out += [
            "## Grafo de dependências internas",
            "",
            "```mermaid",
            "flowchart LR",
        ]
        for module, node in ids.items():
            out.append(f'  {node}["{_mermaid_label(_leaf(module))}"]')
        for src, dst in analysis.get("edges", []):
            if src in ids and dst in ids:
                out.append(f"  {ids[src]} --> {ids[dst]}")
        out += ["```", ""]
        if not analysis.get("edges") and analysis.get("unresolved_internal_imports", 0):
            out += [
                "## Aviso de resolução de imports",
                "",
                "- 🔴 Imports internos detectados não foram resolvidos como arestas.",
                "",
            ]

    pain = [r for r in mods if r["zone"] == "dor"]
    useless = [r for r in mods if r["zone"] == "inutilidade"]
    if pain:
        out += ["## Zona de dor", ""]
        out += [f"- `{r['module']}` (Ca={r['ca']}, I={r['instability']:.2f})" for r in pain]
        out.append("")
    if useless:
        out += ["## Zona de inutilidade", ""]
        out += [f"- `{r['module']}` (I={r['instability']:.2f}, A={r['abstractness']:.2f})" for r in useless]
        out.append("")
    return "\n".join(out)
