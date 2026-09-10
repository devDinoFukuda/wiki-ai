from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .docx.blocks import (
    Block,
    BulletList,
    CodeBlock,
    Heading,
    ListItem,
    Paragraph,
    Run,
    Table,
    TableOfContents,
    text_cell,
    text_runs,
)
from .docx.package import build_package
from .docx.properties import CoreProperties, sharepoint_safe_filename
from .model import (
    Assertion,
    DiagramSpec,
    NarrativeBlock,
    NarrativeDocument,
    NarrativeKind,
)

__all__ = [
    "MERMAID_LANGUAGE",
    "DOCUMENT_CREATOR",
    "FIXED_MOMENT",
    "docx_filename",
    "document_blocks",
    "core_properties",
    "render_document",
]

MERMAID_LANGUAGE = "mermaid"
DOCUMENT_CREATOR = "wiki-ai"
FIXED_MOMENT = datetime(2020, 1, 1, tzinfo=timezone.utc)
_EQUIVALENT_LABEL = "Equivalente textual do diagrama:"
_MAX_KEYWORDS = 24


def docx_filename(document: NarrativeDocument) -> str:
    return sharepoint_safe_filename(document.document_id)


def _assertion_paragraph(assertion: Assertion) -> Paragraph:
    return Paragraph(runs=text_runs(assertion.rendered()))


def _assertion_item(assertion: Assertion) -> ListItem:
    return ListItem(runs=text_runs(assertion.rendered()))


def _table_block(rows: Sequence[Sequence[str]]) -> Table:
    return Table(
        rows=tuple(
            tuple(text_cell(str(cell), bold=(index == 0)) for cell in row)
            for index, row in enumerate(rows)
        ),
        header=True,
    )


def _diagram_blocks(spec: DiagramSpec) -> tuple[Block, ...]:
    return (
        Heading(level=2, text=spec.title),
        CodeBlock(text=spec.mermaid_text, language=MERMAID_LANGUAGE),
        Paragraph(runs=(Run(text=_EQUIVALENT_LABEL, italic=True),)),
        BulletList(
            items=tuple(
                ListItem(runs=text_runs(sentence)) for sentence in spec.textual_equivalent
            )
        ),
    )


def _narrative_blocks(
    block: NarrativeBlock, diagrams: Sequence[DiagramSpec]
) -> tuple[Block, ...]:
    if block.kind is NarrativeKind.SECTION:
        return (Heading(level=1, text=f"{block.section_number}. {block.title}"),)
    if block.kind is NarrativeKind.PARAGRAPH:
        return tuple(_assertion_paragraph(item) for item in block.assertions)
    if block.kind is NarrativeKind.BULLETS:
        if not block.assertions:
            return ()
        return (
            BulletList(
                items=tuple(_assertion_item(item) for item in block.assertions)
            ),
        )
    if block.kind is NarrativeKind.TABLE:
        if not block.rows:
            return ()
        return (_table_block(block.rows),)
    if 0 <= block.diagram_index < len(diagrams):
        return _diagram_blocks(diagrams[block.diagram_index])
    return ()


def document_blocks(document: NarrativeDocument) -> tuple[Block, ...]:
    found: list[Block] = [Heading(level=1, text=document.title), TableOfContents()]
    for block in document.body:
        found.extend(_narrative_blocks(block, document.diagrams))
    return tuple(found)


def core_properties(document: NarrativeDocument) -> CoreProperties:
    properties = document.properties
    if properties is None:
        return CoreProperties(
            title=document.title,
            creator=DOCUMENT_CREATOR,
            created=FIXED_MOMENT,
            modified=FIXED_MOMENT,
        )
    return CoreProperties(
        title=properties.title,
        subject=properties.subject,
        creator=DOCUMENT_CREATOR,
        keywords=tuple(properties.keywords)[:_MAX_KEYWORDS],
        description=properties.description,
        created=FIXED_MOMENT,
        modified=FIXED_MOMENT,
        revision=1,
        category=properties.category,
    )


def render_document(document: NarrativeDocument, out_path: str | Path) -> Path:
    return build_package(
        document_blocks(document), core_properties(document), Path(out_path)
    )
