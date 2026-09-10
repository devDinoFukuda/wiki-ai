from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from tests.benchmark.ground_truth.truth import Anchor, RepositoryTruth

__all__ = [
    "MARK",
    "CorpusUnknown",
    "GeneratedCorpus",
    "Sheet",
    "mark",
    "write",
    "write_docx",
]

MARK = "@@"


class CorpusUnknown(KeyError):
    def __init__(self, name: str) -> None:
        super().__init__(f"corpus desconhecido: {name}")
        self.name = name


@dataclass(frozen=True)
class GeneratedCorpus:
    root: Path
    truth: RepositoryTruth
    documents: tuple[Path, ...] = ()


def mark(token: str, text: str) -> str:
    return f"{MARK}{token}{MARK}{text}"


def _bare(line: str) -> str:
    if not line.startswith(MARK):
        return line
    return line.split(MARK, 2)[2]


class Sheet:
    def __init__(self, path: str, lines: tuple[str, ...]) -> None:
        self.path = path
        self.lines = lines
        self._marks: dict[str, int] = {}
        for number, line in enumerate(lines, start=1):
            if line.startswith(MARK):
                self._marks[line.split(MARK, 2)[1]] = number

    def text(self) -> str:
        return "\n".join(_bare(line) for line in self.lines) + "\n"

    def at(self, token: str) -> int:
        if token not in self._marks:
            raise CorpusUnknown(f"{self.path}#{token}")
        return self._marks[token]

    def span(self, first: str, last: str) -> Anchor:
        return Anchor(self.path, self.at(first), self.at(last))

    def line(self, token: str) -> Anchor:
        number = self.at(token)
        return Anchor(self.path, number, number)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_RELS_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def _paragraph(style: str, text: str) -> str:
    holder = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{holder}<w:r><w:t>{text}</w:t></w:r></w:p>"


def write_docx(
    path: Path, paragraphs: tuple[tuple[str, str], ...], title: str, author: str
) -> Path:
    body = "".join(_paragraph(style, text) for style, text in paragraphs)
    document = f'<?xml version="1.0"?><w:document {_W}><w:body>{body}</w:body></w:document>'
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("word/document.xml", document)
        archive.writestr(
            "word/_rels/document.xml.rels",
            f'<?xml version="1.0"?><Relationships {_RELS_NS}></Relationships>',
        )
        archive.writestr(
            "docProps/core.xml",
            '<?xml version="1.0"?><cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title>"
            f"<dc:creator>{author}</dc:creator>"
            "</cp:coreProperties>",
        )
    return path
