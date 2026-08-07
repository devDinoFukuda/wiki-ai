"""docxgen — Onda E.1: fachada que monta o .docx de uma fonte promovida.

Só orquestra A (docx_ooxml), B (docx_md) e C (docx_meta) — nenhuma lógica de
parsing/heurística/empacotamento é duplicada aqui. Contrato IR (Run/Block)
congelado em docs/plano-execucao-docx-ondas.md §0. Sequência e estrutura do
documento: mesmo arquivo, ONDA E (E.1); docs/plano-wiki-docx.md §3.

Público: build_document(source) -> (bytes, warnings)
"""

import re

from wk import docx_md, docx_meta, docx_ooxml

# Normalização tolerante para detectar H1 do corpo duplicando o título
# derivado (heading level=1 prependado por build_document). Mesmos marcadores
# inline removidos por docx_meta._INLINE_MD_RE, mas aplicados aqui ao texto
# já parseado (runs), não à linha markdown crua.
_INLINE_MD_RE = re.compile(r"\*\*|`|\*")
_WS_RE = re.compile(r"\s+")


def _normalize_heading_text(text: str) -> str:
    """strip + colapso de espaços + minúsculas + marcadores markdown removidos."""
    text = _INLINE_MD_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip().lower()

# Proveniência: (rótulo, chave em `source`), nesta ordem fixa (§3 do plano
# funcional / E.1). "fonte" não é uma chave literal de `source` — mapeia
# para source["rel"].
_PROVENANCE_FIELDS = (
    ("topic", "topic"),
    ("source_type", "source_type"),
    ("confidence", "confidence"),
    ("origin", "origin"),
    ("captured_at", "captured_at"),
    ("fonte", "rel"),
)

# Aviso literal exigido quando o resumo é placeholder de metadados (E.1 passo 7).
_PLACEHOLDER_WARNING = "resumo: placeholder de metadados (nenhum parágrafo próprio aprovado)"


def _run(text: str, bold: bool = False) -> dict:
    """Monta um Run (contrato §0.1) com as 5 chaves obrigatórias."""
    return {"text": text, "bold": bold, "italic": False, "code": False, "href": None}


def build_document(source: dict) -> tuple[bytes, list[str]]:
    """Monta o .docx de uma fonte promovida. Devolve (bytes, avisos)."""
    title = docx_meta.derive_title(source)
    summary, is_placeholder = docx_meta.derive_summary(source)
    body_blocks, warnings = docx_md.parse(source["body"])
    warnings = list(warnings) if warnings else []

    # SP4: o corpo markdown costuma já começar com "# Título" — se o primeiro
    # block for Heading1 e seu texto (normalizado) bater com o título derivado,
    # remove-o para não duplicar o heading prependado abaixo. Só remove em
    # caso de match; qualquer H1 diferente é preservado (fora de escopo mudar).
    if (
        body_blocks
        and body_blocks[0].get("kind") == "heading"
        and body_blocks[0].get("level") == 1
    ):
        first_text = "".join(r.get("text", "") for r in body_blocks[0].get("runs", []))
        if _normalize_heading_text(first_text) == _normalize_heading_text(title):
            body_blocks = body_blocks[1:]

    blocks = [
        {"kind": "heading", "level": 1, "runs": [_run(title)]},
        {"kind": "paragraph", "runs": [_run(summary)], "style": "Normal"},
    ]
    blocks.extend(body_blocks)
    blocks.append({"kind": "heading", "level": 2, "runs": [_run("Proveniência")]})

    # Um list_item por campo presente; ausente/vazio é omitido (nunca "None").
    for label, key in _PROVENANCE_FIELDS:
        value = source.get(key)
        if not value:
            continue
        runs = [_run(f"{label}", bold=True), _run(": "), _run(str(value))]
        blocks.append({"kind": "list_item", "ordered": False, "level": 0, "runs": runs})

    core = docx_meta.core_props(source, title, summary)
    data = docx_ooxml.build_package(blocks, core)

    if is_placeholder:
        warnings.append(_PLACEHOLDER_WARNING)

    return data, warnings
