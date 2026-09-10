from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Union

from .errors import BlockValidationError

MAX_HEADING_LEVEL = 6
MAX_LIST_LEVEL = 8
_REL_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")


def _freeze(instance: object, name: str, value: Sequence[object]) -> tuple[object, ...]:
    frozen = tuple(value)
    object.__setattr__(instance, name, frozen)
    return frozen


@dataclass(frozen=True, slots=True)
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    hyperlink: str | None = None

    def __post_init__(self) -> None:
        if self.hyperlink is not None and not self.hyperlink.strip():
            raise BlockValidationError("Run.hyperlink vazio")


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    text: str

    def __post_init__(self) -> None:
        if not 1 <= self.level <= MAX_HEADING_LEVEL:
            raise BlockValidationError(f"Heading.level fora de 1..{MAX_HEADING_LEVEL}: {self.level}")


@dataclass(frozen=True, slots=True)
class Paragraph:
    runs: tuple[Run, ...]

    def __post_init__(self) -> None:
        _freeze(self, "runs", self.runs)


@dataclass(frozen=True, slots=True)
class ListItem:
    runs: tuple[Run, ...]
    level: int = 0

    def __post_init__(self) -> None:
        _freeze(self, "runs", self.runs)
        if not 0 <= self.level <= MAX_LIST_LEVEL:
            raise BlockValidationError(f"ListItem.level fora de 0..{MAX_LIST_LEVEL}: {self.level}")


@dataclass(frozen=True, slots=True)
class BulletList:
    items: tuple[ListItem, ...]
    ordered: bool = False

    def __post_init__(self) -> None:
        items = _freeze(self, "items", self.items)
        if not items:
            raise BlockValidationError("BulletList sem itens")


@dataclass(frozen=True, slots=True)
class Table:
    rows: tuple[tuple[Paragraph, ...], ...]
    header: bool = True

    def __post_init__(self) -> None:
        rows = tuple(tuple(row) for row in self.rows)
        object.__setattr__(self, "rows", rows)
        if not rows:
            raise BlockValidationError("Table sem linhas")
        widths = {len(row) for row in rows}
        if len(widths) > 1:
            raise BlockValidationError(f"Table com linhas de larguras diferentes: {sorted(widths)}")
        if widths == {0}:
            raise BlockValidationError("Table sem colunas")

    @property
    def column_count(self) -> int:
        return len(self.rows[0])


@dataclass(frozen=True, slots=True)
class CodeBlock:
    text: str
    language: str = ""


@dataclass(frozen=True, slots=True)
class Image:
    rel_id: str
    width_emu: int
    height_emu: int
    alt: str = ""

    def __post_init__(self) -> None:
        if not _REL_ID.match(self.rel_id):
            raise BlockValidationError(f"Image.rel_id invalido: {self.rel_id!r}")
        if self.width_emu <= 0 or self.height_emu <= 0:
            raise BlockValidationError(
                f"Image com dimensao nao positiva: {self.width_emu}x{self.height_emu}"
            )


@dataclass(frozen=True, slots=True)
class PageBreak:
    pass


@dataclass(frozen=True, slots=True)
class TableOfContents:
    pass


Block = Union[
    Heading,
    Paragraph,
    BulletList,
    Table,
    CodeBlock,
    Image,
    PageBreak,
    TableOfContents,
]


def text_runs(text: str, bold: bool = False, italic: bool = False) -> tuple[Run, ...]:
    return (Run(text=text, bold=bold, italic=italic),)


def text_cell(text: str, bold: bool = False) -> Paragraph:
    return Paragraph(runs=text_runs(text, bold=bold))
