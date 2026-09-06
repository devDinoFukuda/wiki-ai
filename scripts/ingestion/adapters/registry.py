"""Cadeia de detecção e ponto de entrada da ingestão de uma fonte (§8.1).

A ordem de `ADAPTERS` importa e não é alfabética: adapters com assinatura de
bytes inequívoca vêm antes dos que decidem por heurística. `transcript` precede
`markdown_txt` porque um `.txt` com falas é transcrição — se a ordem fosse
invertida, o `.txt` viraria prosa e o intervalo de tempo de cada fala se
perderia. `markdown_txt` é o último elo justamente por aceitar texto genérico.

Formato sem adapter não é exceção nem silêncio: vira um `SourceDocument` com
`status=unsupported` e diagnóstico dizendo o que ficou indisponível (§8.1).

`ingest` também executa `reverify`: os bytes da fonte são re-hasheados depois da
extração e qualquer divergência vira diagnóstico de erro. É a verificação
executável de que a extração não altera o conteúdo original.
"""

from __future__ import annotations

import os
from types import ModuleType
from typing import Sequence

from ..normalize import (
    Diagnostic,
    Preserved,
    Severity,
    SourceDocument,
    SourceStatus,
    build_document,
    preserve,
    unsupported_document,
)
from . import (
    directory,
    docx_adapter,
    html_adapter,
    json_xml,
    markdown_txt,
    pdf_adapter,
    transcript,
)

#: Ordem da cadeia de detecção. Ver docstring do módulo.
ADAPTERS: tuple[ModuleType, ...] = (
    directory,
    docx_adapter,
    pdf_adapter,
    html_adapter,
    json_xml,
    transcript,
    markdown_txt,
)

#: Bytes lidos para a decisão de `detect`. Suficiente para assinatura, cabeçalho
#: HTML/XML e algumas linhas de transcrição.
HEAD_BYTES = 8192


def read_head(path: str, size: int = HEAD_BYTES) -> bytes:
    """Primeiros bytes da fonte, somente leitura."""
    if os.path.isdir(path):
        return b""
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def detect_adapter(path: str, head_bytes: bytes | None = None) -> ModuleType | None:
    """Primeiro adapter da cadeia que reconhece a fonte, ou `None`."""
    head = read_head(path) if head_bytes is None else head_bytes
    for adapter in ADAPTERS:
        try:
            if adapter.detect(path, head):
                return adapter
        except Exception:
            # Um `detect` que explode não pode derrubar a cadeia inteira: o
            # próximo adapter ainda tem chance de reconhecer a fonte.
            continue
    return None


def _unsupported(path: str, preserved: Preserved) -> SourceDocument:
    ext = os.path.splitext(path)[1].lower() or "(sem extensão)"
    return unsupported_document(
        preserved,
        kind=ext.lstrip(".") or "unknown",
        adapter="registry",
        reason=(
            f"nenhum adapter reconhece '{ext}' (mime aparente: {preserved.mime_guess}); "
            f"formatos suportados: {', '.join(sorted(supported_extensions()))}"
        ),
        unavailable=(
            f"todo o conteúdo de {os.path.basename(path)} "
            f"({preserved.size} bytes, {preserved.mime_guess})",
        ),
    )


def supported_extensions() -> set[str]:
    """Extensões declaradas pelos adapters da cadeia."""
    out: set[str] = set()
    for adapter in ADAPTERS:
        out |= {e for e in getattr(adapter, "EXTENSIONS", frozenset()) if e}
    return out


def ingest(path: str, *, preserved: Preserved | None = None) -> SourceDocument:
    """Preserva, detecta, extrai e reverifica UMA fonte.

    Nunca levanta por causa do conteúdo: erro de adapter vira
    `extraction_failed` com a exceção registrada, para que um lote não seja
    interrompido por uma fonte ruim (§7.1).
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    # Preservação SEMPRE antes da extração (§8.1).
    pres = preserved or preserve(path)
    adapter = detect_adapter(path, None if os.path.isdir(path) else pres.raw[:HEAD_BYTES])
    if adapter is None:
        return _unsupported(path, pres)
    try:
        document = adapter.extract(path, pres)
    except Exception as exc:
        from ..normalize import failed_document

        document = failed_document(
            pres,
            kind=os.path.splitext(path)[1].lstrip(".").lower() or "unknown",
            adapter=getattr(adapter, "NAME", "?"),
            reason=(
                f"adapter {getattr(adapter, 'NAME', '?')} falhou com "
                f"{type(exc).__name__}: {exc}"
            ),
            unavailable=(f"todo o conteúdo de {os.path.basename(path)}",),
        )
    mutation = _reverify(pres)
    if mutation is not None:
        document = _with_diagnostic(document, mutation)
    return document


def _reverify(preserved: Preserved) -> Diagnostic | None:
    from ..normalize import reverify

    return reverify(preserved)


def _with_diagnostic(document: SourceDocument, diagnostic: Diagnostic) -> SourceDocument:
    """Acrescenta diagnóstico e recalcula o status, que só pode piorar."""
    import dataclasses

    from ..normalize import decide_status

    diagnostics = document.diagnostics + (diagnostic,)
    proposed = (
        document.status
        if document.status in (SourceStatus.UNSUPPORTED, SourceStatus.EXTRACTION_FAILED)
        else None
    )
    return dataclasses.replace(
        document,
        diagnostics=diagnostics,
        status=decide_status(
            document.blocks,
            diagnostics,
            proposed=proposed,
            aggregate=bool(document.children),
        ),
    )


def ingest_many(paths: Sequence[str]) -> list[SourceDocument]:
    """Resultado POR FONTE; falha em uma não apaga o sucesso das demais (§7.1)."""
    results: list[SourceDocument] = []
    for path in paths:
        try:
            results.append(ingest(path))
        except Exception as exc:
            pres = Preserved(
                path_original=os.path.abspath(path),
                bytes_sha256="",
                size=0,
                mime_guess="application/octet-stream",
            )
            results.append(
                build_document(
                    pres,
                    kind="unknown",
                    adapter="registry",
                    diagnostics=[
                        Diagnostic(
                            code="source.unreadable",
                            severity=Severity.ERROR,
                            message=f"fonte inacessível: {type(exc).__name__}: {exc}",
                            unavailable=(f"todo o conteúdo de {path}",),
                            path=os.path.abspath(path),
                        )
                    ],
                    proposed_status=SourceStatus.EXTRACTION_FAILED,
                )
            )
    return results
