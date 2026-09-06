"""Adapters de fonte: um módulo por formato (plano §8.1, §13, onda W5).

Todo adapter expõe a mesma interface:

    detect(path: str, head_bytes: bytes) -> bool
    extract(path: str, preserved: Preserved | None = None) -> SourceDocument

`detect` decide pelos bytes iniciais e pela extensão, sem abrir o arquivo
inteiro; `extract` recebe o `Preserved` já hasheado pelo `registry`, de modo que
a preservação aconteça uma única vez e SEMPRE antes da extração (§8.1).

Nenhum adapter levanta exceção por conteúdo ruim: formato irreconhecível é
`unsupported`, conteúdo ilegível é `extraction_failed`, e ambos carregam
`diagnostics` com o que ficou indisponível.
"""

from . import (  # noqa: F401
    directory,
    docx_adapter,
    html_adapter,
    json_xml,
    markdown_txt,
    pdf_adapter,
    registry,
    transcript,
)

__all__ = [
    "directory",
    "docx_adapter",
    "html_adapter",
    "json_xml",
    "markdown_txt",
    "pdf_adapter",
    "registry",
    "transcript",
]
