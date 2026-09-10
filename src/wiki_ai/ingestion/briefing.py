from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Sequence

from wiki_ai.knowledge.taxonomy import REQUIRED_ATTRIBUTES, EntityKind, RelationKind

from wiki_ai.ingestion.source import SourceKind

__all__ = [
    "METHOD_STEPS",
    "EVIDENCE_RULES",
    "ALLOWED_TYPES_BY_KIND",
    "ALLOWED_RELATIONS_BY_KIND",
    "allowed_types",
    "allowed_relations",
    "build",
]

K = EntityKind
R = RelationKind

METHOD_STEPS: tuple[str, ...] = (
    "read doc.outline first and pick a section, worksheet, page or speaker to work on",
    "list what is there with doc.blocks and read the whole text with doc.read",
    "use doc.table for rule matrices, decision tables and catalogues",
    "use doc.graph for diagram nodes, edges and their neighbourhood",
    "use doc.search when a term must be traced across the whole source",
    "capture the exact blocks that state the claim with evidence.capture",
    "state one finding per claim, with the capture ids that carry it",
    "declare a gap whenever the source raises a question it does not answer",
)

EVIDENCE_RULES: tuple[str, ...] = (
    "a document states what people declared; it never proves implemented behaviour",
    "every finding needs the block ids it came from, captured in this run",
    "a finding without a capture stays unresolved and becomes an explicit gap",
    "the statement must use terms that appear in the captured text",
    "doc.hints are cheap guesses: they never raise confidence and never stand alone",
    "contradicting a hint is allowed and expected whenever the text says otherwise",
)

_TRANSCRIPT_TYPES: tuple[EntityKind, ...] = (
    K.DECISION_RECORD,
    K.PROPOSAL,
    K.REQUIREMENT,
    K.BUSINESS_RULE,
    K.DEPENDENCY,
    K.GAP,
    K.INITIATIVE,
    K.INVARIANT,
    K.FAILURE_MODE,
)

_SPREADSHEET_TYPES: tuple[EntityKind, ...] = (
    K.BUSINESS_RULE,
    K.VALIDATION,
    K.DATA_FIELD,
    K.DATA_ENTITY,
    K.CONFIGURATION,
    K.STATE,
    K.STATE_TRANSITION,
    K.DEPENDENCY,
    K.GAP,
)

_DIAGRAM_TYPES: tuple[EntityKind, ...] = (
    K.SYSTEM,
    K.MODULE,
    K.INTEGRATION,
    K.FLOW,
    K.FLOW_STEP,
    K.DEPENDENCY,
    K.PERSISTENCE,
    K.QUEUE,
    K.GAP,
)

_PROSE_TYPES: tuple[EntityKind, ...] = (
    K.REQUIREMENT,
    K.BUSINESS_RULE,
    K.DECISION_RECORD,
    K.INTEGRATION,
    K.DATA_CONTRACT,
    K.PROPOSAL,
    K.INITIATIVE,
    K.GAP,
)

_TRANSCRIPT_RELATIONS: tuple[RelationKind, ...] = (
    R.DECLARES,
    R.DEPENDS_ON,
    R.AFFECTS,
    R.PROPOSES_CHANGE_TO,
    R.SUPERSEDES,
    R.CONTRADICTS,
)

_SPREADSHEET_RELATIONS: tuple[RelationKind, ...] = (
    R.VALIDATES,
    R.DEPENDS_ON,
    R.TRANSITIONS_TO,
    R.BELONGS_TO,
    R.CONTRADICTS,
)

_DIAGRAM_RELATIONS: tuple[RelationKind, ...] = (
    R.CALLS,
    R.CONSUMES,
    R.PUBLISHES,
    R.DEPENDS_ON,
    R.PERSISTS_TO,
    R.BELONGS_TO,
)

_PROSE_RELATIONS: tuple[RelationKind, ...] = (
    R.DECLARES,
    R.DEPENDS_ON,
    R.AFFECTS,
    R.BELONGS_TO,
    R.CONTRADICTS,
    R.SUPERSEDES,
)

ALLOWED_TYPES_BY_KIND: Mapping[SourceKind, tuple[EntityKind, ...]] = MappingProxyType(
    {
        SourceKind.TRANSCRIPT: _TRANSCRIPT_TYPES,
        SourceKind.XLSX: _SPREADSHEET_TYPES,
        SourceKind.DRAWIO: _DIAGRAM_TYPES,
        SourceKind.DOCX: _PROSE_TYPES,
        SourceKind.PDF: _PROSE_TYPES,
        SourceKind.MARKDOWN: _PROSE_TYPES,
        SourceKind.HTML: _PROSE_TYPES,
        SourceKind.JSON: _SPREADSHEET_TYPES,
        SourceKind.XML: _DIAGRAM_TYPES,
        SourceKind.CODEBASE: _PROSE_TYPES,
    }
)

ALLOWED_RELATIONS_BY_KIND: Mapping[SourceKind, tuple[RelationKind, ...]] = (
    MappingProxyType(
        {
            SourceKind.TRANSCRIPT: _TRANSCRIPT_RELATIONS,
            SourceKind.XLSX: _SPREADSHEET_RELATIONS,
            SourceKind.DRAWIO: _DIAGRAM_RELATIONS,
            SourceKind.DOCX: _PROSE_RELATIONS,
            SourceKind.PDF: _PROSE_RELATIONS,
            SourceKind.MARKDOWN: _PROSE_RELATIONS,
            SourceKind.HTML: _PROSE_RELATIONS,
            SourceKind.JSON: _SPREADSHEET_RELATIONS,
            SourceKind.XML: _DIAGRAM_RELATIONS,
            SourceKind.CODEBASE: _PROSE_RELATIONS,
        }
    )
)

_GOAL_BY_KIND: Mapping[SourceKind, str] = MappingProxyType(
    {
        SourceKind.TRANSCRIPT: (
            "recover what the participants decided, proposed, required and left open"
        ),
        SourceKind.XLSX: (
            "recover the rule matrices, decision tables, catalogues, thresholds, "
            "states and cross-sheet dependencies this workbook encodes"
        ),
        SourceKind.DRAWIO: (
            "recover the systems, modules, integrations and flows the diagram draws, "
            "and the relations its edges declare"
        ),
        SourceKind.DOCX: "recover the requirements, rules, decisions and contracts this document states",
        SourceKind.PDF: "recover the requirements, rules, decisions and contracts this document states",
        SourceKind.MARKDOWN: "recover the requirements, rules, decisions and contracts this document states",
        SourceKind.HTML: "recover the requirements, rules, decisions and contracts this document states",
        SourceKind.JSON: "recover the catalogues, configurations and validations this payload encodes",
        SourceKind.XML: "recover the structures, integrations and dependencies this payload declares",
        SourceKind.CODEBASE: "recover what this source declares about the system",
    }
)


def allowed_types(kind: SourceKind) -> tuple[EntityKind, ...]:
    return ALLOWED_TYPES_BY_KIND.get(kind, _PROSE_TYPES)


def allowed_relations(kind: SourceKind) -> tuple[RelationKind, ...]:
    return ALLOWED_RELATIONS_BY_KIND.get(kind, _PROSE_RELATIONS)


def _type_line(kind: EntityKind) -> str:
    required = REQUIRED_ATTRIBUTES.get(kind, ())
    if required:
        return f"{kind.value} (requires {', '.join(required)})"
    return kind.value


def _bullets(values: Sequence[str]) -> str:
    return "\n".join(f"- {value}" for value in values)


def build(
    kind: SourceKind,
    uri: str,
    source_id: str,
    version_hash: str,
    tool_names: Sequence[str],
    frontier: Sequence[str],
    round_number: int,
    covered: Sequence[str] = (),
) -> str:
    blocks: list[str] = [
        f"Objective: {_GOAL_BY_KIND.get(kind, _GOAL_BY_KIND[SourceKind.DOCX])}",
        f"Source: {uri}",
        f"Source kind: {kind.value}",
        f"Source id: {source_id}",
        f"Version hash: {version_hash}",
        f"Round: {round_number}",
        "Method:\n" + _bullets(METHOD_STEPS),
        "Evidence rules:\n" + _bullets(EVIDENCE_RULES),
        "Tools available:\n" + _bullets(tuple(tool_names)),
        "Allowed finding types:\n"
        + _bullets(tuple(_type_line(item) for item in allowed_types(kind))),
        "Allowed relation kinds:\n"
        + _bullets(tuple(item.value for item in allowed_relations(kind))),
    ]
    if frontier:
        blocks.append("Not covered yet:\n" + _bullets(tuple(frontier)))
    else:
        blocks.append("Not covered yet: nothing, every part of the source was visited.")
    if covered:
        blocks.append("Already covered:\n" + _bullets(tuple(covered)))
    return "\n\n".join(blocks)
