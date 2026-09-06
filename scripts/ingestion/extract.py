"""Candidatos a conhecimento a partir de um `SourceDocument` (plano §8.1).

A regra que este módulo existe para tornar executável é uma só:

    "Não converter transcrição inteira em fato. Extrair afirmações, decisões,
    dúvidas, hipóteses, requisitos e ações, preservando seus estados." (§8.1)

Como isso vira código:

1. A classificação é POR BLOCO e por MARCADOR LINGUÍSTICO EXPLÍCITO
   (`MARKER_RULES`). Um bloco sem marcador não vira decisão, requisito nem
   ação: vira `assertion` de natureza `observed`. Não há classificador
   estatístico, não há score, não há "confiança" — §5.3 proíbe usar confiança
   numérica de LLM como prova, e aqui nem existe número para ser confundido
   com prova.
2. Nenhum candidato nasce com `epistemic=supported` nem com
   `nature=implemented`. Isso não é convenção: é invariante verificado em
   `Candidate.__post_init__`, que levanta `CandidateInvariant`. §5.4 diz que
   prosa/transcrição não sustenta comportamento implementado, e F10 diz que
   metadata declarada (`source_type="code-repo"`) não comprova análise
   determinística — logo nenhum caminho deste módulo pode produzir esses
   estados, nem por engano de quem o chamar depois.
3. Estados são PRESERVADOS: um bloco que propõe (fase de refinamento, marcador
   de proposta, hipótese, ação futura) sai com `lifecycle=proposed` e continua
   proposta em toda reingestão, porque a classificação é função do texto e da
   fase — nunca do número de vezes que a fonte foi ingerida.

O módulo não escreve nada: devolve `ExtractionCandidates` e quem grava é
`correlate.py`, dentro de uma revisão atômica.

Só stdlib + `knowledge`. Nada de `wk`/`codescan`/`sbindex`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

try:  # `PYTHONPATH=scripts` (convenção do repositório)
    from knowledge.models import EntityType, EpistemicStatus, FactNature, LifecycleStatus
except ImportError:  # pragma: no cover - execução como `scripts.ingestion.extract`
    from ..knowledge.models import (  # type: ignore[no-redef]
        EntityType,
        EpistemicStatus,
        FactNature,
        LifecycleStatus,
    )


# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------


class ExtractionError(Exception):
    """Base dos erros de extração."""


class CandidateInvariant(ExtractionError):
    """Candidato tentando nascer `supported`/`implemented` (§5.3, §5.4, F10)."""


class DocumentContractError(ExtractionError):
    """`SourceDocument` sem os campos do contrato de `normalize.py` (§8.1)."""


# --------------------------------------------------------------------------
# Normalização textual
# --------------------------------------------------------------------------

#: Palavras vazias do português usadas só para a recuperação lexical de
#: `correlate.py` (§8.2 passo 5). Recuperação lexical AMPLIA candidatos; nunca
#: confirma vínculo — por isso a lista pode ser imperfeita sem risco.
STOPWORDS: frozenset[str] = frozenset(
    """
    a as o os um uma uns umas de do da dos das em no na nos nas por para com sem sob
    sobre entre ate e ou mas que se ao aos quando como onde qual quais quanto isso
    isto aquilo ele ela eles elas nos vos eu tu voce voces nao sim ja mais menos muito
    pouco todo toda todos todas ser estar ter haver foi era sao eh sera seria tem tinha
    esta estao esse essa esses essas seu sua seus suas lhe nem la ai aqui entao pois
    """.split()
)

_COMBINING = re.compile("[\u0300-\u036f]")
_WORD = re.compile(r"[0-9a-z]+(?:-[0-9a-z]+)*")

#: Sentinela de campo ausente — `None` é valor legítimo no contrato.
_MISSING = object()


def accent_fold(text: str) -> str:
    """Minúsculas sem acento — a forma em que TODO marcador é casado.

    Existe para que "decisão:", "DECISAO:" e "Decisão :" caiam na mesma regra.
    Sem isso, a lista de marcadores viraria uma combinatória de variantes
    ortográficas e um acento perdido na transcrição silenciaria a regra.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    return _COMBINING.sub("", decomposed).lower()


def normalize_tokens(text: str, keep_stopwords: bool = False) -> tuple[str, ...]:
    """Tokens normalizados e deduplicados, na ordem de aparição.

    Usado apenas pela recuperação lexical de §8.2 passo 5, que é explicitamente
    "somente para ampliar candidatos".
    """
    folded = accent_fold(text)
    out: list[str] = []
    seen: set[str] = set()
    for match in _WORD.finditer(folded):
        token = match.group(0)
        if len(token) < 2:
            continue
        if not keep_stopwords and token in STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return tuple(out)


# --------------------------------------------------------------------------
# Identificadores explícitos
# --------------------------------------------------------------------------

#: Prefixo de id -> tipo de entidade. §8.3 fixa INI/DEC/RF/RN/CAP/US; os demais
#: são variantes correntes do mesmo vocabulário. Prefixo desconhecido NÃO é
#: erro: o id é reconhecido e fica sem tipo, para que `correlate.py` o reporte
#: como referência não resolvida em vez de inventar uma entidade.
ID_PREFIX_TYPES: Mapping[str, EntityType] = {
    "INI": EntityType.INITIATIVE,
    "DEC": EntityType.DECISION,
    "ADR": EntityType.DECISION,
    "RF": EntityType.REFINEMENT,
    "REF": EntityType.REFINEMENT,
    "RN": EntityType.BUSINESS_RULE,
    "BR": EntityType.BUSINESS_RULE,
    "CAP": EntityType.CAPABILITY,
    "US": EntityType.STORY,
    "HU": EntityType.STORY,
    "STY": EntityType.STORY,
    "RQ": EntityType.REQUIREMENT,
    "REQ": EntityType.REQUIREMENT,
    "RNF": EntityType.REQUIREMENT,
    "DEF": EntityType.DEFECT,
    "BUG": EntityType.DEFECT,
    "SYS": EntityType.SYSTEM,
    "CMP": EntityType.COMPONENT,
    "CTR": EntityType.CONTRACT,
    "ENT": EntityType.DATA_ENTITY,
    "FLW": EntityType.FLOW,
}

#: Ids explícitos: prefixo alfabético + separador opcional + número.
#: Duas alternativas de propósito:
#: - prefixo CONHECIDO aceita variantes (``ini 008``, ``INI008``, ``ini-008``);
#: - prefixo desconhecido exige MAIÚSCULA e hífen, senão qualquer par
#:   "palavra-número" do texto corrente viraria um id inventado.
_KNOWN_PREFIXES = "|".join(sorted(ID_PREFIX_TYPES, key=len, reverse=True))
EXPLICIT_ID_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?:(?P<known>{_KNOWN_PREFIXES})[\s._-]?(?P<knum>\d{{1,6}})"
    rf"|(?P<other>[A-Z]{{2,6}})-(?P<onum>\d{{1,6}}))(?![A-Za-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExplicitId:
    """Um id citado no texto, já normalizado para a forma canônica ``PRE-NNN``.

    `entity_type` é `None` quando o prefixo não está em `ID_PREFIX_TYPES`:
    reconhecer o id sem saber o que ele é vale mais do que descartá-lo, porque
    permite pedir a decisão material em vez de perder a referência.
    """

    raw: str
    value: str
    prefix: str
    number: str
    entity_type: EntityType | None

    @property
    def known(self) -> bool:
        return self.entity_type is not None


def parse_explicit_ids(text: str) -> tuple[ExplicitId, ...]:
    """Extrai ids explícitos preservando a ordem e sem repetir o mesmo id."""
    out: list[ExplicitId] = []
    seen: set[str] = set()
    for m in EXPLICIT_ID_RE.finditer(text or ""):
        if m.group("known"):
            prefix_raw, number = m.group("known"), m.group("knum")
        else:
            prefix_raw, number = m.group("other"), m.group("onum")
        prefix = prefix_raw.upper()
        if m.group("other") and prefix != prefix_raw:
            # Prefixo desconhecido em minúsculas: não é id, é palavra comum.
            continue
        canonical = f"{prefix}-{number.zfill(3) if len(number) < 3 else number}"
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(
            ExplicitId(
                raw=m.group(0),
                value=canonical,
                prefix=prefix,
                number=number,
                entity_type=ID_PREFIX_TYPES.get(prefix),
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------
# Marcadores linguísticos
# --------------------------------------------------------------------------


class CandidateKind(str, Enum):
    """As seis classes de §8.1, mais nada."""

    ASSERTION = "assertion"
    DECISION = "decision"
    QUESTION = "question"
    HYPOTHESIS = "hypothesis"
    REQUIREMENT = "requirement"
    ACTION = "action"


@dataclass(frozen=True)
class MarkerRule:
    kind: CandidateKind
    label: str
    pattern: re.Pattern[str]


def _rule(kind: CandidateKind, label: str, *alternatives: str) -> MarkerRule:
    return MarkerRule(kind, label, re.compile("|".join(alternatives)))


#: ORDEM É PRECEDÊNCIA e é deliberada:
#:
#: - pergunta primeiro: "o serviço deve validar CPF?" é dúvida em aberto, não
#:   requisito declarado; tratar como requisito criaria obrigação que ninguém
#:   afirmou;
#: - hipótese antes de decisão/requisito: "talvez a gente aprove o token" é
#:   especulação, e promovê-la a decisão é exatamente o erro que §5.3 chama de
#:   colapsar sustentação com vigência;
#: - decisão antes de ação/requisito: "decidimos que o time vai fazer X" é uma
#:   decisão registrada, com a ação como consequência;
#: - ação antes de requisito: "João fica responsável até dia 12" é compromisso
#:   de pessoa, não regra do produto.
MARKER_RULES: tuple[MarkerRule, ...] = (
    _rule(
        CandidateKind.QUESTION,
        "interrogativa/duvida",
        r"\?",
        r"\bnao sabemos\b",
        r"\bnao sei\b",
        r"\bnao esta claro\b",
        r"\bnao ficou claro\b",
        r"\bfica a duvida\b",
        r"\bficou a duvida\b",
        r"\bduvida\s*:",
        r"\bpergunta\s*:",
        r"\bem aberto\b",
        r"\bprecisamos descobrir\b",
        r"\bquestao em aberto\b",
    ),
    _rule(
        CandidateKind.HYPOTHESIS,
        "hipotese",
        r"\btalvez\b",
        r"\bpode ser que\b",
        r"\bpoderia ser\b",
        r"\bsuspeito\b",
        r"\bsuspeitamos\b",
        r"\bacho que\b",
        r"\bachamos que\b",
        r"\bimagino que\b",
        r"\bprovavelmente\b",
        r"\bhipotese\s*:",
        r"\bem tese\b",
    ),
    _rule(
        CandidateKind.DECISION,
        "decisao",
        r"\bdecidimos\b",
        r"\bdecidiu-se\b",
        r"\bficou decidido\b",
        r"\bfoi decidido\b",
        r"\bdecisao\s*:",
        r"\baprovado\b",
        r"\baprovada\b",
        r"\baprovamos\b",
        r"\bvamos seguir com\b",
        r"\boptamos por\b",
        r"\bfechamos (?:com|em)\b",
        r"\bfica definido\b",
    ),
    _rule(
        CandidateKind.ACTION,
        "acao",
        r"\bvai fazer\b",
        r"\bvao fazer\b",
        r"\bfica responsavel\b",
        r"\bficou responsavel\b",
        r"\bficaram responsaveis\b",
        r"\bficou de\b",
        r"\bate (?:o )?dia\b",
        r"\bacao\s*:",
        r"\bresponsavel\s*:",
        r"\bassume a tarefa\b",
    ),
    _rule(
        CandidateKind.REQUIREMENT,
        "requisito",
        r"\bdeve\b",
        r"\bdevem\b",
        r"\bdevera\b",
        r"\bdeverao\b",
        r"\bprecisa\b",
        r"\bprecisam\b",
        r"\be obrigatorio\b",
        r"\bobrigatorio\b",
        r"\bobrigatoria\b",
        r"\btem que\b",
        r"\bnao pode\b",
        r"\brequisito\s*:",
        r"\bregra\s*:",
    ),
)

#: Marcadores de PROPOSTA. Não classificam o candidato; decidem a VIGÊNCIA.
#: §8.3: "proposta de alteração de RN-023 vira proposes_change_to com lifecycle
#: proposed" — e §5.3: proposta aprovada continua proposta.
PROPOSAL_MARKERS = re.compile(
    r"\bproposta\b|\bpropoe\b|\bpropomos\b|\bproponho\b|\bsugiro\b|\bsugerimos\b"
    r"|\bsugestao\b|\bpassaria a\b|\bpretendemos\b|\bqueremos que\b"
)

#: Marcadores de ALTERAÇÃO de algo existente. É o que autoriza `correlate.py` a
#: gravar `proposes_change_to` em vez de apenas registrar uma menção.
CHANGE_MARKERS = re.compile(
    r"\balterar\b|\balteracao\b|\bmudar\b|\bmudanca\b|\bpassa a\b|\bpassara a\b"
    r"|\bsubstituir\b|\brevisar\b|\bdeixa de\b|\bajustar\b|\bmodificar\b"
    r"|\bnova versao d[eoa]\b"
)

#: Fases em que TUDO nasce proposto (§8.3: refinamento produz proposta).
PROPOSING_PHASES: frozenset[str] = frozenset({"refinement", "refinamento", "proposal", "proposta"})

#: `source_type` de metadata que indica autoria de agente. §8.4: "uma resposta
#: de agente pode propor história/refinamento; não pode confirmar a própria
#: correção pela reingestão dessa resposta".
AGENT_SOURCE_TYPES: frozenset[str] = frozenset(
    {"agent-response", "agent", "llm", "assistant", "claude", "copilot"}
)

#: `source_type` que F10 proíbe de conceder tratamento privilegiado.
DECLARED_CODE_SOURCE_TYPES: frozenset[str] = frozenset({"code-repo", "code", "repo", "repository"})

#: Status de `normalize.py` que significam "nada ficou de fora". Qualquer outro
#: (partial/unsupported/extraction_failed) vira diagnóstico: §8.1 proíbe
#: devolver ingestão integral bem-sucedida quando algo ficou indisponível.
COMPLETE_STATUSES: frozenset[str] = frozenset({"ingested", "ok", "extracted", "success"})

#: Chaves de metadata previstas pelo contrato de `normalize.py`.
METADATA_WHITELIST: tuple[str, ...] = (
    "initiative_id",
    "phase",
    "participants",
    "date",
    "title",
    "source_type",
)


# --------------------------------------------------------------------------
# Estados candidatos por classe
# --------------------------------------------------------------------------

#: (natureza, sustentação, vigência-base) por classe.
#:
#: `FactNature.IMPLEMENTED` e `EpistemicStatus.SUPPORTED` NÃO aparecem nesta
#: tabela e não podem aparecer: documento não sustenta comportamento
#: implementado (§5.4) e `supported` é atribuído pelo pipeline de verificação,
#: nunca pela extração (§5.3).
CANDIDATE_STATES: Mapping[CandidateKind, tuple[FactNature, EpistemicStatus, LifecycleStatus]] = {
    CandidateKind.ASSERTION: (
        FactNature.OBSERVED,
        EpistemicStatus.INFERRED,
        LifecycleStatus.CURRENT,
    ),
    CandidateKind.DECISION: (
        FactNature.DECLARED_REQUIREMENT,
        EpistemicStatus.INFERRED,
        LifecycleStatus.CURRENT,
    ),
    CandidateKind.QUESTION: (
        FactNature.OBSERVED,
        EpistemicStatus.UNRESOLVED,
        LifecycleStatus.CURRENT,
    ),
    CandidateKind.HYPOTHESIS: (
        FactNature.OBSERVED,
        EpistemicStatus.INFERRED,
        LifecycleStatus.PROPOSED,
    ),
    CandidateKind.REQUIREMENT: (
        FactNature.DECLARED_REQUIREMENT,
        EpistemicStatus.INFERRED,
        LifecycleStatus.CURRENT,
    ),
    CandidateKind.ACTION: (
        FactNature.DECLARED_REQUIREMENT,
        EpistemicStatus.INFERRED,
        LifecycleStatus.PROPOSED,
    ),
}


# --------------------------------------------------------------------------
# Estruturas de saída
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockRef:
    """Localizador do bloco que originou o candidato (§5.4, linha Transcrição).

    `locator` é copiado do `Block` sem reinterpretação: é ele que vira
    `knowledge.Evidence` em `correlate.py`, e uma citação sem localizador não
    volta a ser verificável.
    """

    block_id: str
    block_kind: str
    locator: Mapping[str, Any] = field(default_factory=dict)
    #: `SourceKind`/`ContentKind` que o bloco declara (contrato de
    #: `normalize.py`), como string. São DICAS: quem decide o que a evidência
    #: pode sustentar é `correlate`, aplicando F10 — um bloco não escolhe
    #: virar código.
    source_kind_hint: str = ""
    content_kind_hint: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        return self.locator.get(key, default) if self.locator else default


@dataclass(frozen=True)
class EntityMention:
    """Menção a uma entidade por id explícito, com o bloco onde apareceu."""

    identifier: ExplicitId
    block_id: str
    occurrences: int = 1


@dataclass(frozen=True)
class Candidate:
    """Um candidato a conhecimento — nunca um fato.

    O `__post_init__` é a fronteira executável de §5.4/F10: qualquer tentativa
    de nascer `implemented` ou `supported` estoura aqui, antes de existir
    qualquer linha no banco. Documentação não segura essa regra; este método
    segura.
    """

    text: str
    kind: CandidateKind
    block_refs: tuple[BlockRef, ...]
    nature: FactNature
    epistemic: EpistemicStatus
    lifecycle: LifecycleStatus
    marker: str = ""
    speaker: str | None = None
    timestamp: str | None = None
    initiative_hint: str | None = None
    phase_hint: str | None = None
    explicit_ids: tuple[ExplicitId, ...] = ()
    tokens: tuple[str, ...] = ()
    authored_by_agent: bool = False

    def __post_init__(self) -> None:
        if self.nature is FactNature.IMPLEMENTED:
            raise CandidateInvariant(
                "candidato extraído de documento com nature=implemented: "
                "comentário, prosa, markdown e bloco de transcrição não sustentam "
                "comportamento implementado (§5.4); origem implementada é atestada "
                "só pelo pipeline de análise de código (F10)"
            )
        if self.epistemic is EpistemicStatus.SUPPORTED:
            raise CandidateInvariant(
                "candidato extraído de documento com epistemic=supported: "
                "'supported' é atribuído pelo pipeline de verificação com registro do "
                "suporte, nunca pela extração nem pela própria saída de LLM (§5.3)"
            )
        if not self.block_refs:
            raise CandidateInvariant(
                "candidato sem block_refs: afirmação sem localizador não é recuperável (§5.4)"
            )

    @property
    def primary_block(self) -> BlockRef:
        return self.block_refs[0]

    @property
    def known_ids(self) -> tuple[ExplicitId, ...]:
        return tuple(i for i in self.explicit_ids if i.known)

    @property
    def has_explicit_id(self) -> bool:
        return bool(self.explicit_ids)

    @property
    def proposes_change(self) -> bool:
        """O bloco pede ALTERAÇÃO de algo existente (§8.3, `proposes_change_to`)."""
        return bool(CHANGE_MARKERS.search(accent_fold(self.text)))


@dataclass(frozen=True)
class ExtractionCandidates:
    """Saída de `extract_candidates`, particionada pelas classes de §8.1."""

    source_id: str
    assertions: tuple[Candidate, ...] = ()
    decisions: tuple[Candidate, ...] = ()
    questions: tuple[Candidate, ...] = ()
    hypotheses: tuple[Candidate, ...] = ()
    requirements: tuple[Candidate, ...] = ()
    actions: tuple[Candidate, ...] = ()
    entities_mentioned: tuple[EntityMention, ...] = ()
    initiative_hint: str | None = None
    phase_hint: str | None = None
    authored_by_agent: bool = False
    declared_code_source: bool = False
    diagnostics: tuple[str, ...] = ()

    _BUCKETS = (
        ("assertions", CandidateKind.ASSERTION),
        ("decisions", CandidateKind.DECISION),
        ("questions", CandidateKind.QUESTION),
        ("hypotheses", CandidateKind.HYPOTHESIS),
        ("requirements", CandidateKind.REQUIREMENT),
        ("actions", CandidateKind.ACTION),
    )

    def all(self) -> tuple[Candidate, ...]:
        """Todos os candidatos, na ordem das classes de §8.1."""
        out: list[Candidate] = []
        for attr, _kind in self._BUCKETS:
            out.extend(getattr(self, attr))
        return tuple(out)

    def by_kind(self, kind: CandidateKind) -> tuple[Candidate, ...]:
        for attr, k in self._BUCKETS:
            if k is kind:
                return tuple(getattr(self, attr))
        return ()

    @property
    def total(self) -> int:
        return len(self.all())

    def explicit_ids(self) -> tuple[ExplicitId, ...]:
        """Ids explícitos citados no documento, deduplicados por valor."""
        seen: dict[str, ExplicitId] = {}
        for mention in self.entities_mentioned:
            seen.setdefault(mention.identifier.value, mention.identifier)
        return tuple(seen.values())


# --------------------------------------------------------------------------
# Classificação
# --------------------------------------------------------------------------


def classify_block(text: str) -> tuple[CandidateKind, str]:
    """Classe do bloco e o marcador que a justifica.

    Devolve `(ASSERTION, "")` quando NENHUM marcador casa. O marcador vazio é
    informação de primeira classe: é a diferença entre "o documento declarou
    uma decisão" e "o pipeline decidiu que aquilo parecia uma decisão".
    """
    folded = accent_fold(text)
    for rule in MARKER_RULES:
        match = rule.pattern.search(folded)
        if match:
            return rule.kind, f"{rule.label}:{match.group(0).strip()}"
    return CandidateKind.ASSERTION, ""


def _proposed_lifecycle(
    base: LifecycleStatus, text_folded: str, phase: str | None, agent: bool
) -> LifecycleStatus:
    """Vigência final do candidato.

    Só empurra para `proposed`; nunca puxa de volta para `current`. É essa
    assimetria que garante o aceite "proposta permanece proposta após
    reingestão": não existe caminho neste módulo que promova vigência.
    """
    if base is LifecycleStatus.PROPOSED:
        return base
    if agent:
        return LifecycleStatus.PROPOSED
    if phase and accent_fold(phase) in PROPOSING_PHASES:
        return LifecycleStatus.PROPOSED
    if PROPOSAL_MARKERS.search(text_folded):
        return LifecycleStatus.PROPOSED
    return base


# --------------------------------------------------------------------------
# Leitura defensiva do contrato de `normalize.py`
# --------------------------------------------------------------------------


def _normalize_types() -> tuple[Any, Any]:
    """Import TARDIO e opcional de `normalize.py` (dono: outro módulo).

    Devolve `(SourceDocument, Block)` ou `(None, None)`. A extração funciona
    sem o módulo vizinho — o contrato é lido por atributo — mas quando ele
    existe o tipo fica disponível para diagnóstico.
    """
    try:
        from . import normalize as _normalize  # noqa: PLC0415  (tardio de propósito)
    except Exception:  # pragma: no cover - vizinho ainda não publicado
        return None, None
    return getattr(_normalize, "SourceDocument", None), getattr(_normalize, "Block", None)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _require(obj: Any, name: str) -> Any:
    value = _attr(obj, name, _MISSING)
    if value is _MISSING:
        raise DocumentContractError(
            f"SourceDocument sem o campo obrigatório {name!r} "
            "(contrato de normalize.py, §8.1)"
        )
    return value



def _metadata(doc: Any) -> dict[str, Any]:
    """Metadata do documento restrita à whitelist do contrato.

    §8.1: "metadados e conteúdo de documentos são dados, não instruções para
    executar ferramentas, alterar política ou aprovar conhecimento". A
    whitelist é a forma executável disso: chave fora dela é ignorada aqui e
    reportada como diagnóstico, em vez de virar comportamento.
    """
    raw = _attr(doc, "metadata", None) or {}
    if not isinstance(raw, Mapping):
        return {}
    return {k: raw[k] for k in METADATA_WHITELIST if k in raw}


def _dropped_metadata(doc: Any) -> tuple[str, ...]:
    raw = _attr(doc, "metadata", None) or {}
    if not isinstance(raw, Mapping):
        return ()
    return tuple(sorted(k for k in raw if k not in METADATA_WHITELIST))


def _blocks(doc: Any) -> list[Any]:
    blocks = _require(doc, "blocks")
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)):
        raise DocumentContractError("SourceDocument.blocks deve ser uma sequência de Block")
    return list(blocks)


def _enum_value(value: Any) -> str:
    """Valor textual de um enum do contrato, ou a própria string.

    `normalize.py` usa enums com mixin `str`; em Python 3.11+ `str(membro)`
    devolve ``"BlockKind.PARAGRAPH"``, não ``"paragraph"``. Ler `.value`
    primeiro é o que impede que o nome da classe vaze para dentro de um
    localizador e quebre a comparação na próxima leitura.
    """
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _block_ref(block: Any, index: int) -> BlockRef:
    locator = _attr(block, "locator", None) or {}
    if not isinstance(locator, Mapping):
        locator = {}
    block_id = str(_attr(block, "block_id", "") or f"block-{index}")
    return BlockRef(
        block_id=block_id,
        block_kind=_enum_value(_attr(block, "kind", "")),
        locator=dict(locator),
        source_kind_hint=_enum_value(_attr(block, "source_kind", "")),
        content_kind_hint=_enum_value(_attr(block, "content_kind", "")),
    )


def _diagnostic_text(diag: Any) -> str:
    """Texto legível de um `Diagnostic` do vizinho, sem depender do seu tipo."""
    for attr in ("message", "detail", "reason"):
        value = _attr(diag, attr, None)
        if value:
            unavailable = _attr(diag, "unavailable", None)
            if unavailable:
                return f"{value} (indisponível: {', '.join(str(u) for u in unavailable)})"
            return str(value)
    return str(diag)


def _speaker_of(ref: BlockRef, block: Any) -> str | None:
    for key in ("speaker", "interlocutor", "author"):
        value = ref.get(key) or _attr(block, key)
        if value:
            return str(value)
    return None


def _timestamp_of(ref: BlockRef, block: Any) -> str | None:
    for key in ("time_start", "timestamp", "time", "start"):
        value = ref.get(key) or _attr(block, key)
        if value:
            return str(value)
    return None


def _initiative_from(metadata: Mapping[str, Any], ids: Iterable[ExplicitId]) -> str | None:
    """Iniciativa declarada em metadata ou citada por id explícito.

    Devolve `None` quando há ZERO ou MAIS DE UMA candidata: ambiguidade é
    reportada, nunca resolvida por escolha arbitrária (§8.3: duas inceptions
    com títulos semelhantes permanecem separadas até resolução de identidade).
    """
    declared = metadata.get("initiative_id")
    if declared:
        found = parse_explicit_ids(str(declared))
        return found[0].value if found else str(declared).strip()
    initiatives = {i.value for i in ids if i.entity_type is EntityType.INITIATIVE}
    if len(initiatives) == 1:
        return next(iter(initiatives))
    return None


# --------------------------------------------------------------------------
# Extração
# --------------------------------------------------------------------------


def extract_candidates(doc: Any) -> ExtractionCandidates:
    """Candidatos a conhecimento de um `SourceDocument` (§8.1).

    Um bloco produz EXATAMENTE um candidato: a granularidade da citação é a do
    localizador que `normalize.py` preservou, e agregar blocos aqui destruiria
    a correspondência entre afirmação e trecho verificável.
    """
    source_id = str(_require(doc, "source_id"))
    metadata = _metadata(doc)
    diagnostics: list[str] = []

    dropped = _dropped_metadata(doc)
    if dropped:
        diagnostics.append(
            "metadata fora da whitelist ignorada (dados, não instruções, §8.1): "
            + ", ".join(dropped)
        )

    document_type, _block_type = _normalize_types()
    if document_type is not None and not isinstance(doc, document_type):
        diagnostics.append(
            "documento não é normalize.SourceDocument: contrato lido por atributo; "
            "divergência de contrato aparece como campo ausente, não como sucesso silencioso"
        )

    doc_status = _enum_value(_attr(doc, "status", "")).lower()
    if doc_status and doc_status not in COMPLETE_STATUSES:
        diagnostics.append(
            f"status da fonte: {doc_status} — extração não é integral; "
            "candidatos cobrem só o que foi extraído (§8.1)"
        )
    for diag in _attr(doc, "diagnostics", ()) or ():
        diagnostics.append(f"normalize: {_diagnostic_text(diag)}")

    source_type = accent_fold(str(metadata.get("source_type", "") or "")).strip()
    authored_by_agent = source_type in AGENT_SOURCE_TYPES
    declared_code = source_type in DECLARED_CODE_SOURCE_TYPES
    if declared_code:
        diagnostics.append(
            f"metadata source_type={source_type!r} é DECLARAÇÃO: não concede natureza "
            "'implemented' nem tratamento privilegiado; origem de extração é atestada "
            "pelo pipeline de análise de código (F10)"
        )
    if authored_by_agent:
        diagnostics.append(
            f"metadata source_type={source_type!r}: saída de agente pode PROPOR, "
            "nunca confirmar a si própria — todos os candidatos nascem propostos (§8.4)"
        )

    phase = metadata.get("phase")
    phase_hint = str(phase) if phase else None

    buckets: dict[CandidateKind, list[Candidate]] = {k: [] for k in CandidateKind}
    mentions: list[EntityMention] = []
    all_ids: list[ExplicitId] = []

    for index, block in enumerate(_blocks(doc)):
        text = str(_attr(block, "text", "") or "")
        if not text.strip():
            continue
        ref = _block_ref(block, index)
        ids = parse_explicit_ids(text)
        all_ids.extend(ids)
        for identifier in ids:
            mentions.append(EntityMention(identifier=identifier, block_id=ref.block_id))

        kind, marker = classify_block(text)
        nature, epistemic, base_life = CANDIDATE_STATES[kind]
        lifecycle = _proposed_lifecycle(base_life, accent_fold(text), phase_hint, authored_by_agent)

        buckets[kind].append(
            Candidate(
                text=text.strip(),
                kind=kind,
                block_refs=(ref,),
                nature=nature,
                epistemic=epistemic,
                lifecycle=lifecycle,
                marker=marker,
                speaker=_speaker_of(ref, block),
                timestamp=_timestamp_of(ref, block),
                initiative_hint=None,  # preenchido abaixo, com a visão do documento
                phase_hint=phase_hint,
                explicit_ids=ids,
                tokens=normalize_tokens(text),
                authored_by_agent=authored_by_agent,
            )
        )

    initiative_hint = _initiative_from(metadata, all_ids)
    if initiative_hint is None:
        diagnostics.append(
            "iniciativa não inequívoca no documento: candidatos ficam sem vínculo de "
            "iniciativa até decisão explícita (§7.1)"
        )

    def _stamped(items: list[Candidate]) -> tuple[Candidate, ...]:
        return tuple(
            c if c.initiative_hint == initiative_hint else _with_initiative(c, initiative_hint)
            for c in items
        )

    return ExtractionCandidates(
        source_id=source_id,
        assertions=_stamped(buckets[CandidateKind.ASSERTION]),
        decisions=_stamped(buckets[CandidateKind.DECISION]),
        questions=_stamped(buckets[CandidateKind.QUESTION]),
        hypotheses=_stamped(buckets[CandidateKind.HYPOTHESIS]),
        requirements=_stamped(buckets[CandidateKind.REQUIREMENT]),
        actions=_stamped(buckets[CandidateKind.ACTION]),
        entities_mentioned=tuple(_dedupe_mentions(mentions)),
        initiative_hint=initiative_hint,
        phase_hint=phase_hint,
        authored_by_agent=authored_by_agent,
        declared_code_source=declared_code,
        diagnostics=tuple(diagnostics),
    )


def _with_initiative(candidate: Candidate, initiative: str | None) -> Candidate:
    """Cópia do candidato com a dica de iniciativa do documento.

    Reconstrução explícita (e não `dataclasses.replace`) para que o invariante
    de `__post_init__` seja reexecutado em toda cópia.
    """
    return Candidate(
        text=candidate.text,
        kind=candidate.kind,
        block_refs=candidate.block_refs,
        nature=candidate.nature,
        epistemic=candidate.epistemic,
        lifecycle=candidate.lifecycle,
        marker=candidate.marker,
        speaker=candidate.speaker,
        timestamp=candidate.timestamp,
        initiative_hint=initiative,
        phase_hint=candidate.phase_hint,
        explicit_ids=candidate.explicit_ids,
        tokens=candidate.tokens,
        authored_by_agent=candidate.authored_by_agent,
    )


def _dedupe_mentions(mentions: Sequence[EntityMention]) -> list[EntityMention]:
    """Agrupa menções por (id, bloco), somando ocorrências."""
    acc: dict[tuple[str, str], EntityMention] = {}
    for m in mentions:
        key = (m.identifier.value, m.block_id)
        prev = acc.get(key)
        if prev is None:
            acc[key] = m
        else:
            acc[key] = EntityMention(prev.identifier, prev.block_id, prev.occurrences + 1)
    return list(acc.values())


def assert_no_implemented(candidates: Iterable[Candidate]) -> None:
    """Reverificação em lote do invariante de §5.4/F10.

    `Candidate.__post_init__` já impede a construção; esta função existe para
    que um chamador que tenha montado candidatos por outro caminho (ou que
    tenha desserializado de fora) possa fechar a mesma porta antes de gravar.
    """
    for c in candidates:
        if c.nature is FactNature.IMPLEMENTED or c.epistemic is EpistemicStatus.SUPPORTED:
            raise CandidateInvariant(
                f"candidato {c.text[:60]!r} com nature={c.nature.value}/"
                f"epistemic={c.epistemic.value}: documento não sustenta comportamento "
                "implementado (§5.4) e sustentação não é auto-declarada (§5.3)"
            )
