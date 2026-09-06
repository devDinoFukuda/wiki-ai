"""Representação normalizada que preserva a origem (plano §8.1, onda W5).

Três responsabilidades, todas executáveis:

1. **Preservação antes da extração** (`preserve`): bytes, hash, tamanho e tipo
   são registrados ANTES de qualquer adapter tocar no conteúdo. `reverify`
   re-hasheia depois da extração; se o dígito mudou, a extração alterou a
   fonte e isso vira diagnóstico — não silêncio.
2. **Localizadores no formato de `knowledge.evidence`**: `document_locator` e
   `transcript_locator` constroem o localizador e o passam por
   `knowledge.evidence.validate_locator`. Um bloco cujo localizador não
   sobreviveria à criação de uma `Evidence` não é produzido aqui.
3. **Metadado é dado, nunca instrução** (§8.1): `sanitize_metadata` aplica uma
   whitelist fechada de chaves; tudo o mais cai em `metadata_extra`, inerte.
   `policy_from_metadata` existe para provar em código que nenhum campo de
   metadata alimenta política: ela retorna sempre `{}`.

Só stdlib. Importa `knowledge` (evidence/models) e nada de `wk`/`codescan`/
`sbindex`.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import mimetypes
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

# `knowledge` é irmão de `ingestion` dentro de scripts/. Dependendo de como o
# processo montou o sys.path o pacote se chama `knowledge` ou
# `scripts.knowledge`; ambos apontam para os mesmos módulos. A resolução é
# feita UMA vez aqui e reexportada, para que nenhum adapter repita o try/except.
try:  # pragma: no cover - depende do sys.path do processo
    from knowledge.evidence import snippet_hash, validate_locator
    from knowledge.models import ContentKind, LocatorInvalid, SourceKind
except ImportError:  # pragma: no cover
    from scripts.knowledge.evidence import snippet_hash, validate_locator
    from scripts.knowledge.models import ContentKind, LocatorInvalid, SourceKind

__all__ = [
    "BlockKind",
    "SourceStatus",
    "Severity",
    "Diagnostic",
    "Block",
    "Preserved",
    "SourceDocument",
    "METADATA_WHITELIST",
    "MetadataNotInert",
    "preserve",
    "reverify",
    "sanitize_metadata",
    "policy_from_metadata",
    "assert_inert",
    "version_label",
    "make_block_id",
    "make_block",
    "document_locator",
    "transcript_locator",
    "decide_status",
    "build_document",
    "unsupported_document",
    "failed_document",
    "utc_now",
    "SourceKind",
    "ContentKind",
    "LocatorInvalid",
    "validate_locator",
    "snippet_hash",
]


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class BlockKind(str, enum.Enum):
    """Natureza estrutural do bloco preservado (§8.1)."""

    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE = "table"
    LIST = "list"
    CODE = "code"
    CAPTION = "caption"
    TIMESTAMPED_UTTERANCE = "timestamped_utterance"


class SourceStatus(str, enum.Enum):
    """Resultado da ingestão de UMA fonte.

    `INGESTED` só é usado quando nada ficou indisponível. Extração parcial é
    `PARTIAL`; formato sem adapter é `UNSUPPORTED`; adapter que existe mas não
    conseguiu ler o conteúdo é `EXTRACTION_FAILED`. §8.1 proíbe devolver
    ingestão integral bem-sucedida quando algo ficou de fora.
    """

    INGESTED = "ingested"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    EXTRACTION_FAILED = "extraction_failed"


#: Status que NÃO podem ser apresentados como sucesso integral.
INCOMPLETE_STATUS: frozenset[SourceStatus] = frozenset(
    {SourceStatus.PARTIAL, SourceStatus.UNSUPPORTED, SourceStatus.EXTRACTION_FAILED}
)


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# --------------------------------------------------------------------------
# Metadado é dado, nunca instrução (§8.1)
# --------------------------------------------------------------------------


#: Whitelist FECHADA de chaves de metadata (§8.1). Qualquer outra chave vai
#: para `metadata_extra` e não é lida por nada além de exibição.
METADATA_WHITELIST: tuple[str, ...] = (
    "initiative_id",
    "phase",
    "participants",
    "date",
    "title",
    "source_type",
)

#: Chaves que uma fonte hostil usaria para tentar virar instrução. Não é a
#: defesa (a defesa é a whitelist acima, que já as exclui): é o diagnóstico,
#: para que a tentativa fique registrada em vez de sumir em silêncio.
SUSPICIOUS_KEYS: frozenset[str] = frozenset(
    {
        "instruction",
        "instructions",
        "prompt",
        "system",
        "system_prompt",
        "policy",
        "policies",
        "approve",
        "approved",
        "approved_by",
        "command",
        "commands",
        "exec",
        "execute",
        "run",
        "shell",
        "tool",
        "tools",
        "allow",
        "allowed_tools",
        "permissions",
        "epistemic_status",
        "nature",
        "supported",
        "trust",
        "budget",
    }
)

#: Tamanho máximo de um valor escalar de metadata, em caracteres.
MAX_METADATA_VALUE = 4000
#: Número máximo de itens preservados em um valor de lista de metadata.
MAX_METADATA_ITEMS = 200


class MetadataNotInert(ValueError):
    """Valor de metadata que não é dado puro (§8.1)."""


def _inert_scalar(value: Any) -> Any:
    """Reduz um valor a dado puro: str, int, float ou bool."""
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value
    if isinstance(value, str):
        return value[:MAX_METADATA_VALUE]
    if value is None:
        return ""
    # Qualquer outra coisa vira texto: um objeto de metadata jamais é chamado,
    # avaliado ou despachado.
    return json.dumps(value, ensure_ascii=False, default=str)[:MAX_METADATA_VALUE]


def _inert(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_inert_scalar(v) for v in list(value)[:MAX_METADATA_ITEMS]]
    if isinstance(value, Mapping):
        return json.dumps(dict(value), ensure_ascii=False, default=str)[:MAX_METADATA_VALUE]
    return _inert_scalar(value)


def assert_inert(mapping: Mapping[str, Any]) -> None:
    """Falha se algum valor de metadata for executável/invocável.

    Chamado por `sanitize_metadata` no resultado que ela mesma produz: é a
    verificação de que a normalização realmente reduziu tudo a dado.
    """
    for key, value in mapping.items():
        if callable(value):
            raise MetadataNotInert(f"metadata['{key}'] é invocável; metadado não executa (§8.1)")
        if isinstance(value, list):
            for item in value:
                if callable(item) or isinstance(item, (list, dict, tuple)):
                    raise MetadataNotInert(
                        f"metadata['{key}'] contém item não escalar; metadado é dado plano (§8.1)"
                    )
        elif not isinstance(value, (str, int, float, bool)):
            raise MetadataNotInert(
                f"metadata['{key}'] é {type(value).__name__}; só escalar ou lista de escalares (§8.1)"
            )


def sanitize_metadata(
    raw: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], list["Diagnostic"]]:
    """Separa metadata whitelisted de `metadata_extra` inerte (§8.1).

    Devolve `(metadata, metadata_extra, diagnostics)`. Nenhuma chave fora de
    `METADATA_WHITELIST` chega em `metadata`, então nenhum campo vindo do
    documento pode alterar política, aprovar conhecimento ou executar ação.
    """
    metadata: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    diags: list[Diagnostic] = []
    for key, value in dict(raw or {}).items():
        norm = str(key).strip().lower().replace("-", "_").replace(" ", "_")
        if norm in METADATA_WHITELIST:
            metadata[norm] = _inert(value)
        else:
            extra[str(key)[:200]] = _inert(value)
            if norm in SUSPICIOUS_KEYS:
                diags.append(
                    Diagnostic(
                        code="metadata.instruction_attempt",
                        severity=Severity.WARNING,
                        message=(
                            f"chave de metadata '{key}' parece instrução/política; tratada como "
                            "dado inerte em metadata_extra e ignorada pelo pipeline (§8.1)"
                        ),
                    )
                )
            else:
                diags.append(
                    Diagnostic(
                        code="metadata.not_whitelisted",
                        severity=Severity.INFO,
                        message=(
                            f"chave de metadata '{key}' fora da whitelist "
                            f"({', '.join(METADATA_WHITELIST)}); preservada inerte em metadata_extra"
                        ),
                    )
                )
    if isinstance(metadata.get("participants"), (str, int, float, bool)):
        raw_participants = str(metadata["participants"])
        metadata["participants"] = [p.strip() for p in raw_participants.split(",") if p.strip()]
    assert_inert(metadata)
    assert_inert(extra)
    return metadata, extra, diags


def policy_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Política derivável de metadata de fonte: nenhuma, por construção (§8.1).

    Retorna sempre `{}`, independentemente do conteúdo. É a forma executável de
    afirmar que metadata de documento não altera orçamento, ferramentas,
    permissões, aprovação nem estado epistêmico.
    """
    return {}


# --------------------------------------------------------------------------
# Diagnóstico
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Diagnostic:
    """O que ficou indisponível e por quê (§8.1)."""

    code: str
    severity: Severity
    message: str
    #: Enumeração explícita do que não pôde ser extraído.
    unavailable: tuple[str, ...] = ()
    #: Caminho da fonte, quando o diagnóstico é de um item de um lote.
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "unavailable": list(self.unavailable),
            "path": self.path,
        }


# --------------------------------------------------------------------------
# Bloco
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """Unidade preservada da fonte, com localizador resolvível.

    `block_id` é estável: índice de ordem + hash do conteúdo. Reingerir a mesma
    fonte produz os mesmos ids; mudar o texto de um bloco muda o id daquele
    bloco e não desloca os demais.
    """

    block_id: str
    kind: BlockKind
    text: str
    locator: Mapping[str, Any]
    index: int
    #: Qual schema de localizador este bloco usa (DOCUMENT ou TRANSCRIPT).
    source_kind: SourceKind = SourceKind.DOCUMENT
    #: `content_kind` que a evidência derivada deste bloco deve declarar.
    content_kind: ContentKind = ContentKind.PROSE
    #: Dados estruturados preservados (JSON/XML), fora do localizador fechado.
    data: Any = None
    #: {iniciativa, fase, participante, momento, versao} — §8.1.
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "kind": self.kind.value,
            "text": self.text,
            "locator": dict(self.locator),
            "index": self.index,
            "source_kind": self.source_kind.value,
            "content_kind": self.content_kind.value,
            "data": self.data,
            "context": dict(self.context),
        }


def make_block_id(index: int, kind: BlockKind, text: str) -> str:
    """Id estável: índice de ordem + hash do conteúdo (§8.1)."""
    digest = hashlib.sha256(f"{kind.value}\x00{text}".encode("utf-8")).hexdigest()
    return f"blk-{index:05d}-{digest[:12]}"


def make_block(
    index: int,
    kind: BlockKind,
    text: str,
    *,
    source_kind: SourceKind = SourceKind.DOCUMENT,
    content_kind: ContentKind | None = None,
    data: Any = None,
    version: str,
    section: str = "",
    file: str = "",
    paragraph: int | None = None,
    heading_path: Sequence[str] | None = None,
    page: int | None = None,
    time_start: str | None = None,
    time_end: str | None = None,
    speaker: str | None = None,
) -> Block:
    """Cria um bloco cujo localizador já passou por `validate_locator`."""
    block_id = make_block_id(index, kind, text)
    if source_kind is SourceKind.TRANSCRIPT:
        locator = transcript_locator(
            file=file,
            version=version,
            block=block_id,
            time_start=time_start,
            time_end=time_end,
            speaker=speaker,
        )
        default_content = ContentKind.TRANSCRIPT_BLOCK
    else:
        locator = document_locator(
            version=version,
            section=section,
            block=block_id,
            paragraph=paragraph,
            heading_path=heading_path,
            page=page,
        )
        default_content = ContentKind.PROSE
    return Block(
        block_id=block_id,
        kind=kind,
        text=text,
        locator=locator,
        index=index,
        source_kind=source_kind,
        content_kind=content_kind or default_content,
        data=data,
    )


# --------------------------------------------------------------------------
# Localizadores no formato de knowledge.evidence (§5.4)
# --------------------------------------------------------------------------


def version_label(bytes_sha256: str) -> str:
    """Rótulo de versão da fonte, derivado do hash preservado."""
    return f"sha256:{bytes_sha256[:16]}"


def document_locator(
    *,
    version: str,
    section: str,
    block: str,
    paragraph: int | None = None,
    heading_path: Sequence[str] | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Localizador `SourceKind.DOCUMENT`: versão, seção e parágrafo/bloco (§5.4).

    `section` vazia vira `"(raiz)"`: `validate_locator` rejeita campo
    obrigatório vazio, e um bloco antes do primeiro heading ainda precisa de
    localizador.
    """
    locator: dict[str, Any] = {
        "version": version,
        "section": section.strip() or "(raiz)",
        "block": block,
    }
    if paragraph is not None:
        locator["paragraph"] = paragraph
    if heading_path:
        locator["heading_path"] = [str(h) for h in heading_path]
    if page is not None:
        locator["page"] = page
    return validate_locator(SourceKind.DOCUMENT, locator)


def transcript_locator(
    *,
    file: str,
    version: str,
    block: str,
    time_start: str | None = None,
    time_end: str | None = None,
    speaker: str | None = None,
) -> dict[str, Any]:
    """Localizador `SourceKind.TRANSCRIPT`: arquivo/versão, bloco, intervalo e interlocutor (§5.4).

    O intervalo é all-or-nothing — `validate_locator` rejeita `time_start` sem
    `time_end`. Fala sem timestamp identificável fica sem intervalo, e não com
    um intervalo inventado.
    """
    locator: dict[str, Any] = {
        "file": file or "(desconhecido)",
        "version": version,
        "block": block,
    }
    if time_start is not None and time_end is not None:
        locator["time_start"] = time_start
        locator["time_end"] = time_end
    if speaker:
        locator["speaker"] = speaker
    return validate_locator(SourceKind.TRANSCRIPT, locator)


# --------------------------------------------------------------------------
# Preservação da fonte antes da extração (§8.1)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Preserved:
    """Registro feito ANTES da extração. `raw` guarda os bytes lidos."""

    path_original: str
    bytes_sha256: str
    size: int
    mime_guess: str
    raw: bytes = b""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_original": self.path_original,
            "bytes_sha256": self.bytes_sha256,
            "size": self.size,
            "mime_guess": self.mime_guess,
        }


#: Assinaturas de bytes que valem mais que a extensão do arquivo.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"PK\x03\x04", "application/zip"),
    (b"{\\rtf", "application/rtf"),
    (b"\xd0\xcf\x11\xe0", "application/x-ole-storage"),
)

_EXT_MIME: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".xml": "application/xml",
    ".xmi": "application/xml",
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
}


def guess_mime(path: str, head: bytes) -> str:
    """Tipo provável a partir dos bytes e, subsidiariamente, da extensão."""
    for signature, mime in _MAGIC:
        if head.startswith(signature):
            if mime == "application/zip" and path.lower().endswith(".docx"):
                return _EXT_MIME[".docx"]
            return mime
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def preserve(path: str, *, keep_bytes: bool = True) -> Preserved:
    """Hash/registro da fonte ANTES da extração (§8.1).

    Abre em modo binário de leitura e nada mais: o conteúdo original nunca é
    reescrito por este módulo nem pelos adapters, que recebem `Preserved.raw`
    em vez de reabrir o arquivo para escrita.
    """
    if os.path.isdir(path):
        return Preserved(
            path_original=os.path.abspath(path),
            bytes_sha256=_directory_digest(path),
            size=0,
            mime_guess="inode/directory",
        )
    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    with open(path, "rb") as handle:  # somente leitura: fonte nunca é alterada
        while True:
            chunk = handle.read(1024 * 256)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if keep_bytes:
                chunks.append(chunk)
    raw = b"".join(chunks) if keep_bytes else b""
    return Preserved(
        path_original=os.path.abspath(path),
        bytes_sha256=digest.hexdigest(),
        size=size,
        mime_guess=guess_mime(path, raw[:512] if raw else b""),
        raw=raw,
    )


def _directory_digest(path: str) -> str:
    """Hash do INVENTÁRIO do diretório (nomes+tamanhos), não do conteúdo."""
    entries: list[str] = []
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, path).replace("\\", "/")
            try:
                entries.append(f"{rel}:{os.path.getsize(full)}")
            except OSError:
                entries.append(f"{rel}:?")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def reverify(preserved: Preserved) -> Diagnostic | None:
    """Re-hasheia a fonte depois da extração. Diagnóstico se os bytes mudaram.

    §8.1 exige que o conteúdo original nunca seja alterado; esta função é a
    verificação executável dessa exigência, não uma afirmação em prosa.
    """
    path = preserved.path_original
    if preserved.mime_guess == "inode/directory":
        return None
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 256), b""):
                digest.update(chunk)
        current = digest.hexdigest()
    except OSError as exc:
        return Diagnostic(
            code="source.reverify_failed",
            severity=Severity.WARNING,
            message=f"não foi possível reverificar os bytes da fonte após a extração: {exc}",
            path=path,
        )
    if current != preserved.bytes_sha256:
        return Diagnostic(
            code="source.mutated_during_extraction",
            severity=Severity.ERROR,
            message=(
                "os bytes da fonte mudaram entre a preservação e o fim da extração "
                f"({preserved.bytes_sha256[:12]} -> {current[:12]})"
            ),
            unavailable=("integridade da fonte no snapshot",),
            path=path,
        )
    return None


# --------------------------------------------------------------------------
# Documento normalizado
# --------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_source_id(bytes_sha256: str, path: str) -> str:
    """Id da fonte: conteúdo primeiro, caminho como desempate de origem."""
    blob = f"{bytes_sha256}\x00{os.path.basename(path)}".encode("utf-8")
    return "src_" + hashlib.sha256(blob).hexdigest()[:32]


@dataclass(frozen=True)
class SourceDocument:
    """Resultado da ingestão de UMA fonte, com origem preservada (§8.1)."""

    source_id: str
    path_original: str
    kind: str
    bytes_sha256: str
    size: int
    ingested_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    blocks: tuple[Block, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    status: SourceStatus = SourceStatus.INGESTED
    #: Chaves fora da whitelist, preservadas sem qualquer efeito (§8.1).
    metadata_extra: Mapping[str, Any] = field(default_factory=dict)
    mime_guess: str = ""
    #: Versão da fonte, no formato usado pelos localizadores.
    version: str = ""
    #: Resultados por fonte, quando esta é um diretório (§7.1).
    children: tuple["SourceDocument", ...] = ()
    #: Qual adapter produziu o resultado.
    adapter: str = ""

    @property
    def complete(self) -> bool:
        """Só é `True` quando nada ficou indisponível."""
        return self.status is SourceStatus.INGESTED

    @property
    def unavailable(self) -> list[str]:
        """Tudo o que ficou indisponível, agregado dos diagnósticos."""
        out: list[str] = []
        for diag in self.diagnostics:
            out.extend(diag.unavailable)
        return out

    def to_dict(self, *, include_children: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source_id": self.source_id,
            "path_original": self.path_original,
            "kind": self.kind,
            "bytes_sha256": self.bytes_sha256,
            "size": self.size,
            "ingested_at": self.ingested_at,
            "metadata": dict(self.metadata),
            "metadata_extra": dict(self.metadata_extra),
            "mime_guess": self.mime_guess,
            "version": self.version,
            "adapter": self.adapter,
            "status": self.status.value,
            "blocks": [b.to_dict() for b in self.blocks],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "unavailable": self.unavailable,
        }
        if include_children:
            data["children"] = [c.to_dict() for c in self.children]
        return data


def decide_status(
    blocks: Sequence[Block],
    diagnostics: Sequence[Diagnostic],
    *,
    proposed: SourceStatus | None = None,
    aggregate: bool = False,
) -> SourceStatus:
    """Status honesto: nunca `ingested` quando algo ficou indisponível (§8.1).

    - `proposed` diferente de `ingested` é o veredito do adapter e prevalece:
      só o adapter sabe distinguir "formato sem suporte" de "conteúdo
      ilegível". `ingested` proposto ainda passa pela verificação abaixo, para
      que nenhum adapter consiga declarar sucesso integral por conta própria.
    - Qualquer diagnóstico com `unavailable` não vazio rebaixa para `partial`.
    - Diagnóstico `ERROR` sem nenhum bloco recuperado é `extraction_failed`.
    - `aggregate=True` (diretório) não exige blocos próprios: o conteúdo está
      nos filhos, e a ausência de blocos no agregado não é falha.
    """
    if proposed is not None and proposed is not SourceStatus.INGESTED:
        return proposed
    has_error = any(d.severity is Severity.ERROR for d in diagnostics)
    lost = any(d.unavailable for d in diagnostics)
    if has_error and not blocks and not aggregate:
        return SourceStatus.EXTRACTION_FAILED
    if has_error or lost:
        return SourceStatus.PARTIAL
    if not blocks and not aggregate:
        return SourceStatus.EXTRACTION_FAILED
    return SourceStatus.INGESTED


def _context_for(block: Block, metadata: Mapping[str, Any], version: str) -> dict[str, Any]:
    """Contexto {iniciativa, fase, participante, momento, versão} do bloco (§8.1)."""
    locator = dict(block.locator)
    participants = metadata.get("participants")
    if isinstance(participants, list):
        default_participant = participants[0] if len(participants) == 1 else ""
    else:
        default_participant = str(participants or "")
    speaker = locator.get("speaker") or default_participant
    moment = locator.get("time_start") or metadata.get("date") or ""
    return {
        "iniciativa": metadata.get("initiative_id", ""),
        "fase": metadata.get("phase", ""),
        "participante": speaker or "",
        "momento": moment,
        "versao": version,
    }


def build_document(
    preserved: Preserved,
    *,
    kind: str,
    adapter: str,
    blocks: Iterable[Block] = (),
    raw_metadata: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    metadata_extra: Mapping[str, Any] | None = None,
    diagnostics: Iterable[Diagnostic] = (),
    proposed_status: SourceStatus | None = None,
    children: Iterable["SourceDocument"] = (),
    ingested_at: str | None = None,
) -> SourceDocument:
    """Monta o `SourceDocument` final: whitelist, contexto e status honesto.

    Passe `raw_metadata` para deixar a whitelist ser aplicada aqui, ou
    `metadata`/`metadata_extra` já sanitizados pelo adapter.
    """
    diags = list(diagnostics)
    source_meta = dict(raw_metadata) if raw_metadata is not None else dict(metadata or {})
    # A whitelist é aplicada AQUI, uma vez, para toda fonte: nenhum adapter
    # consegue injetar chave fora de METADATA_WHITELIST em `metadata` (§8.1).
    clean, extra, meta_diags = sanitize_metadata(source_meta)
    diags.extend(meta_diags)
    for key, value in dict(metadata_extra or {}).items():
        extra.setdefault(str(key)[:200], _inert(value))
    assert_inert(clean)
    assert_inert(extra)
    version = version_label(preserved.bytes_sha256)
    with_context = tuple(
        replace(b, context=_context_for(b, clean, version)) for b in blocks
    )
    child_tuple = tuple(children)
    status = decide_status(
        with_context, diags, proposed=proposed_status, aggregate=bool(child_tuple)
    )
    return SourceDocument(
        source_id=make_source_id(preserved.bytes_sha256, preserved.path_original),
        path_original=preserved.path_original,
        kind=kind,
        bytes_sha256=preserved.bytes_sha256,
        size=preserved.size,
        ingested_at=ingested_at or utc_now(),
        metadata=clean,
        blocks=with_context,
        diagnostics=tuple(diags),
        status=status,
        metadata_extra=extra,
        mime_guess=preserved.mime_guess,
        version=version,
        children=child_tuple,
        adapter=adapter,
    )


def unsupported_document(
    preserved: Preserved,
    *,
    kind: str,
    reason: str,
    unavailable: Sequence[str],
    adapter: str = "",
    extra_diagnostics: Iterable[Diagnostic] = (),
) -> SourceDocument:
    """Fonte sem extração possível: `unsupported` + o que ficou indisponível."""
    diags = [
        Diagnostic(
            code="format.unsupported",
            severity=Severity.ERROR,
            message=reason,
            unavailable=tuple(unavailable),
            path=preserved.path_original,
        ),
        *extra_diagnostics,
    ]
    return build_document(
        preserved,
        kind=kind,
        adapter=adapter,
        diagnostics=diags,
        proposed_status=SourceStatus.UNSUPPORTED,
    )


def failed_document(
    preserved: Preserved,
    *,
    kind: str,
    reason: str,
    unavailable: Sequence[str],
    adapter: str = "",
    blocks: Iterable[Block] = (),
    extra_diagnostics: Iterable[Diagnostic] = (),
) -> SourceDocument:
    """Adapter existia mas não conseguiu ler tudo: `extraction_failed`."""
    diags = [
        Diagnostic(
            code="extraction.failed",
            severity=Severity.ERROR,
            message=reason,
            unavailable=tuple(unavailable),
            path=preserved.path_original,
        ),
        *extra_diagnostics,
    ]
    return build_document(
        preserved,
        kind=kind,
        adapter=adapter,
        blocks=blocks,
        diagnostics=diags,
        proposed_status=SourceStatus.EXTRACTION_FAILED,
    )


def decode_text(raw: bytes) -> tuple[str, list[Diagnostic]]:
    """Decodifica bytes como texto, registrando perda quando houver.

    Nunca levanta: bytes indecodificáveis viram diagnóstico com o que ficou
    indisponível, para não transformar um arquivo corrompido em silêncio.
    """
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding), []
        except UnicodeDecodeError:
            continue
    text = raw.decode("utf-8", errors="replace")
    lost = text.count("�")
    return text, [
        Diagnostic(
            code="text.decode_lossy",
            severity=Severity.WARNING,
            message=f"conteúdo não é UTF-8 válido; {lost} byte(s) substituídos na decodificação",
            unavailable=(f"{lost} byte(s) não decodificáveis",),
        )
    ]


def dataclass_asdict(obj: Any) -> Any:
    """`dataclasses.asdict` tolerante a enums, para depuração ad-hoc."""
    return dataclasses.asdict(obj)
