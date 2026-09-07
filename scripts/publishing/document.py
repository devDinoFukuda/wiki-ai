"""Representação intermediária `KnowledgeDocument` (plano §10.1 e §10.3).

Esta é a FONTE COMUM de Markdown e Word: os renderizadores projetam esta
estrutura, nunca convertem um formato no outro (§10.1). Três regras do plano
existem aqui como código executável, não como recomendação:

1. **Título específico (D02)**: `SemanticUnit`/`KnowledgeDocument` recusam
   títulos genéricos ("Detalhes", "Outros", "Geral", numeração pura). Quem
   monta o plano descobre o problema na construção, não no arquivo publicado.
2. **Sem campos entre colchetes (§10.3)**: a notação `[...]` do plano é
   explicativa; em conteúdo final ela é lacuna disfarçada de resposta.
   `PLACEHOLDER_RE` rejeita colchetes com espaço/elipse dentro — `list[int]`
   e `RN-023` continuam válidos porque não são texto de preenchimento.
3. **Implementado nunca no mesmo bloco que proposta (D10)**: a unidade tem UM
   `state`. Todo `Statement` carregado por ela precisa ter o mesmo estado, o
   que torna estruturalmente impossível misturar comportamento atual e
   proposta na mesma seção. O vínculo entre os dois vira `CrossRef` — um
   ponteiro rotulado, sem copiar o conteúdo da proposta para dentro do bloco
   implementado.

Identidade da unidade (`unit_id`) é derivada de `(namespace, entidade,
assunto, estado)` — NUNCA da posição no documento. Reordenar seções, mudar o
título ou reagrupar documentos não troca o `unit_id`; é o que permite ao
manifesto (§10.5) comparar duas gerações da mesma unidade.

Só stdlib + `knowledge`.
"""

from __future__ import annotations

import enum
import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from knowledge import identity
from knowledge.models import (
    ContentKind,
    Entity,
    EntityType,
    EpistemicStatus,
    Evidence,
    Fact,
    FactNature,
    KnowledgeError,
    LifecycleStatus,
    Relation,
    RelationType,
    SourceKind,
)

# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------


class PublishingError(KnowledgeError):
    """Base dos erros de publicação (§10)."""


class GenericTitle(PublishingError):
    """Título genérico proibido por D02 ("Detalhes", "Outros", ...)."""


class PlaceholderContent(PublishingError):
    """Campo entre colchetes em conteúdo final (§10.3)."""


class MixedStateBlock(PublishingError):
    """Comportamento implementado e proposta no MESMO bloco (D10)."""


class EmptyDocument(PublishingError):
    """Documento sem unidade publicável (§10.2: não é publicado)."""


class InvalidDocumentGrouping(PublishingError):
    """Agrupamento proibido: doc por classe/chunk/etapa ou monolito (§10.2)."""


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class DocKind(str, enum.Enum):
    """§10.2 — as cinco unidades documentais previstas. Fechado de propósito:
    documento por classe, por chunk ou por etapa do pipeline não tem entrada
    nesta enum, então não existe caminho para criá-lo."""

    VISAO_SISTEMA = "visao_sistema"
    CAPACIDADE = "capacidade"
    CONTRATO_DEPENDENCIA = "contrato_dependencia"
    INICIATIVA = "iniciativa"
    EVOLUCAO = "evolucao"


class UnitState(str, enum.Enum):
    """§10.3 item 3 — estado da unidade recuperada isoladamente."""

    IMPLEMENTED = "implemented"
    PROPOSED = "proposed"
    HISTORICAL = "historical"
    UNRESOLVED = "unresolved"


class ContentGrade(str, enum.Enum):
    """ESPÉCIE de conteúdo que a unidade carrega — não o estado dela.

    `UnitState` responde "em que vigência isto está"; `ContentGrade` responde
    "isto chega a descrever comportamento?". São eixos independentes: uma
    unidade `implemented` pode não ter uma única condição/comportamento/exceção
    sustentada — é o caso que produziu Word intitulado "regras, fluxo, falhas e
    contratos" com nada além de `implemented = true` e uma lista de relações.

    - `behavioral`: existe ao menos um `Statement` que passa nas TRÊS provas de
      `is_behavioral_statement()` — predicado comportamental (não estrutural),
      conteúdo com condição E efeito reais, e evidência própria
      (`evidence_ids`). Presença de evidência sozinha NÃO basta: `implemented =
      true` tem evidência executável e mesmo assim não diz o que o sistema faz
      sob que condição — foi exatamente esse atalho que produziu o achado
      bloqueante nº2 da 2ª auditoria (capacidade `parcial` e contrato
      `completo` no MESMO conjunto).
    - `structural`: tem conteúdo, mas só identidade/relações/flags — ou frases
      de comportamento sem nenhuma evidência ligada, ou apenas lacunas e
      limitações. Publicável, desde que o rótulo não prometa comportamento.
    - `empty`: nem statements nem relações.
    """

    BEHAVIORAL = "behavioral"
    STRUCTURAL = "structural"
    EMPTY = "empty"


class AnalysisState(str, enum.Enum):
    """Quanto da análise de COMPORTAMENTO foi feita no documento.

    Publicado no corpo do documento (Markdown e Word) para que o leitor saiba,
    sem abrir outra fonte, se a ausência de uma regra significa "não existe" ou
    "ainda não foi investigado" (§5.3: ausência examinada ≠ ausência global).
    """

    COMPLETO = "completo"
    PARCIAL = "parcial"
    ESTRUTURAL = "estrutural"


#: Rótulo humano do estado da análise — MESMO texto nos dois renderizadores.
ANALYSIS_STATE_LABEL: dict[AnalysisState, str] = {
    AnalysisState.COMPLETO: (
        "completo: todas as unidades publicadas têm condição, comportamento ou exceção "
        "sustentada por evidência"
    ),
    AnalysisState.PARCIAL: (
        "parcial: parte das unidades publicadas ainda não tem comportamento avaliado nesta "
        "revisão"
    ),
    AnalysisState.ESTRUTURAL: (
        "estrutural: nenhuma unidade publicada tem comportamento avaliado nesta revisão; o "
        "documento descreve estrutura, identidade e relações"
    ),
}

#: Rótulo usado quando NENHUM `Statement` do documento é behavioral pela regra
#: de `is_behavioral_statement()`. A frase de completude ("todas as unidades
#: publicadas têm condição, comportamento ou exceção sustentada") é proibida
#: aqui: o único fato pode ser `implemented = true`, que é estrutura com
#: evidência, não comportamento (achado bloqueante nº2 da 2ª auditoria).
NO_BEHAVIOR_ANALYSIS_LABEL: str = (
    "apenas estrutura e contratos confirmados; comportamento não analisado nesta revisão "
    "(nenhuma condição, comportamento ou exceção com conteúdo real sustentado por evidência)"
)

#: Severidade do estado da análise — menor é PIOR. Usada para herdar o PIOR
#: estado entre obrigações que sustentam um documento (planner) e para nunca
#: elevar um estado por herança.
ANALYSIS_SEVERITY: dict[AnalysisState, int] = {
    AnalysisState.ESTRUTURAL: 0,
    AnalysisState.PARCIAL: 1,
    AnalysisState.COMPLETO: 2,
}


def worst_analysis_state(states: Iterable[AnalysisState]) -> AnalysisState | None:
    """PIOR estado entre os informados (`None` quando não há nenhum)."""
    found = [s for s in states if s is not None]
    if not found:
        return None
    return min(found, key=lambda s: ANALYSIS_SEVERITY[s])


def analysis_state_label(state: AnalysisState, has_behavior: bool = True) -> str:
    """Rótulo humano do estado — honesto quanto ao que sustenta a afirmação.

    Sem nenhum `Statement` behavioral, NENHUM estado pode afirmar "condição,
    comportamento ou exceção sustentada": o texto passa a dizer o que de fato
    existe (estrutura e contratos) e que comportamento não foi analisado.
    """
    if not has_behavior:
        return NO_BEHAVIOR_ANALYSIS_LABEL
    return ANALYSIS_STATE_LABEL[state]

#: Título da seção de lacunas do DOCUMENTO (nível de documento, distinta da
#: seção de lacunas por unidade). Compartilhado por markdown/word/validate para
#: que a checagem "a seção existe mesmo no corpo" procure o mesmo texto que o
#: renderizador escreve.
ANALYSIS_GAPS_TITLE = "Lacunas da análise nesta revisão"

#: Tipos de documento cujo contrato de leitura é comportamento: quem abre um
#: documento de capacidade, de contrato ou de evolução está perguntando "o que
#: o sistema faz", não "o que existe". Estrutura pura aqui só é publicável com
#: lacuna declarada.
BEHAVIOR_REQUIRED_KINDS: frozenset[DocKind] = frozenset(
    {DocKind.CAPACIDADE, DocKind.CONTRATO_DEPENDENCIA, DocKind.EVOLUCAO}
)


#: `Repository.neighbors` NÃO aceita `lifecycle=None` (só `facts_for_subject`
#: e `get_entity` aceitam). Publicação precisa enxergar proposta, histórico e
#: substituído para poder ROTULÁ-LOS — filtrar só `current` esconderia
#: exatamente o que D10 manda separar. Daí a tupla explícita com todos.
ALL_LIFECYCLE: tuple[LifecycleStatus, ...] = tuple(LifecycleStatus)


#: Rótulo humano do estado, usado nos dois renderizadores (D05/D10).
STATE_LABEL: dict[UnitState, str] = {
    UnitState.IMPLEMENTED: "Implementado",
    UnitState.PROPOSED: "Proposto (não implementado)",
    UnitState.HISTORICAL: "Histórico",
    UnitState.UNRESOLVED: "Não resolvido",
}

#: Rótulo do bloco de comportamento, por estado (D10: blocos próprios).
STATE_BLOCK_LABEL: dict[UnitState, str] = {
    UnitState.IMPLEMENTED: "Comportamento implementado",
    UnitState.PROPOSED: "Mudança proposta (ainda não implementada)",
    UnitState.HISTORICAL: "Comportamento histórico (não vigente)",
    UnitState.UNRESOLVED: "Ponto não resolvido",
}

#: Entidades de INTENÇÃO (§5.1): iniciativa, decisão, requisito, história,
#: refinamento e defeito. Elas nunca descrevem comportamento implementado —
#: descrevem o que foi declarado. Chamar o bloco delas de "mudança proposta"
#: seria impreciso; chamá-lo de "comportamento" seria falso. Daí o rótulo
#: próprio em `block_label`.
INTENT_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.INITIATIVE,
        EntityType.DECISION,
        EntityType.REQUIREMENT,
        EntityType.STORY,
        EntityType.REFINEMENT,
        EntityType.DEFECT,
    }
)

#: Anchor entity types aceitos por tipo de documento (§10.2). Component,
#: DataEntity, Source e afins NÃO ancoram documento: é a proibição de
#: "um documento por classe/chunk" expressa como dado.
ALLOWED_ANCHORS: dict[DocKind, frozenset[EntityType]] = {
    DocKind.VISAO_SISTEMA: frozenset({EntityType.SYSTEM}),
    DocKind.CAPACIDADE: frozenset({EntityType.CAPABILITY}),
    DocKind.CONTRATO_DEPENDENCIA: frozenset({EntityType.CONTRACT}),
    DocKind.INICIATIVA: frozenset({EntityType.INITIATIVE}),
    DocKind.EVOLUCAO: frozenset(
        {EntityType.BUSINESS_RULE, EntityType.CAPABILITY, EntityType.FLOW, EntityType.CONTRACT}
    ),
}


# --------------------------------------------------------------------------
# D02 — títulos genéricos e §10.3 — colchetes
# --------------------------------------------------------------------------

#: Títulos proibidos por D02 (comparação sobre a forma normalizada).
GENERIC_TITLES: frozenset[str] = frozenset(
    {
        "detalhes",
        "detalhe",
        "outros",
        "outro",
        "outras informacoes",
        "geral",
        "generico",
        "diversos",
        "misc",
        "miscelanea",
        "informacoes",
        "informacoes adicionais",
        "informacao",
        "dados",
        "conteudo",
        "documento",
        "documentacao",
        "notas",
        "nota",
        "observacoes",
        "observacao",
        "anexo",
        "anexos",
        "apendice",
        "apendices",
        "introducao",
        "conclusao",
        "resumo",
        "sumario",
        "visao geral",
        "overview",
        "descricao",
        "itens",
        "lista",
        "referencias",
        "secao",
        "parte",
        "capitulo",
        "tbd",
        "a definir",
        "sem titulo",
        "untitled",
    }
)

#: Colchete de preenchimento: `[texto explicativo]`, `[...]`, `[]`. Um
#: colchete SEM espaço interno (`list[int]`, `RN-023[0]`) é sintaxe legítima
#: de conteúdo técnico e não é bloqueado — a proibição de §10.3 é sobre campo
#: por preencher, não sobre o caractere.
PLACEHOLDER_RE = re.compile(r"\[\s*(?:\]|\.{2,}\]|…\]|[^\[\]]*\s[^\[\]]*\])")

_NUMERIC_TITLE_RE = re.compile(r"^[\s\d.,;:()\-–—/ivxlcIVXLC]*$")


def normalize_title(text: str) -> str:
    """Forma comparável do título: sem acento, minúsculo, espaço colapsado."""
    stripped = unicodedata.normalize("NFKD", (text or "").strip())
    ascii_form = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"[\s_]+", " ", ascii_form.lower()).strip(" .:-–—")


def is_generic_title(text: str) -> bool:
    """D02 — o título responde a alguma pergunta, ou é rótulo de gaveta?

    Rejeita também título vazio, curto demais (< 3 caracteres úteis) e
    puramente numérico/romano ("3", "II", "1.2"), que não identificam assunto
    quando a unidade é recuperada isolada (D09).
    """
    norm = normalize_title(text)
    if len(norm) < 3:
        return True
    if norm in GENERIC_TITLES:
        return True
    if _NUMERIC_TITLE_RE.match(norm):
        return True
    return False


#: Palavras que, num título ou subtítulo, PROMETEM comportamento ao leitor.
#: Um documento intitulado "regras, fluxo, falhas e contratos" assume dívida:
#: quem o abre espera condição, comportamento e exceção, não uma lista de
#: relações. Esta lista é o que torna a promessa verificável em código.
BEHAVIOR_PROMISE_TERMS: frozenset[str] = frozenset(
    {
        "regra",
        "regras",
        "fluxo",
        "fluxos",
        "falha",
        "falhas",
        "comportamento",
        "comportamentos",
        "excecao",
        "excecoes",
        "condicao",
        "condicoes",
        "politica",
        "politicas",
        "validacao",
        "validacoes",
        "criterio",
        "criterios",
    }
)

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def promises_behavior(text: str) -> bool:
    """O rótulo promete comportamento (regra, fluxo, falha, exceção, condição)?

    Comparação por PALAVRA sobre a forma normalizada — "contratos" sozinho não
    promete comportamento (contrato é estrutura), "regras" promete.
    """
    words = {w for w in _WORD_SPLIT_RE.split(normalize_title(text)) if w}
    return bool(words & BEHAVIOR_PROMISE_TERMS)


def assert_specific_title(text: str, where: str) -> str:
    """Valida D02 e devolve o título original (sem reescrever — D13)."""
    if is_generic_title(text):
        raise GenericTitle(
            f"{where}: título {text!r} é genérico ou não identifica o assunto (D02); "
            "use a operação, a regra ou a pergunta respondida"
        )
    return text


def find_placeholders(text: str) -> list[str]:
    """Trechos entre colchetes que caracterizam campo por preencher (§10.3)."""
    return PLACEHOLDER_RE.findall(text or "")


def assert_no_placeholder(text: str, where: str) -> str:
    """Rejeita `[campo por preencher]` em conteúdo final (§10.3)."""
    found = find_placeholders(text)
    if found:
        raise PlaceholderContent(
            f"{where}: conteúdo final com campo entre colchetes {found[0]!r} (§10.3); "
            "colchete é notação explicativa do plano, não texto publicável"
        )
    return text


# --------------------------------------------------------------------------
# Identidade estável da unidade
# --------------------------------------------------------------------------

#: Chaves de assunto reconhecidas. São MÁQUINA: o texto do assunto pode ser
#: reescrito sem trocar o `unit_id`, porque o id deriva desta chave.
SUBJECT_COMPORTAMENTO = "comportamento"
SUBJECT_PROPOSTA = "proposta"
SUBJECT_HISTORICO = "historico"
SUBJECT_LACUNA = "lacuna"
SUBJECT_CONTEXTO = "mini_contexto"
SUBJECT_COMPOSICAO = "composicao"

#: Assunto de cada estado no fluxo padrão de `units_for_entity`.
_STATE_SUBJECT: dict[UnitState, str] = {
    UnitState.IMPLEMENTED: SUBJECT_COMPORTAMENTO,
    UnitState.PROPOSED: SUBJECT_PROPOSTA,
    UnitState.HISTORICAL: SUBJECT_HISTORICO,
    UnitState.UNRESOLVED: SUBJECT_LACUNA,
}


def unit_id(namespace: str, entity_id: str, subject_key: str, state: UnitState) -> str:
    """`unit_id` determinístico de (namespace, entidade, assunto, estado).

    NÃO entra posição, índice, título nem nome de arquivo: §10.1 exige id
    estável enquanto a unidade mantém identidade, e §10.6 exige que a mesma
    unidade não vire duplicata concorrente na busca a cada geração.
    """
    payload = "\x1f".join(
        (
            identity.namespace_salt(namespace),
            entity_id,
            (subject_key or "").strip(),
            state.value,
        )
    )
    return "unt_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def document_id(namespace: str, doc_kind: DocKind, anchor_entity_id: str, discriminator: str = "") -> str:
    """`document_id` determinístico; independe de título e de caminho."""
    payload = "\x1f".join(
        (identity.namespace_salt(namespace), doc_kind.value, anchor_entity_id, discriminator)
    )
    return "doc_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# Peças da unidade
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Belonging:
    """§10.3 item 1 — sistema/capacidade/iniciativa a que a unidade pertence.

    Guardado como par (id, título) porque D08 exige relação em linguagem
    natural E identificador, e D09 proíbe depender do nome do arquivo para
    saber de que assunto a unidade trata.
    """

    system_id: str | None = None
    system_title: str | None = None
    capability_id: str | None = None
    capability_title: str | None = None
    initiative_id: str | None = None
    initiative_title: str | None = None

    def is_empty(self) -> bool:
        return not (self.system_id or self.capability_id or self.initiative_id)

    def pairs(self) -> tuple[tuple[str, str, str], ...]:
        """Trios (rótulo, título, id) em ordem fixa — renderização determinística."""
        out: list[tuple[str, str, str]] = []
        if self.system_id:
            out.append(("Sistema", self.system_title or self.system_id, self.system_id))
        if self.capability_id:
            out.append(("Capacidade", self.capability_title or self.capability_id, self.capability_id))
        if self.initiative_id:
            out.append(("Iniciativa", self.initiative_title or self.initiative_id, self.initiative_id))
        return tuple(out)


@dataclass(frozen=True)
class Statement:
    """Um fato projetado para publicação, com o `value` INTACTO.

    `value` é copiado byte a byte do fato (D12/D13): número, comparador,
    negação, unidade e precedência não sobrevivem a paráfrase. Nenhum método
    desta classe reescreve `value`; a renderização só o envolve.
    """

    fact_id: str
    predicate: str
    value: str
    scope: str
    state: UnitState
    nature: FactNature
    epistemic_status: EpistemicStatus
    lifecycle_status: LifecycleStatus
    revision_id: str
    evidence_ids: tuple[str, ...] = ()
    source_version_id: str | None = None

    @classmethod
    def from_fact(cls, fact: Fact) -> "Statement":
        return cls(
            fact_id=fact.fact_id,
            predicate=fact.predicate,
            value=fact.value,
            scope=fact.scope,
            state=fact_state(fact),
            nature=fact.nature,
            epistemic_status=fact.epistemic_status,
            lifecycle_status=fact.lifecycle_status,
            revision_id=fact.revision_id,
            evidence_ids=tuple(fact.evidence_refs),
            source_version_id=fact.source_version_id,
        )


#: Frase natural por (tipo de relação, direção) — D08 exige a relação
#: explicada, não só o identificador. `{a}` é a entidade da unidade, `{b}` a
#: outra ponta.
RELATION_PHRASES: dict[tuple[RelationType, str], str] = {
    (RelationType.CONTAINS, "out"): "{a} contém {b}",
    (RelationType.CONTAINS, "in"): "{a} faz parte de {b}",
    (RelationType.CALLS, "out"): "{a} chama {b}",
    (RelationType.CALLS, "in"): "{a} é chamado por {b}",
    (RelationType.READS, "out"): "{a} lê {b}",
    (RelationType.READS, "in"): "{a} é lido por {b}",
    (RelationType.WRITES, "out"): "{a} grava em {b}",
    (RelationType.WRITES, "in"): "{a} recebe gravação de {b}",
    (RelationType.PUBLISHES, "out"): "{a} publica {b}",
    (RelationType.PUBLISHES, "in"): "{a} é publicado por {b}",
    (RelationType.CONSUMES, "out"): "{a} consome {b}",
    (RelationType.CONSUMES, "in"): "{a} é consumido por {b}",
    (RelationType.DEPENDS_ON, "out"): "{a} depende de {b}",
    (RelationType.DEPENDS_ON, "in"): "{b} depende de {a}",
    (RelationType.IMPLEMENTS, "out"): "{a} implementa {b}",
    (RelationType.IMPLEMENTS, "in"): "{a} é implementado por {b}",
    (RelationType.VERIFIES, "out"): "{a} verifica {b}",
    (RelationType.VERIFIES, "in"): "{a} é verificado por {b}",
    (RelationType.RECORDS, "out"): "{a} registra {b}",
    (RelationType.RECORDS, "in"): "{a} é registrado por {b}",
    (RelationType.BELONGS_TO, "out"): "{a} pertence a {b}",
    (RelationType.BELONGS_TO, "in"): "{b} pertence a {a}",
    (RelationType.REFINES, "out"): "{a} detalha {b}",
    (RelationType.REFINES, "in"): "{a} é detalhado por {b}",
    (RelationType.PROPOSES_CHANGE_TO, "out"): "{a} propõe mudança em {b}",
    (RelationType.PROPOSES_CHANGE_TO, "in"): "{a} tem mudança proposta por {b}",
    (RelationType.CONTRADICTS, "out"): "{a} contradiz {b}",
    (RelationType.CONTRADICTS, "in"): "{a} é contradito por {b}",
    (RelationType.SUPERSEDES, "out"): "{a} substitui {b}",
    (RelationType.SUPERSEDES, "in"): "{a} foi substituído por {b}",
    (RelationType.DERIVED_FROM, "out"): "{a} deriva de {b}",
    (RelationType.DERIVED_FROM, "in"): "{b} deriva de {a}",
}


@dataclass(frozen=True)
class RelationRef:
    """§10.3 item 7 — relação em linguagem natural E por identificador."""

    relation_id: str
    relation_type: RelationType
    direction: str  # "out" | "in"
    other_entity_id: str
    other_title: str
    other_entity_type: EntityType
    natural_text: str
    lifecycle_status: LifecycleStatus
    epistemic_status: EpistemicStatus
    revision_id: str
    evidence_ids: tuple[str, ...] = ()

    @classmethod
    def from_relation(
        cls,
        relation: Relation,
        anchor_entity_id: str,
        anchor_title: str,
        other_title: str,
        other_entity_type: EntityType,
    ) -> "RelationRef":
        direction = "out" if relation.source_entity_id == anchor_entity_id else "in"
        other_id = (
            relation.target_entity_id if direction == "out" else relation.source_entity_id
        )
        template = RELATION_PHRASES.get(
            (relation.relation_type, direction),
            "{a} tem relação " + relation.relation_type.value + " com {b}",
        )
        text = template.format(a=anchor_title, b=other_title)
        if relation.lifecycle_status is LifecycleStatus.PROPOSED:
            text += " (relação proposta, ainda não confirmada por implementação)"
        elif relation.lifecycle_status is not LifecycleStatus.CURRENT:
            text += f" (relação {relation.lifecycle_status.value})"
        if relation.epistemic_status is not EpistemicStatus.SUPPORTED:
            text += f" (sustentação {relation.epistemic_status.value})"
        return cls(
            relation_id=relation.relation_id,
            relation_type=relation.relation_type,
            direction=direction,
            other_entity_id=other_id,
            other_title=other_title,
            other_entity_type=other_entity_type,
            natural_text=text,
            lifecycle_status=relation.lifecycle_status,
            epistemic_status=relation.epistemic_status,
            revision_id=relation.revision_id,
            evidence_ids=tuple(relation.evidence_refs),
        )


@dataclass(frozen=True)
class EvidenceRef:
    """§10.3 item 8 — evidência PRÓXIMA, com versão exata da fonte.

    `display` é referência textual (arquivo:linha, caso de teste, bloco de
    transcrição). D15: nunca é apresentada como link resolvível; o renderizador
    não constrói hyperlink a partir deste campo.
    """

    evidence_id: str
    source_kind: SourceKind
    content_kind: ContentKind
    source_version_id: str
    display: str
    supports_implemented: bool

    @classmethod
    def from_evidence(cls, ev: Evidence) -> "EvidenceRef":
        return cls(
            evidence_id=ev.evidence_id,
            source_kind=ev.source_kind,
            content_kind=ev.content_kind,
            source_version_id=ev.source_version_id,
            display=describe_locator(ev.source_kind, ev.locator),
            supports_implemented=ev.content_kind
            in (ContentKind.EXECUTABLE, ContentKind.CONFIG_VALUE),
        )


def describe_locator(source_kind: SourceKind, locator: Mapping[str, Any]) -> str:
    """Referência textual do localizador, por tipo de fonte (§5.4).

    Usa apenas parênteses: colchete em conteúdo final é bloqueado por §10.3, e
    a citação faz parte do conteúdo final.
    """
    g = lambda k: locator.get(k)  # noqa: E731 - leitura local, sem efeito
    if source_kind is SourceKind.CODE:
        base = f"{g('path')}:{g('start_line')}-{g('end_line')}"
        extra = [f"repo {g('repo')}", f"commit {g('commit')}"]
        if g("symbol"):
            extra.append(f"símbolo {g('symbol')}")
        if g("snippet_hash"):
            extra.append(f"hash do trecho {g('snippet_hash')}")
        return f"{base} ({', '.join(extra)})"
    if source_kind is SourceKind.CONFIG:
        parts = [f"{g('file')} chave {g('key')}", f"versão {g('version')}"]
        if g("value_masked") is not None:
            parts.append(f"valor mascarado {g('value_masked')}")
        if g("condition"):
            parts.append(f"condição {g('condition')}")
        if g("environment"):
            parts.append(f"ambiente {g('environment')}")
        return f"{parts[0]} ({', '.join(parts[1:])})"
    if source_kind is SourceKind.TEST:
        asserts = ", ".join(str(a) for a in (g("assertions") or ()))
        parts = [f"versão {g('version')}", f"assertions {asserts}"]
        parts.append("executado" if g("executed") else "não executado")
        if g("execution_condition"):
            parts.append(f"condição de execução {g('execution_condition')}")
        if g("mocks"):
            parts.append(f"mocks {', '.join(str(m) for m in g('mocks'))}")
        return f"caso de teste {g('case')} ({'; '.join(parts)})"
    if source_kind is SourceKind.DOCUMENT:
        parts = [f"versão {g('version')}", f"seção {g('section')}", f"bloco {g('block')}"]
        if g("paragraph"):
            parts.append(f"parágrafo {g('paragraph')}")
        if g("page"):
            parts.append(f"página {g('page')}")
        return "documento (" + ", ".join(parts) + ")"
    if source_kind is SourceKind.TRANSCRIPT:
        parts = [f"versão {g('version')}", f"bloco {g('block')}"]
        if g("time_start"):
            parts.append(f"tempo {g('time_start')}–{g('time_end')}")
        if g("speaker"):
            parts.append(f"interlocutor {g('speaker')}")
        return f"transcrição {g('file')} ({', '.join(parts)})"
    parts = [
        f"origem {g('origin')}",
        f"instante {g('instant')}",
        f"escopo {g('scope')}",
        f"execução {g('execution_id')}",
    ]
    return "observação (" + ", ".join(parts) + ")"


@dataclass(frozen=True)
class CrossRef:
    """Ponteiro rotulado entre estados (D10/D16).

    Existe justamente para NÃO copiar o texto da proposta para dentro do bloco
    implementado: o leitor sabe que há mudança proposta, sem que a proposta
    passe a ler-se como comportamento atual.
    """

    label: str
    target_unit_id: str
    target_state: UnitState
    target_entity_id: str
    text: str
    relation_id: str | None = None


# --------------------------------------------------------------------------
# Unidade semântica (§10.3)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SemanticUnit:
    """Unidade compreensível quando recuperada ISOLADA (§10.3).

    Invariantes verificados na construção (`__post_init__`):
    D02 (título específico), §10.3 (sem colchete de preenchimento) e D10
    (nenhum `Statement` de estado diferente do estado da unidade).
    """

    unit_id: str
    title: str
    state: UnitState
    subject: str
    subject_key: str
    belonging: Belonging
    entity_id: str
    entity_type: EntityType
    namespace: str
    revision_id: str
    conditions: tuple[Statement, ...] = ()
    behavior: tuple[Statement, ...] = ()
    exceptions: tuple[Statement, ...] = ()
    limitations: tuple[Statement, ...] = ()
    gaps: tuple[Statement, ...] = ()
    relations: tuple[RelationRef, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    notes: tuple[str, ...] = ()
    cross_refs: tuple[CrossRef, ...] = ()
    shared_context: bool = False
    source_version_ids: tuple[str, ...] = ()

    # ---------------------------------------------------------- invariantes

    def __post_init__(self) -> None:
        assert_specific_title(self.title, f"unidade {self.unit_id}")
        assert_no_placeholder(self.title, f"título da unidade {self.unit_id}")
        assert_no_placeholder(self.subject, f"assunto da unidade {self.unit_id}")
        for group in (self.conditions, self.behavior, self.exceptions, self.limitations, self.gaps):
            for st in group:
                assert_no_placeholder(st.value, f"fato {st.fact_id} na unidade {self.unit_id}")
        for rel in self.relations:
            assert_no_placeholder(rel.natural_text, f"relação {rel.relation_id}")
        for note in self.notes:
            assert_no_placeholder(note, f"nota da unidade {self.unit_id}")
        for st in self.behavior + self.exceptions:
            if st.state is not self.state:
                raise MixedStateBlock(
                    f"unidade {self.unit_id} está em estado {self.state.value} mas carrega o fato "
                    f"{st.fact_id} em estado {st.state.value} (D10: comportamento implementado e "
                    "proposta não compartilham bloco)"
                )

    # ------------------------------------------------------------ conteúdo

    def statements(self) -> tuple[Statement, ...]:
        return self.conditions + self.behavior + self.exceptions + self.limitations + self.gaps

    @property
    def fact_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(st.fact_id for st in self.statements()))

    @property
    def relation_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(r.relation_id for r in self.relations))

    def fact_states(self) -> dict[str, str]:
        """`fact_id` → ciclo de vida preservado (§10.1: mesmos IDs e estado)."""
        return {st.fact_id: st.lifecycle_status.value for st in self.statements()}

    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(e.evidence_id for e in self.evidence))

    # -------------------------------------------------- espécie de conteúdo

    def sustained_statements(self) -> tuple[Statement, ...]:
        """Condição/comportamento/exceção COM evidência própria.

        `limitations` e `gaps` ficam de fora de propósito: limitação e lacuna
        descrevem o que NÃO se sabe; elas nunca respondem "o que o sistema
        faz". Evidência é exigida no próprio `Statement` (não na unidade):
        evidência que sustenta uma relação não sustenta uma regra.
        """
        return tuple(
            st
            for st in (self.conditions + self.behavior + self.exceptions)
            if st.evidence_ids
        )

    def behavioral_statements(self) -> tuple[Statement, ...]:
        """Statements que REALMENTE descrevem comportamento (`is_behavioral_statement`).

        Subconjunto estrito de `sustained_statements()`: além de evidência,
        exige predicado comportamental e conteúdo com condição e efeito. A
        diferença entre os dois conjuntos é exatamente o achado bloqueante nº2
        — `implemented = true` está no primeiro e nunca no segundo.
        """
        return tuple(
            st
            for st in (self.conditions + self.behavior + self.exceptions)
            if is_behavioral_statement(st)
        )

    def structural_statements(self) -> tuple[Statement, ...]:
        """Statements com evidência que, ainda assim, só afirmam estrutura."""
        behavioral = {id(st) for st in self.behavioral_statements()}
        return tuple(st for st in self.statements() if id(st) not in behavioral)

    @property
    def content_grade(self) -> ContentGrade:
        """Espécie de conteúdo desta unidade (ver `ContentGrade`)."""
        if self.behavioral_statements():
            return ContentGrade.BEHAVIORAL
        if self.statements() or self.relations:
            return ContentGrade.STRUCTURAL
        return ContentGrade.EMPTY

    def is_behavioral(self) -> bool:
        return self.content_grade is ContentGrade.BEHAVIORAL

    def declares_gaps(self) -> bool:
        """Há bloco explícito de lacuna (o que falta e em que pé está)?"""
        return bool(self.gaps)

    def is_publishable(self, doc_kind: "DocKind | None" = None) -> bool:
        """Tem resposta, ou só carimbo de pertencimento? (§10.2)

        Título, pertencimento, escopo e versão são MOLDURA. Uma unidade que só
        tem moldura não responde nada quando recuperada isolada e, publicada,
        vira ruído que compete na busca com a unidade que responde.

        `doc_kind` opcional acrescenta a SUFICIÊNCIA por espécie de conteúdo:
        num documento cujo contrato de leitura é comportamento
        (`BEHAVIOR_REQUIRED_KINDS`), unidade apenas estrutural só é publicável
        quando declara lacuna — sem lacuna declarada ela seria publicada como
        se fosse a resposta, e é exatamente esse silêncio que fez 40 células
        sem avaliação passarem por conteúdo. Sem `doc_kind` a resposta é a
        histórica: unidade estrutural continua publicável (o rótulo é que não
        pode prometer comportamento).
        """
        if not (self.behavior or self.exceptions or self.gaps or self.relations):
            return False
        if doc_kind is not None and doc_kind in BEHAVIOR_REQUIRED_KINDS:
            if self.content_grade is not ContentGrade.BEHAVIORAL and not self.declares_gaps():
                return False
        return True

    def unpublishable_reason(self, doc_kind: "DocKind | None" = None) -> str:
        if self.is_publishable(doc_kind):
            return ""
        if not (self.behavior or self.exceptions or self.gaps or self.relations):
            return (
                f"unidade {self.unit_id} ({self.title!r}) sem conteúdo útil: só título e "
                "pertencimento, sem comportamento, exceção, lacuna ou relação (§10.2)"
            )
        return (
            f"unidade {self.unit_id} ({self.title!r}) é {self.content_grade.value} num documento "
            f"{doc_kind.value if doc_kind else ''} que promete comportamento: não há condição, "
            "comportamento ou exceção com conteúdo real (condição e efeito) sustentada por "
            "evidência, e nenhuma lacuna foi declarada dizendo o que falta e em que pé está a "
            "investigação"
        )


# --------------------------------------------------------------------------
# Documento (§10.1)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeDocument:
    """Representação intermediária comum a Markdown e Word (§10.1)."""

    document_id: str
    title: str
    doc_kind: DocKind
    units: tuple[SemanticUnit, ...]
    revision_id: str
    namespace: str
    anchor_entity_id: str
    anchor_entity_type: EntityType
    summary: str = ""
    #: Quanto da análise de comportamento foi feita. `None` = não declarado —
    #: campo ADITIVO: documento antigo continua construível sem informá-lo, e
    #: `effective_analysis_state()` deriva o valor das próprias unidades.
    analysis_state: AnalysisState | None = None
    #: Bloco de lacunas do DOCUMENTO: o que falta e em que pé está a
    #: investigação, em texto publicável. Vazio só é honesto quando o estado da
    #: análise é `completo`.
    analysis_gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        assert_specific_title(self.title, f"documento {self.document_id}")
        assert_no_placeholder(self.title, f"título do documento {self.document_id}")
        assert_no_placeholder(self.summary, f"resumo do documento {self.document_id}")
        for gap in self.analysis_gaps:
            assert_no_placeholder(gap, f"lacuna declarada do documento {self.document_id}")
        allowed = ALLOWED_ANCHORS[self.doc_kind]
        if self.anchor_entity_type not in allowed:
            raise InvalidDocumentGrouping(
                f"documento {self.doc_kind.value} ancorado em {self.anchor_entity_type.value}; "
                f"aceitos: {', '.join(sorted(t.value for t in allowed))} (§10.2: sem documento "
                "por classe, chunk ou etapa operacional)"
            )
        seen: set[str] = set()
        for u in self.units:
            if u.unit_id in seen:
                raise InvalidDocumentGrouping(
                    f"unidade {u.unit_id} repetida no documento {self.document_id}"
                )
            seen.add(u.unit_id)
            if u.revision_id != self.revision_id:
                raise InvalidDocumentGrouping(
                    f"unidade {u.unit_id} é da revisão {u.revision_id}, documento é da "
                    f"{self.revision_id} (§10.2: mini-contexto sempre da mesma revisão)"
                )

    def is_publishable(self) -> bool:
        """§10.2 — documento sem informação útil não é publicado.

        Unidade de mini-contexto (`shared_context`) NÃO conta: um documento
        feito só de contexto repetido não acrescenta conhecimento.
        """
        return any(u.is_publishable() for u in self.units if not u.shared_context)

    def publishable_units(self) -> tuple[SemanticUnit, ...]:
        return tuple(u for u in self.units if u.is_publishable())

    # ------------------------------------------------- suficiência (R2/§10.2)

    def own_units(self) -> tuple[SemanticUnit, ...]:
        """Unidades publicáveis que são conteúdo PRÓPRIO deste documento.

        Mini-contexto fica de fora: ele é contexto repetido de outro assunto e
        nunca poderia responder pelo comportamento prometido no título.
        """
        return tuple(u for u in self.units if u.is_publishable() and not u.shared_context)

    def behavioral_units(self) -> tuple[SemanticUnit, ...]:
        return tuple(u for u in self.own_units() if u.is_behavioral())

    def unevaluated_units(self) -> tuple[SemanticUnit, ...]:
        """Unidades próprias sem comportamento sustentado — o que o título
        promete e o corpo ainda não responde."""
        return tuple(u for u in self.own_units() if not u.is_behavioral())

    def has_behavioral_statement(self) -> bool:
        """Existe ≥1 `Statement` behavioral pela regra de `is_behavioral_statement`?"""
        return any(u.behavioral_statements() for u in self.own_units())

    def derived_analysis_state(self) -> AnalysisState:
        """Estado da análise deduzido do conteúdo, sem depender de declaração.

        REGRA DURA do achado nº2: nenhum documento sai `completo` se nenhuma
        unidade sua é behavioral. Antes bastava um `Statement` com evidência —
        `implemented = true` satisfazia isso e o contrato saía `completo` ao
        lado de uma capacidade `parcial`, sobre o mesmo conjunto.
        """
        own = self.own_units()
        if not own:
            return AnalysisState.ESTRUTURAL
        if not self.behavioral_units() or not self.has_behavioral_statement():
            return AnalysisState.ESTRUTURAL
        if self.unevaluated_units():
            return AnalysisState.PARCIAL
        return AnalysisState.COMPLETO

    def effective_analysis_state(self) -> AnalysisState:
        """O declarado quando existe; o derivado quando não (nunca "presumido
        completo": presumir completude é o defeito que se está corrigindo).

        A declaração VENCE, com um teto: `completo` declarado sem nenhuma
        unidade behavioral é rebaixado para o derivado. Quem investigou pode
        dizer que parou no meio; não pode declarar completude que o corpo do
        documento não sustenta.
        """
        declared = self.analysis_state
        if declared is None:
            return self.derived_analysis_state()
        if declared is AnalysisState.COMPLETO and not self.behavioral_units():
            return self.derived_analysis_state()
        return declared

    def title_promises_behavior(self) -> bool:
        return promises_behavior(self.title)

    def requires_behavior(self) -> bool:
        """O contrato de leitura deste documento é comportamento?"""
        return self.doc_kind in BEHAVIOR_REQUIRED_KINDS or self.title_promises_behavior()

    def declares_gaps(self) -> bool:
        """Lacuna visível: no bloco do documento OU no bloco de alguma unidade."""
        return bool(self.analysis_gaps) or any(u.declares_gaps() for u in self.units)

    def sufficiency_problems(self) -> tuple[str, ...]:
        """Motivos pelos quais este documento NÃO pode ser publicado como está.

        Regra executável do achado bloqueante nº2 (Word "regras, fluxo, falhas
        e contratos" com só `implemented = true` estrutural):

        1. título promete comportamento e nenhuma unidade é behavioral →
           só passa com `analysis_state` declarado ≠ `completo` E lacuna visível;
        2. `analysis_state = completo` declarado com unidade sem comportamento
           avaliado e sem lacuna → completude falsa;
        3. documento de espécie que exige comportamento, sem nenhuma unidade
           behavioral e sem lacuna nenhuma → matriz não avaliada publicada como
           se fosse resposta.
        """
        problems: list[str] = []
        state = self.effective_analysis_state()
        behavioral = self.behavioral_units()
        unevaluated = self.unevaluated_units()

        if self.title_promises_behavior() and not behavioral:
            if self.analysis_state is None or self.analysis_state is AnalysisState.COMPLETO:
                problems.append(
                    f"título {self.title!r} promete comportamento (regra, fluxo, falha, exceção ou "
                    "condição) e nenhuma unidade tem condição/comportamento/exceção sustentada por "
                    "evidência; o documento não declara analysis_state diferente de completo"
                )
            elif not self.declares_gaps():
                problems.append(
                    f"título {self.title!r} promete comportamento, o documento declara "
                    f"analysis_state {self.analysis_state.value} e não publica nenhuma lacuna "
                    "dizendo o que falta e em que pé está a investigação"
                )

        if self.analysis_state is AnalysisState.COMPLETO and not behavioral:
            problems.append(
                "documento declara analysis_state completo sem NENHUMA unidade behavioral: o "
                "que está publicado é estrutura (existência, identidade, relações) com "
                "evidência, não condição, comportamento ou exceção"
            )

        if self.analysis_state is AnalysisState.COMPLETO and unevaluated:
            missing = [u for u in unevaluated if not u.declares_gaps()]
            if missing:
                problems.append(
                    "documento declara analysis_state completo, mas "
                    f"{len(missing)} unidade(s) não têm comportamento avaliado nem lacuna "
                    "declarada: " + ", ".join(u.unit_id for u in missing)
                )

        if self.doc_kind in BEHAVIOR_REQUIRED_KINDS and not behavioral and not self.declares_gaps():
            problems.append(
                f"documento {self.doc_kind.value} sem nenhuma unidade behavioral e sem lacuna "
                "declarada: estrutura e identidade publicadas no lugar da resposta que a espécie "
                "documental promete (§10.2)"
            )

        if state is not AnalysisState.COMPLETO and not self.declares_gaps():
            problems.append(
                f"estado da análise é {state.value} e nenhuma lacuna está publicada no corpo: o "
                "leitor não tem como distinguir ausência de comportamento de ausência de análise"
            )
        return tuple(dict.fromkeys(problems))

    def assert_publishable(self) -> "KnowledgeDocument":
        """Guarda explícita para quem promove a revisão (§10.6).

        `is_publishable()` responde; esta levanta. Existe para o caminho de
        promoção, onde publicar um documento vazio é erro, não decisão.
        """
        if not self.is_publishable():
            raise EmptyDocument(
                f"documento {self.document_id} ({self.title!r}) não tem unidade publicável "
                "além do mini-contexto; §10.2: documento sem informação útil não é publicado"
            )
        return self

    def system_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                u.belonging.system_id for u in self.units if u.belonging.system_id is not None
            )
        )

    def fact_ids(self) -> tuple[str, ...]:
        out: list[str] = []
        for u in self.units:
            out.extend(u.fact_ids)
        return tuple(dict.fromkeys(out))

    def relation_ids(self) -> tuple[str, ...]:
        out: list[str] = []
        for u in self.units:
            out.extend(u.relation_ids)
        return tuple(dict.fromkeys(out))

    def states(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(u.state.value for u in self.units))

    # --------------------------------------------------------- construção

    @classmethod
    def from_revision(
        cls,
        repo: Any,
        revision_id: str,
        namespace: str,
        doc_kind: DocKind,
        anchor_entity_id: str,
        member_entity_ids: Sequence[str] = (),
        title: str | None = None,
        summary: str = "",
        scope: "RevisionScope | None" = None,
        context_units: Sequence[SemanticUnit] = (),
        skipped: list[tuple[str, str]] | None = None,
        discriminator: str = "",
        state_filter: Sequence[UnitState] | None = None,
    ) -> "KnowledgeDocument":
        """Monta o documento a partir de fatos/relações/evidências/lacunas da MESMA revisão.

        `scope` (`RevisionScope`) é o que garante "mesma revisão": qualquer
        fato/relação cuja revisão de cabeça seja POSTERIOR à revisão de
        publicação fica de fora, com motivo em `skipped`. Sem esse filtro, uma
        ingestão concorrente entraria em metade do documento (§10.6:
        consumidores não recebem metade da atualização).
        """
        rev_scope = scope or RevisionScope(repo, revision_id)
        anchor = _require_entity(repo, anchor_entity_id)
        ids: list[str] = [anchor_entity_id]
        ids.extend(e for e in member_entity_ids if e != anchor_entity_id)
        units: list[SemanticUnit] = list(context_units)
        for eid in dict.fromkeys(ids):
            entity = repo.get_entity(eid, lifecycle=None)
            if entity is None:
                _skip(skipped, eid, "entidade inexistente no repositório")
                continue
            belonging = resolve_belonging(repo, entity, rev_scope)
            for unit in units_for_entity(repo, entity, rev_scope, belonging, skipped=skipped):
                if state_filter is not None and unit.state not in state_filter:
                    _skip(
                        skipped,
                        unit.unit_id,
                        f"estado {unit.state.value} fora do recorte deste documento",
                    )
                    continue
                units.append(unit)
        units = link_cross_state(units)
        state, gaps = analysis_summary(units)
        doc_title = honest_title(
            doc_kind, anchor.title, title or f"{anchor.title} — {DOC_TITLE_SUFFIX[doc_kind]}", state
        )
        return cls(
            document_id=document_id(namespace, doc_kind, anchor_entity_id, discriminator),
            title=doc_title,
            doc_kind=doc_kind,
            units=tuple(units),
            revision_id=revision_id,
            namespace=namespace,
            anchor_entity_id=anchor_entity_id,
            anchor_entity_type=anchor.entity_type,
            summary=summary,
            analysis_state=state,
            analysis_gaps=gaps,
        )


def analysis_summary(
    units: Sequence[SemanticUnit],
    declared_state: AnalysisState | None = None,
    declared_gaps: Sequence[str] = (),
    investigation_note: str = "",
    inherited_state: AnalysisState | None = None,
    inherited_reason: str = "",
) -> tuple[AnalysisState, tuple[str, ...]]:
    """Estado da análise + bloco de lacunas, derivados das próprias unidades.

    Uma única fonte para `planner.plan` e para `KnowledgeDocument.from_revision`:
    o texto das lacunas nomeia CADA unidade sem comportamento avaliado, com o
    identificador, para que o leitor saiba exatamente qual célula da matriz
    ficou por avaliar — "análise parcial" sem dizer onde é a mesma opacidade
    com outro nome.

    `declared_state`/`declared_gaps` vêm de quem conduziu a investigação e
    vencem a derivação com UM teto: `completo` declarado sem nenhuma unidade
    behavioral é rebaixado (achado bloqueante nº2 — quem investigou pode dizer
    que parou no meio, não pode declarar completude que o corpo não sustenta).

    `inherited_state`/`inherited_reason` são o TETO vindo das obrigações que
    sustentam o documento (um contrato não é mais completo do que a capacidade
    que o expõe). Herança só REBAIXA: nunca eleva um estado derivado.
    """
    own = [u for u in units if u.is_publishable() and not u.shared_context]
    behavioral = [u for u in own if u.is_behavioral()]
    unevaluated = [u for u in own if not u.is_behavioral()]
    has_behavior = bool(behavioral)

    downgraded_declared = False
    if declared_state is not None:
        state = declared_state
        if state is AnalysisState.COMPLETO and not has_behavior:
            state = AnalysisState.ESTRUTURAL
            downgraded_declared = True
    elif not own or not has_behavior:
        state = AnalysisState.ESTRUTURAL
    elif unevaluated:
        state = AnalysisState.PARCIAL
    else:
        state = AnalysisState.COMPLETO

    inherited_applied = False
    if inherited_state is not None and (
        ANALYSIS_SEVERITY[inherited_state] < ANALYSIS_SEVERITY[state]
    ):
        state = inherited_state
        inherited_applied = True

    if state is AnalysisState.COMPLETO and not declared_gaps:
        return state, ()

    lines: list[str] = [assert_no_placeholder(g, "lacuna declarada") for g in declared_gaps]
    if downgraded_declared:
        lines.append(
            "A investigação declarou análise completa, mas nenhuma unidade deste documento tem "
            "condição, comportamento ou exceção com conteúdo real sustentado por evidência: o "
            "único conteúdo evidenciado é estrutural (existência, identidade, relações). O estado "
            "publicado foi rebaixado para estrutural."
        )
    if inherited_applied and inherited_reason:
        lines.append(assert_no_placeholder(inherited_reason, "herança do estado da análise"))
    if state is AnalysisState.ESTRUTURAL and not lines:
        lines.append(
            "Nenhuma unidade deste documento tem condição, comportamento ou exceção com "
            "conteúdo real sustentado por evidência nesta revisão: o que está publicado é "
            "estrutura, identidade e relações — inclusive quando o fato tem evidência, como "
            "uma marca de existência. Regra, fluxo e falha permanecem por investigar."
        )
    for unit in unevaluated:
        lines.append(
            f"{unit.title} (unidade {unit.unit_id}): sem condição, comportamento ou exceção com "
            "conteúdo real (condição e efeito) sustentado por evidência nesta revisão; o que há "
            "é estrutura. Avaliação de comportamento pendente."
        )
    for unit in own:
        for gap in unit.gaps:
            lines.append(
                f"{unit.title} (unidade {unit.unit_id}): ponto não resolvido registrado no fato "
                f"{gap.fact_id}."
            )
    if investigation_note:
        lines.append(assert_no_placeholder(investigation_note, "estado da investigação"))
    else:
        # A frase de completude só pode afirmar "condição, comportamento ou
        # exceção sustentada" quando existe ≥1 unidade behavioral pela regra
        # nova; senão o texto diz o que de fato há (achado nº2).
        lines.append(
            "Estado da investigação nesta revisão: "
            + analysis_state_label(state, has_behavior)
            + "."
        )
    return state, tuple(dict.fromkeys(lines))


#: Sufixo de título por tipo de documento — específico o bastante para D02/D09
#: (o assunto é identificável sem abrir outro documento nem ler o nome do arquivo).
DOC_TITLE_SUFFIX: dict[DocKind, str] = {
    DocKind.VISAO_SISTEMA: "visão do sistema",
    DocKind.CAPACIDADE: "capacidade, regras e contratos",
    DocKind.CONTRATO_DEPENDENCIA: "contrato e dependências",
    DocKind.INICIATIVA: "iniciativa, decisões e refinamentos",
    DocKind.EVOLUCAO: "estado atual e mudança proposta",
}

#: Sufixo HONESTO quando a análise de comportamento não foi feita: descreve o
#: que o documento realmente tem (estrutura, identidade, contratos) e diz, no
#: próprio título, que a parte comportamental está pendente. Nenhum destes
#: sufixos contém palavra de `BEHAVIOR_PROMISE_TERMS` — a promessa some do
#: título junto com o conteúdo que a sustentaria.
STRUCTURAL_TITLE_SUFFIX: dict[DocKind, str] = {
    DocKind.VISAO_SISTEMA: "estrutura do sistema (análise comportamental pendente)",
    DocKind.CAPACIDADE: "estrutura e contratos (análise comportamental pendente)",
    DocKind.CONTRATO_DEPENDENCIA: "interface e dependências (análise comportamental pendente)",
    DocKind.INICIATIVA: "cadeia declarada (análise comportamental pendente)",
    DocKind.EVOLUCAO: "estrutura e proposta registrada (análise comportamental pendente)",
}


def honest_title(
    doc_kind: DocKind, anchor_title: str, proposed_title: str, state: AnalysisState
) -> str:
    """Título que não promete o que o corpo não entrega.

    Só reescreve no caso indefensável: análise `estrutural` (zero unidade com
    comportamento sustentado) sob título que promete regra/fluxo/falha. Estado
    `parcial` mantém o título — há comportamento publicado — e a extensão do
    que falta é dita no bloco de lacunas, não no título.
    """
    if state is not AnalysisState.ESTRUTURAL:
        return proposed_title
    if not promises_behavior(proposed_title):
        return proposed_title
    return f"{anchor_title}: {STRUCTURAL_TITLE_SUFFIX[doc_kind]}"


# --------------------------------------------------------------------------
# Recorte de revisão (§10.2/§10.6: sempre da MESMA revisão)
# --------------------------------------------------------------------------


class RevisionScope:
    """Decide se um registro pertence ao snapshot da revisão de publicação.

    Regra executável: o registro entra quando a revisão que o produziu foi
    criada em instante MENOR OU IGUAL ao da revisão de publicação. Revisão
    desconhecida ou posterior fica de fora — é assim que uma ingestão que
    aconteceu durante a geração não contamina metade dos documentos.

    Empate no mesmo instante entra: `revisions.created_at` tem resolução de
    segundos, e desempatar por `revision_id` (uuid) excluiria registros por
    sorteio, não por critério.
    """

    def __init__(self, repo: Any, revision_id: str) -> None:
        self.repo = repo
        self.revision_id = revision_id
        revision = repo.get_revision(revision_id)
        if revision is None:
            raise PublishingError(
                f"revisão de publicação {revision_id!r} não existe em knowledge.db; "
                "publicação é sempre por revisão (§10.6)"
            )
        self.created_at = revision.created_at
        self._cache: dict[str, bool] = {revision_id: True}

    def contains(self, other_revision_id: str | None) -> bool:
        if not other_revision_id:
            return False
        hit = self._cache.get(other_revision_id)
        if hit is not None:
            return hit
        other = self.repo.get_revision(other_revision_id)
        ok = other is not None and other.created_at <= self.created_at
        self._cache[other_revision_id] = ok
        return ok

    def reason(self, other_revision_id: str | None) -> str:
        return (
            f"revisão {other_revision_id} não pertence ao snapshot de publicação "
            f"{self.revision_id} (§10.2: unidades sempre da mesma revisão)"
        )


# --------------------------------------------------------------------------
# Construção de unidades a partir do grafo
# --------------------------------------------------------------------------

#: Predicados que descrevem CONDIÇÃO de aplicação (§10.3 item 4).
CONDITION_PREDICATES: frozenset[str] = frozenset(
    {"condition", "precondition", "trigger", "when", "applies_when", "condicao", "gatilho"}
)
#: Predicados de EXCEÇÃO/consequência (§10.3 item 6).
EXCEPTION_PREDICATES: frozenset[str] = frozenset(
    {
        "exception",
        "error",
        "failure",
        "edge_case",
        "consequence",
        "partial_effect",
        "excecao",
        "falha",
        "efeito_parcial",
        "consequencia",
    }
)
#: Predicados de LIMITAÇÃO declarada (§10.3 item 9).
LIMITATION_PREDICATES: frozenset[str] = frozenset(
    {"limitation", "boundary", "assumption", "limitacao", "fronteira", "premissa"}
)


# --------------------------------------------------------------------------
# Estrutural × comportamental por PREDICADO (achado bloqueante nº2, 2ª auditoria)
# --------------------------------------------------------------------------
#
# O defeito corrigido aqui: a classificação anterior chamava de "behavioral"
# qualquer condição/comportamento/exceção que TIVESSE evidência. O fato
# `implemented = true` emitido pelo `wk code` tem evidência executável real
# (`FactDraft(predicate="implemented", value="true", evidence_refs=(ev_id,))`)
# e ainda assim não carrega 404/422/409, condição nenhuma e mudança de estado
# nenhuma. Resultado auditado: capacidade saiu `parcial` e o contrato que ela
# sustenta saiu `completo` no mesmo conjunto.
#
# A separação passa a ser por PREDICADO, não por presença de evidência.

#: Predicados ESTRUTURAIS: respondem "existe / como se chama / do que participa",
#: nunca "o que acontece sob que condição". Um `Statement` cujo predicado cai
#: aqui NUNCA é behavioral — COM ou SEM evidência.
STRUCTURAL_PREDICATES: frozenset[str] = frozenset(
    {
        # flag de existência/implementação (o caso do achado nº2)
        "implemented",
        "implementado",
        "implementada",
        "is_implemented",
        "exists",
        "existe",
        "present",
        "presente",
        "enabled",
        "habilitado",
        "active",
        "ativo",
        "deployed",
        "publicado",
        # identidade e nomenclatura
        "name",
        "nome",
        "title",
        "titulo",
        "identity",
        "identidade",
        "kind",
        "tipo",
        "type",
        "framework",
        "language",
        "linguagem",
        "path",
        "caminho",
        "module",
        "modulo",
        "package",
        "pacote",
        "qualname",
        "symbol",
        "simbolo",
        "stable_key",
        "alias",
        "line",
        "linha",
        "signature",
        "assinatura",
        "version",
        "versao",
        "owner",
        "dono",
        "repo",
        "repositorio",
        "url",
        "endpoint",
        "rota",
        "route",
        "entrypoint",
        "entrypoint_kind",
        # espelho de relação: a aresta já é publicada COMO relação; repeti-la
        # como fato não acrescenta comportamento nenhum
        "contains",
        "contem",
        "calls",
        "chama",
        "called_by",
        "chamado_por",
        "reads",
        "le",
        "writes",
        "grava_em",
        "publishes",
        "consumes",
        "consome",
        "depends_on",
        "depende_de",
        "belongs_to",
        "pertence_a",
        "part_of",
        "faz_parte_de",
        "implements",
        "implementa",
        "uses",
        "usa",
        # investigação: identidade, gatilho declarado e dependências são MAPA
        "investigacao.identidade",
        "investigacao.gatilho",
        "investigacao.dependencias",
        "investigacao.entradas",
        "investigacao.saidas",
        "investigacao.escopo",
    }
)

#: Folha estrutural de predicado QUALIFICADO (`<algo>.<folha>`): `capability.
#: implemented`, `contract.implemented`, `investigacao.gatilho` etc. Só vale
#: para predicado com ponto — `gatilho` sozinho continua sendo condição
#: (`CONDITION_PREDICATES`) e é filtrado pelo conteúdo, não pelo nome.
STRUCTURAL_PREDICATE_LEAVES: frozenset[str] = frozenset(
    {
        "implemented",
        "implementado",
        "implementada",
        "exists",
        "existe",
        "identidade",
        "identity",
        "gatilho",
        "trigger",
        "dependencias",
        "dependencies",
        "entradas",
        "saidas",
        "inputs",
        "outputs",
        "escopo",
        "scope",
        "nome",
        "name",
        "titulo",
        "title",
        "tipo",
        "kind",
        "type",
        "path",
        "caminho",
        "rota",
        "route",
        "url",
        "versao",
        "version",
    }
)

#: Termos que tornam um predicado COMPORTAMENTAL: decisão, persistência,
#: falha, sucesso, borda, regra. Casamento por token OU por subcadeia (termos
#: com 5+ caracteres), para pegar `behavior_on_timeout`, `persistencia_pedido`,
#: `regra_de_rejeicao`.
BEHAVIORAL_PREDICATE_TERMS: frozenset[str] = frozenset(
    {
        # regra e comportamento
        "rule", "regra", "regras", "behavior", "behaviour", "comportamento",
        "policy", "politica", "criterio", "criteria", "invariant", "invariante",
        # decisão
        "decision", "decisao", "decide", "decides", "escolhe", "seleciona",
        "authorization", "autorizacao", "permission", "permissao", "auth",
        # persistência e efeito
        "persist", "persiste", "persistencia", "grava", "gravacao", "save",
        "salva", "store", "armazena", "commit", "rollback", "transacao",
        "transaction", "escrita", "atualiza", "atualizacao",
        # falha, exceção, borda
        "fail", "fails", "falha", "falhas", "failure", "error", "erro",
        "exception", "excecao", "edge", "edge_case", "borda", "timeout",
        "retry", "reintento", "degradacao", "fallback",
        # sucesso e resposta
        "success", "sucesso", "accept", "aceita", "reject", "rejeita",
        "recusa", "deny", "nega", "block", "bloqueia", "status", "response",
        "resposta", "returns", "retorna", "result", "resultado",
        # condição, fluxo e limite
        "condition", "condicao", "precondition", "trigger", "when",
        "applies_when", "consequence", "consequencia", "effect", "efeito",
        "partial_effect", "efeito_parcial", "validation", "validacao",
        "valida", "constraint", "restricao", "limit", "limite", "threshold",
        "limiar", "quota", "cota", "rate", "flow", "fluxo", "step", "etapa",
        "transition", "transicao", "state_machine", "idempot", "concorrencia",
        "concurrency", "lock",
    }
)

#: Valores que são FLAG booleana: nunca carregam condição nem efeito, então
#: nunca sustentam comportamento (o `true` de `implemented = true`).
BOOLEAN_FLAG_VALUES: frozenset[str] = frozenset(
    {"true", "false", "sim", "nao", "yes", "no", "1", "0", "verdadeiro", "falso", "on", "off"}
)

#: CONDIÇÃO real no conteúdo: comparador, conectivo condicional ou limiar.
_CONDITION_MARKER_RE = re.compile(
    r"(?:>=|<=|!=|<>|==|≠|≥|≤|>|<)"
    r"|\b(?:se|quando|caso|sempre que|somente se|apenas se|apenas quando|enquanto|"
    r"apos|antes de|desde que|a partir de|acima de|abaixo de|maior que|menor que|"
    r"igual a|diferente de|excede|exceder|ultrapassa|se e somente se|"
    r"if|when|unless|while|whenever|after|before|above|below|greater than|"
    r"less than|exceeds|only if)\b",
    re.I,
)

#: EFEITO/EXCEÇÃO real no conteúdo: consequência, mudança de estado, código de
#: falha. Sem efeito, uma condição isolada ainda não diz o que o sistema faz.
_EFFECT_MARKER_RE = re.compile(
    r"(?:->|=>|→|⇒)"
    r"|\b(?:entao|retorna|retornar|devolve|responde|resposta|grava|gravar|persiste|"
    r"persistir|salva|salvar|registra|armazena|envia|publica|emite|rejeita|recusa|"
    r"nega|bloqueia|aborta|cancela|falha|lanca|levanta|propaga|aplica|atualiza|"
    r"cria|remove|exclui|marca|define|resulta|impede|interrompe|ignora|descarta|"
    r"then|returns|responds|writes|saves|persists|stores|sends|publishes|emits|"
    r"rejects|denies|blocks|aborts|fails|raises|throws|applies|updates|creates|"
    r"deletes|results|skips)\b"
    r"|\b[45]\d{2}\b"
    r"|\b(?:erro|error|excecao|exception|timeout|rollback|http)\b",
    re.I,
)

# Fallback local dos tokens críticos D12 — usado só se `publishing.validate`
# não puder ser importado. As duas pontas precisam concordar, por isso a
# primeira escolha é SEMPRE `validate.critical_tokens`.
_FB_COMPARATOR_RE = re.compile(r"(?:>=|<=|!=|<>|==|≠|≥|≤|>|<|=)")
_FB_NEGATION_RE = re.compile(
    r"\b(n[ãa]o|nunca|jamais|sem|nenhum(?:a|as|os)?|exceto|salvo|inexistente|ausente|"
    r"not|never|no|neither)\b",
    re.I,
)
_FB_NUMBER_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:%|ms|s|seg|segundos?|min|minutos?|h|horas?|d|dias?|"
    r"kb|mb|gb|tb|req/s|rps)?\b",
    re.I,
)
_FB_STATE_RE = re.compile(
    r"\b(implementad[oa]s?|implemented|propost[oa]s?|proposed|hist[óo]ric[oa]s?|"
    r"historical|n[ãa]o[- ]resolvid[oa]s?|unresolved|bloquead[oa]s?)\b",
    re.I,
)

_CRITICAL_TOKENS_FN: Any = None


def _fallback_critical_tokens(value: str) -> list[str]:
    text = re.sub(r"\s+", " ", (value or "")).strip()
    tokens: list[str] = []
    tokens.extend(m.group(0) for m in _FB_COMPARATOR_RE.finditer(text))
    tokens.extend(m.group(0) for m in _FB_NEGATION_RE.finditer(text))
    tokens.extend(re.sub(r"\s+", " ", m.group(0)).strip() for m in _FB_NUMBER_RE.finditer(text))
    tokens.extend(m.group(0) for m in _FB_STATE_RE.finditer(text))
    return list(dict.fromkeys(t for t in tokens if t))


def critical_tokens_of(value: str) -> list[str]:
    """Tokens críticos D12 do valor — `validate.critical_tokens` quando existe.

    Import PREGUIÇOSO de propósito: `validate` importa `markdown`, que importa
    este módulo. Resolvido só na primeira chamada, quando `document` já está
    completamente carregado, o ciclo não existe.
    """
    global _CRITICAL_TOKENS_FN
    if _CRITICAL_TOKENS_FN is None:
        fn = None
        for mod_path in ("publishing.validate", "validate"):
            try:
                mod = __import__(mod_path, fromlist=["critical_tokens"])
                fn = getattr(mod, "critical_tokens", None)
            except Exception:  # pragma: no cover - ambiente sem o módulo irmão
                fn = None
            if fn is not None:
                break
        _CRITICAL_TOKENS_FN = fn or _fallback_critical_tokens
    return list(_CRITICAL_TOKENS_FN(value or ""))


def is_boolean_flag(value: str) -> bool:
    """O valor é uma flag booleana pura (`true`, `sim`, `0`)?"""
    return normalize_title(value) in BOOLEAN_FLAG_VALUES


def normalize_predicate(predicate: str) -> str:
    """Forma comparável do predicado.

    Sem acento, minúsculo, espaço/hífen colapsados em `_` (`depends on`,
    `depends-on` e `depends_on` são o MESMO predicado) e o ponto preservado,
    porque é ele que separa o qualificador da folha em `capability.implemented`.
    """
    stripped = unicodedata.normalize("NFKD", (predicate or "").strip())
    ascii_form = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"[\s\-]+", "_", ascii_form.lower()).strip("_. ")


def is_structural_predicate(predicate: str) -> bool:
    """O predicado descreve ESTRUTURA (existência, identidade, aresta)?

    Nunca depende de evidência: `implemented = true` com evidência executável
    continua sendo estrutura. É esta função que impede um fato estrutural de
    sustentar a frase "condição, comportamento ou exceção sustentada".
    """
    pred = normalize_predicate(predicate)
    if not pred:
        return True
    if pred in STRUCTURAL_PREDICATES:
        return True
    if "." in pred:
        leaf = pred.rsplit(".", 1)[-1].strip("_ ")
        if leaf in STRUCTURAL_PREDICATE_LEAVES:
            return True
    return False


def is_behavioral_predicate(predicate: str) -> bool:
    """O predicado promete decisão, persistência, falha, sucesso, borda ou regra?

    Lista POSITIVA de propósito: predicado desconhecido conta como estrutura,
    porque o erro conservador (deixar de chamar de comportamento algo que era)
    publica um documento honesto a menos, e o erro oposto publica um documento
    que mente. Ampliar `BEHAVIORAL_PREDICATE_TERMS` é aditivo.
    """
    pred = normalize_predicate(predicate)
    if not pred or is_structural_predicate(predicate):
        return False
    if pred in CONDITION_PREDICATES or pred in EXCEPTION_PREDICATES:
        return True
    tokens = [t for t in _WORD_SPLIT_RE.split(pred.replace(".", " ")) if t]
    if any(t in BEHAVIORAL_PREDICATE_TERMS for t in tokens):
        return True
    long_terms = [t for t in BEHAVIORAL_PREDICATE_TERMS if len(t) >= 5]
    return any(term in tok for tok in tokens for term in long_terms)


def has_behavioral_content(value: str) -> bool:
    """O CONTEÚDO carrega condição/comparador E efeito/exceção reais?

    Exige as duas metades: condição sem efeito não diz o que acontece, efeito
    sem condição nem quantidade é slogan. Flag booleana é recusada antes de
    tudo — `true` nunca foi resposta para "o que o sistema faz".
    """
    text = normalize_title(value)
    if not text or is_boolean_flag(value):
        return False
    condition = bool(_CONDITION_MARKER_RE.search(text))
    if not condition:
        # Tokens críticos D12 (negação, número com unidade, nome de estado)
        # também qualificam a condição — `=` sozinho, não: é o comparador de
        # atribuição de `implemented = true`.
        condition = any(tok.strip() not in ("", "=") for tok in critical_tokens_of(value))
    return condition and bool(_EFFECT_MARKER_RE.search(text))


def is_behavioral_statement(statement: "Statement") -> bool:
    """As TRÊS provas do achado nº2, na ordem em que barram mais cedo.

    1. predicado NÃO estrutural e reconhecidamente comportamental;
    2. conteúdo com condição/comparador E efeito/exceção real;
    3. evidência própria no `Statement` (não na unidade: evidência que sustenta
       uma relação não sustenta uma regra).
    """
    if is_structural_predicate(statement.predicate):
        return False
    if not is_behavioral_predicate(statement.predicate):
        return False
    if not has_behavioral_content(statement.value):
        return False
    return bool(statement.evidence_ids)


def fact_state(fact: Fact) -> UnitState:
    """Estado publicável do fato (§10.3 item 3), a partir dos TRÊS eixos.

    A ordem de decisão importa: sustentação frágil vira lacuna antes de
    qualquer outra coisa, porque publicar `disputed`/`unresolved` como
    comportamento seria transformar dúvida em regra. Depois vigência. Só
    então natureza — e `declared_requirement`/`test_expectation` NUNCA caem em
    `implemented`, que é a regra que mantém requisito aprovado fora da
    resposta de "o que o sistema faz" (§5.3).
    """
    if fact.epistemic_status in (EpistemicStatus.UNRESOLVED, EpistemicStatus.DISPUTED):
        return UnitState.UNRESOLVED
    if fact.lifecycle_status in (
        LifecycleStatus.HISTORICAL,
        LifecycleStatus.SUPERSEDED,
        LifecycleStatus.STALE,
    ):
        return UnitState.HISTORICAL
    if fact.lifecycle_status is LifecycleStatus.PROPOSED:
        return UnitState.PROPOSED
    if fact.nature in (FactNature.IMPLEMENTED, FactNature.OBSERVED):
        return UnitState.IMPLEMENTED
    return UnitState.PROPOSED


def block_label(unit: "SemanticUnit") -> str:
    """Rótulo do bloco de conteúdo desta unidade (D10).

    Entidade de intenção em estado `proposed` recebe "conteúdo declarado": o
    objetivo de uma iniciativa não é uma "mudança proposta" no sistema, mas
    também não é comportamento implementado — e o rótulo precisa dizer as duas
    coisas ao mesmo tempo.
    """
    if unit.state is UnitState.PROPOSED and unit.entity_type in INTENT_TYPES:
        return "Conteúdo declarado (ainda não implementado)"
    return STATE_BLOCK_LABEL[unit.state]


def _subject_text(entity: Entity, state: UnitState) -> str:
    intent = entity.entity_type in INTENT_TYPES
    if state is UnitState.IMPLEMENTED:
        return f"Comportamento em vigor de {entity.title}"
    if state is UnitState.PROPOSED:
        if intent:
            return f"O que {entity.title} declara, sem implementação confirmada nesta revisão"
        return f"Mudança proposta para {entity.title}, ainda não implementada"
    if state is UnitState.HISTORICAL:
        return f"Comportamento anterior de {entity.title}, fora de vigência"
    return f"Pontos não resolvidos sobre {entity.title}"


def _title_text(entity: Entity, state: UnitState) -> str:
    intent = entity.entity_type in INTENT_TYPES
    if state is UnitState.IMPLEMENTED:
        return f"{entity.title}: comportamento em vigor"
    if state is UnitState.PROPOSED:
        if intent:
            return f"{entity.title}: conteúdo declarado, sem implementação confirmada"
        return f"{entity.title}: mudança proposta e não implementada"
    if state is UnitState.HISTORICAL:
        return f"{entity.title}: comportamento anterior, fora de vigência"
    return f"{entity.title}: pontos não resolvidos"


def units_for_entity(
    repo: Any,
    entity: Entity,
    scope: RevisionScope,
    belonging: Belonging,
    skipped: list[tuple[str, str]] | None = None,
) -> list[SemanticUnit]:
    """Uma unidade por (entidade, estado) — nunca uma unidade por fato.

    A separação por estado é a aplicação estrutural de D10: `implemented` e
    `proposed` da mesma entidade saem em unidades distintas, cada uma com seu
    rótulo, ligadas por `CrossRef`. Lacuna vira unidade `unresolved` própria,
    com o campo explícito exigido por §10.3 item 9.
    """
    facts = [f for f in repo.facts_for_subject(entity.entity_id, lifecycle=None)]
    kept: list[Fact] = []
    for f in facts:
        if scope.contains(f.revision_id):
            kept.append(f)
        elif skipped is not None:
            skipped.append((f.fact_id, scope.reason(f.revision_id)))
    relations = _relations_of(repo, entity, scope, skipped)
    by_state: dict[UnitState, list[Fact]] = {}
    for f in sorted(kept, key=lambda x: (x.predicate, x.fact_id)):
        by_state.setdefault(fact_state(f), []).append(f)

    # Relações ficam na unidade do estado dominante da entidade: uma relação
    # descreve a entidade, não um estado dela. `proposed` recebe as relações
    # propostas; o restante acompanha a unidade de estado vigente.
    rel_proposed = tuple(r for r in relations if r.lifecycle_status is LifecycleStatus.PROPOSED)
    rel_other = tuple(r for r in relations if r.lifecycle_status is not LifecycleStatus.PROPOSED)
    primary = _primary_state(by_state, relations)

    units: list[SemanticUnit] = []
    for state in (
        UnitState.IMPLEMENTED,
        UnitState.PROPOSED,
        UnitState.HISTORICAL,
        UnitState.UNRESOLVED,
    ):
        group = by_state.get(state, [])
        rels: tuple[RelationRef, ...] = rel_proposed if state is UnitState.PROPOSED else ()
        if state is primary:
            rels = rels + rel_other
        if not group and not rels:
            continue
        units.append(
            _build_unit(
                repo=repo,
                entity=entity,
                state=state,
                facts=group,
                relations=rels,
                belonging=belonging,
                scope=scope,
            )
        )
    return units


def _primary_state(
    by_state: Mapping[UnitState, Sequence[Fact]], relations: Sequence[RelationRef]
) -> UnitState:
    """Estado que representa a entidade hoje — recebe as relações não propostas."""
    for state in (UnitState.IMPLEMENTED, UnitState.PROPOSED, UnitState.UNRESOLVED, UnitState.HISTORICAL):
        if by_state.get(state):
            return state
    return UnitState.IMPLEMENTED


def _build_unit(
    repo: Any,
    entity: Entity,
    state: UnitState,
    facts: Sequence[Fact],
    relations: Sequence[RelationRef],
    belonging: Belonging,
    scope: RevisionScope,
    subject_key: str | None = None,
    title: str | None = None,
    subject: str | None = None,
    shared_context: bool = False,
) -> SemanticUnit:
    conditions: list[Statement] = []
    behavior: list[Statement] = []
    exceptions: list[Statement] = []
    limitations: list[Statement] = []
    gaps: list[Statement] = []
    notes: list[str] = []
    ev_ids: list[str] = []
    src_versions: list[str] = []
    scopes: list[str] = []

    for f in facts:
        st = Statement.from_fact(f)
        pred = f.predicate.strip().lower()
        if state is UnitState.UNRESOLVED:
            gaps.append(st)
        elif pred in CONDITION_PREDICATES:
            conditions.append(st)
        elif pred in EXCEPTION_PREDICATES:
            exceptions.append(st)
        elif pred in LIMITATION_PREDICATES:
            limitations.append(st)
        else:
            behavior.append(st)
        ev_ids.extend(f.evidence_refs)
        if f.source_version_id:
            src_versions.append(f.source_version_id)
        if f.epistemic_status is EpistemicStatus.INFERRED:
            notes.append(
                f"O fato {f.fact_id} está sustentado por inferência, não por evidência direta; "
                "confirmação pode alterar a resposta."
            )
        if f.nature is FactNature.TEST_EXPECTATION:
            notes.append(
                f"O fato {f.fact_id} vem de expectativa de teste: sustenta o cenário modelado, "
                "não o comportamento real do serviço externo."
            )
        if f.scope:
            scopes.append(f.scope)

    if scopes:
        # §5.3: ausência de tratamento no escopo examinado não prova ausência
        # global. A fronteira examinada é limitação que altera a resposta.
        notes.append(
            "Fronteira examinada desta unidade: "
            + ", ".join(dict.fromkeys(scopes))
            + ". Fora dela, ausência de comportamento não foi verificada."
        )

    for rel in relations:
        ev_ids.extend(rel.evidence_ids)

    evidence = _load_evidence(repo, ev_ids)
    if state is UnitState.IMPLEMENTED and behavior and not any(
        e.supports_implemented for e in evidence
    ):
        notes.append(
            "Nenhuma evidência executável ou de configuração acompanha este bloco; o comportamento "
            "descrito não pode ser tratado como confirmado por código."
        )

    key = subject_key or _STATE_SUBJECT[state]
    return SemanticUnit(
        unit_id=unit_id(entity.namespace, entity.entity_id, key, state),
        title=title or _title_text(entity, state),
        state=state,
        subject=subject or _subject_text(entity, state),
        subject_key=key,
        belonging=belonging,
        entity_id=entity.entity_id,
        entity_type=entity.entity_type,
        namespace=entity.namespace,
        revision_id=scope.revision_id,
        conditions=tuple(conditions),
        behavior=tuple(behavior),
        exceptions=tuple(exceptions),
        limitations=tuple(limitations),
        gaps=tuple(gaps),
        relations=tuple(relations),
        evidence=evidence,
        notes=tuple(dict.fromkeys(notes)),
        shared_context=shared_context,
        source_version_ids=tuple(dict.fromkeys(src_versions)),
    )


def _load_evidence(repo: Any, evidence_ids: Iterable[str]) -> tuple[EvidenceRef, ...]:
    out: list[EvidenceRef] = []
    for eid in dict.fromkeys(evidence_ids):
        ev = repo.get_evidence(eid)
        if ev is None:
            continue
        out.append(EvidenceRef.from_evidence(ev))
    return tuple(sorted(out, key=lambda e: e.evidence_id))


def _relations_of(
    repo: Any,
    entity: Entity,
    scope: RevisionScope,
    skipped: list[tuple[str, str]] | None = None,
) -> tuple[RelationRef, ...]:
    rels = repo.neighbors(entity.entity_id, direction="both", lifecycle=ALL_LIFECYCLE)
    out: list[RelationRef] = []
    for rel in rels:
        if not scope.contains(rel.revision_id):
            if skipped is not None:
                skipped.append((rel.relation_id, scope.reason(rel.revision_id)))
            continue
        other_id = (
            rel.target_entity_id
            if rel.source_entity_id == entity.entity_id
            else rel.source_entity_id
        )
        other = repo.get_entity(other_id, lifecycle=None)
        if other is None:
            continue
        out.append(
            RelationRef.from_relation(rel, entity.entity_id, entity.title, other.title, other.entity_type)
        )
    return tuple(sorted(out, key=lambda r: (r.relation_type.value, r.direction, r.relation_id)))


def link_cross_state(units: Sequence[SemanticUnit]) -> list[SemanticUnit]:
    """Liga unidades da MESMA entidade em estados diferentes (D10/D16).

    O bloco implementado ganha um ponteiro "existe proposta"; o bloco proposto
    ganha "estado atual". Nenhum dos dois recebe o conteúdo do outro.
    """
    by_entity: dict[str, dict[UnitState, SemanticUnit]] = {}
    for u in units:
        by_entity.setdefault(u.entity_id, {})[u.state] = u
    out: list[SemanticUnit] = []
    for u in units:
        siblings = by_entity.get(u.entity_id, {})
        refs: list[CrossRef] = []
        for state, other in sorted(siblings.items(), key=lambda kv: kv[0].value):
            if state is u.state:
                continue
            if u.state is UnitState.IMPLEMENTED and state is UnitState.PROPOSED:
                text = (
                    "Existe mudança proposta para este comportamento, publicada em bloco próprio; "
                    "até esta revisão ela não está implementada."
                )
            elif u.state is UnitState.PROPOSED and state is UnitState.IMPLEMENTED:
                text = "O comportamento em vigor nesta revisão está publicado em bloco próprio."
            elif state is UnitState.UNRESOLVED:
                text = "Há pontos não resolvidos sobre esta entidade, publicados em bloco próprio."
            elif state is UnitState.HISTORICAL:
                text = "Há comportamento anterior fora de vigência, publicado em bloco próprio."
            else:
                text = f"Bloco relacionado em estado {STATE_LABEL[state].lower()}."
            refs.append(
                CrossRef(
                    label=block_label(other),
                    target_unit_id=other.unit_id,
                    target_state=state,
                    target_entity_id=other.entity_id,
                    text=text,
                )
            )
        out.append(replace(u, cross_refs=tuple(refs)) if refs else u)
    return out


# --------------------------------------------------------------------------
# Pertencimento (§10.3 item 1)
# --------------------------------------------------------------------------

#: Relações que sobem na hierarquia até System/Capability/Initiative.
_UP_RELATIONS: tuple[RelationType, ...] = (
    RelationType.CONTAINS,
    RelationType.BELONGS_TO,
    RelationType.IMPLEMENTS,
    RelationType.REFINES,
    RelationType.PROPOSES_CHANGE_TO,
    RelationType.DERIVED_FROM,
)

_MAX_DEPTH = 6


def resolve_belonging(repo: Any, entity: Entity, scope: RevisionScope) -> Belonging:
    """Sobe o grafo até achar System, Capability e Initiative da entidade.

    Busca em largura limitada a `_MAX_DEPTH`: pertencimento é contexto de
    leitura, não travessia completa de grafo — e ciclos em `depends_on`/
    `calls` são permitidos pelo modelo (§5.5), então parada por profundidade e
    por visitados é obrigatória.
    """
    found: dict[EntityType, Entity] = {}
    if entity.entity_type in (EntityType.SYSTEM, EntityType.CAPABILITY, EntityType.INITIATIVE):
        found[entity.entity_type] = entity
    seen = {entity.entity_id}
    frontier = [entity.entity_id]
    depth = 0
    while frontier and depth < _MAX_DEPTH:
        depth += 1
        nxt: list[str] = []
        for eid in frontier:
            for rel in repo.neighbors(eid, direction="both", lifecycle=ALL_LIFECYCLE):
                if not scope.contains(rel.revision_id):
                    continue
                if rel.relation_type not in _UP_RELATIONS:
                    continue
                other_id = rel.target_entity_id if rel.source_entity_id == eid else rel.source_entity_id
                if other_id in seen:
                    continue
                seen.add(other_id)
                other = repo.get_entity(other_id, lifecycle=None)
                if other is None:
                    continue
                if other.entity_type in (
                    EntityType.SYSTEM,
                    EntityType.CAPABILITY,
                    EntityType.INITIATIVE,
                ) and other.entity_type not in found:
                    found[other.entity_type] = other
                nxt.append(other_id)
        frontier = sorted(nxt)
    sys_e = found.get(EntityType.SYSTEM)
    cap_e = found.get(EntityType.CAPABILITY)
    ini_e = found.get(EntityType.INITIATIVE)
    return Belonging(
        system_id=sys_e.entity_id if sys_e else None,
        system_title=sys_e.title if sys_e else None,
        capability_id=cap_e.entity_id if cap_e else None,
        capability_title=cap_e.title if cap_e else None,
        initiative_id=ini_e.entity_id if ini_e else None,
        initiative_title=ini_e.title if ini_e else None,
    )


def context_unit(
    repo: Any, entity: Entity, scope: RevisionScope, belonging: Belonging
) -> SemanticUnit | None:
    """Mini-contexto repetível da entidade (§10.2: catálogo compartilhado).

    Repetição controlada e SEMPRE da mesma revisão: a unidade tem o mesmo
    `unit_id` em todos os documentos onde aparece, então o manifesto sabe que
    é a mesma coisa e a busca não vê duplicatas concorrentes (§10.6).
    """
    facts = [
        f
        for f in repo.facts_for_subject(entity.entity_id, lifecycle=(LifecycleStatus.CURRENT,))
        if scope.contains(f.revision_id) and fact_state(f) is UnitState.IMPLEMENTED
    ]
    facts = sorted(facts, key=lambda f: (f.predicate, f.fact_id))[:3]
    rels = tuple(
        r
        for r in _relations_of(repo, entity, scope)
        if r.lifecycle_status is LifecycleStatus.CURRENT
    )[:5]
    if not facts and not rels:
        return None
    unit = _build_unit(
        repo=repo,
        entity=entity,
        state=UnitState.IMPLEMENTED,
        facts=facts,
        relations=rels,
        belonging=belonging,
        scope=scope,
        subject_key=SUBJECT_CONTEXTO,
        title=f"{entity.title}: contexto mínimo para leitura isolada",
        subject=f"O que é {entity.title} e onde ele se encaixa",
        shared_context=True,
    )
    return unit


# --------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------


def _require_entity(repo: Any, entity_id: str) -> Entity:
    entity = repo.get_entity(entity_id, lifecycle=None)
    if entity is None:
        raise PublishingError(f"entidade âncora {entity_id!r} não existe em knowledge.db")
    return entity


def _skip(skipped: list[tuple[str, str]] | None, target: str, reason: str) -> None:
    if skipped is not None:
        skipped.append((target, reason))


__all__ = [
    "ALLOWED_ANCHORS",
    "ALL_LIFECYCLE",
    "ANALYSIS_GAPS_TITLE",
    "ANALYSIS_SEVERITY",
    "ANALYSIS_STATE_LABEL",
    "AnalysisState",
    "BEHAVIORAL_PREDICATE_TERMS",
    "BEHAVIOR_PROMISE_TERMS",
    "BEHAVIOR_REQUIRED_KINDS",
    "BOOLEAN_FLAG_VALUES",
    "NO_BEHAVIOR_ANALYSIS_LABEL",
    "STRUCTURAL_PREDICATES",
    "STRUCTURAL_PREDICATE_LEAVES",
    "analysis_state_label",
    "critical_tokens_of",
    "has_behavioral_content",
    "is_behavioral_predicate",
    "is_behavioral_statement",
    "is_boolean_flag",
    "is_structural_predicate",
    "normalize_predicate",
    "worst_analysis_state",
    "Belonging",
    "CONDITION_PREDICATES",
    "ContentGrade",
    "CrossRef",
    "DOC_TITLE_SUFFIX",
    "DocKind",
    "STRUCTURAL_TITLE_SUFFIX",
    "analysis_summary",
    "honest_title",
    "promises_behavior",
    "EXCEPTION_PREDICATES",
    "EmptyDocument",
    "EvidenceRef",
    "GENERIC_TITLES",
    "GenericTitle",
    "INTENT_TYPES",
    "InvalidDocumentGrouping",
    "KnowledgeDocument",
    "LIMITATION_PREDICATES",
    "MixedStateBlock",
    "PLACEHOLDER_RE",
    "PlaceholderContent",
    "PublishingError",
    "RELATION_PHRASES",
    "RelationRef",
    "RevisionScope",
    "STATE_BLOCK_LABEL",
    "STATE_LABEL",
    "SemanticUnit",
    "Statement",
    "UnitState",
    "assert_no_placeholder",
    "block_label",
    "assert_specific_title",
    "context_unit",
    "describe_locator",
    "document_id",
    "fact_state",
    "find_placeholders",
    "is_generic_title",
    "link_cross_state",
    "normalize_title",
    "resolve_belonging",
    "unit_id",
    "units_for_entity",
]
