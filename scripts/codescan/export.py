"""Export determinístico de artefatos SDD a partir do surface.json.

Equivalente ao que o Scout do Reversa produz (inventory.md, dependencies.md),
mas gerado por código, não por LLM — então é 🟢 por construção e recebe
`source_type: code-repo` no frontmatter, pronto para o portão de promoção.

Contrato de artefatos modelado no Reversa (github.com/sandeco/reversa,
MIT © sandeco). Aqui nada é copiado do repo analisado além de metadados:
contagens, manifests e caminhos que o surface.py já extraiu.
"""

from __future__ import annotations

import os
import time


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _slug(name: str) -> str:
    out = []
    for ch in name.lower():
        out.append(ch if ch.isalnum() else "-")
    s = "".join(out).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s or "repo"


def _repo_name(surface: dict) -> str:
    return os.path.basename(str(surface.get("repo", "")).rstrip("/\\")) or "repo"


def _origin(surface: dict) -> str:
    repo = _repo_name(surface)
    head = (surface.get("git") or {}).get("head")
    return f"codescan {repo} @{head}" if head else f"codescan {repo} (sem git)"


def _frontmatter(surface: dict, artifact: str, topic: str | None) -> str:
    repo = _repo_name(surface)
    lines = [
        "---",
        f"id: sb-codescan-{_slug(repo)}-{artifact}",
        "source_type: code-repo",
        f'origin: "{_origin(surface)} — export determinístico ({artifact})"',
        f"captured_at: {_now()}",
        "promoted: false",
        "promoted_by:",
        "promoted_at:",
        "confidence: reviewed",
    ]
    if topic:
        lines.append(f"topic: {topic}")
    lines += ["supersedes:", "source_link:", "---", ""]
    return "\n".join(lines)


def _fmt_int(n) -> str:
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "?"


def _inv_overview(surface: dict) -> list[str]:
    git = surface.get("git") or {}
    out = [
        "## Visão geral",
        "",
        f"- Varredura: {surface.get('scanned_at', '?')}",
        f"- Arquivos de código: {_fmt_int(surface.get('total_files'))}",
        f"- LOC: {_fmt_int(surface.get('total_loc'))}",
    ]
    if git.get("available"):
        out.append(
            f"- Git: HEAD `{git.get('head')}` (branch `{git.get('branch')}`), "
            f"{_fmt_int(git.get('total_commits'))} commits, "
            f"último em {git.get('last_commit', '?')}"
        )
    else:
        out.append("- Git: indisponível — churn e autores não puderam ser medidos")
    out.append("")
    return out


def _inv_languages(surface: dict) -> list[str]:
    langs = surface.get("languages") or {}
    if not langs:
        return []
    out = ["## Linguagens", "", "| Linguagem | Arquivos | LOC |", "|---|---:|---:|"]
    for name, v in sorted(langs.items(), key=lambda x: -x[1].get("loc", 0)):
        out.append(f"| {name} | {_fmt_int(v.get('files'))} | {_fmt_int(v.get('loc'))} |")
    out.append("")
    return out


def _inv_entry_points(surface: dict) -> list[str]:
    eps = surface.get("entry_points") or []
    if not eps:
        return []
    out = ["## Entry points", "", "| Caminho | Indício |", "|---|---|"]
    for e in eps:
        out.append(f"| `{e.get('path')}` | {e.get('reason', '')} |")
    out.append("")
    return out


def _opt_int(v) -> str:
    return _fmt_int(v) if v is not None else "—"


def _inv_modules(surface: dict) -> list[str]:
    mods = surface.get("modules") or []
    if not mods:
        return []
    out = [
        "## Módulos (por LOC)",
        "",
        "| Módulo | Arquivos | LOC | Linguagens | Commits | Autores | Último commit |",
        "|---|---:|---:|---|---:|---:|---|",
    ]
    for m in mods:
        out.append(
            f"| `{m.get('path')}` | {_fmt_int(m.get('files'))} | {_fmt_int(m.get('loc'))} "
            f"| {', '.join(m.get('languages') or [])} "
            f"| {_opt_int(m.get('commits'))} "
            f"| {_opt_int(m.get('authors'))} "
            f"| {m.get('last_commit') or '—'} |"
        )
    out.append("")
    return out


def _inv_skips(surface: dict) -> list[str]:
    skipped = surface.get("skipped") or {}
    warnings = surface.get("warnings") or []
    if not skipped and not warnings:
        return []
    out = ["## Ignorados e avisos", ""]
    out += [f"- ignorados/{k}: {_fmt_int(v)}" for k, v in skipped.items()]
    out += [f"- ⚠️ {w}" for w in warnings]
    out.append("")
    return out


def render_inventory(surface: dict, topic: str | None = None) -> str:
    """inventory.md — o inventário do projeto, 100% derivado do surface.json."""
    out = [
        _frontmatter(surface, "inventory", topic),
        f"# Inventário — {_repo_name(surface)}",
        "",
        "Gerado deterministicamente pelo `codescan export` a partir do "
        "`surface.json`. Toda afirmação aqui é 🟢 por construção: contagem "
        "e metadado, não interpretação.",
        "",
    ]
    out += _inv_overview(surface)
    out += _inv_languages(surface)
    out += _inv_entry_points(surface)
    out += _inv_modules(surface)
    out += _inv_skips(surface)
    return "\n".join(out)


def render_dependencies(surface: dict, topic: str | None = None) -> str:
    """dependencies.md — manifests e dependências declaradas, com versões."""
    out = [_frontmatter(surface, "dependencies", topic)]
    out.append(f"# Dependências — {_repo_name(surface)}")
    out.append("")
    out.append(
        "Dependências **declaradas** nos manifests do repositório, extraídas "
        "deterministicamente. Não cobre dependências transitivas nem uso real "
        "no código."
    )
    out.append("")
    manifests = surface.get("manifests") or []
    if not manifests:
        out.append("Nenhum manifest encontrado — dependências desconhecidas (aviso do surface).")
        out.append("")
        return "\n".join(out)
    for m in manifests:
        out.append(f"## `{m.get('path')}` ({m.get('type')})")
        out.append("")
        deps = m.get("dependencies") or []
        if not deps:
            out.append("_Sem dependências declaradas (ou parse falhou; ver manifest original)._")
        for d in deps:
            out.append(f"- `{d}`")
        out.append("")
    return "\n".join(out)


def export_deterministic(surface: dict, outdir: str, topic: str | None = None) -> list[str]:
    """Escreve inventory.md e dependencies.md em <outdir>. Devolve os caminhos."""
    os.makedirs(outdir, exist_ok=True)
    written = []
    for name, render in (
        ("inventory.md", render_inventory),
        ("dependencies.md", render_dependencies),
    ):
        p = os.path.join(outdir, name)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(render(surface, topic))
        written.append(p)
    return written
