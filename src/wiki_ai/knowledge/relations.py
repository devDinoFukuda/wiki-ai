from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Sequence

from .errors import RelationCycle, UnknownReference
from .model import Confidence, EntityId, Relation, validate_kind

DIRECTIONS: tuple[str, ...] = ("out", "in", "both")

CYCLE_FORBIDDEN_KINDS: frozenset[str] = frozenset({"supersedes"})

RELATION_COLUMNS = "relation_id, kind, source_id, target_id, attributes_json, confidence"


def entity_exists(conn: sqlite3.Connection, entity: EntityId) -> bool:
    row = conn.execute(
        "SELECT 1 FROM entities WHERE entity_id=?", (entity.value,)
    ).fetchone()
    return row is not None


def validate_relation(conn: sqlite3.Connection, relation: Relation) -> None:
    validate_kind(relation.kind)
    if not entity_exists(conn, relation.source_id):
        raise UnknownReference(f"source_id inexistente: {relation.source_id.value}")
    if not entity_exists(conn, relation.target_id):
        raise UnknownReference(f"target_id inexistente: {relation.target_id.value}")
    if relation.kind not in CYCLE_FORBIDDEN_KINDS:
        return
    if relation.source_id == relation.target_id:
        raise RelationCycle(
            f"{relation.kind} de {relation.source_id.value} para si mesma fecha ciclo"
        )
    if path_exists(conn, relation.kind, relation.target_id, relation.source_id):
        raise RelationCycle(
            f"gravar {relation.kind} {relation.source_id.value} -> "
            f"{relation.target_id.value} fecharia ciclo"
        )


def path_exists(
    conn: sqlite3.Connection, kind: str, start: EntityId, goal: EntityId
) -> bool:
    seen: set[str] = set()
    stack: list[str] = [start.value]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT target_id FROM relations WHERE source_id=? AND kind=?",
            (node, kind),
        ).fetchall()
        for (target,) in rows:
            if target == goal.value:
                return True
            if target not in seen:
                stack.append(target)
    return False


def neighborhood(
    conn: sqlite3.Connection,
    entity: EntityId,
    direction: str = "both",
    kinds: Iterable[str] | None = None,
) -> list[Relation]:
    if direction not in DIRECTIONS:
        raise UnknownReference(
            f"direction {direction!r} inválida; use {', '.join(DIRECTIONS)}"
        )
    if direction == "out":
        where = "source_id = ?"
        params: list[Any] = [entity.value]
    elif direction == "in":
        where = "target_id = ?"
        params = [entity.value]
    else:
        where = "(source_id = ? OR target_id = ?)"
        params = [entity.value, entity.value]
    sql = f"SELECT {RELATION_COLUMNS} FROM relations WHERE {where}"
    selected = [validate_kind(k) for k in (kinds or ())]
    if selected:
        sql += f" AND kind IN ({','.join('?' for _ in selected)})"
        params.extend(selected)
    sql += " ORDER BY kind, relation_id"
    return [row_to_relation(row) for row in conn.execute(sql, params).fetchall()]


def neighbor_ids(
    conn: sqlite3.Connection,
    entity: EntityId,
    direction: str = "both",
    kinds: Iterable[str] | None = None,
) -> tuple[str, ...]:
    found: list[str] = []
    for relation in neighborhood(conn, entity, direction, kinds):
        other = (
            relation.target_id.value
            if relation.source_id == entity
            else relation.source_id.value
        )
        if other not in found:
            found.append(other)
    return tuple(found)


def row_to_relation(row: Sequence[Any]) -> Relation:
    return Relation(
        id=str(row[0]),
        kind=str(row[1]),
        source_id=EntityId(str(row[2])),
        target_id=EntityId(str(row[3])),
        attributes=json.loads(str(row[4])),
        confidence=Confidence(str(row[5])),
    )
