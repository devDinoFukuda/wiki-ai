"""Namespaces, aliases e resolução de identidade (plano §5.2).

Três regras que existem em código, não em documentação:

1. `entity_id` é derivado de ``(salt do namespace, tipo, stable_key)`` — nunca
   só do título. Trocar o título não troca a identidade; e homônimos em
   namespaces distintos produzem ids distintos, então nunca colidem nem são
   mesclados (`resolve_identity` também jamais consulta fora do namespace).
2. Renomeação preserva identidade SOMENTE com confirmação por histórico/
   metadado/humano. Similaridade de conteúdo, por maior que seja, é rejeitada
   por `confirm_rename` (A06: similaridade isolada não confirma vínculo).
3. Ids determinísticos são o que torna reingestão idêntica idempotente: o
   mesmo insumo recalcula o mesmo id e cai no upsert, sem duplicar identidade.
4. Id explícito do corpus (`RN-023`, `CAP-007`) citado no nome/texto de uma
   entidade vira ALIAS canônico (`explicit_id_aliases`) e é resolvível pela
   chave LITERAL (`find_by_alias`). Sem isso, quem cria a regra deriva
   `stable_key = businessrule:<capacidade>:rn-023` e quem a cita procura por
   "RN-023": os dois lados nunca se encontram e a mesma regra passa a existir
   duas vezes (achado nº4 da 2ª auditoria externa).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    Alias,
    AliasOrigin,
    EntityType,
    IdentityConflict,
    RENAME_CONFIRMING_ORIGINS,
    RenameNotConfirmed,
)

_ID_LEN = 32


# --------------------------------------------------------------------------
# Ids explícitos do corpus (RN-023, CAP-007, ...) como ALIAS canônico
# --------------------------------------------------------------------------

#: Prefixos de id explícito reconhecidos no corpus. Reimplementação LOCAL e
#: mínima do vocabulário de `ingestion.extract`: `knowledge` não importa
#: `ingestion` (a dependência é a inversa), e duplicar a CONSTANTE é o preço de
#: manter a camada de identidade sem depender da camada de ingestão.
EXPLICIT_ID_PREFIXES: tuple[str, ...] = (
    "INI", "DEC", "ADR", "RF", "REF", "RN", "BR", "CAP", "US", "HU", "STY",
    "RQ", "REQ", "RNF", "DEF", "BUG", "SYS", "CMP", "CTR", "ENT", "FLW",
)

#: Duas alternativas, de propósito: prefixo CONHECIDO aceita separador ausente
#: ou solto (`RN-023`, `RN 23`, `rn.23`); prefixo desconhecido exige hífen E
#: maiúsculas, para que uma palavra comum não vire id.
EXPLICIT_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?P<known>"
    + "|".join(sorted(EXPLICIT_ID_PREFIXES, key=len, reverse=True))
    + r")[\s._-]?(?P<knum>\d{1,6})"
    r"|(?P<other>[A-Z]{2,6})-(?P<onum>\d{1,6}))(?![A-Za-z0-9])",
    re.IGNORECASE,
)

#: Origem do alias criado a partir de um id explícito do texto. `metadata_id` é
#: a origem do §5.2 para "id declarado na própria fonte" — e é uma origem que
#: CONFIRMA identidade em renomeação, que é exatamente o estatuto de `RN-023`:
#: o texto trouxe o id, ninguém o inferiu por semelhança.
EXPLICIT_ID_ALIAS_ORIGIN: AliasOrigin = AliasOrigin.METADATA_ID


def parse_explicit_ids(text: str) -> tuple[str, ...]:
    """Ids explícitos citados em `text`, na forma canônica ``PRE-NNN``.

    Determinístico e sem fuzzy: só casa o padrão de id, normaliza o número para
    3 dígitos (``RN 23`` e ``RN-023`` são o MESMO id) e preserva a ordem sem
    repetir. Prefixo desconhecido em minúsculas é palavra comum, não id.
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in EXPLICIT_ID_RE.finditer(text or ""):
        if m.group("known"):
            prefix_raw, number = m.group("known"), m.group("knum")
        else:
            prefix_raw, number = m.group("other"), m.group("onum")
        prefix = prefix_raw.upper()
        if m.group("other") and prefix != prefix_raw:
            continue
        canonical = f"{prefix}-{number.zfill(3) if len(number) < 3 else number}"
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return tuple(out)


def explicit_id_aliases(*texts: str) -> tuple[Alias, ...]:
    """Aliases canônicos para os ids explícitos citados nos textos dados.

    É o que faz `RN-023` no NOME de uma regra virar chave de resolução: sem o
    alias, quem cita a regra pela chave literal (`ingestion`) não encontra a
    entidade, porque a `stable_key` derivada carrega capacidade e slug.
    """
    out: list[Alias] = []
    seen: set[str] = set()
    for text in texts:
        for value in parse_explicit_ids(text or ""):
            if value in seen:
                continue
            seen.add(value)
            out.append(Alias(alias=value, origin=EXPLICIT_ID_ALIAS_ORIGIN))
    return tuple(out)


def _connection_of(target: Any) -> sqlite3.Connection:
    """Aceita `Repository` (que expõe `.conn`) ou a conexão crua."""
    conn = getattr(target, "conn", target)
    if not hasattr(conn, "execute"):
        raise IdentityConflict(
            f"esperado sqlite3.Connection ou Repository, recebido {type(target).__name__}"
        )
    return conn


def find_by_alias(
    target: Any,
    namespace: str,
    alias: str,
    entity_type: EntityType | None = None,
) -> str | None:
    """`entity_id` cujo ALIAS EXATO é `alias`, dentro do namespace.

    Lookup determinístico: casamento exato de string na coluna `alias`, nunca
    aproximação — `parse_explicit_ids` é quem normaliza a forma citada antes,
    se o chamador quiser (`RN 23` -> `RN-023`).

    Ambiguidade é REPORTADA, não resolvida: dois `entity_id` distintos com o
    mesmo alias no mesmo namespace levantam `IdentityConflict`, porque escolher
    um deles seria fundir identidades por chute (§5.2).
    """
    key = (alias or "").strip()
    if not key:
        return None
    ns = normalize_namespace(namespace)
    conn = _connection_of(target)
    sql = (
        "SELECT DISTINCT a.entity_id FROM entity_aliases a "
        "JOIN entities e ON e.entity_id = a.entity_id "
        "WHERE a.namespace=? AND a.alias=?"
    )
    params: list[Any] = [ns, key]
    if entity_type is not None:
        sql += " AND e.entity_type=?"
        params.append(entity_type.value)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return None
    ids = sorted({row[0] for row in rows})
    if len(ids) > 1:
        raise IdentityConflict(
            f"alias {key!r} em {ns!r} aponta para {len(ids)} entidades ({', '.join(ids)}): "
            "ambiguidade de identidade não é resolvida por escolha arbitrária (§5.2)"
        )
    return ids[0]


def normalize_namespace(namespace: str) -> str:
    """Normaliza o namespace (organização/projeto/sistema).

    Só corta espaços e unifica separador; NÃO faz case-folding agressivo nem
    remove segmentos, porque namespace é chave de isolamento: qualquer
    normalização que aproxime dois namespaces distintos vira merge indevido.
    """
    ns = (namespace or "").strip().strip("/")
    if not ns:
        raise IdentityConflict("namespace vazio: identidade exige namespace explícito")
    return "/".join(seg.strip() for seg in ns.split("/") if seg.strip())


def namespace_salt(namespace: str) -> str:
    """Sal estável do namespace, prefixo de todo id derivado."""
    return hashlib.sha256(normalize_namespace(namespace).encode("utf-8")).hexdigest()[:16]


def _digest(*parts: str) -> str:
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_ID_LEN]


def entity_id(namespace: str, entity_type: EntityType, stable_key: str) -> str:
    """`entity_id` determinístico e imutável (§5.2).

    `stable_key` é a chave natural (caminho, símbolo, URN, id externo) e é
    obrigatória: derivar identidade só do título é justamente o que o plano
    proíbe, porque renomear o título passaria a criar entidade nova.
    """
    key = (stable_key or "").strip()
    if not key:
        raise IdentityConflict(
            "stable_key vazia: entity_id não pode ser derivado exclusivamente do título (§5.2)"
        )
    return "ent_" + _digest(namespace_salt(namespace), entity_type.value, key)


def mint_entity_id(namespace: str, entity_type: EntityType) -> str:
    """Identidade sem chave natural conhecida: uuid4 com sal de namespace.

    Usar quando a fonte não oferece chave estável. Uma vez cunhada, a
    identidade é registrada com um `stable_key` sintético (o próprio uuid),
    para que reingestões seguintes a reencontrem por alias.
    """
    return "ent_" + _digest(namespace_salt(namespace), entity_type.value, uuid.uuid4().hex)


def fact_id(namespace: str, subject_id: str, predicate: str, scope: str) -> str:
    """Identidade de fato: sujeito + predicado + escopo.

    Valor NÃO entra: mudar o valor é nova REVISÃO do mesmo fato, não um fato
    novo — é o que permite substituir sem perder a linha do tempo.
    """
    return "fct_" + _digest(namespace_salt(namespace), subject_id, predicate.strip(), scope.strip())


def relation_id(
    namespace: str, source_entity_id: str, relation_type: str, target_entity_id: str, scope: str
) -> str:
    """Identidade de relação: (origem, tipo, destino, escopo)."""
    return "rel_" + _digest(
        namespace_salt(namespace),
        source_entity_id,
        relation_type,
        target_entity_id,
        scope.strip(),
    )


def new_revision_id() -> str:
    """Revisões são sempre novas; nunca determinísticas."""
    return "rev_" + uuid.uuid4().hex


def new_effect_id() -> str:
    return "eff_" + uuid.uuid4().hex


def source_version_id(source_id: str, version_label: str, content_hash: str) -> str:
    """Versão de fonte identificada pelo par (rótulo de versão, hash)."""
    return "srv_" + _digest(source_id, version_label, content_hash)


def source_id(namespace: str, uri: str) -> str:
    return "src_" + _digest(namespace_salt(namespace), uri.strip())


def content_hash(payload: Any) -> str:
    """Hash canônico de conteúdo (§5.2).

    `json.dumps` com `sort_keys` garante que a mesma informação em ordem
    diferente produza o mesmo hash — sem isso, reingestão idêntica pareceria
    mudança e criaria revisão nova a cada execução.
    """
    if isinstance(payload, str):
        blob = payload
    else:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Renomeação
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RenameEvidence:
    """Justificativa de que dois nomes são a MESMA entidade.

    `similarity` é informativo e nunca decide: `confirm_rename` só aceita
    `origin` em `RENAME_CONFIRMING_ORIGINS`.
    """

    origin: AliasOrigin
    detail: str
    similarity: float | None = None
    source_version_id: str | None = None


def confirm_rename(old_name: str, new_name: str, evidence: RenameEvidence) -> Alias:
    """Aceita a renomeação e devolve o alias do nome ANTIGO, ou rejeita.

    Rejeita quando a única base é semelhança de conteúdo/nome (§5.2:
    "conteúdo similar isolado não basta"), inclusive com similaridade 1.0.
    """
    if evidence.origin not in RENAME_CONFIRMING_ORIGINS:
        raise RenameNotConfirmed(
            f"renomeação {old_name!r} -> {new_name!r} apoiada só em "
            f"origin={evidence.origin.value} (similarity={evidence.similarity}); "
            "identidade só é preservada com histórico de VCS, metadado de id ou "
            "confirmação humana (§5.2)"
        )
    if not (evidence.detail or "").strip():
        raise RenameNotConfirmed(
            "confirmação de renomeação sem detalhe verificável (commit, id ou autor)"
        )
    return Alias(
        alias=old_name,
        origin=evidence.origin,
        source_version_id=evidence.source_version_id,
    )


# --------------------------------------------------------------------------
# Resolução no banco
# --------------------------------------------------------------------------


def resolve_identity(
    conn: sqlite3.Connection,
    namespace: str,
    entity_type: EntityType,
    stable_key: str | None = None,
    aliases: Sequence[str] = (),
) -> str | None:
    """Procura uma identidade existente DENTRO do namespace e do tipo.

    Nunca busca em outro namespace nem em outro tipo — é o que garante que
    homônimos em namespaces distintos jamais sejam mesclados.
    """
    ns = normalize_namespace(namespace)
    if stable_key:
        row = conn.execute(
            "SELECT entity_id FROM entities "
            "WHERE namespace=? AND entity_type=? AND stable_key=?",
            (ns, entity_type.value, stable_key),
        ).fetchone()
        if row:
            return row[0]
    for alias in aliases:
        row = conn.execute(
            "SELECT a.entity_id FROM entity_aliases a "
            "JOIN entities e ON e.entity_id = a.entity_id "
            "WHERE a.namespace=? AND a.alias=? AND e.entity_type=?",
            (ns, alias, entity_type.value),
        ).fetchone()
        if row:
            return row[0]
    return None


def resolve_by_name(
    conn: sqlite3.Connection,
    namespace: str,
    name: str,
    entity_type: EntityType | None = None,
) -> list[str]:
    """Todos os `entity_id` do namespace cujo título ou alias casa com `name`.

    Devolve LISTA: ambiguidade é reportada, não resolvida por chute. Duas
    entidades com o mesmo nome no mesmo namespace continuam duas.
    """
    ns = normalize_namespace(namespace)
    params: list[Any] = [ns, name, ns, name]
    sql = (
        "SELECT DISTINCT e.entity_id FROM entities e "
        "JOIN entity_revisions r ON r.entity_id=e.entity_id AND r.revision_id=e.head_revision_id "
        "WHERE (e.namespace=? AND r.title=?) "
        "   OR e.entity_id IN (SELECT entity_id FROM entity_aliases WHERE namespace=? AND alias=?)"
    )
    if entity_type is not None:
        sql += " AND e.entity_type=?"
        params.append(entity_type.value)
    return [row[0] for row in conn.execute(sql, params).fetchall()]


def check_identity_consistency(
    conn: sqlite3.Connection, eid: str, namespace: str, entity_type: EntityType
) -> None:
    """Rejeita reutilizar um `entity_id` com namespace/tipo diferentes."""
    row = conn.execute(
        "SELECT namespace, entity_type FROM entities WHERE entity_id=?", (eid,)
    ).fetchone()
    if row is None:
        return
    ns = normalize_namespace(namespace)
    if row[0] != ns or row[1] != entity_type.value:
        raise IdentityConflict(
            f"entity_id {eid} já registrado como ({row[0]}, {row[1]}); "
            f"recusando reivindicação como ({ns}, {entity_type.value})"
        )


def alias_rows(
    entity: str, namespace: str, aliases: Iterable[Alias], revision: str, now: str
) -> list[tuple[Any, ...]]:
    """Linhas de `entity_aliases` para gravação, já com origem obrigatória."""
    ns = normalize_namespace(namespace)
    out: list[tuple[Any, ...]] = []
    for a in aliases:
        if not isinstance(a, Alias):
            raise IdentityConflict(
                f"alias {a!r} sem origem: §5.2 exige alias com origem declarada"
            )
        out.append((entity, a.alias, a.origin.value, ns, a.source_version_id, revision, now))
    return out


def entity_content_hash(
    title: str,
    stable_key: str,
    attributes: Mapping[str, Any],
    aliases: Sequence[Alias],
    source_version_id_value: str | None,
    lifecycle_status: str,
    valid_from: str | None,
    valid_to: str | None,
) -> str:
    """Hash do conteúdo pertinente da entidade — base do upsert idempotente."""
    return content_hash(
        {
            "title": title,
            "stable_key": stable_key,
            "attributes": dict(attributes or {}),
            "aliases": sorted((a.alias, a.origin.value) for a in aliases),
            "source_version_id": source_version_id_value,
            "lifecycle_status": lifecycle_status,
            "valid_from": valid_from,
            "valid_to": valid_to,
        }
    )
