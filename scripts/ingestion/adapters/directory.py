"""Adapter de diretório: resultados POR FONTE, sem lote tudo-ou-nada (§7.1, §8.1).

Cada arquivo é preservado e extraído individualmente pelo adapter que o
reconhece. Uma fonte que falha vira um `SourceDocument` com
`extraction_failed`/`unsupported` e permanece no resultado ao lado das que
deram certo — §7.1 exige que "falha em um arquivo não apaga sucesso dos
demais", o que só é verificável se cada fonte carregar o próprio status.

O documento devolvido para o diretório em si não tem blocos: ele é o agregado.
`children` traz um `SourceDocument` por arquivo, e o status do agregado é
`partial` sempre que qualquer filho ficou incompleto — nunca `ingested` com um
filho falhado embaixo.
"""

from __future__ import annotations

import os
from typing import Iterable, Sequence

from ..normalize import (
    Diagnostic,
    INCOMPLETE_STATUS,
    Preserved,
    Severity,
    SourceDocument,
    SourceStatus,
    build_document,
    preserve,
)

NAME = "directory"
EXTENSIONS: frozenset[str] = frozenset()

#: Diretórios ignorados na varredura: ruído de ferramenta, não fonte.
SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", ".mypy_cache"}
)
#: Arquivos ignorados por nome.
SKIP_FILES = frozenset({".DS_Store", "Thumbs.db"})


def detect(path: str, head_bytes: bytes) -> bool:
    return os.path.isdir(path)


def iter_files(path: str, *, recursive: bool = True) -> list[str]:
    """Arquivos da árvore, em ordem determinística."""
    collected: list[str] = []
    if not recursive:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isfile(full) and name not in SKIP_FILES:
                collected.append(full)
        return collected
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name in SKIP_FILES:
                continue
            collected.append(os.path.join(root, name))
    return collected


def ingest_files(paths: Sequence[str]) -> list[SourceDocument]:
    """Ingere cada caminho isoladamente. Exceção vira resultado, não interrupção."""
    from .registry import ingest_many

    return ingest_many(paths)


def extract(
    path: str,
    preserved: Preserved | None = None,
    *,
    recursive: bool = True,
) -> SourceDocument:
    """Diretório → agregado com um `SourceDocument` por arquivo em `children`."""
    pres = preserved or preserve(path)
    files = iter_files(path, recursive=recursive)
    children = ingest_files(files)
    diags: list[Diagnostic] = []

    if not files:
        diags.append(
            Diagnostic(
                code="directory.empty",
                severity=Severity.WARNING,
                message="diretório sem arquivos ingeríveis",
                unavailable=("nenhuma fonte encontrada",),
                path=pres.path_original,
            )
        )

    failed = [c for c in children if c.status is SourceStatus.EXTRACTION_FAILED]
    unsupported = [c for c in children if c.status is SourceStatus.UNSUPPORTED]
    partial = [c for c in children if c.status is SourceStatus.PARTIAL]
    ok = [c for c in children if c.status is SourceStatus.INGESTED]

    for child in failed:
        diags.append(
            Diagnostic(
                code="directory.source_failed",
                severity=Severity.ERROR,
                message=(
                    f"{os.path.basename(child.path_original)}: extração falhou; "
                    "as demais fontes do lote foram preservadas"
                ),
                unavailable=tuple(child.unavailable) or (f"conteúdo de {child.path_original}",),
                path=child.path_original,
            )
        )
    for child in unsupported:
        diags.append(
            Diagnostic(
                code="directory.source_unsupported",
                severity=Severity.WARNING,
                message=(
                    f"{os.path.basename(child.path_original)}: formato sem adapter "
                    f"({child.mime_guess})"
                ),
                unavailable=tuple(child.unavailable) or (f"conteúdo de {child.path_original}",),
                path=child.path_original,
            )
        )
    for child in partial:
        diags.append(
            Diagnostic(
                code="directory.source_partial",
                severity=Severity.WARNING,
                message=f"{os.path.basename(child.path_original)}: extração parcial",
                unavailable=tuple(child.unavailable),
                path=child.path_original,
            )
        )
    diags.append(
        Diagnostic(
            code="directory.summary",
            severity=Severity.INFO,
            message=(
                f"{len(children)} fonte(s): {len(ok)} ingerida(s), {len(partial)} parcial(is), "
                f"{len(unsupported)} não suportada(s), {len(failed)} com falha de extração"
            ),
            path=pres.path_original,
        )
    )

    # O status do agregado é derivado dos filhos, não dos próprios diagnósticos:
    # um lote com 4 sucessos e 1 falha é `partial`, nunca `ingested` (o sucesso
    # das demais não some) e nunca `extraction_failed` (a falha de uma não
    # apaga as outras) — §7.1.
    incomplete = any(c.status in INCOMPLETE_STATUS for c in children)
    if not children:
        proposed = SourceStatus.EXTRACTION_FAILED
    elif incomplete and ok:
        proposed = SourceStatus.PARTIAL
    elif incomplete:
        proposed = SourceStatus.EXTRACTION_FAILED
    else:
        proposed = SourceStatus.INGESTED

    return build_document(
        pres,
        kind="directory",
        adapter=NAME,
        raw_metadata={},
        diagnostics=diags,
        proposed_status=proposed,
        children=children,
    )


def flatten(document: SourceDocument) -> Iterable[SourceDocument]:
    """Percorre o agregado devolvendo cada fonte-folha uma vez."""
    if document.children:
        for child in document.children:
            yield from flatten(child)
    else:
        yield document
