from __future__ import annotations

import re

__all__ = [
    "CELL_PATTERN",
    "CROSS_SHEET_PATTERN",
    "column_index",
    "column_name",
    "split_reference",
    "cell_reference",
    "range_reference",
]

CELL_PATTERN = re.compile(r"^\$?([A-Z]{1,3})\$?(\d+)$")
CROSS_SHEET_PATTERN = re.compile(
    r"(?:'(?P<quoted>[^']+)'|(?P<plain>[A-Za-z0-9_À-ſ]+))!"
    r"\$?(?P<col>[A-Z]{1,3})\$?(?P<row>\d+)"
)


def column_index(letters: str) -> int:
    value = 0
    for char in letters.upper():
        value = value * 26 + (ord(char) - 64)
    return value


def column_name(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def split_reference(reference: str) -> tuple[int, int] | None:
    match = CELL_PATTERN.match(reference.strip().upper())
    if match is None:
        return None
    return int(match.group(2)), column_index(match.group(1))


def cell_reference(row: int, column: int) -> str:
    return f"{column_name(column)}{row}"


def range_reference(top: int, left: int, bottom: int, right: int) -> str:
    return f"{cell_reference(top, left)}:{cell_reference(bottom, right)}"
