from __future__ import annotations

import enum
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from wiki_ai.knowledge.gaps import GAP_QUESTION, is_blocking
from wiki_ai.knowledge.model import Entity, EntityId
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind

from .narrative import epistemic_phrase

__all__ = [
    "ANSWER_LIMIT",
    "Intent",
    "AnswerOutcome",
    "AnswerEnricher",
    "Answerer",
    "classify",
    "normalize",
]

ANSWER_LIMIT = 200

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a",
        "as",
        "o",
        "os",
        "de",
        "do",
        "da",
        "dos",
        "das",
        "e",
        "em",
        "no",
        "na",
        "nos",
        "nas",
        "um",
        "uma",
        "para",
        "por",
        "com",
        "que",
        "qual",
        "quais",
        "como",
        "se",
        "este",
        "esta",
        "esse",
        "essa",
        "isto",
        "sao",
        "eh",
        "ainda",
        "sobre",
        "funciona",
        "sistema",
        "analise",
        "analisar",
        "profundamente",
        "gere",
        "gerar",
        "material",
        "atualizado",
        "publicacao",
    }
)


class Intent(str, enum.Enum):
    DEEP_ANALYSIS = "deep_analysis"
    CAPABILITY_PROFILE = "capability_profile"
    BUSINESS_RULES = "business_rules"
    CONTRACT = "contract"
    CRITICAL_INTEGRATIONS = "critical_integrations"
    IMPACT = "impact"
    COMPARISON = "comparison"
    MISSING_EVIDENCE = "missing_evidence"
    PUBLICATION = "publication"


@dataclass(frozen=True)
class AnswerOutcome:
    question: str
    answer: str
    evidence_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "evidence_ids": list(self.evidence_ids),
            "entity_ids": list(self.entity_ids),
            "unresolved": list(self.unresolved),
        }


@runtime_checkable
class AnswerEnricher(Protocol):
    def enrich(self, intent: Intent, question: str, answer: str) -> str: ...


def normalize(question: str) -> str:
    folded = unicodedata.normalize("NFD", str(question).lower())
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")


def _terms(question: str) -> tuple[str, ...]:
    found = [
        token
        for token in _TOKEN.findall(normalize(question))
        if token not in _STOPWORDS and len(token) > 2
    ]
    return tuple(dict.fromkeys(found))


_INTENT_MARKERS: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    (Intent.PUBLICATION, ("sharepoint", "publicacao", "publicar")),
    (Intent.COMPARISON, ("compare", "comparar", "inception", "decisao", "decidido")),
    (Intent.IMPACT, ("quebrar", "quebra", "impacto", "mudar", "afetado")),
    (
        Intent.MISSING_EVIDENCE,
        ("evidencia", "migracao", "faltam", "pendente", "lacuna"),
    ),
    (Intent.CRITICAL_INTEGRATIONS, ("integracoes", "integracao", "critica", "criticas")),
    (
        Intent.CONTRACT,
        ("inputs", "outputs", "invariantes", "edge", "entradas", "saidas"),
    ),
    (Intent.BUSINESS_RULES, ("regra", "regras", "negocio")),
    (Intent.DEEP_ANALYSIS, ("profundamente", "profunda", "visao", "geral")),
)


def classify(question: str) -> Intent:
    text = normalize(question)
    for intent, markers in _INTENT_MARKERS:
        if any(marker in text for marker in markers):
            return intent
    return Intent.CAPABILITY_PROFILE


def _score(entity: Entity, terms: Sequence[str]) -> int:
    name = normalize(entity.name)
    return sum(1 for term in terms if term in name)


def _best_subject(
    query: KnowledgeQuery, terms: Sequence[str], kinds: Sequence[EntityKind]
) -> Entity | None:
    candidates: list[Entity] = []
    for kind in kinds:
        candidates.extend(query.entities(kind, limit=ANSWER_LIMIT))
    for term in terms:
        candidates.extend(query.search(term, limit=ANSWER_LIMIT))
    allowed = {kind.value for kind in kinds}
    rank = {kind.value: index for index, kind in enumerate(kinds)}
    scored = [
        (_score(entity, terms), rank[entity.kind], entity.name, entity)
        for entity in _unique(candidates)
        if entity.kind in allowed
    ]
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1], item[2], item[3].id.value))
    return scored[0][3]


def _line(entity: Entity, prefix: str = "") -> str:
    body = str(entity.attributes.get("statement") or entity.name).strip()
    head = f"{prefix} {body}" if prefix else body
    return f"- {head} ({epistemic_phrase(entity)})."


def _gap_lines(gaps: Sequence[Entity]) -> tuple[str, ...]:
    return tuple(
        str(gap.attributes.get(GAP_QUESTION, gap.name)).strip()
        for gap in sorted(gaps, key=lambda item: (item.name, item.id.value))
    )


def _unique(entities: Sequence[Entity]) -> tuple[Entity, ...]:
    seen: set[str] = set()
    found: list[Entity] = []
    for entity in entities:
        if entity.id.value in seen:
            continue
        seen.add(entity.id.value)
        found.append(entity)
    return tuple(sorted(found, key=lambda item: (item.name, item.id.value)))


class Answerer:
    def __init__(
        self,
        query_factory: Callable[[KnowledgeRepository], KnowledgeQuery] = KnowledgeQuery,
        enricher: AnswerEnricher | None = None,
    ) -> None:
        self._query_factory = query_factory
        self._enricher = enricher

    def run(
        self,
        question: str,
        knowledge: KnowledgeRepository,
        provider: Any | None,
        namespace: str,
    ) -> AnswerOutcome:
        query = self._query_factory(knowledge)
        intent = classify(question)
        terms = _terms(question)
        handlers = {
            Intent.DEEP_ANALYSIS: self._deep,
            Intent.PUBLICATION: self._publication,
            Intent.COMPARISON: self._comparison,
            Intent.IMPACT: self._impact,
            Intent.MISSING_EVIDENCE: self._missing_evidence,
            Intent.CRITICAL_INTEGRATIONS: self._integrations,
            Intent.CONTRACT: self._contract,
            Intent.BUSINESS_RULES: self._rules,
            Intent.CAPABILITY_PROFILE: self._capability,
        }
        lines, entities, unresolved = handlers[intent](query, terms)
        answer = "\n".join(lines).strip() or (
            "Não há conhecimento com evidência registrada para responder a esta pergunta."
        )
        if self._enricher is not None:
            candidate = self._enricher.enrich(intent, question, answer)
            if isinstance(candidate, str) and candidate.strip():
                answer = candidate
        return AnswerOutcome(
            question=str(question),
            answer=answer,
            evidence_ids=self._evidence(query, entities),
            entity_ids=tuple(entity.id.value for entity in entities),
            unresolved=tuple(unresolved),
        )

    def _evidence(
        self, query: KnowledgeQuery, entities: Sequence[Entity]
    ) -> tuple[str, ...]:
        found: set[str] = set()
        for entity in entities:
            for evidence in query.evidence_of(entity.id, limit=ANSWER_LIMIT):
                found.add(evidence.id)
        return tuple(sorted(found))

    def _capability_subject(
        self, query: KnowledgeQuery, terms: Sequence[str]
    ) -> Entity | None:
        return _best_subject(query, terms, (EntityKind.CAPABILITY,))

    def _deep(
        self, query: KnowledgeQuery, terms: Sequence[str]
    ) -> tuple[list[str], tuple[Entity, ...], tuple[str, ...]]:
        systems = _unique(query.entities(EntityKind.SYSTEM, limit=ANSWER_LIMIT))
        capabilities = _unique(query.entities(EntityKind.CAPABILITY, limit=ANSWER_LIMIT))
        lines = ["O conjunto analisado está organizado assim:"]
        for system in systems:
            lines.append(_line(system, "O sistema"))
        for capability in capabilities:
            lines.append(_line(capability, "A capacidade"))
        gaps = _gap_lines(query.gaps(limit=ANSWER_LIMIT))
        if gaps:
            lines.append(
                "Permanecem sem resposta as perguntas listadas como não resolvidas."
            )
        return lines, systems + capabilities, gaps

    def _publication(
        self, query: KnowledgeQuery, terms: Sequence[str]
    ) -> tuple[list[str], tuple[Entity, ...], tuple[str, ...]]:
        systems = _unique(query.entities(EntityKind.SYSTEM, limit=ANSWER_LIMIT))
        capabilities = _unique(query.entities(EntityKind.CAPABILITY, limit=ANSWER_LIMIT))
        lines = [
            "O material de publicação cobre um documento de visão geral por sistema "
            "e um documento por capacidade, além dos catálogos de integrações e de "
            "regras de negócio e do relatório de lacunas.",
            f"Serão gerados documentos para {len(systems)} sistema(s) e "
            f"{len(capabilities)} capacidade(s).",
        ]
        blocking = _gap_lines(query.gaps(blocking_only=True, limit=ANSWER_LIMIT))
        if blocking:
            lines.append(
                "As lacunas bloqueantes abaixo aparecem na seção de lacunas conhecidas "
                "de cada documento."
            )
        return lines, systems + capabilities, blocking

    def _comparison(
        self, query: KnowledgeQuery, terms: Sequence[str]
    ) -> tuple[list[str], tuple[Entity, ...], tuple[str, ...]]:
        report = query.compare(limit=ANSWER_LIMIT)
        lines: list[str] = []
        entities: list[Entity] = []
        unresolved: list[str] = []
        groups = (
            ("Declarado sem implementação encontrada", report.declared_not_implemented),
            ("Implementado sem documento que descreva", report.implemented_not_documented),
            ("Proposta em conflito com o comportamento atual", report.proposal_conflicts),
            ("Decisão que substitui proposta anterior", report.decision_supersedes),
            ("Fonte que contradiz outra fonte", report.source_contradicts_source),
        )
        for label, findings in groups:
            if not findings:
                continue
            lines.append(f"{label}:")
            for finding in findings:
                lines.append(f"- {finding.entity_name}: {finding.detail}.")
                node = query.repository.get_entity(EntityId(finding.entity_id))
                if node is not None:
                    entities.append(node)
                if findings is report.declared_not_implemented or findings is (
                    report.source_contradicts_source
                ):
                    unresolved.append(f"{finding.entity_name}: {finding.detail}")
        return lines, _unique(entities), tuple(unresolved)

    def _impact(
        self, query: KnowledgeQuery, terms: Sequence[str]
    ) -> tuple[list[str], tuple[Entity, ...], tuple[str, ...]]:
        subject = _best_subject(
            query,
            terms,
            (EntityKind.BUSINESS_RULE, EntityKind.CAPABILITY, EntityKind.INTEGRATION),
        )
        if subject is None:
            return (
                ["Não há regra ou capacidade correspondente com evidência registrada."],
                (),
                (),
            )
        report = query.impact(subject.id, limit=ANSWER_LIMIT)
        lines = [f"Mudar {subject.name} altera o comportamento observado a seguir."]
        impacted = _unique(report.impacted)
        for entity in impacted:
            lines.append(_line(entity))
        tests = tuple(
            node
            for _relation, node in query.neighbors(
                subject.id, RelationKind.TESTS, "in", limit=ANSWER_LIMIT
            )
        )
        for test in _unique(tests):
            lines.append(_line(test, "É verificado pelo cenário"))
        if not impacted and not tests:
            lines.append(
                "Nenhum outro elemento com evidência registrada depende deste ponto."
            )
        gaps = _gap_lines(
            tuple(
                gap
                for _relation, gap in query.neighbors(
                    subject.id, RelationKind.AFFECTS, "in", limit=ANSWER_LIMIT
                )
                if gap.kind == EntityKind.GAP.value
            )
        )
        return lines, (subject,) + impacted + _unique(tests), gaps

    def _missing_evidence(
        self, query: KnowledgeQuery, terms: Sequence[str]
    ) -> tuple[list[str], tuple[Entity, ...], tuple[str, ...]]:
        gaps = _unique(query.gaps(limit=ANSWER_LIMIT))
        lines = ["Ainda não possuem evidência os pontos abaixo."]
        for gap in gaps:
            severity = "bloqueante" if is_blocking(gap) else "não bloqueante"
            lines.append(
                f"- {str(gap.attributes.get(GAP_QUESTION, gap.name)).strip()} ({severity})."
            )
        if not gaps:
            lines = ["O conhecimento registrado possui evidência associada em cada ponto."]
        report = query.compare(limit=ANSWER_LIMIT)
        for finding in report.declared_not_implemented:
            lines.append(f"- {finding.entity_name}: {finding.detail}.")
        unresolved = _gap_lines(gaps) + tuple(
            f"{finding.entity_name}: {finding.detail}"
            for finding in report.declared_not_implemented
        )
        return lines, gaps, unresolved

    def _integrations(
        self, query: KnowledgeQuery, terms: Sequence[str]
    ) -> tuple[list[str], tuple[Entity, ...], tuple[str, ...]]:
        integrations = _unique(query.entities(EntityKind.INTEGRATION, limit=ANSWER_LIMIT))
        lines: list[str] = []
        entities: list[Entity] = []
        unresolved: list[str] = []
        for integration in integrations:
            failures = _unique(
                tuple(
                    node
                    for _relation, node in query.neighbors(
                        integration.id, RelationKind.RETRIES, "in", limit=ANSWER_LIMIT
                    )
                )
            )
            entities.append(integration)
            lines.append(_line(integration, "A integração"))
            for failure in failures:
                lines.append(_line(failure, "  Conta com"))
                entities.append(failure)
        modes = _unique(query.entities(EntityKind.FAILURE_MODE, limit=ANSWER_LIMIT))
        for mode in modes:
            trigger = str(mode.attributes.get("trigger", "")).strip()
            effect = str(mode.attributes.get("effect", "")).strip()
            lines.append(
                f"- Se {trigger}, o efeito é {effect} ({epistemic_phrase(mode)})."
                if trigger and effect
                else _line(mode)
            )
            entities.append(mode)
        if not lines:
            lines.append("Nenhuma integração com evidência registrada foi encontrada.")
            unresolved.append("integrações sem evidência")
        return lines, _unique(entities), tuple(unresolved)

    def _rules(
        self, query: KnowledgeQuery, terms: Sequence[str]
    ) -> tuple[list[str], tuple[Entity, ...], tuple[str, ...]]:
        subject = self._capability_subject(query, terms)
        if subject is not None:
            profile = query.capability_profile(subject.id)
            rules = _unique(profile.rules) if profile is not None else ()
            gaps = _gap_lines(profile.gaps) if profile is not None else ()
        else:
            rules = _unique(query.entities(EntityKind.BUSINESS_RULE, limit=ANSWER_LIMIT))
            gaps = ()
        lines = ["As regras de negócio que participam deste fluxo são:"]
        for rule in rules:
            lines.append(_line(rule))
        if not rules:
            lines = ["Nenhuma regra de negócio com evidência registrada foi encontrada."]
        return lines, rules, gaps

    def _contract(
        self, query: KnowledgeQuery, terms: Sequence[str]
    ) -> tuple[list[str], tuple[Entity, ...], tuple[str, ...]]:
        subject = self._capability_subject(query, terms)
        if subject is None:
            return (
                ["Não há capacidade correspondente com evidência registrada."],
                (),
                (),
            )
        profile = query.capability_profile(subject.id)
        if profile is None:
            return (["Não há perfil registrado para esta capacidade."], (subject,), ())
        lines = [f"Contrato observado de {subject.name}:"]
        groups = (
            ("Entradas", profile.inputs),
            ("Saídas", profile.outputs),
            ("Invariantes", profile.invariants + profile.preconditions),
            ("Edge cases", profile.edge_cases),
        )
        entities: list[Entity] = [subject]
        for label, group in groups:
            lines.append(f"{label}:")
            unique = _unique(group)
            for entity in unique:
                lines.append(_line(entity))
                entities.append(entity)
            if not unique:
                lines.append("- Nenhum item com evidência registrada para este tópico.")
        return lines, _unique(entities), _gap_lines(profile.gaps)

    def _capability(
        self, query: KnowledgeQuery, terms: Sequence[str]
    ) -> tuple[list[str], tuple[Entity, ...], tuple[str, ...]]:
        subject = self._capability_subject(query, terms)
        if subject is None:
            return (
                ["Não há capacidade correspondente com evidência registrada."],
                (),
                (),
            )
        profile = query.capability_profile(subject.id)
        if profile is None:
            return (["Não há perfil registrado para esta capacidade."], (subject,), ())
        lines = [f"{subject.name} funciona assim ({epistemic_phrase(subject)}):"]
        entities: list[Entity] = [subject]
        for label, group in (
            ("O acionamento acontece por", profile.entrypoints),
            ("Aplica a regra", profile.rules),
            ("Conversa com", profile.integrations),
            ("Grava em", profile.persistence),
        ):
            for entity in _unique(group):
                lines.append(_line(entity, label))
                entities.append(entity)
        return lines, _unique(entities), _gap_lines(profile.gaps)
