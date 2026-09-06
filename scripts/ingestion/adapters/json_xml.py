"""Adapter JSON e XML/XMI (§8.1).

JSON: um bloco por chave de topo. O valor estruturado é preservado intacto em
`Block.data`; `Block.text` carrega a forma canônica (`chave: {json}`) para que o
bloco continue citável como texto. Achatar o objeto em prosa perderia a
estrutura, que é justamente o que distingue esta fonte de um documento.

XML/XMI: parseado com `_xmlsafe.parse_defused` — DOCTYPE, entidade declarada e
referência externa são recusados. Elementos relevantes são os que têm texto
próprio ou atributos; um contêiner puramente estrutural não vira bloco vazio.
A `section` do localizador é o caminho de tags até o elemento, o que mantém o
bloco resolvível dentro do documento.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..normalize import (
    Block,
    BlockKind,
    ContentKind,
    Preserved,
    SourceDocument,
    build_document,
    decode_text,
    failed_document,
    make_block,
    preserve,
    version_label,
)
from ._xmlsafe import XmlMalformed, XmlNotAllowed, local_name, parse_defused

NAME = "json_xml"
EXTENSIONS = frozenset({".json", ".jsonl", ".xml", ".xmi", ".xsd", ".wsdl", ".rels"})

#: Chaves de topo de um JSON que costumam trazer metadados de iniciativa.
_META_KEYS = frozenset({"initiative_id", "phase", "participants", "date", "title", "source_type"})
_MAX_TEXT = 20000


def detect(path: str, head_bytes: bytes) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in EXTENSIONS:
        return True
    if ext:
        return False
    head = head_bytes[:512].lstrip()
    if head.startswith(b"<?xml"):
        return True
    return head[:1] in (b"{", b"[")


def _is_xml(path: str, text: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xml", ".xmi", ".xsd", ".wsdl", ".rels"):
        return True
    if ext in (".json", ".jsonl"):
        return False
    return text.lstrip()[:5] == "<?xml" or text.lstrip()[:1] == "<"


def _canonical(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_blocks(payload: Any, version: str) -> list[Block]:
    blocks: list[Block] = []
    if isinstance(payload, dict):
        items = list(payload.items())
    elif isinstance(payload, list):
        items = [(f"[{i}]", v) for i, v in enumerate(payload)]
    else:
        items = [("(raiz)", payload)]
    for key, value in items:
        text = f"{key}: {_canonical(value)}"[:_MAX_TEXT]
        blocks.append(
            make_block(
                len(blocks),
                BlockKind.PARAGRAPH,
                text,
                content_kind=ContentKind.PROSE,
                data={"key": key, "value": value},
                version=version,
                section=str(key),
                paragraph=len(blocks) + 1,
                heading_path=[str(key)],
            )
        )
    return blocks


def _xml_blocks(root, version: str) -> list[Block]:
    blocks: list[Block] = []

    def walk(element, path: list[str]) -> None:
        name = local_name(element.tag)
        here = path + [name]
        own_text = (element.text or "").strip()
        attrs = {local_name(k): v for k, v in element.attrib.items()}
        relevant = bool(own_text) or bool(attrs)
        if relevant:
            pieces = [own_text] if own_text else []
            if attrs:
                pieces.append(
                    " ".join(f"{k}={v!r}" for k, v in sorted(attrs.items()))
                )
            text = " ".join(p for p in pieces if p)[:_MAX_TEXT]
            blocks.append(
                make_block(
                    len(blocks),
                    BlockKind.PARAGRAPH,
                    text,
                    content_kind=ContentKind.PROSE,
                    data={"tag": name, "attrib": attrs, "text": own_text},
                    version=version,
                    section="/".join(here),
                    paragraph=len(blocks) + 1,
                    heading_path=here,
                )
            )
        for child in element:
            walk(child, here)
            tail = (child.tail or "").strip()
            if tail:
                blocks.append(
                    make_block(
                        len(blocks),
                        BlockKind.PARAGRAPH,
                        tail[:_MAX_TEXT],
                        content_kind=ContentKind.PROSE,
                        version=version,
                        section="/".join(here),
                        paragraph=len(blocks) + 1,
                        heading_path=here,
                    )
                )

    walk(root, [])
    return blocks


def extract(path: str, preserved: Preserved | None = None) -> SourceDocument:
    """JSON/XML/XMI → `SourceDocument` com estrutura preservada."""
    pres = preserved or preserve(path)
    text, diags = decode_text(pres.raw)
    version = version_label(pres.bytes_sha256)

    if _is_xml(path, text):
        try:
            root = parse_defused(pres.raw)
        except XmlNotAllowed as exc:
            return failed_document(
                pres,
                kind="xml",
                adapter=NAME,
                reason=f"XML recusado por segurança: {exc}",
                unavailable=("todo o conteúdo do documento XML",),
                extra_diagnostics=diags,
            )
        except XmlMalformed as exc:
            return failed_document(
                pres,
                kind="xml",
                adapter=NAME,
                reason=f"XML malformado: {exc}",
                unavailable=("todo o conteúdo do documento XML",),
                extra_diagnostics=diags,
            )
        blocks = _xml_blocks(root, version)
        raw_metadata: dict[str, Any] = {}
        for key, value in root.attrib.items():
            raw_metadata[local_name(key)] = value
        if not blocks:
            return failed_document(
                pres,
                kind="xml",
                adapter=NAME,
                reason="nenhum elemento com texto ou atributos no XML",
                unavailable=("conteúdo do documento XML",),
                extra_diagnostics=diags,
            )
        return build_document(
            pres,
            kind="xmi" if path.lower().endswith(".xmi") else "xml",
            adapter=NAME,
            blocks=blocks,
            raw_metadata=raw_metadata,
            diagnostics=diags,
        )

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return failed_document(
            pres,
            kind="json",
            adapter=NAME,
            reason=f"JSON inválido em linha {exc.lineno}, coluna {exc.colno}: {exc.msg}",
            unavailable=("todo o conteúdo do documento JSON",),
            extra_diagnostics=diags,
        )
    blocks = _json_blocks(payload, version)
    raw_metadata = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in _META_KEYS and isinstance(value, (str, int, float, bool, list)):
                raw_metadata[str(key).lower()] = value
    if not blocks:
        return failed_document(
            pres,
            kind="json",
            adapter=NAME,
            reason="documento JSON vazio",
            unavailable=("conteúdo do documento JSON",),
            extra_diagnostics=diags,
        )
    return build_document(
        pres,
        kind="json",
        adapter=NAME,
        blocks=blocks,
        raw_metadata=raw_metadata,
        diagnostics=diags,
    )
