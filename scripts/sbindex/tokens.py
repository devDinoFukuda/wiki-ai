"""Contagem de tokens.

tiktoken se disponível; senão, aproximação por caracteres.

Por que o fallback existe: tiktoken baixa o arquivo de encoding na primeira
execução. Em ambiente corporativo com egress restrito isso falha. O chunker
não pode depender de rede — 512 é alvo, não fronteira exata, e erro de ~10%
não muda o comportamento de recuperação.
"""

from __future__ import annotations

# Razão char/token para corpus misto pt-BR + código.
# Português gera mais tokens por caractere que inglês (~4.0) por causa de
# acentuação e sufixos. Medir no seu corpus com scripts/calibrate se importar.
CHARS_PER_TOKEN = 3.6

_encoder = None
_tried = False


def _get_encoder():
    global _encoder, _tried
    if _tried:
        return _encoder
    _tried = True
    try:
        import tiktoken  # type: ignore

        _encoder = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _encoder = None  # sem rede, sem tiktoken, sem drama
    return _encoder


def count(text: str) -> int:
    """Número aproximado de tokens em `text`."""
    enc = _get_encoder()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    return int(len(text) / CHARS_PER_TOKEN) + 1


def exact() -> bool:
    """True se a contagem é exata (tiktoken ativo)."""
    return _get_encoder() is not None
