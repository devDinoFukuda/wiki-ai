"""Adapter PDF TEXTUAL, com parsing binário próprio e falha explícita (§8.1).

O que este adapter consegue fazer: ler operadores de texto (`Tj`, `TJ`, `'`,
`"`) de streams de conteúdo NÃO comprimidos, mapeá-los para páginas via os
objetos `/Type /Page` e devolver blocos por parágrafo com o número da página.

O que ele deliberadamente NÃO faz, e por quê:

- Não descomprime `/FlateDecode`, `/LZWDecode`, `/DCTDecode` nem qualquer
  outro filtro. Um PDF cujo conteúdo esteja comprimido sai como
  `extraction_failed` listando exatamente quais objetos ficaram indisponíveis.
  A alternativa — devolver o texto das partes legíveis com status `ingested` —
  é a "ingestão integral bem-sucedida falsa" que §8.1 proíbe.
- Não faz OCR. PDF sem operadores de texto e com XObject de imagem sai como
  `unsupported` com a nota "OCR não configurado" (§8.1).
- Não resolve `/Encoding` customizado nem CMaps. Fonte com `/ToUnicode` ou
  encoding não-padrão é declarada como indisponível: os bytes de um PDF com
  CMap embutido não são o texto que o leitor humano vê, e entregá-los como se
  fossem produziria evidência falsa.

`page` entra no localizador porque, dentro de um snapshot de bytes fixo, a
ordem dos objetos `/Type /Page` é estável — a condição que §5.4 impõe.
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
    failed_document,
    make_block,
    preserve,
    unsupported_document,
    version_label,
)

NAME = "pdf_adapter"
EXTENSIONS = frozenset({".pdf"})

_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj\b", re.DOTALL)
_STREAM = re.compile(rb"\bstream\r?\n?(.*?)\r?\n?\bendstream\b", re.DOTALL)
_FILTER = re.compile(rb"/Filter\s*(/[A-Za-z0-9]+|\[[^\]]*\])")
_TYPE_PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
_TYPE_PAGES = re.compile(rb"/Type\s*/Pages\b")
_CONTENTS = re.compile(rb"/Contents\s*(\[[^\]]*\]|\d+\s+\d+\s+R)")
_REF = re.compile(rb"(\d+)\s+\d+\s+R")
_SUBTYPE_IMAGE = re.compile(rb"/Subtype\s*/Image\b")
_TOUNICODE = re.compile(rb"/ToUnicode\b")
_ENCRYPT = re.compile(rb"/Encrypt\b")
_FONT_ENCODING = re.compile(rb"/Encoding\s*/([A-Za-z0-9]+)")
#: Encodings de fonte que mapeiam byte→caractere de forma direta o bastante.
_SAFE_ENCODINGS = frozenset({b"WinAnsiEncoding", b"MacRomanEncoding", b"StandardEncoding"})

#: `(texto) Tj`, `(texto) '`, `(texto) "` e `[ (a) -250 (b) ] TJ`.
_TEXT_OP = re.compile(rb"(\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\)|\[[^\]]*\])\s*(TJ|Tj|'|\")")
_STRING_IN_ARRAY = re.compile(rb"\((?:[^()\\]|\\.)*\)")
_TD = re.compile(rb"\b(TD|Td|T\*)\b")
_BT = re.compile(rb"\bBT\b")

_ESCAPES = {
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"b": b"\b",
    b"f": b"\f",
    b"(": b"(",
    b")": b")",
    b"\\": b"\\",
}


def detect(path: str, head_bytes: bytes) -> bool:
    """Assinatura `%PDF-`. Alguns geradores deixam lixo antes do cabeçalho."""
    if head_bytes.startswith(b"%PDF-"):
        return True
    return b"%PDF-" in head_bytes[:1024] and os.path.splitext(path)[1].lower() == ".pdf"


def unescape_pdf_string(raw: bytes) -> str:
    """Decodifica uma literal `(...)` de PDF, incluindo octais `\\ddd`."""
    out = bytearray()
    i = 0
    body = raw[1:-1] if raw[:1] == b"(" and raw[-1:] == b")" else raw
    while i < len(body):
        char = body[i : i + 1]
        if char != b"\\":
            out += char
            i += 1
            continue
        nxt = body[i + 1 : i + 2]
        if not nxt:
            break
        if nxt in _ESCAPES:
            out += _ESCAPES[nxt]
            i += 2
            continue
        if nxt == b"\n":  # continuação de linha
            i += 2
            continue
        if nxt.isdigit():
            digits = b""
            j = i + 1
            while j < len(body) and len(digits) < 3 and body[j : j + 1].isdigit():
                digits += body[j : j + 1]
                j += 1
            out.append(int(digits, 8) & 0xFF)
            i = j
            continue
        out += nxt
        i += 2
    if out[:2] in (b"\xfe\xff",):  # UTF-16BE com BOM
        return out[2:].decode("utf-16-be", errors="replace")
    return out.decode("latin-1")


def stream_text(payload: bytes) -> list[str]:
    """Texto de um stream de conteúdo, agrupado por bloco `BT`/posicionamento."""
    lines: list[str] = []
    current: list[str] = []
    position = 0
    for match in _TEXT_OP.finditer(payload):
        between = payload[position : match.start()]
        position = match.end()
        if _BT.search(between) or _TD.search(between):
            if current:
                lines.append("".join(current))
                current = []
        operand, operator = match.group(1), match.group(2)
        if operand.startswith(b"["):
            piece = "".join(
                unescape_pdf_string(s) for s in _STRING_IN_ARRAY.findall(operand)
            )
        else:
            piece = unescape_pdf_string(operand)
        if operator in (b"'", b'"'):
            if current:
                lines.append("".join(current))
                current = []
        current.append(piece)
    if current:
        lines.append("".join(current))
    return [line for line in (l.strip() for l in lines) if line]


def parse_objects(raw: bytes) -> dict[int, tuple[bytes, bytes | None]]:
    """`{numero: (dicionario_bruto, stream_bruto_ou_None)}`."""
    objects: dict[int, tuple[bytes, bytes | None]] = {}
    for match in _OBJ.finditer(raw):
        number = int(match.group(1))
        body = match.group(3)
        stream = _STREAM.search(body)
        if stream:
            objects[number] = (body[: stream.start()], stream.group(1))
        else:
            objects[number] = (body, None)
    return objects


def page_map(objects: dict[int, tuple[bytes, bytes | None]]) -> dict[int, int]:
    """`{numero_do_objeto_de_conteudo: pagina_1based}` na ordem dos objetos."""
    pages = [
        number
        for number, (header, _) in sorted(objects.items())
        if _TYPE_PAGE.search(header) and not _TYPE_PAGES.search(header)
    ]
    mapping: dict[int, int] = {}
    for index, page_obj in enumerate(pages, start=1):
        header, _ = objects[page_obj]
        contents = _CONTENTS.search(header)
        if not contents:
            continue
        for ref in _REF.finditer(contents.group(1)):
            mapping[int(ref.group(1))] = index
    return mapping


def _filters_of(header: bytes) -> list[str]:
    found = _FILTER.search(header)
    if not found:
        return []
    blob = found.group(1)
    return [f.decode("latin-1") for f in re.findall(rb"/([A-Za-z0-9]+)", blob)]


def _split_paragraphs(lines: list[str]) -> list[str]:
    """Junta linhas contíguas em parágrafos; linha curta terminada em `.` fecha."""
    paragraphs: list[str] = []
    buffer: list[str] = []
    for line in lines:
        buffer.append(line)
        if line.endswith((".", "!", "?", ":")) or len(line) < 40:
            paragraphs.append(" ".join(buffer).strip())
            buffer = []
    if buffer:
        paragraphs.append(" ".join(buffer).strip())
    return [p for p in paragraphs if p]


def extract(path: str, preserved: Preserved | None = None) -> SourceDocument:
    """PDF textual → blocos; compressão/encoding não suportado → falha explícita."""
    pres = preserved or preserve(path)
    raw = pres.raw
    version = version_label(pres.bytes_sha256)
    diags: list[Diagnostic] = []

    if _ENCRYPT.search(raw):
        return unsupported_document(
            pres,
            kind="pdf",
            adapter=NAME,
            reason="PDF cifrado (/Encrypt); este adapter não decifra conteúdo",
            unavailable=("todo o conteúdo do documento (PDF cifrado)",),
        )

    objects = parse_objects(raw)
    if not objects:
        return failed_document(
            pres,
            kind="pdf",
            adapter=NAME,
            reason="nenhum objeto PDF (`N 0 obj … endobj`) encontrado nos bytes",
            unavailable=("todo o conteúdo do documento",),
        )

    contents_page = page_map(objects)
    has_image = any(_SUBTYPE_IMAGE.search(header) for header, _ in objects.values())
    unsupported_encodings: list[str] = []
    for number, (header, _) in sorted(objects.items()):
        if _TOUNICODE.search(header):
            unsupported_encodings.append(f"objeto {number}: fonte com CMap /ToUnicode")
            continue
        encoding = _FONT_ENCODING.search(header)
        if encoding and encoding.group(1) not in _SAFE_ENCODINGS:
            unsupported_encodings.append(
                f"objeto {number}: /Encoding /{encoding.group(1).decode('latin-1')}"
            )

    blocked_streams: list[str] = []
    blocks: list[Block] = []
    read_streams = 0
    for number, (header, stream) in sorted(objects.items()):
        if stream is None:
            continue
        if _SUBTYPE_IMAGE.search(header):
            continue
        filters = _filters_of(header)
        if filters:
            blocked_streams.append(
                f"objeto {number}: stream com filtro {'/' + ', /'.join(filters)}"
            )
            continue
        if not (_TEXT_OP.search(stream) or _BT.search(stream)):
            continue
        read_streams += 1
        page = contents_page.get(number)
        lines = stream_text(stream)
        for paragraph_no, text in enumerate(_split_paragraphs(lines), start=1):
            blocks.append(
                make_block(
                    len(blocks),
                    BlockKind.PARAGRAPH,
                    text,
                    content_kind=ContentKind.PROSE,
                    version=version,
                    section=f"página {page}" if page else f"objeto {number}",
                    paragraph=paragraph_no,
                    page=page,
                )
            )

    if unsupported_encodings:
        diags.append(
            Diagnostic(
                code="pdf.unsupported_encoding",
                severity=Severity.ERROR,
                message=(
                    "fontes com encoding/CMap não suportado; os bytes do stream não "
                    "correspondem ao texto exibido e não foram convertidos"
                ),
                unavailable=tuple(unsupported_encodings),
                path=pres.path_original,
            )
        )
    if blocked_streams:
        diags.append(
            Diagnostic(
                code="pdf.compressed_stream",
                severity=Severity.ERROR,
                message=(
                    "streams comprimidos não são descomprimidos por este adapter; "
                    "o texto contido neles não foi extraído"
                ),
                unavailable=tuple(blocked_streams),
                path=pres.path_original,
            )
        )

    if blocked_streams or unsupported_encodings:
        # §8.1: compressão/encoding não suportado é extraction_failed, mesmo
        # tendo recuperado algum texto — o que saiu não é o documento inteiro.
        return failed_document(
            pres,
            kind="pdf",
            adapter=NAME,
            reason=(
                f"extração parcial: {len(blocks)} bloco(s) recuperado(s) de streams legíveis, "
                f"{len(blocked_streams)} stream(s) comprimido(s) e "
                f"{len(unsupported_encodings)} fonte(s) com encoding não suportado ficaram de fora"
            ),
            unavailable=tuple(blocked_streams + unsupported_encodings),
            blocks=blocks,
            extra_diagnostics=diags,
        )

    if not blocks:
        if has_image:
            return unsupported_document(
                pres,
                kind="pdf",
                adapter=NAME,
                reason=(
                    "PDF sem operadores de texto e com XObject de imagem: é PDF imagem. "
                    "OCR não configurado"
                ),
                unavailable=("todo o conteúdo textual (exige OCR, não configurado)",),
            )
        return failed_document(
            pres,
            kind="pdf",
            adapter=NAME,
            reason="nenhum operador de texto (Tj/TJ) encontrado em streams legíveis",
            unavailable=("todo o conteúdo textual do documento",),
        )

    if has_image:
        diags.append(
            Diagnostic(
                code="pdf.image_not_read",
                severity=Severity.WARNING,
                message="o documento contém imagens cujo conteúdo não foi lido; OCR não configurado",
                unavailable=("conteúdo das imagens do documento (OCR não configurado)",),
                path=pres.path_original,
            )
        )
    metadata: dict[str, Any] = {"source_type": "document"}
    return build_document(
        pres,
        kind="pdf",
        adapter=NAME,
        blocks=blocks,
        raw_metadata=metadata,
        diagnostics=diags,
    )
