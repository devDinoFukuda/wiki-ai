from __future__ import annotations

from typing import Sequence

from .model import DiagramSpec, NarrativeBlock, NarrativeDocument, NarrativeKind

__all__ = ["markdown_filename", "markdown_for"]


def markdown_filename(document: NarrativeDocument) -> str:
    return f"{document.document_id}.md"


def _table_lines(rows: Sequence[Sequence[str]]) -> list[str]:
    if not rows:
        return []
    header = rows[0]
    lines = ["| " + " | ".join(str(cell) for cell in header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows[1:]:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def _diagram_lines(spec: DiagramSpec) -> list[str]:
    lines = [f"### {spec.title}", "", "```mermaid", spec.mermaid_text, "```", ""]
    lines.extend(f"- {sentence}" for sentence in spec.textual_equivalent)
    lines.append("")
    return lines


def _block_lines(
    block: NarrativeBlock, diagrams: Sequence[DiagramSpec]
) -> list[str]:
    if block.kind is NarrativeKind.SECTION:
        return ["", f"## {block.section_number}. {block.title}", ""]
    if block.kind is NarrativeKind.PARAGRAPH:
        return [item.rendered() for item in block.assertions] + [""]
    if block.kind is NarrativeKind.BULLETS:
        return [f"- {item.rendered()}" for item in block.assertions] + [""]
    if block.kind is NarrativeKind.TABLE:
        return _table_lines(block.rows) + [""]
    if 0 <= block.diagram_index < len(diagrams):
        return _diagram_lines(diagrams[block.diagram_index])
    return []


def markdown_for(document: NarrativeDocument) -> str:
    lines: list[str] = [f"# {document.title}"]
    for block in document.body:
        lines.extend(_block_lines(block, document.diagrams))
    return "\n".join(lines).rstrip() + "\n"
