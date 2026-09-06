"""Ingestão com preservação de origem (plano §8.1, onda W5).

Ponto de entrada típico:

    from ingestion import ingest

    doc = ingest("inception-INI-008.docx")
    if not doc.complete:
        print(doc.status.value, doc.unavailable)

Um diretório devolve o agregado, com um resultado por fonte em `children`:

    lote = ingest("inbox/INI-008/")
    for fonte in lote.children:
        print(fonte.status.value, fonte.path_original)

Fronteiras deste pacote:

- Preserva bytes, hash, tipo e origem ANTES da extração, e reverifica depois.
- Produz blocos com localizador no formato de `knowledge.evidence` (§5.4).
- NÃO converte transcrição em fato: afirmações, decisões, dúvidas e ações são
  responsabilidade de `ingestion.extract`; aqui a saída são blocos preservados
  com o contexto {iniciativa, fase, participante, momento, versão} quando ele
  existe nos metadados ou no conteúdo (§8.1).
- Metadado de fonte é dado, nunca instrução: `normalize.METADATA_WHITELIST`
  é fechada e `normalize.policy_from_metadata` devolve sempre `{}`.

Só stdlib; importa `knowledge` (evidence/models) e nada de `wk`/`codescan`/
`sbindex`.
"""

from .normalize import (  # noqa: F401
    METADATA_WHITELIST,
    Block,
    BlockKind,
    Diagnostic,
    Preserved,
    Severity,
    SourceDocument,
    SourceStatus,
    policy_from_metadata,
    preserve,
    sanitize_metadata,
)


def ingest(path: str) -> SourceDocument:
    """Ingere um arquivo ou diretório, devolvendo o resultado da fonte."""
    from .adapters.registry import ingest as _ingest

    return _ingest(path)


def ingest_many(paths):
    """Ingere várias fontes; resultado por fonte, sem lote tudo-ou-nada (§7.1)."""
    from .adapters.registry import ingest_many as _ingest_many

    return _ingest_many(list(paths))


def detect_adapter(path: str):
    """Adapter que reconhece a fonte, ou `None` se o formato não é suportado."""
    from .adapters.registry import detect_adapter as _detect

    return _detect(path)


__all__ = [
    "Block",
    "BlockKind",
    "Diagnostic",
    "METADATA_WHITELIST",
    "Preserved",
    "Severity",
    "SourceDocument",
    "SourceStatus",
    "detect_adapter",
    "ingest",
    "ingest_many",
    "policy_from_metadata",
    "preserve",
    "sanitize_metadata",
]
