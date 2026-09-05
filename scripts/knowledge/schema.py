"""DDL de `knowledge.db` (plano §4.2) e controle explícito de versão de schema.

Forma geral: cada coisa versionada tem uma tabela de IDENTIDADE (imutável,
uma linha por `*_id`) e uma tabela de REVISÕES (uma linha por versão). A
identidade guarda `head_revision_id`; o histórico nunca é apagado — é isso que
sustenta `historical`/`superseded`/`stale` sem `DELETE`.

A outbox (`effects`) vive AQUI e não em `runtime.db`, exatamente para nascer na
mesma transação da revisão que a originou (§4.2). `runtime.db` referencia
`effect_id` e confirma o destino de forma idempotente; não há transação
distribuída entre os dois bancos.

Migração é explícita e é escopo de W8: aqui só existe detecção de divergência
(`SchemaVersionMismatch`), nunca migração automática.
"""

from __future__ import annotations

import sqlite3

from .models import SchemaVersionMismatch

#: Versão do schema criada por este módulo. Só sobe junto com uma migração
#: explícita em `migrate.py` (W8) — nunca por edição silenciosa do DDL.
SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL PRIMARY KEY,
    applied_at  TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------- revisões
CREATE TABLE IF NOT EXISTS revisions (
    revision_id         TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    author              TEXT NOT NULL,
    reason              TEXT NOT NULL DEFAULT '',
    parent_revision_id  TEXT REFERENCES revisions(revision_id),
    change_count        INTEGER NOT NULL DEFAULT 0
);

-- outbox de efeitos: gravada na MESMA transação da revisão (§4.2)
CREATE TABLE IF NOT EXISTS effects (
    effect_id    TEXT PRIMARY KEY,
    revision_id  TEXT NOT NULL REFERENCES revisions(revision_id),
    effect_type  TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL
);

-- ------------------------------------------------------------------ fontes
CREATE TABLE IF NOT EXISTS sources (
    source_id    TEXT PRIMARY KEY,
    namespace    TEXT NOT NULL,
    source_kind  TEXT NOT NULL,
    uri          TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_versions (
    source_version_id TEXT PRIMARY KEY,
    source_id         TEXT NOT NULL REFERENCES sources(source_id),
    version_label     TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    captured_at       TEXT NOT NULL,
    metadata_json     TEXT NOT NULL DEFAULT '{}'
);

-- --------------------------------------------------------------- entidades
CREATE TABLE IF NOT EXISTS entities (
    entity_id           TEXT PRIMARY KEY,
    namespace           TEXT NOT NULL,
    entity_type         TEXT NOT NULL,
    stable_key          TEXT NOT NULL,
    created_revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
    head_revision_id    TEXT NOT NULL REFERENCES revisions(revision_id)
);

CREATE TABLE IF NOT EXISTS entity_revisions (
    entity_id         TEXT NOT NULL REFERENCES entities(entity_id),
    revision_id       TEXT NOT NULL REFERENCES revisions(revision_id),
    title             TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    source_version_id TEXT REFERENCES source_versions(source_version_id),
    lifecycle_status  TEXT NOT NULL,
    valid_from        TEXT,
    valid_to          TEXT,
    recorded_at       TEXT NOT NULL,
    attributes_json   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (entity_id, revision_id)
);

-- aliases COM origem (§5.2); `origin` é o que separa renomeação confirmada
-- de simples semelhança de nome.
CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id         TEXT NOT NULL REFERENCES entities(entity_id),
    alias             TEXT NOT NULL,
    origin            TEXT NOT NULL,
    namespace         TEXT NOT NULL,
    source_version_id TEXT REFERENCES source_versions(source_version_id),
    revision_id       TEXT NOT NULL REFERENCES revisions(revision_id),
    recorded_at       TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias, origin)
);

-- ------------------------------------------------------------------ fatos
CREATE TABLE IF NOT EXISTS facts (
    fact_id             TEXT PRIMARY KEY,
    namespace           TEXT NOT NULL,
    subject_id          TEXT NOT NULL REFERENCES entities(entity_id),
    predicate           TEXT NOT NULL,
    scope               TEXT NOT NULL,
    created_revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
    head_revision_id    TEXT NOT NULL REFERENCES revisions(revision_id)
);

CREATE TABLE IF NOT EXISTS fact_revisions (
    fact_id             TEXT NOT NULL REFERENCES facts(fact_id),
    revision_id         TEXT NOT NULL REFERENCES revisions(revision_id),
    subject_id          TEXT NOT NULL REFERENCES entities(entity_id),
    predicate           TEXT NOT NULL,
    value               TEXT NOT NULL,
    scope               TEXT NOT NULL,
    nature              TEXT NOT NULL,
    epistemic_status    TEXT NOT NULL,
    lifecycle_status    TEXT NOT NULL,
    approval_state      TEXT NOT NULL DEFAULT 'none',
    asserted_by         TEXT NOT NULL,
    support_recorded_by TEXT,
    source_version_id   TEXT REFERENCES source_versions(source_version_id),
    content_hash        TEXT NOT NULL,
    valid_from          TEXT,
    valid_to            TEXT,
    recorded_at         TEXT NOT NULL,
    PRIMARY KEY (fact_id, revision_id)
);

-- --------------------------------------------------------------- relações
CREATE TABLE IF NOT EXISTS relations (
    relation_id         TEXT PRIMARY KEY,
    namespace           TEXT NOT NULL,
    source_entity_id    TEXT NOT NULL REFERENCES entities(entity_id),
    relation_type       TEXT NOT NULL,
    target_entity_id    TEXT NOT NULL REFERENCES entities(entity_id),
    scope               TEXT NOT NULL,
    created_revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
    head_revision_id    TEXT NOT NULL REFERENCES revisions(revision_id)
);

CREATE TABLE IF NOT EXISTS relation_revisions (
    relation_id         TEXT NOT NULL REFERENCES relations(relation_id),
    revision_id         TEXT NOT NULL REFERENCES revisions(revision_id),
    source_entity_id    TEXT NOT NULL REFERENCES entities(entity_id),
    relation_type       TEXT NOT NULL,
    target_entity_id    TEXT NOT NULL REFERENCES entities(entity_id),
    scope               TEXT NOT NULL,
    epistemic_status    TEXT NOT NULL,
    lifecycle_status    TEXT NOT NULL,
    asserted_by         TEXT NOT NULL,
    support_recorded_by TEXT,
    source_version_id   TEXT REFERENCES source_versions(source_version_id),
    content_hash        TEXT NOT NULL,
    valid_from          TEXT,
    valid_to            TEXT,
    recorded_at         TEXT NOT NULL,
    attributes_json     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (relation_id, revision_id)
);

-- ------------------------------------------------------------- evidências
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id       TEXT PRIMARY KEY,
    namespace         TEXT NOT NULL,
    source_kind       TEXT NOT NULL,
    content_kind      TEXT NOT NULL,
    source_version_id TEXT NOT NULL REFERENCES source_versions(source_version_id),
    locator_json      TEXT NOT NULL,
    snippet_hash      TEXT,
    recorded_at       TEXT NOT NULL
);

-- `evidence_refs` materializado: um vínculo por (evidência, alvo, revisão),
-- para que a lista de evidências de um fato seja consultável por revisão.
CREATE TABLE IF NOT EXISTS evidence_links (
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    target_kind TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
    PRIMARY KEY (evidence_id, target_kind, target_id, revision_id)
);

-- ------------------------------------------------------------------ índices
CREATE INDEX IF NOT EXISTS ix_entities_ns        ON entities(namespace, entity_type);
CREATE INDEX IF NOT EXISTS ix_entities_key       ON entities(namespace, entity_type, stable_key);
CREATE INDEX IF NOT EXISTS ix_entrev_rev         ON entity_revisions(revision_id);
CREATE INDEX IF NOT EXISTS ix_entrev_srcver      ON entity_revisions(source_version_id);
CREATE INDEX IF NOT EXISTS ix_entrev_life        ON entity_revisions(lifecycle_status);
CREATE INDEX IF NOT EXISTS ix_alias_ns           ON entity_aliases(namespace, alias);
CREATE INDEX IF NOT EXISTS ix_facts_ns           ON facts(namespace);
CREATE INDEX IF NOT EXISTS ix_facts_subject      ON facts(subject_id, predicate);
CREATE INDEX IF NOT EXISTS ix_factrev_rev        ON fact_revisions(revision_id);
CREATE INDEX IF NOT EXISTS ix_factrev_subject    ON fact_revisions(subject_id, predicate);
CREATE INDEX IF NOT EXISTS ix_factrev_srcver     ON fact_revisions(source_version_id);
CREATE INDEX IF NOT EXISTS ix_factrev_life       ON fact_revisions(lifecycle_status, nature);
CREATE INDEX IF NOT EXISTS ix_relations_ns       ON relations(namespace, relation_type);
CREATE INDEX IF NOT EXISTS ix_relations_src      ON relations(source_entity_id, relation_type);
CREATE INDEX IF NOT EXISTS ix_relations_tgt      ON relations(target_entity_id, relation_type);
CREATE INDEX IF NOT EXISTS ix_relrev_rev         ON relation_revisions(revision_id);
CREATE INDEX IF NOT EXISTS ix_relrev_src         ON relation_revisions(source_entity_id, relation_type);
CREATE INDEX IF NOT EXISTS ix_relrev_tgt         ON relation_revisions(target_entity_id, relation_type);
CREATE INDEX IF NOT EXISTS ix_relrev_srcver      ON relation_revisions(source_version_id);
CREATE INDEX IF NOT EXISTS ix_relrev_life        ON relation_revisions(lifecycle_status);
CREATE INDEX IF NOT EXISTS ix_evidence_srcver    ON evidence(source_version_id);
CREATE INDEX IF NOT EXISTS ix_evidence_ns        ON evidence(namespace, source_kind);
CREATE INDEX IF NOT EXISTS ix_evlinks_target     ON evidence_links(target_kind, target_id);
CREATE INDEX IF NOT EXISTS ix_evlinks_rev        ON evidence_links(revision_id);
CREATE INDEX IF NOT EXISTS ix_srcver_source      ON source_versions(source_id);
CREATE INDEX IF NOT EXISTS ix_srcver_hash        ON source_versions(content_hash);
CREATE INDEX IF NOT EXISTS ix_effects_status     ON effects(status, created_at);
CREATE INDEX IF NOT EXISTS ix_effects_rev        ON effects(revision_id);
"""

#: Nome de toda tabela criada por `DDL`. Usado por `installed_tables()` e pela
#: verificação de integridade estrutural.
TABLES = (
    "schema_version",
    "revisions",
    "effects",
    "sources",
    "source_versions",
    "entities",
    "entity_revisions",
    "entity_aliases",
    "facts",
    "fact_revisions",
    "relations",
    "relation_revisions",
    "evidence",
    "evidence_links",
)


def apply_schema(conn: sqlite3.Connection, now: str) -> int:
    """Cria o schema se ausente e valida a versão instalada.

    Levanta `SchemaVersionMismatch` quando o banco está em versão diferente de
    `SCHEMA_VERSION`: migração é ato explícito (W8), nunca efeito colateral de
    abrir conexão.
    """
    conn.executescript(DDL)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    installed = row[0] if row else None
    if installed is None:
        conn.execute(
            "INSERT INTO schema_version(version, applied_at, note) VALUES (?,?,?)",
            (SCHEMA_VERSION, now, "initial"),
        )
        return SCHEMA_VERSION
    if int(installed) != SCHEMA_VERSION:
        raise SchemaVersionMismatch(
            f"knowledge.db está na versão {installed}; este código suporta "
            f"{SCHEMA_VERSION}. Migração é explícita — não há upgrade automático."
        )
    return int(installed)


def installed_version(conn: sqlite3.Connection) -> int | None:
    """Versão registrada em `schema_version`, ou `None` se o banco é novo."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row and row[0] is not None else None
