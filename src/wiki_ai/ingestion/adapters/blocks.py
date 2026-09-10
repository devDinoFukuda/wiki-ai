from __future__ import annotations

from typing import Any, Mapping

from wiki_ai.ingestion.outcome import Gap
from wiki_ai.ingestion.source import (
    Block,
    BlockKind,
    Diagnostic,
    DiagnosticLevel,
    make_block,
)

__all__ = ["BlockBuilder", "IMAGE_GAP_CODE", "image_gap"]

IMAGE_GAP_CODE = Gap.IMAGE_CONTENT_NOT_INTERPRETED.value


class BlockBuilder:
    def __init__(self, source_id: str) -> None:
        self._source_id = source_id
        self._blocks: list[Block] = []
        self._diagnostics: list[Diagnostic] = []

    def add(
        self,
        kind: BlockKind,
        text: str,
        locator: Mapping[str, Any] | None = None,
        parent_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Block:
        block = make_block(
            source_id=self._source_id,
            order=len(self._blocks),
            kind=kind,
            text=text,
            locator=dict(locator or {}),
            parent_id=parent_id,
            attributes=dict(attributes or {}),
        )
        self._blocks.append(block)
        return block

    def record(
        self,
        level: DiagnosticLevel,
        code: str,
        message: str,
        locator: Mapping[str, Any] | None = None,
    ) -> Diagnostic:
        diagnostic = Diagnostic(
            level=level, code=code, message=message, locator=dict(locator or {})
        )
        self._diagnostics.append(diagnostic)
        return diagnostic

    def warn(
        self, code: str, message: str, locator: Mapping[str, Any] | None = None
    ) -> Diagnostic:
        return self.record(DiagnosticLevel.WARNING, code, message, locator)

    def fail(
        self, code: str, message: str, locator: Mapping[str, Any] | None = None
    ) -> Diagnostic:
        return self.record(DiagnosticLevel.ERROR, code, message, locator)

    def extend_diagnostics(self, found: tuple[Diagnostic, ...]) -> None:
        self._diagnostics.extend(found)

    @property
    def blocks(self) -> tuple[Block, ...]:
        return tuple(self._blocks)

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return tuple(self._diagnostics)


def image_gap(builder: BlockBuilder, target: str, locator: Mapping[str, Any]) -> None:
    builder.warn(
        IMAGE_GAP_CODE,
        f"{target} carries image content that the structural adapter does not interpret",
        locator,
    )
