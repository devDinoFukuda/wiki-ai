"""Projeção Markdown de `KnowledgeDocument` (plano §10.1, §10.4, §10.5).

Projeção, não conversão: o Markdown nasce da representação intermediária, do
mesmo modo que o Word — nenhum dos dois é gerado a partir do outro (§10.1).

As regras D01–D16 aplicáveis a texto estão implementadas aqui:

| Regra | Onde |
|---|---|
| D01 | `#`/`##`/`###` nativos e listas reais; negrito nunca substitui estrutura |
| D02 | título específico por unidade — validado antes, em `document.py` |
| D03 | `_belonging_lines` traz nome de negócio e identificador juntos |
| D04 | condição, comportamento, exceção e consequência na MESMA seção |
| D05 | estado, versão e escopo no CORPO da unidade, não só no cabeçalho |
| D07 | todo conteúdo essencial é texto; nada depende de imagem ou diagrama |
| D08 | relação em frase natural seguida do identificador |
| D09 | cada unidade repete sistema/capacidade/iniciativa e revisão |
| D10 | um bloco rotulado por estado, e ponteiro (não conteúdo) para o outro |
| D11 | sem boilerplate, sem metodologia do agente, sem log operacional |
| D12/D13 | `_verbatim` copia o `value` do fato sem tocar em nada |
| D15 | evidência sai como texto de referência; nunca vira link `[x](y)` |

Determinismo: nenhuma data de geração, nenhum caminho absoluto, nenhuma
iteração sobre estrutura não ordenada. Mesma revisão → mesmos bytes, que é o
que permite ao validador comparar duas gerações (§10.5/§10.6).

Só stdlib + `knowledge`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Sequence

from knowledge.models import EpistemicStatus, LifecycleStatus

from .document import (
    ANALYSIS_GAPS_TITLE,
    ANALYSIS_STATE_LABEL,
    STATE_LABEL,
    AnalysisState,
    DocKind,
    EvidenceRef,
    KnowledgeDocument,
    block_label,
    RelationRef,
    SemanticUnit,
    Statement,
)

#: Quebra de linha fixa. Markdown gerado com `os.linesep` deixaria de ser
#: byte a byte igual entre Windows e Linux, e a comparação do manifesto
#: passaria a acusar diferença onde não há (§10.5).
NEWLINE = "\n"

#: Subdiretório por tipo de documento — navegação previsível, sem que o
#: caminho carregue significado (D09: o conteúdo se identifica sozinho).
DOC_DIR: dict[DocKind, str] = {
    DocKind.VISAO_SISTEMA: "sistemas",
    DocKind.CAPACIDADE: "capacidades",
    DocKind.CONTRATO_DEPENDENCIA: "contratos",
    DocKind.INICIATIVA: "iniciativas",
    DocKind.EVOLUCAO: "evolucoes",
}

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 60) -> str:
    """Slug ASCII estável (sem acento, minúsculo, hifenizado)."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_form = "".join(c for c in normalized if not unicodedata.combining(c))
    slug = _SLUG_STRIP.sub("-", ascii_form.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "sem-titulo"


def document_path(document: KnowledgeDocument) -> str:
    """Caminho relativo determinístico do arquivo Markdown do documento.

    O sufixo com `document_id` garante unicidade sem depender do título: dois
    documentos com títulos parecidos não colidem, e renomear o título não move
    o arquivo (§10.6: nomes estáveis enquanto a unidade mantém identidade).
    """
    return (
        f"{DOC_DIR[document.doc_kind]}/{slugify(document.title)}-"
        f"{document.document_id[4:12]}.md"
    )


def anchor_of(title: str) -> str:
    """Âncora da seção, no formato de heading slug usual do Markdown."""
    return slugify(title, max_len=80)


def document_anchors(document: KnowledgeDocument) -> dict[str, str]:
    """`unit_id` → âncora dentro do arquivo, com a MESMA regra de desempate
    usada pelos leitores de Markdown (`-1`, `-2` para títulos repetidos).

    Renderizador e manifesto compartilham esta função de propósito: âncora
    calculada duas vezes por caminhos diferentes é âncora que diverge quando
    um dos dois muda (§10.5 compara presença por localização).
    """
    used: dict[str, int] = {}
    out: dict[str, str] = {}
    for unit in document.units:
        if not unit.is_publishable():
            continue
        base = anchor_of(unit.title)
        count = used.get(base, 0)
        used[base] = count + 1
        out[unit.unit_id] = base if count == 0 else f"{base}-{count}"
    return out


# --------------------------------------------------------------------------
# Renderização
# --------------------------------------------------------------------------


def render(document: KnowledgeDocument) -> str:
    """Markdown completo do documento (§10.1).

    Estrutura: `#` documento, `##` unidade, `###` bloco da unidade. Cada
    unidade é autossuficiente — pertencimento, assunto, estado, versão,
    condição, comportamento, exceção, relação, evidência e limitação ficam
    juntos, porque D04/D09 exigem que a unidade recuperada isolada continue
    respondendo sem o resto do documento.
    """
    lines: list[str] = []
    lines.append(f"# {document.title}")
    lines.append("")
    if document.summary:
        lines.append(document.summary)
        lines.append("")
    lines.append(f"- Tipo de documento: {_doc_kind_label(document.doc_kind)}")
    lines.append(f"- Namespace: {document.namespace}")
    lines.append(f"- Revisão de conhecimento: {document.revision_id}")
    lines.append(f"- Identificador do documento: {document.document_id}")
    lines.append(f"- Unidades publicadas: {len(document.publishable_units())}")
    lines.extend(_analysis_state_lines(document))
    lines.append("")
    lines.extend(_analysis_gaps_lines(document))

    for unit in document.units:
        if not unit.is_publishable():
            continue
        lines.extend(render_unit(unit))
    return NEWLINE.join(lines).rstrip() + NEWLINE


def _analysis_state_value(document: KnowledgeDocument) -> str:
    """Valor textual do estado da análise — vazio quando não declarado."""
    state = getattr(document, "analysis_state", None)
    if isinstance(state, AnalysisState):
        return state.value
    return str(state) if state else ""


def _analysis_state_lines(document: KnowledgeDocument) -> list[str]:
    """Estado da análise no CORPO do documento (mesmo espírito de D05).

    Sem esta linha, ausência de regra e ausência de investigação são
    indistinguíveis para quem lê — foi assim que um Word com só `implemented =
    true` passou por documento de regras.
    """
    state = getattr(document, "analysis_state", None) or None
    if state is None:
        return []
    label = ANALYSIS_STATE_LABEL.get(state, str(getattr(state, "value", state)))
    return [f"- Estado da análise de comportamento: {label}"]


def _analysis_gaps_lines(document: KnowledgeDocument) -> list[str]:
    """Bloco de lacunas do documento: o que falta e em que pé está."""
    gaps = tuple(getattr(document, "analysis_gaps", ()) or ())
    if not gaps:
        return []
    out = [f"## {ANALYSIS_GAPS_TITLE}", ""]
    out.extend(f"- {gap}" for gap in gaps)
    out.append("")
    return out


def render_unit(unit: SemanticUnit) -> list[str]:
    """Linhas Markdown de UMA unidade semântica (§10.3, itens 1 a 9)."""
    out: list[str] = []
    out.append(f"## {unit.title}")
    out.append("")
    if unit.shared_context:
        out.append(
            "Contexto mínimo repetido de propósito, extraído da mesma revisão deste documento."
        )
        out.append("")
    out.extend(_belonging_lines(unit))
    out.append(f"- Assunto: {unit.subject}")
    # D05: estado, versão e escopo no corpo — não só em propriedade do arquivo.
    out.append(f"- Estado: {STATE_LABEL[unit.state]} na revisão {unit.revision_id}")
    out.append(f"- Identificador da unidade: {unit.unit_id}")
    if unit.source_version_ids:
        out.append(f"- Versões de fonte: {', '.join(unit.source_version_ids)}")
    out.append("")

    if unit.conditions:
        out.append("### Condições de aplicação")
        out.append("")
        out.extend(_statement_lines(unit.conditions))
        out.append("")

    if unit.behavior:
        out.append(f"### {block_label(unit)}")
        out.append("")
        out.extend(_statement_lines(unit.behavior))
        out.append("")

    if unit.exceptions:
        out.append("### Exceções e consequências")
        out.append("")
        out.extend(_statement_lines(unit.exceptions))
        out.append("")

    if unit.gaps:
        out.append("### Lacunas e pontos não resolvidos")
        out.append("")
        out.extend(_statement_lines(unit.gaps))
        out.append("")

    if unit.cross_refs:
        # D10: o outro estado é referenciado, nunca copiado para dentro deste
        # bloco. O leitor sabe que existe proposta sem confundi-la com o
        # comportamento atual.
        out.append("### Blocos relacionados por estado")
        out.append("")
        for ref in unit.cross_refs:
            out.append(f"- {ref.label}: {ref.text} (unidade {ref.target_unit_id})")
        out.append("")

    if unit.relations:
        out.append("### Relações")
        out.append("")
        for rel in unit.relations:
            out.append(_relation_line(rel))
        out.append("")

    if unit.evidence:
        out.append("### Evidências")
        out.append("")
        for ev in unit.evidence:
            out.append(_evidence_line(ev))
        out.append("")
        out.append(
            "Referências acima são localizadores de fonte, não endereços navegáveis: "
            "resolva-os no repositório ou na fonte na versão indicada."
        )
        out.append("")

    if unit.limitations or unit.notes:
        out.append("### Limitações que alterariam a resposta")
        out.append("")
        out.extend(_statement_lines(unit.limitations))
        for note in unit.notes:
            out.append(f"- {note}")
        out.append("")

    return out


def _doc_kind_label(kind: DocKind) -> str:
    return {
        DocKind.VISAO_SISTEMA: "visão do sistema",
        DocKind.CAPACIDADE: "capacidade",
        DocKind.CONTRATO_DEPENDENCIA: "contrato ou dependência",
        DocKind.INICIATIVA: "iniciativa",
        DocKind.EVOLUCAO: "evolução (estado atual e proposta)",
    }[kind]


def _belonging_lines(unit: SemanticUnit) -> list[str]:
    """D03/D09 — nome de negócio E identificador, dentro da própria unidade."""
    pairs = unit.belonging.pairs()
    if not pairs:
        return [f"- Pertencimento: {unit.entity_type.value} {unit.entity_id}"]
    return [
        f"- {label}: {title} (identificador {eid})" for label, title, eid in pairs
    ]


def _verbatim(value: str) -> str:
    """Devolve o valor do fato EXATAMENTE como está no conhecimento.

    D12/D13: número, comparador, negação, unidade, nome de estado e precedência
    não sobrevivem a reescrita. Esta função existe para deixar explícito que a
    renderização não normaliza, não capitaliza e não traduz — só decide o
    invólucro. Valor com quebra de linha vai para bloco de código justamente
    para preservar as quebras sem virar item de lista partido.
    """
    return value


def _statement_lines(statements: Sequence[Statement]) -> list[str]:
    out: list[str] = []
    for st in statements:
        meta = (
            f"(fato {st.fact_id}; predicado {st.predicate}; natureza {st.nature.value}; "
            f"sustentação {st.epistemic_status.value}; ciclo de vida {st.lifecycle_status.value})"
        )
        value = _verbatim(st.value)
        if "\n" in value:
            out.append(f"- {meta}")
            out.append("")
            out.append("```text")
            out.extend(value.split("\n"))
            out.append("```")
            out.append("")
        else:
            out.append(f"- {value} {meta}")
        if st.epistemic_status is not EpistemicStatus.SUPPORTED:
            out.append(
                f"  - Atenção: este item está com sustentação {st.epistemic_status.value}; "
                "não é comportamento confirmado."
            )
        if st.lifecycle_status is LifecycleStatus.PROPOSED:
            out.append(
                "  - Este item é proposta registrada; permanece proposta até haver evidência "
                "de implementação."
            )
    return out


def _relation_line(rel: RelationRef) -> str:
    """D08 — frase natural PRIMEIRO, identificadores depois, sem hyperlink."""
    return (
        f"- {rel.natural_text} (relação {rel.relation_id}; tipo {rel.relation_type.value}; "
        f"entidade {rel.other_entity_type.value} {rel.other_entity_id})"
    )


def _evidence_line(ev: EvidenceRef) -> str:
    """D15 — referência textual com versão exata; nunca `[texto](destino)`."""
    suffix = (
        ""
        if ev.supports_implemented
        else " — não sustenta comportamento implementado (§5.4)"
    )
    return (
        f"- {ev.display} — fonte {ev.source_kind.value}, conteúdo {ev.content_kind.value}, "
        f"versão {ev.source_version_id} (evidência {ev.evidence_id}){suffix}"
    )


# --------------------------------------------------------------------------
# Plano inteiro
# --------------------------------------------------------------------------


def render_plan(plan: Any) -> dict[str, str]:
    """`caminho relativo` → conteúdo Markdown, para todo o plano.

    Ordenado por caminho: a escrita em disco fica previsível e a comparação
    entre duas gerações não depende da ordem do dicionário.
    """
    out: dict[str, str] = {}
    for doc in plan.documents:
        out[document_path(doc)] = render(doc)
    return dict(sorted(out.items()))


def render_manifest(plan: Any) -> dict[str, Any]:
    """`semantic_unit_id` → `{markdown_path, anchors, state, fact_ids, ...}` (§10.5).

    Mapeamento PLANO, na forma que `release.py` consome (`manifest.get(uid)`) e
    que `word.render_manifest_word` espelha para a conferência de equivalência.
    O envelope com metadados da revisão está em `render_manifest_envelope`,
    cuja chave `units` é exatamente este dicionário.

    Uma unidade de mini-contexto aparece em mais de um documento com o MESMO
    id: `markdown_path` é a localização primária (primeira em ordem de
    caminho) e `paths`/`anchors` listam todas — uma unidade, várias
    localizações, nunca duas unidades concorrentes na busca (§10.6).

    `md_filename`/`md_path` repetem a localização sob os nomes que
    `release.py` procura; `blocked` é sempre `False` porque o Markdown que
    chega aqui já foi renderizado por inteiro — quem bloqueia unidade por
    conteúdo não convertível (D14) é o renderizador Word e a validação.
    """
    return render_manifest_envelope(plan)["units"]


def render_manifest_envelope(plan: Any) -> dict[str, Any]:
    """Manifesto com metadados da revisão; `["units"]` é o mapa plano (§10.5/§10.6).

    O envelope existe porque o manifesto ATIVO precisa apontar para um
    conjunto completo: revisão, documentos, o que ficou de fora e por quê.
    Consumidor que só quer localizar uma unidade usa `render_manifest`.
    """
    units: dict[str, dict[str, Any]] = {}
    documents: dict[str, dict[str, Any]] = {}

    for doc in sorted(plan.documents, key=document_path):
        path = document_path(doc)
        anchor_map = document_anchors(doc)
        anchors_in_doc: list[str] = []
        for unit in doc.units:
            if not unit.is_publishable():
                continue
            anchor = anchor_map[unit.unit_id]
            anchors_in_doc.append(anchor)
            entry = units.get(unit.unit_id)
            if entry is None:
                entry = {
                    "markdown_path": path,
                    "md_path": path,
                    "md_filename": path.rsplit("/", 1)[-1],
                    "paths": [path],
                    "anchors": [f"{path}#{anchor}"],
                    "heading_anchor": anchor,
                    "state": unit.state.value,
                    "state_label": STATE_LABEL[unit.state],
                    "blocked": False,
                    "blocked_reasons": [],
                    "fact_ids": list(unit.fact_ids),
                    "relation_ids": list(unit.relation_ids),
                    "fact_states": unit.fact_states(),
                    "evidence_ids": list(unit.evidence_ids()),
                    "title": unit.title,
                    "subject_key": unit.subject_key,
                    "entity_id": unit.entity_id,
                    "shared_context": unit.shared_context,
                    "document_ids": [doc.document_id],
                    "revision_id": unit.revision_id,
                }
                units[unit.unit_id] = entry
            else:
                if path not in entry["paths"]:
                    entry["paths"].append(path)
                    entry["anchors"].append(f"{path}#{anchor}")
                if doc.document_id not in entry["document_ids"]:
                    entry["document_ids"].append(doc.document_id)
        documents[doc.document_id] = {
            "markdown_path": path,
            "title": doc.title,
            "doc_kind": doc.doc_kind.value,
            "namespace": doc.namespace,
            "anchor_entity_id": doc.anchor_entity_id,
            "unit_ids": [u.unit_id for u in doc.units if u.is_publishable()],
            "anchors": anchors_in_doc,
            "fact_ids": list(doc.fact_ids()),
            "relation_ids": list(doc.relation_ids()),
            "states": list(doc.states()),
            # Aditivo (§10.5): a suficiência do documento viaja no manifesto,
            # para o consumidor não precisar abrir o arquivo para saber se a
            # análise de comportamento foi feita.
            "analysis_state": _analysis_state_value(doc),
            "analysis_gaps": list(getattr(doc, "analysis_gaps", ()) or ()),
        }

    return {
        "manifest_version": 1,
        "revision_id": plan.revision_id,
        "namespace": plan.namespace,
        "consumers": list(plan.consumers),
        "catalog_unit_ids": [u.unit_id for u in plan.catalog_units],
        "documents": documents,
        "units": dict(sorted(units.items())),
        "skipped": [s.as_dict() for s in plan.skipped],
        "warnings": list(plan.warnings),
    }


__all__ = [
    "DOC_DIR",
    "NEWLINE",
    "anchor_of",
    "document_anchors",
    "document_path",
    "render",
    "render_manifest",
    "render_manifest_envelope",
    "render_plan",
    "render_unit",
    "slugify",
]
