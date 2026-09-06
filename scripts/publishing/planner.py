"""Planejamento das unidades documentais (plano §10.2).

Decide QUAIS documentos existem e o que entra em cada um, a partir do grafo de
uma revisão. É aqui que as duas proibições do plano viram código:

- **Sem documento por classe, chunk ou etapa operacional**: só quatro tipos de
  entidade ancoram documento (System, Capability, Contract, Initiative) e a
  entidade de evolução precisa ser alvo de `proposes_change_to`. Component,
  DataEntity, Source, Flow solto e qualquer artefato de pipeline entram como
  CONTEÚDO de um documento de assunto, nunca como documento próprio
  (`document.ALLOWED_ANCHORS` + `assert_single_system`).
- **Sem monolito**: `assert_single_system` rejeita documento que misture
  unidades de mais de um `System`; a visão do sistema é a única exceção, e
  mesmo ela é ancorada em UM sistema.

Documento vazio não entra no plano: vai para `skipped` com motivo legível, do
mesmo jeito que a unidade sem conteúdo útil (§10.2).

O plano é determinístico: toda coleção é ordenada por chave estável (tipo,
`stable_key`, id), nunca por ordem de chegada do banco. Duas execuções sobre a
mesma revisão produzem o mesmo plano — pré-requisito para o Markdown byte a
byte igual e para a comparação do manifesto (§10.5).

Só stdlib + `knowledge`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from knowledge.models import (
    Entity,
    EntityType,
    LifecycleStatus,
    RelationType,
)

from .document import (
    ALL_LIFECYCLE,
    Belonging,
    DocKind,
    InvalidDocumentGrouping,
    KnowledgeDocument,
    PublishingError,
    RevisionScope,
    SemanticUnit,
    context_unit,
    document_id,
    link_cross_state,
    resolve_belonging,
    units_for_entity,
)

#: Consumidores previstos em §9.1/§11.3. Fazem parte do plano como metadado
#: declarado: o manifesto registra para quem a revisão foi preparada.
DEFAULT_CONSUMERS: tuple[str, ...] = ("perguntas", "inception", "historias", "refinamento")

#: Tamanho operacional (§10.2). Não corta conteúdo — conteúdo cortado é
#: exatamente o que D14 proíbe; registra aviso para revisão de granularidade.
MAX_UNITS_PER_DOCUMENT = 40

#: Ordem de apresentação das entidades dentro de um documento de iniciativa —
#: é a cadeia inception → decisão → requisito → refinamento → impacto (D16).
INITIATIVE_MEMBER_ORDER: tuple[EntityType, ...] = (
    EntityType.DECISION,
    EntityType.REQUIREMENT,
    EntityType.REFINEMENT,
    EntityType.STORY,
    EntityType.DEFECT,
)

#: Tipos que compõem o documento de uma capacidade (§10.2): regras, fluxos,
#: falhas e contratos relacionados vivem COM a capacidade, não em documento
#: próprio por classe.
CAPABILITY_MEMBER_TYPES: frozenset[EntityType] = frozenset(
    {EntityType.BUSINESS_RULE, EntityType.FLOW, EntityType.CONTRACT, EntityType.DATA_ENTITY}
)

#: Relações que ligam a capacidade aos seus membros.
_CAPABILITY_EDGES: tuple[RelationType, ...] = (
    RelationType.CONTAINS,
    RelationType.IMPLEMENTS,
    RelationType.CALLS,
    RelationType.READS,
    RelationType.WRITES,
    RelationType.PUBLISHES,
    RelationType.CONSUMES,
    RelationType.DEPENDS_ON,
    RelationType.BELONGS_TO,
)

#: Relações que ligam a iniciativa aos seus artefatos.
_INITIATIVE_EDGES: tuple[RelationType, ...] = (
    RelationType.CONTAINS,
    RelationType.BELONGS_TO,
    RelationType.RECORDS,
    RelationType.REFINES,
    RelationType.DERIVED_FROM,
)


@dataclass(frozen=True)
class SkippedItem:
    """Algo que NÃO foi publicado, com motivo legível (§10.2)."""

    target_id: str
    kind: str  # "unit" | "document" | "fact" | "relation" | "entity"
    title: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "target_id": self.target_id,
            "kind": self.kind,
            "title": self.title,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PublicationPlan:
    """Conjunto completo preparado para UMA revisão (§10.6).

    `catalog_units` são as unidades de mini-contexto compartilhadas: aparecem
    repetidas dentro dos documentos, com o MESMO `unit_id`, e são listadas uma
    vez aqui — é o catálogo que evita duplicação extensa sem quebrar a leitura
    isolada.
    """

    revision_id: str
    namespace: str
    documents: tuple[KnowledgeDocument, ...]
    catalog_units: tuple[SemanticUnit, ...] = ()
    skipped: tuple[SkippedItem, ...] = ()
    consumers: tuple[str, ...] = DEFAULT_CONSUMERS
    warnings: tuple[str, ...] = ()

    def unit_index(self) -> dict[str, SemanticUnit]:
        """`unit_id` → unidade (a mesma unidade compartilhada aparece uma vez)."""
        out: dict[str, SemanticUnit] = {}
        for doc in self.documents:
            for unit in doc.units:
                out.setdefault(unit.unit_id, unit)
        for unit in self.catalog_units:
            out.setdefault(unit.unit_id, unit)
        return out

    def documents_of_unit(self) -> dict[str, tuple[str, ...]]:
        out: dict[str, list[str]] = {}
        for doc in self.documents:
            for unit in doc.units:
                out.setdefault(unit.unit_id, []).append(doc.document_id)
        return {k: tuple(v) for k, v in out.items()}

    def fact_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for doc in self.documents:
            seen.extend(doc.fact_ids())
        return tuple(dict.fromkeys(seen))

    def summary(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "namespace": self.namespace,
            "documents": len(self.documents),
            "units": sum(len(d.units) for d in self.documents),
            "catalog_units": len(self.catalog_units),
            "facts": len(self.fact_ids()),
            "skipped": len(self.skipped),
            "warnings": len(self.warnings),
            "by_kind": {
                k.value: sum(1 for d in self.documents if d.doc_kind is k) for k in DocKind
            },
        }


# --------------------------------------------------------------------------
# API principal
# --------------------------------------------------------------------------


def plan(
    repo: Any,
    revision_id: str,
    namespace: str | None = None,
    consumers: Sequence[str] = DEFAULT_CONSUMERS,
) -> PublicationPlan:
    """Planeja a publicação de uma revisão (§10.2), de forma determinística.

    `namespace=None` planeja todos os namespaces presentes em `knowledge.db`;
    namespaces continuam isolados (§5.2), cada entidade só encontra vizinhos
    do próprio namespace porque a travessia parte das entidades dele.
    """
    scope = RevisionScope(repo, revision_id)
    namespaces = [namespace] if namespace else _all_namespaces(repo)
    documents: list[KnowledgeDocument] = []
    catalog: dict[str, SemanticUnit] = {}
    skipped: list[SkippedItem] = []
    warnings: list[str] = []

    for ns in namespaces:
        ctx = _PlanContext(repo=repo, namespace=ns, scope=scope, skipped=skipped)
        docs = (
            _plan_systems(ctx)
            + _plan_capabilities(ctx)
            + _plan_contracts(ctx)
            + _plan_initiatives(ctx)
            + _plan_evolutions(ctx)
        )
        for doc in docs:
            documents.append(doc)
        catalog.update(ctx.catalog)

    kept: list[KnowledgeDocument] = []
    for doc in sorted(documents, key=_document_sort_key):
        if not doc.is_publishable():
            skipped.append(
                SkippedItem(
                    target_id=doc.document_id,
                    kind="document",
                    title=doc.title,
                    reason=(
                        "documento sem unidade publicável além do mini-contexto; "
                        "§10.2: documento sem informação útil não é publicado"
                    ),
                )
            )
            continue
        try:
            assert_single_system(doc)
        except InvalidDocumentGrouping as exc:
            # A proibição é estrutural, mas não pode derrubar a publicação
            # inteira: o documento monolítico fica de fora COM motivo, e o
            # restante da revisão continua íntegro (§10.6).
            skipped.append(
                SkippedItem(doc.document_id, "document", doc.title, str(exc))
            )
            continue
        if len(doc.units) > MAX_UNITS_PER_DOCUMENT:
            warnings.append(
                f"documento {doc.document_id} ({doc.title!r}) tem {len(doc.units)} unidades, "
                f"acima do tamanho operacional {MAX_UNITS_PER_DOCUMENT} (§10.2): rever granularidade"
            )
        kept.append(doc)

    # Catálogo compartilhado (§10.2): mini-contextos efetivamente usados MAIS
    # qualquer unidade que apareça em mais de um documento. Repetição só é
    # "controlada" quando é nominal: mesmo `unit_id`, mesmo conteúdo, mesma
    # revisão, listado uma vez aqui — é o que impede duas cópias divergentes
    # da mesma unidade competindo na busca (§10.6).
    occurrences: dict[str, int] = {}
    instances: dict[str, SemanticUnit] = {}
    for doc in kept:
        for unit in doc.units:
            occurrences[unit.unit_id] = occurrences.get(unit.unit_id, 0) + 1
            instances.setdefault(unit.unit_id, unit)
    shared_ids = {
        uid for uid, count in occurrences.items() if count > 1 or uid in catalog
    }
    catalog_units = tuple(
        sorted((instances[uid] for uid in shared_ids if uid in instances), key=lambda u: u.unit_id)
    )
    return PublicationPlan(
        revision_id=revision_id,
        namespace=namespace or ",".join(namespaces),
        documents=tuple(kept),
        catalog_units=catalog_units,
        skipped=tuple(skipped),
        consumers=tuple(consumers),
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------
# Contexto de planejamento
# --------------------------------------------------------------------------


@dataclass
class _PlanContext:
    repo: Any
    namespace: str
    scope: RevisionScope
    skipped: list[SkippedItem]
    catalog: dict[str, SemanticUnit] = field(default_factory=dict)
    _belonging: dict[str, Belonging] = field(default_factory=dict)

    def entities(self, entity_type: EntityType) -> list[Entity]:
        found = [
            e
            for e in self.repo.find_entities(self.namespace, entity_type, lifecycle=None)
            if self.scope.contains(e.revision_id)
        ]
        return sorted(found, key=lambda e: (e.stable_key, e.entity_id))

    def belonging(self, entity: Entity) -> Belonging:
        hit = self._belonging.get(entity.entity_id)
        if hit is None:
            hit = resolve_belonging(self.repo, entity, self.scope)
            self._belonging[entity.entity_id] = hit
        return hit

    def skip(self, target_id: str, kind: str, title: str, reason: str) -> None:
        self.skipped.append(SkippedItem(target_id, kind, title, reason))

    def context_for(self, entity: Entity) -> SemanticUnit | None:
        """Mini-contexto da entidade, memoizado — mesmo `unit_id` em todo doc."""
        unit = context_unit(self.repo, entity, self.scope, self.belonging(entity))
        if unit is None:
            return None
        self.catalog.setdefault(unit.unit_id, unit)
        return self.catalog[unit.unit_id]


# --------------------------------------------------------------------------
# Montagem por tipo de documento
# --------------------------------------------------------------------------


def _plan_systems(ctx: _PlanContext) -> list[KnowledgeDocument]:
    """Um documento de visão por `System` (§10.2)."""
    out: list[KnowledgeDocument] = []
    for system in ctx.entities(EntityType.SYSTEM):
        members = _members_of_system(ctx, system)
        out.extend(
            _assemble(
                ctx,
                doc_kind=DocKind.VISAO_SISTEMA,
                anchor=system,
                members=members,
                title=f"{system.title}: visão do sistema, capacidades e fronteiras",
            )
        )
    return out


def _plan_capabilities(ctx: _PlanContext) -> list[KnowledgeDocument]:
    """Um documento por `Capability`, com regras/fluxos/contratos relacionados."""
    out: list[KnowledgeDocument] = []
    for cap in ctx.entities(EntityType.CAPABILITY):
        members = _neighbors_of_type(
            ctx, cap, CAPABILITY_MEMBER_TYPES, _CAPABILITY_EDGES
        )
        out.extend(
            _assemble(
                ctx,
                doc_kind=DocKind.CAPACIDADE,
                anchor=cap,
                members=members,
                title=f"{cap.title}: regras, fluxo, falhas e contratos",
            )
        )
    return out


def _plan_contracts(ctx: _PlanContext) -> list[KnowledgeDocument]:
    """Um documento por `Contract` RELEVANTE (§10.2).

    Relevância é verificada, não presumida: um contrato sem fato e sem ponta
    consumidora/provedora não descreve interface nenhuma — publicá-lo cria uma
    página que compete na busca sem responder nada.
    """
    out: list[KnowledgeDocument] = []
    for contract in ctx.entities(EntityType.CONTRACT):
        peers = _neighbors_of_type(
            ctx,
            contract,
            frozenset({EntityType.CAPABILITY, EntityType.COMPONENT, EntityType.SYSTEM}),
            _CAPABILITY_EDGES,
        )
        facts = [
            f
            for f in ctx.repo.facts_for_subject(contract.entity_id, lifecycle=None)
            if ctx.scope.contains(f.revision_id)
        ]
        if not peers and not facts:
            ctx.skip(
                contract.entity_id,
                "entity",
                contract.title,
                "contrato sem consumidor/provedor e sem fato na revisão: não é dependência "
                "relevante (§10.2)",
            )
            continue
        out.extend(
            _assemble(
                ctx,
                doc_kind=DocKind.CONTRATO_DEPENDENCIA,
                anchor=contract,
                members=(),
                title=f"{contract.title}: interface, condições e falhas",
            )
        )
    return out


def _plan_initiatives(ctx: _PlanContext) -> list[KnowledgeDocument]:
    """Um documento por `Initiative`, com a cadeia inception → impacto (D16)."""
    out: list[KnowledgeDocument] = []
    for ini in ctx.entities(EntityType.INITIATIVE):
        members = _neighbors_of_type(
            ctx, ini, frozenset(INITIATIVE_MEMBER_ORDER), _INITIATIVE_EDGES
        )
        members = sorted(
            members,
            key=lambda e: (INITIATIVE_MEMBER_ORDER.index(e.entity_type), e.stable_key, e.entity_id),
        )
        # Impacto: o que a iniciativa propõe mudar entra como membro, para que
        # a cadeia decisão → refinamento → alvo fique NO MESMO documento (D16).
        impacted = _impacted_by(ctx, [ini] + members)
        out.extend(
            _assemble(
                ctx,
                doc_kind=DocKind.INICIATIVA,
                anchor=ini,
                members=tuple(members) + tuple(impacted),
                title=f"{ini.title}: decisões, refinamentos e impactos",
                impacted_ids={e.entity_id for e in impacted},
            )
        )
    return out


def _plan_evolutions(ctx: _PlanContext) -> list[KnowledgeDocument]:
    """Documento de evolução onde há proposta referenciando implementado (§10.2).

    Só nasce quando existe `proposes_change_to` apontando para uma entidade que
    tem comportamento em vigor: é o par "estado atual × proposta" exigido pela
    unidade documental Evolução, e o lugar onde D10 é mais visível.
    """
    out: list[KnowledgeDocument] = []
    for etype in (
        EntityType.BUSINESS_RULE,
        EntityType.CAPABILITY,
        EntityType.FLOW,
        EntityType.CONTRACT,
    ):
        for entity in ctx.entities(etype):
            proposers = [
                r
                for r in ctx.repo.neighbors(
                    entity.entity_id, direction="in", lifecycle=ALL_LIFECYCLE
                )
                if r.relation_type is RelationType.PROPOSES_CHANGE_TO
                and ctx.scope.contains(r.revision_id)
            ]
            if not proposers:
                continue
            has_current = any(
                ctx.scope.contains(f.revision_id)
                for f in ctx.repo.facts_for_subject(
                    entity.entity_id, lifecycle=(LifecycleStatus.CURRENT,)
                )
            )
            if not has_current:
                ctx.skip(
                    entity.entity_id,
                    "entity",
                    entity.title,
                    "proposta sem comportamento em vigor para comparar: não há evolução a "
                    "documentar, apenas proposta (§10.2)",
                )
                continue
            sources = []
            for rel in sorted(proposers, key=lambda r: r.relation_id):
                src = ctx.repo.get_entity(rel.source_entity_id, lifecycle=None)
                if src is not None:
                    sources.append(src)
            out.extend(
                _assemble(
                    ctx,
                    doc_kind=DocKind.EVOLUCAO,
                    anchor=entity,
                    members=tuple(sources),
                    title=f"{entity.title}: estado atual e mudança proposta",
                )
            )
    return out


# --------------------------------------------------------------------------
# Montagem comum
# --------------------------------------------------------------------------


def _assemble(
    ctx: _PlanContext,
    doc_kind: DocKind,
    anchor: Entity,
    members: Sequence[Entity],
    title: str,
    impacted_ids: set[str] | None = None,
) -> list[KnowledgeDocument]:
    """Monta um documento e devolve `[]` quando ele não deve ser publicado.

    Devolve lista (não `Optional`) porque o motivo da não publicação já foi
    registrado em `skipped`: quem chama não precisa decidir nada.
    """
    units: list[SemanticUnit] = []
    raw_skips: list[tuple[str, str]] = []
    context = _context_of(ctx, doc_kind, anchor)
    if context is not None:
        units.append(context)

    entities: list[Entity] = [anchor] + [e for e in members if e.entity_id != anchor.entity_id]
    seen: set[str] = set()
    for entity in entities:
        if entity.entity_id in seen:
            continue
        seen.add(entity.entity_id)
        try:
            built = units_for_entity(
                ctx.repo, entity, ctx.scope, ctx.belonging(entity), skipped=raw_skips
            )
        except PublishingError as exc:
            ctx.skip(entity.entity_id, "entity", entity.title, str(exc))
            continue
        for unit in built:
            if unit.unit_id == (context.unit_id if context else None):
                continue
            if not unit.is_publishable():
                ctx.skip(unit.unit_id, "unit", unit.title, unit.unpublishable_reason())
                continue
            units.append(unit)

    for target, reason in raw_skips:
        ctx.skip(target, "fact_or_relation", "", reason)

    if not any(u.is_publishable() for u in units if not u.shared_context):
        ctx.skip(
            document_id(ctx.namespace, doc_kind, anchor.entity_id),
            "document",
            title,
            "nenhuma unidade publicável para esta âncora; §10.2: documento sem informação útil "
            "não é publicado",
        )
        return []

    linked = link_cross_state(units)
    doc = KnowledgeDocument(
        document_id=document_id(ctx.namespace, doc_kind, anchor.entity_id),
        title=title,
        doc_kind=doc_kind,
        units=tuple(linked),
        revision_id=ctx.scope.revision_id,
        namespace=ctx.namespace,
        anchor_entity_id=anchor.entity_id,
        anchor_entity_type=anchor.entity_type,
        summary=_summary_for(doc_kind, anchor, len(linked), impacted_ids or set()),
    )
    return [doc]


def _context_of(
    ctx: _PlanContext, doc_kind: DocKind, anchor: Entity
) -> SemanticUnit | None:
    """Mini-contexto do PAI da âncora — capacidade, senão sistema (§10.2).

    Nunca o mini-contexto da própria âncora: ele repetiria, resumido, o que as
    unidades do documento já dizem por inteiro, e repetição redundante é o
    boilerplate que D11 manda evitar. O que falta a quem lê a regra isolada é
    onde ela se encaixa — por isso o contexto vem de um nível acima.
    """
    if doc_kind is DocKind.VISAO_SISTEMA:
        return None
    belonging = ctx.belonging(anchor)
    for candidate in (belonging.capability_id, belonging.system_id):
        if not candidate or candidate == anchor.entity_id:
            continue
        parent = ctx.repo.get_entity(candidate, lifecycle=None)
        if parent is None or not ctx.scope.contains(parent.revision_id):
            continue
        return ctx.context_for(parent)
    return None


def _summary_for(
    doc_kind: DocKind, anchor: Entity, unit_count: int, impacted: set[str]
) -> str:
    base = {
        DocKind.VISAO_SISTEMA: (
            f"Escopo, capacidades e fronteiras de {anchor.title} nesta revisão."
        ),
        DocKind.CAPACIDADE: (
            f"Regras, fluxo, falhas e contratos de {anchor.title}, com estado e evidência de cada bloco."
        ),
        DocKind.CONTRATO_DEPENDENCIA: (
            f"Interface, consumidores, provedores, condições e falhas de {anchor.title}."
        ),
        DocKind.INICIATIVA: (
            f"Cadeia de {anchor.title}: objetivo declarado, decisões, requisitos, refinamentos "
            "e impactos no sistema, com o estado de cada proposta."
        ),
        DocKind.EVOLUCAO: (
            f"Comportamento em vigor de {anchor.title} e a mudança proposta, em blocos separados."
        ),
    }[doc_kind]
    if impacted:
        base += f" Entidades de sistema impactadas nesta revisão: {len(impacted)}."
    return base


# --------------------------------------------------------------------------
# Travessia do grafo
# --------------------------------------------------------------------------


def _neighbors_of_type(
    ctx: _PlanContext,
    entity: Entity,
    wanted: frozenset[EntityType],
    edges: Sequence[RelationType],
) -> list[Entity]:
    """Vizinhos diretos do tipo desejado, em ordem estável."""
    found: dict[str, Entity] = {}
    for rel in ctx.repo.neighbors(
        entity.entity_id, direction="both", relation_types=edges, lifecycle=ALL_LIFECYCLE
    ):
        if not ctx.scope.contains(rel.revision_id):
            continue
        other_id = (
            rel.target_entity_id
            if rel.source_entity_id == entity.entity_id
            else rel.source_entity_id
        )
        other = ctx.repo.get_entity(other_id, lifecycle=None)
        if other is None or other.entity_type not in wanted:
            continue
        if other.namespace != entity.namespace:
            continue
        if not ctx.scope.contains(other.revision_id):
            continue
        found[other_id] = other
    return sorted(found.values(), key=lambda e: (e.entity_type.value, e.stable_key, e.entity_id))


def _members_of_system(ctx: _PlanContext, system: Entity) -> list[Entity]:
    """Capacidades e componentes contidos no sistema — só o nível de visão.

    Regras e fluxos NÃO entram aqui: eles pertencem ao documento da capacidade.
    Trazê-los para a visão é o primeiro passo para o Word monolítico do
    repositório inteiro, proibido por §10.2.
    """
    return _neighbors_of_type(
        ctx,
        system,
        frozenset({EntityType.CAPABILITY, EntityType.COMPONENT, EntityType.CONTRACT}),
        (RelationType.CONTAINS, RelationType.BELONGS_TO, RelationType.DEPENDS_ON),
    )


def _impacted_by(ctx: _PlanContext, entities: Sequence[Entity]) -> list[Entity]:
    """Alvos de `proposes_change_to`/`refines` a partir da iniciativa (D16)."""
    found: dict[str, Entity] = {}
    for entity in entities:
        for rel in ctx.repo.neighbors(
            entity.entity_id,
            direction="out",
            relation_types=(RelationType.PROPOSES_CHANGE_TO,),
            lifecycle=ALL_LIFECYCLE,
        ):
            if not ctx.scope.contains(rel.revision_id):
                continue
            other = ctx.repo.get_entity(rel.target_entity_id, lifecycle=None)
            if other is None or other.namespace != entity.namespace:
                continue
            found[other.entity_id] = other
    return sorted(found.values(), key=lambda e: (e.entity_type.value, e.stable_key, e.entity_id))


def _all_namespaces(repo: Any) -> list[str]:
    rows = repo.conn.execute("SELECT DISTINCT namespace FROM entities ORDER BY namespace").fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------
# Proibições estruturais (§10.2)
# --------------------------------------------------------------------------


def assert_single_system(doc: KnowledgeDocument) -> None:
    """Nenhum documento reúne unidades de mais de um `System` (anti-monolito).

    A visão do sistema é a única exceção prevista, e ainda assim ancorada em um
    único sistema: ela pode citar fronteiras com outros, não absorvê-los.
    """
    systems = doc.system_ids()
    if len(systems) > 1 and doc.doc_kind is not DocKind.VISAO_SISTEMA:
        raise InvalidDocumentGrouping(
            f"documento {doc.document_id} ({doc.title!r}) reúne unidades de {len(systems)} "
            f"sistemas ({', '.join(systems)}); §10.2 proíbe reunir o repositório inteiro em um "
            "documento monolítico"
        )


def _document_sort_key(doc: KnowledgeDocument) -> tuple[str, str, str]:
    order = {
        DocKind.VISAO_SISTEMA: "1",
        DocKind.CAPACIDADE: "2",
        DocKind.CONTRATO_DEPENDENCIA: "3",
        DocKind.INICIATIVA: "4",
        DocKind.EVOLUCAO: "5",
    }[doc.doc_kind]
    return (order, doc.namespace, doc.document_id)


__all__ = [
    "CAPABILITY_MEMBER_TYPES",
    "DEFAULT_CONSUMERS",
    "INITIATIVE_MEMBER_ORDER",
    "MAX_UNITS_PER_DOCUMENT",
    "PublicationPlan",
    "SkippedItem",
    "assert_single_system",
    "plan",
]
