from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from wiki_ai.agent.protocol import ToolCall, ToolResult
from wiki_ai.agent.session import AgentRun, AgentSession, Budget, RunStatus, ToolSpec
from wiki_ai.knowledge.errors import InvalidRelationPair, KnowledgeError
from wiki_ai.knowledge.evidence import locator_from_dict, make_evidence
from wiki_ai.knowledge.gaps import open_gap
from wiki_ai.knowledge.model import (
    Confidence,
    Entity,
    EntityId,
    KnowledgeState,
    Relation,
    SourceVersion,
)
from wiki_ai.knowledge.repository import KnowledgeRepository, RevisionTransaction
from wiki_ai.knowledge.taxonomy import REQUIRED_ATTRIBUTES, EntityKind, RelationKind, validate_pair

from wiki_ai.ingestion import briefing
from wiki_ai.ingestion.finding import (
    DocumentEvidenceRef,
    DocumentFinding,
    DocumentFindingError,
    DocumentRelationClaim,
    Rejection,
    VerifiedDocumentFinding,
    document_finding_schema,
    state_for,
    parse_finding,
    parse_findings,
    verify,
)
from wiki_ai.ingestion.harness import (
    DocumentEvidenceCapture,
    DocumentHarness,
    DocumentToolError,
)
from wiki_ai.ingestion.hints import normalize_text, stable_key
from wiki_ai.ingestion.pipeline import IngestedSource

__all__ = [
    "AUTHOR",
    "DEFAULT_ROUND_BUDGET",
    "DEFAULT_TOTAL_TOOL_CALLS",
    "DEFAULT_MAX_SECONDS",
    "DocumentFindingError",
    "DocumentFinding",
    "DocumentEvidenceRef",
    "DocumentRelationClaim",
    "Rejection",
    "VerifiedDocumentFinding",
    "SemanticOutcome",
    "SessionProvider",
    "document_finding_schema",
    "parse_finding",
    "parse_findings",
    "verify",
    "state_for",
    "SemanticInvestigator",
]

AUTHOR = "ingestion"
DEFAULT_ROUND_BUDGET = 30
DEFAULT_TOTAL_TOOL_CALLS = 150
DEFAULT_MAX_SECONDS = 600.0


@runtime_checkable
class SessionProvider(Protocol):
    def run(self, session: AgentSession) -> AgentRun: ...


@dataclass(frozen=True)
class SemanticOutcome:
    entities_written: int = 0
    relations_written: int = 0
    evidence_written: int = 0
    gaps_opened: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    rounds: int = 0
    tool_calls: int = 0
    verified: tuple[VerifiedDocumentFinding, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "entities_written": self.entities_written,
            "relations_written": self.relations_written,
            "evidence_written": self.evidence_written,
            "gaps_opened": list(self.gaps_opened),
            "diagnostics": list(self.diagnostics),
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
        }


class _HarnessBridge:
    def __init__(self, harness: DocumentHarness) -> None:
        self._harness = harness
        self._covered: list[str] = []

    @property
    def covered(self) -> tuple[str, ...]:
        return tuple(self._covered)

    def __call__(self, call: ToolCall) -> ToolResult:
        call_id = uuid.uuid4().hex
        try:
            payload = self._harness.invoke(call.name, call.arguments)
        except DocumentToolError as exc:
            return ToolResult(call_id=call_id, ok=False, payload={}, error=str(exc))
        for key in ("worksheet", "speaker", "page", "section"):
            value = call.arguments.get(key)
            if value is None:
                continue
            marker = f"{'section' if key == 'section' else key}:{value}"
            if marker not in self._covered:
                self._covered.append(marker)
        for identifier in payload.get("block_ids") or ():
            marker = self._marker_for(str(identifier))
            if marker and marker not in self._covered:
                self._covered.append(marker)
        for item in payload.get("blocks") or ():
            if isinstance(item, Mapping):
                marker = self._marker_for(str(item.get("block_id") or ""))
                if marker and marker not in self._covered:
                    self._covered.append(marker)
        return ToolResult(call_id=call_id, ok=True, payload=payload)

    def _marker_for(self, block_id: str) -> str:
        return self._harness.entry_key_of(block_id)


def _entity_of(
    item: VerifiedDocumentFinding, source_version_key: str
) -> Entity:
    payload: dict[str, Any] = {}
    for key, value in item.finding.attributes.items():
        payload[key] = list(value) if isinstance(value, tuple) else value
    if item.reasons:
        payload["verification_notes"] = list(item.reasons)
    payload["origin"] = "document"
    return Entity.create(
        kind=item.finding.type.value,
        name=item.finding.subject,
        stable_key=item.stable_key,
        attributes=payload,
        state=item.state,
        confidence=item.confidence,
        source_versions=(source_version_key,),
    )


_DEFAULT_TARGET_KIND: Mapping[str, EntityKind] = {
    RelationKind.CALLS.value: EntityKind.MODULE,
    RelationKind.CONSUMES.value: EntityKind.INTEGRATION,
    RelationKind.PUBLISHES.value: EntityKind.EVENT,
    RelationKind.DEPENDS_ON.value: EntityKind.DEPENDENCY,
    RelationKind.VALIDATES.value: EntityKind.DATA_FIELD,
    RelationKind.TRANSITIONS_TO.value: EntityKind.STATE,
    RelationKind.PERSISTS_TO.value: EntityKind.PERSISTENCE,
    RelationKind.BELONGS_TO.value: EntityKind.MODULE,
    RelationKind.DECLARES.value: EntityKind.BUSINESS_RULE,
    RelationKind.AFFECTS.value: EntityKind.CAPABILITY,
    RelationKind.PROPOSES_CHANGE_TO.value: EntityKind.CAPABILITY,
    RelationKind.SUPERSEDES.value: EntityKind.DECISION_RECORD,
    RelationKind.CONTRADICTS.value: EntityKind.BUSINESS_RULE,
}


class SemanticInvestigator:
    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        round_budget: int = DEFAULT_ROUND_BUDGET,
        total_tool_calls: int = DEFAULT_TOTAL_TOOL_CALLS,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> None:
        self._clock = clock or time.monotonic
        self._round_budget = round_budget
        self._total_tool_calls = total_tool_calls
        self._max_seconds = max_seconds

    def run(
        self,
        ingested: IngestedSource,
        knowledge: KnowledgeRepository,
        provider: SessionProvider,
        namespace: str,
    ) -> SemanticOutcome:
        harness = DocumentHarness(ingested.document)
        frontier = list(harness.frontier())
        covered: list[str] = []
        totals = {"entities": 0, "relations": 0, "evidence": 0, "tool_calls": 0}
        diagnostics: list[str] = []
        opened: list[str] = []
        verified: list[VerifiedDocumentFinding] = []
        rounds = 0
        while frontier or rounds == 0:
            remaining = self._total_tool_calls - totals["tool_calls"]
            if remaining <= 0:
                diagnostics.append("semantic_investigation_budget_exhausted")
                break
            rounds += 1
            bridge = _HarnessBridge(harness)
            session = self._session(
                harness,
                ingested,
                bridge,
                tuple(frontier),
                tuple(covered),
                rounds,
                min(self._round_budget, remaining),
            )
            run = provider.run(session)
            totals["tool_calls"] += run.usage.tool_calls
            if run.status is not RunStatus.COMPLETED:
                diagnostics.append(f"semantic_round_{run.status.value}:{run.reason}")
                break
            findings, rejected = parse_findings(run.findings)
            diagnostics.extend(rejected)
            checked, gaps = verify(findings, harness)
            verified.extend(checked)
            written = self._persist(
                [item for item in checked if item.persistable],
                gaps,
                harness,
                ingested,
                knowledge,
                namespace,
            )
            totals["entities"] += written.entities_written
            totals["relations"] += written.relations_written
            totals["evidence"] += written.evidence_written
            opened.extend(written.gaps_opened)
            diagnostics.extend(written.diagnostics)
            for marker in bridge.covered:
                if marker not in covered:
                    covered.append(marker)
            previous = tuple(frontier)
            frontier = [item for item in frontier if item not in covered]
            if frontier and totals["tool_calls"] >= self._total_tool_calls:
                diagnostics.append("semantic_investigation_budget_exhausted")
                break
            if tuple(frontier) == previous and not run.findings:
                break
        return SemanticOutcome(
            entities_written=totals["entities"],
            relations_written=totals["relations"],
            evidence_written=totals["evidence"],
            gaps_opened=tuple(dict.fromkeys(opened)),
            diagnostics=tuple(dict.fromkeys(diagnostics)),
            rounds=rounds,
            tool_calls=totals["tool_calls"],
            verified=tuple(verified),
        )

    def _session(
        self,
        harness: DocumentHarness,
        ingested: IngestedSource,
        bridge: _HarnessBridge,
        frontier: Sequence[str],
        covered: Sequence[str],
        round_number: int,
        round_calls: int,
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
        objective = briefing.build(
            kind=harness.kind,
            uri=ingested.uri,
            source_id=harness.source_id,
            version_hash=harness.version_hash,
            tool_names=harness.names(),
            frontier=frontier,
            round_number=round_number,
            covered=covered,
        )
        return AgentSession(
            objective=objective,
            tools=specs,
            budget=Budget(
                max_tool_calls=max(1, round_calls), max_seconds=self._max_seconds
            ),
            snapshot_id=f"{harness.source_id}@{harness.version_hash}",
            executor=bridge,
            started_at=self._clock(),
            finding_schema=document_finding_schema(harness.kind),
        )

    def _persist(
        self,
        items: Sequence[VerifiedDocumentFinding],
        gaps: Sequence[str],
        harness: DocumentHarness,
        ingested: IngestedSource,
        knowledge: KnowledgeRepository,
        namespace: str,
    ) -> SemanticOutcome:
        if not items and not gaps:
            return SemanticOutcome()
        version = SourceVersion(
            source_id=harness.source_id,
            version_hash=harness.version_hash,
            locator_root=ingested.uri,
            captured_at=ingested.source.captured_at.isoformat(),
        )
        by_key = {item.stable_key: item for item in items}
        subject_index: dict[str, str] = {}
        for key, item in by_key.items():
            subject_index.setdefault(normalize_text(item.finding.subject), key)
        entity_ids: dict[str, EntityId] = {}
        evidence_written: set[str] = set()
        relations: set[str] = set()
        diagnostics: list[str] = []
        opened: list[str] = []
        with knowledge.begin_revision(
            author=AUTHOR, summary=f"{namespace}:{harness.source_id}"
        ) as transaction:
            transaction.put_source_version(version)
            for key in sorted(by_key):
                entity = _entity_of(by_key[key], version.key)
                transaction.put_entity(entity)
                entity_ids[key] = entity.id
            self._link_evidence(
                transaction, by_key, entity_ids, version, evidence_written
            )
            relations.update(
                self._write_relations(
                    transaction,
                    by_key,
                    entity_ids,
                    subject_index,
                    version,
                    diagnostics,
                )
            )
            for question in dict.fromkeys(item for item in gaps if item):
                open_gap(transaction, question, blocking=False)
                opened.append(question)
        return SemanticOutcome(
            entities_written=len(entity_ids),
            relations_written=len(relations),
            evidence_written=len(evidence_written),
            gaps_opened=tuple(opened),
            diagnostics=tuple(diagnostics),
        )

    def _link_evidence(
        self,
        transaction: RevisionTransaction,
        by_key: Mapping[str, VerifiedDocumentFinding],
        entity_ids: Mapping[str, EntityId],
        version: SourceVersion,
        written: set[str],
    ) -> None:
        holders: dict[str, list[EntityId]] = {}
        payloads: dict[str, DocumentEvidenceCapture] = {}
        for key in sorted(by_key):
            for capture in by_key[key].captures:
                payloads[capture.capture_id] = capture
                slot = holders.setdefault(capture.capture_id, [])
                if entity_ids[key] not in slot:
                    slot.append(entity_ids[key])
        for capture_id in sorted(payloads):
            capture = payloads[capture_id]
            evidence = make_evidence(
                source_id=version.source_id,
                version_hash=version.version_hash,
                locator=locator_from_dict(capture.locator),
                excerpt=capture.excerpt,
                captured_at=version.captured_at,
            )
            transaction.put_evidence(evidence, entity_ids=tuple(holders[capture_id]))
            written.add(evidence.id)

    def _write_relations(
        self,
        transaction: RevisionTransaction,
        by_key: Mapping[str, VerifiedDocumentFinding],
        entity_ids: dict[str, EntityId],
        subject_index: Mapping[str, str],
        version: SourceVersion,
        diagnostics: list[str],
    ) -> set[str]:
        written: set[str] = set()
        for key in sorted(by_key):
            item = by_key[key]
            for claim in item.finding.relations:
                target = normalize_text(claim.target_subject)
                target_key = subject_index.get(target)
                if target_key is None:
                    kind = claim.target_type or _DEFAULT_TARGET_KIND.get(
                        claim.kind.value, EntityKind.CAPABILITY
                    )
                    target_key = stable_key(kind.value, claim.target_subject)
                    if target_key not in entity_ids:
                        placeholder = Entity.create(
                            kind=kind.value,
                            name=claim.target_subject,
                            stable_key=target_key,
                            attributes={
                                name: claim.target_subject
                                for name in REQUIRED_ATTRIBUTES.get(kind, ())
                            },
                            state=KnowledgeState.DECLARED,
                            confidence=Confidence.UNRESOLVED,
                            source_versions=(version.key,),
                        )
                        transaction.put_entity(placeholder)
                        entity_ids[target_key] = placeholder.id
                try:
                    validate_pair(
                        claim.kind,
                        item.finding.type.value,
                        target_key.split("::", 1)[0],
                    )
                except InvalidRelationPair as exc:
                    diagnostics.append(str(exc))
                    continue
                relation = Relation.create(
                    kind=claim.kind.value,
                    source_id=entity_ids[key],
                    target_id=entity_ids[target_key],
                    attributes=dict(claim.attributes),
                    confidence=_relation_confidence(item, by_key.get(target_key)),
                )
                try:
                    transaction.put_relation(relation)
                except KnowledgeError as exc:
                    diagnostics.append(str(exc))
                    continue
                written.add(relation.id)
        return written


def _relation_confidence(
    source: VerifiedDocumentFinding, target: VerifiedDocumentFinding | None
) -> Confidence:
    if target is None:
        return Confidence.INFERRED
    if (
        source.confidence is Confidence.SUPPORTED
        and target.confidence is Confidence.SUPPORTED
    ):
        return Confidence.SUPPORTED
    return Confidence.INFERRED
