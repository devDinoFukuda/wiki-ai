"""Evidence packs e verificação determinística para codebase.

Esta é a camada incremental antes de qualquer parser nativo: usa o `surface`
existente, leitura confinada ao repo e busca lexical simples para gerar contexto
rastreável. O LLM pode sintetizar em cima disso, mas `verify` bloqueia claims
confirmadas sem `arquivo:linha`.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

from .surface import LANGUAGES, MAX_FILE_BYTES, SKIP_DIRS, _is_generated

MAX_SCAN_BYTES = 2_000_000
DEFAULT_CONTEXT = 4
DEFAULT_MAX_LINES = 80


@dataclass
class Citation:
    path: str
    line_start: int
    line_end: int


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_path(repo: str, rel: str) -> str:
    repo_abs = os.path.abspath(repo)
    full = os.path.abspath(os.path.join(repo_abs, rel))
    if full != repo_abs and not full.startswith(repo_abs + os.sep):
        raise ValueError(f"caminho fora do repositório: {rel}")
    return full


def _read_lines(repo: str, rel: str) -> list[str]:
    full = _safe_path(repo, rel)
    if not os.path.isfile(full):
        raise ValueError(f"não é arquivo: {rel}")
    if os.path.getsize(full) > MAX_SCAN_BYTES:
        raise ValueError(f"arquivo grande demais para evidence: {rel}")
    with open(full, encoding="utf-8-sig", errors="replace") as f:
        return f.read().splitlines()


def _tokens(topic: str) -> list[str]:
    parts = [x.lower() for x in re.findall(r"[\w.-]+", topic or "", re.UNICODE)]
    return sorted(set(x for x in parts if len(x) >= 2))


def _iter_code_files(repo: str):
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
        ]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in LANGUAGES:
                continue
            if _is_generated(fn):
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield os.path.relpath(full, repo)


def _score_file(repo: str, rel: str, terms: list[str]) -> tuple[int, list[int]]:
    """Pontua arquivo por tópico. Caminho pesa mais que corpo.

    Não tenta ser classificador semântico; é um candidato determinístico para o
    agente cavar. Se não houver termo lexical, o comando faz fallback para
    entry points/top modules.
    """
    lower_path = rel.replace("\\", "/").lower()
    score = sum(8 for t in terms if t in lower_path)
    matches: list[int] = []
    try:
        lines = _read_lines(repo, rel)
    except ValueError:
        return score, matches
    for i, ln in enumerate(lines, start=1):
        low = ln.lower()
        hit_count = sum(1 for t in terms if t in low)
        if hit_count:
            score += hit_count
            matches.append(i)
    return score, matches


def _ranges(matches: list[int], total: int, context: int, max_lines: int) -> list[tuple[int, int]]:
    if total <= 0:
        return []
    if not matches:
        return [(1, min(total, max_lines))]
    out: list[tuple[int, int]] = []
    for m in matches:
        start = max(1, m - context)
        end = min(total, m + context)
        if out and start <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
        if sum(e - s + 1 for s, e in out) >= max_lines:
            break
    clipped: list[tuple[int, int]] = []
    used = 0
    for start, end in out:
        take = min(end - start + 1, max_lines - used)
        if take <= 0:
            break
        clipped.append((start, start + take - 1))
        used += take
    return clipped


def _excerpt(lines: list[str], start: int, end: int) -> str:
    width = len(str(end))
    return "\n".join(
        f"{str(i).rjust(width)}\t{lines[i - 1]}" for i in range(start, end + 1)
    )


def _surface_commit(surface: dict) -> str | None:
    git = surface.get("git") or {}
    return git.get("head")


def _fallback_files(surface: dict, top: int) -> list[tuple[str, str, int]]:
    files: list[tuple[str, str, int]] = []
    for ep in surface.get("entry_points") or []:
        path = ep.get("path")
        if path:
            files.append((path, f"entry point: {ep.get('reason') or 'detectado'}", 1))
    for mod in (surface.get("modules") or [])[:top]:
        path = mod.get("path")
        if path:
            files.append((path, "módulo de alto LOC sem match lexical", 0))
    return files


def build_evidence_pack(
    repo: str,
    surface: dict,
    topic: str,
    *,
    top: int = 20,
    context: int = DEFAULT_CONTEXT,
    max_lines: int = DEFAULT_MAX_LINES,
) -> dict:
    repo = os.path.abspath(repo)
    terms = _tokens(topic)
    if not terms:
        raise ValueError("topic vazio: evidence exige --topic ou state.topic")

    scored: list[tuple[int, str, list[int]]] = []
    for rel in _iter_code_files(repo):
        score, matches = _score_file(repo, rel, terms)
        if score > 0:
            scored.append((score, rel, matches))
    scored.sort(key=lambda x: (-x[0], x[1]))

    selected: list[tuple[str, str, int, list[int]]] = [
        (rel, "match lexical em tópico/caminho/conteúdo", score, matches)
        for score, rel, matches in scored[:top]
    ]
    if not selected:
        for rel, reason, score in _fallback_files(surface, top):
            full = os.path.join(repo, rel)
            if os.path.isfile(full):
                selected.append((rel, reason, score, []))
            elif os.path.isdir(full):
                for cand in _iter_code_files(full):
                    selected.append((os.path.join(rel, cand), reason, score, []))
                    if len(selected) >= top:
                        break
            if len(selected) >= top:
                break

    items = []
    for idx, (rel, reason, score, matches) in enumerate(selected, start=1):
        lines = _read_lines(repo, rel)
        for r_idx, (start, end) in enumerate(_ranges(matches, len(lines), context, max_lines), start=1):
            items.append(
                {
                    "id": f"ev-{idx:04d}-{r_idx}",
                    "type": "code-excerpt",
                    "file": rel.replace("\\", "/"),
                    "line_start": start,
                    "line_end": end,
                    "symbol": None,
                    "score": score,
                    "reason": reason,
                    "excerpt": _excerpt(lines, start, end),
                }
            )

    return {
        "schema": "codescan.evidence.v1",
        "repo": repo,
        "commit": _surface_commit(surface),
        "topic": topic,
        "created_at": _now(),
        "strategy": "lexical-surface",
        "items": items,
        "warnings": [] if items else ["nenhuma evidência encontrada"],
    }


# F-29: a classe original era `[A-Za-z0-9_.-]`, ASCII puro. Em legado real
# (o alvo declarado deste pipeline) nome de arquivo com acento é comum —
# `servico/Usuário.java`, `dominio/Cotação.cs`. A citação simplesmente NÃO era
# extraída: a claim ficava "sem citação" e o verify reprovava com
# `claim_sem_evidencia` uma evidência que existia e estava correta.
# `\w` em padrão `str` do Python 3 já é Unicode-aware, então cobre acento,
# cedilha e alfabetos não-latinos, mantendo a exclusão de espaço e de ':'
# (que separa caminho de linha) — ':' e ' ' não pertencem a `\w`.
#
# `extra` cobre a forma multi-linha `Path.java:9,15,30`: uma lista de números
# soltos (sem `-`) colada ao primeiro `:linha`/`:linha-linha`, sem espaço. Cada
# número extra vira uma citação (path, n, n) independente em `citations()` —
# antes o `,15` era silenciosamente descartado pelo regex (só `:9` casava).
_CITATION_RE = re.compile(
    r"(?P<path>(?:[\w.-]+[/\\])*[\w.-]+\.\w+)"
    r":(?P<start>\d+)(?:-(?P<end>\d+))?(?P<extra>(?:,\d+)*)"
)
_GREEN = "\U0001F7E2"
_YELLOW = "\U0001F7E1"
_RED = "\U0001F534"
_PT_BR_MARKERS = (
    "visão",
    "responsabilidade",
    "regra",
    "requisito",
    "critério",
    "fluxo",
    "dependência",
    "entidade",
    "função",
    "rastreabilidade",
    "evidência",
    "lacuna",
    "decisão",
)
_EN_MARKERS = (
    "responsibility",
    "responsibilities",
    "overview",
    "business rules",
    "requirements",
    "acceptance criteria",
    "technical design",
    "implementation tasks",
    "dependencies",
    "data structures",
)
# F-30: o detector antigo comparava só a PRESENÇA destes marcadores, num texto
# que incluía código, crases e caminhos de arquivo. Duas consequências:
#   (1) falso positivo — um artefato em PT-BR que cite `requirements.md`,
#       `dependencies` de um pom.xml ou um heading técnico "## Overview"
#       era acusado de estar em inglês;
#   (2) manipulável por keyword — bastava salpicar duas palavras PT-BR da
#       lista para desligar a acusação de um documento inteiro em inglês.
# A recalibração abaixo troca "presença de palavra-chave" por PROPORÇÃO de
# sinal linguístico sobre a PROSA (código/crase/caminho removidos antes):
# palavras funcionais são as que um autor não escolhe conscientemente, e por
# isso não são manipuláveis como um heading é.
_EN_FUNCTION_WORDS = frozenset(
    """
    the this that these those with without from after before and or of for to in on at by
    is are was were be been being has have had does did will would should must can could
    may might when which while whose where what who into than then their there they them
    its it not but also such each other another more most only both between during through
    over under about above below within across any all every some many few
    """.split()
)
# `as`, `do`, `a`, `e`, `o`, `no`, `os` ficam DE FORA de propósito: são
# palavras funcionais das DUAS línguas e envenenariam a contagem.
_PT_FUNCTION_WORDS = frozenset(
    """
    de da do das dos para com sem que não nao é são ser está estão como pelo pela pelos
    pelas quando onde cada este esta esse essa aquele aquela seu sua seus suas ao aos às
    um uma uns umas na nas nos em por mais menos também entre sobre até após antes depois
    todo toda todos todas isso aquilo qual quais deve devem faz fazem usa usam existe
    existem apenas ainda já pode podem foi foram sendo cujo cuja porque então
    """.split()
)
# Sinal PT-BR mais barato e mais difícil de forjar que qualquer lista: o acento.
_ACCENTED_WORD_RE = re.compile(r"\b\w*[À-ÿ]\w*\b", re.UNICODE)
_WORD_RE = re.compile(r"[\wÀ-ÿ']+", re.UNICODE)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
# Caminho de arquivo/identificador com extensão: `src/quotes/Quote.java:12`,
# `pom.xml`, `README.md`. Nada disso diz em que idioma a PROSA está escrita.
_PATH_LIKE_RE = re.compile(
    r"\S*[/\\]\S*|\b[\w.-]+\.[A-Za-z0-9_]{1,8}\b(?::\d+(?:-\d+)?)?",
    re.UNICODE,
)
# Marcador conta o dobro de uma palavra funcional (é um sinal deliberado e
# multivocabular), mas o PISO alto (`_EN_MIN_SIGNAL`) garante que headings
# técnicos em inglês ISOLADOS — "## Overview", "## Dependencies" — não bastem
# para acusar um documento; e a razão mínima garante que prosa PT-BR de verdade
# ao redor sempre vence.
_MARKER_WEIGHT = 2
_EN_MIN_SIGNAL = 8
_EN_PT_RATIO = 2

_TECHNICAL_BOILERPLATE_MARKERS = ("TODO", "TBD", "FIXME", "XXX")
_TECHNICAL_BOILERPLATE_RE_TEMPLATE = r"(?<!\w){marker}(?!\w)"
_BOILERPLATE_TEXT_MARKERS = (
    "lorem ipsum",
    "as an ai",
    "as a language model",
    "not enough information",
    "no information available",
    "preencher",
    "pendente de análise",
    "<arquivo:linha>",
    "<descrição>",
    "<descricao>",
    "<preencher>",
    "<pendente>",
)
_GENERIC_GREEN_RE = re.compile(
    r"\b(existe|exists|handles|gerencia|contém|contains|usa|uses|tem|has)\b",
    re.I,
)


def citations(text: str) -> list[Citation]:
    out = []
    for m in _CITATION_RE.finditer(text or ""):
        path = m.group("path").replace("\\", "/")
        start = int(m.group("start"))
        end = int(m.group("end") or start)
        out.append(Citation(path, start, end))
        # `:9,15,30` (sem `-`): cada número extra é uma citação de linha única
        # própria — (path, 9, 9), (path, 15, 15), (path, 30, 30). `:9-15`
        # (range) não passa por aqui: `extra` só casa vírgula sem `-`.
        for tok in (m.group("extra") or "").split(","):
            tok = tok.strip()
            if not tok:
                continue
            n = int(tok)
            out.append(Citation(path, n, n))
    return out


def find_boilerplate_markers(
    text: str,
    text_markers: tuple[str, ...] = _BOILERPLATE_TEXT_MARKERS,
) -> list[str]:
    found: list[str] = []
    raw = text or ""
    lower = raw.lower()
    for marker in _TECHNICAL_BOILERPLATE_MARKERS:
        pattern = _TECHNICAL_BOILERPLATE_RE_TEMPLATE.format(marker=re.escape(marker))
        if re.search(pattern, raw):
            found.append(marker)
    seen_text_markers: set[str] = set()
    for marker in text_markers:
        key = marker.lower()
        if key in seen_text_markers:
            continue
        seen_text_markers.add(key)
        if key in lower:
            found.append(marker)
    return found


def _claim_blocks(markdown: str) -> list[tuple[int, str]]:
    """Extrai blocos de bullet como claims verificáveis."""
    lines = markdown.splitlines()
    claims: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not re.match(r"^\s*[-*]\s+", line):
            i += 1
            continue
        start = i + 1
        block = [line]
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if re.match(r"^\s*[-*]\s+", nxt) or nxt.startswith("#"):
                break
            if not nxt.strip():
                i += 1
                break
            block.append(nxt)
            i += 1
        claims.append((start, "\n".join(block)))
    return claims


def _basename_index(repo: str, cache: dict[str, dict[str, list[str]]] | None) -> dict[str, list[str]]:
    """Mapa `basename -> [caminho relativo, ...]` de todo o repo.

    Mesmos `SKIP_DIRS` do estágio `surface` (bin/obj/node_modules/.git/etc. —
    varredura de repositório real sem isso devolve lixo por milhares). Ao
    contrário de `_iter_code_files`, não filtra por `LANGUAGES`/gerado: uma
    citação pode nomear qualquer arquivo do repo, não só código-fonte.

    Cacheado por `repo` em `cache` (um dict passado pelo chamador, tipicamente
    um por chamada de `verify_markdown`) para que N citações sem separador no
    mesmo documento só varram a árvore do repo uma vez.
    """
    if cache is not None and repo in cache:
        return cache[repo]
    index: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), repo).replace("\\", "/")
            index.setdefault(fn, []).append(rel)
    if cache is not None:
        cache[repo] = index
    return index


def _citation_error(
    repo: str,
    c: Citation,
    *,
    basename_cache: dict[str, dict[str, list[str]]] | None = None,
) -> dict | None:
    try:
        full = _safe_path(repo, c.path)
    except ValueError as e:
        return {"citation": f"{c.path}:{c.line_start}", "rule": "fora_do_repo", "detail": str(e)}
    if c.path != c.path.strip() or os.path.isabs(c.path):
        return {"citation": f"{c.path}:{c.line_start}", "rule": "caminho_invalido", "detail": c.path}
    if not os.path.isfile(full):
        # citação sem separador (`Quote.java:9`, não `src/Quote.java:9`)
        # que não existe como caminho literal. Antes disso virava direto
        # `arquivo_inexistente` com `detail: c.path` — verdade, mas inacionável
        # quando o arquivo existe em outro lugar do repo e só falta o caminho
        # completo. Busca o basename: exatamente 1 match no repo inteiro vira
        # `caminho_parcial` com o caminho relativo pronto para copiar; 0 ou 2+
        # matches (ambíguo ou de fato inexistente) mantém `arquivo_inexistente`
        # — ainda é erro, só a mensagem melhora.
        if "/" not in c.path:
            matches = _basename_index(repo, basename_cache).get(c.path) or []
            if len(matches) == 1:
                return {
                    "citation": f"{c.path}:{c.line_start}",
                    "rule": "caminho_parcial",
                    "detail": f"use o caminho relativo completo: {matches[0]}",
                }
        return {
            "citation": f"{c.path}:{c.line_start}",
            "rule": "arquivo_inexistente",
            "detail": f"{c.path} — verifique o caminho completo da raiz do repo",
        }
    exact_rel = os.path.relpath(full, repo).replace("\\", "/")
    if c.path.replace("\\", "/") != exact_rel:
        return {
            "citation": f"{c.path}:{c.line_start}",
            "rule": "caminho_parcial",
            "detail": f"use o caminho relativo completo: {exact_rel}",
        }
    with open(full, encoding="utf-8-sig", errors="replace") as f:
        total = len(f.read().splitlines())
    if c.line_start < 1 or c.line_end < c.line_start or c.line_end > total:
        return {
            "citation": f"{c.path}:{c.line_start}-{c.line_end}",
            "rule": "linha_invalida",
            "detail": f"arquivo tem {total} linhas",
        }
    return None


def _prose_only(text: str) -> str:
    """Prosa do artefato: sem fences, sem crases, sem caminho de arquivo.

    É o único recorte em que uma pergunta sobre IDIOMA faz sentido — código e
    caminho são inglês por construção em qualquer repositório.
    """
    cleaned = _CODE_FENCE_RE.sub(" ", text or "")
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    return _PATH_LIKE_RE.sub(" ", cleaned)


def _language_signal(markdown: str) -> tuple[int, int]:
    """(en_hits, pt_hits) sobre a prosa. Determinístico e simétrico."""
    prose = _prose_only(markdown)
    lower = prose.lower()
    words = _WORD_RE.findall(lower)

    en_hits = _MARKER_WEIGHT * sum(lower.count(marker) for marker in _EN_MARKERS)
    en_hits += sum(1 for w in words if w in _EN_FUNCTION_WORDS)

    pt_hits = _MARKER_WEIGHT * sum(lower.count(marker) for marker in _PT_BR_MARKERS)
    pt_hits += sum(1 for w in words if w in _PT_FUNCTION_WORDS)
    pt_hits += len(_ACCENTED_WORD_RE.findall(lower))
    return en_hits, pt_hits


def provavel_ingles(markdown: str) -> bool:
    """Verdadeiro só quando o sinal EN é alto EM ABSOLUTO e domina o PT.

    Antes bastavam 2 palavras-chave em inglês em qualquer lugar do arquivo
    (inclusive dentro de crase) para reprovar o artefato.
    """
    en_hits, pt_hits = _language_signal(markdown)
    return en_hits >= _EN_MIN_SIGNAL and en_hits >= _EN_PT_RATIO * pt_hits


def _claim_word_count(block: str) -> tuple[int, str]:
    """(palavras, prosa) de uma claim.

    F-31: o conteúdo entre crases era APAGADO antes da contagem, então
    `- O teto de retry é `MAX_RETRIES` em `PaymentService.retry()`. 🟢` perdia
    justamente os tokens que a tornam precisa e caía no piso de 7 palavras
    como se fosse genérica. Cada trecho em crase vale 1 palavra — é 1 termo,
    não 0 e não N.
    """
    code_spans = _INLINE_CODE_RE.findall(block)
    prose = _INLINE_CODE_RE.sub(" ", block)
    words = re.findall(r"\b[\wÀ-ÿ]{3,}\b", prose, re.UNICODE)
    return len(words) + len(code_spans), prose


def _markdown_quality_errors(markdown: str, claim_blocks: list[tuple[int, str]]) -> list[dict]:
    errors: list[dict] = []
    if provavel_ingles(markdown):
        errors.append(
            {
                "rule": "provavel_ingles",
                "detail": "artifact confirmado parece estar em inglês; contrato exige PT-BR",
            }
        )
    found_boilerplate = find_boilerplate_markers(markdown)
    if found_boilerplate:
        errors.append(
            {
                "rule": "boilerplate",
                "detail": "artifact confirmado contém placeholder/boilerplate: "
                + ", ".join(found_boilerplate[:3]),
            }
        )
    for line_no, block in claim_blocks:
        if _GREEN not in block:
            continue
        total_words, claim_text = _claim_word_count(block)
        if total_words < 7 or (_GENERIC_GREEN_RE.search(claim_text) and total_words < 10):
            errors.append(
                {
                    "line": line_no,
                    "rule": "claim_verde_generica",
                    "detail": "claim verde precisa ser operacional, específica e rastreável",
                    "text": block[:240],
                }
            )
    return errors


def _verify_markdown_legacy(repo: str, markdown: str) -> dict:
    repo = os.path.abspath(repo)
    errors = []
    warnings = []
    claim_blocks = _claim_blocks(markdown)
    all_citations = citations(markdown)

    for line_no, block in claim_blocks:
        if not citations(block):
            errors.append(
                {
                    "line": line_no,
                    "rule": "claim_sem_evidencia",
                    "detail": "claim em bullet sem citação arquivo:linha",
                    "text": block[:240],
                }
            )

    # Mesma `_citation_error` usada por `verify_markdown` — antes este ramo
    # legado duplicava a lógica de checagem de citação à mão (isfile, linha
    # dentro do arquivo) sem o tratamento de basename-único/caminho-parcial,
    # e as duas versões podiam divergir silenciosamente a cada mudança de uma
    # sem a outra.
    basename_cache: dict[str, dict[str, list[str]]] = {}
    for c in all_citations:
        err = _citation_error(repo, c, basename_cache=basename_cache)
        if err:
            errors.append(err)

    if not claim_blocks:
        warnings.append("nenhuma claim em bullet encontrada")
    if not all_citations:
        errors.append({"rule": "sem_citacoes", "detail": "nenhuma citação arquivo:linha encontrada"})

    return {
        "claims": len(claim_blocks),
        "citations": len(all_citations),
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
    }


def verify_markdown(repo: str, markdown: str) -> dict:
    repo = os.path.abspath(repo)
    errors = []
    warnings = []
    claim_blocks = _claim_blocks(markdown)
    all_citations = citations(markdown)
    valid_citations: set[tuple[str, int, int]] = set()

    # Cache por chamada: N citações sem separador (`Quote.java:1`) no mesmo
    # documento reusam o mesmo índice de basenames em vez de varrer o repo
    # inteiro de novo a cada uma.
    basename_cache: dict[str, dict[str, list[str]]] = {}
    for c in all_citations:
        err = _citation_error(repo, c, basename_cache=basename_cache)
        if err:
            errors.append(err)
        else:
            valid_citations.add((c.path, c.line_start, c.line_end))

    green_claims = 0
    for line_no, block in claim_blocks:
        if _GREEN not in block:
            continue
        green_claims += 1
        block_valid = any((c.path, c.line_start, c.line_end) in valid_citations for c in citations(block))
        if not block_valid:
            errors.append(
                {
                    "line": line_no,
                    "rule": "claim_sem_evidencia",
                    "detail": "claim verde em bullet sem citacao arquivo:linha valida",
                    "text": block[:240],
                }
            )

    errors.extend(_markdown_quality_errors(markdown, claim_blocks))

    if not claim_blocks:
        warnings.append("nenhuma claim em bullet encontrada")
    # F-03: a versão legada (`_verify_markdown_legacy`, linhas 437-440) exigia
    # citação em TODA claim em bullet; a versão atual passou a exigir apenas em
    # claim 🟢 — e, com isso, um documento inteiro sem uma única claim verde
    # passava com `ok=True` mesmo sem NENHUMA evidência (o gate de
    # promote/compile/docx lia isso como aprovado). O piso restaurado é de
    # DOCUMENTO, não de bullet: prosa auxiliar (bullets 🟡/🔴 ou sem selo)
    # continua isenta individualmente, mas um artefato com claims e zero
    # citações válidas não é verificável e reprova.
    if claim_blocks and not valid_citations:
        errors.append(
            {
                "rule": "sem_evidencia",
                "detail": (
                    f"documento tem {len(claim_blocks)} claim(s) em bullet e nenhuma citacao "
                    "arquivo:linha valida"
                ),
            }
        )

    return {
        "claims": len(claim_blocks),
        "green_claims": green_claims,
        "citations": len(all_citations),
        "valid_citations": len(valid_citations),
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
    }


def read_json(path: str) -> dict:
    return json.load(open(path, encoding="utf-8"))


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
