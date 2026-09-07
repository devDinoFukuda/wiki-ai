"""Correlação incremental de uma fonte com o grafo canônico (plano §8.2-§8.4).

Executa os dez passos de §8.2 numa REVISÃO ATÔMICA só. Ou a fonte inteira
entra (entidades, fatos, arestas, evidências e o efeito de republicação), ou
nada entra — `repository.revision()` faz `ROLLBACK` e não sobra meia ingestão.

As decisões que este módulo transforma em código executável, e por quê:

| Regra do plano | Onde vive |
|---|---|
| §8.2.1 fonte duplicada não duplica fato/aresta | `_already_ingested` + retorno `duplicate` |
| §8.2.3 id explícito resolve no namespace | `_resolve_explicit_ids` |
| §8.2.5 lexical só AMPLIA candidatos | `_lexical_suggestions` (`candidate_only=True`) |
| §8.2.7 score não funde identidade | `_assert_no_merge_by_score` |
| §8.2.8 divergência vira `disputed` + `contradicts` | `_write_statement_fact` |
| §8.2.9 unidades afetadas | `_affected_units` |
| §8.3 proposta continua proposta | `_entity_lifecycle_of` + `_proposed_lifecycle` (extract) |
| §5.5 aresta de proposta é proposta | `_entity_lifecycle_of` (autoridade é a entidade) |
| fonte fraca não rebaixa aresta já sustentada | `_put_relation` |
| §8.3 sem id, sem vínculo por proximidade temporal | `_link_targets` (só ids explícitos) |
| §8.4 fonte derivada não vira evidência independente | `detect_derived` + `DerivedInfo.independent_evidence` |
| §8.4 resposta de agente propõe, não confirma | `_agent_gate` |
| F10 `source_type=code-repo` não concede `implemented` | `_source_kind_for` + `_assert_cannot_support_implemented` |
| §7.1 ambiguidade aceita a fonte e pede UMA decisão | `_pending_initiative_decision` |
| aceite W5 ambiguidade não paralisa o lote | `correlate_batch` |

O que este módulo NUNCA faz, e a ausência é o requisito: não cria entidade
técnica (`Capability`, `BusinessRule`, `Component`...) a partir de prosa; não
escreve nenhum fato de natureza `implemented`; não usa data/proximidade para
vincular; não funde duas identidades por semelhança.

Só stdlib + `knowledge`. Nada de `wk`/`codescan`/`sbindex`.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

try:  # `PYTHONPATH=scripts` (convenção do repositório)
    from knowledge import evidence as ev_mod
    from knowledge import identity
    from knowledge.models import (
        Alias,
        AliasOrigin,
        ContentKind,
        EntityDraft,
        EntityType,
        EpistemicStatus,
        Evidence,
        FactDraft,
        FactNature,
        LifecycleStatus,
        RelationDraft,
        RelationType,
        SourceKind,
    )
    from knowledge.relations import validate_pair
    from knowledge.models import InvalidRelationPair
except ImportError:  # pragma: no cover - execução como `scripts.ingestion.correlate`
    from ..knowledge import evidence as ev_mod  # type: ignore[no-redef]
    from ..knowledge import identity  # type: ignore[no-redef]
    from ..knowledge.models import (  # type: ignore[no-redef]
        Alias,
        AliasOrigin,
        ContentKind,
        EntityDraft,
        EntityType,
        EpistemicStatus,
        Evidence,
        FactDraft,
        FactNature,
        InvalidRelationPair,
        LifecycleStatus,
        RelationDraft,
        RelationType,
        SourceKind,
    )
    from ..knowledge.relations import validate_pair  # type: ignore[no-redef]

from .extract import (
    Candidate,
    CandidateKind,
    ExplicitId,
    ExtractionCandidates,
    MentionPolarity,
    accent_fold,
    assert_no_implemented,
    normalize_tokens,
    parse_explicit_ids,
)

#: Quem AFIRMA (a extração) e quem REGISTRA SUSTENTAÇÃO (o pipeline). Precisam
#: ser diferentes e o segundo precisa ser `pipeline:`/`human:`, senão
#: `repository._check_support` rejeita — é a trava de §5.3 contra sustentação
#: auto-declarada, e ela vale também para este módulo.
ASSERTED_BY = "extractor:ingestion.extract"
SUPPORT_RECORDED_BY = "pipeline:ingestion.correlate"
REVISION_AUTHOR = "pipeline:ingestion.correlate"

#: Tipos que PODEM ser criados a partir de um documento (§8.2.2-3: entidades de
#: INTENÇÃO). `Capability`, `BusinessRule`, `Component` e demais entidades
#: técnicas ficam de fora de propósito: sua existência e seu comportamento são
#: atestados pelo pipeline de análise de código, não por um texto que as cita
#: (§5.4, F10). Citação a uma técnica inexistente vira referência não resolvida.
CREATABLE_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.INITIATIVE,
        EntityType.DECISION,
        EntityType.REQUIREMENT,
        EntityType.STORY,
        EntityType.REFINEMENT,
    }
)

#: Entidades de intenção que pertencem a uma iniciativa (`belongs_to`).
INTENT_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.DECISION,
        EntityType.REQUIREMENT,
        EntityType.STORY,
        EntityType.REFINEMENT,
        EntityType.DEFECT,
    }
)

#: Tipos técnicos alvo de `proposes_change_to`.
TECHNICAL_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.SYSTEM,
        EntityType.COMPONENT,
        EntityType.CAPABILITY,
        EntityType.CONTRACT,
        EntityType.DATA_ENTITY,
        EntityType.FLOW,
        EntityType.BUSINESS_RULE,
    }
)

#: Tipos que nascem propostos quando criados por um documento (§8.3: o
#: refinamento e a história derivada têm status proposto).
BORN_PROPOSED: frozenset[EntityType] = frozenset({EntityType.REFINEMENT, EntityType.STORY})

#: Preferência de aresta entre o SUJEITO do documento e uma entidade citada.
#: A tabela existe porque a matriz de §5.5 é direcional: `refines` só aceita
#: `Refinement -> Decision|Requirement|Story`. Uma história que cita um
#: refinamento não vira `Story refines Refinement` (par inválido) — vira
#: `Story derived_from Refinement`, que é o que de fato aconteceu.
LINK_PREFERENCE: Mapping[tuple[EntityType, EntityType], RelationType] = {
    (EntityType.REFINEMENT, EntityType.DECISION): RelationType.REFINES,
    (EntityType.REFINEMENT, EntityType.REQUIREMENT): RelationType.REFINES,
    (EntityType.REFINEMENT, EntityType.STORY): RelationType.REFINES,
    (EntityType.STORY, EntityType.REFINEMENT): RelationType.DERIVED_FROM,
    (EntityType.STORY, EntityType.DECISION): RelationType.DERIVED_FROM,
    (EntityType.STORY, EntityType.REQUIREMENT): RelationType.DERIVED_FROM,
    (EntityType.REQUIREMENT, EntityType.DECISION): RelationType.DERIVED_FROM,
    (EntityType.DECISION, EntityType.DECISION): RelationType.DERIVED_FROM,
    (EntityType.REFINEMENT, EntityType.REFINEMENT): RelationType.DERIVED_FROM,
}

#: Ordem de preferência do SUJEITO do documento (§8.3: o documento de RF-042
#: fala em nome de RF-042).
SUBJECT_PREFERENCE: tuple[EntityType, ...] = (
    EntityType.REFINEMENT,
    EntityType.STORY,
    EntityType.DEFECT,
    EntityType.DECISION,
    EntityType.REQUIREMENT,
)

#: Sobreposição lexical mínima para SUGERIR (nunca para confirmar).
LEXICAL_MIN_OVERLAP = 0.34

#: Todos os estados de vigência — usado onde a pergunta é "o que existe",
#: e não "o que vale hoje" (unidades afetadas incluem proposta e histórico).
ALL_LIFECYCLE: tuple[LifecycleStatus, ...] = tuple(LifecycleStatus)

#: Marcas de publicação própria (§8.4). Procuradas na metadata bruta E no
#: conteúdo, porque o rodapé de uma publicação sobrevive ao copiar/colar
#: enquanto a metadata do arquivo não.
_PUBLICATION_ID_RE = re.compile(r"publication[_\s-]?id\s*[:=]\s*([A-Za-z0-9._:-]+)", re.IGNORECASE)
_KNOWLEDGE_REV_RE = re.compile(
    r"knowledge[_\s-]?revision\s*[:=]\s*([A-Za-z0-9._:-]+)", re.IGNORECASE
)

_TRANSCRIPT_KINDS = ("srt", "vtt", "transcript", "transcricao", "transcrição", "caption", "sbv")
_MARKDOWN_KINDS = ("md", "markdown", "html", "htm")


# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------


class CorrelationError(Exception):
    """Base dos erros de correlação."""


class IdentityMergeRefused(CorrelationError):
    """Tentativa de fundir identidades por similaridade (§8.2.7, §8.3)."""


class ImplementedOverrideRefused(CorrelationError):
    """Documento tentando alterar fato `implemented` sem evidência de código."""


class DerivedEvidenceRefused(CorrelationError):
    """Publicação própria tentando virar evidência independente (§8.4)."""


# --------------------------------------------------------------------------
# Resultado
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedRef:
    """Um id explícito e o que aconteceu com ele no namespace."""

    identifier: ExplicitId
    entity_id: str | None
    entity_type: EntityType | None
    created: bool
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.entity_id is not None


@dataclass(frozen=True)
class Suggestion:
    """Aproximação por alias/lexical. SEMPRE candidata, nunca identidade.

    `candidate_only` é `True` por construção e verificado por
    `_assert_no_merge_by_score` antes de qualquer escrita: §8.2.7 proíbe fundir
    identidades por score isolado e §8.3 exige que duas inceptions homônimas
    permaneçam separadas.
    """

    text: str
    entity_id: str
    entity_title: str
    entity_type: EntityType
    basis: str
    overlap: float
    block_id: str
    candidate_only: bool = True


@dataclass(frozen=True)
class OrphanCandidate:
    """Candidato aceito sem correlação, com o motivo (§7.1)."""

    text: str
    kind: str
    block_id: str
    reason: str


@dataclass(frozen=True)
class PendingDecision:
    """A ÚNICA coisa que se pede ao humano: uma decisão que muda um vínculo.

    §7.1: "solicita somente a decisão que alteraria um vínculo material".
    Ambiguidade sem efeito material não vira pergunta e não paralisa o lote.
    """

    key: str
    question: str
    options: tuple[str, ...] = ()
    material_effect: str = ""


@dataclass(frozen=True)
class WrittenRelation:
    relation_id: str
    source_entity_id: str
    relation_type: str
    target_entity_id: str
    epistemic: str
    lifecycle: str
    changed: bool


@dataclass(frozen=True)
class DerivedInfo:
    """Diagnóstico de realimentação (§8.4)."""

    is_derived: bool = False
    publication_id: str | None = None
    knowledge_revision: str | None = None
    origin_entity_id: str | None = None
    independent_evidence: bool = True
    authored_by_agent: bool = False
    reason: str = ""


@dataclass
class CorrelationResult:
    """Retorno de `correlate` — auditável sem abrir o banco."""

    namespace: str
    source_id: str
    source_version_id: str
    source_entity_id: str | None = None
    duplicate: bool = False
    revision_id: str | None = None
    initiative_id: str | None = None
    initiative_key: str | None = None
    entities_created: tuple[str, ...] = ()
    entities_touched: tuple[str, ...] = ()
    facts_written: tuple[str, ...] = ()
    relations_written: tuple[WrittenRelation, ...] = ()
    candidate_relations: tuple[WrittenRelation, ...] = ()
    resolved_refs: tuple[ResolvedRef, ...] = ()
    unresolved_refs: tuple[ResolvedRef, ...] = ()
    suggestions: tuple[Suggestion, ...] = ()
    orphans: tuple[OrphanCandidate, ...] = ()
    disputed_facts: tuple[str, ...] = ()
    contradictions: tuple[WrittenRelation, ...] = ()
    pending_decisions: tuple[PendingDecision, ...] = ()
    derived: DerivedInfo = field(default_factory=DerivedInfo)
    affected_units: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    error: str | None = None

    @property
    def correlated(self) -> bool:
        """Houve vínculo material? Órfão puro conta como fonte preservada."""
        return bool(self.relations_written or self.facts_written)

    def summary(self) -> str:
        if self.error:
            return f"erro: {self.error}"
        if self.duplicate:
            return "duplicada: nenhum fato/aresta novo"
        return (
            f"entidades={len(self.entities_touched)} fatos={len(self.facts_written)} "
            f"arestas={len(self.relations_written)} candidatas={len(self.candidate_relations)} "
            f"orfaos={len(self.orphans)} pendencias={len(self.pending_decisions)}"
        )


# --------------------------------------------------------------------------
# Leitura do contrato de `normalize.py` (import tardio, sem editar o vizinho)
# --------------------------------------------------------------------------


def _normalize_module() -> Any:
    """Import TARDIO de `normalize.py`; `None` quando ainda não existe.

    O contrato é lido por atributo em todo o módulo, então a correlação roda
    sem o vizinho. O import serve só para diagnóstico de versão do contrato.
    """
    try:
        from . import normalize as _normalize  # noqa: PLC0415  (tardio de propósito)
    except Exception:  # pragma: no cover - vizinho ainda não publicado
        return None
    return _normalize


def _contract_stamp() -> str:
    """Marca, no efeito de republicação, com qual contrato a fonte foi lida.

    Serve para depurar divergência entre os dois lados: uma unidade republicada
    por uma ingestão que rodou sem `normalize.py` foi lida por atributo, e a
    diferença precisa ser visível na outbox e não só no log.
    """
    module = _normalize_module()
    if module is None:
        return "normalize:ausente (contrato lido por atributo)"
    return f"normalize:{getattr(module, '__version__', 'presente')}"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _raw_metadata(doc: Any) -> Mapping[str, Any]:
    raw = _attr(doc, "metadata", None) or {}
    return raw if isinstance(raw, Mapping) else {}


def _doc_uri(doc: Any) -> str:
    return str(_attr(doc, "path_original", "") or _attr(doc, "source_id", "") or "documento")


def _doc_kind(doc: Any) -> str:
    return accent_fold(str(_attr(doc, "kind", "") or ""))


# --------------------------------------------------------------------------
# F10: natureza da fonte é atestada, nunca declarada
# --------------------------------------------------------------------------


def _source_kind_for(doc: Any) -> SourceKind:
    """Tipo de fonte de um documento ingerido: TRANSCRIPT ou DOCUMENT. Nunca CODE.

    F10 em código: `metadata.source_type="code-repo"` é uma DECLARAÇÃO do
    arquivo. Se ela pudesse escolher `SourceKind.CODE`, um texto qualquer
    ganharia localizador de código e, com `ContentKind.EXECUTABLE`, passaria a
    sustentar `implemented`. A escolha aqui ignora `source_type` de propósito e
    olha só o formato que `normalize.py` extraiu.
    """
    kind = _doc_kind(doc)
    if any(token in kind for token in _TRANSCRIPT_KINDS):
        return SourceKind.TRANSCRIPT
    return SourceKind.DOCUMENT


def _content_kind_for(source_kind: SourceKind, doc: Any) -> ContentKind:
    """O que o localizador aponta dentro da fonte — sempre conteúdo não executável."""
    if source_kind is SourceKind.TRANSCRIPT:
        return ContentKind.TRANSCRIPT_BLOCK
    kind = _doc_kind(doc)
    if any(token == kind or token in kind for token in _MARKDOWN_KINDS):
        return ContentKind.MARKDOWN
    return ContentKind.PROSE


def _assert_cannot_support_implemented(ev: Evidence) -> None:
    """Trava final de §5.4/F10 antes de gravar qualquer evidência de documento.

    `knowledge.evidence.supports_implemented` é a autoridade sobre o que
    sustenta comportamento implementado. Se ela algum dia disser `True` para
    uma evidência montada aqui, a ingestão para — em vez de o corpus passar a
    aceitar prosa como código.
    """
    if ev_mod.supports_implemented(ev):
        raise ImplementedOverrideRefused(
            f"evidência de documento ({ev.source_kind.value}/{ev.content_kind.value}) "
            "sustentaria natureza 'implemented'; documento declarado não vira "
            "comportamento implementado (§5.4, F10)"
        )


# --------------------------------------------------------------------------
# Localizadores (§5.4)
# --------------------------------------------------------------------------


#: Tipos de fonte que um DOCUMENTO ingerido pode ter. F10 em uma linha: por
#: mais que o bloco declare outra coisa, ingestão de texto não produz
#: `SourceKind.CODE`/`CONFIG`/`TEST`.
DOCUMENTARY_SOURCE_KINDS: frozenset[SourceKind] = frozenset(
    {SourceKind.DOCUMENT, SourceKind.TRANSCRIPT}
)


def _evidence_kinds(ref: Any, doc: Any) -> tuple[SourceKind, ContentKind]:
    """Tipos de evidência do bloco, com a dica do vizinho CLAMPADA por F10.

    `normalize.py` já declara em cada bloco qual `source_kind`/`content_kind` a
    evidência deve usar, e respeitar essa dica é o que mantém um bloco de
    código dentro de um Markdown citado como o que ele é. Mas a dica é dado,
    não instrução (§8.1): se ela pedir `code`/`config`/`test` — os únicos tipos
    cujo conteúdo executável sustenta `implemented` — o pedido é ignorado e
    vale o tipo derivado do formato da fonte.
    """
    doc_kind = _source_kind_for(doc)
    hinted = _enum_value(_attr(ref, "source_kind_hint", ""))
    source_kind = doc_kind
    if hinted:
        try:
            parsed = SourceKind(hinted)
        except ValueError:
            parsed = doc_kind
        source_kind = parsed if parsed in DOCUMENTARY_SOURCE_KINDS else doc_kind

    content_kind = _content_kind_for(source_kind, doc)
    hinted_content = _enum_value(_attr(ref, "content_kind_hint", ""))
    if hinted_content:
        try:
            parsed_content = ContentKind(hinted_content)
        except ValueError:
            parsed_content = content_kind
        if parsed_content in ev_mod.ALLOWED_CONTENT[source_kind]:
            content_kind = parsed_content
    return source_kind, content_kind


def _enum_value(value: Any) -> str:
    """Valor textual de um enum do contrato (mixin `str` não basta em 3.11+)."""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _locator_for(source_kind: SourceKind, doc: Any, ref: Any, version_label: str) -> dict[str, Any]:
    """Localizador VÁLIDO para `knowledge.evidence.validate_locator`.

    Primeiro tenta PRESERVAR o localizador que `normalize.py` construiu — §8.1
    manda preservar localizadores, e o vizinho já usa o vocabulário do modelo
    de evidência. Só quando ele não valida (contrato divergente, bloco sem
    localizador) é que um localizador equivalente é montado campo a campo
    aqui; traduzir em vez de copiar às cegas é o que mantém a citação
    resolvível quando qualquer dos dois lados mudar.
    """
    preserved = _attr(ref, "locator", None)
    if isinstance(preserved, Mapping) and preserved:
        try:
            return ev_mod.validate_locator(source_kind, preserved)
        except Exception:
            pass  # contrato divergente: monta abaixo, sem perder a citação

    block_id = str(_attr(ref, "block_id", "") or "bloco")
    if source_kind is SourceKind.TRANSCRIPT:
        locator: dict[str, Any] = {
            "file": _doc_uri(doc),
            "version": version_label,
            "block": block_id,
        }
        start, end = ref.get("time_start"), ref.get("time_end")
        if start and end:
            locator["time_start"] = str(start)
            locator["time_end"] = str(end)
        speaker = ref.get("speaker")
        if speaker:
            locator["speaker"] = str(speaker)
        return locator

    section = ref.get("section") or _attr(ref, "block_kind", "") or "corpo"
    locator = {"version": version_label, "section": str(section), "block": block_id}
    paragraph = ref.get("paragraph")
    if paragraph:
        locator["paragraph"] = str(paragraph)
    heading = ref.get("heading_path")
    if heading:
        locator["heading_path"] = str(heading)
    return locator


def _metadata_locator(source_kind: SourceKind, doc: Any, version_label: str) -> dict[str, Any]:
    """Localizador do CABEÇALHO da fonte — onde a metadata declarada mora.

    Bloco `metadata` é um trecho real e recuperável da fonte, então declarar
    `initiative_id` no cabeçalho é uma citação verificável. O que ele NÃO faz é
    mudar a natureza do que sustenta: continua prosa/transcrição (F10).
    """
    if source_kind is SourceKind.TRANSCRIPT:
        return {"file": _doc_uri(doc), "version": version_label, "block": "metadata"}
    return {"version": version_label, "section": "metadata", "block": "metadata"}


# --------------------------------------------------------------------------
# §8.2 passo 1 — fonte duplicada
# --------------------------------------------------------------------------


def _already_ingested(conn: sqlite3.Connection, svid: str) -> bool:
    """A MESMA versão de fonte já produziu conhecimento?

    Não basta a versão existir em `source_versions`: uma execução interrompida
    antes da revisão poderia ter deixado a linha sem nenhum conhecimento
    associado, e recusar a reingestão nesse caso perderia a fonte. Duplicada é
    a versão que já tem evidência, entidade, fato ou aresta apontando para ela.
    """
    row = conn.execute(
        "SELECT 1 FROM source_versions WHERE source_version_id=? LIMIT 1", (svid,)
    ).fetchone()
    if row is None:
        return False
    queries = (
        "SELECT 1 FROM evidence WHERE source_version_id=? LIMIT 1",
        "SELECT 1 FROM entity_revisions WHERE source_version_id=? LIMIT 1",
        "SELECT 1 FROM fact_revisions WHERE source_version_id=? LIMIT 1",
        "SELECT 1 FROM relation_revisions WHERE source_version_id=? LIMIT 1",
    )
    return any(conn.execute(sql, (svid,)).fetchone() is not None for sql in queries)


# --------------------------------------------------------------------------
# §8.4 — conhecimento derivado e realimentação
# --------------------------------------------------------------------------


def _scan_publication_marks(doc: Any) -> tuple[str | None, str | None]:
    meta = _raw_metadata(doc)
    pub = meta.get("publication_id")
    rev = meta.get("knowledge_revision")
    if pub or rev:
        return (str(pub) if pub else None, str(rev) if rev else None)
    for block in _attr(doc, "blocks", ()) or ():
        text = str(_attr(block, "text", "") or "")
        if pub is None:
            m = _PUBLICATION_ID_RE.search(text)
            if m:
                pub = m.group(1)
        if rev is None:
            m = _KNOWLEDGE_REV_RE.search(text)
            if m:
                rev = m.group(1)
        if pub and rev:
            break
    return (str(pub) if pub else None, str(rev) if rev else None)


def detect_derived(doc: Any, repo: Any, namespace: str | None = None) -> DerivedInfo:
    """A fonte é publicação PRÓPRIA reingerida, ou saída de agente? (§8.4)

    Duas consequências distintas, e é importante que sejam distintas:

    - publicação própria: a linhagem `derived_from` é preservada e NENHUMA
      evidência independente é criada. Contar a própria publicação como
      evidência nova é auto-confirmação: o corpus passaria a "provar" o que ele
      mesmo escreveu, e a segunda ingestão pareceria confirmação da primeira.
    - resposta de agente: pode PROPOR (tudo nasce `proposed`/`inferred`), nunca
      confirmar a si própria (`supported` fica proibido para esta fonte).
    """
    pub, rev = _scan_publication_marks(doc)
    meta = _raw_metadata(doc)
    source_type = accent_fold(str(meta.get("source_type", "") or "")).strip()
    agent = source_type in {"agent-response", "agent", "llm", "assistant", "claude", "copilot"}

    if not pub and not rev:
        # Resposta de agente NÃO é publicação derivada: ela pode criar evidência
        # da própria existência (alguém escreveu aquilo), mas `_agent_gate`
        # impede que essa evidência a confirme como `supported` (§8.4).
        return DerivedInfo(
            is_derived=False,
            authored_by_agent=agent,
            independent_evidence=True,
            reason="sem marca de publicação própria",
        )

    origin: str | None = None
    if namespace and pub:
        ns = identity.normalize_namespace(namespace)
        for key in (f"publication:{pub}", pub):
            found = identity.resolve_identity(repo.conn, ns, EntityType.SOURCE, key)
            if found:
                origin = found
                break

    return DerivedInfo(
        is_derived=True,
        publication_id=pub,
        knowledge_revision=rev,
        origin_entity_id=origin,
        independent_evidence=False,
        authored_by_agent=agent,
        reason=(
            "publicação própria reconhecida (publication_id/knowledge_revision): "
            "linhagem derived_from preservada, sem evidência independente (§8.4)"
        ),
    )


def _agent_gate(epistemic: EpistemicStatus, derived: DerivedInfo) -> EpistemicStatus:
    """Saída de agente nunca chega a `supported` (§8.4).

    A trava é aqui e não só na tabela de estados porque `correlate` é o
    pipeline — o único autorizado a atribuir `supported` (§5.3). Sem esta
    função, reingerir a própria resposta do agente confirmaria a resposta.
    """
    if derived.authored_by_agent and epistemic is EpistemicStatus.SUPPORTED:
        return EpistemicStatus.INFERRED
    return epistemic


# --------------------------------------------------------------------------
# §8.2 passos 3-5 — resolução de identidade
# --------------------------------------------------------------------------


def _entity_title(identifier: ExplicitId, candidates: Sequence[Candidate], fallback: str) -> str:
    for c in candidates:
        if any(i.value == identifier.value for i in c.explicit_ids):
            text = c.text.strip().replace("\n", " ")
            return f"{identifier.value} {text[:110]}".strip()
    return f"{identifier.value} {fallback}".strip()


def _entity_lifecycle(entity_type: EntityType, candidates: Sequence[Candidate]) -> LifecycleStatus:
    """Vigência da entidade criada: a mais conservadora entre as citações.

    `proposed` vence `current` — o refinamento e a história derivada nascem
    propostos (§8.3) e nada neste módulo os promove.
    """
    if entity_type in BORN_PROPOSED:
        return LifecycleStatus.PROPOSED
    for c in candidates:
        if c.lifecycle is LifecycleStatus.PROPOSED:
            return LifecycleStatus.PROPOSED
    return LifecycleStatus.CURRENT


def _resolve_explicit_ids(
    rev: Any,
    repo: Any,
    ns: str,
    identifiers: Sequence[ExplicitId],
    candidates: Sequence[Candidate],
    svid: str,
    evidence_by_id: Mapping[str, tuple[str, ...]],
    create: bool,
) -> tuple[dict[str, ResolvedRef], list[str], list[str]]:
    """§8.2 passos 2-3: resolve ids explícitos e cria as entidades criáveis.

    `create=False` (fonte derivada) resolve mas não cria: a publicação própria
    não é autorizada a inventar entidade nova (§8.4).
    """
    resolved: dict[str, ResolvedRef] = {}
    created: list[str] = []
    touched: list[str] = []

    for identifier in identifiers:
        if identifier.value in resolved:
            continue
        etype = identifier.entity_type
        if etype is None:
            resolved[identifier.value] = ResolvedRef(
                identifier,
                None,
                None,
                False,
                f"prefixo {identifier.prefix!r} sem tipo conhecido: referência preservada, "
                "entidade não inventada",
            )
            continue

        existing = identity.resolve_identity(repo.conn, ns, etype, identifier.value)
        if existing:
            resolved[identifier.value] = ResolvedRef(identifier, existing, etype, False, "id explícito")
            touched.append(existing)
            if create and etype in CREATABLE_TYPES:
                _refresh_lineage(rev, repo, ns, existing, svid, evidence_by_id.get(identifier.value, ()))
            continue

        if not create:
            resolved[identifier.value] = ResolvedRef(
                identifier,
                None,
                etype,
                False,
                "fonte derivada não cria entidade nova (§8.4)",
            )
            continue

        if etype not in CREATABLE_TYPES:
            resolved[identifier.value] = ResolvedRef(
                identifier,
                None,
                etype,
                False,
                f"{etype.value} não é criada a partir de documento: existência e "
                "comportamento de entidade técnica são atestados pelo pipeline de "
                "análise de código (§5.4, F10)",
            )
            continue

        mentioning = [c for c in candidates if any(i.value == identifier.value for i in c.explicit_ids)]
        draft = EntityDraft(
            namespace=ns,
            entity_type=etype,
            stable_key=identifier.value,
            title=_entity_title(identifier, mentioning, etype.value),
            source_version_id=svid,
            aliases=(Alias(identifier.value, AliasOrigin.METADATA_ID, svid),),
            attributes={"external_id": identifier.value, "origin": "document-ingestion"},
            lifecycle_status=_entity_lifecycle(etype, mentioning),
            evidence_refs=evidence_by_id.get(identifier.value, ()),
        )
        result = rev.put_entity(draft)
        resolved[identifier.value] = ResolvedRef(
            identifier, result.target_id, etype, True, "criada por id explícito"
        )
        created.append(result.target_id)
        touched.append(result.target_id)

    return resolved, created, touched


def _refresh_lineage(
    rev: Any, repo: Any, ns: str, entity_id: str, svid: str, evidence_refs: Sequence[str]
) -> None:
    """Reingestão aponta a entidade para a versão de fonte MAIS RECENTE.

    §8.3: "mesmo refinement_id quando identidade confirmada; nova revisão". A
    identidade é preservada (`entity_id` explícito) e o que muda é a linhagem.

    Título, atributos e vigência são COPIADOS do que já está lá — nunca
    recalculados a partir do texto novo. Sem isso, reingerir um documento
    reescreveria um título curado à mão e, pior, poderia promover uma proposta
    a vigente sem que ninguém tivesse decidido nada.
    """
    prev = repo.get_entity(entity_id, lifecycle=None)
    if prev is None:
        return
    etype = repo.entity_type_of(entity_id)
    if etype is None:
        return
    rev.put_entity(
        EntityDraft(
            namespace=ns,
            entity_type=etype,
            stable_key=prev.stable_key,
            title=prev.title,
            source_version_id=svid,
            aliases=(Alias(prev.stable_key, AliasOrigin.METADATA_ID, svid),),
            attributes=dict(prev.attributes or {}),
            lifecycle_status=prev.lifecycle_status,
            valid_from=prev.valid_from,
            valid_to=prev.valid_to,
            entity_id=entity_id,
            evidence_refs=tuple(evidence_refs),
        )
    )


def _assert_no_merge_by_score(suggestions: Iterable[Suggestion]) -> None:
    """§8.2.7/§8.3: nenhuma sugestão pode escapar do estado de candidata.

    Verificada antes de escrever. Se um dia alguém construir uma `Suggestion`
    com `candidate_only=False` para "só desta vez" fundir dois homônimos, a
    ingestão para aqui.
    """
    for s in suggestions:
        if not s.candidate_only:
            raise IdentityMergeRefused(
                f"sugestão {s.basis} (overlap={s.overlap:.2f}) para {s.entity_id} marcada como "
                "não-candidata; identidades não são fundidas por score de similaridade "
                "isolado e homônimos permanecem separados (§8.2.7, §8.3)"
            )


def _alias_and_lexical_suggestions(
    repo: Any,
    ns: str,
    candidates: Sequence[Candidate],
    already_resolved: Mapping[str, ResolvedRef],
) -> list[Suggestion]:
    """§8.2 passos 4-5: aproxima por alias e por sobreposição lexical.

    Devolve SUGESTÕES. Nenhuma delas vira identidade nem aresta confirmada; o
    máximo que produzem é uma aresta candidata (`inferred`) ou uma pendência.
    """
    resolved_ids = {r.entity_id for r in already_resolved.values() if r.entity_id}
    pool: list[tuple[str, str, EntityType, tuple[str, ...]]] = []
    for etype in (
        EntityType.INITIATIVE,
        EntityType.DECISION,
        EntityType.REQUIREMENT,
        EntityType.STORY,
        EntityType.REFINEMENT,
        EntityType.CAPABILITY,
        EntityType.BUSINESS_RULE,
    ):
        for entity in repo.find_entities(ns, etype, lifecycle=None):
            pool.append((entity.entity_id, entity.title, etype, normalize_tokens(entity.title)))

    out: dict[tuple[str, str], Suggestion] = {}
    for candidate in candidates:
        if candidate.has_explicit_id:
            continue
        ctokens = set(candidate.tokens)
        if not ctokens:
            continue
        for eid, title, etype, etokens in pool:
            if eid in resolved_ids or not etokens:
                continue
            shared = ctokens & set(etokens)
            if not shared:
                continue
            overlap = len(shared) / len(set(etokens))
            if overlap < LEXICAL_MIN_OVERLAP:
                continue
            key = (eid, candidate.primary_block.block_id)
            previous = out.get(key)
            if previous is not None and previous.overlap >= overlap:
                continue
            out[key] = Suggestion(
                text=candidate.text[:160],
                entity_id=eid,
                entity_title=title,
                entity_type=etype,
                basis="lexical",
                overlap=round(overlap, 3),
                block_id=candidate.primary_block.block_id,
            )
    return sorted(out.values(), key=lambda s: (-s.overlap, s.entity_id))


# --------------------------------------------------------------------------
# §8.2 passos 6-7 — arestas
# --------------------------------------------------------------------------


def _relation_type_for(source_type: EntityType, target_type: EntityType, change: bool) -> RelationType | None:
    """Aresta apropriada entre duas entidades citadas explicitamente.

    `None` significa "referência registrada, aresta não confirmada": é o caso
    da menção a uma entidade técnica sem marcador de alteração. Citar
    `CAP-023` como contexto não é propor mudança nela, e inventar uma aresta
    aí seria exatamente a "relação plausível" que §8.2.7 manda manter fora do
    grafo confirmado.
    """
    if target_type in TECHNICAL_TYPES:
        if change and source_type in INTENT_TYPES:
            return RelationType.PROPOSES_CHANGE_TO
        return None
    return LINK_PREFERENCE.get((source_type, target_type))


# --------------------------------------------------------------------------
# §8.2 passo 8 — divergência
# --------------------------------------------------------------------------


#: Classes cuja afirmação é SINGULAR por entidade — o escopo é compartilhado
#: entre fontes, então uma segunda fonte que diga outra coisa cai no mesmo
#: fato e a divergência aparece. `DEC-017` tem UMA decisão vigente; duas
#: transcrições que a descrevam de formas incompatíveis são uma contradição a
#: registrar, não dois fatos a conviver.
SHARED_SCOPE_KINDS: frozenset[CandidateKind] = frozenset(
    {CandidateKind.DECISION, CandidateKind.REQUIREMENT}
)


def _fact_scope(kind: CandidateKind, source_id: str) -> str:
    """Escopo do fato — decide o que conta como divergência (§8.2.8).

    Observação, dúvida, hipótese e ação são PER FONTE: duas atas podem
    observar coisas diferentes sobre a mesma iniciativa sem se contradizerem.
    Tratá-las como escopo compartilhado transformaria cada nova ata em uma
    contradição falsa, e o `disputed` deixaria de significar alguma coisa.
    """
    if kind in SHARED_SCOPE_KINDS:
        return "document-ingestion"
    return f"source:{source_id}"


def _source_entity_of_version(
    repo: Any, ns: str, source_version_id: str | None
) -> str | None:
    """Entidade `Source` que originou uma versão de fonte — para o `contradicts`."""
    if not source_version_id:
        return None
    row = repo.conn.execute(
        "SELECT source_id FROM source_versions WHERE source_version_id=?", (source_version_id,)
    ).fetchone()
    if row is None:
        return None
    eid = identity.entity_id(ns, EntityType.SOURCE, row[0])
    return eid if repo.entity_exists(eid) else None


# --------------------------------------------------------------------------
# §8.2 passo 9 — unidades afetadas
# --------------------------------------------------------------------------


def _affected_units(repo: Any, touched: Iterable[str]) -> tuple[str, ...]:
    """Entidades tocadas mais a vizinhança direta que precisa ser republicada.

    Um salto, nos dois sentidos: alterar RF-042 muda a unidade de RF-042 e a de
    DEC-017, que passa a exibir o refinamento. Fecho transitivo completo
    republicaria a base inteira a cada bloco novo.
    """
    units: set[str] = set()
    for eid in touched:
        units.add(eid)
        for rel in repo.neighbors(eid, direction="both", lifecycle=ALL_LIFECYCLE):
            units.add(rel.source_entity_id)
            units.add(rel.target_entity_id)
    return tuple(sorted(units))


# --------------------------------------------------------------------------
# Ambiguidade (§7.1)
# --------------------------------------------------------------------------


def _pending_initiative_decision(
    initiative_key: str | None,
    intent_entities: Sequence[str],
    orphans: Sequence[OrphanCandidate],
    candidate_initiatives: Sequence[str],
) -> PendingDecision | None:
    """A única pergunta admissível quando a iniciativa não é inequívoca.

    Só é feita quando a resposta MUDARIA um vínculo material — há entidade de
    intenção para ligar, ou há candidatos órfãos e mais de uma iniciativa
    plausível (o caso dos homônimos de §8.3). Uma fonte ambígua que não ligaria
    nada não vira pergunta: ela é aceita não correlacionada e o lote segue
    (aceite W5: ambiguidade sem impacto bloqueante não paralisa o corpus).
    """
    if initiative_key:
        return None
    plausible = tuple(sorted(set(candidate_initiatives)))
    material: str
    if intent_entities:
        material = (
            f"{len(intent_entities)} entidade(s) de intenção ficam sem belongs_to até a decisão"
        )
    elif orphans and plausible:
        material = (
            f"{len(orphans)} candidato(s) órfão(s) e {len(plausible)} iniciativa(s) "
            "homônima(s)/plausível(is); nenhuma é escolhida por semelhança (§8.3)"
        )
    else:
        return None
    return PendingDecision(
        key="initiative",
        question=(
            "A qual iniciativa esta fonte pertence? Sem id explícito, o vínculo "
            "belongs_to não é criado — proximidade de data não autoriza associação (§8.3)."
        ),
        options=plausible,
        material_effect=material,
    )


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def correlate(
    candidates: ExtractionCandidates,
    doc: Any,
    repo: Any,
    namespace: str,
) -> CorrelationResult:
    """Os dez passos de §8.2 numa revisão atômica.

    Devolve sempre um `CorrelationResult`: ambiguidade, referência não
    resolvida e fonte derivada são RESULTADOS, não exceções. Só erro de
    contrato ou violação de invariante do repositório levanta.
    """
    ns = identity.normalize_namespace(namespace)
    assert_no_implemented(candidates.all())  # §5.4/F10 antes de qualquer escrita

    source_kind = _source_kind_for(doc)
    content_kind = _content_kind_for(source_kind, doc)
    uri = _doc_uri(doc)
    bytes_sha = str(_attr(doc, "bytes_sha256", "") or "")
    if not bytes_sha:
        raise CorrelationError(
            "SourceDocument sem bytes_sha256: preservar bytes/hash antes da extração "
            "é pré-condição de §8.1"
        )
    version_label = f"sha256:{bytes_sha[:16]}"

    sid = identity.source_id(ns, uri)
    svid = identity.source_version_id(sid, version_label, bytes_sha)

    result = CorrelationResult(namespace=ns, source_id=sid, source_version_id=svid)
    diagnostics: list[str] = list(candidates.diagnostics)

    # ------------------------------------------------------ passo 1: duplicada
    if _already_ingested(repo.conn, svid):
        result.duplicate = True
        result.source_entity_id = identity.entity_id(ns, EntityType.SOURCE, sid)
        diagnostics.append(
            f"fonte já ingerida nesta versão ({version_label}); hash e identidade idênticos: "
            "nenhum fato ou aresta novo (§8.2.1)"
        )
        result.diagnostics = tuple(diagnostics)
        return result

    derived = detect_derived(doc, repo, ns)
    result.derived = derived
    if derived.is_derived:
        diagnostics.append(derived.reason)
    if derived.authored_by_agent:
        diagnostics.append(
            "fonte autorada por agente: pode propor história/refinamento, "
            "não pode confirmar a própria correção (§8.4)"
        )

    write_evidence = derived.independent_evidence
    all_candidates = candidates.all()
    identifiers = candidates.explicit_ids()

    with repo.revision(
        author=REVISION_AUTHOR,
        reason=f"ingestão de {uri} ({version_label})",
    ) as rev:
        result.revision_id = rev.revision_id
        source = repo.register_source(ns, source_kind, uri)
        repo.register_source_version(
            source,
            version_label,
            bytes_sha,
            metadata={
                k: v
                for k, v in _raw_metadata(doc).items()
                if k in ("initiative_id", "phase", "participants", "date", "title", "source_type")
            },
        )

        # -------------------------------------------- evidências dos blocos
        evidence_of_block: dict[str, str] = {}
        evidence_by_id: dict[str, list[str]] = {}
        if write_evidence:
            for candidate in all_candidates:
                ref = candidate.primary_block
                block_source_kind, block_content_kind = _evidence_kinds(ref, doc)
                locator = _locator_for(block_source_kind, doc, ref, version_label)
                ev = ev_mod.make_evidence(
                    ns, block_source_kind, block_content_kind, svid, locator
                )
                _assert_cannot_support_implemented(ev)
                evidence_of_block[ref.block_id] = rev.add_evidence(ev)
                for identifier in candidate.explicit_ids:
                    evidence_by_id.setdefault(identifier.value, []).append(ev.evidence_id)
            # A metadata que declara a iniciativa também é um trecho citável da
            # fonte. Sem esta evidência, um refinamento que só declara
            # `initiative_id` no cabeçalho ficaria com `belongs_to` sem
            # sustentação — e a alternativa (aceitar `supported` sem citação)
            # é justamente o que §5.3 proíbe.
            declared = _raw_metadata(doc).get("initiative_id")
            if declared:
                meta_ev = ev_mod.make_evidence(
                    ns,
                    source_kind,
                    content_kind,
                    svid,
                    _metadata_locator(source_kind, doc, version_label),
                )
                _assert_cannot_support_implemented(meta_ev)
                rev.add_evidence(meta_ev)
                for identifier in parse_explicit_ids(str(declared)) or ():
                    evidence_by_id.setdefault(identifier.value, []).append(meta_ev.evidence_id)
        else:
            diagnostics.append(
                "fonte derivada: blocos NÃO viram evidência independente; só a linhagem "
                "derived_from é preservada (§8.4)"
            )

        # ------------------------------------------------ entidade da fonte
        source_entity = rev.put_entity(
            EntityDraft(
                namespace=ns,
                entity_type=EntityType.SOURCE,
                stable_key=sid,
                title=str(_raw_metadata(doc).get("title") or uri),
                source_version_id=svid,
                attributes={
                    "uri": uri,
                    "kind": str(_attr(doc, "kind", "") or ""),
                    "bytes_sha256": bytes_sha,
                    "size": _attr(doc, "size", None),
                    "status": str(_attr(doc, "status", "") or ""),
                    "source_type": str(_raw_metadata(doc).get("source_type") or ""),
                    "phase": candidates.phase_hint or "",
                    "derived": derived.is_derived,
                },
                lifecycle_status=LifecycleStatus.CURRENT,
                evidence_refs=tuple(dict.fromkeys(evidence_of_block.values())),
            )
        )
        result.source_entity_id = source_entity.target_id
        touched: list[str] = [source_entity.target_id]

        # ------------------------------------------ passos 2-3: ids explícitos
        resolved, created, resolved_touched = _resolve_explicit_ids(
            rev,
            repo,
            ns,
            identifiers,
            all_candidates,
            svid,
            {k: tuple(v) for k, v in evidence_by_id.items()},
            create=not derived.is_derived,
        )
        touched.extend(resolved_touched)
        result.entities_created = tuple(created)
        result.resolved_refs = tuple(r for r in resolved.values() if r.resolved)
        result.unresolved_refs = tuple(r for r in resolved.values() if not r.resolved)

        # ------------------------------------------ passos 4-5: aproximações
        suggestions = _alias_and_lexical_suggestions(repo, ns, all_candidates, resolved)
        _assert_no_merge_by_score(suggestions)
        result.suggestions = tuple(suggestions)

        # ------------------------------------------- iniciativa da fonte
        initiative_key = candidates.initiative_hint
        initiative_ref = resolved.get(initiative_key or "")
        if initiative_key and initiative_ref is None:
            # Iniciativa veio da metadata e não foi citada em nenhum bloco.
            extra = parse_explicit_ids(initiative_key)
            if extra:
                more, more_created, more_touched = _resolve_explicit_ids(
                    rev,
                    repo,
                    ns,
                    extra,
                    all_candidates,
                    svid,
                    {k: tuple(v) for k, v in evidence_by_id.items()},
                    create=not derived.is_derived,
                )
                resolved.update(more)
                created.extend(more_created)
                touched.extend(more_touched)
                initiative_ref = resolved.get(initiative_key)
                result.entities_created = tuple(created)
        initiative_id = initiative_ref.entity_id if initiative_ref and initiative_ref.resolved else None
        result.initiative_key = initiative_key
        result.initiative_id = initiative_id

        # --------------------------------------------- sujeito do documento
        subject_ref = _document_subject(resolved, doc)

        # ----------------------------------- passo 8 + fatos por candidato
        facts: list[str] = []
        disputed: list[str] = []
        contradictions: list[WrittenRelation] = []
        orphans: list[OrphanCandidate] = []

        grouped = _group_candidates(all_candidates, resolved)
        for (entity_id, kind), group in grouped.items():
            if entity_id is None:
                for c in group:
                    orphans.append(
                        OrphanCandidate(
                            text=c.text[:200],
                            kind=c.kind.value,
                            block_id=c.primary_block.block_id,
                            reason=(
                                "fonte derivada: sem evidência independente (§8.4)"
                                if derived.is_derived
                                else "sem id explícito resolvido: correlação por "
                                "similaridade não confirma vínculo (§8.2.7)"
                            ),
                        )
                    )
                continue
            if not write_evidence:
                for c in group:
                    orphans.append(
                        OrphanCandidate(
                            text=c.text[:200],
                            kind=c.kind.value,
                            block_id=c.primary_block.block_id,
                            reason="fonte derivada: linhagem preservada, sem fato novo (§8.4)",
                        )
                    )
                continue
            written, was_disputed, contradiction = _write_statement_fact(
                rev, repo, ns, entity_id, kind, group, svid, sid, evidence_of_block, derived
            )
            if written:
                facts.append(written)
                touched.append(entity_id)
            if was_disputed:
                disputed.append(written or "")
            if contradiction is not None:
                contradictions.append(contradiction)

        result.facts_written = tuple(facts)
        result.disputed_facts = tuple(d for d in disputed if d)
        result.contradictions = tuple(contradictions)
        result.orphans = tuple(orphans)

        # --------------------------------------- passos 6-7: arestas do grafo
        confirmed: list[WrittenRelation] = []
        candidate_edges: list[WrittenRelation] = []

        if write_evidence:
            confirmed.extend(
                _write_records_edges(rev, repo, ns, source_entity.target_id, resolved, svid, evidence_by_id, derived)
            )
            confirmed.extend(
                _write_belongs_to(
                    rev, repo, ns, initiative_id, resolved, all_candidates, svid, evidence_by_id, derived
                )
            )
            written, mentions_only, negation_facts = _write_subject_links(
                rev, repo, ns, subject_ref, resolved, all_candidates, svid, sid, evidence_by_id, derived
            )
            confirmed.extend(written)
            for note in mentions_only:
                diagnostics.append(note)
            if negation_facts:
                # §R3: a negação declarada fica consultável pelo MESMO canal
                # que os demais fatos — não só num diagnóstico de texto.
                result.facts_written = result.facts_written + tuple(negation_facts)
            candidate_edges.extend(
                _write_candidate_edges(
                    rev, repo, ns, source_entity.target_id, suggestions, svid, evidence_of_block, derived
                )
            )
        elif derived.origin_entity_id:
            lineage = _put_relation(
                rev,
                repo,
                ns,
                source_entity.target_id,
                RelationType.DERIVED_FROM,
                derived.origin_entity_id,
                scope="publication-lineage",
                epistemic=EpistemicStatus.SUPPORTED,
                lifecycle=LifecycleStatus.CURRENT,
                svid=svid,
                evidence_refs=(),
                derived=derived,
                attributes={"publication_id": derived.publication_id or ""},
            )
            if lineage:
                confirmed.append(lineage)
                touched.append(derived.origin_entity_id)
        elif derived.is_derived:
            diagnostics.append(
                f"publicação própria {derived.publication_id!r} sem entidade de origem "
                "registrada: linhagem não pôde ser ligada; fonte preservada sem evidência"
            )

        result.relations_written = tuple(confirmed)
        result.candidate_relations = tuple(candidate_edges)

        # --------------------------------------------- passo 9: unidades
        touched_unique = tuple(dict.fromkeys(touched))
        result.entities_touched = touched_unique
        result.affected_units = _affected_units(repo, touched_unique)

        # ------------------------------------------- ambiguidade material
        intent_entities = [
            r.entity_id
            for r in resolved.values()
            if r.resolved and r.entity_type in INTENT_TYPES
        ]
        pending = _pending_initiative_decision(
            initiative_id,
            intent_entities,
            result.orphans,
            [s.entity_title for s in suggestions if s.entity_type is EntityType.INITIATIVE],
        )
        result.pending_decisions = (pending,) if pending else ()

        # -------------------------------- passo 10: republicação via outbox
        effect = rev.enqueue_effect(
            "publication.republish_required",
            {
                "source_version_id": svid,
                "namespace": ns,
                "units": list(result.affected_units),
                "contract": _contract_stamp(),
                "reason": "correlação de fonte ingerida (§8.2.10)",
            },
        )
        result.effects = (effect,)

    result.diagnostics = tuple(diagnostics)
    return result


def correlate_batch(
    items: Sequence[tuple[ExtractionCandidates, Any]],
    repo: Any,
    namespace: str,
) -> list[CorrelationResult]:
    """Correlaciona um lote, isolando a falha de cada fonte.

    Aceite W5: "ambiguidade sem impacto bloqueante não paralisa todo o corpus"
    e §7.1: "falha em um arquivo não apaga sucesso dos demais". Cada item tem a
    própria revisão; a exceção de um vira `result.error` e o laço continua.
    """
    out: list[CorrelationResult] = []
    for candidates, doc in items:
        try:
            out.append(correlate(candidates, doc, repo, namespace))
        except Exception as exc:  # isolamento por fonte é o requisito
            out.append(
                CorrelationResult(
                    namespace=namespace,
                    source_id=str(_attr(doc, "source_id", "") or ""),
                    source_version_id="",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return out


# --------------------------------------------------------------------------
# Escrita
# --------------------------------------------------------------------------


def _document_subject(resolved: Mapping[str, ResolvedRef], doc: Any) -> ResolvedRef | None:
    """Entidade em nome de quem o documento fala (§8.3: o doc de RF-042 é RF-042).

    Dois sinais, ambos deliberadamente restritivos:

    1. o TÍTULO declarado na metadata ("RF-042", "US-031");
    2. senão, o primeiro id do PRIMEIRO bloco que cita algum id — um artefato
       se nomeia na abertura ("Refinamento RF-042 detalha a decisão DEC-017",
       "A história US-031 deriva do refinamento RF-042").

    Não existe fallback por tipo, e a ausência é o requisito. Uma transcrição
    de inception não fala em nome de nenhuma entidade: ela registra várias.
    Elegendo um sujeito por preferência de tipo, a primeira decisão citada
    viraria a "dona" do documento e apareceria ligada por `derived_from` a
    todas as outras — arestas que ninguém afirmou. Sem sujeito, sobram
    `belongs_to` e `records`, que são exatamente o que a fonte sustenta.

    Também é por isso que a busca para no PRIMEIRO bloco com id: se ele nomeia
    a iniciativa (que não é sujeito possível), o documento é uma ata, não um
    artefato — e a resposta correta é `None`.
    """
    declared = str(_raw_metadata(doc).get("title") or "")
    for identifier in parse_explicit_ids(declared):
        ref = resolved.get(identifier.value)
        if ref and ref.resolved and ref.entity_type in SUBJECT_PREFERENCE:
            return ref

    for block in _attr(doc, "blocks", ()) or ():
        identifiers = parse_explicit_ids(str(_attr(block, "text", "") or ""))
        if not identifiers:
            continue
        for identifier in identifiers:
            ref = resolved.get(identifier.value)
            if ref and ref.resolved and ref.entity_type in SUBJECT_PREFERENCE:
                return ref
        return None
    return None


def _group_candidates(
    candidates: Sequence[Candidate], resolved: Mapping[str, ResolvedRef]
) -> dict[tuple[str | None, CandidateKind], list[Candidate]]:
    """Agrupa candidatos por (entidade citada, classe).

    O agrupamento é o que permite detectar divergência ENTRE FONTES no passo 8:
    duas transcrições que declaram decisões diferentes para o mesmo `DEC-017`
    caem no mesmo fato (`subject`+`predicate`+`scope`) e a segunda encontra a
    primeira. Fatiar por bloco esconderia a contradição em dois fatos distintos.
    """
    groups: dict[tuple[str | None, CandidateKind], list[Candidate]] = {}
    for candidate in candidates:
        targets = [
            resolved[i.value].entity_id
            for i in candidate.explicit_ids
            if i.value in resolved and resolved[i.value].resolved
        ]
        if not targets:
            groups.setdefault((None, candidate.kind), []).append(candidate)
            continue
        for entity_id in dict.fromkeys(targets):
            groups.setdefault((entity_id, candidate.kind), []).append(candidate)
    return groups


def _assert_no_implemented_override(repo: Any, fact_id: str) -> None:
    """Documento não altera fato `implemented` (§8.3, aceite W5).

    Predicados de ingestão são distintos dos predicados do pipeline de código,
    então a colisão não deveria acontecer. Esta função existe para o caso em
    que passe a acontecer: prefere-se parar a ingestão a reescrever silen-
    ciosamente um comportamento implementado a partir de prosa.
    """
    prev = repo.get_fact(fact_id, lifecycle=None)
    if prev is not None and prev.nature is FactNature.IMPLEMENTED:
        raise ImplementedOverrideRefused(
            f"fato {fact_id} tem nature=implemented; refinamento/documento não altera "
            "comportamento implementado sem nova evidência de código (§8.3)"
        )


def _write_statement_fact(
    rev: Any,
    repo: Any,
    ns: str,
    entity_id: str,
    kind: CandidateKind,
    group: Sequence[Candidate],
    svid: str,
    source_id: str,
    evidence_of_block: Mapping[str, str],
    derived: DerivedInfo,
) -> tuple[str | None, bool, WrittenRelation | None]:
    """Grava o fato declarado por um grupo de candidatos, tratando divergência.

    Devolve `(fact_id, disputado, contradicts)`. A divergência de §8.2.8 vira
    `epistemic=disputed` com as evidências DOS DOIS LADOS no mesmo fato, mais
    uma aresta `contradicts` entre as duas fontes — que é onde a contradição
    de fato mora: dois documentos, não uma entidade contra si mesma.
    """
    if not derived.independent_evidence:
        # Belt: o chamador já desvia fontes derivadas para `orphans`. Se algum
        # dia deixar de desviar, a publicação própria NÃO passa a se confirmar
        # sozinha por aqui (§8.4).
        raise DerivedEvidenceRefused(
            "fonte derivada (publicação própria) tentando gravar fato com evidência "
            "independente; a reingestão preserva a linhagem, não cria sustentação nova (§8.4)"
        )
    primary = group[0]
    predicate = f"{kind.value}.statement"
    scope = _fact_scope(kind, source_id)
    fid = identity.fact_id(ns, entity_id, predicate, scope)
    _assert_no_implemented_override(repo, fid)

    refs = tuple(
        dict.fromkeys(
            evidence_of_block[c.primary_block.block_id]
            for c in group
            if c.primary_block.block_id in evidence_of_block
        )
    )
    if not refs:
        return None, False, None

    value = " ".join(c.text for c in group).strip()
    epistemic = _agent_gate(
        EpistemicStatus.SUPPORTED if primary.epistemic is EpistemicStatus.INFERRED else primary.epistemic,
        derived,
    )
    lifecycle = primary.lifecycle
    contradiction: WrittenRelation | None = None
    disputed = False

    prev = repo.get_fact(fid, lifecycle=None)
    if prev is not None and prev.value != value:
        disputed = True
        epistemic = EpistemicStatus.DISPUTED
        refs = tuple(dict.fromkeys(tuple(prev.evidence_refs) + refs))
        other = _source_entity_of_version(repo, ns, prev.source_version_id)
        mine = _source_entity_of_version(repo, ns, svid)
        if other and mine and other != mine:
            contradiction = _put_relation(
                rev,
                repo,
                ns,
                mine,
                RelationType.CONTRADICTS,
                other,
                scope=f"fact:{fid}",
                epistemic=EpistemicStatus.SUPPORTED,
                lifecycle=LifecycleStatus.CURRENT,
                svid=svid,
                evidence_refs=refs,
                derived=derived,
                attributes={"fact_id": fid, "predicate": predicate},
            )

    draft = FactDraft(
        namespace=ns,
        subject_id=entity_id,
        predicate=predicate,
        value=value,
        scope=scope,
        nature=primary.nature,  # nunca IMPLEMENTED: garantido em extract.Candidate
        epistemic_status=epistemic,
        lifecycle_status=lifecycle,
        asserted_by=ASSERTED_BY,
        evidence_refs=refs,
        source_version_id=svid,
        support_recorded_by=(
            SUPPORT_RECORDED_BY if epistemic is EpistemicStatus.SUPPORTED else None
        ),
    )
    written = rev.put_fact(draft)
    return written.target_id, disputed, contradiction


def _put_relation(
    rev: Any,
    repo: Any,
    ns: str,
    source_entity_id: str,
    relation_type: RelationType,
    target_entity_id: str,
    scope: str,
    epistemic: EpistemicStatus,
    lifecycle: LifecycleStatus,
    svid: str,
    evidence_refs: Sequence[str],
    derived: DerivedInfo,
    attributes: Mapping[str, Any] | None = None,
) -> WrittenRelation | None:
    """Grava uma aresta validando o par de tipos ANTES de tocar o banco.

    `validate_pair` já é chamado por `repository.put_relation`; chamá-lo aqui
    permite degradar para "menção não confirmada" em vez de derrubar a revisão
    inteira por causa de uma citação com direção inesperada.
    """
    if source_entity_id == target_entity_id:
        return None
    stype = repo.entity_type_of(source_entity_id)
    ttype = repo.entity_type_of(target_entity_id)
    if stype is None or ttype is None:
        return None
    try:
        validate_pair(relation_type, stype, ttype)
    except InvalidRelationPair:
        return None

    epistemic = _agent_gate(epistemic, derived)
    if epistemic is EpistemicStatus.SUPPORTED and not evidence_refs:
        # Sem citação não há sustentação (§5.3). Rebaixar é o comportamento
        # correto — e é melhor do que a alternativa de derrubar a revisão
        # inteira por uma aresta que ainda vale como candidata.
        epistemic = EpistemicStatus.INFERRED

    rid = identity.relation_id(ns, source_entity_id, relation_type.value, target_entity_id, scope)
    prev = repo.get_relation(rid, lifecycle=ALL_LIFECYCLE)
    if (
        prev is not None
        and prev.epistemic_status is EpistemicStatus.SUPPORTED
        and epistemic is EpistemicStatus.INFERRED
    ):
        # Uma fonte fraca NÃO retira sustentação já registrada. Reingerir a
        # resposta de um agente que menciona `RF-042 refines DEC-017` não pode
        # rebaixar a aresta que a referência explícita do refinamento sustentou
        # (§8.4: o agente propõe, não confirma nem desconfirma). Retirar
        # sustentação é ato explícito de invalidação, não efeito colateral de
        # ingestão.
        return WrittenRelation(
            relation_id=rid,
            source_entity_id=source_entity_id,
            relation_type=relation_type.value,
            target_entity_id=target_entity_id,
            epistemic=prev.epistemic_status.value,
            lifecycle=prev.lifecycle_status.value,
            changed=False,
        )
    if (
        prev is not None
        and prev.lifecycle_status is LifecycleStatus.CURRENT
        and lifecycle is LifecycleStatus.PROPOSED
    ):
        # Um vínculo já vigente não volta a ser proposta porque um documento em
        # fase de refinamento o mencionou. Despromover vigência é ato de
        # `set_relation_lifecycle`, com autor e motivo — não efeito colateral.
        lifecycle = LifecycleStatus.CURRENT
    result = rev.put_relation(
        RelationDraft(
            namespace=ns,
            source_entity_id=source_entity_id,
            relation_type=relation_type,
            target_entity_id=target_entity_id,
            scope=scope,
            epistemic_status=epistemic,
            lifecycle_status=lifecycle,
            asserted_by=ASSERTED_BY,
            evidence_refs=tuple(evidence_refs),
            source_version_id=svid,
            support_recorded_by=(
                SUPPORT_RECORDED_BY if epistemic is EpistemicStatus.SUPPORTED else None
            ),
            attributes=dict(attributes or {}),
        )
    )
    return WrittenRelation(
        relation_id=result.target_id,
        source_entity_id=source_entity_id,
        relation_type=relation_type.value,
        target_entity_id=target_entity_id,
        epistemic=epistemic.value,
        lifecycle=lifecycle.value,
        changed=result.changed,
    )


def _write_records_edges(
    rev: Any,
    repo: Any,
    ns: str,
    source_entity_id: str,
    resolved: Mapping[str, ResolvedRef],
    svid: str,
    evidence_by_id: Mapping[str, Sequence[str]],
    derived: DerivedInfo,
) -> list[WrittenRelation]:
    """`Source records X` para cada entidade citada com id explícito.

    É a linhagem exigida por §8.3 ("a linhagem registra quais fatos publicados
    vieram da inception, do refinamento ou do código"): sem esta aresta, saber
    qual documento trouxe cada entidade dependeria de varrer evidências.
    """
    out: list[WrittenRelation] = []
    for ref in resolved.values():
        if not ref.resolved:
            continue
        edge = _put_relation(
            rev,
            repo,
            ns,
            source_entity_id,
            RelationType.RECORDS,
            ref.entity_id or "",
            scope="document-ingestion",
            epistemic=EpistemicStatus.SUPPORTED,
            lifecycle=LifecycleStatus.CURRENT,
            svid=svid,
            evidence_refs=tuple(evidence_by_id.get(ref.identifier.value, ())),
            derived=derived,
            attributes={"external_id": ref.identifier.value},
        )
        if edge:
            out.append(edge)
    return out


def _proposed_from(candidates: Sequence[Candidate], external_id: str) -> bool:
    """Algum candidato que cita este id nasceu proposto?"""
    return any(
        c.lifecycle is LifecycleStatus.PROPOSED
        for c in candidates
        if any(i.value == external_id for i in c.explicit_ids)
    )


def _entity_lifecycle_of(repo: Any, entity_id: str | None, fallback_proposed: bool) -> LifecycleStatus:
    """Vigência a usar numa aresta: a da entidade, com o candidato como reserva.

    A entidade é a autoridade porque ela persiste entre ingestões; a fase do
    documento que a citou desta vez, não. Sem isso, ingerir um refinamento
    (fase de proposta) que menciona uma decisão vigente rebaixaria o
    `belongs_to` dessa decisão a proposto — um documento novo desfazendo um
    vínculo estabelecido, que é o oposto de ingestão incremental.
    """
    entity = repo.get_entity(entity_id or "", lifecycle=None) if entity_id else None
    if entity is not None:
        return (
            LifecycleStatus.PROPOSED
            if entity.lifecycle_status is LifecycleStatus.PROPOSED
            else LifecycleStatus.CURRENT
        )
    return LifecycleStatus.PROPOSED if fallback_proposed else LifecycleStatus.CURRENT


def _write_candidate_edges(
    rev: Any,
    repo: Any,
    ns: str,
    source_entity_id: str,
    suggestions: Sequence[Suggestion],
    svid: str,
    evidence_of_block: Mapping[str, str],
    derived: DerivedInfo,
) -> list[WrittenRelation]:
    """§8.2.7: aproximação lexical vira aresta CANDIDATA, nunca identidade.

    A aresta é `Source records X` com `epistemic=inferred` e sem
    `support_recorded_by` — a consulta padrão de sustentação a distingue da
    citação por id explícito. Quando o texto casa com dois homônimos, saem DUAS
    arestas candidatas e as duas entidades continuam separadas: é exatamente o
    resultado que §8.3 exige, e é o oposto de escolher a de maior score.
    """
    out: list[WrittenRelation] = []
    for suggestion in suggestions:
        edge = _put_relation(
            rev,
            repo,
            ns,
            source_entity_id,
            RelationType.RECORDS,
            suggestion.entity_id,
            scope=f"lexical:{suggestion.block_id}",
            epistemic=EpistemicStatus.INFERRED,
            lifecycle=LifecycleStatus.CURRENT,
            svid=svid,
            evidence_refs=tuple(
                x for x in (evidence_of_block.get(suggestion.block_id),) if x
            ),
            derived=derived,
            attributes={
                "basis": suggestion.basis,
                "overlap": suggestion.overlap,
                "candidate_only": True,
                "matched_title": suggestion.entity_title,
            },
        )
        if edge:
            out.append(edge)
    return out


def _write_belongs_to(
    rev: Any,
    repo: Any,
    ns: str,
    initiative_id: str | None,
    resolved: Mapping[str, ResolvedRef],
    candidates: Sequence[Candidate],
    svid: str,
    evidence_by_id: Mapping[str, Sequence[str]],
    derived: DerivedInfo,
) -> list[WrittenRelation]:
    """`X belongs_to Initiative` — só com iniciativa resolvida por ID EXPLÍCITO.

    Sem id, nada é criado: §8.3 é literal em "ausência de ID da história no
    commit não autoriza associar por mera proximidade temporal", e a data do
    documento não entra nesta função em momento algum.
    """
    if not initiative_id:
        return []
    out: list[WrittenRelation] = []
    for ref in resolved.values():
        if not ref.resolved or ref.entity_type not in INTENT_TYPES:
            continue
        # A vigência do vínculo é a da ENTIDADE, não a da fase do documento que
        # a citou. `RF-042` é proposta, logo `RF-042 belongs_to INI-008` é
        # proposta; `DEC-017` é vigente e continua vigente mesmo quando um
        # refinamento (fase de proposta) a menciona de passagem.
        lifecycle = _entity_lifecycle_of(
            repo, ref.entity_id, _proposed_from(candidates, ref.identifier.value)
        )
        edge = _put_relation(
            rev,
            repo,
            ns,
            ref.entity_id or "",
            RelationType.BELONGS_TO,
            initiative_id,
            scope="initiative",
            epistemic=EpistemicStatus.SUPPORTED,
            lifecycle=lifecycle,
            svid=svid,
            evidence_refs=tuple(evidence_by_id.get(ref.identifier.value, ())),
            derived=derived,
            attributes={"basis": "id explícito"},
        )
        if edge:
            out.append(edge)
    return out


#: Escopo fixo das arestas de §8.3 (sujeito -> referência citada). Repetido
#: aqui como constante porque `_dispute_relation_on_negation` precisa montar
#: o MESMO `relation_id` que `_write_subject_links` já usou para achar a
#: aresta afirmativa a contestar — duplicar o literal seria o tipo de
#: divergência silenciosa que quebra a busca por `identity.relation_id`.
_SUBJECT_LINK_SCOPE = "document-ingestion"


def _mention_polarities(candidates: Sequence[Candidate], value: str) -> tuple[MentionPolarity, ...]:
    """Polaridade de cada menção ao id `value` nos candidatos dados (§ R3)."""
    return tuple(
        i.mention_polarity for c in candidates for i in c.explicit_ids if i.value == value
    )


def _combined_polarity(polarities: Sequence[MentionPolarity]) -> MentionPolarity:
    """Combina polaridades de várias menções ao MESMO id no MESMO documento.

    Prioridade conservadora — nunca a favor de `supported`: uma negação em
    qualquer menção pesa mais que uma afirmação em outra (documento
    internamente inconsistente não deveria gravar a relação como sustentada
    de qualquer forma); neutra pesa mais que afirmada pelo mesmo motivo.
    Sem nenhuma menção, o default é `affirmed` (compatibilidade com chamadas
    sem grupo de candidatos).
    """
    if not polarities:
        return MentionPolarity.AFFIRMED
    if MentionPolarity.NEGATED in polarities:
        return MentionPolarity.NEGATED
    if MentionPolarity.NEUTRAL in polarities:
        return MentionPolarity.NEUTRAL
    return MentionPolarity.AFFIRMED


def _write_declared_negation(
    rev: Any,
    repo: Any,
    ns: str,
    subject_entity_id: str,
    relation_type: RelationType,
    ref: ResolvedRef,
    group: Sequence[Candidate],
    svid: str,
    source_id: str,
    refs: Sequence[str],
    derived: DerivedInfo,
) -> str | None:
    """Fato consultável de negação declarada (§ R3, achado bloqueante nº3).

    "RF-042 nao refina a decisao DEC-017" NUNCA cria `RF-042 refines
    DEC-017` — é exatamente o contrário do que a fonte afirma. Mas a negação
    em si é informação: fica gravada como fato `declared_not_<relacao>`,
    consultável, com a evidência do bloco que a afirma. `scope=ref.entity_id`
    torna o fato estável por par (sujeito, predicado, alvo): reingestão da
    mesma negação apenas acumula evidência, não duplica.
    """
    if not refs:
        return None
    value = " ".join(c.text for c in group).strip() or (
        f"{subject_entity_id} nao {relation_type.value} {ref.entity_id} "
        f"(fonte {source_id}, id citado {ref.identifier.value})"
    )
    epistemic = _agent_gate(EpistemicStatus.SUPPORTED, derived)
    draft = FactDraft(
        namespace=ns,
        subject_id=subject_entity_id,
        predicate=f"declared_not_{relation_type.value}",
        value=value,
        scope=ref.entity_id or "",
        nature=FactNature.OBSERVED,
        epistemic_status=epistemic,
        lifecycle_status=LifecycleStatus.CURRENT,
        asserted_by=ASSERTED_BY,
        evidence_refs=tuple(refs),
        source_version_id=svid,
        support_recorded_by=(SUPPORT_RECORDED_BY if epistemic is EpistemicStatus.SUPPORTED else None),
    )
    written = rev.put_fact(draft)
    return written.target_id


def _dispute_relation_on_negation(
    rev: Any,
    repo: Any,
    ns: str,
    subject_entity_id: str,
    relation_type: RelationType,
    ref: ResolvedRef,
    svid: str,
    refs: Sequence[str],
    derived: DerivedInfo,
) -> WrittenRelation | None:
    """Negação chegando sobre uma relação já `supported` de OUTRA fonte vira
    `disputed`, com evidência dos dois lados (§8.2.8 aplicado a relações: o
    mesmo mecanismo que `_write_statement_fact` usa para fatos divergentes).

    Nunca deleta a relação: ela continua existindo, só deixa de ser
    incontestável — "o grafo nunca mostra o contrário da fonte" também vale
    ao contrário, uma negação isolada não apaga o que outra fonte sustentou.
    Uma fonte se contradizendo consigo mesma (`other == mine`) fica fora
    deste mecanismo: §8.2.8 é sobre divergência ENTRE fontes.
    """
    if not refs:
        return None
    rid = identity.relation_id(
        ns, subject_entity_id, relation_type.value, ref.entity_id or "", _SUBJECT_LINK_SCOPE
    )
    prev = repo.get_relation(rid, lifecycle=ALL_LIFECYCLE)
    if prev is None or prev.epistemic_status is not EpistemicStatus.SUPPORTED:
        return None
    other = _source_entity_of_version(repo, ns, prev.source_version_id)
    mine = _source_entity_of_version(repo, ns, svid)
    if other is not None and mine is not None and other == mine:
        return None
    combined_refs = tuple(dict.fromkeys(tuple(prev.evidence_refs) + tuple(refs)))
    return _put_relation(
        rev,
        repo,
        ns,
        subject_entity_id,
        relation_type,
        ref.entity_id or "",
        scope=_SUBJECT_LINK_SCOPE,
        epistemic=EpistemicStatus.DISPUTED,
        lifecycle=prev.lifecycle_status,
        svid=svid,
        evidence_refs=combined_refs,
        derived=derived,
        attributes={
            "basis": "negação declarada sobre relação sustentada (§R3)",
            "external_id": ref.identifier.value,
        },
    )


def _write_subject_links(
    rev: Any,
    repo: Any,
    ns: str,
    subject: ResolvedRef | None,
    resolved: Mapping[str, ResolvedRef],
    candidates: Sequence[Candidate],
    svid: str,
    source_id: str,
    evidence_by_id: Mapping[str, Sequence[str]],
    derived: DerivedInfo,
) -> tuple[list[WrittenRelation], list[str], list[str]]:
    """Arestas do sujeito do documento para o que ele referencia (§8.3, § R3).

    Produz `RF-042 refines DEC-017` e `RF-042 proposes_change_to RN-023`, e
    devolve como MENÇÕES (segundo elemento) as citações técnicas sem marcador
    de alteração — registradas, não transformadas em aresta confirmada.

    A decisão NÃO é mais só o par de tipos das entidades (esse era o achado
    bloqueante nº3: "RF-042 nao refina DEC-017" virava `refines supported`
    porque só o par Refinement->Decision era olhado). Agora o par de tipos
    decide o TIPO de relação (`_relation_type_for`), e a polaridade da menção
    (§ R3, `extract.MentionPolarity`) decide o que fazer com ela:

    - `affirmed`: comportamento de sempre — aresta `supported`.
    - `neutral` (comparação/citação, ex. "diferente de"): aresta CANDIDATA,
      `epistemic=inferred` com `attributes.candidate_only=True` — nunca
      `supported` (§8.2.7).
    - `negated`: a aresta afirmativa NÃO é criada; grava-se um fato
      `declared_not_<relacao>` consultável, e se já existir aresta
      `supported` de outra fonte para o mesmo par, ela vira `disputed` com
      evidência dos dois lados (nunca é apagada).
    """
    if subject is None or not subject.resolved or subject.entity_type is None:
        return [], [], []

    out: list[WrittenRelation] = []
    notes: list[str] = []
    negation_facts: list[str] = []
    for ref in resolved.values():
        if not ref.resolved or ref.entity_id == subject.entity_id or ref.entity_type is None:
            continue
        if ref.entity_type is EntityType.INITIATIVE:
            continue  # coberto por belongs_to
        mentioning = [
            c for c in candidates if any(i.value == ref.identifier.value for i in c.explicit_ids)
        ]
        change = any(c.proposes_change for c in mentioning)
        rtype = _relation_type_for(subject.entity_type, ref.entity_type, change)
        if rtype is None:
            notes.append(
                f"menção a {ref.identifier.value} ({ref.entity_type.value}) registrada sem "
                "aresta confirmada: referência sem marcador de alteração não vira "
                "proposta de mudança (§8.2.7)"
            )
            continue

        polarity = _combined_polarity(_mention_polarities(mentioning, ref.identifier.value))
        refs = tuple(evidence_by_id.get(ref.identifier.value, ()))

        if polarity is MentionPolarity.NEGATED:
            fact_id = _write_declared_negation(
                rev, repo, ns, subject.entity_id or "", rtype, ref, mentioning, svid, source_id, refs, derived
            )
            if fact_id:
                negation_facts.append(fact_id)
                notes.append(
                    f"negação declarada: {subject.entity_id} nao {rtype.value} "
                    f"{ref.entity_id} (fato {fact_id}, §R3) — aresta afirmativa NÃO criada"
                )
            disputed_edge = _dispute_relation_on_negation(
                rev, repo, ns, subject.entity_id or "", rtype, ref, svid, refs, derived
            )
            if disputed_edge:
                out.append(disputed_edge)
            continue

        # Aqui a autoridade é o SUJEITO: uma aresta que sai de um refinamento
        # proposto é proposta (§5.5: relação extraída de proposta conserva a
        # natureza de proposta).
        lifecycle = _entity_lifecycle_of(
            repo, subject.entity_id, any(c.lifecycle is LifecycleStatus.PROPOSED for c in mentioning)
        )
        if rtype is RelationType.PROPOSES_CHANGE_TO:
            lifecycle = LifecycleStatus.PROPOSED  # §8.3: proposta de alteração é proposta

        attributes: dict[str, Any] = {"basis": "referência explícita", "external_id": ref.identifier.value}
        if polarity is MentionPolarity.NEUTRAL:
            # Comparação/citação (§ R3): candidata explícita, nunca supported,
            # mesmo que a fonte tenha evidência — a evidência sustenta a
            # CITAÇÃO, não o vínculo (§8.2.7).
            epistemic = EpistemicStatus.INFERRED
            attributes["candidate_only"] = True
        else:
            epistemic = EpistemicStatus.SUPPORTED

        edge = _put_relation(
            rev,
            repo,
            ns,
            subject.entity_id or "",
            rtype,
            ref.entity_id or "",
            scope=_SUBJECT_LINK_SCOPE,
            epistemic=epistemic,
            lifecycle=lifecycle,
            svid=svid,
            evidence_refs=refs,
            derived=derived,
            attributes=attributes,
        )
        if edge:
            out.append(edge)
    return out, notes, negation_facts
