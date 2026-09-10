from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from wiki_ai.agent.session import (
    AgentRun,
    AgentSession,
    RunStatus,
    SessionError,
)
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository

from .answer_fallback import (
    ANSWER_LIMIT,
    AnswerEnricher,
    DeterministicAnswerer,
    FallbackAnswer,
    Intent,
    classify,
    normalize,
    provenance,
)
from .answer_session import (
    ANSWER_MAX_SECONDS,
    ANSWER_ROUND_CALLS,
    RejectionReason,
    ValidatedAnswer,
    ValidatedClaim,
    build_session,
    compose_answer,
    envelope_of,
    validate_envelope,
)
from .query_harness import KnowledgeQueryHarness, QueryFactory, QueryLimits

__all__ = [
    "ANSWER_LIMIT",
    "AnswerEnricher",
    "AnswerMode",
    "AnswerOutcome",
    "AnswerStatus",
    "Answerer",
    "Intent",
    "RejectionReason",
    "ValidatedClaim",
    "classify",
    "normalize",
    "provenance",
]

FALLBACK_NOTE = (
    "Esta resposta foi montada pelo motor determinístico da wiki, sem investigação "
    "do agente."
)
NO_PROVIDER = "provider_unavailable"
EMPTY_ANSWER = "agent_answer_empty"
NO_VALIDATED_CLAIM = "no_validated_claims"
DISCARDED_PREFIX = "Afirmação descartada"


class AnswerMode(str, Enum):
    AGENTIC = "agentic"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class AnswerStatus(str, Enum):
    ANSWERED = "answered"
    PARTIAL = "partial"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class AnswerOutcome:
    question: str
    answer: str
    evidence_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    mode: AnswerMode = AnswerMode.DETERMINISTIC_FALLBACK
    reason: str = ""
    status: AnswerStatus = AnswerStatus.ANSWERED
    draft: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "evidence_ids": list(self.evidence_ids),
            "entity_ids": list(self.entity_ids),
            "unresolved": list(self.unresolved),
            "mode": self.mode.value,
            "reason": self.reason,
            "status": self.status.value,
            "draft": self.draft,
        }


def _discard_notes(validated: ValidatedAnswer) -> tuple[str, ...]:
    notes: list[str] = []
    for claim in validated.discarded_claims:
        if not claim.statement:
            continue
        note = f"{DISCARDED_PREFIX} ({claim.reason.value}): {claim.statement}"
        if claim.missing_terms:
            note = f"{note} [sem lastro para: {', '.join(claim.missing_terms)}]"
        if note not in notes:
            notes.append(note)
    return tuple(notes)


@runtime_checkable
class SessionProvider(Protocol):
    def run(self, session: AgentSession) -> AgentRun: ...


class Answerer:
    def __init__(
        self,
        query_factory: QueryFactory = KnowledgeQuery,
        enricher: AnswerEnricher | None = None,
        limits: QueryLimits | None = None,
        round_calls: int = ANSWER_ROUND_CALLS,
        max_seconds: float = ANSWER_MAX_SECONDS,
    ) -> None:
        self._query_factory = query_factory
        self._deterministic = DeterministicAnswerer(
            query_factory=query_factory, enricher=enricher
        )
        self._limits = limits
        self._round_calls = round_calls
        self._max_seconds = max_seconds

    def run(
        self,
        question: str,
        knowledge: KnowledgeRepository,
        provider: Any | None,
        namespace: str,
    ) -> AnswerOutcome:
        if provider is None:
            return self._fallback(question, knowledge, NO_PROVIDER)
        harness = KnowledgeQueryHarness(
            knowledge,
            namespace,
            limits=self._limits,
            query_factory=self._query_factory,
        )
        session = build_session(
            question,
            harness,
            snapshot_id=namespace,
            round_calls=self._round_calls,
            max_seconds=self._max_seconds,
        )
        run, failure = self._invoke(provider, session)
        if failure:
            return self._fallback(question, knowledge, failure)
        envelope = envelope_of(run)
        if envelope is None:
            return self._fallback(question, knowledge, EMPTY_ANSWER)
        validated = validate_envelope(envelope, harness)
        if not validated.supported_claims:
            return self._blocked(question, knowledge, validated)
        return self._agentic(question, validated, harness)

    def _invoke(
        self, provider: Any, session: AgentSession
    ) -> tuple[AgentRun | None, str]:
        try:
            run = provider.run(session)
        except SessionError as exc:
            return None, f"agent_session_error:{exc}"
        except (OSError, TimeoutError) as exc:
            return None, f"provider_unreachable:{exc}"
        if not isinstance(run, AgentRun):
            return None, f"invalid_run:{type(run).__name__}"
        if run.status is not RunStatus.COMPLETED:
            return None, f"{run.status.value}:{run.reason}"
        return run, ""

    def _agentic(
        self,
        question: str,
        validated: ValidatedAnswer,
        harness: KnowledgeQueryHarness,
    ) -> AnswerOutcome:
        discarded = _discard_notes(validated)
        unresolved = list(validated.unresolved)
        for note in discarded:
            if note not in unresolved:
                unresolved.append(note)
        return AnswerOutcome(
            question=str(question),
            answer=compose_answer(validated, harness),
            evidence_ids=validated.evidence_ids,
            entity_ids=validated.entity_ids,
            unresolved=tuple(unresolved),
            mode=AnswerMode.AGENTIC,
            status=(
                AnswerStatus.PARTIAL if discarded else AnswerStatus.ANSWERED
            ),
            draft=validated.answer,
        )

    def _blocked(
        self,
        question: str,
        knowledge: KnowledgeRepository,
        validated: ValidatedAnswer,
    ) -> AnswerOutcome:
        produced: FallbackAnswer = self._deterministic.run(question, knowledge)
        unresolved = list(validated.unresolved)
        for note in _discard_notes(validated):
            if note not in unresolved:
                unresolved.append(note)
        for note in produced.unresolved:
            if note not in unresolved:
                unresolved.append(note)
        return AnswerOutcome(
            question=str(question),
            answer=f"{produced.answer}\n\n{FALLBACK_NOTE}".strip(),
            evidence_ids=produced.evidence_ids,
            entity_ids=produced.entity_ids,
            unresolved=tuple(unresolved),
            mode=AnswerMode.DETERMINISTIC_FALLBACK,
            reason=NO_VALIDATED_CLAIM,
            status=AnswerStatus.BLOCKED,
            draft=validated.answer,
        )

    def _fallback(
        self, question: str, knowledge: KnowledgeRepository, reason: str
    ) -> AnswerOutcome:
        produced: FallbackAnswer = self._deterministic.run(question, knowledge)
        return AnswerOutcome(
            question=str(question),
            answer=f"{produced.answer}\n\n{FALLBACK_NOTE}".strip(),
            evidence_ids=produced.evidence_ids,
            entity_ids=produced.entity_ids,
            unresolved=produced.unresolved,
            mode=AnswerMode.DETERMINISTIC_FALLBACK,
            reason=reason,
        )
