"""Tipos e enums do domínio de conhecimento (plano §5).

Este módulo é só vocabulário: enums fechados, dataclasses de entrada (`*Draft`)
e de leitura (`Entity`, `Fact`, `Relation`), mais a hierarquia de erros de
invariante. Nenhuma regra é aplicada aqui — quem rejeita é `repository.py`,
`relations.py`, `evidence.py` e `identity.py`, em código executável.

Três eixos ficam SEPARADOS de propósito (§5.3): sustentação (`EpistemicStatus`),
vigência (`LifecycleStatus`) e natureza (`FactNature`). Colapsar dois deles em um
campo só é o erro que faz proposta aprovada virar comportamento implementado.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# --------------------------------------------------------------------------
# Enums do domínio
# --------------------------------------------------------------------------


class EntityType(str, enum.Enum):
    """§5.1 — as 14 classes de entidade do modelo mínimo."""

    SYSTEM = "System"
    COMPONENT = "Component"
    CAPABILITY = "Capability"
    BUSINESS_RULE = "BusinessRule"
    FLOW = "Flow"
    CONTRACT = "Contract"
    DATA_ENTITY = "DataEntity"
    INITIATIVE = "Initiative"
    DECISION = "Decision"
    REQUIREMENT = "Requirement"
    STORY = "Story"
    REFINEMENT = "Refinement"
    DEFECT = "Defect"
    SOURCE = "Source"


class EpistemicStatus(str, enum.Enum):
    """§5.3 — sustentação. `SUPPORTED` é atribuído pelo pipeline, nunca pela LLM."""

    SUPPORTED = "supported"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"
    DISPUTED = "disputed"


class LifecycleStatus(str, enum.Enum):
    """§5.3 — ciclo de vida/vigência."""

    CURRENT = "current"
    PROPOSED = "proposed"
    HISTORICAL = "historical"
    SUPERSEDED = "superseded"
    STALE = "stale"


class FactNature(str, enum.Enum):
    """§5.3 — natureza. Código, decisão e teste têm autoridades diferentes."""

    IMPLEMENTED = "implemented"
    DECLARED_REQUIREMENT = "declared_requirement"
    OBSERVED = "observed"
    TEST_EXPECTATION = "test_expectation"


class ApprovalState(str, enum.Enum):
    """Aprovação é eixo PRÓPRIO, fora de natureza e de ciclo de vida.

    Existe exatamente para o aceite W1 "requisito aprovado não aparece como
    implementação atual": aprovar registra aqui e não toca em `FactNature`.
    """

    NONE = "none"
    APPROVED = "approved"
    REJECTED = "rejected"


class RelationType(str, enum.Enum):
    """§5.5 — os 16 tipos de relação."""

    CONTAINS = "contains"
    CALLS = "calls"
    READS = "reads"
    WRITES = "writes"
    PUBLISHES = "publishes"
    CONSUMES = "consumes"
    DEPENDS_ON = "depends_on"
    IMPLEMENTS = "implements"
    VERIFIES = "verifies"
    RECORDS = "records"
    BELONGS_TO = "belongs_to"
    REFINES = "refines"
    PROPOSES_CHANGE_TO = "proposes_change_to"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"


class SourceKind(str, enum.Enum):
    """§5.4 — tipo de fonte, que determina o localizador obrigatório."""

    CODE = "code"
    CONFIG = "config"
    TEST = "test"
    DOCUMENT = "document"  # Word/PDF
    TRANSCRIPT = "transcript"
    OBSERVATION = "observation"


class ContentKind(str, enum.Enum):
    """O que o localizador aponta DENTRO da fonte.

    §5.4: comentário, docstring, README e plano não sustentam natureza
    `implemented`. Sem este campo, um localizador de código válido apontando
    para um comentário passaria pela validação estrutural e sustentaria uma
    afirmação de comportamento implementado.
    """

    EXECUTABLE = "executable"
    CONFIG_VALUE = "config_value"
    TEST_ASSERTION = "test_assertion"
    OBSERVATION_RECORD = "observation_record"
    COMMENT = "comment"
    DOCSTRING = "docstring"
    MARKDOWN = "markdown"
    PROSE = "prose"
    TRANSCRIPT_BLOCK = "transcript_block"


#: Conteúdos que NÃO sustentam `FactNature.IMPLEMENTED` (§5.4).
NON_IMPLEMENTING_CONTENT: frozenset[ContentKind] = frozenset(
    {
        ContentKind.COMMENT,
        ContentKind.DOCSTRING,
        ContentKind.MARKDOWN,
        ContentKind.PROSE,
        ContentKind.TRANSCRIPT_BLOCK,
    }
)


class OriginKind(str, enum.Enum):
    """Quem afirmou. Prefixo obrigatório dos identificadores de autoria.

    Convenção verificada em código: toda autoria é a string ``"<kind>:<id>"``,
    ex. ``"llm:investigator-3"``, ``"pipeline:verify-static"``. É isso que
    permite ao repositório rejeitar `supported` auto-declarado por LLM sem
    depender de heurística de nome.
    """

    LLM = "llm"
    PIPELINE = "pipeline"
    HUMAN = "human"
    EXTRACTOR = "extractor"


class AliasOrigin(str, enum.Enum):
    """§5.2 — aliases sempre com origem; similaridade isolada não funde (A06)."""

    VCS_RENAME = "vcs_rename"
    METADATA_ID = "metadata_id"
    HUMAN_CONFIRMED = "human_confirmed"
    EXTRACTED = "extracted"
    DECLARED = "declared"


#: Origens que CONFIRMAM preservação de identidade em renomeação (§5.2).
RENAME_CONFIRMING_ORIGINS: frozenset[AliasOrigin] = frozenset(
    {AliasOrigin.VCS_RENAME, AliasOrigin.METADATA_ID, AliasOrigin.HUMAN_CONFIRMED}
)


class TargetKind(str, enum.Enum):
    """Alvo de um vínculo de evidência ou de uma mudança de ciclo de vida."""

    ENTITY = "entity"
    FACT = "fact"
    RELATION = "relation"


class EffectStatus(str, enum.Enum):
    """Estado do efeito na outbox de `knowledge.db` (§4.2).

    `runtime.db` referencia `effect_id` e confirma o destino; a outbox aqui só
    registra que o efeito nasceu na MESMA transação da revisão.
    """

    PENDING = "pending"
    DISPATCHED = "dispatched"
    DONE = "done"
    FAILED = "failed"


#: Filtro padrão de consulta (§ aceite W1: consulta padrão não mistura
#: histórico/substituído com vigente).
DEFAULT_LIFECYCLE: tuple[LifecycleStatus, ...] = (LifecycleStatus.CURRENT,)


# --------------------------------------------------------------------------
# Erros de invariante
# --------------------------------------------------------------------------


class KnowledgeError(Exception):
    """Base de todo erro do domínio de conhecimento."""


class InvariantViolation(KnowledgeError):
    """Regra do plano §5 violada. A mensagem diz qual invariante e por quê."""


class MissingEvidence(InvariantViolation):
    """`supported` sem `evidence_refs` (aceite W1)."""


class SelfDeclaredSupport(InvariantViolation):
    """Saída de LLM tentando declarar a si própria `supported` (§5.3)."""


class NatureChangeRejected(InvariantViolation):
    """Aprovação/edição tentando promover proposta a `implemented` (§5.3)."""


class UnsupportedContentKind(InvariantViolation):
    """Evidência de comentário/docstring/markdown sustentando `implemented` (§5.4)."""


class InvalidRelationPair(InvariantViolation):
    """Par (tipo origem, tipo destino) fora da matriz permitida (§5.5)."""


class SupersedesCycle(InvariantViolation):
    """Ciclo de substituição de versões — inválido por §5.5."""


class IdentityConflict(InvariantViolation):
    """Mesma identidade reivindicada com namespace/tipo divergente (§5.2)."""


class RenameNotConfirmed(InvariantViolation):
    """Renomeação sem histórico/metadado; similaridade isolada não basta (§5.2)."""


class LocatorInvalid(InvariantViolation):
    """Localizador de evidência sem os campos obrigatórios do seu tipo (§5.4)."""


class SchemaVersionMismatch(KnowledgeError):
    """`knowledge.db` em versão diferente da suportada; migração é explícita (W8)."""


class UnknownReference(InvariantViolation):
    """Referência a entidade/fato/evidência inexistente."""


# --------------------------------------------------------------------------
# Fontes e versões
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """§5.1 `Source` — arquivo, transcrição, snapshot, execução ou entrada autorizada."""

    source_id: str
    namespace: str
    source_kind: SourceKind
    uri: str
    recorded_at: str = ""


@dataclass(frozen=True)
class SourceVersion:
    """Versão imutável de uma fonte (§4.2: snapshots imutáveis, com hash)."""

    source_version_id: str
    source_id: str
    version_label: str
    content_hash: str
    captured_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Evidência (§5.4)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """Localizador resolvível, imutável.

    `evidence_id` é derivado do conteúdo do localizador (ver
    `evidence.evidence_id`), logo reingestão idêntica reaproveita a mesma linha
    em vez de recontar evidência derivada (A09).
    """

    evidence_id: str
    namespace: str
    source_kind: SourceKind
    content_kind: ContentKind
    source_version_id: str
    locator: Mapping[str, Any]
    snippet_hash: str | None = None
    recorded_at: str = ""


# --------------------------------------------------------------------------
# Entidades (§5.1, §5.2)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Alias:
    """Termo alternativo COM origem (§5.2)."""

    alias: str
    origin: AliasOrigin
    source_version_id: str | None = None


@dataclass(frozen=True)
class EntityDraft:
    """Entrada de escrita de entidade.

    `stable_key` é a chave natural (caminho, símbolo, URN, id externo) e é
    obrigatória e distinta de `title`: §5.2 exige `entity_id` NÃO derivado
    exclusivamente do título. `title` pode mudar sem trocar a identidade.

    `valid_from`/`valid_to` ficam `None` quando desconhecidos. O repositório
    nunca os preenche por inferência a partir de `recorded_at` (§5.2).
    """

    namespace: str
    entity_type: EntityType
    stable_key: str
    title: str
    source_version_id: str | None = None
    aliases: Sequence[Alias] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)
    lifecycle_status: LifecycleStatus = LifecycleStatus.CURRENT
    valid_from: str | None = None
    valid_to: str | None = None
    content_hash: str | None = None
    entity_id: str | None = None  # explícito só para identidade já resolvida
    evidence_refs: Sequence[str] = ()


@dataclass(frozen=True)
class Entity:
    """Entidade lida do banco, na revisão apontada por `revision_id`."""

    entity_id: str
    namespace: str
    entity_type: EntityType
    revision_id: str
    stable_key: str
    title: str
    content_hash: str
    source_version_id: str | None
    lifecycle_status: LifecycleStatus
    valid_from: str | None
    valid_to: str | None
    recorded_at: str
    attributes: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Fatos (§5.3)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FactDraft:
    """Entrada de escrita de fato.

    `asserted_by` e `support_recorded_by` usam a convenção ``"<kind>:<id>"``
    de `OriginKind`. Para `epistemic_status=supported` o repositório exige
    `support_recorded_by` de origem `pipeline`/`human` e diferente de
    `asserted_by` (§5.3: saída de LLM não declara a si própria sustentada).
    """

    namespace: str
    subject_id: str
    predicate: str
    value: str
    scope: str
    nature: FactNature
    epistemic_status: EpistemicStatus
    lifecycle_status: LifecycleStatus
    asserted_by: str
    evidence_refs: Sequence[str] = ()
    source_version_id: str | None = None
    support_recorded_by: str | None = None
    approval_state: ApprovalState = ApprovalState.NONE
    valid_from: str | None = None
    valid_to: str | None = None
    nature_change_recorded_by: str | None = None
    fact_id: str | None = None


@dataclass(frozen=True)
class Fact:
    """Fato lido do banco, na revisão apontada por `revision_id`."""

    fact_id: str
    revision_id: str
    namespace: str
    subject_id: str
    predicate: str
    value: str
    scope: str
    nature: FactNature
    epistemic_status: EpistemicStatus
    lifecycle_status: LifecycleStatus
    approval_state: ApprovalState
    asserted_by: str
    support_recorded_by: str | None
    source_version_id: str | None
    evidence_refs: tuple[str, ...]
    content_hash: str
    valid_from: str | None
    valid_to: str | None
    recorded_at: str


# --------------------------------------------------------------------------
# Relações (§5.5)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RelationDraft:
    """Entrada de escrita de relação tipada."""

    namespace: str
    source_entity_id: str
    relation_type: RelationType
    target_entity_id: str
    scope: str
    epistemic_status: EpistemicStatus
    lifecycle_status: LifecycleStatus
    asserted_by: str
    evidence_refs: Sequence[str] = ()
    source_version_id: str | None = None
    support_recorded_by: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    relation_id: str | None = None


@dataclass(frozen=True)
class Relation:
    """Relação lida do banco, na revisão apontada por `revision_id`."""

    relation_id: str
    revision_id: str
    namespace: str
    source_entity_id: str
    relation_type: RelationType
    target_entity_id: str
    scope: str
    epistemic_status: EpistemicStatus
    lifecycle_status: LifecycleStatus
    asserted_by: str
    support_recorded_by: str | None
    source_version_id: str | None
    evidence_refs: tuple[str, ...]
    content_hash: str
    valid_from: str | None
    valid_to: str | None
    recorded_at: str


# --------------------------------------------------------------------------
# Revisão e outbox (§4.2)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Revision:
    """Revisão atômica de conhecimento — unidade de escrita do repositório."""

    revision_id: str
    created_at: str
    author: str
    reason: str
    parent_revision_id: str | None = None
    change_count: int = 0


@dataclass(frozen=True)
class Effect:
    """Efeito enfileirado na outbox, na MESMA transação da revisão (§4.2)."""

    effect_id: str
    revision_id: str
    effect_type: str
    payload: Mapping[str, Any]
    status: EffectStatus
    created_at: str


@dataclass(frozen=True)
class WriteResult:
    """Resultado de uma escrita dentro de uma revisão.

    `changed=False` significa reingestão idêntica absorvida por upsert
    idempotente (aceite W1): identidade preservada, nenhuma linha nova.
    """

    kind: TargetKind
    target_id: str
    revision_id: str
    changed: bool
    reason: str = ""


def origin_kind(author: str) -> OriginKind | None:
    """Extrai o `OriginKind` de uma autoria ``"<kind>:<id>"``; `None` se inválida."""
    if not author or ":" not in author:
        return None
    prefix = author.split(":", 1)[0].strip().lower()
    try:
        return OriginKind(prefix)
    except ValueError:
        return None
