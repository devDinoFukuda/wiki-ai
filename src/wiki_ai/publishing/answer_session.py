from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from wiki_ai.agent.protocol import ToolCall, ToolResult
from wiki_ai.agent.session import (
    AgentRun,
    AgentSession,
    Budget,
    RunStatus,
    ToolSpec,
)

from wiki_ai.knowledge.grounding import GroundingCheck, check_component

from .answer_fallback import provenance
from .query_harness import (
    GroundingVocabulary,
    INCLUDE_INFERRED,
    KnowledgeQueryHarness,
    QueryToolError,
    TOOL_ENTITY,
    TOOL_EVIDENCE,
    TOOL_GAPS,
    TOOL_NEIGHBORS,
    TOOL_SEARCH,
)

__all__ = [
    "ANSWER_ROUND_CALLS",
    "ANSWER_MAX_SECONDS",
    "ClaimVerdict",
    "INFERRED_CONFIDENCE",
    "INFERRED_LABEL",
    "RejectionReason",
    "ValidatedClaim",
    "ValidatedAnswer",
    "AnswerHarnessBridge",
    "answer_schema",
    "build_briefing",
    "build_session",
    "claim_grounding",
    "validate_envelope",
    "provenance_appendix",
    "compose_answer",
    "envelope_of",
]

ANSWER_ROUND_CALLS = 24
ANSWER_MAX_SECONDS = 300.0
MAX_CLAIMS = 40
MAX_UNRESOLVED = 40
INFERRED_CONFIDENCE = "inferred"
INFERRED_LABEL = "inferido, sem evidência que sustente a relação"

METHOD_STEPS: tuple[str, ...] = (
    f"locate the entities the question is about with {TOOL_SEARCH}, then read each "
    f"one with {TOOL_ENTITY}",
    "follow the typed relations out of those entities until the question is decided "
    "by what the knowledge holds, not by what you expect a system like this to do",
    f"read the evidence that sustains every entity you rely on with {TOOL_EVIDENCE}, "
    "read the excerpt text it returns and keep the evidence identifiers",
    "answer only with what the evidence sustains; an entity without evidence is not "
    "an answer, it is a lead",
    f"list everything the knowledge leaves open as unresolved, including the gaps "
    f"{TOOL_GAPS} declares and anything the evidence does not decide",
)

ANSWER_RULES: tuple[str, ...] = (
    "every claim carries the entity identifiers it stands on and the evidence "
    "identifiers that sustain those entities",
    "an identifier you did not read from a tool result does not exist; never invent one",
    "when the knowledge contradicts itself, say both sides and which evidence backs each",
    "when the knowledge cannot decide the question, say so in unresolved instead of "
    "answering from general reasoning",
    "the answer text is read by a person: no identifiers, no store internals, no "
    "tool names",
    "an evidence identifier only counts when it is attached to one of the entities "
    "or relations the same claim cites; evidence borrowed from another entity is "
    "refused",
    "the wording of a claim is checked against the excerpt text of the evidence it "
    "cites, so read the excerpt before writing the claim and stay inside what it "
    "says",
    "the wording of a claim must be sustained by what its evidence and its entities "
    "actually say; wording the knowledge does not carry is refused",
    "the free answer text is a draft kept only for audit: the delivered answer is "
    "assembled from the claims that survive validation, so everything that must be "
    "read has to live inside a claim",
    f"the relation tools walk only the relations the evidence sustains; passing "
    f"{INCLUDE_INFERRED} true to {TOOL_NEIGHBORS} and its siblings also walks the "
    "relations the knowledge merely infers, and each one comes back with its own "
    "relation_confidence",
    f"a claim that stands on a relation whose relation_confidence is not "
    f"\"supported\" is only accepted when the claim itself declares "
    f"confidence \"{INFERRED_CONFIDENCE}\"; without that mark the claim is "
    "refused, because the reader would take an inference for a fact",
)

CLAIM_COMPONENT = "claim"


class ClaimVerdict(Enum):
    VALID = "valid"
    REJECTED = "rejected"


class RejectionReason(Enum):
    NONE = ""
    MALFORMED = "malformed_claim"
    NO_EVIDENCE = "no_evidence"
    UNKNOWN_ENTITY = "unknown_entity"
    UNKNOWN_EVIDENCE = "unknown_evidence"
    EVIDENCE_NOT_LINKED = "evidence_not_linked"
    EVIDENCE_WITHOUT_EXCERPT = "evidence_without_excerpt"
    NOT_GROUNDED = "claim_not_grounded"
    UNSUPPORTED_RELATION_CITED = "unsupported_relation_cited"


@dataclass(frozen=True)
class ValidatedClaim:
    statement: str
    entity_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    confidence: str
    verdict: ClaimVerdict
    reason: RejectionReason = RejectionReason.NONE
    rejected_entity_ids: tuple[str, ...] = ()
    rejected_evidence_ids: tuple[str, ...] = ()
    missing_terms: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return self.verdict is ClaimVerdict.VALID

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "entity_ids": list(self.entity_ids),
            "evidence_ids": list(self.evidence_ids),
            "confidence": self.confidence,
            "verdict": self.verdict.value,
            "reason": self.reason.value,
            "rejected_entity_ids": list(self.rejected_entity_ids),
            "rejected_evidence_ids": list(self.rejected_evidence_ids),
            "missing_terms": list(self.missing_terms),
        }


@dataclass(frozen=True)
class ValidatedAnswer:
    answer: str
    claims: tuple[ValidatedClaim, ...]
    unresolved: tuple[str, ...]

    @property
    def supported_claims(self) -> tuple[ValidatedClaim, ...]:
        return tuple(claim for claim in self.claims if claim.supported)

    @property
    def discarded_claims(self) -> tuple[ValidatedClaim, ...]:
        return tuple(claim for claim in self.claims if not claim.supported)

    @property
    def entity_ids(self) -> tuple[str, ...]:
        found: list[str] = []
        for claim in self.supported_claims:
            for identifier in claim.entity_ids:
                if identifier not in found:
                    found.append(identifier)
        return tuple(found)

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        found: list[str] = []
        for claim in self.supported_claims:
            for identifier in claim.evidence_ids:
                if identifier not in found:
                    found.append(identifier)
        return tuple(found)


def answer_schema() -> Mapping[str, Any]:
    return {
        "type": "object",
        "required": ["answer", "claims"],
        "additionalProperties": False,
        "properties": {
            "answer": {"type": "string"},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["statement", "entity_ids", "evidence_ids"],
                    "additionalProperties": False,
                    "properties": {
                        "statement": {"type": "string"},
                        "entity_ids": {"type": "array", "items": {"type": "string"}},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "string"},
                    },
                },
            },
            "unresolved": {"type": "array", "items": {"type": "string"}},
        },
    }


def _numbered(prefix: str, items: Sequence[str]) -> str:
    return "\n".join(f"{prefix}{index}. {item}" for index, item in enumerate(items, 1))


def build_briefing(
    question: str, tool_names: Sequence[str], namespace: str
) -> str:
    tools = ", ".join(sorted(tool_names))
    return "\n".join(
        (
            "You answer a question about a system using only the curated knowledge "
            f"of namespace {namespace}. The knowledge is read-only and it is the "
            "single ground for the answer.",
            "",
            f"Question: {question}",
            "",
            f"Tools available: {tools}.",
            "",
            "Method:",
            _numbered("", METHOD_STEPS),
            "",
            "Rules:",
            _numbered("", ANSWER_RULES),
            "",
            "Return one finding shaped by the declared schema: the draft answer "
            "text, the claims it is made of with their entity and evidence "
            "identifiers, and what stayed unresolved. Only validated claims reach "
            "the reader.",
        )
    )


class AnswerHarnessBridge:
    def __init__(self, harness: KnowledgeQueryHarness) -> None:
        self._harness = harness

    @property
    def harness(self) -> KnowledgeQueryHarness:
        return self._harness

    def __call__(self, call: ToolCall) -> ToolResult:
        call_id = uuid.uuid4().hex
        try:
            payload = self._harness.invoke(call.name, call.arguments)
        except QueryToolError as exc:
            return ToolResult(call_id=call_id, ok=False, payload={}, error=str(exc))
        return ToolResult(call_id=call_id, ok=True, payload=payload)


def build_session(
    question: str,
    harness: KnowledgeQueryHarness,
    snapshot_id: str,
    round_calls: int = ANSWER_ROUND_CALLS,
    max_seconds: float = ANSWER_MAX_SECONDS,
) -> AgentSession:
    specs = {
        spec.name: ToolSpec(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
        )
        for spec in harness.specs()
    }
    return AgentSession(
        objective=build_briefing(question, tuple(specs), harness.namespace),
        tools=specs,
        budget=Budget(
            max_tool_calls=max(1, round_calls),
            max_seconds=max_seconds,
        ),
        snapshot_id=snapshot_id,
        executor=AnswerHarnessBridge(harness),
        finding_schema=answer_schema(),
    )


def _identifiers(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates: Sequence[Any] = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        return ()
    found: list[str] = []
    for item in candidates:
        text = str(item).strip()
        if text and text not in found:
            found.append(text)
    return tuple(found)


def _rejected(
    statement: str,
    entity_ids: tuple[str, ...],
    evidence_ids: tuple[str, ...],
    confidence: str,
    reason: RejectionReason,
    rejected_entity_ids: tuple[str, ...] = (),
    rejected_evidence_ids: tuple[str, ...] = (),
    missing_terms: tuple[str, ...] = (),
) -> ValidatedClaim:
    return ValidatedClaim(
        statement=statement,
        entity_ids=entity_ids,
        evidence_ids=evidence_ids,
        confidence=confidence,
        verdict=ClaimVerdict.REJECTED,
        reason=reason,
        rejected_entity_ids=rejected_entity_ids,
        rejected_evidence_ids=rejected_evidence_ids,
        missing_terms=missing_terms,
    )


def claim_grounding(
    statement: str, vocabulary: GroundingVocabulary
) -> GroundingCheck:
    semantic = check_component(CLAIM_COMPONENT, statement, vocabulary.semantic)
    if not semantic.terms_required:
        return semantic
    naming = tuple(
        term
        for term in semantic.terms_required
        if term not in semantic.terms_found and term in vocabulary.referential
    )
    if not naming:
        return semantic
    required = tuple(
        term for term in semantic.required_terms if term not in naming
    )
    universe = tuple(
        term for term in semantic.terms_required if term not in naming
    )
    found = semantic.terms_found
    if not universe or not found:
        return GroundingCheck(
            component=CLAIM_COMPONENT,
            terms_required=semantic.terms_required,
            terms_found=found,
            ok=False,
            required_terms=semantic.required_terms,
        )
    seen = set(found)
    optional = tuple(term for term in universe if term not in required)
    optional_ok = not optional or sum(
        1 for term in optional if term in seen
    ) * 2 >= len(optional)
    ok = all(term in seen for term in required) and optional_ok
    return GroundingCheck(
        component=CLAIM_COMPONENT,
        terms_required=semantic.terms_required,
        terms_found=found,
        ok=ok,
        required_terms=semantic.required_terms,
    )


def _validate_claim(
    raw: Any, harness: KnowledgeQueryHarness
) -> ValidatedClaim:
    if not isinstance(raw, Mapping):
        return _rejected(str(raw).strip(), (), (), "", RejectionReason.MALFORMED)
    statement = str(raw.get("statement", "")).strip()
    entity_ids = _identifiers(raw.get("entity_ids"))
    evidence_ids = _identifiers(raw.get("evidence_ids"))
    confidence = str(raw.get("confidence", "")).strip()
    if not statement:
        return _rejected(
            statement, entity_ids, evidence_ids, confidence, RejectionReason.MALFORMED
        )
    unknown_evidence = tuple(
        identifier
        for identifier in evidence_ids
        if not harness.evidence_exists(identifier)
    )
    if unknown_evidence:
        return _rejected(
            statement,
            entity_ids,
            evidence_ids,
            confidence,
            RejectionReason.UNKNOWN_EVIDENCE,
            rejected_evidence_ids=unknown_evidence,
        )
    unknown_entities = tuple(
        identifier for identifier in entity_ids if not harness.entity_exists(identifier)
    )
    if unknown_entities:
        return _rejected(
            statement,
            entity_ids,
            evidence_ids,
            confidence,
            RejectionReason.UNKNOWN_ENTITY,
            rejected_entity_ids=unknown_entities,
        )
    if not evidence_ids or not entity_ids:
        return _rejected(
            statement, entity_ids, evidence_ids, confidence, RejectionReason.NO_EVIDENCE
        )
    unlinked = tuple(
        identifier
        for identifier in evidence_ids
        if not harness.evidence_linked_to(identifier, entity_ids)
    )
    if unlinked:
        return _rejected(
            statement,
            entity_ids,
            evidence_ids,
            confidence,
            RejectionReason.EVIDENCE_NOT_LINKED,
            rejected_evidence_ids=unlinked,
        )
    without_excerpt = harness.evidence_without_excerpt(evidence_ids)
    if without_excerpt:
        return _rejected(
            statement,
            entity_ids,
            evidence_ids,
            confidence,
            RejectionReason.EVIDENCE_WITHOUT_EXCERPT,
            rejected_evidence_ids=without_excerpt,
        )
    unsupported = harness.unsupported_relations(entity_ids, evidence_ids)
    if unsupported and confidence.lower() != INFERRED_CONFIDENCE:
        return _rejected(
            statement,
            entity_ids,
            evidence_ids,
            confidence,
            RejectionReason.UNSUPPORTED_RELATION_CITED,
            rejected_entity_ids=tuple(
                relation.target_id.value for relation in unsupported
            ),
        )
    vocabulary = harness.grounding_vocabulary(entity_ids, evidence_ids)
    grounding = claim_grounding(statement, vocabulary)
    if not grounding.ok:
        return _rejected(
            statement,
            entity_ids,
            evidence_ids,
            confidence,
            RejectionReason.NOT_GROUNDED,
            missing_terms=grounding.required_missing or grounding.terms_missing,
        )
    return ValidatedClaim(
        statement=statement,
        entity_ids=entity_ids,
        evidence_ids=evidence_ids,
        confidence=confidence,
        verdict=ClaimVerdict.VALID,
    )


def validate_envelope(
    envelope: Mapping[str, Any], harness: KnowledgeQueryHarness
) -> ValidatedAnswer:
    raw_claims = envelope.get("claims")
    claims = tuple(
        _validate_claim(item, harness)
        for item in (raw_claims if isinstance(raw_claims, (list, tuple)) else ())
    )[:MAX_CLAIMS]
    unresolved_raw = envelope.get("unresolved")
    unresolved: list[str] = []
    if isinstance(unresolved_raw, (list, tuple)):
        for item in unresolved_raw:
            text = str(item).strip()
            if text and text not in unresolved:
                unresolved.append(text)
    return ValidatedAnswer(
        answer=str(envelope.get("answer", "")).strip(),
        claims=claims,
        unresolved=tuple(unresolved[:MAX_UNRESOLVED]),
    )


def provenance_appendix(
    validated: ValidatedAnswer, harness: KnowledgeQueryHarness
) -> tuple[str, ...]:
    lines: list[str] = []
    for claim in validated.supported_claims:
        places: list[str] = []
        for identifier in claim.evidence_ids:
            evidence = harness.evidence_by_id(identifier)
            if evidence is None:
                continue
            where = provenance(evidence.locator)
            if where not in places:
                places.append(where)
        if not places:
            continue
        line = f"- {claim.statement} (sustentado por {'; '.join(places)})."
        if line not in lines:
            lines.append(line)
    return tuple(lines)


def compose_answer(
    validated: ValidatedAnswer, harness: KnowledgeQueryHarness
) -> str:
    statements: list[str] = []
    for claim in validated.supported_claims:
        sentence = claim.statement.rstrip()
        if claim.confidence.lower() == INFERRED_CONFIDENCE:
            sentence = f"{sentence.rstrip('.')} ({INFERRED_LABEL})"
        if not sentence.endswith((".", "!", "?", ":")):
            sentence = f"{sentence}."
        if sentence not in statements:
            statements.append(sentence)
    if not statements:
        return ""
    parts = [" ".join(statements)]
    appendix = provenance_appendix(validated, harness)
    if appendix:
        parts.append("Proveniência:")
        parts.extend(appendix)
    return "\n".join(parts).strip()


def envelope_of(run: AgentRun) -> Mapping[str, Any] | None:
    if run.status is not RunStatus.COMPLETED:
        return None
    for finding in run.findings:
        if isinstance(finding, Mapping) and "answer" in finding:
            return finding
    return None
