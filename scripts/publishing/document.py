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

    def is_publishable(self) -> bool:
        """Tem resposta, ou só carimbo de pertencimento? (§10.2)

        Título, pertencimento, escopo e versão são MOLDURA. Uma unidade que só
        tem moldura não responde nada quando recuperada isolada e, publicada,
        vira ruído que compete na busca com a unidade que responde.
        """
        return bool(self.behavior or self.exceptions or self.gaps or self.relations)

    def unpublishable_reason(self) -> str:
        if self.is_publishable():
            return ""
        return (
            f"unidade {self.unit_id} ({self.title!r}) sem conteúdo útil: só título e "
            "pertencimento, sem comportamento, exceção, lacuna ou relação (§10.2)"
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

    def __post_init__(self) -> None:
        assert_specific_title(self.title, f"documento {self.document_id}")
        assert_no_placeholder(self.title, f"título do documento {self.document_id}")
        assert_no_placeholder(self.summary, f"resumo do documento {self.document_id}")
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
        doc_title = title or f"{anchor.title} — {DOC_TITLE_SUFFIX[doc_kind]}"
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
        )


#: Sufixo de título por tipo de documento — específico o bastante para D02/D09
#: (o assunto é identificável sem abrir outro documento nem ler o nome do arquivo).
DOC_TITLE_SUFFIX: dict[DocKind, str] = {
    DocKind.VISAO_SISTEMA: "visão do sistema",
    DocKind.CAPACIDADE: "capacidade, regras e contratos",
    DocKind.CONTRATO_DEPENDENCIA: "contrato e dependências",
    DocKind.INICIATIVA: "iniciativa, decisões e refinamentos",
    DocKind.EVOLUCAO: "estado atual e mudança proposta",
}


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
    "Belonging",
    "CONDITION_PREDICATES",
    "CrossRef",
    "DOC_TITLE_SUFFIX",
    "DocKind",
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
