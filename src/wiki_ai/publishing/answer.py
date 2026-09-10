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
    ValidatedAnswer,
    ValidatedClaim,
    build_session,
    compose_answer,
    envelope_of,
    validate_envelope,
)
from .query_harness import KnowledgeQueryHarness, QueryLimits

__all__ = [
    "ANSWER_LIMIT",
    "AnswerEnricher",
    "AnswerMode",
    "AnswerOutcome",
    "Answerer",
    "Intent",
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
NO_SUPPORTED_CLAIM = "no_claim_with_valid_evidence"
DISCARDED_PREFIX = "Afirmação descartada por citar evidência inexistente: "


class AnswerMode(str, Enum):
    AGENTIC = "agentic"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


@dataclass(frozen=True)
class AnswerOutcome:
    question: str
    answer: str
    evidence_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    mode: AnswerMode = AnswerMode.DETERMINISTIC_FALLBACK
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "evidence_ids": list(self.evidence_ids),
            "entity_ids": list(self.entity_ids),
            "unresolved": list(self.unresolved),
            "mode": self.mode.value,
            "reason": self.reason,
        }


@runtime_checkable
class SessionProvider(Protocol):
    def run(self, session: AgentSession) -> AgentRun: ...


class Answerer:
    def __init__(
        self,
        query_factory: Callable[[KnowledgeRepository], KnowledgeQuery] = KnowledgeQuery,
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
            return self._fallback(question, knowledge, NO_SUPPORTED_CLAIM)
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
        unresolved = list(validated.unresolved)
        for claim in validated.discarded_claims:
            note = DISCARDED_PREFIX + claim.statement
            if claim.statement and note not in unresolved:
                unresolved.append(note)
        return AnswerOutcome(
            question=str(question),
            answer=compose_answer(validated, harness),
            evidence_ids=validated.evidence_ids,
            entity_ids=validated.entity_ids,
            unresolved=tuple(unresolved),
            mode=AnswerMode.AGENTIC,
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
