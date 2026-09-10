from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Sequence

from wiki_ai.knowledge.taxonomy import REQUIRED_ATTRIBUTES, EntityKind, RelationKind

from wiki_ai.investigation.compaction import InvestigationState, render
from wiki_ai.investigation.objective import Objective, ObjectiveKind

__all__ = [
    "FOCUS_FRAMING",
    "REPOSITORY_FOCUS",
    "METHOD_STEPS",
    "EVIDENCE_RULES",
    "RELATION_RULES",
    "SECTION_QUESTIONS",
    "SECTION_FRAMING",
    "allowed_finding_types",
    "allowed_relation_kinds",
    "section_questions",
    "build",
]

SECTION_FRAMING = (
    "The questions below are the frontier of this capability, not a form to fill. "
    "Answer the ones the executable code decides, ignore the ones this capability "
    "does not have, and declare a gap for the ones the code leaves open."
)

SECTION_QUESTIONS: Mapping[str, str] = MappingProxyType(
    {
        "purpose": "what does this capability do, decided by executable code",
        "entrypoints": "which entry points reach it and through which mechanism",
        "inputs": "what data enters it and with which shape",
        "outputs": "what does it return or emit",
        "preconditions": "what must hold before it runs",
        "rules": "which business rules does it implement, with conditions and effects",
        "invariants": "what stays true across it",
        "decisions": "which branches decide its behaviour and on which criteria",
        "states": "which states does it move between",
        "persistence": "what does it read or write and where",
        "integrations": "which external systems does it talk to and how",
        "events": "which events does it publish or consume",
        "failures": "how does it fail and what happens then",
        "retries": "does it retry, how many times and with which interval",
        "fallbacks": "what does it do when the main path is unavailable",
        "timeouts": "which timeouts bound it",
        "idempotency": "what happens when it runs twice with the same input",
        "edge_cases": "which boundary inputs does the code treat differently",
        "tests": "which executable tests exercise it",
        "dependencies": "what does it depend on to run",
    }
)

METHOD_STEPS: tuple[str, ...] = (
    "state a hypothesis about implemented behavior before reading anything",
    "locate candidates with repo.inventory, repo.search, repo.symbol or repo.dependencies",
    "read the executable lines with repo.read until the behavior is decided by code",
    "capture the exact lines that decide it with evidence.capture and keep the capture id",
    "relate the finding to other subjects with typed relations",
    "identify every relation target by target_owner or target_id whenever the name "
    "alone can match more than one subject",
    "capture the lines that decide the relation itself and pass them as the "
    "relation evidence",
    "declare a gap whenever the code does not answer the question",
)

EVIDENCE_RULES: tuple[str, ...] = (
    "every finding marked supported needs at least one evidence.capture over executable code",
    "comments, docstrings and prose never raise a finding above inferred",
    "a finding without evidence stays unresolved and becomes an explicit gap",
    "the statement must use terms that appear in the captured excerpt or its path",
    "never claim behavior you did not read in this snapshot",
)

RELATION_RULES: tuple[str, ...] = (
    "target_subject alone identifies a target only when a single subject in the "
    "repository carries that name",
    "target_owner names the subject that owns the target when the same name is "
    "used under more than one owner",
    "target_id points at the identifier the code itself declares and wins over "
    "target_subject and target_owner",
    "an ambiguous target is never guessed: the relation is dropped and the "
    "question is raised as a gap",
    "a relation is supported only when its own evidence excerpt names both the "
    "finding subject and the target; endpoints being supported never suffices",
)

_HEADING = "Objective"

REPOSITORY_FOCUS = "the whole repository"

FOCUS_FRAMING = (
    "repo.inventory, repo.search, repo.symbol, repo.dependencies, repo.tests and "
    "repo.config answer only inside this focus; pass scope=repository on those tools "
    "to widen a single call. repo.read, repo.references and evidence.capture reach "
    "any path in the snapshot, so following a call chain outside the focus is "
    "legitimate and is counted."
)


def allowed_finding_types() -> tuple[str, ...]:
    lines: list[str] = []
    for kind in EntityKind:
        required = REQUIRED_ATTRIBUTES.get(kind, ())
        if required:
            lines.append(f"{kind.value} (requires {', '.join(required)})")
        else:
            lines.append(kind.value)
    return tuple(lines)


def allowed_relation_kinds() -> tuple[str, ...]:
    return tuple(kind.value for kind in RelationKind)


def section_questions(sections: Sequence[str]) -> tuple[str, ...]:
    found: list[str] = []
    for section in sections:
        key = str(section).strip().lower()
        question = SECTION_QUESTIONS.get(key)
        if question is None:
            continue
        entry = f"{key}: {question}"
        if entry not in found:
            found.append(entry)
    return tuple(found)


def build(
    objective: Objective,
    state: InvestigationState,
    tool_names: Sequence[str],
    round_number: int,
    open_sections: Sequence[str] = (),
    subject: str = "",
    focus_paths: Sequence[str] = (),
) -> str:
    focused = tuple(str(item) for item in focus_paths if str(item).strip())
    blocks: list[str] = [
        f"{_HEADING}: {objective.goal}",
        f"Kind: {objective.kind.value}",
        f"Scope: {objective.scope.describe()}",
        f"Round: {round_number}",
        "Focus:\n"
        + (_bullets(focused) if focused else f"- {REPOSITORY_FOCUS}")
        + "\n"
        + FOCUS_FRAMING,
    ]
    if objective.constraints:
        blocks.append("Constraints:\n" + _bullets(objective.constraints))
    questions = (
        section_questions(open_sections)
        if objective.kind is ObjectiveKind.CAPABILITY_ANALYSIS
        else ()
    )
    if questions:
        heading = (
            f"Open questions about {subject}" if subject else "Open questions"
        )
        blocks.append(f"{heading}:\n{SECTION_FRAMING}\n" + _bullets(questions))
    blocks.append("Method:\n" + _bullets(METHOD_STEPS))
    blocks.append("Evidence rules:\n" + _bullets(EVIDENCE_RULES))
    blocks.append("Tools available:\n" + _bullets(tool_names))
    blocks.append("Allowed finding types:\n" + _bullets(allowed_finding_types()))
    blocks.append("Allowed relation kinds:\n" + _bullets(allowed_relation_kinds()))
    blocks.append("Relation rules:\n" + _bullets(RELATION_RULES))
    rendered = render(state)
    if rendered:
        blocks.append(rendered)
    else:
        blocks.append("State: first round, nothing investigated yet.")
    return "\n\n".join(blocks)


def _bullets(values: Sequence[str]) -> str:
    return "\n".join(f"- {value}" for value in values)
