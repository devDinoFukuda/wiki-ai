"""Dependentes afetados por mudança de fonte (plano §5.2/§5.3, aceite A10).

Quando uma `source_version` muda, o conhecimento extraído dela não vira falso —
vira NÃO CONFIÁVEL até revalidação. É o que `LifecycleStatus.STALE` significa
aqui, e por isso a operação é uma nova revisão que grava novas linhas: nada é
deletado, o histórico continua legível por `fact_history`/`relation_history`.

Alcance da invalidação, em três camadas:

1. **Direta** — fato/relação/entidade cuja revisão de cabeça aponta para a
   `source_version` alterada, ou cuja evidência (na revisão de cabeça) cita
   aquela versão.
2. **Transitiva por `derived_from`** — se `B derived_from A` e `A` foi
   afetada, `B` também é. O percurso é reverso (do afetado para quem dele
   deriva) e protegido por conjunto de visitados: `derived_from` pode conter
   ciclo de dados sem que isso trave a varredura.
3. **Fatos/relações das entidades afetadas transitivamente** — conhecimento
   sobre uma entidade derivada de fonte alterada também fica `stale`.

O que NÃO acontece: marcar como `stale` o que já é `historical`/`superseded`
(passado não precisa de revalidação) e apagar qualquer coisa.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .models import LifecycleStatus, RelationType, WriteResult
from .repository import Repository

#: Estados que já são passado: não são invalidados de novo.
_TERMINAL = (LifecycleStatus.HISTORICAL.value, LifecycleStatus.SUPERSEDED.value)


@dataclass(frozen=True)
class Dependents:
    """Conjunto alcançado por uma mudança de fonte."""

    source_version_id: str
    entities: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    relations: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.entities) + len(self.facts) + len(self.relations)


@dataclass
class StaleReport:
    """Resultado de `mark_stale_for_source_version`."""

    revision_id: str
    source_version_id: str
    dependents: Dependents
    changed: list[WriteResult] = field(default_factory=list)
    unchanged: list[WriteResult] = field(default_factory=list)

    @property
    def marked(self) -> int:
        return len(self.changed)


def _direct_entities(conn: sqlite3.Connection, svid: str) -> set[str]:
    rows = conn.execute(
        "SELECT e.entity_id FROM entities e "
        "JOIN entity_revisions er ON er.entity_id=e.entity_id AND er.revision_id=e.head_revision_id "
        "LEFT JOIN evidence_links el ON el.target_kind='entity' AND el.target_id=e.entity_id "
        "  AND el.revision_id=e.head_revision_id "
        "LEFT JOIN evidence ev ON ev.evidence_id=el.evidence_id "
        "WHERE (er.source_version_id=? OR ev.source_version_id=?) "
        f"  AND er.lifecycle_status NOT IN ({','.join('?' * len(_TERMINAL))})",
        (svid, svid, *_TERMINAL),
    ).fetchall()
    return {r[0] for r in rows}


def _direct_facts(conn: sqlite3.Connection, svid: str) -> set[str]:
    rows = conn.execute(
        "SELECT f.fact_id FROM facts f "
        "JOIN fact_revisions fr ON fr.fact_id=f.fact_id AND fr.revision_id=f.head_revision_id "
        "LEFT JOIN evidence_links el ON el.target_kind='fact' AND el.target_id=f.fact_id "
        "  AND el.revision_id=f.head_revision_id "
        "LEFT JOIN evidence ev ON ev.evidence_id=el.evidence_id "
        "WHERE (fr.source_version_id=? OR ev.source_version_id=?) "
        f"  AND fr.lifecycle_status NOT IN ({','.join('?' * len(_TERMINAL))})",
        (svid, svid, *_TERMINAL),
    ).fetchall()
    return {r[0] for r in rows}


def _direct_relations(conn: sqlite3.Connection, svid: str) -> set[str]:
    rows = conn.execute(
        "SELECT r.relation_id FROM relations r "
        "JOIN relation_revisions rr ON rr.relation_id=r.relation_id "
        "  AND rr.revision_id=r.head_revision_id "
        "LEFT JOIN evidence_links el ON el.target_kind='relation' AND el.target_id=r.relation_id "
        "  AND el.revision_id=r.head_revision_id "
        "LEFT JOIN evidence ev ON ev.evidence_id=el.evidence_id "
        "WHERE (rr.source_version_id=? OR ev.source_version_id=?) "
        f"  AND rr.lifecycle_status NOT IN ({','.join('?' * len(_TERMINAL))})",
        (svid, svid, *_TERMINAL),
    ).fetchall()
    return {r[0] for r in rows}


def _derived_closure(conn: sqlite3.Connection, seeds: set[str]) -> set[str]:
    """Fecho transitivo reverso de `derived_from` a partir de `seeds`.

    Aresta `X derived_from Y` significa "X deriva de Y", então a invalidação
    caminha de Y para X. O `visited` é obrigatório: `derived_from` não é
    proibido de ter ciclo (só `supersedes` é), e sem ele a varredura não
    termina.
    """
    visited = set(seeds)
    frontier = list(seeds)
    while frontier:
        node = frontier.pop()
        rows = conn.execute(
            "SELECT rr.source_entity_id FROM relation_revisions rr "
            "JOIN relations r ON r.relation_id=rr.relation_id AND r.head_revision_id=rr.revision_id "
            "WHERE rr.target_entity_id=? AND rr.relation_type=? "
            f"  AND rr.lifecycle_status NOT IN ({','.join('?' * len(_TERMINAL))})",
            (node, RelationType.DERIVED_FROM.value, *_TERMINAL),
        ).fetchall()
        for (child,) in rows:
            if child not in visited:
                visited.add(child)
                frontier.append(child)
    return visited


def _knowledge_about(conn: sqlite3.Connection, entity_ids: set[str]) -> tuple[set[str], set[str]]:
    """Fatos e relações vivos cujo sujeito/origem está no conjunto."""
    if not entity_ids:
        return set(), set()
    marks = ",".join("?" * len(entity_ids))
    ids = list(entity_ids)
    facts = {
        r[0]
        for r in conn.execute(
            "SELECT f.fact_id FROM facts f "
            "JOIN fact_revisions fr ON fr.fact_id=f.fact_id AND fr.revision_id=f.head_revision_id "
            f"WHERE fr.subject_id IN ({marks}) "
            f"  AND fr.lifecycle_status NOT IN ({','.join('?' * len(_TERMINAL))})",
            (*ids, *_TERMINAL),
        ).fetchall()
    }
    rels = {
        r[0]
        for r in conn.execute(
            "SELECT r.relation_id FROM relations r "
            "JOIN relation_revisions rr ON rr.relation_id=r.relation_id "
            "  AND rr.revision_id=r.head_revision_id "
            f"WHERE rr.source_entity_id IN ({marks}) "
            f"  AND rr.lifecycle_status NOT IN ({','.join('?' * len(_TERMINAL))})",
            (*ids, *_TERMINAL),
        ).fetchall()
    }
    return facts, rels


def dependents_of_source_version(conn: sqlite3.Connection, source_version_id: str) -> Dependents:
    """Tudo o que ficaria `stale` se `source_version_id` mudasse. Só leitura.

    Serve para inspeção/plano de impacto antes de gravar, e é a mesma função
    usada por `mark_stale_for_source_version` — não há duas definições de
    alcance que possam divergir.
    """
    direct_entities = _direct_entities(conn, source_version_id)
    facts = _direct_facts(conn, source_version_id)
    rels = _direct_relations(conn, source_version_id)

    reachable = _derived_closure(conn, direct_entities)
    derived_only = reachable - direct_entities
    dfacts, drels = _knowledge_about(conn, derived_only)

    return Dependents(
        source_version_id=source_version_id,
        entities=tuple(sorted(reachable)),
        facts=tuple(sorted(facts | dfacts)),
        relations=tuple(sorted(rels | drels)),
    )


def mark_stale_for_source_version(
    repo: Repository,
    source_version_id: str,
    recorded_by: str,
    reason: str = "",
) -> StaleReport:
    """Marca dependentes como `stale` numa revisão única.

    Uma revisão só: ou todos os dependentes ficam marcados, ou nenhum. Marcar
    metade deixaria conhecimento derivado de fonte alterada sendo servido como
    vigente, que é exatamente o que A10 proíbe.
    """
    dependents = dependents_of_source_version(repo.conn, source_version_id)
    report = StaleReport(revision_id="", source_version_id=source_version_id, dependents=dependents)

    with repo.revision(
        author=recorded_by,
        reason=reason or f"invalidação por mudança de {source_version_id}",
    ) as rev:
        report.revision_id = rev.revision_id
        for eid in dependents.entities:
            _collect(report, rev.set_entity_lifecycle(eid, LifecycleStatus.STALE, recorded_by))
        for fid in dependents.facts:
            _collect(report, rev.set_fact_lifecycle(fid, LifecycleStatus.STALE, recorded_by, reason=reason))
        for rid in dependents.relations:
            _collect(report, rev.set_relation_lifecycle(rid, LifecycleStatus.STALE, recorded_by))
        rev.enqueue_effect(
            "knowledge.invalidated",
            {
                "source_version_id": source_version_id,
                "entities": len(dependents.entities),
                "facts": len(dependents.facts),
                "relations": len(dependents.relations),
                "reason": reason,
            },
        )
    return report


def _collect(report: StaleReport, result: WriteResult) -> None:
    (report.changed if result.changed else report.unchanged).append(result)


def stale_targets(conn: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    """Inventário do que está `stale` agora — insumo do reprocessamento."""
    facts = tuple(
        r[0]
        for r in conn.execute(
            "SELECT f.fact_id FROM facts f JOIN fact_revisions fr "
            "ON fr.fact_id=f.fact_id AND fr.revision_id=f.head_revision_id "
            "WHERE fr.lifecycle_status=? ORDER BY f.fact_id",
            (LifecycleStatus.STALE.value,),
        ).fetchall()
    )
    rels = tuple(
        r[0]
        for r in conn.execute(
            "SELECT r.relation_id FROM relations r JOIN relation_revisions rr "
            "ON rr.relation_id=r.relation_id AND rr.revision_id=r.head_revision_id "
            "WHERE rr.lifecycle_status=? ORDER BY r.relation_id",
            (LifecycleStatus.STALE.value,),
        ).fetchall()
    )
    ents = tuple(
        r[0]
        for r in conn.execute(
            "SELECT e.entity_id FROM entities e JOIN entity_revisions er "
            "ON er.entity_id=e.entity_id AND er.revision_id=e.head_revision_id "
            "WHERE er.lifecycle_status=? ORDER BY e.entity_id",
            (LifecycleStatus.STALE.value,),
        ).fetchall()
    )
    return {"entities": ents, "facts": facts, "relations": rels}


__all__ = [
    "Dependents",
    "StaleReport",
    "dependents_of_source_version",
    "mark_stale_for_source_version",
    "stale_targets",
]
