"""Build do `wk.pyz` — executável autocontido (código + documentação).

    python scripts/build_pyz.py [-o wk.pyz]

Empacota `wk/`, `codescan/` e `sbindex/` junto com os markdown da skill em
`sb/_docs/`. Testes e `__pycache__` ficam de fora. O artefato roda com
`python wk.pyz <comando>` — sem instalação, sem PYTHONPATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
PACKAGES = ("wk", "codescan", "sbindex", "knowledge", "analysis")

# Nome do arquivo do manifesto embutido no .pyz (dentro do pacote `wk`, para
# ser lido em runtime via `pkgutil.get_data("wk", ...)`, o mesmo mecanismo de
# `wk/cli.py:_read_asset`). FIX 5 (lote E): permite `wk doctor` detectar um
# `.pyz` desatualizado em relação ao `scripts/` que o gerou.
BUILD_MANIFEST_NAME = "_build_manifest.json"


def _source_sha256(scripts_dir: str) -> str:
    """Hash agregado determinístico de todo `*.py` sob `wk/`, `codescan/`,
    `sbindex/` (os mesmos pacotes empacotados por `stage()`, com os mesmos
    diretórios excluídos — `__pycache__`, `tests`).

    Mantido em sincronia manual com `wk/cli.py:_source_sha256` (duplicado lá
    de propósito: o `.pyz` não empacota este arquivo, então `wk doctor`
    rodando a partir do `.pyz` não pode importar `build_pyz`). Qualquer
    mudança aqui exige a mesma mudança lá, senão o comparador de `doctor`
    nunca bate mesmo com fonte idêntica.
    """
    entries = []
    for pkg in PACKAGES:
        pkg_dir = os.path.join(scripts_dir, pkg)
        if not os.path.isdir(pkg_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(pkg_dir):
            dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", "tests"))
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, scripts_dir).replace("\\", "/")
                with open(full, "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
                entries.append(f"{rel}:{digest}")
    entries.sort()
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()

# slug -> (caminho no repo, caminho relativo na instalação, título)
DOCS = {
    "skill": ("SKILL.md", "SKILL.md", "Roteador de operações (entrada da skill)"),
    "schema": ("schema.md", "schema.md", "Contrato de proveniência e portão"),
    "install": ("INSTALL.md", "INSTALL.md", "Instalação e fluxos de uso"),
    "ingest": ("operations/ingest.md", "operations/ingest.md", "Operação: ingest"),
    "ingest-codebase": (
        "operations/ingest-codebase.md",
        "operations/ingest-codebase.md",
        "Operação: ingest codebase (pipeline SDD)",
    ),
    "promote": ("operations/promote.md", "operations/promote.md", "Operação: promote"),
    "compile": ("operations/compile.md", "operations/compile.md", "Operação: compile"),
    "docx": ("operations/docx.md", "operations/docx.md", "Operação: docx"),
    "lint": ("operations/lint.md", "operations/lint.md", "Operação: lint"),
    "fluxo3": ("operations/fluxo3.md", "operations/fluxo3.md", "FLUXO 3 — máquina de estados e porquês"),
    "retrieval": (
        "references/retrieval.md",
        "references/retrieval.md",
        "Referência: recuperação e índice",
    ),
    "sdd-contract": (
        "references/sdd-contract.md",
        "references/sdd-contract.md",
        "Contrato dos artefatos SDD",
    ),
    "source-frontmatter": (
        "templates/source-frontmatter.yaml",
        "templates/source-frontmatter.yaml",
        "Template de proveniência",
    ),
}

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "tests")


def stage(tmp: str) -> dict:
    for pkg in PACKAGES:
        src = os.path.join(SCRIPTS, pkg)
        if not os.path.isdir(src):
            raise SystemExit(f"pacote ausente: {src}")
        shutil.copytree(src, os.path.join(tmp, pkg), ignore=IGNORE)

    docs_dir = os.path.join(tmp, "wk", "_docs")
    os.makedirs(docs_dir, exist_ok=True)
    manifest = {}
    for slug, (src_rel, install_rel, title) in DOCS.items():
        src = os.path.join(ROOT, src_rel)
        if not os.path.isfile(src):
            print(f"  aviso: documento ausente, pulando: {src_rel}", file=sys.stderr)
            continue
        flat = install_rel.replace("/", "__")
        shutil.copyfile(src, os.path.join(docs_dir, flat))
        manifest[slug] = {"file": install_rel, "asset": flat, "title": title}

    with open(os.path.join(docs_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # FIX 5 (lote E): hash agregado do `scripts/` de ORIGEM (SCRIPTS, não
    # `tmp` — o hash tem que representar a fonte real, não a cópia que acabou
    # de ser feita para dentro do zip) embutido no pacote `wk`, para
    # `wk doctor` comparar em runtime e detectar `.pyz` desatualizado.
    build_manifest = {
        "source_sha256": _source_sha256(SCRIPTS),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(os.path.join(tmp, "wk", BUILD_MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(build_manifest, f, ensure_ascii=False, indent=2)

    with open(os.path.join(tmp, "__main__.py"), "w", encoding="utf-8") as f:
        f.write("import sys\nfrom wk.cli import main\nsys.exit(main())\n")
    return manifest


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="build do wk.pyz")
    p.add_argument("-o", "--output", default=os.path.join(ROOT, "wk.pyz"))
    p.add_argument("--interpreter", default="/usr/bin/env python3")
    p.add_argument(
        "--unpacked",
        help="gera uma PASTA desempacotada (mesmo conteúdo do zip, executável "
        "com `python <pasta>`) em vez do .pyz",
    )
    a = p.parse_args(argv)

    if a.unpacked:
        dest = os.path.abspath(a.unpacked)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(dest, exist_ok=True)
        manifest = stage(dest)
        print(
            json.dumps(
                {"pasta": dest, "executar": f'python "{dest}"',
                 "documentos": sorted(manifest), "pacotes": list(PACKAGES),
                 "source_sha256": _source_sha256(SCRIPTS)},
                ensure_ascii=False, indent=2,
            )
        )
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        manifest = stage(tmp)
        zipapp.create_archive(tmp, target=a.output, interpreter=a.interpreter)

    size = os.path.getsize(a.output)
    print(
        json.dumps(
            {
                "artifact": os.path.abspath(a.output),
                "source_sha256": _source_sha256(SCRIPTS),
                "bytes": size,
                "kb": round(size / 1024, 1),
                "documentos": sorted(manifest),
                "pacotes": list(PACKAGES),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
