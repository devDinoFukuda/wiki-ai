"""docx_meta — Onda C: nomenclatura, título, resumo e metadados core do .docx.

Referência normativa: docs/plano-execucao-docx-ondas.md §0 (contrato IR) e
ONDA C (§C.1-§C.4); docs/plano-wiki-docx.md §3-§4.

Só stdlib. Não importa `wk.cli` (evita import circular; mantém o módulo
puro e sem dependência do resto de `wk`, `codescan` ou `sbindex`).
"""

import hashlib
import re
import unicodedata

# ---------------------------------------------------------------------------
# Constantes de módulo
# ---------------------------------------------------------------------------

# Marcador de reingestão (M7 do plano funcional) — guarda em `dc:identifier`.
DOCX_GERADO_IDENTIFIER = "wk-docx-gerado"

# Prefixos boilerplate normalizados (minúsculas, espaço colapsado) dos
# geradores determinísticos do codescan. Verificados literalmente contra:
#   - scripts/codescan/export.py:155-157 (render_inventory)
#   - scripts/codescan/export.py:173-177 (render_dependencies)
# Texto fixo, sem interpolação, idêntico entre repositórios.
BOILERPLATE_PREFIXES: tuple[str, ...] = (
    "gerado deterministicamente pelo `codescan export`",
    "dependências **declaradas** nos manifests do repositório",
)

# Seções nomeadas prioritárias para derive_summary (§C.3 cascata, opção D
# — decisão do usuário). Texto de heading normalizado (minúsculas, sem
# acento, sem pontuação) que, se casar, faz o primeiro parágrafo corrido
# logo abaixo virar o resumo, sem exigir menção literal a repo/subject/
# topic (2ª defesa removida — rejeitava resumos legítimos, ver ADR ligado
# ao defeito de sdd/coupling.md). Ajustável: acrescente sinônimos aqui.
SUMMARY_SECTIONS: frozenset[str] = frozenset(
    {"resumo", "visao geral", "sumario", "sintese", "overview"}
)

# Padrões para extrair subject/scope/repo/artifact a partir do id (§C.1, §4).
_ID_PATTERN_CODESCAN = re.compile(
    r"^sb-codescan-(?P<repo>.+)-(?P<artifact>inventory|dependencies|coupling)$"
)
_ID_PATTERN_PUBLISH = re.compile(
    r"^sb-publish-(?P<repo>.+?)-(?P<rest>(?:modules|sdd)-.+)$"
)
_ID_FALLBACK_PREFIX = re.compile(r"^sb-[a-z]+-")

# Sanitização e truncamento de stem de nome de arquivo (§4 passos 4-5;
# SP5+SP6: cobre reservado Windows ∪ SharePoint, não só Windows).
_FORBIDDEN_CHARS = re.compile(r'["*:<>?/\\|~#%&{}]')
# Stems reservados já normalizados na forma pós-limpeza de _sanitize_stem
# (minúsculas, sem ponto/traço nas pontas — por isso ".lock" aparece aqui
# como "lock": o ponto inicial é removido pela regra SP6 antes desta
# comparação, então o literal com ponto nunca sobreviveria ao pipeline).
_RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    "com0", "com1", "com2", "com3", "com4",
    "com5", "com6", "com7", "com8", "com9",
    "lpt0", "lpt1", "lpt2", "lpt3", "lpt4",
    "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    "lock", "desktop.ini",
}

# Título: heading no topo do body (§C.2) e limpeza de markdown inline.
_HEADING_RE = re.compile(r"^#{1,2}\s+(.+?)\s*#*\s*$")
_INLINE_MD_RE = re.compile(r"\*\*|`|\*")

# Resumo: fronteira de frase e limite de truncamento (§C.3).
_SENTENCE_BOUNDARY_CHARS = (".", "!", "?")
_SUMMARY_LIMIT = 600


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------

# --- _slug -------------------------------------------------------------
# Cópia literal de scripts/wk/cli.py:945-949. Duplicada aqui (em vez de
# importada) por decisão explícita de docs/plano-execucao-docx-ondas.md
# ONDA C: evitar import circular entre `wk.cli` e `wk.docx_meta` e manter
# este módulo puro (só stdlib, sem dependência do resto de `wk`).


def _slug(s: str) -> str:
    s = (s or "geral").strip().lower()
    s = re.sub(r"[^a-z0-9._/-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-/")
    return s or "geral"


def _extract_id_parts(source_id: str) -> dict:
    """Extrai subject/scope/repo/artifact do id (§C.1, §4).

    Contrato interno consumido pelas demais funções do módulo. Chaves
    sempre presentes no retorno: subject (str), scope (str | None),
    repo (str | None), artifact (str | None).
    """
    sid = source_id or ""

    m = _ID_PATTERN_CODESCAN.match(sid)
    if m:
        repo = m.group("repo")
        artifact = m.group("artifact")
        return {"subject": artifact, "scope": repo, "repo": repo, "artifact": artifact}

    m = _ID_PATTERN_PUBLISH.match(sid)
    if m:
        repo = m.group("repo")
        rest = m.group("rest")
        return {"subject": rest, "scope": repo, "repo": repo, "artifact": None}

    subject = _ID_FALLBACK_PREFIX.sub("", sid)
    return {"subject": subject, "scope": None, "repo": None, "artifact": None}


def _sanitize_stem(stem: str) -> str:
    """Sanitiza stem para nome de arquivo Windows+SharePoint-safe (§4 passo
    4; SP5+SP6). Ordem: minúsculas -> `~$` inicial removido -> caracteres
    proibidos viram `-` (nunca removidos — remover colapsaria segmentos
    separados por `/`, ex. domínio+escopo do topic, ver defeito SP5) ->
    `..`+ vira `-` -> `-` repetido colapsa -> ponto/traço nas pontas
    removido (SP6) -> stems reservados e `_vti_` (substring, SP6) ganham
    sufixo `-doc`.
    """
    s = (stem or "").lower()
    if s.startswith("~$"):
        s = s[2:]
    s = _FORBIDDEN_CHARS.sub("-", s)
    s = s.replace(" ", "-")
    s = re.sub(r"\.{2,}", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("- .")
    if not s:
        s = "documento"
    if s in _RESERVED_STEMS or "_vti_" in s:
        s = f"{s}-doc"
    return s


def _truncate_stem(stem: str) -> str:
    """Trunca em 120 chars preservando o final (§4 passo 5)."""
    if len(stem) <= 120:
        return stem
    return stem[:60] + "-" + stem[-59:]


def _iter_plain_paragraphs(body: str):
    """Devolve os parágrafos de texto corrido do `body`, na ordem em que aparecem.

    Ignora heading, item de lista, linha de tabela, fence, citação, front
    matter e regra horizontal — defesa em profundidade mesmo quando o corpo
    já vem sem front matter (`_promoted_raw_sources` já remove).
    """
    lines = (body or "").split("\n")
    n = len(lines)
    i = 0

    # Front matter: se o texto começa com "---", descarta até o próximo "---".
    if n and lines[0].strip() == "---":
        j = 1
        while j < n and lines[j].strip() != "---":
            j += 1
        if j < n:
            i = j + 1

    paragraphs: list[str] = []
    buf: list[str] = []
    in_fence = False

    def _flush():
        if buf:
            paragraphs.append(" ".join(buf).strip())
            buf.clear()

    heading_re = re.compile(r"^#{1,6}(\s|$)")
    list_re = re.compile(r"^([-*+]|\d+[.)])\s")
    quote_re = re.compile(r"^>\s?")
    hr_re = re.compile(r"^(-{3,}|_{3,}|\*{3,})\s*$")

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if in_fence:
            if stripped.startswith("```"):
                in_fence = False
            i += 1
            continue

        if stripped.startswith("```"):
            in_fence = True
            _flush()
            i += 1
            continue

        if stripped == "":
            _flush()
            i += 1
            continue

        if heading_re.match(stripped):
            _flush()
            i += 1
            continue

        if list_re.match(stripped):
            _flush()
            i += 1
            continue

        if stripped.startswith("|"):
            _flush()
            i += 1
            continue

        if quote_re.match(stripped):
            _flush()
            i += 1
            continue

        if hr_re.match(stripped):
            _flush()
            i += 1
            continue

        buf.append(stripped)
        i += 1

    _flush()
    return [p for p in paragraphs if p]


def _normalize_ws(text: str) -> str:
    """Normaliza para comparação: minúsculas, espaços colapsados."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _is_boilerplate(normalized_paragraph: str) -> bool:
    """Testa se o parágrafo normalizado começa com boilerplate conhecido."""
    return any(normalized_paragraph.startswith(p) for p in BOILERPLATE_PREFIXES)


def _normalize_heading_text(text: str) -> str:
    """Normaliza texto de heading p/ comparar contra SUMMARY_SECTIONS:
    remove markdown inline, minúsculas, sem acento, sem pontuação."""
    t = _INLINE_MD_RE.sub("", text or "")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _named_section_summary(body: str) -> str | None:
    """§C.3 cascata passo 1 (opção D): heading (`#`..`######`) cujo texto
    normalizado esteja em SUMMARY_SECTIONS -> primeiro parágrafo corrido
    da seção (até o próximo heading de qualquer nível). None se não houver
    heading nomeado, ou a seção não tiver parágrafo corrido não-boilerplate.
    """
    lines = (body or "").split("\n")
    n = len(lines)
    i = 0

    # Front matter: mesmo tratamento de _iter_plain_paragraphs.
    if n and lines[0].strip() == "---":
        j = 1
        while j < n and lines[j].strip() != "---":
            j += 1
        if j < n:
            i = j + 1

    heading_re = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
    any_heading_re = re.compile(r"^#{1,6}(\s|$)")
    in_fence = False

    while i < n:
        stripped = lines[i].strip()

        if in_fence:
            if stripped.startswith("```"):
                in_fence = False
            i += 1
            continue

        if stripped.startswith("```"):
            in_fence = True
            i += 1
            continue

        m = heading_re.match(stripped)
        if m and _normalize_heading_text(m.group(1)) in SUMMARY_SECTIONS:
            j = i + 1
            while j < n and not any_heading_re.match(lines[j].strip()):
                j += 1
            section_body = "\n".join(lines[i + 1 : j])
            for paragraph in _iter_plain_paragraphs(section_body):
                if not _is_boilerplate(_normalize_ws(paragraph)):
                    return paragraph
            return None  # heading achado mas sem parágrafo corrido válido

        i += 1

    return None


def _truncate_at_boundary(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    """Trunca `text` em `limit` chars na fronteira de frase; senão, de palavra."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = -1
    for ch in _SENTENCE_BOUNDARY_CHARS:
        idx = window.rfind(ch)
        if idx > cut:
            cut = idx
    if cut >= 0:
        return window[: cut + 1].rstrip() + "…"
    idx = window.rfind(" ")
    if idx > 0:
        return window[:idx].rstrip() + "…"
    return window.rstrip() + "…"


def _parse_w3cdtf(value) -> str | None:
    """Normaliza `value` para W3CDTF (`YYYY-MM-DDThh:mm:ssZ`); None se não parseável."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", v):
        return v
    if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        return f"{v}T00:00:00Z"
    return None


# ---------------------------------------------------------------------------
# API pública (assinaturas congeladas em docs/plano-execucao-docx-ondas.md §0.3)
# ---------------------------------------------------------------------------


def docx_filename(source: dict) -> str:
    """Gera nome de arquivo .docx determinístico a partir da fonte (§C.1)."""
    domain = _slug(source["topic"] or "geral")
    parts = _extract_id_parts(source["id"])
    subject = parts["subject"]
    scope = parts["scope"]
    stem = f"{domain}-{subject}-{scope}" if scope else f"{domain}-{subject}"
    stem = _sanitize_stem(stem)
    stem = _truncate_stem(stem)
    return f"{stem}.docx"


def docx_filename_unique(source: dict, taken: set) -> str:
    """Nome .docx único e determinístico; não consulta disco (§C.1).

    `taken` contém stems (sem extensão) já usados nesta execução.
    """
    name = docx_filename(source)
    stem = name[:-5]
    if stem not in taken:
        return name
    digest = hashlib.sha1(source["id"].encode("utf-8")).hexdigest()
    short_stem = f"{stem}-{digest[:8]}"
    if short_stem not in taken:
        return f"{short_stem}.docx"
    return f"{stem}-{digest}.docx"


def derive_title(source: dict) -> str:
    """Deriva título por cascata: H1/H2 -> id reconhecido -> id humanizado
    -> placeholder fixo (§C.2). Nunca devolve string vazia.
    """
    body = source.get("body") or ""
    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if not m:
            continue
        title = _INLINE_MD_RE.sub("", m.group(1)).strip()
        if title:
            return title[:250]

    parts = _extract_id_parts(source["id"])
    if parts["repo"] and parts["artifact"]:
        return f"{parts['artifact'].capitalize()} — {parts['repo']}"[:250]

    humanized = _ID_FALLBACK_PREFIX.sub("", source["id"])
    humanized = humanized.replace("-", " ").replace("_", " ").strip()
    if humanized:
        humanized = humanized[0].upper() + humanized[1:]
        return humanized[:250]

    return "Documento sem título"


def derive_summary(source: dict) -> tuple[str, bool]:
    """Deriva o resumo de uma fonte promovida por cascata (§C.3, opção D):
    1) primeiro parágrafo corrido sob heading de SUMMARY_SECTIONS;
    2) primeiro parágrafo corrido do corpo que não seja boilerplate;
    3) placeholder fixo. Devolve (texto, is_placeholder).
    """
    body = source.get("body") or ""
    topic = source.get("topic") or "geral"
    source_type = source.get("source_type") or "sem-tipo"
    origin = source.get("origin") or "origem desconhecida"

    named = _named_section_summary(body)
    if named:
        return _truncate_at_boundary(named), False

    for paragraph in _iter_plain_paragraphs(body):
        if not _is_boilerplate(_normalize_ws(paragraph)):
            return _truncate_at_boundary(paragraph), False

    placeholder = f"Artefato {source_type} do tópico {topic}, gerado a partir de {origin}."
    return placeholder, True


def core_props(source: dict, title: str, summary: str) -> dict:
    """Monta o dict de propriedades core (§C.4) para `docx_ooxml.build_package`."""
    topic = source.get("topic") or "geral"
    source_type = source.get("source_type") or "sem-tipo"
    origin = source.get("origin") or ""

    id_parts = _extract_id_parts(source.get("id") or "")
    subject_part = id_parts.get("subject") or ""

    props: dict = {
        "title": title,
        "subject": topic,
        "creator": origin[:250],
        "category": source_type,
        "keywords": f"{topic}, {source_type}, {subject_part}",
        "description": summary,
        "identifier": DOCX_GERADO_IDENTIFIER,
    }

    w3c = _parse_w3cdtf(source.get("captured_at"))
    if w3c is not None:
        props["created"] = w3c
        props["modified"] = w3c

    return props
