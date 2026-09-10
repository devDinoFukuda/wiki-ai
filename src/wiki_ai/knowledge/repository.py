from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Iterable, Sequence

from . import relations as relation_store
from .errors import (
    FormatVersionMismatch,
    RevisionClosed,
    UnknownReference,
    UnsupportedEvidence,
)
from .evidence import assert_supports_implemented, locator_from_dict
from .identity import new_revision_id
from .model import (
    Confidence,
    Entity,
    EntityId,
    KnowledgeState,
    Evidence,
    Relation,
    Revision,
    SourceVersion,
    validate_kind,
)
from .taxonomy import validate_attributes, validate_pair

FORMAT_VERSION = "3"
FORMAT_VERSION_KEY = "format_version"
DATABASE_FILENAME = "state.db"

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    revision_id TEXT PRIMARY KEY,
    parent_id   TEXT REFERENCES revisions(revision_id),
    author      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS source_versions (
    source_version_key TEXT PRIMARY KEY,
    source_id          TEXT NOT NULL,
    version_hash       TEXT NOT NULL,
    locator_root       TEXT NOT NULL,
    captured_at        TEXT NOT NULL,
    revision_id        TEXT NOT NULL REFERENCES revisions(revision_id)
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id       TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    state           TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    revision_id     TEXT NOT NULL REFERENCES revisions(revision_id)
);

CREATE TABLE IF NOT EXISTS entity_source_versions (
    entity_id          TEXT NOT NULL REFERENCES entities(entity_id),
    source_version_key TEXT NOT NULL REFERENCES source_versions(source_version_key),
    ordinal            INTEGER NOT NULL,
    PRIMARY KEY (entity_id, source_version_key)
);

CREATE TABLE IF NOT EXISTS relations (
    relation_id     TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    source_id       TEXT NOT NULL REFERENCES entities(entity_id),
    target_id       TEXT NOT NULL REFERENCES entities(entity_id),
    attributes_json TEXT NOT NULL DEFAULT '{}',
    confidence      TEXT NOT NULL DEFAULT 'unresolved',
    revision_id     TEXT NOT NULL REFERENCES revisions(revision_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id        TEXT PRIMARY KEY,
    source_id          TEXT NOT NULL,
    version_hash       TEXT NOT NULL,
    source_version_key TEXT NOT NULL,
    locator_json       TEXT NOT NULL,
    excerpt_hash       TEXT NOT NULL,
    captured_at        TEXT NOT NULL,
    revision_id        TEXT NOT NULL REFERENCES revisions(revision_id)
);

CREATE TABLE IF NOT EXISTS evidence_links (
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    entity_id   TEXT NOT NULL REFERENCES entities(entity_id),
    PRIMARY KEY (evidence_id, entity_id)
);

CREATE TABLE IF NOT EXISTS relation_evidence_links (
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    relation_id TEXT NOT NULL REFERENCES relations(relation_id),
    PRIMARY KEY (evidence_id, relation_id)
);

CREATE TABLE IF NOT EXISTS invalidations (
    target_kind        TEXT NOT NULL,
    target_id          TEXT NOT NULL,
    source_version_key TEXT NOT NULL,
    revision_id        TEXT NOT NULL REFERENCES revisions(revision_id),
    PRIMARY KEY (target_kind, target_id)
);

CREATE INDEX IF NOT EXISTS ix_entities_kind    ON entities(kind);
CREATE INDEX IF NOT EXISTS ix_entities_rev     ON entities(revision_id);
CREATE INDEX IF NOT EXISTS ix_entsrc_version   ON entity_source_versions(source_version_key);
CREATE INDEX IF NOT EXISTS ix_relations_source ON relations(source_id, kind);
CREATE INDEX IF NOT EXISTS ix_relations_target ON relations(target_id, kind);
CREATE INDEX IF NOT EXISTS ix_evidence_version ON evidence(source_version_key);
CREATE INDEX IF NOT EXISTS ix_evidence_source  ON evidence(source_id);
CREATE INDEX IF NOT EXISTS ix_evlinks_entity   ON evidence_links(entity_id);
CREATE INDEX IF NOT EXISTS ix_rellinks_rel     ON relation_evidence_links(relation_id);
CREATE INDEX IF NOT EXISTS ix_srcver_source    ON source_versions(source_id);
"""

ENTITY_COLUMNS = "entity_id, kind, name, attributes_json, state, confidence"
EVIDENCE_COLUMNS = (
    "e.evidence_id, e.source_id, e.version_hash, e.locator_json, e.excerpt_hash, e.captured_at"
)
SOURCE_VERSION_COLUMNS = "source_id, version_hash, locator_root, captured_at"
RELATION_COLUMNS = "relation_id, kind, source_id, target_id, attributes_json, confidence"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(dict(value or {}), sort_keys=True, ensure_ascii=False, default=str)


def _ensure_wal(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    deadline = time.monotonic() + max(0.1, busy_timeout_ms / 1000.0)
    while True:
        mode = str((conn.execute("PRAGMA journal_mode").fetchone() or [""])[0]).lower()
        if mode == "wal":
            return
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _apply_format_version(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?", (FORMAT_VERSION_KEY,)
    ).fetchone()
    installed = row[0] if row else None
    if installed is None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?,?)",
                (FORMAT_VERSION_KEY, FORMAT_VERSION),
            )
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (FORMAT_VERSION_KEY,)
            ).fetchone()
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        installed = row[0] if row else FORMAT_VERSION
    if str(installed) != FORMAT_VERSION:
        raise FormatVersionMismatch(
            f"{DATABASE_FILENAME} está no formato {installed!r}; este código suporta "
            f"{FORMAT_VERSION!r}. Migração é ato explícito."
        )
    return str(installed)


def connect(path: str, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    _ensure_wal(conn, busy_timeout_ms)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(DDL)
    _apply_format_version(conn)
    return conn


class RevisionTransaction:
    def __init__(self, repository: "KnowledgeRepository", revision: Revision) -> None:
        self._repository = repository
        self._conn = repository.conn
        self._revision = revision
        self._open = False
        self._touched_entities: list[str] = []
        self._touched_relations: list[str] = []

    @property
    def revision(self) -> Revision:
        return self._revision

    @property
    def revision_id(self) -> str:
        return self._revision.id

    def __enter__(self) -> "RevisionTransaction":
        self._conn.execute("BEGIN IMMEDIATE")
        self._open = True
        self._conn.execute(
            "INSERT INTO revisions(revision_id, parent_id, author, created_at, summary) "
            "VALUES (?,?,?,?,?)",
            (
                self._revision.id,
                self._revision.parent_id,
                self._revision.author,
                self._revision.created_at,
                self._revision.summary,
            ),
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc_type is not None:
            self._conn.execute("ROLLBACK")
            self._open = False
            return False
        try:
            self._check_support()
        except BaseException:
            self._conn.execute("ROLLBACK")
            self._open = False
            raise
        self._conn.execute("COMMIT")
        self._open = False
        return False

    def _guard(self) -> None:
        if not self._open:
            raise RevisionClosed(
                "revisão encerrada: abra outra com begin_revision() antes de escrever"
            )

    def put_source_version(self, version: SourceVersion) -> str:
        self._guard()
        self._conn.execute(
            "INSERT INTO source_versions(source_version_key, source_id, version_hash, "
            "locator_root, captured_at, revision_id) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(source_version_key) DO UPDATE SET locator_root=excluded.locator_root, "
            "captured_at=excluded.captured_at, revision_id=excluded.revision_id",
            (
                version.key,
                version.source_id,
                version.version_hash,
                version.locator_root,
                version.captured_at,
                self._revision.id,
            ),
        )
        return version.key

    def put_entity(self, entity: Entity) -> EntityId:
        self._guard()
        validate_attributes(entity.kind, entity.attributes)
        for key in entity.source_versions:
            known = self._conn.execute(
                "SELECT 1 FROM source_versions WHERE source_version_key=?", (key,)
            ).fetchone()
            if known is None:
                raise UnknownReference(
                    f"entidade {entity.id.value} cita source_version desconhecida: {key}"
                )
        self._conn.execute(
            "INSERT INTO entities(entity_id, kind, name, attributes_json, state, "
            "confidence, revision_id) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_id) DO UPDATE SET kind=excluded.kind, name=excluded.name, "
            "attributes_json=excluded.attributes_json, state=excluded.state, "
            "confidence=excluded.confidence, revision_id=excluded.revision_id",
            (
                entity.id.value,
                entity.kind,
                entity.name,
                _dumps(entity.attributes),
                entity.state.value,
                entity.confidence.value,
                self._revision.id,
            ),
        )
        self._conn.execute(
            "DELETE FROM entity_source_versions WHERE entity_id=?", (entity.id.value,)
        )
        for ordinal, key in enumerate(entity.source_versions):
            self._conn.execute(
                "INSERT OR REPLACE INTO entity_source_versions(entity_id, "
                "source_version_key, ordinal) VALUES (?,?,?)",
                (entity.id.value, key, ordinal),
            )
        if entity.id.value not in self._touched_entities:
            self._touched_entities.append(entity.id.value)
        return entity.id

    def put_relation(self, relation: Relation) -> str:
        self._guard()
        relation_store.validate_relation(self._conn, relation)
        validate_pair(
            relation.kind,
            self._kind_of(relation.source_id),
            self._kind_of(relation.target_id),
        )
        if relation.id not in self._touched_relations:
            self._touched_relations.append(relation.id)
        self._conn.execute(
            "INSERT INTO relations(relation_id, kind, source_id, target_id, "
            "attributes_json, confidence, revision_id) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(relation_id) DO UPDATE SET attributes_json=excluded.attributes_json, "
            "confidence=excluded.confidence, revision_id=excluded.revision_id",
            (
                relation.id,
                relation.kind,
                relation.source_id.value,
                relation.target_id.value,
                _dumps(relation.attributes),
                relation.confidence.value,
                self._revision.id,
            ),
        )
        return relation.id

    def put_evidence(
        self,
        evidence: Evidence,
        entity_ids: Sequence[EntityId] = (),
        relation_ids: Sequence[str] = (),
    ) -> str:
        self._guard()
        known = self._conn.execute(
            "SELECT 1 FROM source_versions WHERE source_version_key=?",
            (evidence.source_version_key,),
        ).fetchone()
        if known is None:
            raise UnknownReference(
                f"evidência {evidence.id} cita source_version desconhecida: "
                f"{evidence.source_version_key}"
            )
        self._conn.execute(
            "INSERT INTO evidence(evidence_id, source_id, version_hash, "
            "source_version_key, locator_json, excerpt_hash, captured_at, revision_id) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(evidence_id) DO UPDATE SET "
            "excerpt_hash=excluded.excerpt_hash, captured_at=excluded.captured_at, "
            "revision_id=excluded.revision_id",
            (
                evidence.id,
                evidence.source_id,
                evidence.version_hash,
                evidence.source_version_key,
                json.dumps(evidence.locator.to_dict(), sort_keys=True, ensure_ascii=False),
                evidence.excerpt_hash,
                evidence.captured_at,
                self._revision.id,
            ),
        )
        for entity in entity_ids:
            if not relation_store.entity_exists(self._conn, entity):
                raise UnknownReference(
                    f"evidência {evidence.id} vinculada a entidade inexistente: {entity.value}"
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO evidence_links(evidence_id, entity_id) VALUES (?,?)",
                (evidence.id, entity.value),
            )
            if entity.value not in self._touched_entities:
                self._touched_entities.append(entity.value)
        for relation_id in relation_ids:
            known_relation = self._conn.execute(
                "SELECT 1 FROM relations WHERE relation_id=?", (relation_id,)
            ).fetchone()
            if known_relation is None:
                raise UnknownReference(
                    f"evidência {evidence.id} vinculada a relação inexistente: {relation_id}"
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO relation_evidence_links(evidence_id, relation_id) "
                "VALUES (?,?)",
                (evidence.id, relation_id),
            )
            if relation_id not in self._touched_relations:
                self._touched_relations.append(relation_id)
        return evidence.id

    def record_invalidation(self, target_kind: str, target_id: str, key: str) -> None:
        self._guard()
        self._conn.execute(
            "INSERT OR REPLACE INTO invalidations(target_kind, target_id, "
            "source_version_key, revision_id) VALUES (?,?,?,?)",
            (validate_kind(target_kind), target_id, key, self._revision.id),
        )

    def _kind_of(self, entity_id: EntityId) -> str:
        row = self._conn.execute(
            "SELECT kind FROM entities WHERE entity_id=?", (entity_id.value,)
        ).fetchone()
        if row is None:
            raise UnknownReference(f"entidade inexistente: {entity_id.value}")
        return str(row[0])

    def _check_support(self) -> None:
        self._check_relation_support()
        for entity_id in self._touched_entities:
            entity = self._repository.get_entity(EntityId(entity_id))
            if entity is None:
                continue
            if entity.state is not KnowledgeState.IMPLEMENTED:
                continue
            if entity.confidence is not Confidence.SUPPORTED:
                continue
            assert_supports_implemented(self._repository.evidence_for(entity.id))

    def _check_relation_support(self) -> None:
        for relation_id in self._touched_relations:
            relation = self._repository.get_relation(relation_id)
            if relation is None or relation.confidence is not Confidence.SUPPORTED:
                continue
            if self._repository.evidence_for_relation(relation_id):
                continue
            endpoints = (
                self._repository.get_entity(relation.source_id),
                self._repository.get_entity(relation.target_id),
            )
            if all(
                node is not None and node.confidence is Confidence.SUPPORTED
                for node in endpoints
            ):
                continue
            raise UnsupportedEvidence(
                f"relação {relation.kind} {relation.source_id.value} -> "
                f"{relation.target_id.value} marcada supported sem evidência própria "
                "e sem extremos supported"
            )


class KnowledgeRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: str, busy_timeout_ms: int = 5000) -> "KnowledgeRepository":
        return cls(connect(str(path), busy_timeout_ms))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "KnowledgeRepository":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.close()
        return False

    @property
    def format_version(self) -> str:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (FORMAT_VERSION_KEY,)
        ).fetchone()
        return str(row[0]) if row else ""

    def begin_revision(self, author: str, summary: str = "") -> RevisionTransaction:
        revision = Revision(
            id=new_revision_id(),
            parent_id=self.head_revision_id(),
            author=author,
            created_at=utc_now(),
            summary=summary,
        )
        return RevisionTransaction(self, revision)

    def head_revision_id(self) -> str | None:
        row = self.conn.execute(
            "SELECT revision_id FROM revisions ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else None

    def get_revision(self, revision_id: str) -> Revision | None:
        row = self.conn.execute(
            "SELECT revision_id, parent_id, author, created_at, summary FROM revisions "
            "WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            return None
        return Revision(
            id=str(row[0]),
            parent_id=row[1],
            author=str(row[2]),
            created_at=str(row[3]),
            summary=str(row[4]),
        )

    def revision_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0])

    def get_entity(self, entity_id: EntityId) -> Entity | None:
        row = self.conn.execute(
            f"SELECT {ENTITY_COLUMNS} FROM entities WHERE entity_id=?",
            (entity_id.value,),
        ).fetchone()
        return self.row_to_entity(row) if row else None

    def find_entities(self, kind: str | None = None) -> list[Entity]:
        sql = f"SELECT {ENTITY_COLUMNS} FROM entities"
        params: list[Any] = []
        if kind is not None:
            sql += " WHERE kind=?"
            params.append(validate_kind(kind))
        sql += " ORDER BY entity_id"
        return [self.row_to_entity(row) for row in self.conn.execute(sql, params)]

    def entity_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0])

    def relations_of(
        self,
        entity_id: EntityId,
        direction: str = "both",
        kinds: Iterable[str] | None = None,
    ) -> list[Relation]:
        return relation_store.neighborhood(self.conn, entity_id, direction, kinds)

    def get_relation(self, relation_id: str) -> Relation | None:
        row = self.conn.execute(
            f"SELECT {RELATION_COLUMNS} FROM relations WHERE relation_id=?",
            (relation_id,),
        ).fetchone()
        return relation_store.row_to_relation(row) if row else None

    def find_relations(self, kind: str | None = None) -> list[Relation]:
        sql = f"SELECT {RELATION_COLUMNS} FROM relations"
        params: list[Any] = []
        if kind is not None:
            sql += " WHERE kind=?"
            params.append(validate_kind(kind))
        sql += " ORDER BY relation_id"
        return [relation_store.row_to_relation(row) for row in self.conn.execute(sql, params)]

    def evidence_for_relation(self, relation_id: str) -> list[Evidence]:
        rows = self.conn.execute(
            f"SELECT {EVIDENCE_COLUMNS} FROM evidence e JOIN relation_evidence_links l "
            "ON l.evidence_id = e.evidence_id WHERE l.relation_id=? ORDER BY e.evidence_id",
            (relation_id,),
        ).fetchall()
        return [self._row_to_evidence(row) for row in rows]

    def relation_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0])

    def evidence_for(self, entity_id: EntityId) -> list[Evidence]:
        rows = self.conn.execute(
            f"SELECT {EVIDENCE_COLUMNS} FROM evidence e JOIN evidence_links l "
            "ON l.evidence_id = e.evidence_id WHERE l.entity_id=? ORDER BY e.evidence_id",
            (entity_id.value,),
        ).fetchall()
        return [self._row_to_evidence(row) for row in rows]

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        row = self.conn.execute(
            f"SELECT {EVIDENCE_COLUMNS} FROM evidence e WHERE e.evidence_id=?",
            (evidence_id,),
        ).fetchone()
        return self._row_to_evidence(row) if row else None

    def source_versions(self, source_id: str | None = None) -> list[SourceVersion]:
        sql = f"SELECT {SOURCE_VERSION_COLUMNS} FROM source_versions"
        params: list[Any] = []
        if source_id is not None:
            sql += " WHERE source_id=?"
            params.append(source_id)
        sql += " ORDER BY source_id, captured_at, version_hash"
        return [
            SourceVersion(
                source_id=str(row[0]),
                version_hash=str(row[1]),
                locator_root=str(row[2]),
                captured_at=str(row[3]),
            )
            for row in self.conn.execute(sql, params)
        ]

    def entities_using(self, source_version_key: str) -> tuple[str, ...]:
        rows = self.conn.execute(
            "SELECT entity_id FROM entity_source_versions WHERE source_version_key=? "
            "UNION SELECT l.entity_id FROM evidence_links l JOIN evidence e "
            "ON e.evidence_id = l.evidence_id WHERE e.source_version_key=?",
            (source_version_key, source_version_key),
        ).fetchall()
        return tuple(sorted(str(row[0]) for row in rows))

    def all_evidence_keys(self) -> tuple[tuple[str, str], ...]:
        rows = self.conn.execute(
            "SELECT evidence_id, source_version_key FROM evidence ORDER BY evidence_id"
        ).fetchall()
        return tuple((str(row[0]), str(row[1])) for row in rows)

    def evidence_using(self, source_version_key: str) -> tuple[str, ...]:
        rows = self.conn.execute(
            "SELECT evidence_id FROM evidence WHERE source_version_key=? ORDER BY evidence_id",
            (source_version_key,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def invalidated(self, target_kind: str) -> tuple[str, ...]:
        rows = self.conn.execute(
            "SELECT target_id FROM invalidations WHERE target_kind=? ORDER BY target_id",
            (validate_kind(target_kind),),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def row_to_entity(self, row: Sequence[Any]) -> Entity:
        entity_id = str(row[0])
        keys = self.conn.execute(
            "SELECT source_version_key FROM entity_source_versions WHERE entity_id=? "
            "ORDER BY ordinal",
            (entity_id,),
        ).fetchall()
        return Entity(
            id=EntityId(entity_id),
            kind=str(row[1]),
            name=str(row[2]),
            attributes=json.loads(str(row[3])),
            state=KnowledgeState(row[4]),
            confidence=Confidence(row[5]),
            source_versions=tuple(str(key[0]) for key in keys),
        )

    def _row_to_evidence(self, row: Sequence[Any]) -> Evidence:
        return Evidence(
            id=str(row[0]),
            source_id=str(row[1]),
            version_hash=str(row[2]),
            locator=locator_from_dict(json.loads(str(row[3]))),
            excerpt_hash=str(row[4]),
            captured_at=str(row[5]),
        )
