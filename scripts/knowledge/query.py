"""knowledge.query — consultas por consumidor sobre `knowledge.db` (§9.1/§9.2, W7-T7.2).

Este módulo NÃO extrai, NÃO escreve e NÃO chama rede: é uma camada de LEITURA
sobre `repository.Repository`, que resolve escopo e devolve um recorte do
grafo relevante para um consumidor específico (`perguntas`, `inception`,
`historias`, `refinamento`, `bug`).

Duas funções públicas, na ordem exigida pelo §9.2:

1. `resolve_scope` — resolve sistema/iniciativa/capacidade (por id OU por
   nome, com ambiguidade REPORTADA nunca adivinhada) e a fatia temporal
   (`as_of`) ANTES de qualquer expansão de relação. Isso é o que torna
   "consulta sobre versão atual não privilegia substituído" uma propriedade
   estrutural: a busca nunca começa a expandir a partir de um substituto.

2. `retrieve` — busca lexical simples (tokens normalizados; SEM embeddings,
   por §9.2 este módulo cobre só a parte lexical+relacional) sobre os fatos
   das entidades no escopo, seguida de expansão de relações tipadas com:

   - política POR CONSUMIDOR (`CONSUMER_RELATION_POLICY`): cada consumidor só
     segue os tipos de relação pertinentes à sua pergunta (§9.1);
   - orçamento (`Budget`): teto de nós e arestas visitados — nunca o grafo
     inteiro (§9.2 "não carregar o grafo inteiro");
   - conjunto visitado: cada entidade entra na fronteira no máximo uma vez.

   O resultado SEPARA `implemented_current` / `proposed` / `historical`
   (nunca mesclados — é o eixo `lifecycle_status` + a natureza do fato/tipo do
   sujeito, nunca o score léxico). `score` é um campo À PARTE em
   `ScoredFact`/`ScoredRelation`: nenhuma linha de código deste módulo usa o
   score para mudar `epistemic_status` ou `lifecycle_status` de nada — isso
   seria exatamente o erro que §5.3 e §9.2 proíbem ("score de busca não altera
   status do fato").

   Pergunta fora do escopo examinado devolve `gaps` com a fronteira
   examinada (`scope_examined`): namespace, âncoras resolvidas, política de
   relação aplicada, quantos nós/arestas foram de fato visitados. Nunca uma
   resposta genérica preenchida no lugar da lacuna (§9.2).

Fronteira de responsabilidade (ver ordens da tarefa): este módulo importa
SOMENTE `knowledge` (seus próprios vizinhos de pacote) — stdlib e nada mais.
Não importa `sbindex` (em edição concorrente por outro agente) nem qualquer
módulo de `publishing`: a fusão com busca vetorial/publicação é integração
futura, documentada como limitação no relatório de entrega, não código aqui.
Nenhuma chamada de rede: toda consulta é leitura local de `knowledge.db` via
`Repository`.

Determinismo: toda coleção devolvida é ordenada por chave estável (id), nunca
por ordem de visita/inserção — duas chamadas com o mesmo banco e os mesmos
argumentos produzem o mesmo `RetrievalResult` (inclusive `to_dict()`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import identity
from .models import (
    Entity,
    EntityType,
    EpistemicStatus,
    Fact,
    FactNature,
    KnowledgeError,
    LifecycleStatus,
    Relation,
    RelationType,
)

# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------


class QueryError(KnowledgeError):
    """Base dos erros deste módulo."""


class UnknownConsumer(QueryError):
    """`consumer` fora do conjunto suportado por §9.1."""


class AmbiguousScopeReference(QueryError):
    """Nome de sistema/iniciativa/capacidade casa com mais de uma entidade.

    Mesma postura de `identity.resolve_by_name` (§5.2): ambiguidade é
    reportada, nunca resolvida por chute.
    """


# --------------------------------------------------------------------------
# Consumidores e política de expansão (§9.1)
# --------------------------------------------------------------------------

CONSUMER_ASK = "perguntas"
CONSUMER_INCEPTION = "inception"
CONSUMER_STORIES = "historias"
CONSUMER_REFINEMENT = "refinamento"
CONSUMER_BUG = "bug"

#: Os cinco consumidores previstos na tabela do §9.1. Fechado de propósito.
CONSUMERS: frozenset[str] = frozenset(
    {CONSUMER_ASK, CONSUMER_INCEPTION, CONSUMER_STORIES, CONSUMER_REFINEMENT, CONSUMER_BUG}
)

#: Tipos de relação seguidos na expansão, por consumidor (§9.1/§9.2). A
#: política existe para não ser "todo tipo de relação, sempre": um consumidor
#: de refinamento precisa de contrato/persistência/chamada; um consumidor de
#: inception precisa de composição/dependência/decisão — não o mesmo grafo.
CONSUMER_RELATION_POLICY: dict[str, tuple[RelationType, ...]] = {
    CONSUMER_ASK: (
        RelationType.CONTAINS,
        RelationType.IMPLEMENTS,
        RelationType.DEPENDS_ON,
        RelationType.PROPOSES_CHANGE_TO,
    ),
    CONSUMER_INCEPTION: (
        RelationType.CONTAINS,
        RelationType.DEPENDS_ON,
        RelationType.CALLS,
        RelationType.RECORDS,
        RelationType.BELONGS_TO,
    ),
    CONSUMER_STORIES: (
        RelationType.BELONGS_TO,
        RelationType.IMPLEMENTS,
        RelationType.PROPOSES_CHANGE_TO,
        RelationType.CONTAINS,
        RelationType.REFINES,
    ),
    CONSUMER_REFINEMENT: (
        RelationType.CONTAINS,
        RelationType.CALLS,
        RelationType.READS,
        RelationType.WRITES,
        RelationType.PUBLISHES,
        RelationType.CONSUMES,
        RelationType.DEPENDS_ON,
        RelationType.VERIFIES,
        RelationType.REFINES,
        RelationType.PROPOSES_CHANGE_TO,
        RelationType.DERIVED_FROM,
    ),
    CONSUMER_BUG: (
        RelationType.CONTAINS,
        RelationType.CALLS,
        RelationType.READS,
        RelationType.WRITES,
        RelationType.VERIFIES,
        RelationType.CONTRADICTS,
        RelationType.DEPENDS_ON,
    ),
}

#: Entidades de INTENÇÃO (§5.1): descrevem o que foi DECLARADO, nunca
#: comportamento implementado — mesmo com `lifecycle_status=current` (uma
#: decisão pode estar "vigente" como decisão sem que o código a tenha
#: implementado). Fato cujo sujeito é um destes tipos nunca cai em
#: `implemented_current`, mesmo se algum extrator marcar a natureza errada;
#: é uma segunda barreira, redundante de propósito, à mistura proposta ×
#: implementado que §5.3/§9.2 proíbem.
_INTENT_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.INITIATIVE,
        EntityType.DECISION,
        EntityType.REQUIREMENT,
        EntityType.STORY,
        EntityType.REFINEMENT,
        EntityType.DEFECT,
    }
)

#: Tipos de entidade cujo CLUSTER inteiro de fatos entra junto quando
#: qualquer fato/título daquela entidade casa com a pergunta (D04: "manter
#: juntos regra, condição, exceção e consequência essenciais"). Sem isso, uma
#: exceção com vocabulário diferente da pergunta ficaria de fora do resultado
#: mesmo pertencendo à mesma regra que respondeu a pergunta.
CONSUMER_CLUSTER_TYPES: dict[str, frozenset[EntityType]] = {
    CONSUMER_ASK: frozenset({EntityType.BUSINESS_RULE}),
    CONSUMER_INCEPTION: frozenset(),
    CONSUMER_STORIES: frozenset({EntityType.BUSINESS_RULE}),
    CONSUMER_REFINEMENT: frozenset({EntityType.CONTRACT, EntityType.DATA_ENTITY, EntityType.BUSINESS_RULE}),
    CONSUMER_BUG: frozenset({EntityType.FLOW, EntityType.BUSINESS_RULE}),
}

#: Todo `LifecycleStatus`, para navegação de leitura que precisa ENXERGAR
#: proposta/histórico/substituído para poder separá-los — nunca para
#: misturá-los na resposta. `Repository.neighbors` teria filtrado só
#: `current` sem isto.
_ALL_LIFECYCLE: tuple[LifecycleStatus, ...] = tuple(LifecycleStatus)

#: Buckets do resultado (§9.2: "retornar fatos atuais implementados separados
#: de propostas e histórico"). Nomes literais usados como chave de
#: `RetrievalResult.to_dict()["facts"]`.
BUCKET_IMPLEMENTED = "implemented_current"
BUCKET_PROPOSED = "proposed"
BUCKET_HISTORICAL = "historical"


# --------------------------------------------------------------------------
# Tokenização léxica (sem embeddings — §9.2 escopo deste módulo)
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[0-9a-z]+", re.UNICODE)

#: Palavras de função em português/inglês que não carregam assunto. Curta de
#: propósito: o objetivo é reduzir ruído óbvio, não fazer NLP.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "o", "as", "os", "de", "do", "da", "dos", "das", "e", "ou", "um", "uma",
        "uns", "umas", "que", "para", "por", "com", "sem", "no", "na", "nos", "nas",
        "em", "se", "ao", "aos", "as", "é", "sao", "foi", "ser", "the", "of", "and",
        "or", "to", "in", "on", "for", "is", "are", "a", "an",
    }
)


def _fold(text: str) -> str:
    """Minúsculo, sem acento — mesma técnica de `document.normalize_title`."""
    stripped = unicodedata.normalize("NFKD", (text or ""))
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower()


def normalize_tokens(text: str | Iterable[str]) -> frozenset[str]:
    """Tokens normalizados de uma pergunta ou trecho: minúsculo, sem acento,
    sem palavra de função, sem token de 1 caractere. Determinístico: mesma
    entrada sempre produz o mesmo conjunto."""
    if isinstance(text, str):
        raw = _WORD_RE.findall(_fold(text))
    else:
        raw = []
        for item in text:
            raw.extend(_WORD_RE.findall(_fold(str(item))))
    return frozenset(t for t in raw if len(t) > 1 and t not in _STOPWORDS)


def _lexical_score(question_tokens: frozenset[str], *texts: str) -> float:
    """Fração dos tokens da pergunta encontrados no texto candidato.

    `0.0` quando não há sobreposição — inclusive quando a pergunta está
    vazia: pergunta vazia não "casa com tudo" por default.
    """
    if not question_tokens:
        return 0.0
    candidate = normalize_tokens(" ".join(t for t in texts if t))
    if not candidate:
        return 0.0
    overlap = question_tokens & candidate
    if not overlap:
        return 0.0
    return round(len(overlap) / len(question_tokens), 6)


# --------------------------------------------------------------------------
# Escopo (§9.2: resolver ANTES de expandir relações)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """Escopo resolvido: entidades-âncora e fatia temporal, ANTES de
    qualquer expansão de relação (§9.2).

    `system_id`/`initiative_id`/`capability_id` ficam `None` quando o nome
    pedido não foi encontrado — isso NÃO é erro (pode ser exatamente a
    lacuna que a pergunta está testando); `retrieve` usa a ausência para
    montar `scope_examined` de forma honesta, nunca inventa uma âncora.
    """

    namespace: str
    system_id: str | None = None
    system_title: str | None = None
    initiative_id: str | None = None
    initiative_title: str | None = None
    capability_id: str | None = None
    capability_title: str | None = None
    as_of: str | None = None
    unresolved: tuple[str, ...] = ()  # nomes pedidos e não encontrados no namespace

    def anchor_ids(self) -> tuple[str, ...]:
        return tuple(
            eid for eid in (self.system_id, self.initiative_id, self.capability_id) if eid
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "system": {"id": self.system_id, "title": self.system_title},
            "initiative": {"id": self.initiative_id, "title": self.initiative_title},
            "capability": {"id": self.capability_id, "title": self.capability_title},
            "as_of": self.as_of,
            "unresolved": list(self.unresolved),
        }


def _resolve_one(
    repo: Any, namespace: str, entity_type: EntityType, ref: str | None
) -> tuple[str | None, str | None, bool]:
    """Resolve `ref` (id OU nome) dentro de `(namespace, entity_type)`.

    Devolve `(entity_id, title, found)`. Ambiguidade por nome levanta
    `AmbiguousScopeReference` — nunca escolhe a primeira em silêncio.
    """
    if not ref:
        return None, None, True  # não pedido: não é "não encontrado"
    ref = str(ref).strip()
    if not ref:
        return None, None, True

    direct = repo.get_entity(ref, lifecycle=None)
    if direct is not None and direct.entity_type is entity_type and direct.namespace == namespace:
        return direct.entity_id, direct.title, True

    candidates = identity.resolve_by_name(repo.conn, namespace, ref, entity_type)
    if len(candidates) == 1:
        ent = repo.get_entity(candidates[0], lifecycle=None)
        return candidates[0], (ent.title if ent else ref), True
    if len(candidates) > 1:
        raise AmbiguousScopeReference(
            f"{entity_type.value} {ref!r} casa com {len(candidates)} entidades em "
            f"{namespace!r}: {', '.join(sorted(candidates))}; escopo não resolvido por chute (§9.2)"
        )
    return None, None, False


def resolve_scope(
    repo: Any,
    namespace: str,
    *,
    system: str | None = None,
    initiative: str | None = None,
    capability: str | None = None,
    as_of: str | None = None,
) -> Scope:
    """Resolve sistema/iniciativa/capacidade e a fatia temporal (§9.2 item 1).

    `system`/`initiative`/`capability` aceitam `entity_id` OU título/alias
    (via `identity.resolve_by_name`). Roda ANTES de `retrieve`: nenhuma
    relação é seguida aqui — é só identidade e vigência.

    `as_of` é repassado sem inferência: `retrieve` o usa para não contar como
    vigente um fato/relação cuja janela `[valid_from, valid_to)` já fechou
    ANTES de `as_of` ou ainda não abriu; fato sem vigência conhecida (§5.2:
    ausência não é preenchida por inferência) nunca é excluído só por isso.
    """
    ns = identity.normalize_namespace(namespace)
    unresolved: list[str] = []

    sys_id, sys_title, sys_found = _resolve_one(repo, ns, EntityType.SYSTEM, system)
    if not sys_found:
        unresolved.append(f"system={system!r}")
    ini_id, ini_title, ini_found = _resolve_one(repo, ns, EntityType.INITIATIVE, initiative)
    if not ini_found:
        unresolved.append(f"initiative={initiative!r}")
    cap_id, cap_title, cap_found = _resolve_one(repo, ns, EntityType.CAPABILITY, capability)
    if not cap_found:
        unresolved.append(f"capability={capability!r}")

    return Scope(
        namespace=ns,
        system_id=sys_id,
        system_title=sys_title,
        initiative_id=ini_id,
        initiative_title=ini_title,
        capability_id=cap_id,
        capability_title=cap_title,
        as_of=as_of,
        unresolved=tuple(unresolved),
    )


def _within_temporal_window(valid_from: str | None, valid_to: str | None, as_of: str | None) -> bool:
    """`True` quando `as_of` cai dentro de `[valid_from, valid_to)`.

    Vigência desconhecida (`None`) NUNCA exclui (§5.2: ausência não é
    inferência de invalidez); só exclui quando a janela é conhecida e
    `as_of` está fora dela.
    """
    if as_of is None:
        return True
    if valid_from is not None and as_of < valid_from:
        return False
    if valid_to is not None and as_of >= valid_to:
        return False
    return True


# --------------------------------------------------------------------------
# Resultado
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """Orçamento de expansão (§9.2: "não carregar o grafo inteiro").

    `max_nodes`/`max_edges` limitam a travessia; `max_facts_per_bucket` evita
    que uma entidade com centenas de fatos afogue o resultado de um único
    consumidor.
    """

    max_nodes: int = 40
    max_edges: int = 120
    max_facts_per_bucket: int = 100

    def to_dict(self) -> dict[str, int]:
        return {
            "max_nodes": self.max_nodes,
            "max_edges": self.max_edges,
            "max_facts_per_bucket": self.max_facts_per_bucket,
        }


DEFAULT_BUDGET = Budget()


@dataclass(frozen=True)
class ScoredFact:
    """Fato + score léxico, EM CAMPOS SEPARADOS (§9.2: score nunca altera
    `epistemic_status`/`lifecycle_status` — os dois vêm intactos de `Fact`)."""

    fact: Fact
    score: float
    subject_type: EntityType
    subject_title: str

    def to_dict(self) -> dict[str, Any]:
        f = self.fact
        return {
            "fact_id": f.fact_id,
            "subject_id": f.subject_id,
            "subject_type": self.subject_type.value,
            "subject_title": self.subject_title,
            "predicate": f.predicate,
            "value": f.value,
            "scope": f.scope,
            "nature": f.nature.value,
            "epistemic_status": f.epistemic_status.value,
            "lifecycle_status": f.lifecycle_status.value,
            "approval_state": f.approval_state.value,
            "evidence_refs": list(f.evidence_refs),
            "valid_from": f.valid_from,
            "valid_to": f.valid_to,
            "score": self.score,
        }


@dataclass(frozen=True)
class ScoredRelation:
    """Relação + score léxico (derivado das entidades ligadas), campos
    separados pela mesma razão de `ScoredFact`."""

    relation: Relation
    score: float

    def to_dict(self) -> dict[str, Any]:
        r = self.relation
        return {
            "relation_id": r.relation_id,
            "source_entity_id": r.source_entity_id,
            "relation_type": r.relation_type.value,
            "target_entity_id": r.target_entity_id,
            "scope": r.scope,
            "epistemic_status": r.epistemic_status.value,
            "lifecycle_status": r.lifecycle_status.value,
            "evidence_refs": list(r.evidence_refs),
            "score": self.score,
        }


@dataclass(frozen=True)
class Gap:
    """Pergunta (ou parte dela) fora do que foi examinado (§9.2).

    `scope_examined` é a fronteira REAL da busca: nunca um texto genérico —
    sempre os dados de `Scope` mais o que a travessia efetivamente visitou.
    """

    reason: str
    scope_examined: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "scope_examined": dict(self.scope_examined)}


@dataclass(frozen=True)
class RetrievalResult:
    """Recorte do grafo para UM consumidor, determinístico e serializável.

    `implemented_current` / `proposed` / `historical` nunca se sobrepõem:
    cada `ScoredFact` pertence a exatamente um bucket (`_bucket_of`). Fatos
    cujo `epistemic_status` não é `supported` (inferido/disputado/não
    resolvido) continuam DENTRO do bucket correspondente — `hypotheses` é só
    uma VISÃO filtrada (mesmos objetos, não uma cópia mutada), preservando o
    epistemic_status de cada um para o consumidor `bug` (§9.1: "hipóteses
    identificadas como hipóteses").
    """

    consumer: str
    scope: Scope
    question_tokens: tuple[str, ...]
    implemented_current: tuple[ScoredFact, ...]
    proposed: tuple[ScoredFact, ...]
    historical: tuple[ScoredFact, ...]
    relations: tuple[ScoredRelation, ...]
    entities: tuple[Entity, ...]
    gaps: tuple[Gap, ...]
    budget: Budget
    nodes_visited: int
    edges_visited: int
    truncated: bool

    def hypotheses(self) -> tuple[ScoredFact, ...]:
        """Fatos NÃO `supported` (§5.3 eixo sustentação), de qualquer bucket
        — visão para o consumidor `bug`; não remove nada dos buckets."""
        all_facts = self.implemented_current + self.proposed + self.historical
        return tuple(
            sf for sf in all_facts if sf.fact.epistemic_status is not EpistemicStatus.SUPPORTED
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumer": self.consumer,
            "scope": self.scope.to_dict(),
            "question_tokens": sorted(self.question_tokens),
            "facts": {
                BUCKET_IMPLEMENTED: [sf.to_dict() for sf in self.implemented_current],
                BUCKET_PROPOSED: [sf.to_dict() for sf in self.proposed],
                BUCKET_HISTORICAL: [sf.to_dict() for sf in self.historical],
            },
            "hypotheses": [sf.to_dict() for sf in self.hypotheses()],
            "relations": [sr.to_dict() for sr in self.relations],
            "entities": [
                {"entity_id": e.entity_id, "entity_type": e.entity_type.value, "title": e.title}
                for e in self.entities
            ],
            "gaps": [g.to_dict() for g in self.gaps],
            "budget": self.budget.to_dict(),
            "nodes_visited": self.nodes_visited,
            "edges_visited": self.edges_visited,
            "truncated": self.truncated,
        }


# --------------------------------------------------------------------------
# retrieve (§9.2)
# --------------------------------------------------------------------------


def _bucket_of(fact: Fact, subject_type: EntityType) -> str:
    """Único ponto de decisão de bucket — NUNCA olha `score` (§9.2)."""
    if fact.lifecycle_status in (
        LifecycleStatus.HISTORICAL,
        LifecycleStatus.SUPERSEDED,
        LifecycleStatus.STALE,
    ):
        return BUCKET_HISTORICAL
    if fact.lifecycle_status is LifecycleStatus.PROPOSED:
        return BUCKET_PROPOSED
    # lifecycle_status == CURRENT a partir daqui.
    if subject_type in _INTENT_TYPES:
        # Decisão/requisito/refinamento "vigente" continua sendo o que foi
        # DECLARADO, nunca comportamento implementado (§5.3).
        return BUCKET_PROPOSED
    if fact.nature in (FactNature.IMPLEMENTED, FactNature.OBSERVED):
        return BUCKET_IMPLEMENTED
    # declared_requirement/test_expectation com lifecycle current mas sujeito
    # técnico: ainda não é comportamento confirmado do sistema.
    return BUCKET_PROPOSED


def _visit_graph(
    repo: Any, scope: Scope, allowed_relations: Sequence[RelationType], budget: Budget
) -> tuple[dict[str, Entity], list[Relation], bool]:
    """BFS por relações tipadas, com orçamento e conjunto visitado (§9.2).

    Devolve `(entidades_visitadas, relações_seguidas, truncado)`. Nunca
    atravessa para outro namespace — homônimos entre sistemas não se
    confundem (§5.2).
    """
    visited: dict[str, Entity] = {}
    for eid in scope.anchor_ids():
        ent = repo.get_entity(eid, lifecycle=None)
        if ent is not None:
            visited[eid] = ent

    queue: list[str] = list(visited.keys())
    relations: list[Relation] = []
    seen_relation_ids: set[str] = set()
    truncated = False
    cursor = 0
    while cursor < len(queue):
        current = queue[cursor]
        cursor += 1
        if len(relations) >= budget.max_edges:
            truncated = True
            break
        neighbor_rels = repo.neighbors(
            current, direction="both", relation_types=allowed_relations, lifecycle=_ALL_LIFECYCLE
        )
        for rel in sorted(neighbor_rels, key=lambda r: r.relation_id):
            if len(relations) >= budget.max_edges:
                truncated = True
                break
            if rel.relation_id in seen_relation_ids:
                continue
            seen_relation_ids.add(rel.relation_id)
            relations.append(rel)
            other_id = (
                rel.target_entity_id if rel.source_entity_id == current else rel.source_entity_id
            )
            if other_id in visited:
                continue
            if len(visited) >= budget.max_nodes:
                truncated = True
                continue
            other = repo.get_entity(other_id, lifecycle=None)
            if other is None or other.namespace != scope.namespace:
                continue
            visited[other_id] = other
            queue.append(other_id)
    return visited, relations, truncated


def retrieve(
    repo: Any,
    scope: Scope,
    question_tokens: str | Iterable[str],
    *,
    consumer: str,
    budget: Budget | None = None,
) -> RetrievalResult:
    """Recupera fatos/relações/entidades pertinentes a `consumer` dentro de
    `scope` (§9.1/§9.2). Ver docstring do módulo para as garantias.

    `question_tokens` aceita a pergunta como string (tokenizada aqui) ou já
    como coleção de tokens/termos.
    """
    if consumer not in CONSUMERS:
        raise UnknownConsumer(
            f"consumer={consumer!r} não é um dos suportados por §9.1: {sorted(CONSUMERS)}"
        )
    budget = budget or DEFAULT_BUDGET
    tokens = normalize_tokens(question_tokens)
    allowed_relations = CONSUMER_RELATION_POLICY[consumer]
    cluster_types = CONSUMER_CLUSTER_TYPES[consumer]

    visited, relations, truncated = _visit_graph(repo, scope, allowed_relations, budget)

    # Score de entidade (título) — usado para (a) score de relação e (b)
    # decidir se o cluster inteiro de uma entidade "casa" com a pergunta.
    entity_hit: dict[str, float] = {
        eid: _lexical_score(tokens, ent.title, ent.stable_key) for eid, ent in visited.items()
    }

    buckets: dict[str, list[ScoredFact]] = {
        BUCKET_IMPLEMENTED: [],
        BUCKET_PROPOSED: [],
        BUCKET_HISTORICAL: [],
    }
    any_hit = False

    for eid in sorted(visited):
        entity = visited[eid]
        facts = [
            f
            for f in repo.facts_for_subject(eid, lifecycle=None)
            if _within_temporal_window(f.valid_from, f.valid_to, scope.as_of)
        ]
        if not facts:
            continue
        scored = [
            (f, _lexical_score(tokens, f.predicate, f.value))
            for f in sorted(facts, key=lambda x: x.fact_id)
        ]
        entity_scored_any = entity_hit.get(eid, 0.0) > 0.0 or any(s > 0.0 for _f, s in scored)
        cluster_whole = entity_scored_any and entity.entity_type in cluster_types

        for fact, score in scored:
            include = score > 0.0 or entity_hit.get(eid, 0.0) > 0.0 or cluster_whole
            if not include:
                continue
            any_hit = any_hit or score > 0.0 or entity_hit.get(eid, 0.0) > 0.0
            bucket_name = _bucket_of(fact, entity.entity_type)
            bucket = buckets[bucket_name]
            if len(bucket) >= budget.max_facts_per_bucket:
                truncated = True
                continue
            bucket.append(
                ScoredFact(
                    fact=fact, score=score, subject_type=entity.entity_type, subject_title=entity.title
                )
            )

    scored_relations: list[ScoredRelation] = []
    for rel in sorted(relations, key=lambda r: r.relation_id):
        rel_score = max(
            entity_hit.get(rel.source_entity_id, 0.0), entity_hit.get(rel.target_entity_id, 0.0)
        )
        scored_relations.append(ScoredRelation(relation=rel, score=rel_score))
        any_hit = any_hit or rel_score > 0.0

    gaps: list[Gap] = []
    scope_examined = {
        **scope.to_dict(),
        "consumer": consumer,
        "relation_policy": [r.value for r in allowed_relations],
        "nodes_visited": len(visited),
        "edges_visited": len(relations),
        "visited_entity_ids": sorted(visited.keys()),
        "budget": budget.to_dict(),
    }
    if scope.unresolved:
        gaps.append(
            Gap(
                reason=(
                    "referência de escopo não encontrada no namespace examinado: "
                    + "; ".join(scope.unresolved)
                ),
                scope_examined=scope_examined,
            )
        )
    if not any_hit:
        gaps.append(
            Gap(
                reason=(
                    "nenhum fato ou relação, dentro da fronteira examinada, casa com a "
                    "pergunta; não preenchido com conhecimento genérico (§9.2)"
                ),
                scope_examined=scope_examined,
            )
        )

    return RetrievalResult(
        consumer=consumer,
        scope=scope,
        question_tokens=tuple(sorted(tokens)),
        implemented_current=tuple(
            sorted(buckets[BUCKET_IMPLEMENTED], key=lambda sf: sf.fact.fact_id)
        ),
        proposed=tuple(sorted(buckets[BUCKET_PROPOSED], key=lambda sf: sf.fact.fact_id)),
        historical=tuple(sorted(buckets[BUCKET_HISTORICAL], key=lambda sf: sf.fact.fact_id)),
        relations=tuple(scored_relations),
        entities=tuple(sorted(visited.values(), key=lambda e: e.entity_id)),
        gaps=tuple(gaps),
        budget=budget,
        nodes_visited=len(visited),
        edges_visited=len(relations),
        truncated=truncated,
    )


__all__ = [
    "AmbiguousScopeReference",
    "Budget",
    "BUCKET_HISTORICAL",
    "BUCKET_IMPLEMENTED",
    "BUCKET_PROPOSED",
    "CONSUMER_ASK",
    "CONSUMER_BUG",
    "CONSUMER_INCEPTION",
    "CONSUMER_REFINEMENT",
    "CONSUMER_STORIES",
    "CONSUMERS",
    "CONSUMER_RELATION_POLICY",
    "DEFAULT_BUDGET",
    "Gap",
    "QueryError",
    "RetrievalResult",
    "Scope",
    "ScoredFact",
    "ScoredRelation",
    "UnknownConsumer",
    "normalize_tokens",
    "resolve_scope",
    "retrieve",
]
