"""Matriz de tipos permitidos por relação e navegação tipada (plano §5.5).

"Definir pares de tipos permitidos por relação; rejeitar combinação inválida"
é requisito do plano. A matriz abaixo é a definição executável: `validate_pair`
levanta `InvalidRelationPair` com a lista de pares aceitos, para que o erro
seja objetivo e diagnosticável, não um "relação inválida" genérico.

Duas relações são reflexivas por classe (`SAME_TYPE`): `contradicts` e
`supersedes` só ligam entidades do MESMO tipo — substituir um `Requirement`
por um `Component` não é substituição de versão, é confusão de eixo.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Sequence

from .models import (
    DEFAULT_LIFECYCLE,
    EntityType,
    EpistemicStatus,
    InvalidRelationPair,
    LifecycleStatus,
    Relation,
    RelationType,
)

E = EntityType

#: Sentinela: origem e destino devem ter exatamente o mesmo `EntityType`.
SAME_TYPE = "__same_type__"

_TECH = frozenset(
    {E.SYSTEM, E.COMPONENT, E.CAPABILITY, E.CONTRACT, E.DATA_ENTITY, E.FLOW, E.BUSINESS_RULE}
)
_INTENT = frozenset({E.INITIATIVE, E.DECISION, E.REQUIREMENT, E.STORY, E.REFINEMENT, E.DEFECT})
_ALL = frozenset(E)

#: Pares permitidos por tipo de relação. Cada entrada é uma lista de blocos
#: ``(origens, destinos)``; a relação é válida se casar com QUALQUER bloco.
ALLOWED_PAIRS: dict[RelationType, object] = {
    RelationType.CONTAINS: [
        (frozenset({E.SYSTEM}), _TECH),
        (
            frozenset({E.COMPONENT}),
            frozenset({E.COMPONENT, E.CAPABILITY, E.CONTRACT, E.DATA_ENTITY, E.FLOW, E.BUSINESS_RULE}),
        ),
        (frozenset({E.CAPABILITY}), frozenset({E.FLOW, E.BUSINESS_RULE, E.CONTRACT})),
        (frozenset({E.FLOW}), frozenset({E.FLOW, E.BUSINESS_RULE})),
        (
            frozenset({E.INITIATIVE}),
            frozenset({E.STORY, E.REQUIREMENT, E.DECISION, E.REFINEMENT, E.DEFECT}),
        ),
    ],
    # §5.5 (exemplo normativo do plano): Component/Capability -> Component/Contract
    RelationType.CALLS: [
        (frozenset({E.COMPONENT, E.CAPABILITY}), frozenset({E.COMPONENT, E.CONTRACT})),
    ],
    RelationType.READS: [
        (frozenset({E.COMPONENT, E.CAPABILITY, E.FLOW}), frozenset({E.DATA_ENTITY, E.CONTRACT})),
    ],
    RelationType.WRITES: [
        (frozenset({E.COMPONENT, E.CAPABILITY, E.FLOW}), frozenset({E.DATA_ENTITY, E.CONTRACT})),
    ],
    RelationType.PUBLISHES: [
        (frozenset({E.COMPONENT, E.CAPABILITY, E.FLOW}), frozenset({E.CONTRACT})),
    ],
    RelationType.CONSUMES: [
        (frozenset({E.COMPONENT, E.CAPABILITY, E.FLOW}), frozenset({E.CONTRACT})),
    ],
    RelationType.DEPENDS_ON: [
        (
            frozenset({E.SYSTEM, E.COMPONENT, E.CAPABILITY, E.CONTRACT, E.DATA_ENTITY}),
            frozenset({E.SYSTEM, E.COMPONENT, E.CAPABILITY, E.CONTRACT, E.DATA_ENTITY}),
        ),
    ],
    RelationType.IMPLEMENTS: [
        (
            frozenset({E.COMPONENT, E.CAPABILITY, E.FLOW, E.BUSINESS_RULE, E.CONTRACT}),
            frozenset({E.REQUIREMENT, E.STORY, E.DECISION, E.CAPABILITY, E.BUSINESS_RULE}),
        ),
    ],
    RelationType.VERIFIES: [
        (
            frozenset({E.SOURCE, E.COMPONENT}),
            frozenset(
                {
                    E.BUSINESS_RULE,
                    E.FLOW,
                    E.CAPABILITY,
                    E.CONTRACT,
                    E.COMPONENT,
                    E.DATA_ENTITY,
                    E.REQUIREMENT,
                    E.STORY,
                }
            ),
        ),
    ],
    RelationType.RECORDS: [
        (frozenset({E.SOURCE}), _ALL - frozenset({E.SOURCE})),
    ],
    RelationType.BELONGS_TO: [
        (frozenset({E.STORY, E.REQUIREMENT, E.DECISION, E.REFINEMENT, E.DEFECT}), frozenset({E.INITIATIVE})),
        (
            frozenset({E.COMPONENT, E.CAPABILITY, E.CONTRACT, E.DATA_ENTITY, E.FLOW, E.BUSINESS_RULE}),
            frozenset({E.SYSTEM, E.COMPONENT, E.CAPABILITY}),
        ),
    ],
    # §5.5 (exemplo normativo do plano): Refinement -> Decision/Requirement/Story
    RelationType.REFINES: [
        (frozenset({E.REFINEMENT}), frozenset({E.DECISION, E.REQUIREMENT, E.STORY})),
    ],
    RelationType.PROPOSES_CHANGE_TO: [
        (frozenset({E.STORY, E.REQUIREMENT, E.DECISION, E.REFINEMENT, E.DEFECT}), _TECH),
    ],
    RelationType.CONTRADICTS: SAME_TYPE,
    RelationType.SUPERSEDES: SAME_TYPE,
    RelationType.DERIVED_FROM: [(_ALL, _ALL)],
}

#: Relações em que ciclo é ESPERADO e não deve ser rejeitado (§5.5).
CYCLE_ALLOWED: frozenset[RelationType] = frozenset(
    {RelationType.DEPENDS_ON, RelationType.CALLS, RelationType.CONTRADICTS}
)

#: Relações em que ciclo é inválido por definição (§5.5: "ciclos de
#: substituição de versões são inválidos").
CYCLE_FORBIDDEN: frozenset[RelationType] = frozenset({RelationType.SUPERSEDES})


def validate_pair(
    relation_type: RelationType, source_type: EntityType, target_type: EntityType
) -> None:
    """Rejeita combinação (origem, tipo, destino) fora da matriz."""
    rule = ALLOWED_PAIRS.get(relation_type)
    if rule is None:
        raise InvalidRelationPair(f"tipo de relação sem matriz definida: {relation_type}")
    if rule is SAME_TYPE:
        if source_type is not target_type:
            raise InvalidRelationPair(
                f"{relation_type.value} exige entidades da MESMA classe; "
                f"recebido {source_type.value} -> {target_type.value} (§5.5)"
            )
        return
    for sources, targets in rule:  # type: ignore[union-attr]
        if source_type in sources and target_type in targets:
            return
    raise InvalidRelationPair(
        f"{relation_type.value} não aceita {source_type.value} -> {target_type.value}. "
        f"Pares aceitos: {describe_allowed(relation_type)}"
    )


def describe_allowed(relation_type: RelationType) -> str:
    """Texto objetivo dos pares aceitos, usado na mensagem de rejeição."""
    rule = ALLOWED_PAIRS.get(relation_type)
    if rule is SAME_TYPE:
        return "origem e destino do mesmo EntityType"
    if rule is None:
        return "(indefinido)"
    blocks = []
    for sources, targets in rule:  # type: ignore[union-attr]
        s = "|".join(sorted(t.value for t in sources))
        d = "|".join(sorted(t.value for t in targets))
        blocks.append(f"{s} -> {d}")
    return "; ".join(blocks)


def allows_cycle(relation_type: RelationType) -> bool:
    """True quando ciclos são permitidos naquele tipo (dependência, chamada)."""
    return relation_type not in CYCLE_FORBIDDEN


# --------------------------------------------------------------------------
# Navegação tipada
# --------------------------------------------------------------------------

#: Projeção única usada por toda leitura de relação, na ordem consumida por
#: `row_to_relation`. Manter as duas em sincronia é obrigatório.
RELATION_COLUMNS = (
    "rr.relation_id, rr.revision_id, r.namespace, rr.source_entity_id, rr.relation_type, "
    "rr.target_entity_id, rr.scope, rr.epistemic_status, rr.lifecycle_status, rr.asserted_by, "
    "rr.support_recorded_by, rr.source_version_id, rr.content_hash, rr.valid_from, "
    "rr.valid_to, rr.recorded_at"
)

#: `relation_revisions` unida à identidade, restrita à revisão de cabeça.
RELATION_HEAD_FROM = (
    "FROM relation_revisions rr "
    "JOIN relations r ON r.relation_id = rr.relation_id "
    "  AND r.head_revision_id = rr.revision_id"
)


def _lifecycle_clause(lifecycle: Sequence[LifecycleStatus]) -> tuple[str, list[str]]:
    values = [s.value for s in lifecycle]
    placeholders = ",".join("?" for _ in values)
    return f" AND rr.lifecycle_status IN ({placeholders})", values


def neighbors(
    conn: sqlite3.Connection,
    entity_id: str,
    direction: str = "out",
    relation_types: Iterable[RelationType] | None = None,
    lifecycle: Sequence[LifecycleStatus] = DEFAULT_LIFECYCLE,
) -> list[Relation]:
    """Vizinhos por tipo e direção, filtrando por ciclo de vida.

    `direction`: ``"out"`` (entidade como origem), ``"in"`` (como destino) ou
    ``"both"``. O filtro padrão é só `current`: histórico/substituído só
    aparecem quando `lifecycle` é passado explicitamente.
    """
    if direction not in ("out", "in", "both"):
        raise ValueError("direction deve ser 'out', 'in' ou 'both'")
    if direction == "out":
        where = "rr.source_entity_id = ?"
    elif direction == "in":
        where = "rr.target_entity_id = ?"
    else:
        where = "(rr.source_entity_id = ? OR rr.target_entity_id = ?)"
    params: list[object] = [entity_id, entity_id] if direction == "both" else [entity_id]

    sql = f"SELECT {RELATION_COLUMNS} {RELATION_HEAD_FROM} WHERE {where}"
    if relation_types:
        types = [t.value for t in relation_types]
        sql += f" AND rr.relation_type IN ({','.join('?' for _ in types)})"
        params.extend(types)
    clause, life_params = _lifecycle_clause(lifecycle)
    sql += clause
    params.extend(life_params)

    rows = conn.execute(sql, params).fetchall()
    return [row_to_relation(row, evidence_refs_for(conn, row[0], row[1])) for row in rows]


def evidence_refs_for(conn: sqlite3.Connection, relation: str, revision: str) -> tuple[str, ...]:
    """`evidence_refs` da relação naquela revisão, em ordem estável."""
    rows = conn.execute(
        "SELECT evidence_id FROM evidence_links "
        "WHERE target_kind='relation' AND target_id=? AND revision_id=? ORDER BY evidence_id",
        (relation, revision),
    ).fetchall()
    return tuple(r[0] for r in rows)


def row_to_relation(row: Sequence[object], evidence_refs: tuple[str, ...] = ()) -> Relation:
    """Converte uma linha de `RELATION_COLUMNS` em `Relation`."""
    return Relation(
        relation_id=str(row[0]),
        revision_id=str(row[1]),
        namespace=str(row[2]),
        source_entity_id=str(row[3]),
        relation_type=RelationType(row[4]),
        target_entity_id=str(row[5]),
        scope=str(row[6]),
        epistemic_status=EpistemicStatus(row[7]),
        lifecycle_status=LifecycleStatus(row[8]),
        asserted_by=str(row[9]),
        support_recorded_by=row[10],  # type: ignore[arg-type]
        source_version_id=row[11],  # type: ignore[arg-type]
        evidence_refs=evidence_refs,
        content_hash=str(row[12]),
        valid_from=row[13],  # type: ignore[arg-type]
        valid_to=row[14],  # type: ignore[arg-type]
        recorded_at=str(row[15]),
    )


def supersedes_path_exists(
    conn: sqlite3.Connection, start_entity_id: str, goal_entity_id: str
) -> bool:
    """Existe caminho `supersedes` de `start` até `goal` entre relações vivas?

    Usado por `repository` antes de gravar uma nova aresta `supersedes`: se o
    destino já alcança a origem, a nova aresta fecharia um ciclo de
    substituição de versões — inválido por §5.5.
    """
    seen: set[str] = set()
    stack = [start_entity_id]
    while stack:
        node = stack.pop()
        if node == goal_entity_id and node != start_entity_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT rr.target_entity_id FROM relation_revisions rr "
            "JOIN relations r ON r.relation_id = rr.relation_id "
            "  AND r.head_revision_id = rr.revision_id "
            "WHERE rr.source_entity_id = ? AND rr.relation_type = ? "
            "  AND rr.lifecycle_status NOT IN ('historical','superseded')",
            (node, RelationType.SUPERSEDES.value),
        ).fetchall()
        for (target,) in rows:
            if target == goal_entity_id:
                return True
            if target not in seen:
                stack.append(target)
    return False
