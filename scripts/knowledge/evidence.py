"""Localizadores de evidência por tipo de fonte, hash e mascaramento (§5.4).

Duas responsabilidades, ambas executáveis:

1. **Validação estrutural**: cada `SourceKind` tem campos obrigatórios
   próprios (`REQUIRED_FIELDS`). Um localizador de código sem `commit` ou sem
   intervalo não é evidência — é uma referência que não volta a ser
   verificável no futuro, e `validate_locator` o rejeita.
2. **`content_kind`**: §5.4 diz que comentário, docstring, README e plano não
   sustentam comportamento implementado. Sem um campo dedicado, um localizador
   de código apontando para um comentário passaria em toda validação
   estrutural. `supports_implemented` é a função que separa os dois casos, e o
   repositório a consulta antes de aceitar `nature=implemented` + `supported`.

Valor sensível de configuração é mascarado SEM perder a condição relevante
(`mask_sensitive`): o que importa para a regra é "existe e está definido como
não-vazio", não o segredo.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

from .models import (
    ContentKind,
    Evidence,
    LocatorInvalid,
    NON_IMPLEMENTING_CONTENT,
    SourceKind,
    UnsupportedContentKind,
)

#: Campos obrigatórios do localizador, por tipo de fonte (§5.4).
REQUIRED_FIELDS: dict[SourceKind, tuple[str, ...]] = {
    SourceKind.CODE: ("repo", "commit", "path", "start_line", "end_line", "snippet_hash"),
    SourceKind.CONFIG: ("file", "key", "version"),
    SourceKind.TEST: ("case", "assertions", "version"),
    SourceKind.DOCUMENT: ("version", "section", "block"),
    SourceKind.TRANSCRIPT: ("file", "version", "block"),
    SourceKind.OBSERVATION: ("origin", "instant", "scope", "execution_id"),
}

#: Campos opcionais reconhecidos. Campo fora de obrigatório+opcional é
#: rejeitado: §13.1 exige schemas fechados para contratos de máquina.
OPTIONAL_FIELDS: dict[SourceKind, tuple[str, ...]] = {
    SourceKind.CODE: ("symbol", "language"),
    SourceKind.CONFIG: ("condition", "value_masked", "masked", "environment"),
    SourceKind.TEST: ("execution_condition", "executed", "mocks", "runner"),
    SourceKind.DOCUMENT: ("page", "paragraph", "heading_path"),
    SourceKind.TRANSCRIPT: ("time_start", "time_end", "speaker"),
    SourceKind.OBSERVATION: ("correlation_id", "metric", "sample_size"),
}

#: `content_kind` aceitável para cada tipo de fonte.
ALLOWED_CONTENT: dict[SourceKind, frozenset[ContentKind]] = {
    SourceKind.CODE: frozenset(
        {
            ContentKind.EXECUTABLE,
            ContentKind.COMMENT,
            ContentKind.DOCSTRING,
            ContentKind.MARKDOWN,
            ContentKind.PROSE,
        }
    ),
    SourceKind.CONFIG: frozenset({ContentKind.CONFIG_VALUE, ContentKind.COMMENT}),
    SourceKind.TEST: frozenset(
        {ContentKind.TEST_ASSERTION, ContentKind.EXECUTABLE, ContentKind.COMMENT, ContentKind.DOCSTRING}
    ),
    SourceKind.DOCUMENT: frozenset({ContentKind.PROSE, ContentKind.MARKDOWN}),
    SourceKind.TRANSCRIPT: frozenset({ContentKind.TRANSCRIPT_BLOCK}),
    SourceKind.OBSERVATION: frozenset({ContentKind.OBSERVATION_RECORD}),
}

MASK = "***"


def snippet_hash(text: str) -> str:
    """Hash do trecho citado — permite detectar que a fonte mudou sob a citação."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mask_sensitive(value: str, keep: int = 0) -> str:
    """Mascara valor sensível preservando a CONDIÇÃO relevante (§5.4).

    Devolve `"***"` (ou `"ab***"` com `keep`), nunca string vazia: distinguir
    "chave definida com valor secreto" de "chave ausente" é o que a regra de
    negócio costuma depender.
    """
    if value is None:
        raise LocatorInvalid("mask_sensitive recebeu None: ausência não é mascaramento")
    if keep > 0:
        return value[:keep] + MASK
    return MASK


def validate_locator(source_kind: SourceKind, locator: Mapping[str, Any]) -> dict[str, Any]:
    """Valida e normaliza o localizador; levanta `LocatorInvalid` com o motivo."""
    if not isinstance(locator, Mapping):
        raise LocatorInvalid(f"localizador deve ser um mapa, recebido {type(locator).__name__}")
    required = REQUIRED_FIELDS[source_kind]
    optional = OPTIONAL_FIELDS[source_kind]
    missing = [f for f in required if locator.get(f) in (None, "", [])]
    if missing:
        raise LocatorInvalid(
            f"localizador de {source_kind.value} sem campos obrigatórios: "
            f"{', '.join(missing)} (exigidos: {', '.join(required)})"
        )
    unknown = [k for k in locator if k not in required and k not in optional]
    if unknown:
        raise LocatorInvalid(
            f"campos não previstos no localizador de {source_kind.value}: {', '.join(sorted(unknown))}"
        )
    out = dict(locator)

    if source_kind is SourceKind.CODE:
        start, end = out["start_line"], out["end_line"]
        if not isinstance(start, int) or not isinstance(end, int):
            raise LocatorInvalid("intervalo de código exige start_line/end_line inteiros")
        if start < 1 or end < start:
            raise LocatorInvalid(f"intervalo de código inválido: {start}..{end}")
    elif source_kind is SourceKind.TEST:
        assertions = out["assertions"]
        if not isinstance(assertions, (list, tuple)) or not assertions:
            raise LocatorInvalid("localizador de teste exige lista de assertions não vazia")
        out["assertions"] = list(assertions)
        if out.get("executed") and not out.get("execution_condition"):
            raise LocatorInvalid(
                "teste marcado como executado exige execution_condition (§5.4)"
            )
    elif source_kind is SourceKind.CONFIG:
        # Valor sensível: aceitar apenas a forma mascarada.
        if "value_masked" in out and out.get("masked") is False:
            raise LocatorInvalid("value_masked presente com masked=False: contradição")
    elif source_kind is SourceKind.DOCUMENT:
        # §5.4: página só é localizador aceitável se estável no snapshot.
        if "page" in out and not out.get("block"):
            raise LocatorInvalid("página sem bloco/parágrafo não é localizador estável")
    elif source_kind is SourceKind.TRANSCRIPT:
        if ("time_start" in out) != ("time_end" in out):
            raise LocatorInvalid("intervalo de tempo da transcrição exige time_start e time_end")
    return out


def evidence_id(
    namespace: str,
    source_kind: SourceKind,
    content_kind: ContentKind,
    source_version_id: str,
    locator: Mapping[str, Any],
) -> str:
    """Id determinístico da evidência: mesma citação nunca vira duas linhas.

    É o que impede recontagem de evidência derivada em reingestão (A09).
    """
    import json

    blob = json.dumps(
        {
            "ns": namespace,
            "sk": source_kind.value,
            "ck": content_kind.value,
            "sv": source_version_id,
            "loc": dict(locator),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return "evd_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def make_evidence(
    namespace: str,
    source_kind: SourceKind,
    content_kind: ContentKind,
    source_version_id: str,
    locator: Mapping[str, Any],
    recorded_at: str = "",
) -> Evidence:
    """Constrói evidência validada, com id determinístico."""
    if content_kind not in ALLOWED_CONTENT[source_kind]:
        raise LocatorInvalid(
            f"content_kind={content_kind.value} incompatível com fonte {source_kind.value}; "
            f"aceitos: {', '.join(sorted(c.value for c in ALLOWED_CONTENT[source_kind]))}"
        )
    if not (source_version_id or "").strip():
        raise LocatorInvalid("evidência exige source_version_id: citação sem versão não é verificável")
    normalized = validate_locator(source_kind, locator)
    return Evidence(
        evidence_id=evidence_id(namespace, source_kind, content_kind, source_version_id, normalized),
        namespace=namespace,
        source_kind=source_kind,
        content_kind=content_kind,
        source_version_id=source_version_id,
        locator=normalized,
        snippet_hash=normalized.get("snippet_hash"),
        recorded_at=recorded_at,
    )


def supports_implemented(ev: Evidence) -> bool:
    """A evidência pode sustentar `FactNature.IMPLEMENTED`?

    `False` para comentário, docstring, markdown, prosa e bloco de transcrição
    (§5.4) — mesmo quando o localizador aponta para dentro do repositório.
    Teste sustenta `test_expectation`, não comportamento real do serviço
    externo, então `TEST_ASSERTION` também não sustenta `implemented`.
    """
    if ev.content_kind in NON_IMPLEMENTING_CONTENT:
        return False
    return ev.content_kind in (ContentKind.EXECUTABLE, ContentKind.CONFIG_VALUE)


def assert_supports_implemented(evidences: Iterable[Evidence]) -> None:
    """Exige ao menos uma evidência que sustente `implemented`, ou rejeita."""
    items = list(evidences)
    if any(supports_implemented(e) for e in items):
        return
    kinds = sorted({e.content_kind.value for e in items}) or ["(nenhuma)"]
    raise UnsupportedContentKind(
        "natureza 'implemented' com sustentação 'supported' exige evidência executável ou "
        f"de valor de configuração; recebido apenas: {', '.join(kinds)} (§5.4)"
    )
