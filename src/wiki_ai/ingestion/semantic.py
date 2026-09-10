from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from wiki_ai.agent.protocol import ToolCall, ToolResult
from wiki_ai.agent.session import AgentRun, AgentSession, Budget, RunStatus, ToolSpec
from wiki_ai.knowledge.errors import (
    IdentityCollision,
    InvalidRelationPair,
    KnowledgeError,
)
from wiki_ai.knowledge.identity import contextual_key
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
from wiki_ai.knowledge.repository import (
    NAMESPACE_ATTRIBUTE,
    KnowledgeRepository,
    RevisionTransaction,
)
from wiki_ai.knowledge.taxonomy import (
    REQUIRED_ATTRIBUTES,
    EntityKind,
    entity_kind,
    validate_pair,
)

from wiki_ai.ingestion import briefing, relations
from wiki_ai.ingestion.finding import (
    DEFAULT_NAMESPACE,
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
    resolve_captures,
    verify,
)
from wiki_ai.ingestion.harness import (
    DocumentEvidenceCapture,
    DocumentHarness,
    DocumentToolError,
)
from wiki_ai.ingestion.outcome import SemanticFault
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
    "SemanticFault",
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
    faults: tuple[SemanticFault, ...] = ()
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
            "faults": [item.value for item in self.faults],
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
    item: VerifiedDocumentFinding,
    source_version_key: str,
    owner_id: EntityId | None = None,
) -> Entity:
    payload: dict[str, Any] = {}
    for key, value in item.finding.attributes.items():
        payload[key] = list(value) if isinstance(value, tuple) else value
    if item.reasons:
        payload["verification_notes"] = list(item.reasons)
    payload["origin"] = "document"
    payload[NAMESPACE_ATTRIBUTE] = item.finding.namespace
    if item.finding.owner:
        payload["owner"] = item.finding.owner
    return Entity.create(
        kind=item.finding.type.value,
        name=item.finding.subject,
        stable_key=item.stable_key,
        attributes=payload,
        state=item.state,
        confidence=item.confidence,
        source_versions=(source_version_key,),
        owner_id=owner_id,
    )


def _owner_entity(
    owner: str, namespace: str, uri: str, source_version_key: str
) -> Entity:
    return Entity.create(
        kind=EntityKind.SOURCE.value,
        name=owner,
        stable_key=contextual_key(namespace, EntityKind.SOURCE.value, None, owner),
        attributes={
            "origin": "document",
            "locator_root": uri,
            NAMESPACE_ATTRIBUTE: namespace,
        },
        state=KnowledgeState.DECLARED,
        confidence=Confidence.UNRESOLVED,
        source_versions=(source_version_key,),
    )


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
        faults: list[SemanticFault] = []
        verified: list[VerifiedDocumentFinding] = []
        rounds = 0
        while frontier or rounds == 0:
            remaining = self._total_tool_calls - totals["tool_calls"]
            if remaining <= 0:
                diagnostics.append(SemanticFault.BUDGET_EXHAUSTED.value)
                faults.append(SemanticFault.BUDGET_EXHAUSTED)
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
                faults.append(SemanticFault.PROVIDER_RUN_ABORTED)
                break
            findings, rejected = parse_findings(run.findings)
            diagnostics.extend(rejected)
            checked, gaps = verify(findings, harness, namespace)
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
            faults.extend(written.faults)
            for marker in bridge.covered:
                if marker not in covered:
                    covered.append(marker)
            previous = tuple(frontier)
            frontier = [item for item in frontier if item not in covered]
            if frontier and totals["tool_calls"] >= self._total_tool_calls:
                diagnostics.append(SemanticFault.BUDGET_EXHAUSTED.value)
                faults.append(SemanticFault.BUDGET_EXHAUSTED)
                break
            if tuple(frontier) == previous and not run.findings:
                break
        return SemanticOutcome(
            entities_written=totals["entities"],
            relations_written=totals["relations"],
            evidence_written=totals["evidence"],
            gaps_opened=tuple(dict.fromkeys(opened)),
            diagnostics=tuple(dict.fromkeys(diagnostics)),
            faults=tuple(dict.fromkeys(faults)),
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
        entity_ids: dict[str, EntityId] = {}
        evidence_written: set[str] = set()
        written_relations: set[str] = set()
        diagnostics: list[str] = []
        opened: list[str] = []
        faults: list[SemanticFault] = []
        collisions: list[str] = []
        raised: list[str] = list(gaps)
        with knowledge.begin_revision(
            author=AUTHOR, summary=f"{namespace}:{harness.source_id}"
        ) as transaction:
            transaction.put_source_version(version)
            owners = self._owner_ids(
                transaction, by_key, namespace, ingested.uri, version.key, collisions
            )
            written_keys: list[str] = []
            for key in sorted(by_key):
                item = by_key[key]
                entity = _entity_of(item, version.key, owners.get(item.finding.owner))
                try:
                    transaction.put_entity(entity)
                except IdentityCollision as exc:
                    collisions.append(str(exc))
                    continue
                entity_ids[key] = entity.id
                written_keys.append(key)
            persisted = {key: by_key[key] for key in written_keys}
            self._link_evidence(
                transaction, persisted, entity_ids, version, evidence_written
            )
            written_relations.update(
                self._write_relations(
                    transaction,
                    persisted,
                    entity_ids,
                    harness,
                    knowledge,
                    version,
                    diagnostics,
                    raised,
                    namespace,
                )
            )
            for question in dict.fromkeys(
                tuple(item for item in raised if item) + tuple(collisions)
            ):
                open_gap(transaction, question, blocking=bool(collisions))
                opened.append(question)
        if collisions:
            faults.append(SemanticFault.IDENTITY_COLLISION)
            diagnostics.extend(collisions)
        return SemanticOutcome(
            entities_written=len(entity_ids),
            relations_written=len(written_relations),
            evidence_written=len(evidence_written),
            gaps_opened=tuple(opened),
            diagnostics=tuple(diagnostics),
            faults=tuple(faults),
        )

    def _owner_ids(
        self,
        transaction: RevisionTransaction,
        by_key: Mapping[str, VerifiedDocumentFinding],
        namespace: str,
        uri: str,
        source_version_key: str,
        collisions: list[str],
    ) -> dict[str, EntityId]:
        found: dict[str, EntityId] = {}
        names = sorted(
            {item.finding.owner for item in by_key.values() if item.finding.owner}
        )
        for name in names:
            entity = _owner_entity(name, namespace, uri, source_version_key)
            try:
                transaction.put_entity(entity)
            except IdentityCollision as exc:
                collisions.append(str(exc))
                continue
            found[name] = entity.id
        return found

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
        harness: DocumentHarness,
        knowledge: KnowledgeRepository,
        version: SourceVersion,
        diagnostics: list[str],
        gaps: list[str],
        namespace: str = DEFAULT_NAMESPACE,
    ) -> set[str]:
        written: set[str] = set()
        kinds = {key: item.finding.type.value for key, item in by_key.items()}
        for key in sorted(by_key):
            item = by_key[key]
            for claim in item.finding.relations:
                resolution = relations.resolve_target(
                    claim, item, namespace, by_key, entity_ids, knowledge
                )
                if resolution.outcome is relations.TargetOutcome.AMBIGUOUS:
                    diagnostics.append(relations.ambiguity_diagnostic(claim))
                    gaps.append(
                        relations.ambiguity_gap(claim, resolution.candidates)
                    )
                    continue
                target_id = self._target_id(
                    claim,
                    resolution,
                    transaction,
                    entity_ids,
                    kinds,
                    version,
                    diagnostics,
                    namespace,
                )
                if target_id is None:
                    continue
                try:
                    validate_pair(
                        claim.kind,
                        item.finding.type.value,
                        kinds[resolution.key],
                    )
                except InvalidRelationPair as exc:
                    diagnostics.append(str(exc))
                    continue
                captures, rejections, reasons = resolve_captures(
                    claim.evidence, harness
                )
                verdict = relations.assess_relation(
                    claim,
                    item,
                    resolution,
                    _target_name(claim, resolution, by_key, knowledge),
                    captures,
                    bool(rejections),
                )
                diagnostics.extend(reasons)
                diagnostics.extend(verdict.reasons)
                relation = Relation.create(
                    kind=claim.kind.value,
                    source_id=entity_ids[key],
                    target_id=target_id,
                    attributes=dict(claim.attributes),
                    confidence=verdict.confidence,
                )
                try:
                    transaction.put_relation(relation)
                except KnowledgeError as exc:
                    diagnostics.append(str(exc))
                    continue
                self._link_relation_evidence(
                    transaction, relation.id, verdict.captures, version
                )
                written.add(relation.id)
        return written

    def _target_id(
        self,
        claim: DocumentRelationClaim,
        resolution: relations.TargetResolution,
        transaction: RevisionTransaction,
        entity_ids: dict[str, EntityId],
        kinds: dict[str, str],
        version: SourceVersion,
        diagnostics: list[str],
        namespace: str,
    ) -> EntityId | None:
        if resolution.entity_id is not None:
            kinds.setdefault(resolution.key, resolution.kind)
            entity_ids.setdefault(resolution.key, resolution.entity_id)
            return resolution.entity_id
        kinds.setdefault(resolution.key, resolution.kind)
        known = entity_ids.get(resolution.key)
        if known is not None:
            return known
        kind = entity_kind(resolution.kind)
        placeholder = Entity.create(
            kind=resolution.kind,
            name=claim.target_subject or str(claim.target_id),
            stable_key=resolution.key,
            attributes={
                **{
                    name: claim.target_subject or str(claim.target_id)
                    for name in REQUIRED_ATTRIBUTES.get(kind, ())
                },
                NAMESPACE_ATTRIBUTE: namespace,
            },
            state=KnowledgeState.DECLARED,
            confidence=Confidence.UNRESOLVED,
            source_versions=(version.key,),
        )
        try:
            transaction.put_entity(placeholder)
        except IdentityCollision as exc:
            diagnostics.append(str(exc))
            return None
        entity_ids[resolution.key] = placeholder.id
        return placeholder.id

    def _link_relation_evidence(
        self,
        transaction: RevisionTransaction,
        relation_id: str,
        captures: Sequence[DocumentEvidenceCapture],
        version: SourceVersion,
    ) -> None:
        for capture in captures:
            evidence = make_evidence(
                source_id=version.source_id,
                version_hash=version.version_hash,
                locator=locator_from_dict(capture.locator),
                excerpt=capture.excerpt,
                captured_at=version.captured_at,
            )
            transaction.put_evidence(evidence, relation_ids=(relation_id,))


def _target_name(
    claim: DocumentRelationClaim,
    resolution: relations.TargetResolution,
    by_key: Mapping[str, VerifiedDocumentFinding],
    knowledge: KnowledgeRepository,
) -> str:
    item = by_key.get(resolution.key)
    if item is not None:
        return item.finding.subject
    if resolution.entity_id is not None:
        stored = knowledge.get_entity(resolution.entity_id)
        if stored is not None:
            return stored.name
    return claim.target_subject


