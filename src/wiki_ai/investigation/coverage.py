from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Sequence

from wiki_ai.knowledge.identity import canonical_name
from wiki_ai.knowledge.model import Confidence
from wiki_ai.knowledge.taxonomy import EntityKind
from wiki_ai.repository.symbols import SymbolKind

from wiki_ai.investigation.finding import RelationClaim
from wiki_ai.investigation.verifier import VerificationReport, VerifiedFinding

__all__ = [
    "AMBIGUOUS_KIND",
    "ENTRY_LIKE_SYMBOL_KINDS",
    "ENTRY_LIKE_ENTITY_KINDS",
    "INTEGRATION_KINDS",
    "SECTION_KIND",
    "FrontierItem",
    "CoverageState",
    "CompletenessReport",
    "initial_state",
    "target_identity",
    "entity_identities",
    "update",
    "with_sections",
    "sections_of",
]

SECTION_KIND = "section"
AMBIGUOUS_KIND = "relation"

ENTRY_LIKE_SYMBOL_KINDS: tuple[SymbolKind, ...] = (
    SymbolKind.PROGRAM,
    SymbolKind.STEP,
    SymbolKind.PROCEDURE,
    SymbolKind.MODULE,
)

ENTRY_LIKE_ENTITY_KINDS: tuple[EntityKind, ...] = (
    EntityKind.ENTRY_POINT,
    EntityKind.ENDPOINT,
    EntityKind.OPERATION,
    EntityKind.PROCEDURE,
)

CAPABILITY_KINDS: tuple[EntityKind, ...] = (EntityKind.CAPABILITY,)

INTEGRATION_KINDS: tuple[EntityKind, ...] = (
    EntityKind.INTEGRATION,
    EntityKind.TOPIC,
    EntityKind.QUEUE,
    EntityKind.EVENT,
)


@dataclass(frozen=True)
class FrontierItem:
    kind: str
    subject: str
    reason: str

    @property
    def key(self) -> str:
        return f"{self.kind}::{self.subject.strip().lower()}"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "reason": self.reason}


@dataclass(frozen=True)
class CompletenessReport:
    score: float
    discovered_entrypoints: tuple[str, ...] = ()
    discovered_capabilities: tuple[str, ...] = ()
    unresolved_calls: tuple[str, ...] = ()
    unresolved_effects: tuple[str, ...] = ()
    unresolved_integrations: tuple[str, ...] = ()
    unresolved_branches: tuple[str, ...] = ()
    unresolved_sections: tuple[str, ...] = ()
    unresolved_relations: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    explicit_gaps: tuple[str, ...] = ()
    files_covered: int = 0
    files_total: int = 0

    @property
    def outstanding(self) -> tuple[str, ...]:
        return (
            self.unresolved_calls
            + self.unresolved_effects
            + self.unresolved_integrations
            + self.unresolved_branches
            + self.unresolved_sections
            + self.unresolved_relations
            + self.missing_evidence
            + self.explicit_gaps
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "discovered_entrypoints": list(self.discovered_entrypoints),
            "discovered_capabilities": list(self.discovered_capabilities),
            "unresolved_calls": list(self.unresolved_calls),
            "unresolved_effects": list(self.unresolved_effects),
            "unresolved_integrations": list(self.unresolved_integrations),
            "unresolved_branches": list(self.unresolved_branches),
            "unresolved_sections": list(self.unresolved_sections),
            "unresolved_relations": list(self.unresolved_relations),
            "missing_evidence": list(self.missing_evidence),
            "explicit_gaps": list(self.explicit_gaps),
            "files_covered": self.files_covered,
            "files_total": self.files_total,
        }


@dataclass(frozen=True)
class CoverageState:
    files_total: int = 0
    entrypoints: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    files_covered: tuple[str, ...] = ()
    frontier: tuple[FrontierItem, ...] = ()
    resolved_subjects: tuple[str, ...] = ()
    known_identities: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    explicit_gaps: tuple[str, ...] = ()
    rounds: int = 0

    def frontier_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.frontier)

    def completeness(self) -> CompletenessReport:
        calls = _frontier_of(self.frontier, "call")
        effects = _frontier_of(self.frontier, "effect")
        integrations = _frontier_of(self.frontier, "integration")
        branches = _frontier_of(self.frontier, "branch")
        sections = _frontier_of(self.frontier, SECTION_KIND)
        ambiguous = _frontier_of(self.frontier, AMBIGUOUS_KIND)
        outstanding = (
            len(calls)
            + len(effects)
            + len(integrations)
            + len(branches)
            + len(sections)
            + len(ambiguous)
            + len(self.missing_evidence)
            + len(self.explicit_gaps)
        )
        known = len(self.resolved_subjects) + outstanding
        score = 1.0 if known == 0 else len(self.resolved_subjects) / known
        return CompletenessReport(
            score=score,
            discovered_entrypoints=self.entrypoints,
            discovered_capabilities=self.capabilities,
            unresolved_calls=calls,
            unresolved_effects=effects,
            unresolved_integrations=integrations,
            unresolved_branches=branches,
            unresolved_sections=sections,
            unresolved_relations=ambiguous,
            missing_evidence=self.missing_evidence,
            explicit_gaps=self.explicit_gaps,
            files_covered=len(self.files_covered),
            files_total=self.files_total,
        )

    def is_done(self, remaining_budget: int) -> bool:
        if remaining_budget <= 0:
            return True
        return not self.frontier

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "entrypoints": list(self.entrypoints),
            "capabilities": list(self.capabilities),
            "files_covered": list(self.files_covered),
            "frontier": [item.to_dict() for item in self.frontier],
            "resolved_subjects": list(self.resolved_subjects),
            "missing_evidence": list(self.missing_evidence),
            "explicit_gaps": list(self.explicit_gaps),
        }


def _frontier_of(frontier: Sequence[FrontierItem], kind: str) -> tuple[str, ...]:
    return tuple(item.subject for item in frontier if item.kind == kind)


def initial_state(
    files_total: int,
    entrypoints: Sequence[str] = (),
    integrations: Sequence[str] = (),
) -> CoverageState:
    frontier = tuple(
        FrontierItem("entrypoint", name, "discovered by inventory and symbols")
        for name in _unique(entrypoints)
    ) + tuple(
        FrontierItem("integration", name, "declared by dependency detection")
        for name in _unique(integrations)
    )
    return CoverageState(
        files_total=files_total,
        entrypoints=_unique(entrypoints),
        frontier=frontier,
    )


def target_identity(claim: RelationClaim, source_owner: str | None) -> str:
    if claim.target_id:
        return f"{EXPLICIT_PREFIX}{claim.target_id}"
    owner = claim.target_owner or source_owner or ""
    kind = "" if claim.target_type is None else claim.target_type.value
    return "::".join(
        (
            canonical_name(kind),
            canonical_name(owner) or GLOBAL_OWNER,
            canonical_name(claim.target_subject),
        )
    )


def entity_identities(item: VerifiedFinding) -> tuple[str, ...]:
    name = canonical_name(item.finding.subject)
    declared = canonical_name(item.finding.owner or "")
    owner = declared or GLOBAL_OWNER
    kind = canonical_name(item.finding.type.value)
    found = [
        f"{kind}::{owner}::{name}",
        f"::{owner}::{name}",
    ]
    if not declared:
        found.append(f"{kind}::{GLOBAL_OWNER}::{name}")
        found.append(f"::{GLOBAL_OWNER}::{name}")
    if item.finding.explicit_id:
        found.append(f"{EXPLICIT_PREFIX}{item.finding.explicit_id}")
    return tuple(dict.fromkeys(found))


def _relation_frontier(item: VerifiedFinding, known: set[str]) -> list[FrontierItem]:
    found: list[FrontierItem] = []
    for claim in item.finding.relations:
        if target_identity(claim, item.finding.owner) in known:
            continue
        kind = "integration" if claim.kind.value in _INTEGRATION_RELATIONS else "call"
        found.append(
            FrontierItem(
                kind,
                claim.label,
                f"{item.finding.subject} {claim.kind.value} an unknown target",
            )
        )
    return found


_INTEGRATION_RELATIONS = frozenset(
    {"publishes", "consumes", "persists_to", "reads", "writes"}
)

EXPLICIT_PREFIX = "explicit::"
GLOBAL_OWNER = "global"

_INCOMPLETE_KINDS = frozenset({"effect", "branch"})

_PERSISTENT_KINDS = frozenset({SECTION_KIND, AMBIGUOUS_KIND})


def _effect_frontier(item: VerifiedFinding) -> list[FrontierItem]:
    if not item.finding.conditions or item.finding.effects:
        return []
    return [
        FrontierItem(
            "effect",
            item.finding.subject,
            "condition declared without a corresponding effect",
        )
    ]


def _branch_frontier(item: VerifiedFinding) -> list[FrontierItem]:
    if len(item.finding.conditions) < 2:
        return []
    if len(item.finding.effects) >= len(item.finding.conditions):
        return []
    return [
        FrontierItem(
            "branch",
            item.finding.subject,
            f"{len(item.finding.conditions)} conditions with "
            f"{len(item.finding.effects)} effects",
        )
    ]


def update(
    state: CoverageState,
    report: VerificationReport,
    covered_paths: Sequence[str] = (),
    *,
    unresolved_relations: Sequence[str] = (),
    gaps: Sequence[str] = (),
) -> CoverageState:
    resolved = list(state.resolved_subjects)
    entrypoints = list(state.entrypoints)
    capabilities = list(state.capabilities)
    integrations: list[str] = []
    missing = list(state.missing_evidence)
    opened = list(state.explicit_gaps)
    files = list(state.files_covered)

    for path in covered_paths:
        if path not in files:
            files.append(path)

    for item in report.verified:
        subject = item.finding.subject.strip().lower()
        if item.confidence in (Confidence.SUPPORTED, Confidence.INFERRED):
            if subject not in resolved:
                resolved.append(subject)
        if item.confidence is Confidence.UNRESOLVED:
            label = f"{item.finding.type.value}: {item.finding.subject}"
            if label not in missing:
                missing.append(label)
        if item.finding.type in ENTRY_LIKE_ENTITY_KINDS and item.finding.subject not in entrypoints:
            entrypoints.append(item.finding.subject)
        if item.finding.type in CAPABILITY_KINDS and item.finding.subject not in capabilities:
            capabilities.append(item.finding.subject)
        if item.finding.type in INTEGRATION_KINDS and subject not in integrations:
            integrations.append(subject)
        for resolved_evidence in item.evidence:
            if resolved_evidence.capture.path not in files:
                files.append(resolved_evidence.capture.path)

    for question in tuple(report.gaps) + tuple(gaps):
        if question not in opened:
            opened.append(question)

    known = set(resolved)
    known |= {name.strip().lower() for name in entrypoints}
    known |= {name.strip().lower() for name in capabilities}
    known |= set(integrations)
    identities = set(state.known_identities)
    for item in report.verified:
        identities |= set(entity_identities(item))

    round_subjects = {
        item.finding.subject.strip().lower() for item in report.verified
    }
    frontier: list[FrontierItem] = []
    for item in state.frontier:
        subject = item.subject.strip().lower()
        if item.kind in _PERSISTENT_KINDS:
            frontier.append(item)
            continue
        if item.kind in _INCOMPLETE_KINDS:
            if subject in round_subjects:
                continue
        elif subject in known:
            continue
        frontier.append(item)
    for item in report.verified:
        for candidate in _relation_frontier(item, identities):
            if candidate.key not in {existing.key for existing in frontier}:
                frontier.append(candidate)
        for candidate in _effect_frontier(item) + _branch_frontier(item):
            if candidate.key not in {existing.key for existing in frontier}:
                frontier.append(candidate)
    for subject in _unique(unresolved_relations):
        candidate = FrontierItem(
            AMBIGUOUS_KIND, subject, "relation target could not be resolved"
        )
        if candidate.key not in {existing.key for existing in frontier}:
            frontier.append(candidate)

    return replace(
        state,
        entrypoints=tuple(entrypoints),
        capabilities=tuple(capabilities),
        files_covered=tuple(files),
        frontier=tuple(frontier),
        resolved_subjects=tuple(resolved),
        known_identities=tuple(sorted(identities)),
        missing_evidence=tuple(missing),
        explicit_gaps=tuple(opened),
        rounds=state.rounds + 1,
    )


def section_subject(capability: str, section: str) -> str:
    return f"{capability} :: {section}"


def with_sections(
    state: CoverageState,
    capability: str,
    sections: Sequence[str],
    reason: str = "section of the capability profile still empty",
) -> CoverageState:
    prefix = f"{SECTION_KIND}::{capability.strip().lower()} :: "
    kept = [
        item
        for item in state.frontier
        if not (item.kind == SECTION_KIND and item.key.startswith(prefix))
    ]
    for section in _unique(sections):
        candidate = FrontierItem(
            SECTION_KIND, section_subject(capability, section), reason
        )
        if candidate.key not in {item.key for item in kept}:
            kept.append(candidate)
    return replace(state, frontier=tuple(kept))


def sections_of(state: CoverageState, capability: str) -> tuple[str, ...]:
    prefix = f"{capability.strip().lower()} :: "
    found: list[str] = []
    for item in state.frontier:
        if item.kind != SECTION_KIND:
            continue
        subject = item.subject.strip().lower()
        if not subject.startswith(prefix):
            continue
        found.append(item.subject[len(prefix) :])
    return tuple(found)


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in values if str(item).strip()))

