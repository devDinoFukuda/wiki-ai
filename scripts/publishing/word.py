"""publishing.word — projeção Word (OOXML) direto da representação semântica.

Contrato (plano §10.1): o Word é gerado a partir do `KnowledgeDocument`/
`SemanticUnit` de `publishing.document`, NUNCA por parsing do Markdown já
renderizado. As duas saídas são irmãs da mesma revisão, não derivadas uma da
outra — só o layout pode divergir.

Reuso (módulos puros stdlib, sem estado):
  - `wk.docx_ooxml.build_package`       → .docx determinístico, styles/numbering nativos (D01)
  - `wk.docx_meta.core_props`           → docProps/core.xml (complementa D05, não substitui)
  - `wk.docx_meta.docx_filename_unique` → nome de arquivo estável e único
  - `wk.docx_md._convert_mermaid`       → mermaid → blocos IR estruturados (D07/F08)

Do vizinho `publishing.document` (import tardio, nunca editado aqui) vêm os
rótulos compartilhados `STATE_LABEL`/`STATE_BLOCK_LABEL`: se cada renderizador
inventasse o seu, o mesmo estado apareceria com nomes diferentes nas duas
saídas e a conferência de equivalência viraria falso negativo.

IR de blocos (contrato de `docx_ooxml`):
  block = {"kind": "heading"|"paragraph"|"list_item"|"table", ...}
  run   = {"text": str, "bold": bool, "italic": bool, "code": bool, "href": str|None}
"""

from __future__ import annotations

import enum
import re
import sys
import unicodedata
from pathlib import Path

try:  # pragma: no cover - caminho normal quando scripts/ já está no sys.path
    from wk import docx_md, docx_meta, docx_ooxml
except ImportError:  # pragma: no cover
    _SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from wk import docx_md, docx_meta, docx_ooxml


# ---------------------------------------------------------------------------
# Import tardio do vizinho `document.py` (dono: outro agente)
# ---------------------------------------------------------------------------

_DOC_MODULE = None
_DOC_LOADED = False


def _document_module():
    """Carrega `publishing.document` sob demanda; ausência nunca quebra a renderização."""
    global _DOC_MODULE, _DOC_LOADED
    if _DOC_LOADED:
        return _DOC_MODULE
    _DOC_LOADED = True
    for loader in (
        lambda: __import__("publishing.document", fromlist=["document"]),
        lambda: __import__("document"),
    ):
        try:
            _DOC_MODULE = loader()
            break
        except Exception:
            continue
    return _DOC_MODULE


# Rótulos de fallback — usados só quando `document.py` não está disponível
# (renderização de um objeto que apenas honra os nomes de campo).
_FALLBACK_STATE_LABEL = {
    "implemented": "Implementado",
    "proposed": "Proposto (não implementado)",
    "historical": "Histórico",
    "unresolved": "Não resolvido",
}
_FALLBACK_STATE_BLOCK_LABEL = {
    "implemented": "Comportamento implementado",
    "proposed": "Mudança proposta (ainda não implementada)",
    "historical": "Comportamento histórico (não vigente)",
    "unresolved": "Ponto não resolvido",
}


def _label_map(name, fallback):
    """`STATE_LABEL`/`STATE_BLOCK_LABEL` do vizinho, indexados por string de estado."""
    mod = _document_module()
    table = getattr(mod, name, None) if mod is not None else None
    if not table:
        return dict(fallback)
    return {_enum_value(k): v for k, v in table.items()}


# ---------------------------------------------------------------------------
# Rótulos próprios do Word (seções que só existem aqui)
# ---------------------------------------------------------------------------

L_CONTEXT = "Contexto"
L_SUBJECT = "Assunto"
L_STATE = "Estado"
L_UNIT_ID = "Identificador da unidade"
L_SOURCE_VERSIONS = "Versões de fonte"
L_CONDITIONS = "Condições de aplicação"
L_EXCEPTIONS = "Exceções e consequências"
L_GAPS = "Lacunas e pontos não resolvidos"
L_CROSS = "Blocos relacionados por estado"
L_RELATIONS = "Relações"
L_EVIDENCE = "Evidências"
L_LIMITS = "Limitações que alterariam a resposta"
L_DIAGRAM = "Representação textual do diagrama"
L_BLOCKED = "Unidade bloqueada"
L_UNITS_INDEX = "Unidades deste documento"

#: D15 — a mesma advertência do Markdown: localizador de fonte não é endereço
#: navegável, e o Copilot não deve tratá-lo como link.
EVIDENCE_NOTE = (
    "Referências acima são localizadores de fonte, não endereços navegáveis: "
    "resolva-os no repositório ou na fonte na versão indicada."
)

SHARED_CONTEXT_NOTE = (
    "Contexto mínimo repetido de propósito, extraído da mesma revisão deste documento."
)

STATE_BLOCKED = "blocked"

_WS_RE = re.compile(r"\s+")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_MERMAID_FENCE_RE = re.compile(r"```+\s*mermaid\b(.*?)```+", re.S | re.I)
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

# D02 — títulos genéricos usados quando `document.is_generic_title` não está
# disponível. A lista canônica é a de `document.GENERIC_TITLES`.
_FALLBACK_GENERIC_TITLES = frozenset(
    {
        "detalhes", "detalhe", "outros", "outro", "geral", "diversos", "informacoes",
        "notas", "observacoes", "documento", "conteudo", "misc", "anexo", "apendice",
        "resumo", "introducao", "sumario", "overview", "details", "other",
        "referencias", "secao", "parte", "capitulo", "tbd", "a definir",
        "sem titulo", "untitled",
    }
)

# D11 — metodologia do agente, aviso repetido e log operacional nunca entram.
BOILERPLATE_PREFIXES = (
    "gerado automaticamente", "gerado por", "este documento foi gerado",
    "nota do agente", "log de execucao", "log:", "debug:", "trace:",
    "metodologia:", "prompt:", "resposta do modelo", "agent run",
    "disclaimer:", "todo:", "fixme:",
)


# ---------------------------------------------------------------------------
# Acesso tolerante ao modelo (dataclass frozen, objeto ou dict)
# ---------------------------------------------------------------------------


def _enum_value(value):
    """Desembrulha `enum.Enum`.

    `UnitState`, `DocKind` e as enums de `knowledge.models` herdam de `str`:
    sem este desembrulho, `str(UnitState.IMPLEMENTED)` produziria
    "UnitState.IMPLEMENTED" no corpo do Word e no manifesto, divergindo do
    "implemented" que o Markdown publica.
    """
    if isinstance(value, enum.Enum):
        return value.value
    return value


def _get(obj, *names, default=None):
    """Primeiro atributo/chave existente e não vazio dentre `names`.

    Propriedades (`fact_ids`) e métodos sem argumento (`statements`) são
    resolvidos: o contrato do vizinho é honrado por nome, não por tipo.
    """
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, dict):
            val = obj.get(name)
        else:
            val = getattr(obj, name, None)
            if callable(val):
                try:
                    val = val()
                except TypeError:
                    continue
        if val not in (None, "", [], {}, ()):
            return val
    return default


def _as_list(value):
    """Normaliza str/None/objeto/sequência para lista, preservando a ordem."""
    if value in (None, "", [], {}, ()):
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    return [value]


def _text_of(value):
    """Texto simples de um item (str ou objeto/dict com campo textual)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    txt = _get(value, "text", "texto", "natural_text", "display", "statement",
               "descricao", "description", "value", default=None)
    return str(txt).strip() if txt is not None else str(value).strip()


def _norm_ws(text):
    """Colapsa espaço em branco; aceita enum e devolve string."""
    if text is None:
        return ""
    return _WS_RE.sub(" ", str(_enum_value(text))).strip()


def _fold(text):
    """Minúsculas sem acento — comparações de título e boilerplate."""
    norm = unicodedata.normalize("NFKD", _norm_ws(text).lower())
    return "".join(c for c in norm if not unicodedata.combining(c))


def slugify(text, max_len=80):
    """Slug ASCII estável — MESMA regra de `markdown.slugify`, para as âncoras
    das duas saídas não divergirem quando comparadas no manifesto (§10.5)."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_form = "".join(c for c in normalized if not unicodedata.combining(c))
    slug = _SLUG_STRIP.sub("-", ascii_form.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "sem-titulo"


def anchor_of(title):
    """Âncora da seção; delega a `markdown.anchor_of` quando disponível."""
    try:  # pragma: no cover - depende do módulo vizinho
        from publishing import markdown as _markdown  # type: ignore

        return _markdown.anchor_of(title)
    except Exception:
        return slugify(title, max_len=80)


def is_generic_title(title):
    """D02 — revalida o título herdado do document contra a lista de genéricos."""
    mod = _document_module()
    checker = getattr(mod, "is_generic_title", None) if mod is not None else None
    if callable(checker):
        return bool(checker(title or ""))
    folded = _fold(title).strip(" .:-")
    return len(folded) < 3 or folded in _FALLBACK_GENERIC_TITLES


def is_boilerplate(text):
    """D11 — descarta metodologia do agente, aviso repetido e log operacional."""
    folded = _fold(text)
    if not folded:
        return True
    return any(folded.startswith(prefix) for prefix in BOILERPLATE_PREFIXES)


# ---------------------------------------------------------------------------
# Construtores do IR
# ---------------------------------------------------------------------------


def _run(text, bold=False, italic=False, code=False, href=None):
    """Run no formato exato consumido por `docx_ooxml._run_xml` (5 chaves).

    `href` permanece sempre None nesta projeção: D15 proíbe apresentar
    localizador local como link resolvível.
    """
    return {"text": "" if text is None else str(text), "bold": bold,
            "italic": italic, "code": code, "href": href}


def _heading(text, level):
    """D01 — Heading nativo (Heading1..4 de styles.xml), nunca negrito simulado."""
    return {"kind": "heading", "level": max(1, min(4, int(level))), "runs": [_run(text)]}


def _para(runs, style="Normal"):
    return {"kind": "paragraph", "runs": list(runs), "style": style}


def _text_para(text, style="Normal"):
    return _para([_run(text)], style)


def _labeled(label, text):
    """`Rótulo: conteúdo` — o rótulo é semântico; a estrutura vem do heading (D01)."""
    return _para([_run(f"{label}: ", bold=True), _run(_norm_ws(text))])


def _bullet(text, level=0):
    """D01 — item de lista com numbering nativo (ListParagraph + numPr)."""
    return {"kind": "list_item", "level": level, "ordered": False, "runs": [_run(text)]}


def _table(header, rows):
    """D06 — tabela simples: cabeçalho claro, sem merge (gridSpan/vMerge nunca emitidos)."""
    return {
        "kind": "table",
        "header": [[_run(h)] for h in header],
        "rows": [[[_run("" if c is None else str(c))] for c in row] for row in rows],
    }


# ---------------------------------------------------------------------------
# Leitura de uma SemanticUnit
# ---------------------------------------------------------------------------

_STATEMENT_GROUPS = ("conditions", "behavior", "exceptions", "limitations", "gaps")


def unit_statements(unit):
    """Todos os `Statement` da unidade, na ordem canônica do contrato."""
    stmts = _get(unit, "statements", default=None)
    if stmts:
        return list(stmts)
    out = []
    for group in _STATEMENT_GROUPS:
        out.extend(_as_list(_get(unit, group, default=[])))
    out.extend(_as_list(_get(unit, "facts", "fatos", default=[])))
    return out


def unit_state(unit):
    """Estado da unidade como string canônica (`UnitState.value`)."""
    return _norm_ws(_get(unit, "state", "estado", default="unresolved"))


def unit_state_label(unit):
    """Rótulo humano do estado — o MESMO usado pelo Markdown (D05/D10)."""
    labels = _label_map("STATE_LABEL", _FALLBACK_STATE_LABEL)
    state = unit_state(unit)
    return labels.get(state, state.capitalize() if state else "Não resolvido")


def unit_behavior_label(unit):
    """D10 — rótulo do bloco de comportamento por estado; implementado e
    proposta nunca compartilham o mesmo rótulo."""
    labels = _label_map("STATE_BLOCK_LABEL", _FALLBACK_STATE_BLOCK_LABEL)
    state = unit_state(unit)
    return labels.get(state, f"Comportamento (estado: {state})")


def unit_title(unit, document=None):
    """D02/D09 — título específico; enriquece quando o herdado é genérico.

    `document.SemanticUnit` já rejeita título genérico na construção; a
    revalidação existe para o caso de a unidade chegar por outro caminho.
    """
    title = _norm_ws(_get(unit, "title", "titulo", default=""))
    if title and not is_generic_title(title):
        return title
    parts = [p for p in (
        _norm_ws(_get(_get(unit, "belonging", default=None), "system_title", default="")),
        _norm_ws(_get(unit, "subject", "assunto", default="")),
        title,
    ) if p]
    if not parts:
        parts = [_norm_ws(_get(unit, "unit_id", "id", default="unidade")) or "unidade"]
    built = " — ".join(dict.fromkeys(parts))
    label = unit_state_label(unit)
    return f"{built} ({label})" if label else built


def unit_fact_ids(unit):
    """IDs de fatos, sem duplicatas, na ordem declarada."""
    declared = [str(f) for f in _as_list(_get(unit, "fact_ids", default=[]))]
    if declared:
        return list(dict.fromkeys(declared))
    return list(dict.fromkeys(
        str(_get(st, "fact_id", "id", default="")) for st in unit_statements(unit)
        if _get(st, "fact_id", "id", default=None) is not None
    ))


def unit_relation_ids(unit):
    """IDs de relações, sem duplicatas, na ordem declarada."""
    declared = [str(r) for r in _as_list(_get(unit, "relation_ids", default=[]))]
    if declared:
        return list(dict.fromkeys(declared))
    return list(dict.fromkeys(
        str(_get(rel, "relation_id", "id", default="")) for rel in _as_list(
            _get(unit, "relations", "relacoes", default=[]))
        if _get(rel, "relation_id", "id", default=None) is not None
    ))


def is_publishable(unit):
    """§10.2 — unidade só com moldura (título + pertencimento) não é publicada.

    Usa `SemanticUnit.is_publishable()` quando existe, para o Word publicar
    exatamente o mesmo conjunto que o Markdown.
    """
    checker = getattr(unit, "is_publishable", None)
    if callable(checker):
        return bool(checker())
    return bool(
        _get(unit, "behavior", default=None)
        or _get(unit, "exceptions", default=None)
        or _get(unit, "gaps", default=None)
        or _get(unit, "relations", default=None)
        or _get(unit, "facts", default=None)
    )


def unpublishable_reason(unit):
    reason = getattr(unit, "unpublishable_reason", None)
    if callable(reason):
        return reason()
    return (
        f"unidade {_get(unit, 'unit_id', default='')} sem conteúdo útil: só título e "
        "pertencimento, sem comportamento, exceção, lacuna ou relação (§10.2)"
    )


# ---------------------------------------------------------------------------
# Statements → blocos (D04, D06, D12, D13)
# ---------------------------------------------------------------------------

_STATEMENT_HEADER = ("Fato", "Afirmação (verbatim)", "Predicado", "Natureza",
                     "Sustentação", "Ciclo de vida")


def _statement_meta(statement):
    """Metadados do fato na MESMA ordem do Markdown (só parênteses — §10.3
    proíbe colchete em conteúdo final)."""
    return (
        f"(fato {_norm_ws(_get(statement, 'fact_id', 'id', default=''))}; "
        f"predicado {_norm_ws(_get(statement, 'predicate', default=''))}; "
        f"natureza {_norm_ws(_get(statement, 'nature', default=''))}; "
        f"sustentação {_norm_ws(_get(statement, 'epistemic_status', default=''))}; "
        f"ciclo de vida {_norm_ws(_get(statement, 'lifecycle_status', default=''))})"
    )


def _statement_caveats(statement):
    """Advertências de sustentação/ciclo de vida — as mesmas do Markdown."""
    out = []
    epistemic = _norm_ws(_get(statement, "epistemic_status", default=""))
    lifecycle = _norm_ws(_get(statement, "lifecycle_status", default=""))
    if epistemic and epistemic != "supported":
        out.append(
            f"Atenção: este item está com sustentação {epistemic}; "
            "não é comportamento confirmado."
        )
    if lifecycle == "proposed":
        out.append(
            "Este item é proposta registrada; permanece proposta até haver evidência "
            "de implementação."
        )
    return out


def _statement_blocks(statements, heading):
    """Um grupo de fatos: tabela simples (D06) + valores multilinha em bloco
    de código, preservando as quebras VERBATIM (D12/D13).

    O `value` nunca é normalizado, capitalizado ou traduzido aqui; a projeção
    só decide o invólucro.
    """
    statements = list(statements)
    if not statements:
        return []

    blocks = [_heading(heading, 3)]
    rows = []
    multiline = []
    for st in statements:
        fid = _norm_ws(_get(st, "fact_id", "id", default=""))
        value = _get(st, "value", "valor", "statement", "afirmacao", "text", default="")
        value = "" if value is None else str(value)
        row = [
            fid,
            value if "\n" not in value else "(valor multilinha reproduzido abaixo desta tabela)",
            _norm_ws(_get(st, "predicate", default="")),
            _norm_ws(_get(st, "nature", default="")),
            _norm_ws(_get(st, "epistemic_status", default="")),
            _norm_ws(_get(st, "lifecycle_status", default="")),
        ]
        rows.append(row)
        if "\n" in value:
            multiline.append((fid, value, st))
    blocks.append(_table(list(_STATEMENT_HEADER), rows))

    for fid, value, st in multiline:
        blocks.append(_text_para(f"Valor integral do fato {fid} {_statement_meta(st)}"))
        for line in value.split("\n"):
            blocks.append(_text_para(line, style="CodeBlock"))

    for st in statements:
        for caveat in _statement_caveats(st):
            blocks.append(_bullet(f"{_norm_ws(_get(st, 'fact_id', default=''))}: {caveat}"))
    return blocks


# ---------------------------------------------------------------------------
# D07/D14/F08 — diagramas, HTML e equivalência textual
# ---------------------------------------------------------------------------


def _mermaid_blocks(source, label):
    """Converte mermaid em blocos IR. Retorna (blocks, estruturado, nota)."""
    lines = ["```mermaid"] + str(source).splitlines() + ["```"]
    converted, warns = docx_md._convert_mermaid(lines)
    degraded = any(str(w).startswith("mermaid:") for w in warns or [])
    if degraded:
        return [], False, f"{label}: conversão do diagrama degradada ({'; '.join(warns)})"
    return converted, True, ""


def _embedded_visual_blocks(unit):
    """Diagramas/HTML embutidos no `value` dos fatos (F08).

    O `value` verbatim já é a representação textual do conteúdo — por isso um
    mermaid embutido NÃO bloqueia a unidade mesmo quando a conversão degrada.
    Bloqueia apenas quando o conteúdo é só marcação: HTML sem nenhum texto
    restante depois de removidas as tags, isto é, informação que existiria só
    como asset e desapareceria do Word (D14: nunca omissão silenciosa).
    """
    blocks = []
    notes = []
    blocked = []

    for st in unit_statements(unit):
        fid = _norm_ws(_get(st, "fact_id", "id", default=""))
        value = _get(st, "value", "valor", "text", default="")
        value = "" if value is None else str(value)
        if not value:
            continue

        for match in _MERMAID_FENCE_RE.finditer(value):
            label = f"{L_DIAGRAM} (fato {fid})"
            converted, structured, note = _mermaid_blocks(match.group(1), label)
            if structured:
                blocks.append(_heading(label, 3))
                blocks.extend(converted)
            else:
                notes.append(note)
                notes.append(
                    f"{label}: o texto do fato {fid} permanece como representação equivalente"
                )

        without_mermaid = _MERMAID_FENCE_RE.sub(" ", value)
        if _HTML_TAG_RE.search(without_mermaid):
            stripped = _norm_ws(_HTML_TAG_RE.sub(" ", without_mermaid))
            if stripped:
                blocks.append(_heading(f"Conteúdo do bloco HTML (fato {fid})", 3))
                blocks.append(_text_para(stripped))
            else:
                blocked.append(
                    f"fato {fid}: conteúdo apenas em marcação HTML, sem texto equivalente "
                    "(D07/D14/F08)"
                )
    return blocks, notes, blocked


def _declared_diagram_blocks(unit):
    """Diagramas declarados como campo próprio da unidade (forma opcional).

    Mantido porque a representação pode transportar o diagrama fora do `value`;
    a regra de bloqueio é a mesma: sem texto equivalente possível, bloqueia.
    """
    blocks = []
    notes = []
    blocked = []
    for idx, diagram in enumerate(_as_list(_get(unit, "diagrams", "diagramas", default=[])), start=1):
        label = _norm_ws(_get(diagram, "title", "titulo", default="")) or f"{L_DIAGRAM} {idx}"
        blocks.append(_heading(label, 3))
        has_equivalent = False

        equivalent = _norm_ws(_text_of(_get(diagram, "text_equivalent", "texto_equivalente",
                                            "equivalent_text", default="")))
        if equivalent and not is_boilerplate(equivalent):
            blocks.append(_labeled("Descrição equivalente", equivalent))
            has_equivalent = True

        source = _get(diagram, "source", "mermaid", "code", default="")
        if source and "mermaid" in _fold(_get(diagram, "format", "formato", default="mermaid")):
            converted, structured, note = _mermaid_blocks(source, label)
            if structured:
                blocks.extend(converted)
                has_equivalent = True
            else:
                notes.append(note)

        rel_rows = _relation_rows(unit)
        if rel_rows:
            blocks.append(_text_para("Equivalente textual das arestas do diagrama:"))
            blocks.append(_table(["Origem", "Relação", "Destino", "ID da relação"], rel_rows))
            has_equivalent = True

        if not has_equivalent:
            blocked.append(f"{label}: sem representação textual equivalente (D07/D14/F08)")
    return blocks, notes, blocked


def _declared_asset_blocks(unit):
    """Assets (HTML de métricas/grafo, imagem) declarados na unidade.

    F08: o comportamento antigo de ignorar a fonte com asset HTML é
    substituído por publicar os dados como texto — ou bloquear a unidade.
    """
    blocks = []
    notes = []
    blocked = []
    for idx, asset in enumerate(_as_list(_get(unit, "assets", "anexos", default=[])), start=1):
        kind = _norm_ws(_get(asset, "kind", "tipo", "format", default="asset"))
        label = _norm_ws(_get(asset, "title", "titulo", default="")) or f"Conteúdo anexo {idx} ({kind})"
        blocks.append(_heading(label, 3))
        has_equivalent = False

        equivalent = _norm_ws(_text_of(_get(asset, "text_equivalent", "texto_equivalente",
                                            default="")))
        if equivalent and not is_boilerplate(equivalent):
            blocks.append(_text_para(equivalent))
            has_equivalent = True

        header = [str(h) for h in _as_list(_get(asset, "header", "cabecalho", "columns", default=[]))]
        rows = [list(r) for r in _as_list(_get(asset, "rows", "linhas", "data", default=[]))]
        if header and rows:
            blocks.append(_table(header, rows))
            has_equivalent = True

        ref = _norm_ws(_get(asset, "ref", "path", "src", default=""))
        if ref:
            # D15 — citado como texto; nunca como link resolvível.
            blocks.append(_labeled("Origem do anexo", ref))

        if not has_equivalent:
            blocked.append(f"{label}: asset sem representação textual equivalente (D14/F08)")
    return blocks, notes, blocked


def _relation_rows(unit):
    """Arestas como linhas de tabela — equivalente textual de um diagrama (D07)."""
    rows = []
    anchor = _norm_ws(_get(unit, "entity_id", "unit_id", default=""))
    for rel in _as_list(_get(unit, "relations", "relacoes", default=[])):
        direction = _norm_ws(_get(rel, "direction", default="out"))
        other = _norm_ws(_get(rel, "other_entity_id", "target", "destino", default=""))
        kind = _norm_ws(_get(rel, "relation_type", "kind", "tipo", default="relaciona-se com"))
        src, dst = (anchor, other) if direction != "in" else (other, anchor)
        rows.append([src, kind, dst, _norm_ws(_get(rel, "relation_id", "id", default=""))])
    return rows


# ---------------------------------------------------------------------------
# Seções da unidade
# ---------------------------------------------------------------------------


def _belonging_blocks(unit):
    """D03/D09 — nome de negócio E identificador dentro da própria unidade.

    Sem isto, a unidade recuperada isolada dependeria do nome do arquivo ou da
    pasta para dizer de que sistema/capacidade ela fala.
    """
    belonging = _get(unit, "belonging", "pertencimento", default=None)
    pairs = _get(belonging, "pairs", default=None) if belonging is not None else None
    blocks = []
    if pairs:
        for label, title, eid in pairs:
            blocks.append(_labeled(label, f"{title} (identificador {eid})"))
        return blocks
    entity_type = _norm_ws(_get(unit, "entity_type", default=""))
    entity_id = _norm_ws(_get(unit, "entity_id", default=""))
    if entity_type or entity_id:
        blocks.append(_labeled("Pertencimento", f"{entity_type} {entity_id}".strip()))
    return blocks


def _relations_blocks(unit):
    """D08 — frase natural PRIMEIRO, identificadores depois, sem hyperlink."""
    relations = _as_list(_get(unit, "relations", "relacoes", default=[]))
    if not relations:
        return []
    blocks = [_heading(L_RELATIONS, 3)]
    for rel in relations:
        phrase = _norm_ws(_get(rel, "natural_text", "statement", "descricao", default=""))
        if not phrase:
            direction = _norm_ws(_get(rel, "direction", default="out"))
            anchor = _norm_ws(_get(unit, "entity_id", "unit_id", default=""))
            other = _norm_ws(_get(rel, "other_entity_id", "target", default=""))
            kind = _norm_ws(_get(rel, "relation_type", "kind", default="relaciona-se com"))
            phrase = (f"{anchor} {kind} {other}" if direction != "in"
                      else f"{other} {kind} {anchor}")
        rid = _norm_ws(_get(rel, "relation_id", "id", default=""))
        rtype = _norm_ws(_get(rel, "relation_type", "kind", default=""))
        other_type = _norm_ws(_get(rel, "other_entity_type", default=""))
        other_id = _norm_ws(_get(rel, "other_entity_id", "target", default=""))
        detail = "; ".join(p for p in (
            f"relação {rid}" if rid else "",
            f"tipo {rtype}" if rtype else "",
            f"entidade {other_type} {other_id}".strip() if other_id else "",
        ) if p)
        blocks.append(_bullet(f"{phrase} ({detail})" if detail else phrase))
    # D07 — as mesmas arestas também como tabela: o grafo existe como texto.
    rows = _relation_rows(unit)
    if rows:
        blocks.append(_table(["Origem", "Tipo de relação", "Destino", "ID da relação"], rows))
    return blocks


def _cross_ref_blocks(unit):
    """D10/D16 — o outro estado é REFERENCIADO, nunca copiado para este bloco.

    É também onde a cadeia inception → decisão → refinamento → impacto aparece
    quando o planejador a materializou como `CrossRef`.
    """
    refs = _as_list(_get(unit, "cross_refs", "cross_references", default=[]))
    if not refs:
        return []
    blocks = [_heading(L_CROSS, 3)]
    for ref in refs:
        label = _norm_ws(_get(ref, "label", default=""))
        text = _norm_ws(_get(ref, "text", "descricao", default=""))
        target = _norm_ws(_get(ref, "target_unit_id", default=""))
        state = _norm_ws(_get(ref, "target_state", default=""))
        detail = f"unidade {target}" if target else ""
        if state:
            detail = f"{detail}, estado {state}" if detail else f"estado {state}"
        line = f"{label}: {text}" if label else text
        blocks.append(_bullet(f"{line} ({detail})" if detail else line))
    return blocks


def _evidence_blocks(unit):
    """D15 — evidência próxima, com a versão exata da fonte, como TEXTO."""
    refs = _as_list(_get(unit, "evidence", "evidence_refs", "evidencias", default=[]))
    if not refs:
        return []
    blocks = [_heading(L_EVIDENCE, 3)]
    for ev in refs:
        if isinstance(ev, str):
            blocks.append(_bullet(_norm_ws(ev)))
            continue
        display = _norm_ws(_get(ev, "display", "locator", "path", "ref", default=""))
        source_kind = _norm_ws(_get(ev, "source_kind", default=""))
        content_kind = _norm_ws(_get(ev, "content_kind", default=""))
        version = _norm_ws(_get(ev, "source_version_id", "version", "versao", default=""))
        eid = _norm_ws(_get(ev, "evidence_id", "id", default=""))
        parts = [display] if display else []
        if source_kind:
            parts.append(f"fonte {source_kind}")
        if content_kind:
            parts.append(f"conteúdo {content_kind}")
        if version:
            parts.append(f"versão {version}")
        line = " — ".join([parts[0]] + [", ".join(parts[1:])]) if len(parts) > 1 else (
            parts[0] if parts else _text_of(ev))
        if eid:
            line = f"{line} (evidência {eid})"
        if _get(ev, "supports_implemented", default=None) is False:
            line += " — não sustenta comportamento implementado (§5.4)"
        blocks.append(_bullet(line))
    blocks.append(_text_para(EVIDENCE_NOTE))
    return blocks


def _limits_blocks(unit):
    """§10.3 item 9 — limitações e notas que alterariam a resposta."""
    limits = _as_list(_get(unit, "limitations", "limitacoes", default=[]))
    notes = [_norm_ws(n) for n in _as_list(_get(unit, "notes", default=[]))]
    notes = [n for n in notes if n and not is_boilerplate(n)]
    if not limits and not notes:
        return []
    blocks = _statement_blocks(limits, L_LIMITS) if limits else [_heading(L_LIMITS, 3)]
    blocks.extend(_bullet(n) for n in notes)
    return blocks


def render_unit(unit, document=None):
    """Projeta uma `SemanticUnit` em blocos IR.

    Retorna dict: {unit_id, title, anchor, state, state_label, fact_ids,
                   relation_ids, blocks, blocked, blocked_reasons, notes,
                   publishable}.
    """
    unit_id = _norm_ws(_get(unit, "unit_id", "id", default=""))
    title = unit_title(unit, document)
    state = unit_state(unit)
    revision = _norm_ws(_get(unit, "revision_id", default="")) or _norm_ws(
        _get(document, "revision_id", "revision", default="")
    )
    notes = []
    blocked_reasons = []

    if is_generic_title(_get(unit, "title", "titulo", default="")):
        notes.append(f"{unit_id}: título genérico substituído por título específico (D02)")

    blocks = [_heading(title, 2)]
    if _get(unit, "shared_context", default=False) is True:
        blocks.append(_text_para(SHARED_CONTEXT_NOTE))
    blocks.extend(_belonging_blocks(unit))  # D03/D09

    subject = _norm_ws(_get(unit, "subject", "assunto", "summary", default=""))
    if subject and not is_boilerplate(subject):
        blocks.append(_labeled(L_SUBJECT, subject))

    # D05 — estado, versão e escopo no CORPO; core props só complementam.
    label = unit_state_label(unit)
    blocks.append(_labeled(L_STATE, f"{label} na revisão {revision}" if revision else label))
    blocks.append(_labeled(L_UNIT_ID, unit_id))
    namespace = _norm_ws(_get(unit, "namespace", default="")) or _norm_ws(
        _get(document, "namespace", default="")
    )
    if namespace:
        blocks.append(_labeled("Escopo", namespace))
    versions = [str(v) for v in _as_list(_get(unit, "source_version_ids", default=[]))]
    if versions:
        blocks.append(_labeled(L_SOURCE_VERSIONS, ", ".join(versions)))

    # D04 — condição, comportamento, exceção e lacuna em seções ADJACENTES da
    # mesma unidade; nenhuma delas é substituída por remissão.
    blocks.extend(_statement_blocks(_as_list(_get(unit, "conditions", default=[])), L_CONDITIONS))
    blocks.extend(_statement_blocks(_as_list(_get(unit, "behavior", default=[])),
                                    unit_behavior_label(unit)))
    blocks.extend(_statement_blocks(_as_list(_get(unit, "exceptions", default=[])), L_EXCEPTIONS))
    blocks.extend(_statement_blocks(_as_list(_get(unit, "gaps", default=[])), L_GAPS))

    # Forma alternativa (`facts`), quando a unidade não usa os grupos acima.
    if not any(_get(unit, g, default=None) for g in _STATEMENT_GROUPS):
        blocks.extend(_statement_blocks(_as_list(_get(unit, "facts", "fatos", default=[])),
                                        unit_behavior_label(unit)))

    blocks.extend(_cross_ref_blocks(unit))   # D10/D16
    blocks.extend(_relations_blocks(unit))   # D08

    for producer in (_embedded_visual_blocks, _declared_diagram_blocks, _declared_asset_blocks):
        vis_blocks, vis_notes, vis_blocked = producer(unit)
        blocks.extend(vis_blocks)
        notes.extend(vis_notes)
        blocked_reasons.extend(vis_blocked)

    blocks.extend(_evidence_blocks(unit))  # D15
    blocks.extend(_limits_blocks(unit))    # §10.3 item 9

    declared = _norm_ws(_get(unit, "blocked_reason", "motivo_bloqueio", default=""))
    if declared:
        blocked_reasons.append(declared)
    if _get(unit, "blocked", default=False) is True:
        blocked_reasons.append("unidade marcada como bloqueada na representação semântica")

    blocked = bool(blocked_reasons)
    if blocked:
        # D14 — nunca omissão silenciosa: o motivo é publicado no corpo E
        # sinalizado no manifesto, que barra a revisão inteira.
        blocks.append(_heading(L_BLOCKED, 3))
        blocks.append(_text_para(
            "Esta unidade não pode ser publicada como conhecimento completo: há "
            "informação essencial sem representação textual equivalente."
        ))
        blocks.extend(_bullet(reason) for reason in blocked_reasons)

    return {
        "unit_id": unit_id,
        "title": title,
        "anchor": anchor_of(title),
        "state": STATE_BLOCKED if blocked else state,
        "state_label": "Bloqueado por falta de equivalente textual" if blocked else label,
        "fact_ids": unit_fact_ids(unit),
        "relation_ids": unit_relation_ids(unit),
        "blocks": blocks,
        "blocked": blocked,
        "blocked_reasons": blocked_reasons,
        "notes": notes,
        "publishable": is_publishable(unit),
    }


# ---------------------------------------------------------------------------
# Documento
# ---------------------------------------------------------------------------


def document_units(document):
    """Unidades do documento, na ordem declarada."""
    return _as_list(_get(document, "units", "unidades", "semantic_units", default=[]))


def publishable_units(document):
    """Só as unidades que respondem algo (§10.2) — o mesmo conjunto do Markdown."""
    return [u for u in document_units(document) if is_publishable(u)]


def _dedupe_anchors(rendered):
    """Desempate `-1`, `-2` para títulos repetidos — a mesma regra de
    `markdown.document_anchors`, para as âncoras não divergirem."""
    used = {}
    for unit in rendered:
        base = unit["anchor"]
        count = used.get(base, 0)
        used[base] = count + 1
        if count:
            unit["anchor"] = f"{base}-{count}"
    return rendered


def _document_header_blocks(document, rendered):
    """Heading1, contexto do documento e índice de unidades (D02/D05/D09/D11).

    Sem boilerplate: identidade, revisão e o que há dentro — nada de
    metodologia ou log.
    """
    title = _norm_ws(_get(document, "title", "titulo", default="")) or _norm_ws(
        _get(document, "document_id", default="Documento de conhecimento")
    )
    blocks = [_heading(title, 1)]

    ident = [f"Documento: {_norm_ws(_get(document, 'document_id', 'id', default=''))}"]
    kind = _norm_ws(_get(document, "doc_kind", "kind", default=""))
    if kind:
        ident.append(f"Tipo de unidade documental: {kind}")
    anchor_entity = _norm_ws(_get(document, "anchor_entity_id", default=""))
    if anchor_entity:
        anchor_type = _norm_ws(_get(document, "anchor_entity_type", default=""))
        ident.append(f"Entidade âncora: {anchor_type} {anchor_entity}".replace("  ", " "))
    namespace = _norm_ws(_get(document, "namespace", default=""))
    if namespace:
        ident.append(f"Escopo: {namespace}")
    revision = _norm_ws(_get(document, "revision_id", "revision", default=""))
    if revision:
        ident.append(f"Revisão: {revision}")
    blocks.append(_labeled(L_CONTEXT, " | ".join(ident)))

    summary = _norm_ws(_get(document, "summary", default=""))
    if summary and not is_boilerplate(summary):
        blocks.append(_text_para(summary))

    if rendered:
        blocks.append(_heading(L_UNITS_INDEX, 2))
        blocks.append(_table(
            ["Identificador da unidade", "Assunto", "Estado"],
            [[u["unit_id"], u["title"], u["state_label"]] for u in rendered],
        ))
    return blocks


def _core_source(document):
    """Fonte sintética para `docx_meta.core_props`/`docx_filename_unique`."""
    return {
        "id": _norm_ws(_get(document, "document_id", "id", default="documento")),
        "topic": _norm_ws(_get(document, "namespace", default="")) or "geral",
        "body": "",
        "source_type": _norm_ws(_get(document, "doc_kind", "kind", default="knowledge-document")),
        "origin": _norm_ws(_get(document, "origin", default="publicacao wiki-ai")),
        "captured_at": _get(document, "captured_at", "created_at", "generated_at", default=None),
    }


def _core_summary(document, rendered):
    """Resumo específico para dc:description (complemento de D05, nunca substituto)."""
    declared = _norm_ws(_get(document, "summary", default=""))
    if declared:
        return declared
    kind = _norm_ws(_get(document, "doc_kind", "kind", default="unidade documental"))
    revision = _norm_ws(_get(document, "revision_id", "revision", default=""))
    subjects = "; ".join(u["title"] for u in rendered[:3])
    parts = [f"{kind} com {len(rendered)} unidade(s) recuperável(is)"]
    if revision:
        parts.append(f"revisão {revision}")
    if subjects:
        parts.append(f"assuntos: {subjects}")
    return ". ".join(parts) + "."


def build_blocks(document):
    """Blocos IR do documento inteiro. Retorna (blocks, rendered_units)."""
    rendered = _dedupe_anchors(
        [render_unit(u, document) for u in publishable_units(document)]
    )
    blocks = _document_header_blocks(document, rendered)
    for unit in rendered:
        blocks.extend(unit["blocks"])
    return blocks, rendered


def docx_filename(document, taken=None):
    """Nome de arquivo .docx estável para o documento (§10.6: nomes estáveis)."""
    source = _core_source(document)
    if taken is None:
        return docx_meta.docx_filename(source)
    return docx_meta.docx_filename_unique(source, taken)


def render(document):
    """Projeção Word do `KnowledgeDocument` → bytes de um .docx válido.

    Nunca faz parsing do Markdown gerado (§10.1). Estilos nativos vêm de
    `docx_ooxml.STYLES_XML`/`NUMBERING_XML` (D01); estado, versão e escopo
    estão no corpo (D05) e TAMBÉM nas core props.
    """
    blocks, rendered = build_blocks(document)
    source = _core_source(document)
    title = _norm_ws(_get(document, "title", "titulo", default="")) or source["id"]
    core = docx_meta.core_props(source, title, _core_summary(document, rendered))
    return docx_ooxml.build_package(blocks, core)


def render_document(document):
    """Igual a `render`, com o relatório de renderização.

    Retorna (docx_bytes, report); `report["blocked_units"]` e
    `report["skipped_units"]` alimentam o bloqueio de publicação em `release`.
    """
    blocks, rendered = build_blocks(document)
    source = _core_source(document)
    title = _norm_ws(_get(document, "title", "titulo", default="")) or source["id"]
    core = docx_meta.core_props(source, title, _core_summary(document, rendered))
    payload = docx_ooxml.build_package(blocks, core)
    skipped = [
        {"unit_id": _norm_ws(_get(u, "unit_id", default="")), "reason": unpublishable_reason(u)}
        for u in document_units(document) if not is_publishable(u)
    ]
    report = {
        "document_id": _norm_ws(_get(document, "document_id", "id", default="")),
        "revision_id": _norm_ws(_get(document, "revision_id", "revision", default="")),
        "docx_filename": docx_filename(document),
        "units": rendered,
        "blocked_units": [u["unit_id"] for u in rendered if u["blocked"]],
        "skipped_units": skipped,
        "notes": [n for u in rendered for n in u["notes"]],
    }
    return payload, report


def render_plan(plan):
    """`nome do arquivo .docx` → bytes, para todo o plano (irmão de
    `markdown.render_plan`). Nomes únicos e determinísticos."""
    out = {}
    taken = set()
    for doc in _sorted_documents(plan):
        name = docx_filename(doc, taken)
        taken.add(name[:-5])
        out[name] = render(doc)
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# Manifesto
# ---------------------------------------------------------------------------


def plan_documents(plan):
    """Documentos do plano — aceita `PublicationPlan` ou iterável de documentos."""
    docs = _get(plan, "documents", "knowledge_documents", "docs", default=None)
    if docs is None:
        if isinstance(plan, (list, tuple)):
            return list(plan)
        return []
    return _as_list(docs)


def _sorted_documents(plan):
    """Ordem determinística de geração (nome do arquivo não depende do dict)."""
    return sorted(
        plan_documents(plan),
        key=lambda d: (_norm_ws(_get(d, "namespace", default="")),
                       _norm_ws(_get(d, "document_id", "id", default=""))),
    )


def plan_revision(plan):
    """Revisão do plano; cai para a revisão do primeiro documento quando ausente."""
    rev = _norm_ws(_get(plan, "revision_id", "revision", default=""))
    if rev:
        return rev
    for doc in plan_documents(plan):
        rev = _norm_ws(_get(doc, "revision_id", "revision", default=""))
        if rev:
            return rev
    return ""


def render_manifest_word(plan):
    """Manifesto Word: `{unit_id → {docx_filename, heading_anchor, state, fact_ids}}`.

    Espelha `markdown.render_manifest(plan)["units"]` para a conferência de
    equivalência (§10.5). A unidade de mini-contexto aparece em mais de um
    documento com o MESMO id: `docx_filename` é a localização primária e
    `docx_filenames` lista todas — nunca duas unidades concorrentes.

    Chaves extras (`relation_ids`, `revision_id`, `blocked`...) não atrapalham
    a comparação, que é feita por chave.
    """
    manifest = {}
    taken = set()
    revision = plan_revision(plan)
    for doc in _sorted_documents(plan):
        filename = docx_filename(doc, taken)
        taken.add(filename[:-5])
        doc_rev = _norm_ws(_get(doc, "revision_id", "revision", default="")) or revision
        doc_id = _norm_ws(_get(doc, "document_id", "id", default=""))
        rendered = _dedupe_anchors([render_unit(u, doc) for u in publishable_units(doc)])
        for unit in rendered:
            entry = manifest.get(unit["unit_id"])
            if entry is None:
                manifest[unit["unit_id"]] = {
                    "docx_filename": filename,
                    "docx_filenames": [filename],
                    "heading_anchor": unit["anchor"],
                    "heading_anchors": [f"{filename}#{unit['anchor']}"],
                    "state": unit["state"],
                    "state_label": unit["state_label"],
                    "fact_ids": list(unit["fact_ids"]),
                    "relation_ids": list(unit["relation_ids"]),
                    "title": unit["title"],
                    "document_ids": [doc_id],
                    "revision_id": doc_rev,
                    "blocked": unit["blocked"],
                    "blocked_reasons": list(unit["blocked_reasons"]),
                }
            else:
                if filename not in entry["docx_filenames"]:
                    entry["docx_filenames"].append(filename)
                    entry["heading_anchors"].append(f"{filename}#{unit['anchor']}")
                if doc_id not in entry["document_ids"]:
                    entry["document_ids"].append(doc_id)
    return dict(sorted(manifest.items()))


def render_manifest_word_envelope(plan):
    """Manifesto Word no MESMO envelope de `markdown.render_manifest(plan)`.

    Facilita a comparação e o consumo por `release.py`: mesmas chaves de topo,
    `units` com o mapeamento por `unit_id`.
    """
    units = render_manifest_word(plan)
    documents = {}
    taken = set()
    for doc in _sorted_documents(plan):
        filename = docx_filename(doc, taken)
        taken.add(filename[:-5])
        rendered = _dedupe_anchors([render_unit(u, doc) for u in publishable_units(doc)])
        documents[_norm_ws(_get(doc, "document_id", "id", default=""))] = {
            "docx_filename": filename,
            "title": _norm_ws(_get(doc, "title", default="")),
            "doc_kind": _norm_ws(_get(doc, "doc_kind", "kind", default="")),
            "namespace": _norm_ws(_get(doc, "namespace", default="")),
            "anchor_entity_id": _norm_ws(_get(doc, "anchor_entity_id", default="")),
            "unit_ids": [u["unit_id"] for u in rendered],
            "anchors": [u["anchor"] for u in rendered],
            "blocked_unit_ids": [u["unit_id"] for u in rendered if u["blocked"]],
        }
    return {
        "manifest_version": 1,
        "revision_id": plan_revision(plan),
        "namespace": _norm_ws(_get(plan, "namespace", default="")),
        "documents": documents,
        "units": units,
        "blocked_unit_ids": [uid for uid, e in units.items() if e["blocked"]],
    }


#: Nome simétrico ao de `markdown.render_manifest_envelope` — o consumidor
#: chama o mesmo método nos dois módulos.
render_manifest_envelope = render_manifest_word_envelope
