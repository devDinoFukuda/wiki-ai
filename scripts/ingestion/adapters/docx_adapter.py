"""Adapter DOCX via `zipfile` + XML defusado, sem dependência externa (§8.1).

Um `.docx` é um ZIP OOXML. Este adapter lê `word/document.xml` e percorre os
filhos do corpo NA ORDEM do documento, o que preserva a posição relativa de
parágrafos e tabelas — um `w:tbl` entre dois parágrafos sai entre eles, e não
no fim.

- `w:pPr/w:pStyle/@w:val` define heading: `Heading2`, `Ttulo 2`, `Heading Char`
  e variantes com espaço/acento são reconhecidas; o nível vira `heading_path`
  e `section` do localizador.
- `w:tbl` vira UM bloco `table`; suas células não são reemitidas como
  parágrafos soltos.
- `w:tab` e `w:br` viram tabulação/quebra em vez de sumirem, para que a fala
  original continue reconhecível.
- `docProps/core.xml` alimenta metadata (título, autor, data), que passa pela
  whitelist de `normalize` como qualquer outro metadado de fonte.

DTD e entidades são recusados por `_xmlsafe.parse_defused`: um `.docx` com
DOCTYPE malicioso vira `extraction_failed`, não expansão de entidade.
"""

from __future__ import annotations

import os
import re
import zipfile
from typing import Any

from ..normalize import (
    Block,
    BlockKind,
    ContentKind,
    Diagnostic,
    Preserved,
    Severity,
    SourceDocument,
    build_document,
    failed_document,
    make_block,
    preserve,
    version_label,
)
from ._xmlsafe import XmlMalformed, XmlNotAllowed, local_name, parse_defused

NAME = "docx_adapter"
EXTENSIONS = frozenset({".docx", ".docm"})

DOCUMENT_PART = "word/document.xml"
CORE_PART = "docProps/core.xml"

#: `Heading2`, `heading 2`, `Ttulo2`, `Título 2`, `Cabealho3`.
_HEADING_STYLE = re.compile(
    r"^(?:heading|ttulo|titulo|t[ií]tulo|cabe[cç]alho|cabealho)\s*[-_]?\s*(\d)$",
    re.IGNORECASE,
)
_CAPTION_STYLE = re.compile(r"^(?:caption|legenda)", re.IGNORECASE)
_LIST_STYLE = re.compile(r"^(?:listparagraph|parágrafodalista|paragrafodalista)", re.IGNORECASE)


def detect(path: str, head_bytes: bytes) -> bool:
    """ZIP com extensão OOXML de texto. A assinatura vale mais que o nome."""
    if not head_bytes.startswith(b"PK\x03\x04"):
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in EXTENSIONS:
        return True
    if ext:
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            return DOCUMENT_PART in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def _para_style(paragraph) -> str:
    for child in paragraph:
        if local_name(child.tag) != "pPr":
            continue
        for prop in child:
            if local_name(prop.tag) == "pStyle":
                for attr, value in prop.attrib.items():
                    if local_name(attr) == "val":
                        return str(value)
    return ""


def _numbered(paragraph) -> bool:
    """`w:numPr` marca item de lista numerada/com marcador."""
    for child in paragraph:
        if local_name(child.tag) != "pPr":
            continue
        for prop in child:
            if local_name(prop.tag) == "numPr":
                return True
    return False


def _para_text(node) -> str:
    """Texto do parágrafo preservando tabulação, quebra e campos de texto."""
    parts: list[str] = []
    for element in node.iter():
        name = local_name(element.tag)
        if name in ("t", "delText", "instrText"):
            if name == "instrText":
                continue
            parts.append(element.text or "")
        elif name == "tab":
            parts.append("\t")
        elif name in ("br", "cr"):
            parts.append("\n")
        elif name == "noBreakHyphen":
            parts.append("-")
    return "".join(parts).strip()


def _table_text(table) -> tuple[str, list[list[str]]]:
    """Tabela como texto (`célula | célula`) e como matriz preservada."""
    rows: list[list[str]] = []
    for row in table:
        if local_name(row.tag) != "tr":
            continue
        cells: list[str] = []
        for cell in row:
            if local_name(cell.tag) != "tc":
                continue
            cell_parts = [
                _para_text(paragraph)
                for paragraph in cell
                if local_name(paragraph.tag) == "p"
            ]
            cells.append(" ".join(p for p in cell_parts if p).strip())
        if cells:
            rows.append(cells)
    text = "\n".join(" | ".join(cell for cell in row) for row in rows)
    return text, rows


def _core_metadata(archive: zipfile.ZipFile) -> tuple[dict[str, Any], list[Diagnostic]]:
    """`docProps/core.xml` → title/participants/date, antes da whitelist."""
    if CORE_PART not in archive.namelist():
        return {}, []
    try:
        root = parse_defused(archive.read(CORE_PART))
    except (XmlNotAllowed, XmlMalformed) as exc:
        return {}, [
            Diagnostic(
                code="docx.core_unreadable",
                severity=Severity.WARNING,
                message=f"docProps/core.xml não pôde ser lido: {exc}",
                unavailable=("metadados do documento (docProps/core.xml)",),
            )
        ]
    raw: dict[str, Any] = {}
    creators: list[str] = []
    for element in root.iter():
        name = local_name(element.tag)
        value = (element.text or "").strip()
        if not value:
            continue
        if name == "title":
            raw["title"] = value
        elif name in ("creator", "lastModifiedBy"):
            if value not in creators:
                creators.append(value)
        elif name == "created":
            raw["date"] = value
        elif name in ("subject", "description", "category", "keywords"):
            # Fora da whitelist de propósito: vira metadata_extra inerte.
            raw[name] = value
    if creators:
        raw["participants"] = creators
    return raw, []


def parse_body(root, version: str) -> tuple[list[Block], list[Diagnostic]]:
    """Percorre o corpo do documento na ordem original."""
    body = None
    for child in root:
        if local_name(child.tag) == "body":
            body = child
            break
    if body is None:
        body = root
    blocks: list[Block] = []
    diags: list[Diagnostic] = []
    stack: list[tuple[int, str]] = []
    paragraph_no = 0
    for node in body:
        name = local_name(node.tag)
        if name == "p":
            text = _para_text(node)
            if not text:
                continue
            style = _para_style(node)
            heading = _HEADING_STYLE.match(style.replace(" ", ""))
            paragraph_no += 1
            if heading:
                level = int(heading.group(1))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                blocks.append(
                    make_block(
                        len(blocks),
                        BlockKind.HEADING,
                        text,
                        content_kind=ContentKind.PROSE,
                        version=version,
                        section=" > ".join([t for _, t in stack] + [text]),
                        paragraph=paragraph_no,
                        heading_path=[t for _, t in stack] + [text],
                    )
                )
                stack.append((level, text))
                continue
            if _CAPTION_STYLE.match(style):
                kind = BlockKind.CAPTION
            elif _LIST_STYLE.match(style.replace(" ", "")) or _numbered(node):
                kind = BlockKind.LIST
            else:
                kind = BlockKind.PARAGRAPH
            blocks.append(
                make_block(
                    len(blocks),
                    kind,
                    text,
                    content_kind=ContentKind.PROSE,
                    version=version,
                    section=" > ".join(t for _, t in stack),
                    paragraph=paragraph_no,
                    heading_path=[t for _, t in stack],
                )
            )
        elif name == "tbl":
            text, rows = _table_text(node)
            if not text.strip():
                diags.append(
                    Diagnostic(
                        code="docx.empty_table",
                        severity=Severity.INFO,
                        message="tabela sem texto extraível",
                        unavailable=("conteúdo de uma tabela",),
                    )
                )
                continue
            paragraph_no += 1
            blocks.append(
                make_block(
                    len(blocks),
                    BlockKind.TABLE,
                    text,
                    content_kind=ContentKind.PROSE,
                    data={"rows": rows},
                    version=version,
                    section=" > ".join(t for _, t in stack),
                    paragraph=paragraph_no,
                    heading_path=[t for _, t in stack],
                )
            )
        elif name == "sdt":
            # Structured document tag: o conteúdo real está em sdtContent.
            for inner in node:
                if local_name(inner.tag) != "sdtContent":
                    continue
                inner_blocks, inner_diags = parse_body(inner, version)
                for block in inner_blocks:
                    blocks.append(
                        make_block(
                            len(blocks),
                            block.kind,
                            block.text,
                            content_kind=block.content_kind,
                            data=block.data,
                            version=version,
                            section=block.locator.get("section", ""),
                            paragraph=len(blocks) + 1,
                            heading_path=block.locator.get("heading_path"),
                        )
                    )
                diags.extend(inner_diags)
    return blocks, diags


def _missing_parts(archive: zipfile.ZipFile) -> list[str]:
    """Partes cujo conteúdo este adapter não extrai — declaradas, não omitidas."""
    names = set(archive.namelist())
    missing: list[str] = []
    if any(n.startswith("word/embeddings/") for n in names):
        missing.append("objetos embutidos (word/embeddings/)")
    if any(n.startswith("word/media/") for n in names):
        missing.append("imagens (word/media/); OCR não configurado")
    if "word/footnotes.xml" in names:
        missing.append("notas de rodapé (word/footnotes.xml)")
    if "word/endnotes.xml" in names:
        missing.append("notas de fim (word/endnotes.xml)")
    if any(re.match(r"word/(header|footer)\d*\.xml$", n) for n in names):
        missing.append("cabeçalhos/rodapés (word/header*.xml, word/footer*.xml)")
    if "word/comments.xml" in names:
        missing.append("comentários de revisão (word/comments.xml)")
    return missing


def extract(path: str, preserved: Preserved | None = None) -> SourceDocument:
    """DOCX → `SourceDocument`. Parte ilegível vira falha explícita."""
    pres = preserved or preserve(path)
    version = version_label(pres.bytes_sha256)
    try:
        archive = zipfile.ZipFile(pres.path_original)
    except (zipfile.BadZipFile, OSError) as exc:
        return failed_document(
            pres,
            kind="docx",
            adapter=NAME,
            reason=f"arquivo não é um ZIP OOXML legível: {exc}",
            unavailable=("todo o conteúdo do documento",),
        )
    with archive:
        if DOCUMENT_PART not in archive.namelist():
            return failed_document(
                pres,
                kind="docx",
                adapter=NAME,
                reason=f"ZIP sem a parte {DOCUMENT_PART}; não é um documento Word",
                unavailable=("todo o conteúdo do documento",),
            )
        try:
            payload = archive.read(DOCUMENT_PART)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            return failed_document(
                pres,
                kind="docx",
                adapter=NAME,
                reason=f"{DOCUMENT_PART} não pôde ser descomprimido: {exc}",
                unavailable=(f"todo o conteúdo de {DOCUMENT_PART}",),
            )
        try:
            root = parse_defused(payload)
        except XmlNotAllowed as exc:
            return failed_document(
                pres,
                kind="docx",
                adapter=NAME,
                reason=f"{DOCUMENT_PART} recusado por segurança: {exc}",
                unavailable=("todo o conteúdo do documento",),
            )
        except XmlMalformed as exc:
            return failed_document(
                pres,
                kind="docx",
                adapter=NAME,
                reason=f"{DOCUMENT_PART} malformado: {exc}",
                unavailable=("todo o conteúdo do documento",),
            )
        raw_metadata, meta_diags = _core_metadata(archive)
        missing = _missing_parts(archive)
    blocks, diags = parse_body(root, version)
    diags = meta_diags + diags
    if missing:
        diags.append(
            Diagnostic(
                code="docx.parts_not_extracted",
                severity=Severity.WARNING,
                message=(
                    "partes do documento não são extraídas por este adapter: "
                    + "; ".join(missing)
                ),
                unavailable=tuple(missing),
                path=pres.path_original,
            )
        )
    if not blocks:
        return failed_document(
            pres,
            kind="docx",
            adapter=NAME,
            reason="nenhum parágrafo ou tabela com texto em word/document.xml",
            unavailable=("todo o conteúdo textual do documento",),
            extra_diagnostics=diags,
        )
    return build_document(
        pres,
        kind="docx",
        adapter=NAME,
        blocks=blocks,
        raw_metadata=raw_metadata,
        diagnostics=diags,
    )
