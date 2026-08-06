"""Chunking.

Duas estratégias, porque são corpora diferentes:

  wiki/  -> corte por heading. A wiki é ESCRITA pelo compile: a seção já é a
            unidade coerente de sentido. Fragmentá-la em 512 tokens desfaz o
            que o compile pagou para sintetizar. Cada chunk recebe o caminho
            "titulo > heading" como prefixo — Contextual Retrieval de graça.

  raw/   -> recursive character splitting, 512 tokens, 15% overlap. É o default
            de mercado (69% accuracy end-to-end vs 54% do semantic chunking no
            benchmark Vecta/FloTorch fev/2026) e raw/ é bagunçado: transcrição
            não tem heading, então o recursive desce para parágrafo e linha.

Code fence nunca é cortado no meio, nas duas estratégias.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .tokens import count

# Hierarquia de separadores do recursive split. A ordem é a decisão:
# tenta heading, depois parágrafo, depois linha, depois frase, depois palavra.
SEPARATORS = ["\n## ", "\n### ", "\n#### ", "\n\n", "\n", ". ", " "]

RAW_TARGET = 512
RAW_OVERLAP_PCT = 0.15
WIKI_MAX = 800  # seção maior que isso é sintoma de compile ruim, mas não quebra

_FENCE = re.compile(r"^\s*(```|~~~)")
_H2 = re.compile(r"^##\s+(.+?)\s*$")
_H1 = re.compile(r"^#\s+(.+?)\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_CHARS = re.compile(r"^[\s:|-]+$")


def _is_table_sep(line: str) -> bool:
    """Linha separadora de tabela: só `|`, `-`, `:`, espaço, e ao menos um `-`."""
    return "-" in line and bool(_TABLE_SEP_CHARS.match(line))


@dataclass
class Chunk:
    ord: int
    heading: str
    text: str
    token_count: int


def _fence_mask(lines: list[str]) -> list[bool]:
    """True nas linhas que estão dentro de um bloco de código."""
    inside = []
    open_fence = False
    for ln in lines:
        is_fence = bool(_FENCE.match(ln))
        if is_fence:
            # a própria linha da cerca pertence ao bloco
            inside.append(True)
            open_fence = not open_fence
            continue
        inside.append(open_fence)
    return inside


def doc_title(text: str) -> str:
    lines = text.splitlines()
    mask = _fence_mask(lines)
    for ln, in_code in zip(lines, mask):
        if in_code:
            continue
        m = _H1.match(ln)
        if m:
            return m.group(1)
    return ""


def _table_mask(lines: list[str], code_mask: list[bool]) -> list[bool]:
    """True nas linhas de uma tabela markdown (fora de code fence).

    Tabela = linha de cabeçalho + linha separadora (`|---|`) + corpo. Uma tabela
    partida em 512 tokens deixa linhas órfãs sem cabeçalho, inúteis na busca; por
    isso é bloco atômico, como code fence.
    """
    inside = [False] * len(lines)
    i = 0
    n = len(lines)
    while i < n:
        if not code_mask[i] and _TABLE_ROW.match(lines[i]) and i + 1 < n \
                and not code_mask[i + 1] and _is_table_sep(lines[i + 1]):
            j = i
            while j < n and not code_mask[j] and _TABLE_ROW.match(lines[j]):
                inside[j] = True
                j += 1
            i = j
        else:
            i += 1
    return inside


def _split_code_atoms(text: str) -> list[str]:
    """Quebra em átomos: code fences e tabelas markdown são unidades atômicas."""
    lines = text.splitlines(keepends=True)
    bare = [ln.rstrip("\n") for ln in lines]
    code_mask = _fence_mask(bare)
    table_mask = _table_mask(bare, code_mask)
    # 'atomic' = está num bloco que não pode ser partido (código ou tabela).
    atomic = [c or t for c, t in zip(code_mask, table_mask)]
    atoms: list[str] = []
    buf: list[str] = []
    cur_atomic = False
    for ln, is_atomic in zip(lines, atomic):
        if is_atomic != cur_atomic:
            if buf:
                atoms.append("".join(buf))
            buf = [ln]
            cur_atomic = is_atomic
        else:
            buf.append(ln)
    if buf:
        atoms.append("".join(buf))
    return atoms


def _recursive(text: str, seps: list[str], max_tokens: int) -> list[str]:
    if count(text) <= max_tokens:
        return [text] if text else []
    if not seps:
        return _hard_split(text, max_tokens)
    sep, rest = seps[0], seps[1:]
    if sep not in text:
        return _recursive(text, rest, max_tokens)
    parts = text.split(sep)
    out: list[str] = []
    for i, p in enumerate(parts):
        piece = p if i == 0 else sep + p
        if not piece:
            continue
        if count(piece) <= max_tokens:
            out.append(piece)
        else:
            out.extend(_recursive(piece, rest, max_tokens))
    return out


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """Último recurso: corte por caractere. Só chega aqui palavra gigante."""
    approx = max(1, int(max_tokens * 3.6))
    return [text[i : i + approx] for i in range(0, len(text), approx)]


def _merge(pieces: list[str], target: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for p in pieces:
        t = count(p)
        if cur and cur_tok + t > target:
            chunks.append("".join(cur))
            tail: list[str] = []
            tail_tok = 0
            for q in reversed(cur):
                qt = count(q)
                if tail_tok + qt > overlap:
                    break
                tail.insert(0, q)
                tail_tok += qt
            cur, cur_tok = tail, tail_tok
        cur.append(p)
        cur_tok += t
    if cur:
        chunks.append("".join(cur))
    return [c for c in chunks if c.strip()]


def _is_atomic(atom: str) -> bool:
    """Átomo indivisível: começa com code fence ou é uma tabela markdown."""
    stripped = atom.lstrip("\n")
    if _FENCE.match(stripped):
        return True
    lines = atom.strip().splitlines()
    return (
        len(lines) >= 2
        and bool(_TABLE_ROW.match(lines[0]))
        and _is_table_sep(lines[1])
    )


def chunk_raw(text: str, target: int = RAW_TARGET) -> list[Chunk]:
    """Recursive splitting para fontes brutas."""
    overlap = int(target * RAW_OVERLAP_PCT)
    pieces: list[str] = []
    for atom in _split_code_atoms(text):
        if _is_atomic(atom):  # code fence ou tabela: não parte
            pieces.append(atom)
        else:
            pieces.extend(_recursive(atom, SEPARATORS, target))
    merged = _merge(pieces, target, overlap)
    return [
        Chunk(ord=i, heading="", text=c.strip(), token_count=count(c))
        for i, c in enumerate(merged)
    ]


def chunk_wiki(text: str, max_tokens: int = WIKI_MAX) -> list[Chunk]:
    """Corte por heading H2. Cada chunk carrega 'titulo > heading' como prefixo."""
    title = doc_title(text)
    lines = text.splitlines()
    mask = _fence_mask(lines)

    sections: list[tuple[str, list[str]]] = []
    cur_head = ""
    cur: list[str] = []
    for ln, in_code in zip(lines, mask):
        m = None if in_code else _H2.match(ln)
        if m:
            if cur_head or any(x.strip() for x in cur):
                sections.append((cur_head, cur))
            cur_head = m.group(1)
            cur = []
        else:
            cur.append(ln)
    if cur_head or any(x.strip() for x in cur):
        sections.append((cur_head, cur))

    out: list[Chunk] = []
    n = 0
    for head, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not head:
            # preâmbulo: o H1 já vive no prefixo de todo chunk. Sozinho é ruído.
            body = "\n".join(
                ln for ln in body.splitlines() if not _H1.match(ln)
            ).strip()
        if not body and not head:
            continue
        path = " > ".join(x for x in (title, head) if x)
        prefix = f"[{path}]\n\n" if path else ""
        full = f"{prefix}## {head}\n\n{body}" if head else f"{prefix}{body}"
        if count(full) <= max_tokens:
            out.append(Chunk(n, path, full.strip(), count(full)))
            n += 1
            continue
        # seção grande: recursive dentro dela, mantendo o prefixo em cada pedaço
        for piece in chunk_raw(body, target=RAW_TARGET):
            t = f"{prefix}## {head}\n\n{piece.text}" if head else f"{prefix}{piece.text}"
            out.append(Chunk(n, path, t.strip(), count(t)))
            n += 1
    return out


def chunk(text: str, collection: str) -> list[Chunk]:
    return chunk_wiki(text) if collection == "wiki" else chunk_raw(text)
