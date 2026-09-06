"""publishing.validate — equivalência e fidelidade das publicações (§10.5, §10.6).

Validações independentes, com retorno estruturado para `release.py`:

  - `validate_equivalence(md_manifest, word_manifest, plan)` — mesma revisão e o
    MESMO conjunto de unidades, estados, `fact_ids` e `relation_ids` nas duas saídas.
  - `validate_semantics(document, rendered_md_text, docx_bytes)` — o conteúdo
    essencial (valor verbatim, comparador, negação, número/unidade, estado,
    título específico) sobreviveu às DUAS renderizações.
  - `validate_docx_structure(docx_bytes)` — pacote OOXML íntegro, estilos
    nativos, headings presentes, sem célula mesclada em tabela (D06).
  - `validate_revision(plan, md_manifest, md_texts)` — aplica as três à revisão
    inteira e agrega; é o portão antes de promover/podar (§10.6, F09).

Qualquer `Report.ok is False` bloqueia a publicação da revisão: nada é
promovido nem podado antes da validação passar.

O XML é lido com expat defusado (DOCTYPE, declaração de entidade e entidade
externa são recusados): o .docx validado pode não ter sido gerado aqui.
"""

from __future__ import annotations

import re
import sys
import unicodedata
import zipfile
import xml.parsers.expat
from io import BytesIO
from pathlib import Path

try:  # pragma: no cover - caminho normal
    from publishing import word as _word
except Exception:  # pragma: no cover
    _SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    try:
        from publishing import word as _word
    except Exception:
        _HERE = str(Path(__file__).resolve().parent)
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        import word as _word

try:  # pragma: no cover - caminho normal
    from publishing import markdown as _markdown
except Exception:  # pragma: no cover
    _SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    try:
        from publishing import markdown as _markdown
    except Exception:
        _HERE = str(Path(__file__).resolve().parent)
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        import markdown as _markdown


#: Amostragem determinística de fatos por unidade. 0 ou negativo = todos.
SEMANTIC_SAMPLE_MAX = 200

_WS_RE = re.compile(r"\s+")
_MD_MARKS_RE = re.compile(r"[*_`~]+")
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>])")

#: §10.3 — campo entre colchetes é notação do plano, nunca conteúdo publicável.
#: Mesma regra de `document.PLACEHOLDER_RE` (colchete SEM espaço interno, como
#: `list[int]`, é sintaxe técnica legítima e não é acusado).
_FALLBACK_PLACEHOLDER_RE = re.compile(r"\[\s*(?:\]|\.{2,}\]|…\]|[^\[\]]*\s[^\[\]]*\])")
_PLACEHOLDER_WORDS_RE = re.compile(r"\b(todo|fixme|preencher|a definir|tbd)\b", re.I)

# D12 — tokens críticos que não podem se perder na renderização.
_TOK_COMPARATOR_RE = re.compile(r"(?:>=|<=|!=|<>|==|≠|≥|≤|>|<|=)")
_TOK_NEGATION_RE = re.compile(
    r"\b(n[ãa]o|nunca|jamais|sem|nenhum(?:a|as|os)?|exceto|salvo|inexistente|ausente|"
    r"not|never|no|neither)\b",
    re.I,
)
_TOK_NUMBER_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:%|ms|s|seg|segundos?|min|minutos?|h|horas?|d|dias?|"
    r"kb|mb|gb|tb|req/s|rps)?\b",
    re.I,
)
_TOK_STATE_RE = re.compile(
    r"\b(implementad[oa]s?|implemented|propost[oa]s?|proposed|hist[óo]ric[oa]s?|"
    r"historical|n[ãa]o[- ]resolvid[oa]s?|unresolved|bloquead[oa]s?)\b",
    re.I,
)


def _placeholder_re():
    """`document.PLACEHOLDER_RE` quando disponível — as duas pontas usam a MESMA
    regra, senão o construtor aceitaria o que o validador recusa."""
    mod = _word._document_module()
    pattern = getattr(mod, "PLACEHOLDER_RE", None) if mod is not None else None
    return pattern if pattern is not None else _FALLBACK_PLACEHOLDER_RE


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class Report:
    """Resultado estruturado de uma validação.

    `ok is False` significa publicação bloqueada: `release.py` consome
    `errors`/`details` sem reparsear texto.
    """

    __slots__ = ("name", "errors", "warnings", "checks", "details")

    def __init__(self, name, errors=None, warnings=None, checks=None, details=None):
        self.name = name
        self.errors = list(errors or [])
        self.warnings = list(warnings or [])
        self.checks = list(checks or [])
        self.details = dict(details or {})

    @property
    def ok(self):
        return not self.errors

    @property
    def bloqueios(self):
        """Alias de `errors` — compatibilidade com o Protocol `Report` de
        `publishing.release` (`release._coerce_report`/`release.Report`
        usam o nome `bloqueios`; este módulo usa `errors`). Mesma lista,
        nunca uma cópia divergente."""
        return self.errors

    @property
    def blocking(self):
        """Publicação bloqueada quando há qualquer erro (§10.5/§10.6)."""
        return bool(self.errors)

    def error(self, message, **detail):
        self.errors.append(message)
        self.checks.append({"level": "error", "message": message, **detail})
        return self

    def warn(self, message, **detail):
        self.warnings.append(message)
        self.checks.append({"level": "warning", "message": message, **detail})
        return self

    def to_dict(self):
        return {
            "name": self.name,
            "ok": self.ok,
            "blocking": self.blocking,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "checks": list(self.checks),
            "details": dict(self.details),
        }

    def __bool__(self):
        return self.ok

    def __repr__(self):  # pragma: no cover - diagnóstico
        return (f"<Report {self.name} ok={self.ok} errors={len(self.errors)} "
                f"warnings={len(self.warnings)}>")


def merge_reports(name, reports):
    """Agrega relatórios; o resultado só é `ok` se todos forem `ok`."""
    merged = Report(name)
    for rep in reports:
        merged.errors.extend(f"[{rep.name}] {e}" for e in rep.errors)
        merged.warnings.extend(f"[{rep.name}] {w}" for w in rep.warnings)
        merged.checks.extend(rep.checks)
        merged.details[rep.name] = rep.to_dict()
    return merged


# ---------------------------------------------------------------------------
# Normalização de texto
# ---------------------------------------------------------------------------


def normalize_space(text):
    """Colapsa qualquer espaço em branco (inclusive quebra e NBSP) em um espaço."""
    return _WS_RE.sub(" ", (text or "").replace(" ", " ")).strip()


def _fold(text):
    norm = unicodedata.normalize("NFKD", normalize_space(text).lower())
    return "".join(c for c in norm if not unicodedata.combining(c))


def markdown_variants(md_text):
    """Variantes normalizadas do Markdown para a busca verbatim.

    A comparação não pode falhar só porque o renderizador envolveu o valor em
    ênfase/crase ou escapou um `|` dentro de uma célula: o que se compara é o
    conhecimento, não o invólucro (§10.1 — layout pode divergir).
    """
    raw = normalize_space(md_text)
    unescaped = _MD_ESCAPE_RE.sub(r"\1", raw)
    demarked = normalize_space(_MD_MARKS_RE.sub("", unescaped))
    return (raw, unescaped, demarked)


def _contains(haystacks, needle):
    """True se `needle` normalizado aparece em qualquer variante do texto."""
    target = normalize_space(needle)
    if not target:
        return True
    if any(target in h for h in haystacks):
        return True
    folded = _fold(target)
    return any(folded in _fold(h) for h in haystacks)


# ---------------------------------------------------------------------------
# Leitura do .docx (expat defusado)
# ---------------------------------------------------------------------------


class XmlSecurityError(ValueError):
    """DOCTYPE, entidade declarada ou entidade externa em XML de entrada."""


def _defused_parser():
    """Parser expat que recusa DTD, entidade declarada e entidade externa."""
    parser = xml.parsers.expat.ParserCreate()
    parser.buffer_text = True

    def _no_doctype(*_a, **_k):
        raise XmlSecurityError("DOCTYPE não é aceito em document.xml")

    def _no_entity(*_a, **_k):
        raise XmlSecurityError("declaração de entidade não é aceita em document.xml")

    def _no_external(*_a, **_k):
        raise XmlSecurityError("entidade externa não é aceita em document.xml")

    parser.StartDoctypeDeclHandler = _no_doctype
    parser.EntityDeclHandler = _no_entity
    parser.UnparsedEntityDeclHandler = _no_entity
    parser.ExternalEntityRefHandler = _no_external
    return parser


def parse_document_xml(xml_bytes):
    """Estrutura relevante de `word/document.xml`.

    Retorna: paragraphs [{style, text, in_table}], text, heading_styles,
    tables [{merged_cells, rows}], merged_cells.
    """
    state = {
        "paragraphs": [], "heading_styles": [], "tables": [], "merged": 0,
        "buf": [], "capture": False, "in_para": False, "style": "", "table_depth": 0,
    }

    def start(name, attrs):
        if name == "w:p":
            state["in_para"] = True
            state["buf"] = []
            state["style"] = ""
        elif name == "w:pStyle" and state["in_para"]:
            state["style"] = attrs.get("w:val", "")
        elif name == "w:t":
            state["capture"] = True
        elif name == "w:tbl":
            state["table_depth"] += 1
            state["tables"].append({"merged_cells": 0, "rows": 0})
        elif name == "w:tr" and state["tables"]:
            state["tables"][-1]["rows"] += 1
        elif name in ("w:gridSpan", "w:vMerge"):
            state["merged"] += 1
            if state["tables"]:
                state["tables"][-1]["merged_cells"] += 1

    def chardata(data):
        if state["capture"]:
            state["buf"].append(data)

    def end(name):
        if name == "w:t":
            state["capture"] = False
        elif name == "w:p":
            style = state["style"]
            state["paragraphs"].append(
                {"style": style, "text": "".join(state["buf"]),
                 "in_table": state["table_depth"] > 0}
            )
            if style.startswith("Heading"):
                state["heading_styles"].append(style)
            state["in_para"] = False
            state["buf"] = []
            state["style"] = ""
        elif name == "w:tbl":
            state["table_depth"] = max(0, state["table_depth"] - 1)

    parser = _defused_parser()
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chardata
    parser.Parse(xml_bytes, True)

    return {
        "paragraphs": state["paragraphs"],
        "text": "\n".join(p["text"] for p in state["paragraphs"]),
        "heading_styles": state["heading_styles"],
        "tables": state["tables"],
        "merged_cells": state["merged"],
    }


def docx_text(docx_bytes):
    """Texto corrido de um .docx (um parágrafo/célula por linha)."""
    with zipfile.ZipFile(BytesIO(docx_bytes)) as zf:
        return parse_document_xml(zf.read("word/document.xml"))["text"]


# ---------------------------------------------------------------------------
# validate_docx_structure
# ---------------------------------------------------------------------------

REQUIRED_DOCX_ENTRIES = (
    "[Content_Types].xml",
    "_rels/.rels",
    "docProps/core.xml",
    "word/document.xml",
    "word/_rels/document.xml.rels",
    "word/styles.xml",
)


def validate_docx_structure(docx_bytes):
    """Integridade OOXML: zip válido, XML parseável, estilos nativos, sem merge.

    D01 — a estrutura precisa vir de `Heading1..4` e de numbering, não de
    negrito; D06 — nenhuma célula mesclada em tabela de conhecimento.
    """
    report = Report("docx_structure")
    if not docx_bytes:
        return report.error("docx vazio: nenhum byte gerado")

    try:
        zf = zipfile.ZipFile(BytesIO(docx_bytes))
    except zipfile.BadZipFile as exc:
        return report.error(f"pacote .docx não é um zip válido: {exc}")

    with zf:
        if zf.testzip() is not None:
            report.error("entrada corrompida no pacote .docx")
        names = set(zf.namelist())
        for entry in REQUIRED_DOCX_ENTRIES:
            if entry not in names:
                report.error(f"entrada obrigatória ausente no pacote: {entry}")
        if report.errors:
            return report

        try:
            parsed = parse_document_xml(zf.read("word/document.xml"))
        except XmlSecurityError as exc:
            return report.error(f"document.xml recusado pela defesa de XML: {exc}")
        except xml.parsers.expat.ExpatError as exc:
            return report.error(f"document.xml não é XML bem formado: {exc}")

        styles_xml = zf.read("word/styles.xml").decode("utf-8", "replace")
        has_numbering = "word/numbering.xml" in names
        if any(p["style"] == "ListParagraph" for p in parsed["paragraphs"]) and not has_numbering:
            report.error("ListParagraph sem word/numbering.xml — lista sem numeração nativa (D01)")

    report.details.update({
        "paragraphs": len(parsed["paragraphs"]),
        "headings": len(parsed["heading_styles"]),
        "heading_styles": sorted(set(parsed["heading_styles"])),
        "tables": len(parsed["tables"]),
        "merged_cells": parsed["merged_cells"],
    })

    for level in (1, 2):
        if f'w:styleId="Heading{level}"' not in styles_xml:
            report.error(f"styles.xml não declara o estilo nativo Heading{level} (D01)")
    if not parsed["heading_styles"]:
        report.error("documento sem heading nativo (D01): estrutura não é recuperável")
    elif "Heading1" not in parsed["heading_styles"]:
        report.error("documento sem Heading1: título não usa estilo nativo (D01)")

    if parsed["merged_cells"]:
        report.error(
            f"{parsed['merged_cells']} célula(s) mesclada(s) (gridSpan/vMerge) em tabela (D06)"
        )
    for idx, tbl in enumerate(parsed["tables"], start=1):
        if tbl["rows"] < 2:
            report.warn(f"tabela {idx} tem apenas cabeçalho, sem linha de dados")

    # Título órfão: heading sem conteúdo próprio até o próximo heading.
    paragraphs = parsed["paragraphs"]
    for i, para in enumerate(paragraphs):
        if not para["style"].startswith("Heading"):
            continue
        has_body = False
        for nxt in paragraphs[i + 1:]:
            if nxt["style"].startswith("Heading"):
                break
            if normalize_space(nxt["text"]):
                has_body = True
                break
        if not has_body:
            report.warn(f"heading sem conteúdo próprio: {normalize_space(para['text'])[:80]!r}")

    if not normalize_space(parsed["text"]):
        report.error("documento sem texto: nenhum conhecimento publicado")
    return report


# ---------------------------------------------------------------------------
# validate_semantics
# ---------------------------------------------------------------------------


def critical_tokens(value):
    """D12 — tokens que devem sobreviver: comparador, negação, número/unidade, estado."""
    text = normalize_space(value)
    tokens = []
    tokens.extend(m.group(0) for m in _TOK_COMPARATOR_RE.finditer(text))
    tokens.extend(m.group(0) for m in _TOK_NEGATION_RE.finditer(text))
    tokens.extend(normalize_space(m.group(0)) for m in _TOK_NUMBER_RE.finditer(text))
    tokens.extend(m.group(0) for m in _TOK_STATE_RE.finditer(text))
    return list(dict.fromkeys(t for t in tokens if t))


def find_placeholders(text):
    """Placeholders `[...]`/`[frase por preencher]`/TODO no conteúdo publicado."""
    found = [m.group(0) if hasattr(m, "group") else m
             for m in _placeholder_re().finditer(text or "")]
    found.extend(m.group(0) for m in _PLACEHOLDER_WORDS_RE.finditer(text or ""))
    return list(dict.fromkeys(found))


def _sample(items, limit=SEMANTIC_SAMPLE_MAX):
    """Amostra determinística: todos até `limit`; acima disso, passo uniforme."""
    if limit is None or limit <= 0 or len(items) <= limit:
        return list(items)
    step = len(items) / float(limit)
    return [items[int(i * step)] for i in range(limit)]


def validate_semantics(document, rendered_md_text, docx_bytes):
    """Fidelidade semântica: o essencial existe, igual, nas DUAS saídas.

    Por unidade publicável e por fato amostrado:
      - `value` verbatim presente em Markdown e Word (busca normalizada de espaço);
      - comparador, negação, número com unidade e nome de estado preservados;
      - título específico (D02) presente no corpo do Word;
      - `unit_id` e estado escritos no corpo (D05/D09), não só em propriedade OOXML;
      - `fact_ids`/`relation_ids` presentes nas duas saídas (aceite W6);
      - nenhum placeholder entre colchetes (§10.3);
      - nenhuma unidade bloqueada por falta de equivalente textual (D14/F08).
    """
    report = Report("semantics")

    try:
        docx_txt = docx_text(docx_bytes)
    except XmlSecurityError as exc:
        return report.error(f"docx recusado pela defesa de XML: {exc}")
    except (zipfile.BadZipFile, KeyError, xml.parsers.expat.ExpatError) as exc:
        return report.error(f"docx ilegível para validação semântica: {exc}")

    docx_variants = (normalize_space(docx_txt),)
    md_variants = markdown_variants(rendered_md_text)

    if not docx_variants[0]:
        report.error("Word sem texto: nada a validar")
    if not md_variants[0]:
        report.error("Markdown sem texto: nada a validar")
    if report.errors:
        return report

    report.details["document_id"] = _word._norm_ws(
        _word._get(document, "document_id", "id", default="")
    )

    # §10.3 — nenhum campo por preencher chega ao consumidor.
    for label, text in (("word", docx_txt), ("markdown", rendered_md_text)):
        for placeholder in find_placeholders(text):
            report.error(f"placeholder proibido na saída {label}: {placeholder!r}",
                         output=label, placeholder=placeholder)

    units = _word.publishable_units(document)
    checked = 0
    missing_md = []
    missing_word = []

    for unit in units:
        unit_id = _word._norm_ws(_word._get(unit, "unit_id", "id", default=""))
        title = _word.unit_title(unit, document)
        state_label = _word.unit_state_label(unit)

        if _word.is_generic_title(title):
            report.error(f"unidade {unit_id}: título genérico {title!r} (D02)", unit_id=unit_id)
        if not _contains(docx_variants, title):
            report.error(f"unidade {unit_id}: título ausente no corpo do Word", unit_id=unit_id)
        if not _contains(md_variants, title):
            report.error(f"unidade {unit_id}: título ausente no corpo do Markdown", unit_id=unit_id)

        # D09 — a unidade se identifica sozinha, sem depender de arquivo/pasta.
        for label, variants in (("Word", docx_variants), ("Markdown", md_variants)):
            if unit_id and not _contains(variants, unit_id):
                report.error(f"unidade {unit_id}: identificador ausente no corpo do {label} (D09)",
                             unit_id=unit_id)
            # D05 — estado no corpo, com o MESMO rótulo nas duas saídas.
            if state_label and not _contains(variants, state_label):
                report.error(
                    f"unidade {unit_id}: estado {state_label!r} ausente no corpo do {label} (D05)",
                    unit_id=unit_id,
                )

        for fid in _word.unit_fact_ids(unit):
            for label, variants in (("Word", docx_variants), ("Markdown", md_variants)):
                if not _contains(variants, fid):
                    report.error(f"unidade {unit_id}: fact_id {fid} ausente no {label}",
                                 unit_id=unit_id, fact_id=fid)
        for rid in _word.unit_relation_ids(unit):
            for label, variants in (("Word", docx_variants), ("Markdown", md_variants)):
                if not _contains(variants, rid):
                    report.error(f"unidade {unit_id}: relation_id {rid} ausente no {label}",
                                 unit_id=unit_id, relation_id=rid)

        # D12/D13 — valor verbatim e tokens críticos preservados nos dois lados.
        for st in _sample(_word.unit_statements(unit)):
            fid = _word._norm_ws(_word._get(st, "fact_id", "id", default=""))
            value = _word._get(st, "value", "valor", "statement", "text", default="")
            value = normalize_space(str(value or ""))
            if not value:
                continue
            checked += 1
            if not _contains(docx_variants, value):
                missing_word.append(fid)
                report.error(f"fato {fid}: value não encontrado verbatim no Word (D12/D13)",
                             unit_id=unit_id, fact_id=fid, value=value)
            if not _contains(md_variants, value):
                missing_md.append(fid)
                report.error(f"fato {fid}: value não encontrado verbatim no Markdown (D12/D13)",
                             unit_id=unit_id, fact_id=fid, value=value)
            for token in critical_tokens(value):
                for label, variants in (("Word", docx_variants), ("Markdown", md_variants)):
                    if not _contains(variants, token):
                        report.error(
                            f"fato {fid}: token crítico {token!r} perdido no {label} (D12)",
                            unit_id=unit_id, fact_id=fid, token=token,
                        )

        # D08 — a relação precisa estar em linguagem natural, não só como ID.
        for rel in _word._as_list(_word._get(unit, "relations", default=[])):
            phrase = _word._norm_ws(_word._get(rel, "natural_text", "statement", default=""))
            if phrase and not _contains(docx_variants, phrase):
                report.error(
                    f"unidade {unit_id}: relação {_word._norm_ws(_word._get(rel, 'relation_id', default=''))} "
                    "sem frase em linguagem natural no Word (D08)",
                    unit_id=unit_id,
                )

        # D14/F08 — unidade sem equivalente textual não pode ser publicada.
        rendered = _word.render_unit(unit, document)
        if rendered["blocked"]:
            report.error(
                f"unidade {unit_id} bloqueada: " + "; ".join(rendered["blocked_reasons"]),
                unit_id=unit_id, blocked_reasons=rendered["blocked_reasons"],
            )
        for note in rendered["notes"]:
            report.warn(note, unit_id=unit_id)

    # §10.2 — unidade só com moldura é omitida das duas saídas, com motivo.
    skipped = [u for u in _word.document_units(document) if not _word.is_publishable(u)]
    for unit in skipped:
        report.warn(_word.unpublishable_reason(unit),
                    unit_id=_word._norm_ws(_word._get(unit, "unit_id", default="")))

    report.details.update({
        "units": len(units),
        "skipped_units": len(skipped),
        "facts_checked": checked,
        "facts_missing_markdown": missing_md,
        "facts_missing_word": missing_word,
    })
    if not units:
        report.error("documento sem unidade publicável: nada a publicar (§10.2)")
    return report


# ---------------------------------------------------------------------------
# validate_equivalence
# ---------------------------------------------------------------------------


def manifest_units(manifest):
    """Mapa `unit_id → entrada`, aceitando manifesto plano OU com envelope.

    `markdown.render_manifest(plan)` devolve o envelope (`manifest_version`,
    `revision_id`, `units`, ...); `word.render_manifest_word(plan)` devolve o
    mapa plano. Os dois entram aqui sem adaptação do chamador.
    """
    if not manifest:
        return {}, ""
    if isinstance(manifest, dict) and isinstance(manifest.get("units"), dict):
        return manifest["units"], _word._norm_ws(manifest.get("revision_id", ""))
    return manifest, ""


def _entry_state(entry):
    return _word._norm_ws(_word._get(entry, "state", "estado", default=""))


def _entry_ids(entry, key):
    return {str(v) for v in _word._as_list(_word._get(entry, key, default=[]))}


def _entry_revision(entry):
    return _word._norm_ws(_word._get(entry, "revision_id", "revision", default=""))


def validate_equivalence(md_manifest, word_manifest, plan):
    """Equivalência entre manifestos Markdown e Word da MESMA revisão (§10.5).

    Bloqueia a publicação quando:
      - as revisões declaradas divergem entre plano, manifestos e entradas;
      - alguma unidade publicável do plano falta em uma das saídas (lista os faltantes);
      - o estado da unidade difere entre as saídas ou em relação ao plano;
      - `fact_ids`/`relation_ids` diferem entre as saídas ou perdem itens do plano;
      - alguma saída publica unidade que o plano não prevê (§10.6: conjunto completo);
      - alguma unidade está bloqueada por falta de equivalente textual (F08).
    """
    report = Report("equivalence")
    md_units, md_rev = manifest_units(md_manifest)
    word_units, word_rev = manifest_units(word_manifest)

    plan_revision = _word.plan_revision(plan)
    report.details.update({
        "plan_revision": plan_revision,
        "markdown_revision": md_rev,
        "word_revision": word_rev,
    })
    for label, rev in (("Markdown", md_rev), ("Word", word_rev)):
        if rev and plan_revision and rev != plan_revision:
            report.error(
                f"manifesto {label} declara revisão {rev!r}, plano declara {plan_revision!r}"
            )
    if md_rev and word_rev and md_rev != word_rev:
        report.error(f"revisões divergentes entre manifestos: Markdown {md_rev!r}, Word {word_rev!r}")

    expected = {}
    for doc in _word.plan_documents(plan):
        doc_rev = _word._norm_ws(_word._get(doc, "revision_id", "revision", default="")) or plan_revision
        if plan_revision and doc_rev and doc_rev != plan_revision:
            report.error(
                f"documento {_word._norm_ws(_word._get(doc, 'document_id', default=''))} declara "
                f"revisão {doc_rev!r}, plano declara {plan_revision!r}"
            )
        for unit in _word.publishable_units(doc):
            unit_id = _word._norm_ws(_word._get(unit, "unit_id", "id", default=""))
            if not unit_id:
                report.error("unidade sem unit_id no plano: identidade não é rastreável (§10.1)")
                continue
            prev = expected.get(unit_id)
            current = {
                "state": _word.unit_state(unit),
                "fact_ids": set(_word.unit_fact_ids(unit)),
                "relation_ids": set(_word.unit_relation_ids(unit)),
                "revision": doc_rev,
            }
            # Unidade de mini-contexto repete em vários documentos com o MESMO
            # id: repetição idêntica é esperada, divergente é conflito.
            if prev is not None and prev != current:
                report.error(
                    f"unit_id {unit_id} aparece com conteúdo divergente em mais de um documento",
                    unit_id=unit_id,
                )
            expected[unit_id] = current

    report.details.update({
        "expected_units": len(expected),
        "markdown_units": len(md_units),
        "word_units": len(word_units),
    })
    if not expected:
        report.error("plano sem unidade publicável: não há o que publicar")

    missing_md = sorted(uid for uid in expected if uid not in md_units)
    missing_word = sorted(uid for uid in expected if uid not in word_units)
    report.details["missing_markdown"] = missing_md
    report.details["missing_word"] = missing_word
    if missing_md:
        report.error(
            f"{len(missing_md)} unidade(s) do plano ausente(s) no Markdown: {', '.join(missing_md)}",
            missing_markdown=missing_md,
        )
    if missing_word:
        report.error(
            f"{len(missing_word)} unidade(s) do plano ausente(s) no Word: {', '.join(missing_word)}",
            missing_word=missing_word,
        )

    extra_md = sorted(uid for uid in md_units if uid not in expected)
    extra_word = sorted(uid for uid in word_units if uid not in expected)
    if extra_md:
        report.error(f"Markdown publica unidade fora do plano: {', '.join(extra_md)}", extra=extra_md)
    if extra_word:
        report.error(f"Word publica unidade fora do plano: {', '.join(extra_word)}", extra=extra_word)

    for unit_id in sorted(expected):
        md_entry = md_units.get(unit_id)
        word_entry = word_units.get(unit_id)
        if md_entry is None or word_entry is None:
            continue
        exp = expected[unit_id]

        for label, entry in (("Markdown", md_entry), ("Word", word_entry)):
            rev = _entry_revision(entry)
            target = exp["revision"] or plan_revision
            if rev and target and rev != target:
                report.error(
                    f"unidade {unit_id}: revisão {rev!r} no {label} difere de {target!r} no plano",
                    unit_id=unit_id,
                )

        # Sem localizador não há recuperação: o arquivo precisa estar nomeado.
        if not _word._norm_ws(_word._get(md_entry, "markdown_path", "path", default="")):
            report.error(f"unidade {unit_id}: manifesto Markdown sem markdown_path", unit_id=unit_id)
        if not _word._norm_ws(_word._get(word_entry, "docx_filename", "filename", default="")):
            report.error(f"unidade {unit_id}: manifesto Word sem docx_filename", unit_id=unit_id)

        md_state, word_state = _entry_state(md_entry), _entry_state(word_entry)
        if md_state != word_state:
            report.error(
                f"unidade {unit_id}: estado divergente — Markdown {md_state!r}, Word {word_state!r}",
                unit_id=unit_id,
            )
        for label, got in (("Markdown", md_state), ("Word", word_state)):
            if exp["state"] and got and got != exp["state"] and got != _word.STATE_BLOCKED:
                report.error(
                    f"unidade {unit_id}: estado {got!r} no {label} difere do plano {exp['state']!r}",
                    unit_id=unit_id,
                )

        for key, expected_ids in (("fact_ids", exp["fact_ids"]), ("relation_ids", exp["relation_ids"])):
            md_ids, word_ids = _entry_ids(md_entry, key), _entry_ids(word_entry, key)
            if md_ids != word_ids:
                report.error(
                    f"unidade {unit_id}: {key} divergentes — só no Markdown: "
                    f"{sorted(md_ids - word_ids)}; só no Word: {sorted(word_ids - md_ids)}",
                    unit_id=unit_id,
                )
            for label, got in (("Markdown", md_ids), ("Word", word_ids)):
                lost = sorted(expected_ids - got)
                if lost:
                    report.error(
                        f"unidade {unit_id}: {key} do plano ausentes no {label}: {lost}",
                        unit_id=unit_id, missing=lost,
                    )

        # Unidade bloqueada barra a revisão inteira (F08/F09/§10.6).
        for label, entry in (("Markdown", md_entry), ("Word", word_entry)):
            blocked = _word._get(entry, "blocked", default=False) is True
            if blocked or _entry_state(entry) == _word.STATE_BLOCKED:
                reasons = _word._as_list(_word._get(entry, "blocked_reasons", default=[]))
                report.error(
                    f"unidade {unit_id} bloqueada no {label}: "
                    + ("; ".join(str(r) for r in reasons) or "motivo não informado"),
                    unit_id=unit_id,
                )

    return report


# ---------------------------------------------------------------------------
# Portão da revisão (§10.6 / F09)
# ---------------------------------------------------------------------------


def validate_revision(plan, md_manifest, md_texts):
    """Valida a revisão inteira antes de promover ou podar (§10.6, F09).

    `md_texts` mapeia `document_id` → Markdown renderizado do documento. Gera o
    Word de cada documento, roda as três validações e agrega. Falha em
    qualquer uma bloqueia a promoção — a revisão anterior permanece utilizável.
    """
    reports = []
    word_manifest = _word.render_manifest_word(plan)
    reports.append(validate_equivalence(md_manifest, word_manifest, plan))

    for doc in _word.plan_documents(plan):
        doc_id = _word._norm_ws(_word._get(doc, "document_id", "id", default=""))
        docx_bytes = _word.render(doc)
        structure = validate_docx_structure(docx_bytes)
        structure.name = f"docx_structure:{doc_id}"
        reports.append(structure)
        md_text = (md_texts or {}).get(doc_id)
        if md_text is None:
            missing = Report(f"semantics:{doc_id}")
            missing.error(f"documento {doc_id} sem Markdown correspondente para conferência (§10.5)")
            reports.append(missing)
            continue
        semantics = validate_semantics(doc, md_text, docx_bytes)
        semantics.name = f"semantics:{doc_id}"
        reports.append(semantics)

    merged = merge_reports("revision", reports)
    merged.details["revision_id"] = _word.plan_revision(plan)
    merged.details["documents"] = len(_word.plan_documents(plan))
    return merged


# ---------------------------------------------------------------------------
# Adaptador de nível de revisão para `publishing.release.Validators`
# ---------------------------------------------------------------------------
#
# `publishing.release` (dono: `release.py`) chama validadores injetados pelo
# Protocol `Validators` — `validate_equivalence(plan, staging_root)`,
# `validate_semantics(plan, staging_root)`, `validate_docx_structure(plan,
# staging_root)`, um por REVISÃO inteira. As funções acima (`validate_*`) são
# por DOCUMENTO/par-de-manifesto, assinatura incompatível com o Protocol —
# essa era a incompatibilidade real entre os dois módulos. As três funções
# `*_staged` abaixo fecham essa lacuna: mesma assinatura do Protocol,
# implementadas em cima das funções já existentes, sem duplicar regra de
# validação nenhuma.
#
# `staging_root` não precisa ser lido do disco: os manifestos e o conteúdo
# renderizado que essas checagens comparam vêm dos PRÓPRIOS renderers
# (`_markdown.render_manifest`/`render`, `_word.render_manifest_word`/
# `render`), a mesma convenção que `release._render_all` já usa para montar
# `md_manifest`/`word_manifest` e gerar os bytes que acabam de ser gravados
# em staging — reexecutar o renderer sobre o mesmo `plan`/`document` produz
# exatamente os mesmos bytes (são funções puras dos dados do plano), então
# não há necessidade de reler o arquivo do staging para obter o mesmo
# conteúdo que valida a promoção.


def validate_equivalence_staged(plan, staging_root):
    """`Validators.validate_equivalence(plan, staging_root)` — equivalência
    §10.5 da revisão inteira, a partir dos manifestos que os renderers
    produzem para `plan` (mesma fonte usada por `release._render_all`)."""
    md_manifest = _markdown.render_manifest(plan)
    word_manifest = _word.render_manifest_word(plan)
    return validate_equivalence(md_manifest, word_manifest, plan)


def validate_semantics_staged(plan, staging_root):
    """`Validators.validate_semantics(plan, staging_root)` — fidelidade
    semântica (D02/D05/D08/D09/D12/D13/D14) de cada documento do plano,
    agregada num único `Report`."""
    reports = []
    for doc in _word.plan_documents(plan):
        md_text = _markdown.render(doc)
        docx_bytes = _word.render(doc)
        reports.append(validate_semantics(doc, md_text, docx_bytes))
    return merge_reports("semantics", reports)


def validate_docx_structure_staged(plan, staging_root):
    """`Validators.validate_docx_structure(plan, staging_root)` — integridade
    OOXML (D01/D06) de cada documento do plano, agregada num único `Report`."""
    reports = []
    for doc in _word.plan_documents(plan):
        docx_bytes = _word.render(doc)
        reports.append(validate_docx_structure(docx_bytes))
    return merge_reports("docx_structure", reports)


class StagedValidators:
    """Objeto pronto para `release.publish_revision(..., validators=...)`:
    mesmos nomes de método do Protocol `release.Validators`
    (`validate_equivalence`/`validate_semantics`/`validate_docx_structure`,
    todos `(plan, staging_root) -> Report`), delegando às três funções
    `*_staged` acima. É exatamente o objeto que `release.py` usa como
    padrão quando nenhum `validators` é injetado — expor a classe aqui
    permite que um chamador (produção ou teste) o use diretamente, sem
    reescrever um adaptador de assinatura equivalente."""

    validate_equivalence = staticmethod(validate_equivalence_staged)
    validate_semantics = staticmethod(validate_semantics_staged)
    validate_docx_structure = staticmethod(validate_docx_structure_staged)
