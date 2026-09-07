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
from typing import Any, Mapping, Sequence

from knowledge.models import (
    Entity,
    EntityType,
    LifecycleStatus,
    RelationType,
)

from .document import (
    ALL_LIFECYCLE,
    ANALYSIS_STATE_LABEL,
    AnalysisState,
    Belonging,
    DocKind,
    InvalidDocumentGrouping,
    KnowledgeDocument,
    PublishingError,
    RevisionScope,
    SemanticUnit,
    analysis_summary,
    context_unit,
    document_id,
    honest_title,
    link_cross_state,
    resolve_belonging,
    units_for_entity,
    worst_analysis_state,
)

#: Espécies de documento cuja completude é DERIVADA das obrigações que as
#: sustentam, não só das próprias unidades (achado bloqueante nº2 da 2ª
#: auditoria): um contrato não é mais completo do que a capacidade que o expõe
#: ou consome. Sem esta herança, a capacidade saía `parcial` e o contrato
#: `completo` no MESMO conjunto, sobre a mesma investigação.
INHERITING_DOC_KINDS: frozenset[DocKind] = frozenset({DocKind.CONTRATO_DEPENDENCIA})

#: Tipos de entidade cujo estado de análise SUSTENTA o documento herdeiro.
SUSTAINING_ENTITY_TYPES: tuple[EntityType, ...] = (EntityType.CAPABILITY,)

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
            # Aditivo: quantos documentos saem com análise completa, parcial ou
            # só estrutural — quem lê o plano vê a suficiência antes de abrir
            # qualquer arquivo.
            "by_analysis_state": {
                s.value: sum(
                    1 for d in self.documents if d.effective_analysis_state() is s
                )
                for s in AnalysisState
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
    investigation_states: Mapping[str, Any] | None = None,
) -> PublicationPlan:
    """Planeja a publicação de uma revisão (§10.2), de forma determinística.

    `namespace=None` planeja todos os namespaces presentes em `knowledge.db`;
    namespaces continuam isolados (§5.2), cada entidade só encontra vizinhos
    do próprio namespace porque a travessia parte das entidades dele.

    `investigation_states` é OPCIONAL e vem de quem conduziu a investigação
    (objetivos e lacunas do FLUXO 3). Chave: `document_id`, `entity_id`
    (de Capability, Contract ou qualquer âncora), `stable_key`, título da
    âncora ou o NAMESPACE (teto de último recurso para documentos que não
    resolvem obrigação nenhuma). Chaves que não casam com documento algum são
    ignoradas sem erro. Valor: o estado (`completo`, `parcial`, `estrutural` ou
    `AnalysisState`) ou um mapa
    `{"state": ..., "gaps": ["o que falta e em que pé está", ...]}`.
    Sem ele, o estado é DERIVADO das unidades — nunca presumido completo:
    documento sem nenhuma unidade com comportamento sustentado por evidência
    sai como `estrutural`, com título honesto e lacunas no corpo.

    Duas regras duras do achado bloqueante nº2 (2ª auditoria):

    - documento sem NENHUMA unidade behavioral nunca sai `completo`, nem quando
      a investigação declarou completude (a declaração é rebaixada e a lacuna
      explica por quê);
    - documento de contrato/dependência herda o PIOR estado das capacidades que
      o expõem/consomem — capacidade `parcial` e contrato `completo` no mesmo
      conjunto deixou de ser representável.
    """
    scope = RevisionScope(repo, revision_id)
    namespaces = [namespace] if namespace else _all_namespaces(repo)
    documents: list[KnowledgeDocument] = []
    catalog: dict[str, SemanticUnit] = {}
    skipped: list[SkippedItem] = []
    warnings: list[str] = []
    investigation = dict(investigation_states or {})

    for ns in namespaces:
        ctx = _PlanContext(
            repo=repo,
            namespace=ns,
            scope=scope,
            skipped=skipped,
            investigation=investigation,
        )
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
        if doc.effective_analysis_state() is AnalysisState.ESTRUTURAL:
            warnings.append(
                f"documento {doc.document_id} ({doc.title!r}) sai como análise estrutural: "
                "nenhuma unidade tem condição, comportamento ou exceção sustentada por evidência "
                "nesta revisão; título e resumo foram ajustados e as lacunas estão no corpo"
            )
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
    investigation: Mapping[str, Any] = field(default_factory=dict)
    catalog: dict[str, SemanticUnit] = field(default_factory=dict)
    #: `entity_id`/`document_id` → estado de análise já PUBLICADO nesta execução.
    #: Capacidades são planejadas antes dos contratos (`plan`), então o contrato
    #: sempre encontra aqui o estado da capacidade que o sustenta.
    analysis_by_key: dict[str, AnalysisState] = field(default_factory=dict)
    _belonging: dict[str, Belonging] = field(default_factory=dict)

    def record_analysis(self, doc_id: str, anchor: Entity, state: AnalysisState) -> None:
        """Registra o estado do documento montado, por documento E por âncora."""
        for key in (doc_id, anchor.entity_id, anchor.stable_key):
            if key:
                self.analysis_by_key[key] = state

    def analysis_of(self, entity: Entity) -> AnalysisState:
        """Estado da análise de uma entidade que SUSTENTA outro documento.

        Ordem: o que já foi montado nesta execução → o que a investigação
        declarou → `estrutural`. O último caso não é chute: entidade sem
        documento montado e sem estado declarado não tem comportamento
        publicado nenhum nesta revisão.
        """
        for key in (entity.entity_id, entity.stable_key):
            if key and key in self.analysis_by_key:
                return self.analysis_by_key[key]
        declared, _ = self.declared_analysis(
            document_id(self.namespace, DocKind.CAPACIDADE, entity.entity_id), entity
        )
        if declared is not None:
            return declared
        return AnalysisState.ESTRUTURAL

    def namespace_analysis(self) -> AnalysisState | None:
        """Estado declarado para o NAMESPACE inteiro, quando informado.

        É o teto de último recurso do documento que não resolve nenhuma
        obrigação: `investigation_states={"acme/pagamentos": "parcial"}`.
        """
        raw = self.investigation.get(self.namespace)
        if raw is None:
            return None
        if isinstance(raw, Mapping):
            raw = raw.get("state") or raw.get("estado")
        if raw is None:
            return None
        if isinstance(raw, AnalysisState):
            return raw
        try:
            return AnalysisState(str(raw).strip().lower())
        except ValueError:
            raise PublishingError(
                f"estado de investigação {raw!r} do namespace {self.namespace!r} não é um "
                f"AnalysisState válido ({', '.join(s.value for s in AnalysisState)})"
            ) from None

    def declared_analysis(
        self, doc_id: str, anchor: Entity
    ) -> tuple[AnalysisState | None, tuple[str, ...]]:
        """Estado e lacunas informados pela investigação, se houver.

        Procura por `document_id`, `entity_id`, `stable_key` e título — quem
        conduz a investigação conhece a entidade pelo código de negócio
        ("CAP-023"), não pelo hash do documento.
        """
        raw = None
        for key in (doc_id, anchor.entity_id, anchor.stable_key, anchor.title):
            if key and key in self.investigation:
                raw = self.investigation[key]
                break
        if raw is None:
            return None, ()
        if isinstance(raw, Mapping):
            state_val = raw.get("state") or raw.get("estado")
            gaps = tuple(
                str(g).strip()
                for g in (raw.get("gaps") or raw.get("lacunas") or ())
                if str(g).strip()
            )
        else:
            state_val, gaps = raw, ()
        if state_val is None:
            return None, gaps
        if isinstance(state_val, AnalysisState):
            return state_val, gaps
        try:
            return AnalysisState(str(state_val).strip().lower()), gaps
        except ValueError:
            raise PublishingError(
                f"estado de investigação {state_val!r} não é um AnalysisState válido "
                f"({', '.join(s.value for s in AnalysisState)})"
            ) from None

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
                # A completude do contrato é herdada das capacidades que o
                # expõem/consomem (achado nº2): `peers` já traz as pontas
                # tipadas resolvidas pelas relações da revisão.
                inherit_from=peers,
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
    inherit_from: Sequence[Entity] = (),
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
    doc_id = document_id(ctx.namespace, doc_kind, anchor.entity_id)

    # Suficiência ANTES do título (achado bloqueante nº2): o rótulo do
    # documento é decidido depois de saber que espécie de conteúdo existe,
    # nunca antes. Documento sem nenhuma unidade behavioral não sai com o
    # título que promete regra, fluxo e falha.
    declared_state, declared_gaps = ctx.declared_analysis(doc_id, anchor)
    inherited_state, inherited_reason = _inherited_analysis(ctx, doc_kind, inherit_from)
    analysis_state, analysis_gaps = analysis_summary(
        linked,
        declared_state=declared_state,
        declared_gaps=declared_gaps,
        inherited_state=inherited_state,
        inherited_reason=inherited_reason,
    )
    ctx.record_analysis(doc_id, anchor, analysis_state)
    doc_title = honest_title(doc_kind, anchor.title, title, analysis_state)
    summary = _summary_for(
        doc_kind, anchor, len(linked), impacted_ids or set(), analysis_state
    )

    doc = KnowledgeDocument(
        document_id=doc_id,
        title=doc_title,
        doc_kind=doc_kind,
        units=tuple(linked),
        revision_id=ctx.scope.revision_id,
        namespace=ctx.namespace,
        anchor_entity_id=anchor.entity_id,
        anchor_entity_type=anchor.entity_type,
        summary=summary,
        analysis_state=analysis_state,
        analysis_gaps=analysis_gaps,
    )
    # Rede de segurança: nenhum caminho deste módulo pode produzir documento
    # que prometa comportamento sem nenhuma unidade behavioral. Se acontecer, o
    # documento fica de fora COM motivo, em vez de ser publicado mentindo.
    problems = doc.sufficiency_problems()
    if problems and not doc.behavioral_units() and doc.title_promises_behavior():
        ctx.skip(doc_id, "document", doc_title, "; ".join(problems))
        return []
    return [doc]


def _inherited_analysis(
    ctx: _PlanContext, doc_kind: DocKind, related: Sequence[Entity]
) -> tuple[AnalysisState | None, str]:
    """TETO do estado da análise vindo das obrigações que sustentam o documento.

    Regra do achado bloqueante nº2: um documento de contrato/dependência não
    pode sair `completo` enquanto a capacidade que o expõe ou consome está
    `parcial` — as duas páginas descrevem a MESMA investigação e sair com
    rótulos diferentes é a contradição que a auditoria encontrou.

    Ordem de resolução:

    1. PIOR estado entre as capacidades relacionadas por relação tipada da
       revisão (`consumes`/`publishes`/`contains`/`implements`/`depends_on`…);
    2. sem capacidade resolvível, o estado declarado para o NAMESPACE em
       `investigation_states`, quando houver;
    3. sem nada disso, teto `parcial`: sem obrigação resolvível não há como
       AFIRMAR completude — mas também não se declara `estrutural` um documento
       que publica regra com evidência, porque isso seria a mentira simétrica.

    Devolve `(None, "")` para espécies que não herdam.
    """
    if doc_kind not in INHERITING_DOC_KINDS:
        return None, ""
    sustaining = [e for e in related if e.entity_type in SUSTAINING_ENTITY_TYPES]
    if sustaining:
        states = {e.entity_id: ctx.analysis_of(e) for e in sustaining}
        worst = worst_analysis_state(states.values())
        if worst is None:  # pragma: no cover - `sustaining` não vazio garante estado
            return None, ""
        names = ", ".join(
            sorted(e.title for e in sustaining if states[e.entity_id] is worst)
        )
        return worst, (
            "A completude deste documento está limitada pelo estado da análise da capacidade "
            f"que o sustenta ({names}): {ANALYSIS_STATE_LABEL[worst]}. Contrato e capacidade "
            "descrevem a mesma investigação e não podem sair com rótulos diferentes."
        )
    ns_state = ctx.namespace_analysis()
    if ns_state is not None:
        return ns_state, (
            "Nenhuma capacidade relacionada foi resolvida nesta revisão para este contrato; o "
            f"estado herdado é o declarado para o namespace {ctx.namespace}: "
            f"{ANALYSIS_STATE_LABEL[ns_state]}."
        )
    return AnalysisState.PARCIAL, (
        "Nenhuma capacidade que exponha ou consuma este contrato foi resolvida nesta revisão: "
        "não há obrigação conhecida contra a qual verificar a completude, então o documento não "
        "pode ser declarado completo."
    )


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
    doc_kind: DocKind,
    anchor: Entity,
    unit_count: int,
    impacted: set[str],
    analysis_state: AnalysisState | None = None,
) -> str:
    if analysis_state is AnalysisState.ESTRUTURAL:
        # O subtítulo acompanha o título: prometer "regras, fluxo, falhas e
        # contratos" no resumo enquanto o título já foi corrigido apenas move a
        # promessa falsa de lugar.
        base = {
            DocKind.VISAO_SISTEMA: (
                f"Estrutura, fronteiras e relações de {anchor.title} nesta revisão."
            ),
            DocKind.CAPACIDADE: (
                f"Estrutura, identidade e contratos de {anchor.title} nesta revisão."
            ),
            DocKind.CONTRATO_DEPENDENCIA: (
                f"Interface, consumidores e provedores de {anchor.title} nesta revisão."
            ),
            DocKind.INICIATIVA: (
                f"Cadeia declarada de {anchor.title}: artefatos, decisões e vínculos registrados."
            ),
            DocKind.EVOLUCAO: (
                f"Estrutura de {anchor.title} e a proposta registrada nesta revisão."
            ),
        }[doc_kind]
        base += (
            " Nenhum comportamento foi avaliado nesta revisão; as lacunas estão declaradas no "
            "corpo do documento."
        )
        if impacted:
            base += f" Entidades de sistema impactadas nesta revisão: {len(impacted)}."
        return base
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
    "INHERITING_DOC_KINDS",
    "INITIATIVE_MEMBER_ORDER",
    "MAX_UNITS_PER_DOCUMENT",
    "SUSTAINING_ENTITY_TYPES",
    "PublicationPlan",
    "SkippedItem",
    "assert_single_system",
    "plan",
]
