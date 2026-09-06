"""Adapter Markdown/TXT: blocos por parágrafo, heading, lista e código (§8.1).

Regras estruturais implementadas:

- Bloco de código cercado (```` ``` ````/`~~~`) é preservado COM as cercas e
  com o texto interno intacto — nada de reflow, nada de escape.
- Um heading fecha o bloco anterior e passa a compor a `section` do
  localizador; a hierarquia inteira vai em `heading_path`.
- Frontmatter YAML mínimo (`---` … `---`) vira metadata, e passa pela whitelist
  de `normalize`: `initiative_id: X` é dado; `command: rm -rf` é `metadata_extra`
  inerte com diagnóstico.
"""

from __future__ import annotations

import os
import re
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
    decode_text,
    make_block,
    preserve,
    version_label,
)

NAME = "markdown_txt"
EXTENSIONS = frozenset({".md", ".markdown", ".mdown", ".txt", ".text", ""})

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT = re.compile(r"^(=+|-{2,})\s*$")
_FENCE = re.compile(r"^\s*(```+|~~~+)(.*)$")
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+\S")
_FRONTMATTER = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", re.DOTALL)
#: Bytes de controle que denunciam binário disfarçado de texto.
_BINARY_BYTES = bytes(range(0, 9)) + bytes([11, 12]) + bytes(range(14, 32))


def detect(path: str, head_bytes: bytes) -> bool:
    """Extensão textual conhecida, ou bytes que não parecem binários.

    É o último elo da cadeia do `registry`, então aceita texto sem extensão —
    mas recusa qualquer coisa com bytes de controle ou NUL, que seria ingerida
    como lixo em vez de falhar explicitamente.
    """
    ext = os.path.splitext(path)[1].lower()
    if head_bytes[:1] == b"\x00" or b"\x00" in head_bytes[:1024]:
        return False
    sample = head_bytes[:2048]
    control = sum(1 for b in sample if b in _BINARY_BYTES)
    if sample and control / len(sample) > 0.02:
        return False
    if ext in EXTENSIONS:
        return True
    # Sem extensão conhecida: só aceita se decodifica como UTF-8.
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return bool(sample.strip())


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, list[Diagnostic]]:
    """YAML mínimo: `chave: valor`, `chave: [a, b]` e listas com `- item`.

    Deliberadamente não é um parser YAML: aninhamento e tags são reportados
    como não interpretados em vez de virarem estrutura adivinhada.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text, []
    body = match.group(1)
    rest = text[match.end():]
    data: dict[str, Any] = {}
    diags: list[Diagnostic] = []
    current_key: str | None = None
    for raw_line in body.split("\n"):
        line = raw_line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and current_key:
            data.setdefault(current_key, [])
            if isinstance(data[current_key], list):
                data[current_key].append(_scalar(stripped[2:]))
            continue
        if ":" not in stripped:
            diags.append(
                Diagnostic(
                    code="frontmatter.unparsed_line",
                    severity=Severity.INFO,
                    message=f"linha de frontmatter não interpretada: {stripped[:120]!r}",
                    unavailable=(f"frontmatter: {stripped[:80]}",),
                )
            )
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if line[:1] in (" ", "\t"):
            diags.append(
                Diagnostic(
                    code="frontmatter.nested_ignored",
                    severity=Severity.INFO,
                    message=f"frontmatter aninhado não interpretado em '{key}'",
                    unavailable=(f"frontmatter aninhado: {key}",),
                )
            )
            continue
        current_key = key
        if not value:
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            data[key] = [_scalar(v) for v in value[1:-1].split(",") if v.strip()]
        else:
            data[key] = _scalar(value)
    return data, rest, diags


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _section(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)


def _flush_paragraph(
    lines: list[str],
    blocks: list[Block],
    stack: list[tuple[int, str]],
    version: str,
    paragraph_no: list[int],
) -> None:
    text = "\n".join(lines).strip()
    lines.clear()
    if not text:
        return
    kind = BlockKind.LIST if _LIST_ITEM.match(text.split("\n", 1)[0]) else BlockKind.PARAGRAPH
    paragraph_no[0] += 1
    blocks.append(
        make_block(
            len(blocks),
            kind,
            text,
            content_kind=ContentKind.MARKDOWN,
            version=version,
            section=_section(stack),
            paragraph=paragraph_no[0],
            heading_path=[t for _, t in stack],
        )
    )


def parse_blocks(text: str, version: str) -> list[Block]:
    """Divide o texto em blocos preservados, na ordem original."""
    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    paragraph_no = [0]
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            fenced = [line]
            i += 1
            while i < len(lines):
                inner = lines[i].rstrip("\r")
                fenced.append(inner)
                if inner.strip().startswith(marker[0] * len(marker)):
                    i += 1
                    break
                i += 1
            _flush_paragraph(buffer, blocks, stack, version, paragraph_no)
            paragraph_no[0] += 1
            blocks.append(
                make_block(
                    len(blocks),
                    BlockKind.CODE,
                    "\n".join(fenced),
                    content_kind=ContentKind.MARKDOWN,
                    version=version,
                    section=_section(stack),
                    paragraph=paragraph_no[0],
                    heading_path=[t for _, t in stack],
                )
            )
            continue
        heading = _HEADING.match(line)
        setext = (
            _SETEXT.match(line)
            and buffer
            and len(buffer) == 1
            and buffer[0].strip()
            and not _LIST_ITEM.match(buffer[0])
        )
        if heading or setext:
            if setext:
                title = buffer[0].strip()
                level = 1 if line.strip().startswith("=") else 2
                buffer.clear()
            else:
                level = len(heading.group(1))
                title = heading.group(2).strip()
                _flush_paragraph(buffer, blocks, stack, version, paragraph_no)
            while stack and stack[-1][0] >= level:
                stack.pop()
            paragraph_no[0] += 1
            blocks.append(
                make_block(
                    len(blocks),
                    BlockKind.HEADING,
                    title,
                    content_kind=ContentKind.MARKDOWN,
                    version=version,
                    section=_section(stack + [(level, title)]),
                    paragraph=paragraph_no[0],
                    heading_path=[t for _, t in stack] + [title],
                )
            )
            stack.append((level, title))
            i += 1
            continue
        if not line.strip():
            _flush_paragraph(buffer, blocks, stack, version, paragraph_no)
            i += 1
            continue
        # Uma linha de lista fecha o parágrafo em prosa que a antecedia, e
        # vice-versa: bloco de lista não engole o parágrafo anterior.
        if buffer:
            was_list = bool(_LIST_ITEM.match(buffer[0]))
            is_list = bool(_LIST_ITEM.match(line))
            if was_list != is_list:
                _flush_paragraph(buffer, blocks, stack, version, paragraph_no)
        buffer.append(line)
        i += 1
    _flush_paragraph(buffer, blocks, stack, version, paragraph_no)
    return blocks


def extract(path: str, preserved: Preserved | None = None) -> SourceDocument:
    """MD/TXT → `SourceDocument` com blocos e frontmatter como metadata."""
    pres = preserved or preserve(path)
    text, diags = decode_text(pres.raw)
    metadata, body, fm_diags = parse_frontmatter(text)
    diags.extend(fm_diags)
    version = version_label(pres.bytes_sha256)
    blocks = parse_blocks(body, version)
    ext = os.path.splitext(path)[1].lower()
    kind = "markdown" if ext in (".md", ".markdown", ".mdown") else "text"
    if not blocks:
        diags.append(
            Diagnostic(
                code="text.empty",
                severity=Severity.ERROR,
                message="nenhum bloco textual encontrado na fonte",
                unavailable=("conteúdo textual",),
                path=pres.path_original,
            )
        )
    return build_document(
        pres,
        kind=kind,
        adapter=NAME,
        blocks=blocks,
        raw_metadata=metadata,
        diagnostics=diags,
    )
