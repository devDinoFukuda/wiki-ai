"""Leitura do frontmatter de proveniência.

Estes campos viram COLUNAS do índice, não texto. É a diferença central em
relação ao QMD: lá `source_type` é string casada por BM25 (probabilístico);
aqui é `WHERE source_type = 'agent-output'` (exato).
"""

from __future__ import annotations

import re
from typing import Any

# Tolerante a BOM (\ufeff) e a comentários/linhas em branco antes do `---`.
# O `---` deve aparecer dentro das primeiras MAX_PREAMBLE_LINES linhas, senão é
# tratado como corpo — evita falso positivo com separador horizontal (`---`) no
# meio de um documento markdown comum.
MAX_PREAMBLE_LINES = 8
_FM_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)

# Campos do schema promovidos a coluna.
FIELDS = (
    "id",
    "source_type",
    "origin",
    "captured_at",
    "promoted",
    "promoted_by",
    "promoted_at",
    "confidence",
    "supersedes",
    "source_link",
    "topic",
    "sources",   # ids das fontes que alimentaram a página (wiki). Habilita L1/L5.
)

VALID_SOURCE_TYPES = {
    "human-transcript",
    "human-doc",
    "code-repo",
    "agent-output",
    "web-clip",
}


def _coerce(v: Any) -> Any:
    if isinstance(v, bool):
        return 1 if v else 0
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().strip("\"'")
    if s == "":
        return None
    low = s.lower()
    if low in ("true", "yes"):
        return 1
    if low in ("false", "no"):
        return 0
    return s


def _strip_inline_comment(value: str) -> str:
    """Remove comentario inline YAML: `#` precedido de espaco, respeitando aspas.

    YAML: `#` inicia comentario só se precedido de whitespace (ou no inicio).
    Dentro de aspas simples ou duplas, `#` é literal. Sem isto, o template
    distribuído (com comentarios instrutivos ao lado de cada campo) produz
    valores como 'human-transcript | human-doc | ...' em vez de 'human-transcript',
    e o audit dá falso positivo de L2 (source_type invalido).
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or value[i - 1] in (" ", "\t"):
                return value[:i].rstrip()
    return value.rstrip()


def _parse_minimal(block: str) -> dict:
    """Parser de `chave: valor` para quando pyyaml não existe.

    Descasca comentarios inline (`# ...` ao final da linha) respeitando aspas,
    para que o template distribuído funcione mesmo sem pyyaml instalado.
    """
    out: dict = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t"):  # aninhado: fora do escopo
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = _strip_inline_comment(v.strip())
    return out


def _strip_preamble(text: str) -> str:
    """Descasca BOM e pula comentários/blank lines antes do `---` do frontmatter.

    YAML frontmatter deve começar com `---` no topo, mas editores no Windows
    (Notepad, PowerShell 5.1 Set-Content -Encoding UTF8) adicionam BOM, e o
    template distribuído vem com comentários instrutivos acima do `---`.
    Sem isto, `split` retorna {} e o audit dá falso positivo de L2.

    Limite de MAX_PREAMBLE_LINES: se o `---` não aparecer nas primeiras linhas,
    o arquivo é tratado como corpo puro (sem frontmatter) — evita casar com
    separador horizontal `---` no meio de um markdown comum.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    cut = 0
    for i, ln in enumerate(lines[:MAX_PREAMBLE_LINES]):
        stripped = ln.strip()
        if stripped == "---" or stripped.startswith("--- "):
            cut = i
            break
        if stripped and not stripped.startswith("#"):
            # primeira linha não-vazia e não-comentário que não é `---`:
            # não há frontmatter aqui.
            return text
    else:
        return text  # não achou `---` no preâmbulo: é corpo
    return "".join(lines[cut:]) if cut else text


def split(text: str) -> tuple[dict, str]:
    """Devolve (metadados, corpo-sem-frontmatter)."""
    text = _strip_preamble(text)
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    block = m.group(1)
    body = text[m.end() :]
    data: dict = {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(block)
        if isinstance(loaded, dict):
            data = loaded
        else:
            data = _parse_minimal(block)
    except Exception:
        data = _parse_minimal(block)
    meta = {k: _coerce(data.get(k)) for k in FIELDS if k != "sources"}
    raw_src = data.get("sources")
    if isinstance(raw_src, (list, tuple)):
        meta["sources"] = [str(x).strip() for x in raw_src if str(x).strip()]
    elif isinstance(raw_src, str):
        cleaned = raw_src.strip().strip("[]")
        meta["sources"] = [x.strip().strip("\"'") for x in cleaned.split(",") if x.strip()]
    else:
        meta["sources"] = []
    return meta, body


def provenance_gaps(meta: dict) -> list[str]:
    """Violações de L2 detectáveis sem LLM: proveniência ausente ou inválida."""
    gaps = []
    if not meta.get("origin"):
        gaps.append("origin ausente")
    st = meta.get("source_type")
    if not st:
        gaps.append("source_type ausente")
    elif st not in VALID_SOURCE_TYPES:
        gaps.append(f"source_type invalido: {st}")
    return gaps
