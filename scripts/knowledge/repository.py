"""Transações, invariantes e consultas tipadas de `knowledge.db` (§4.2, §5).

Toda escrita passa por uma REVISÃO. Não existe `put_fact` solto: o chamador
abre `repository.revision(...)`, empilha mudanças e a saída do `with` comita
tudo — mudanças, vínculos de evidência e a outbox de efeitos — numa transação
só (§4.2). Uma exceção no meio derruba a revisão inteira, inclusive a linha da
própria revisão; não sobra estado meio gravado.

Os invariantes do plano são aplicados AQUI, em código, não em documentação:

| Invariante | Onde |
|---|---|
| `supported` exige `evidence_refs` | `_check_support` |
| LLM não declara a si própria `supported` | `_check_support` |
| `implemented` exige evidência executável | `_check_support` |
| aprovação não muda natureza | `approve_fact` / `_check_nature_transition` |
| reingestão idêntica não duplica | `put_entity` / `put_fact` / `put_relation` |
| consulta padrão só devolve `current` | `DEFAULT_LIFECYCLE` em toda consulta |
| ciclo de `supersedes` rejeitado | `_check_supersedes_cycle` |
| par de tipos inválido rejeitado | `relations.validate_pair` |

Histórico nunca é apagado: substituir é gravar nova revisão e mover
`head_revision_id`; as revisões anteriores continuam legíveis via
`fact_history` / `entity_history` / `relation_history`.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import evidence as ev_mod
from . import identity, relations as rel_mod
from .models import (
    DEFAULT_LIFECYCLE,
    Alias,
    ApprovalState,
    ContentKind,
    Effect,
    EffectStatus,
    Entity,
    EntityDraft,
    EntityType,
    EpistemicStatus,
    Evidence,
    Fact,
    FactDraft,
    FactNature,
    InvariantViolation,
    LifecycleStatus,
    MissingEvidence,
    NatureChangeRejected,
    OriginKind,
    Relation,
    RelationDraft,
    RelationType,
    Revision,
    SelfDeclaredSupport,
    Source,
    SourceKind,
    SourceVersion,
    SupersedesCycle,
    TargetKind,
    UnknownReference,
    WriteResult,
    origin_kind,
)
from .schema import apply_schema

#: Origens autorizadas a REGISTRAR sustentação (§5.3: o pipeline de verificação
#: atribui `supported`; a LLM que afirmou o fato não pode atribuí-lo a si).
SUPPORT_RECORDING_ORIGINS: frozenset[OriginKind] = frozenset(
    {OriginKind.PIPELINE, OriginKind.HUMAN}
)

ENTITY_COLUMNS = (
    "e.entity_id, e.namespace, e.entity_type, er.revision_id, e.stable_key, er.title, "
    "er.content_hash, er.source_version_id, er.lifecycle_status, er.valid_from, "
    "er.valid_to, er.recorded_at, er.attributes_json"
)

FACT_COLUMNS = (
    "f.fact_id, fr.revision_id, f.namespace, fr.subject_id, fr.predicate, fr.value, fr.scope, "
    "fr.nature, fr.epistemic_status, fr.lifecycle_status, fr.approval_state, fr.asserted_by, "
    "fr.support_recorded_by, fr.source_version_id, fr.content_hash, fr.valid_from, "
    "fr.valid_to, fr.recorded_at"
)


def utc_now() -> str:
    """Instante ISO-8601 UTC. Único relógio do módulo."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str, now: str | None = None) -> sqlite3.Connection:
    """Abre `knowledge.db` com WAL, `foreign_keys` ON e schema validado.

    `isolation_level=None` desliga o gerenciamento implícito de transação do
    driver: quem decide os limites é `revision()`, com `BEGIN IMMEDIATE`. Sem
    isso, o `sqlite3` abriria transação sozinho em pontos arbitrários e a
    atomicidade "revisão + outbox" deixaria de ser garantida.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    apply_schema(conn, now or utc_now())
    return conn


def _json(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, ensure_ascii=False, default=str)


def _validity(value: str | None, field_name: str) -> str | None:
    """Vigência: `None` = desconhecida. Nunca inferida (§5.2).

    String vazia é rejeitada de propósito — é o valor que costuma vazar de
    parser e virar "vigência conhecida" sem que ninguém tenha decidido isso.
    """
    if value is None:
        return None
    if not str(value).strip():
        raise InvariantViolation(
            f"{field_name} vazio: use None para vigência desconhecida; "
            "ausência não deve ser preenchida por inferência (§5.2)"
        )
    return str(value)


# --------------------------------------------------------------------------
# Revisão
# --------------------------------------------------------------------------


class RevisionBuilder:
    """Conjunto de mudanças de UMA revisão, dentro de UMA transação aberta."""

    def __init__(self, repo: "Repository", revision_id: str, now: str) -> None:
        self.repo = repo
        self.conn = repo.conn
        self.revision_id = revision_id
        self.now = now
        self.changes: list[WriteResult] = []
        self.effects: list[str] = []
        self._closed = False

    # ---------------------------------------------------------- evidências

    def add_evidence(self, ev: Evidence) -> str:
        """Grava a evidência (idempotente pelo id derivado) e devolve o id."""
        self._guard()
        row = self.conn.execute(
            "SELECT evidence_id FROM evidence WHERE evidence_id=?", (ev.evidence_id,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO evidence(evidence_id, namespace, source_kind, content_kind, "
                "source_version_id, locator_json, snippet_hash, recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    ev.evidence_id,
                    identity.normalize_namespace(ev.namespace),
                    ev.source_kind.value,
                    ev.content_kind.value,
                    ev.source_version_id,
                    _json(dict(ev.locator)),
                    ev.snippet_hash,
                    ev.recorded_at or self.now,
                ),
            )
        return ev.evidence_id

    def _link_evidence(self, kind: TargetKind, target_id: str, refs: Sequence[str]) -> None:
        for ref in refs:
            self.conn.execute(
                "INSERT OR IGNORE INTO evidence_links(evidence_id, target_kind, target_id, "
                "revision_id) VALUES (?,?,?,?)",
                (ref, kind.value, target_id, self.revision_id),
            )

    def _load_evidence(self, refs: Sequence[str]) -> list[Evidence]:
        out: list[Evidence] = []
        for ref in refs:
            row = self.conn.execute(
                "SELECT evidence_id, namespace, source_kind, content_kind, source_version_id, "
                "locator_json, snippet_hash, recorded_at FROM evidence WHERE evidence_id=?",
                (ref,),
            ).fetchone()
            if row is None:
                raise UnknownReference(f"evidence_refs aponta para evidência inexistente: {ref}")
            out.append(
                Evidence(
                    evidence_id=row[0],
                    namespace=row[1],
                    source_kind=SourceKind(row[2]),
                    content_kind=ContentKind(row[3]),
                    source_version_id=row[4],
                    locator=json.loads(row[5]),
                    snippet_hash=row[6],
                    recorded_at=row[7],
                )
            )
        return out

    # ------------------------------------------------------------ efeitos

    def enqueue_effect(self, effect_type: str, payload: Mapping[str, Any]) -> str:
        """Enfileira efeito na outbox, na MESMA transação da revisão (§4.2).

        `runtime.db` referencia este `effect_id` e só marca concluído após
        confirmação idempotente do destino; nada aqui tenta transação
        distribuída com filesystem ou SharePoint.
        """
        self._guard()
        effect_id = identity.new_effect_id()
        self.conn.execute(
            "INSERT INTO effects(effect_id, revision_id, effect_type, payload_json, status, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (
                effect_id,
                self.revision_id,
                effect_type,
                _json(dict(payload)),
                EffectStatus.PENDING.value,
                self.now,
            ),
        )
        self.effects.append(effect_id)
        return effect_id

    # ----------------------------------------------------------- entidades

    def put_entity(self, draft: EntityDraft) -> WriteResult:
        """Cria ou revisa uma entidade. Reingestão idêntica não gera revisão."""
        self._guard()
        ns = identity.normalize_namespace(draft.namespace)
        aliases = tuple(draft.aliases or ())
        for a in aliases:
            if not isinstance(a, Alias):
                raise InvariantViolation("aliases devem ser models.Alias (com origem, §5.2)")

        eid = draft.entity_id or identity.resolve_identity(
            self.conn, ns, draft.entity_type, draft.stable_key, [a.alias for a in aliases]
        ) or identity.entity_id(ns, draft.entity_type, draft.stable_key)
        identity.check_identity_consistency(self.conn, eid, ns, draft.entity_type)

        valid_from = _validity(draft.valid_from, "valid_from")
        valid_to = _validity(draft.valid_to, "valid_to")
        chash = draft.content_hash or identity.entity_content_hash(
            draft.title,
            draft.stable_key,
            draft.attributes,
            aliases,
            draft.source_version_id,
            draft.lifecycle_status.value,
            valid_from,
            valid_to,
        )

        head = self.conn.execute(
            "SELECT e.head_revision_id, er.content_hash FROM entities e "
            "JOIN entity_revisions er ON er.entity_id=e.entity_id "
            "  AND er.revision_id=e.head_revision_id WHERE e.entity_id=?",
            (eid,),
        ).fetchone()
        if head is not None and head[1] == chash:
            result = WriteResult(TargetKind.ENTITY, eid, head[0], False, "conteúdo idêntico")
            self.changes.append(result)
            return result

        if head is None:
            self.conn.execute(
                "INSERT INTO entities(entity_id, namespace, entity_type, stable_key, "
                "created_revision_id, head_revision_id) VALUES (?,?,?,?,?,?)",
                (eid, ns, draft.entity_type.value, draft.stable_key, self.revision_id, self.revision_id),
            )
        else:
            self.conn.execute(
                "UPDATE entities SET head_revision_id=? WHERE entity_id=?", (self.revision_id, eid)
            )
        self.conn.execute(
            "INSERT OR REPLACE INTO entity_revisions(entity_id, revision_id, title, content_hash, "
            "source_version_id, lifecycle_status, valid_from, valid_to, recorded_at, "
            "attributes_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                eid,
                self.revision_id,
                draft.title,
                chash,
                draft.source_version_id,
                draft.lifecycle_status.value,
                valid_from,
                valid_to,
                self.now,
                _json(dict(draft.attributes)),
            ),
        )
        for row in identity.alias_rows(eid, ns, aliases, self.revision_id, self.now):
            self.conn.execute(
                "INSERT OR REPLACE INTO entity_aliases(entity_id, alias, origin, namespace, "
                "source_version_id, revision_id, recorded_at) VALUES (?,?,?,?,?,?,?)",
                row,
            )
        self._link_evidence(TargetKind.ENTITY, eid, tuple(draft.evidence_refs or ()))
        result = WriteResult(TargetKind.ENTITY, eid, self.revision_id, True)
        self.changes.append(result)
        return result

    def set_entity_lifecycle(
        self, entity_id: str, status: LifecycleStatus, recorded_by: str
    ) -> WriteResult:
        """Move o ciclo de vida da entidade preservando identidade e título."""
        self._guard()
        prev = self.repo.get_entity(entity_id, lifecycle=None)
        if prev is None:
            raise UnknownReference(f"entity_id inexistente: {entity_id}")
        if prev.lifecycle_status is status:
            result = WriteResult(TargetKind.ENTITY, entity_id, prev.revision_id, False, "ciclo idêntico")
            self.changes.append(result)
            return result
        chash = identity.content_hash(
            {"base": prev.content_hash, "lifecycle": status.value, "by": recorded_by}
        )
        self.conn.execute(
            "UPDATE entities SET head_revision_id=? WHERE entity_id=?", (self.revision_id, entity_id)
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO entity_revisions(entity_id, revision_id, title, content_hash, "
            "source_version_id, lifecycle_status, valid_from, valid_to, recorded_at, "
            "attributes_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                entity_id,
                self.revision_id,
                prev.title,
                chash,
                prev.source_version_id,
                status.value,
                prev.valid_from,
                prev.valid_to,
                self.now,
                _json(dict(prev.attributes)),
            ),
        )
        result = WriteResult(TargetKind.ENTITY, entity_id, self.revision_id, True, f"lifecycle={status.value}")
        self.changes.append(result)
        return result

    # --------------------------------------------------------------- fatos

    def put_fact(self, draft: FactDraft) -> WriteResult:
        """Cria ou revisa um fato, aplicando os invariantes de §5.3/§5.4."""
        self._guard()
        ns = identity.normalize_namespace(draft.namespace)
        if not self.repo.entity_exists(draft.subject_id):
            raise UnknownReference(f"subject_id inexistente: {draft.subject_id}")

        refs = tuple(dict.fromkeys(draft.evidence_refs or ()))
        evidences = self._load_evidence(refs)
        _check_support(
            kind="fato",
            epistemic_status=draft.epistemic_status,
            refs=refs,
            asserted_by=draft.asserted_by,
            support_recorded_by=draft.support_recorded_by,
            nature=draft.nature,
            evidences=evidences,
        )

        fid = draft.fact_id or identity.fact_id(ns, draft.subject_id, draft.predicate, draft.scope)
        valid_from = _validity(draft.valid_from, "valid_from")
        valid_to = _validity(draft.valid_to, "valid_to")

        prev = self.repo.get_fact(fid, lifecycle=None)
        if prev is not None:
            _check_nature_transition(prev, draft, evidences)

        chash = identity.content_hash(
            {
                "subject": draft.subject_id,
                "predicate": draft.predicate,
                "value": draft.value,
                "scope": draft.scope,
                "nature": draft.nature.value,
                "epistemic": draft.epistemic_status.value,
                "lifecycle": draft.lifecycle_status.value,
                "approval": draft.approval_state.value,
                "asserted_by": draft.asserted_by,
                "support_recorded_by": draft.support_recorded_by,
                "source_version_id": draft.source_version_id,
                "evidence": sorted(refs),
                "valid_from": valid_from,
                "valid_to": valid_to,
            }
        )
        if prev is not None and prev.content_hash == chash:
            result = WriteResult(TargetKind.FACT, fid, prev.revision_id, False, "conteúdo idêntico")
            self.changes.append(result)
            return result

        if prev is None:
            self.conn.execute(
                "INSERT INTO facts(fact_id, namespace, subject_id, predicate, scope, "
                "created_revision_id, head_revision_id) VALUES (?,?,?,?,?,?,?)",
                (fid, ns, draft.subject_id, draft.predicate, draft.scope, self.revision_id, self.revision_id),
            )
        else:
            self.conn.execute(
                "UPDATE facts SET head_revision_id=? WHERE fact_id=?", (self.revision_id, fid)
            )
        self._insert_fact_revision(
            fid,
            subject_id=draft.subject_id,
            predicate=draft.predicate,
            value=draft.value,
            scope=draft.scope,
            nature=draft.nature,
            epistemic_status=draft.epistemic_status,
            lifecycle_status=draft.lifecycle_status,
            approval_state=draft.approval_state,
            asserted_by=draft.asserted_by,
            support_recorded_by=draft.support_recorded_by,
            source_version_id=draft.source_version_id,
            content_hash=chash,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self._link_evidence(TargetKind.FACT, fid, refs)
        result = WriteResult(TargetKind.FACT, fid, self.revision_id, True)
        self.changes.append(result)
        return result

    def _insert_fact_revision(self, fid: str, **f: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO fact_revisions(fact_id, revision_id, subject_id, predicate, "
            "value, scope, nature, epistemic_status, lifecycle_status, approval_state, "
            "asserted_by, support_recorded_by, source_version_id, content_hash, valid_from, "
            "valid_to, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fid,
                self.revision_id,
                f["subject_id"],
                f["predicate"],
                f["value"],
                f["scope"],
                f["nature"].value,
                f["epistemic_status"].value,
                f["lifecycle_status"].value,
                f["approval_state"].value,
                f["asserted_by"],
                f["support_recorded_by"],
                f["source_version_id"],
                f["content_hash"],
                f["valid_from"],
                f["valid_to"],
                self.now,
            ),
        )

    def approve_fact(
        self, fact_id: str, approved_by: str, state: ApprovalState = ApprovalState.APPROVED
    ) -> WriteResult:
        """Registra aprovação SEM tocar em natureza nem em ciclo de vida.

        Assinatura deliberadamente sem parâmetro de `nature`/`lifecycle`: o
        aceite W1 "requisito aprovado não aparece como implementação atual"
        depende de aprovação ser um eixo próprio. Uma proposta aprovada
        continua `declared_requirement` até haver evidência de implementação,
        que só entra por `put_fact` com evidência executável (§5.3).
        """
        self._guard()
        prev = self.repo.get_fact(fact_id, lifecycle=None)
        if prev is None:
            raise UnknownReference(f"fact_id inexistente: {fact_id}")
        if prev.approval_state is state:
            result = WriteResult(TargetKind.FACT, fact_id, prev.revision_id, False, "aprovação idêntica")
            self.changes.append(result)
            return result

        chash = identity.content_hash({"base": prev.content_hash, "approval": state.value, "by": approved_by})
        self.conn.execute("UPDATE facts SET head_revision_id=? WHERE fact_id=?", (self.revision_id, fact_id))
        self._insert_fact_revision(
            fact_id,
            subject_id=prev.subject_id,
            predicate=prev.predicate,
            value=prev.value,
            scope=prev.scope,
            nature=prev.nature,  # cópia literal: aprovação NÃO muda natureza
            epistemic_status=prev.epistemic_status,
            lifecycle_status=prev.lifecycle_status,  # nem vigência
            approval_state=state,
            asserted_by=prev.asserted_by,
            support_recorded_by=prev.support_recorded_by,
            source_version_id=prev.source_version_id,
            content_hash=chash,
            valid_from=prev.valid_from,
            valid_to=prev.valid_to,
        )
        self._link_evidence(TargetKind.FACT, fact_id, prev.evidence_refs)
        self.conn.execute(
            "INSERT OR IGNORE INTO effects(effect_id, revision_id, effect_type, payload_json, "
            "status, created_at) VALUES (?,?,?,?,?,?)",
            (
                identity.new_effect_id(),
                self.revision_id,
                "fact.approval_recorded",
                _json({"fact_id": fact_id, "state": state.value, "by": approved_by}),
                EffectStatus.PENDING.value,
                self.now,
            ),
        )
        result = WriteResult(TargetKind.FACT, fact_id, self.revision_id, True, f"approval={state.value}")
        self.changes.append(result)
        return result

    def set_fact_lifecycle(
        self,
        fact_id: str,
        status: LifecycleStatus,
        recorded_by: str,
        evidence_refs: Sequence[str] = (),
        reason: str = "",
    ) -> WriteResult:
        """Move o ciclo de vida de um fato, preservando natureza e sustentação.

        `PROPOSED -> CURRENT` exige evidência nova: §5.3 diz que proposta
        aprovada continua proposta até evidência de implementação.
        """
        self._guard()
        prev = self.repo.get_fact(fact_id, lifecycle=None)
        if prev is None:
            raise UnknownReference(f"fact_id inexistente: {fact_id}")
        if prev.lifecycle_status is status:
            result = WriteResult(TargetKind.FACT, fact_id, prev.revision_id, False, "ciclo idêntico")
            self.changes.append(result)
            return result
        refs = tuple(dict.fromkeys(tuple(prev.evidence_refs) + tuple(evidence_refs)))
        if prev.lifecycle_status is LifecycleStatus.PROPOSED and status is LifecycleStatus.CURRENT:
            if not evidence_refs:
                raise MissingEvidence(
                    f"fato {fact_id}: transição proposed->current exige evidência nova; "
                    "proposta aprovada continua proposta até evidência (§5.3)"
                )
            self._load_evidence(tuple(evidence_refs))
        chash = identity.content_hash(
            {"base": prev.content_hash, "lifecycle": status.value, "by": recorded_by, "reason": reason}
        )
        self.conn.execute("UPDATE facts SET head_revision_id=? WHERE fact_id=?", (self.revision_id, fact_id))
        self._insert_fact_revision(
            fact_id,
            subject_id=prev.subject_id,
            predicate=prev.predicate,
            value=prev.value,
            scope=prev.scope,
            nature=prev.nature,
            epistemic_status=prev.epistemic_status,
            lifecycle_status=status,
            approval_state=prev.approval_state,
            asserted_by=prev.asserted_by,
            support_recorded_by=prev.support_recorded_by,
            source_version_id=prev.source_version_id,
            content_hash=chash,
            valid_from=prev.valid_from,
            valid_to=prev.valid_to,
        )
        self._link_evidence(TargetKind.FACT, fact_id, refs)
        result = WriteResult(TargetKind.FACT, fact_id, self.revision_id, True, f"lifecycle={status.value}")
        self.changes.append(result)
        return result

    # ------------------------------------------------------------ relações

    def put_relation(self, draft: RelationDraft) -> WriteResult:
        """Cria ou revisa uma relação, validando par de tipos e ciclos (§5.5)."""
        self._guard()
        ns = identity.normalize_namespace(draft.namespace)
        source_type = self.repo.entity_type_of(draft.source_entity_id)
        target_type = self.repo.entity_type_of(draft.target_entity_id)
        if source_type is None:
            raise UnknownReference(f"source_entity_id inexistente: {draft.source_entity_id}")
        if target_type is None:
            raise UnknownReference(f"target_entity_id inexistente: {draft.target_entity_id}")
        rel_mod.validate_pair(draft.relation_type, source_type, target_type)

        refs = tuple(dict.fromkeys(draft.evidence_refs or ()))
        evidences = self._load_evidence(refs)
        _check_support(
            kind="relação",
            epistemic_status=draft.epistemic_status,
            refs=refs,
            asserted_by=draft.asserted_by,
            support_recorded_by=draft.support_recorded_by,
            nature=None,
            evidences=evidences,
        )
        if draft.relation_type is RelationType.SUPERSEDES:
            self._check_supersedes_cycle(draft)

        rid = draft.relation_id or identity.relation_id(
            ns,
            draft.source_entity_id,
            draft.relation_type.value,
            draft.target_entity_id,
            draft.scope,
        )
        valid_from = _validity(draft.valid_from, "valid_from")
        valid_to = _validity(draft.valid_to, "valid_to")
        chash = identity.content_hash(
            {
                "source": draft.source_entity_id,
                "type": draft.relation_type.value,
                "target": draft.target_entity_id,
                "scope": draft.scope,
                "epistemic": draft.epistemic_status.value,
                "lifecycle": draft.lifecycle_status.value,
                "asserted_by": draft.asserted_by,
                "support_recorded_by": draft.support_recorded_by,
                "source_version_id": draft.source_version_id,
                "evidence": sorted(refs),
                "attributes": dict(draft.attributes or {}),
                "valid_from": valid_from,
                "valid_to": valid_to,
            }
        )
        prev = self.repo.get_relation(rid, lifecycle=None)
        if prev is not None and prev.content_hash == chash:
            result = WriteResult(TargetKind.RELATION, rid, prev.revision_id, False, "conteúdo idêntico")
            self.changes.append(result)
            return result

        if prev is None:
            self.conn.execute(
                "INSERT INTO relations(relation_id, namespace, source_entity_id, relation_type, "
                "target_entity_id, scope, created_revision_id, head_revision_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    rid,
                    ns,
                    draft.source_entity_id,
                    draft.relation_type.value,
                    draft.target_entity_id,
                    draft.scope,
                    self.revision_id,
                    self.revision_id,
                ),
            )
        else:
            self.conn.execute(
                "UPDATE relations SET head_revision_id=? WHERE relation_id=?", (self.revision_id, rid)
            )
        self.conn.execute(
            "INSERT OR REPLACE INTO relation_revisions(relation_id, revision_id, source_entity_id, "
            "relation_type, target_entity_id, scope, epistemic_status, lifecycle_status, "
            "asserted_by, support_recorded_by, source_version_id, content_hash, valid_from, "
            "valid_to, recorded_at, attributes_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                self.revision_id,
                draft.source_entity_id,
                draft.relation_type.value,
                draft.target_entity_id,
                draft.scope,
                draft.epistemic_status.value,
                draft.lifecycle_status.value,
                draft.asserted_by,
                draft.support_recorded_by,
                draft.source_version_id,
                chash,
                valid_from,
                valid_to,
                self.now,
                _json(dict(draft.attributes or {})),
            ),
        )
        self._link_evidence(TargetKind.RELATION, rid, refs)
        result = WriteResult(TargetKind.RELATION, rid, self.revision_id, True)
        self.changes.append(result)
        return result

    def _check_supersedes_cycle(self, draft: RelationDraft) -> None:
        """Rejeita `A supersedes A` e qualquer ciclo já alcançável (§5.5)."""
        if draft.source_entity_id == draft.target_entity_id:
            raise SupersedesCycle(
                f"supersedes de {draft.source_entity_id} para si mesma: ciclo de substituição"
            )
        if rel_mod.supersedes_path_exists(
            self.conn, draft.target_entity_id, draft.source_entity_id
        ):
            raise SupersedesCycle(
                f"gravar supersedes {draft.source_entity_id} -> {draft.target_entity_id} fecharia "
                "ciclo de substituição de versões (§5.5); relação rejeitada"
            )

    def set_relation_lifecycle(
        self, relation_id: str, status: LifecycleStatus, recorded_by: str
    ) -> WriteResult:
        """Move o ciclo de vida de uma relação preservando o resto."""
        self._guard()
        prev = self.repo.get_relation(relation_id, lifecycle=None)
        if prev is None:
            raise UnknownReference(f"relation_id inexistente: {relation_id}")
        if prev.lifecycle_status is status:
            result = WriteResult(TargetKind.RELATION, relation_id, prev.revision_id, False, "ciclo idêntico")
            self.changes.append(result)
            return result
        chash = identity.content_hash(
            {"base": prev.content_hash, "lifecycle": status.value, "by": recorded_by}
        )
        self.conn.execute(
            "UPDATE relations SET head_revision_id=? WHERE relation_id=?", (self.revision_id, relation_id)
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO relation_revisions(relation_id, revision_id, source_entity_id, "
            "relation_type, target_entity_id, scope, epistemic_status, lifecycle_status, "
            "asserted_by, support_recorded_by, source_version_id, content_hash, valid_from, "
            "valid_to, recorded_at, attributes_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                relation_id,
                self.revision_id,
                prev.source_entity_id,
                prev.relation_type.value,
                prev.target_entity_id,
                prev.scope,
                prev.epistemic_status.value,
                status.value,
                prev.asserted_by,
                prev.support_recorded_by,
                prev.source_version_id,
                chash,
                prev.valid_from,
                prev.valid_to,
                self.now,
                "{}",
            ),
        )
        self._link_evidence(TargetKind.RELATION, relation_id, prev.evidence_refs)
        result = WriteResult(TargetKind.RELATION, relation_id, self.revision_id, True, f"lifecycle={status.value}")
        self.changes.append(result)
        return result

    # ------------------------------------------------------------ interno

    def _guard(self) -> None:
        if self._closed:
            raise InvariantViolation("revisão já encerrada: abra uma nova com repository.revision()")

    def close(self) -> None:
        self._closed = True

    @property
    def change_count(self) -> int:
        return sum(1 for c in self.changes if c.changed)


# --------------------------------------------------------------------------
# Invariantes compartilhados
# --------------------------------------------------------------------------


def _check_support(
    kind: str,
    epistemic_status: EpistemicStatus,
    refs: Sequence[str],
    asserted_by: str,
    support_recorded_by: str | None,
    nature: FactNature | None,
    evidences: Sequence[Evidence],
) -> None:
    """Invariantes de sustentação (§5.3, §5.4). Aplicado a fatos E relações.

    Três rejeições distintas, para que o erro diga qual regra caiu:
    evidência ausente, sustentação auto-declarada e evidência que não sustenta
    comportamento implementado.
    """
    if epistemic_status is not EpistemicStatus.SUPPORTED:
        # Só `supported` é a afirmação forte; inferido/disputado/não resolvido
        # podem existir sem citação, desde que o estado diga isso.
        return
    if not refs:
        raise MissingEvidence(
            f"{kind} com epistemic_status=supported sem evidence_refs; "
            "sustentação exige evidência primária versionada (§5.3)"
        )
    recorder = (support_recorded_by or "").strip()
    if not recorder:
        raise SelfDeclaredSupport(
            f"{kind} 'supported' sem support_recorded_by; o estado é atribuído pelo pipeline "
            "de verificação, com registro do suporte (§5.3)"
        )
    recorder_kind = origin_kind(recorder)
    if recorder_kind not in SUPPORT_RECORDING_ORIGINS:
        raise SelfDeclaredSupport(
            f"support_recorded_by={recorder!r} não é origem autorizada a registrar sustentação; "
            f"use prefixo {' ou '.join(sorted(o.value for o in SUPPORT_RECORDING_ORIGINS))} "
            "(saída de LLM não declara a si própria supported, §5.3)"
        )
    if recorder == asserted_by.strip():
        raise SelfDeclaredSupport(
            f"{kind}: support_recorded_by igual a asserted_by ({recorder!r}); "
            "quem afirma não registra a própria sustentação (§5.3)"
        )
    if nature is FactNature.IMPLEMENTED:
        ev_mod.assert_supports_implemented(evidences)


def _check_nature_transition(prev: Fact, draft: FactDraft, evidences: Sequence[Evidence]) -> None:
    """Rejeita promover proposta/requisito a `implemented` sem evidência (§5.3).

    A porta é dupla: registrar quem decidiu a mudança de natureza E ter
    evidência executável no mesmo movimento. Aprovação não passa por aqui —
    `approve_fact` copia a natureza anterior literalmente.
    """
    if prev.nature is draft.nature:
        return
    recorded_by = (draft.nature_change_recorded_by or "").strip()
    if not recorded_by:
        raise NatureChangeRejected(
            f"fato {prev.fact_id}: mudança de natureza {prev.nature.value} -> "
            f"{draft.nature.value} exige nature_change_recorded_by explícito (§5.3)"
        )
    if origin_kind(recorded_by) not in SUPPORT_RECORDING_ORIGINS:
        raise NatureChangeRejected(
            f"nature_change_recorded_by={recorded_by!r} não é origem autorizada; "
            "LLM não promove a própria proposta a implementação (§5.3)"
        )
    if draft.nature is FactNature.IMPLEMENTED:
        if not evidences:
            raise NatureChangeRejected(
                f"fato {prev.fact_id}: {prev.nature.value} -> implemented sem evidência; "
                "proposta aprovada continua proposta até evidência de implementação (§5.3)"
            )
        ev_mod.assert_supports_implemented(evidences)


# --------------------------------------------------------------------------
# Repositório
# --------------------------------------------------------------------------


class Repository:
    """Fachada de leitura e escrita de `knowledge.db`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: str) -> "Repository":
        return cls(connect(path))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ----------------------------------------------------------- transação

    @contextlib.contextmanager
    def revision(
        self, author: str, reason: str = "", parent_revision_id: str | None = None
    ) -> Iterator[RevisionBuilder]:
        """Transação de revisão: mudanças + outbox comitadas juntas (§4.2).

        `BEGIN IMMEDIATE` toma o lock de escrita já na abertura: duas revisões
        concorrentes falham cedo e explicitamente, em vez de descobrirem o
        conflito no commit com metade do trabalho feito.
        """
        now = utc_now()
        rid = identity.new_revision_id()
        self.conn.execute("BEGIN IMMEDIATE")
        builder = RevisionBuilder(self, rid, now)
        try:
            self.conn.execute(
                "INSERT INTO revisions(revision_id, created_at, author, reason, "
                "parent_revision_id, change_count) VALUES (?,?,?,?,?,0)",
                (rid, now, author, reason, parent_revision_id),
            )
            yield builder
            self.conn.execute(
                "UPDATE revisions SET change_count=? WHERE revision_id=?",
                (builder.change_count, rid),
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            builder.close()

    # -------------------------------------------------------------- fontes

    def register_source(self, namespace: str, source_kind: SourceKind, uri: str) -> Source:
        """Registra fonte (idempotente por `(namespace, uri)`)."""
        ns = identity.normalize_namespace(namespace)
        sid = identity.source_id(ns, uri)
        now = utc_now()
        self.conn.execute(
            "INSERT OR IGNORE INTO sources(source_id, namespace, source_kind, uri, recorded_at) "
            "VALUES (?,?,?,?,?)",
            (sid, ns, source_kind.value, uri, now),
        )
        return Source(sid, ns, source_kind, uri, now)

    def register_source_version(
        self,
        source: Source | str,
        version_label: str,
        content_hash: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceVersion:
        """Registra versão imutável de uma fonte (idempotente pelo id derivado)."""
        sid = source.source_id if isinstance(source, Source) else source
        svid = identity.source_version_id(sid, version_label, content_hash)
        now = utc_now()
        self.conn.execute(
            "INSERT OR IGNORE INTO source_versions(source_version_id, source_id, version_label, "
            "content_hash, captured_at, metadata_json) VALUES (?,?,?,?,?,?)",
            (svid, sid, version_label, content_hash, now, _json(dict(metadata or {}))),
        )
        return SourceVersion(svid, sid, version_label, content_hash, now, dict(metadata or {}))

    # ------------------------------------------------------------ consultas

    def entity_exists(self, entity_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM entities WHERE entity_id=?", (entity_id,)
            ).fetchone()
            is not None
        )

    def entity_type_of(self, entity_id: str) -> EntityType | None:
        row = self.conn.execute(
            "SELECT entity_type FROM entities WHERE entity_id=?", (entity_id,)
        ).fetchone()
        return EntityType(row[0]) if row else None

    def get_entity(
        self, entity_id: str, lifecycle: Sequence[LifecycleStatus] | None = DEFAULT_LIFECYCLE
    ) -> Entity | None:
        """Entidade na revisão de cabeça.

        `lifecycle=None` desliga o filtro — é a "flag explícita" que o plano
        exige para ver histórico/substituído.
        """
        sql = (
            f"SELECT {ENTITY_COLUMNS} FROM entities e "
            "JOIN entity_revisions er ON er.entity_id=e.entity_id "
            "  AND er.revision_id=e.head_revision_id WHERE e.entity_id=?"
        )
        params: list[Any] = [entity_id]
        sql, params = _apply_lifecycle(sql, params, "er", lifecycle)
        row = self.conn.execute(sql, params).fetchone()
        return _row_to_entity(row) if row else None

    def find_entities(
        self,
        namespace: str,
        entity_type: EntityType | None = None,
        lifecycle: Sequence[LifecycleStatus] | None = DEFAULT_LIFECYCLE,
    ) -> list[Entity]:
        sql = (
            f"SELECT {ENTITY_COLUMNS} FROM entities e "
            "JOIN entity_revisions er ON er.entity_id=e.entity_id "
            "  AND er.revision_id=e.head_revision_id WHERE e.namespace=?"
        )
        params: list[Any] = [identity.normalize_namespace(namespace)]
        if entity_type is not None:
            sql += " AND e.entity_type=?"
            params.append(entity_type.value)
        sql, params = _apply_lifecycle(sql, params, "er", lifecycle)
        return [_row_to_entity(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_fact(
        self, fact_id: str, lifecycle: Sequence[LifecycleStatus] | None = DEFAULT_LIFECYCLE
    ) -> Fact | None:
        sql = (
            f"SELECT {FACT_COLUMNS} FROM facts f "
            "JOIN fact_revisions fr ON fr.fact_id=f.fact_id AND fr.revision_id=f.head_revision_id "
            "WHERE f.fact_id=?"
        )
        params: list[Any] = [fact_id]
        sql, params = _apply_lifecycle(sql, params, "fr", lifecycle)
        row = self.conn.execute(sql, params).fetchone()
        return self._row_to_fact(row) if row else None

    def facts_for_subject(
        self,
        subject_id: str,
        predicate: str | None = None,
        nature: FactNature | None = None,
        lifecycle: Sequence[LifecycleStatus] | None = DEFAULT_LIFECYCLE,
        epistemic_status: EpistemicStatus | None = None,
    ) -> list[Fact]:
        """Fatos vigentes de um sujeito. Sem `lifecycle` explícito, só `current`."""
        sql = (
            f"SELECT {FACT_COLUMNS} FROM facts f "
            "JOIN fact_revisions fr ON fr.fact_id=f.fact_id AND fr.revision_id=f.head_revision_id "
            "WHERE fr.subject_id=?"
        )
        params: list[Any] = [subject_id]
        if predicate is not None:
            sql += " AND fr.predicate=?"
            params.append(predicate)
        if nature is not None:
            sql += " AND fr.nature=?"
            params.append(nature.value)
        if epistemic_status is not None:
            sql += " AND fr.epistemic_status=?"
            params.append(epistemic_status.value)
        sql, params = _apply_lifecycle(sql, params, "fr", lifecycle)
        sql += " ORDER BY fr.predicate"
        return [self._row_to_fact(r) for r in self.conn.execute(sql, params).fetchall()]

    def implemented_facts(self, subject_id: str) -> list[Fact]:
        """Comportamento atualmente IMPLEMENTADO do sujeito.

        Filtro duplo — `nature=implemented` E `lifecycle=current` — é o que
        mantém requisito aprovado fora da resposta de "o que o sistema faz".
        """
        return self.facts_for_subject(
            subject_id, nature=FactNature.IMPLEMENTED, lifecycle=(LifecycleStatus.CURRENT,)
        )

    def get_relation(
        self, relation_id: str, lifecycle: Sequence[LifecycleStatus] | None = DEFAULT_LIFECYCLE
    ) -> Relation | None:
        sql = (
            f"SELECT {rel_mod.RELATION_COLUMNS} {rel_mod.RELATION_HEAD_FROM} "
            "WHERE rr.relation_id=?"
        )
        params: list[Any] = [relation_id]
        sql, params = _apply_lifecycle(sql, params, "rr", lifecycle)
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return rel_mod.row_to_relation(row, rel_mod.evidence_refs_for(self.conn, row[0], row[1]))

    def neighbors(
        self,
        entity_id: str,
        direction: str = "out",
        relation_types: Iterable[RelationType] | None = None,
        lifecycle: Sequence[LifecycleStatus] = DEFAULT_LIFECYCLE,
    ) -> list[Relation]:
        return rel_mod.neighbors(self.conn, entity_id, direction, relation_types, lifecycle)

    def evidence_refs_of_fact(self, fact_id: str, revision_id: str) -> tuple[str, ...]:
        rows = self.conn.execute(
            "SELECT evidence_id FROM evidence_links WHERE target_kind='fact' AND target_id=? "
            "AND revision_id=? ORDER BY evidence_id",
            (fact_id, revision_id),
        ).fetchall()
        return tuple(r[0] for r in rows)

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        row = self.conn.execute(
            "SELECT evidence_id, namespace, source_kind, content_kind, source_version_id, "
            "locator_json, snippet_hash, recorded_at FROM evidence WHERE evidence_id=?",
            (evidence_id,),
        ).fetchone()
        if row is None:
            return None
        return Evidence(
            evidence_id=row[0],
            namespace=row[1],
            source_kind=SourceKind(row[2]),
            content_kind=ContentKind(row[3]),
            source_version_id=row[4],
            locator=json.loads(row[5]),
            snippet_hash=row[6],
            recorded_at=row[7],
        )

    # -------------------------------------------------------------- histórico

    def fact_history(self, fact_id: str) -> list[Fact]:
        """Todas as revisões do fato, da mais antiga para a mais nova.

        Existe justamente porque nada é apagado: `superseded`/`historical`
        continuam consultáveis, mas só por esta porta explícita.
        """
        rows = self.conn.execute(
            f"SELECT {FACT_COLUMNS} FROM facts f "
            "JOIN fact_revisions fr ON fr.fact_id=f.fact_id "
            "JOIN revisions rv ON rv.revision_id=fr.revision_id "
            "WHERE f.fact_id=? ORDER BY rv.created_at, rv.revision_id",
            (fact_id,),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def entity_history(self, entity_id: str) -> list[Entity]:
        rows = self.conn.execute(
            f"SELECT {ENTITY_COLUMNS} FROM entities e "
            "JOIN entity_revisions er ON er.entity_id=e.entity_id "
            "JOIN revisions rv ON rv.revision_id=er.revision_id "
            "WHERE e.entity_id=? ORDER BY rv.created_at, rv.revision_id",
            (entity_id,),
        ).fetchall()
        return [_row_to_entity(r) for r in rows]

    def relation_history(self, relation_id: str) -> list[Relation]:
        rows = self.conn.execute(
            f"SELECT {rel_mod.RELATION_COLUMNS} FROM relation_revisions rr "
            "JOIN relations r ON r.relation_id = rr.relation_id "
            "JOIN revisions rv ON rv.revision_id = rr.revision_id "
            "WHERE rr.relation_id=? ORDER BY rv.created_at, rv.revision_id",
            (relation_id,),
        ).fetchall()
        return [rel_mod.row_to_relation(r) for r in rows]

    def get_revision(self, revision_id: str) -> Revision | None:
        row = self.conn.execute(
            "SELECT revision_id, created_at, author, reason, parent_revision_id, change_count "
            "FROM revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            return None
        return Revision(row[0], row[1], row[2], row[3], row[4], row[5])

    def pending_effects(self) -> list[Effect]:
        """Efeitos da outbox ainda não confirmados pelo destino (§4.2)."""
        rows = self.conn.execute(
            "SELECT effect_id, revision_id, effect_type, payload_json, status, created_at "
            "FROM effects WHERE status=? ORDER BY created_at, effect_id",
            (EffectStatus.PENDING.value,),
        ).fetchall()
        return [
            Effect(r[0], r[1], r[2], json.loads(r[3]), EffectStatus(r[4]), r[5]) for r in rows
        ]

    def _row_to_fact(self, row: Sequence[Any]) -> Fact:
        return Fact(
            fact_id=row[0],
            revision_id=row[1],
            namespace=row[2],
            subject_id=row[3],
            predicate=row[4],
            value=row[5],
            scope=row[6],
            nature=FactNature(row[7]),
            epistemic_status=EpistemicStatus(row[8]),
            lifecycle_status=LifecycleStatus(row[9]),
            approval_state=ApprovalState(row[10]),
            asserted_by=row[11],
            support_recorded_by=row[12],
            source_version_id=row[13],
            evidence_refs=self.evidence_refs_of_fact(row[0], row[1]),
            content_hash=row[14],
            valid_from=row[15],
            valid_to=row[16],
            recorded_at=row[17],
        )


def _apply_lifecycle(
    sql: str, params: list[Any], alias: str, lifecycle: Sequence[LifecycleStatus] | None
) -> tuple[str, list[Any]]:
    """Aplica o filtro de ciclo de vida. `None` = sem filtro (flag explícita)."""
    if lifecycle is None:
        return sql, params
    values = [s.value for s in lifecycle]
    if not values:
        return sql, params
    sql += f" AND {alias}.lifecycle_status IN ({','.join('?' for _ in values)})"
    params.extend(values)
    return sql, params


def _row_to_entity(row: Sequence[Any]) -> Entity:
    return Entity(
        entity_id=row[0],
        namespace=row[1],
        entity_type=EntityType(row[2]),
        revision_id=row[3],
        stable_key=row[4],
        title=row[5],
        content_hash=row[6],
        source_version_id=row[7],
        lifecycle_status=LifecycleStatus(row[8]),
        valid_from=row[9],
        valid_to=row[10],
        recorded_at=row[11],
        attributes=json.loads(row[12]),
    )
