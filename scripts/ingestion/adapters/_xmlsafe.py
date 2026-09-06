"""Parsing XML defusado, compartilhado por `docx_adapter` e `json_xml`.

`xml.etree.ElementTree` expande entidades internas e, em versões antigas do
expat, referencia entidades externas. Fonte de terceiros é dado hostil por
premissa (§13.1: "dados de fontes podem conter instruções maliciosas"), então
o parser aqui recusa DOCTYPE, declaração de entidade e referência externa —
levantando `XmlNotAllowed`, que o adapter converte em diagnóstico.

O parser é montado sobre `xml.parsers.expat` diretamente porque
`ET.XMLParser` deixou de expor o objeto expat subjacente (`.parser`) nas
versões recentes do CPython, o que tornaria os handlers de bloqueio
inalcançáveis. Nomes de elemento vêm qualificados (`w:p`), sem separador de
namespace, o que preserva o prefixo original do documento.
"""

from __future__ import annotations

import xml.parsers.expat as expat
from xml.etree.ElementTree import Element, TreeBuilder


class XmlNotAllowed(ValueError):
    """DOCTYPE, entidade declarada ou referência externa em fonte ingerida."""


class XmlMalformed(ValueError):
    """XML sintaticamente inválido."""


def parse_defused(data: bytes | str) -> Element:
    """Parseia XML bloqueando DTD, entidades e referências externas."""
    builder = TreeBuilder()
    parser = expat.ParserCreate()
    parser.buffer_text = True

    def _doctype(name, sysid, pubid, has_internal_subset):
        raise XmlNotAllowed(
            f"DOCTYPE '{name}' recusado: fonte ingerida não define DTD nem entidades"
        )

    def _entity_decl(*_args):
        raise XmlNotAllowed("declaração de entidade recusada em fonte ingerida")

    def _external(*_args):
        raise XmlNotAllowed("referência a entidade externa recusada em fonte ingerida")

    parser.StartDoctypeDeclHandler = _doctype
    parser.EntityDeclHandler = _entity_decl
    parser.UnparsedEntityDeclHandler = _entity_decl
    parser.ExternalEntityRefHandler = _external
    parser.StartElementHandler = lambda tag, attrs: builder.start(tag, attrs)
    parser.EndElementHandler = lambda tag: builder.end(tag)
    parser.CharacterDataHandler = builder.data

    payload = data.encode("utf-8") if isinstance(data, str) else data
    try:
        parser.Parse(payload, True)
    except XmlNotAllowed:
        raise
    except expat.ExpatError as exc:
        raise XmlMalformed(f"XML inválido: {exc}") from exc
    try:
        return builder.close()
    except Exception as exc:  # pragma: no cover - árvore incompleta
        raise XmlMalformed(f"XML incompleto: {exc}") from exc


def local_name(tag: str) -> str:
    """`w:p` → `p`; `{urn}p` → `p`."""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag
