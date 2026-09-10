from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "DocumentKind",
    "DiagramKind",
    "Assertion",
    "AssertionStance",
    "SectionPlan",
    "DocumentPlan",
    "PublicationPlan",
    "NarrativeBlock",
    "NarrativeKind",
    "TraceEntry",
    "DiagramSpec",
    "NarrativeDocument",
    "DocumentProperties",
    "CAPABILITY_SECTIONS",
    "SYSTEM_SECTIONS",
    "CATALOG_SECTIONS",
    "GAPS_SECTIONS",
    "CHANGE_IMPACT_SECTIONS",
    "GAPS_SECTION_TITLE",
    "TRACEABILITY_SECTION_TITLE",
]

GAPS_SECTION_TITLE = "Lacunas conhecidas"
TRACEABILITY_SECTION_TITLE = "Rastreabilidade técnica"


class DocumentKind(str, enum.Enum):
    SYSTEM_OVERVIEW = "system_overview"
    CAPABILITY = "capability"
    INTEGRATION_CATALOG = "integration_catalog"
    RULES_CATALOG = "rules_catalog"
    GAPS_REPORT = "gaps_report"
    CHANGE_IMPACT = "change_impact"


class DiagramKind(str, enum.Enum):
    FLOWCHART = "flowchart"
    SEQUENCE = "sequence"
    STATE = "state"
    DEPENDENCY = "dependency"


class NarrativeKind(str, enum.Enum):
    SECTION = "section"
    PARAGRAPH = "paragraph"
    BULLETS = "bullets"
    TABLE = "table"
    DIAGRAM = "diagram"


class AssertionStance(str, enum.Enum):
    FACT = "fact"
    RESERVED = "reserved"
    OPEN = "open"


CAPABILITY_SECTIONS: tuple[str, ...] = (
    "Objetivo",
    "Como funciona",
    "Fluxo principal",
    "Inputs",
    "Outputs",
    "Regras de negócio",
    "Invariantes",
    "Edge cases",
    "Integrações",
    "Persistência",
    "Falhas e recuperação",
    "Diagramas",
    "Testes e evidências",
    GAPS_SECTION_TITLE,
    TRACEABILITY_SECTION_TITLE,
)

SYSTEM_SECTIONS: tuple[str, ...] = (
    "Objetivo",
    "Como funciona",
    "Capacidades",
    "Módulos",
    "Integrações",
    "Persistência",
    "Diagramas",
    GAPS_SECTION_TITLE,
    TRACEABILITY_SECTION_TITLE,
)

CATALOG_SECTIONS: tuple[str, ...] = (
    "Objetivo",
    "Catálogo",
    "Detalhamento",
    GAPS_SECTION_TITLE,
    TRACEABILITY_SECTION_TITLE,
)

GAPS_SECTIONS: tuple[str, ...] = (
    "Objetivo",
    "Lacunas bloqueantes",
    "Lacunas não bloqueantes",
    "Contradições entre fontes",
    GAPS_SECTION_TITLE,
    TRACEABILITY_SECTION_TITLE,
)

CHANGE_IMPACT_SECTIONS: tuple[str, ...] = (
    "Objetivo",
    "O que muda",
    "O que é afetado",
    "Comportamento implementado versus decisão registrada",
    "Diagramas",
    GAPS_SECTION_TITLE,
    TRACEABILITY_SECTION_TITLE,
)


@dataclass(frozen=True)
class SectionPlan:
    number: int
    title: str
    content_ref: str

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError(f"SectionPlan.number deve ser >= 1: {self.number}")
        if not str(self.title).strip():
            raise ValueError("SectionPlan.title vazio")
        if not str(self.content_ref).strip():
            raise ValueError("SectionPlan.content_ref vazio")


@dataclass(frozen=True)
class DocumentPlan:
    document_id: str
    title: str
    subject_entity_id: str
    kind: DocumentKind
    sections: tuple[SectionPlan, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", tuple(self.sections))
        if not str(self.document_id).strip():
            raise ValueError("DocumentPlan.document_id vazio")
        if not str(self.title).strip():
            raise ValueError("DocumentPlan.title vazio")
        numbers = [section.number for section in self.sections]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError(
                f"seções de {self.document_id} devem ser numeradas de 1..n: {numbers}"
            )

    def section_titles(self) -> tuple[str, ...]:
        return tuple(section.title for section in self.sections)


@dataclass(frozen=True)
class PublicationPlan:
    documents: tuple[DocumentPlan, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "documents", tuple(self.documents))
        seen: set[str] = set()
        for document in self.documents:
            if document.document_id in seen:
                raise ValueError(f"documento duplicado no plano: {document.document_id}")
            seen.add(document.document_id)

    def __len__(self) -> int:
        return len(self.documents)

    def of_kind(self, kind: DocumentKind) -> tuple[DocumentPlan, ...]:
        return tuple(d for d in self.documents if d.kind is kind)

    @property
    def is_empty(self) -> bool:
        return not self.documents


@dataclass(frozen=True)
class Assertion:
    text: str
    stance: AssertionStance = AssertionStance.FACT
    qualifier: str = ""

    def rendered(self) -> str:
        body = str(self.text).strip().rstrip(".")
        if not self.qualifier:
            return f"{body}."
        return f"{body} ({self.qualifier})."


@dataclass(frozen=True)
class NarrativeBlock:
    kind: NarrativeKind
    section_number: int
    title: str = ""
    assertions: tuple[Assertion, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    diagram_index: int = -1

    def __post_init__(self) -> None:
        object.__setattr__(self, "assertions", tuple(self.assertions))
        object.__setattr__(self, "rows", tuple(tuple(row) for row in self.rows))


@dataclass(frozen=True)
class TraceEntry:
    finding: str
    evidence: str
    source_version: str
    locator_kind: str
    locator_primary: str
    locator_detail: str

    def as_row(self) -> tuple[str, ...]:
        return (
            self.finding,
            self.evidence,
            self.source_version,
            self.locator_primary,
            self.locator_detail,
        )


@dataclass(frozen=True)
class DiagramSpec:
    kind: DiagramKind
    title: str
    mermaid_text: str
    textual_equivalent: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "textual_equivalent", tuple(self.textual_equivalent))
        if not self.textual_equivalent:
            raise ValueError(
                f"diagrama {self.title!r} sem equivalente textual: representação "
                "visual exige lista de frases equivalentes"
            )


@dataclass(frozen=True)
class DocumentProperties:
    title: str
    subject: str
    category: str
    description: str
    keywords: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "keywords", tuple(self.keywords))


@dataclass(frozen=True)
class NarrativeDocument:
    document_id: str
    title: str
    body: tuple[NarrativeBlock, ...] = ()
    traceability: tuple[TraceEntry, ...] = ()
    diagrams: tuple[DiagramSpec, ...] = ()
    properties: DocumentProperties | None = None
    entity_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", tuple(self.body))
        object.__setattr__(self, "traceability", tuple(self.traceability))
        object.__setattr__(self, "diagrams", tuple(self.diagrams))
        object.__setattr__(self, "entity_ids", tuple(self.entity_ids))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))

    def section_titles(self) -> tuple[str, ...]:
        return tuple(
            block.title
            for block in self.body
            if block.kind is NarrativeKind.SECTION
        )
