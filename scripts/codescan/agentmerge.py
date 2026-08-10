"""Deterministic merge for subagent outputs."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from . import evidence as ev_mod
from . import export as ex_mod
from . import sdd as sdd_mod
from . import state as st_mod

try:
    from . import noise as noise_mod
except Exception:  # pragma: no cover
    noise_mod = None


class MergeError(ValueError):
    """Erro de merge determinístico.

    `str(error)` continua sendo uma string legível (retrocompatibilidade:
    `cli.cmd_merge_agent_output` faz `"error": str(e)` sem inspecionar mais
    nada). Os kwargs abaixo só existem para quem chama `merge_agent_output`
    diretamente (testes, ferramentas futuras) e quer os dados estruturados
    sem re-parsear a mensagem — ver FIX 1 do lote D (`_reject_noise`).
    """

    def __init__(
        self,
        message: str,
        *,
        violacoes: list[dict] | None = None,
        violacoes_total: int | None = None,
        acao: str | None = None,
    ) -> None:
        super().__init__(message)
        self.violacoes = violacoes or []
        self.violacoes_total = violacoes_total if violacoes_total is not None else len(self.violacoes)
        self.acao = acao


@dataclass(frozen=True)
class MergedArtifact:
    item: str
    artifacts: list[str]


MODULE_RE = re.compile(r"^=== MODULE:\s*(.+?)\s*===\s*$")
SPEC_RE = re.compile(r"^=== SPEC:\s*(.+?)\s*===\s*$")
RULES_RE = re.compile(r"^=== RULES:\s*(.+?)\s*===\s*$")
ARCHITECTURE_RE = re.compile(r"^=== ARCHITECTURE:\s*(.+?)\s*===\s*$")
SYNTH_RE = re.compile(r"^=== SYNTH:\s*(.+?)\s*===\s*$")
END_RE = re.compile(r"^=== END ===\s*$")

NAMED_STAGE_HEADER_RE: dict[str, re.Pattern[str]] = {
    "rules": RULES_RE,
    "architecture": ARCHITECTURE_RE,
    "synth": SYNTH_RE,
}
NAMED_STAGE_ALLOWED: dict[str, tuple[str, ...]] = {
    "rules": ("domain", "state-machines", "permissions"),
    "architecture": (
        "architecture", "c4-context", "c4-containers", "c4-components", "erd-complete",
        "traceability/spec-impact-matrix",
    ),
    "synth": ("confirmed", "inferred"),
}
# Famílias de artefatos com nome variável por stage (sdd-contract §1):
# rules aceita ADRs retroativos; architecture aceita diagramas de sequência.
NAMED_STAGE_PREFIX_RES: dict[str, tuple[re.Pattern[str], ...]] = {
    "rules": (re.compile(r"^adrs/\d{3}-[a-z0-9][a-z0-9-]*$"),),
    "architecture": (re.compile(r"^sequences/[a-z0-9][a-z0-9-]*$"),),
}
NAMED_STAGE_PREFIX_HINTS: dict[str, tuple[str, ...]] = {
    "rules": ("adrs/NNN-<slug>",),
    "architecture": ("sequences/<slug>",),
}
# Documentos nomeados aceitos em bloco SPEC (além das units com o trio canônico).
SPECS_DOC_ALLOWED = ("confidence-report", "gaps", "traceability/code-spec-matrix")
SPECS_DOC_PREFIX_RES = (
    re.compile(r"^user-stories/[a-z0-9][a-z0-9-]*$"),
    re.compile(r"^openapi/[a-z0-9][a-z0-9-]*$"),
)
SPECS_DOC_PREFIX_HINTS = ("user-stories/<slug>", "openapi/<slug>")
ALL_HEADER_RES = (MODULE_RE, SPEC_RE, RULES_RE, ARCHITECTURE_RE, SYNTH_RE)
SPEC_FILE_RE = re.compile(
    r"^---\s*(requirements\.md|design\.md|tasks\.md|contracts\.md|edge-cases\.md)\s*---\s*$"
)
SPEC_OPTIONAL_FILES = ("contracts.md", "edge-cases.md")
SCRIPT_EXTENSIONS = (".py", ".ps1", ".sh", ".bat", ".cmd", ".js", ".ts", ".mjs", ".cjs")
IDENTIFIER_RE = re.compile(r"`([A-Za-z][A-Za-z0-9_.$<>?, ]*)`")
RECORD_RE = re.compile(r"\brecord\s+([A-Z][A-Za-z0-9_]*)\s*\(([^)]*)\)")
FIELD_WORD_RE = re.compile(r"\b([a-z][A-Za-z0-9_]{2,})\b")
TYPE_WORD_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})\b")


def _normalize_item(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    normalized = re.sub(r"/+", "/", normalized).strip("/")
    if not normalized or normalized.startswith("../") or "/../" in normalized:
        raise MergeError(f"item inválido: {value}")
    return normalized


def _module_artifact(wd: str, item: str) -> str:
    return os.path.join(wd, "modules", f"{ex_mod._slug(item)}.md")


def _is_any_header(line: str) -> bool:
    return any(pattern.match(line) for pattern in ALL_HEADER_RES)


def _named_artifact(wd: str, name: str) -> str:
    return os.path.join(wd, "sdd", *f"{name}.md".split("/"))


def _is_specs_doc(name: str) -> bool:
    return name in SPECS_DOC_ALLOWED or any(p.match(name) for p in SPECS_DOC_PREFIX_RES)


def _specs_doc_artifact(wd: str, name: str) -> str:
    ext = ".yaml" if name.startswith("openapi/") else ".md"
    return os.path.join(wd, "sdd", *name.split("/")) + ext


def _spec_dir(wd: str, unit: str) -> str:
    base = os.path.abspath(os.path.join(wd, "sdd", "specs"))
    full = os.path.abspath(os.path.join(base, ex_mod._slug(unit)))
    if full == base or not full.startswith(base + os.sep):
        raise MergeError(f"unit inválida: {unit}")
    return full


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip() + "\n")
    os.replace(tmp, path)


def _module_docs(wd: str, overrides: dict[str, str] | None = None) -> list[tuple[str, str, str]]:
    root = os.path.join(wd, "modules")
    docs: list[tuple[str, str, str]] = []
    if os.path.isdir(root):
        for filename in sorted(os.listdir(root)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(root, filename)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                docs.append((filename[:-3], path, f.read()))
    if overrides:
        by_path = {os.path.abspath(path): (label, path, content) for label, path, content in docs}
        for path, content in overrides.items():
            label = os.path.basename(path)[:-3]
            by_path[os.path.abspath(path)] = (label, path, content)
        docs = sorted(by_path.values(), key=lambda item: os.path.basename(item[1]))
    return docs


def _write_code_analysis(wd: str, docs: list[tuple[str, str, str]]) -> str:
    path = os.path.join(wd, "sdd", "code-analysis.md")
    chunks = [
        "# Análise de código",
        "",
        "## Visão geral",
        f"- 🟢 Consolidação determinística de {len(docs)} módulo(s) entregues por subagentes.",
        "",
        "## Módulos",
        "",
    ]
    for label, _path, content in docs:
        chunks.extend([f"### Módulo: {label}", "", content.strip(), ""])
    chunks.extend([
        "## Fluxos",
        "- 🟢 Fluxos consolidados a partir dos artefatos em `modules/*.md`.",
        "",
        "## Riscos",
        "- 🟡 Riscos preservados nas lacunas de cada módulo consolidado.",
        "",
        "## Rastreabilidade",
        "- 🟢 Evidências preservadas em cada seção de módulo.",
        "",
    ])
    _write_text(path, "\n".join(chunks))
    return path


def _citation_text(citation: ev_mod.Citation) -> str:
    if citation.line_end != citation.line_start:
        return f"{citation.path}:{citation.line_start}-{citation.line_end}"
    return f"{citation.path}:{citation.line_start}"


def _clean_identifier(value: str) -> str:
    value = re.sub(r"<.*?>", "", value)
    value = value.replace("$", ".").strip(" .,:;()[]{}")
    return value.split(".")[-1].strip()


def _entity_kind(name: str) -> str:
    lower = name.lower()
    if lower.endswith(("dto", "request", "response")):
        return "DTO/API"
    if lower.endswith(("entity", "model")):
        return "Entidade persistida"
    if lower.endswith("event"):
        return "Evento"
    if lower.endswith(("repository", "gateway", "client")):
        return "Integração"
    if lower.endswith("rule"):
        return "Regra de negócio"
    if lower.endswith(("exception", "error")):
        return "Erro"
    if lower.endswith(("properties", "configuration", "config")):
        return "Configuração"
    if lower.endswith(("service", "controller", "handler", "policy")):
        return "Componente"
    if lower.endswith("code") or name.isupper():
        return "Enumeração"
    return "Tipo de domínio"


def _field_candidates(text: str) -> list[str]:
    reserved = {
        "public", "private", "return", "record", "class", "final", "static",
        "string", "integer", "boolean", "optional", "list", "map", "bigdecimal",
        "instant", "duration", "uuid", "null", "true", "false",
    }
    fields: list[str] = []
    for word in FIELD_WORD_RE.findall(text):
        if word.lower() in reserved:
            continue
        if word not in fields:
            fields.append(word)
    return fields[:6]


def _extract_dictionary(docs: list[tuple[str, str, str]]) -> tuple[list[dict], list[dict], list[str]]:
    entities: dict[str, dict] = {}
    fields: list[dict] = []
    all_citations: list[str] = []
    for module, _path, content in docs:
        citations = [_citation_text(c) for c in ev_mod.citations(content)]
        for citation in citations:
            if citation not in all_citations:
                all_citations.append(citation)
        if not citations:
            continue
        module_citation = citations[0]
        candidates: list[str] = []
        for raw in IDENTIFIER_RE.findall(content):
            for part in re.split(r"[, ]+", raw):
                name = _clean_identifier(part)
                if TYPE_WORD_RE.fullmatch(name) and len(name) >= 3:
                    candidates.append(name)
        for match in RECORD_RE.finditer(content):
            candidates.append(match.group(1))
            for raw_field in match.group(2).split(","):
                bits = raw_field.strip().split()
                if len(bits) >= 2:
                    field = _clean_identifier(bits[-1])
                    if field and field[0].islower():
                        fields.append({
                            "entity": match.group(1),
                            "field": field,
                            "type": _clean_identifier(" ".join(bits[:-1])),
                            "module": module,
                            "citation": module_citation,
                        })
        for line in content.splitlines():
            line_citations = [_citation_text(c) for c in ev_mod.citations(line)]
            line_citation = line_citations[0] if line_citations else module_citation
            line_entities = [
                _clean_identifier(raw)
                for raw in IDENTIFIER_RE.findall(line)
                if TYPE_WORD_RE.fullmatch(_clean_identifier(raw))
            ]
            for entity in line_entities[:3]:
                for field in _field_candidates(line):
                    if field != entity and not field[0].isupper():
                        fields.append({
                            "entity": entity,
                            "field": field,
                            "type": "identificador citado",
                            "module": module,
                            "citation": line_citation,
                        })
        for name in sorted(set(candidates)):
            entities.setdefault(name, {
                "name": name,
                "kind": _entity_kind(name),
                "module": module,
                "citation": module_citation,
            })
    unique_fields: dict[tuple[str, str], dict] = {}
    for field in fields:
        unique_fields.setdefault((field["entity"], field["field"]), field)
    return (
        [entities[name] for name in sorted(entities)],
        [unique_fields[key] for key in sorted(unique_fields)],
        all_citations,
    )


def _write_data_dictionary(wd: str, docs: list[tuple[str, str, str]]) -> tuple[str | None, str | None]:
    entities, fields, citations = _extract_dictionary(docs)
    if not entities or len(citations) < 2:
        return None, "dados insuficientes para data-dictionary.md: exige entidades/tipos e ao menos 2 citações nos módulos"
    path = os.path.join(wd, "sdd", "data-dictionary.md")
    chunks = [
        "# Dicionário de dados",
        "",
        "## Entidades",
        "",
        "| Entidade | Tipo | Módulo | Evidência |",
        "| --- | --- | --- | --- |",
    ]
    for entity in entities:
        chunks.append(
            f"| `{entity['name']}` | {entity['kind']} | `{entity['module']}` | 🟢 `{entity['citation']}` |"
        )
    chunks.extend([
        "",
        "## Campos",
        "",
        "| Entidade | Campo | Tipo observado | Origem |",
        "| --- | --- | --- | --- |",
    ])
    if fields:
        for field in fields:
            chunks.append(
                f"| `{field['entity']}` | `{field['field']}` | {field['type']} | 🟢 `{field['citation']}` |"
            )
    else:
        for entity in entities:
            chunks.append(
                f"| `{entity['name']}` | `(campos não isolados)` | tipo citado no módulo | 🟡 `{entity['citation']}` |"
            )
    chunks.extend([
        "",
        "## Origem",
        "",
        "- 🟢 A extração usa somente os artefatos `modules/*.md` já aceitos pelo merge.",
        "- 🟢 Cada entidade mantém a citação de arquivo:linha preservada pelo subagente.",
        "- 🟡 Campos sem declaração explícita ficam marcados como não isolados, sem inventar atributo.",
        "- 🟢 O estado do dicionário é reconstruído de forma determinística após cada batch.",
        "",
        "## Cobertura operacional",
        "",
        f"- Entidades rastreadas: {len(entities)}.",
        f"- Campos rastreados: {len(fields)}.",
        f"- Citações distintas usadas como origem: {len(citations)}.",
        "- Entrada: módulos consolidados; saída: tabela de entidades, campos e origem.",
    ])
    _write_text(path, "\n".join(chunks))
    return path, None


def _sanitize_label(value: str, *, fallback: str) -> str:
    label = re.sub(r"<[^>]*>", "", value)
    label = re.sub(r"[@\[\]{}();`]", " ", label)
    label = re.sub(r"\s+", " ", label).strip(" -_/\\")
    if not label:
        label = fallback
    if len(label) > 54:
        label = label[:51].rstrip() + "..."
    return label.replace('"', "'")


def _write_flowchart_index(wd: str, docs: list[tuple[str, str, str]]) -> str | None:
    if not docs:
        return None
    path = os.path.join(wd, "sdd", "flowcharts", "_index.md")
    node_count = min(len(docs), 12)
    lines = [
        "# Fluxos de módulos",
        "",
        "## Fluxo consolidado",
        "",
        "- Entrada: resultados de análise dos módulos em `modules/*.md`.",
        "- Processo: cada módulo alimenta a consolidação de SDD do estágio `modules`.",
        "- Saída: `code-analysis.md`, `data-dictionary.md` quando há entidades, e este índice Mermaid.",
        "- Estado: os itens só são marcados como concluídos depois que os artefatos são gravados.",
        "- Rastreabilidade: detalhes e citações permanecem nos módulos individuais.",
        "",
        "```mermaid",
        "flowchart LR",
        '  inicio["Entrada de módulos"]',
    ]
    for idx, (label, _path, _content) in enumerate(docs[:node_count], start=1):
        safe = _sanitize_label(label, fallback=f"Modulo {idx}")
        lines.append(f'  m{idx:03d}["{safe}"]')
    lines.append('  fim["Artefatos SDD"]')
    if node_count == 1:
        lines.extend(["  inicio --> m001", "  m001 --> fim"])
    else:
        previous = "inicio"
        for idx in range(1, node_count + 1):
            current = f"m{idx:03d}"
            lines.append(f"  {previous} --> {current}")
            previous = current
        lines.append(f"  {previous} --> fim")
    lines.extend(["```", ""])
    _write_text(path, "\n".join(lines))
    return path


def _generate_module_sdd(wd: str, docs: list[tuple[str, str, str]]) -> tuple[list[str], list[str]]:
    created: list[str] = []
    blockers: list[str] = []
    code_analysis = _write_code_analysis(wd, docs)
    created.append(code_analysis)
    st = st_mod.load(wd) or {}
    if sdd_mod.doc_level(st) in ("completo", "detalhado"):
        data_dictionary, blocker = _write_data_dictionary(wd, docs)
        if data_dictionary:
            created.append(data_dictionary)
        if blocker:
            blockers.append(blocker)
        flowchart = _write_flowchart_index(wd, docs)
        if flowchart:
            created.append(flowchart)
        else:
            blockers.append("dados insuficientes para flowcharts: nenhum módulo consolidado")
    return created, blockers


MAX_REPORTED_NOISE_VIOLATIONS = 10


def _format_noise_violation(v: dict) -> str:
    loc = f"linha {v['linha']}" if v.get("linha") is not None else "agregado"
    return f"[{v['regra']}] {loc}: {v['trecho']} (padrão: {v['padrao']})"


def _reject_noise(text: str) -> None:
    """Rejeita saída de subagente com ruído — e diz exatamente por quê.

    FIX 1 do lote D: antes disso a exceção só listava os NOMES das regras
    (`agent_output_too_long, edit_echo`), então o operador não sabia qual
    linha disparou a rejeição nem quanto o output excedeu o limite — numa
    execução real isso custou 1.120 linhas de log reescrevendo a saída
    inteira por tentativa e erro. Agora cada violação carrega regra, linha
    (1-indexed), trecho ofensivo truncado e o padrão que casou; para
    `agent_output_too_long` o trecho já traz "N linhas, limite 220".
    """

    if noise_mod is None:
        return
    contract_text = "\n".join(
        "<SPEC_FILE>" if SPEC_FILE_RE.match(line) else line
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    )
    if hasattr(noise_mod, "detect_violations"):
        violations = noise_mod.detect_violations(contract_text, agent=True)
    else:  # pragma: no cover - fallback para noise.py sem diagnóstico detalhado
        violations = [
            {"regra": code, "linha": None, "trecho": "", "padrao": ""}
            for code in noise_mod.validate_agent_output(contract_text)
        ]
    if not violations:
        return
    total = len(violations)
    shown = violations[:MAX_REPORTED_NOISE_VIOLATIONS]
    acao = "rode `sdd-brief <stage>` para o contrato canônico do estágio e corrija apenas o(s) trecho(s) apontado(s) abaixo antes de reenviar"
    # Primeira linha preserva o formato legado ("ruído rejeitado: <regras>")
    # para retrocompatibilidade da chave "error" (string legível) — quem já
    # fazia `assertIn("ruído rejeitado", err)` continua passando.
    lines = ["ruído rejeitado: " + ", ".join(v["regra"] for v in shown)]
    if total > len(shown):
        lines[0] += f" (+{total - len(shown)} outra(s), total {total} violação(ões))"
    lines.extend(f"- {_format_noise_violation(v)}" for v in shown)
    if total > len(shown):
        lines.append(f"... {total - len(shown)} violação(ões) omitida(s) (total {total})")
    lines.append(f"acao: {acao}")
    raise MergeError(
        "\n".join(lines),
        violacoes=shown,
        violacoes_total=total,
        acao=acao,
    )


def _parse_modules(text: str) -> list[tuple[str, str]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        match = MODULE_RE.match(lines[i])
        if not match:
            raise MergeError(
                "prosa fora de bloco MODULE; esperado: `=== MODULE: <path> ===` "
                "… `=== END ===` (todo conteúdo dentro de blocos)"
            )
        item = _normalize_item(match.group(1))
        i += 1
        body: list[str] = []
        while i < len(lines) and not END_RE.match(lines[i]):
            if MODULE_RE.match(lines[i]) or SPEC_RE.match(lines[i]):
                raise MergeError("bloco MODULE sem END")
            body.append(lines[i])
            i += 1
        if i >= len(lines):
            raise MergeError("bloco MODULE sem END")
        content = "\n".join(body).strip()
        if not content:
            raise MergeError(f"MODULE vazio: {item}")
        blocks.append((item, content))
        i += 1
    if not blocks:
        raise MergeError("nenhum bloco MODULE")
    return blocks


def _parse_spec_body(item: str, body: list[str]) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body:
        match = SPEC_FILE_RE.match(line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
            continue
        if current is None:
            if line.strip():
                raise MergeError(
                    f"prosa fora de arquivo SPEC: {item}; esperado: seções "
                    "`--- requirements.md ---` / `--- design.md ---` / `--- tasks.md ---` "
                    "(opcionais: contracts.md, edge-cases.md) antes de qualquer conteúdo"
                )
            continue
        sections[current].append(line)
    required = ("requirements.md", "design.md", "tasks.md")
    missing = [name for name in required if not "\n".join(sections.get(name, [])).strip()]
    if missing:
        raise MergeError(f"SPEC incompleta {item}: " + ", ".join(missing))
    out = {name: "\n".join(sections[name]).strip() for name in required}
    for name in SPEC_OPTIONAL_FILES:
        if name not in sections:
            continue
        content = "\n".join(sections[name]).strip()
        if not content:
            raise MergeError(f"arquivo opcional vazio em SPEC {item}: {name}")
        out[name] = content
    return out


def _parse_specs(text: str) -> tuple[list[tuple[str, dict[str, str]]], list[tuple[str, str]]]:
    """Blocos SPEC: units (trio canônico + opcionais) e documentos nomeados
    (`confidence-report`, `gaps`, `traceability/code-spec-matrix`,
    `user-stories/<slug>`, `openapi/<slug>`) com corpo livre."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    units: list[tuple[str, dict[str, str]]] = []
    docs: list[tuple[str, str]] = []
    seen_docs: set[str] = set()
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        match = SPEC_RE.match(lines[i])
        if not match:
            raise MergeError(
                "prosa fora de bloco SPEC; esperado: `=== SPEC: <unit> ===` "
                "… `=== END ===` (SPEC singular, não SPECS; todo conteúdo dentro de blocos)"
            )
        item = _normalize_item(match.group(1))
        i += 1
        body: list[str] = []
        while i < len(lines) and not END_RE.match(lines[i]):
            if MODULE_RE.match(lines[i]) or SPEC_RE.match(lines[i]):
                raise MergeError("bloco SPEC sem END")
            body.append(lines[i])
            i += 1
        if i >= len(lines):
            raise MergeError("bloco SPEC sem END")
        if _is_specs_doc(item):
            if any(SPEC_FILE_RE.match(ln) for ln in body):
                raise MergeError(f"documento nomeado não aceita seções de arquivo: {item}")
            if item in seen_docs:
                raise MergeError(f"bloco SPEC duplicado: {item}")
            seen_docs.add(item)
            content = "\n".join(body).strip()
            if not content:
                raise MergeError(f"SPEC vazio: {item}")
            docs.append((item, content))
        else:
            units.append((item, _parse_spec_body(item, body)))
        i += 1
    if not units and not docs:
        raise MergeError("nenhum bloco SPEC")
    return units, docs


def _parse_named_blocks(stage: str, text: str) -> list[tuple[str, str]]:
    header_re = NAMED_STAGE_HEADER_RE[stage]
    allowed = NAMED_STAGE_ALLOWED[stage]
    label = stage.upper()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[tuple[str, str]] = []
    seen: set[str] = set()
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        match = header_re.match(lines[i])
        if not match:
            accepted = ", ".join(allowed + NAMED_STAGE_PREFIX_HINTS.get(stage, ()))
            raise MergeError(
                f"prosa fora de bloco {label}; esperado: `=== {label}: <id> ===` "
                f"… `=== END ===` com <id> em: {accepted} "
                f"(o nível de confiança vai nos marcadores 🟢🟡🔴, não no cabeçalho)"
            )
        name = _normalize_item(match.group(1))
        prefix_res = NAMED_STAGE_PREFIX_RES.get(stage, ())
        if name not in allowed and not any(p.match(name) for p in prefix_res):
            accepted = ", ".join(allowed + NAMED_STAGE_PREFIX_HINTS.get(stage, ()))
            raise MergeError(
                f"nome de artefato não aceito para {stage}: {name} (aceitos: {accepted})"
            )
        if name in seen:
            raise MergeError(f"bloco {label} duplicado: {name}")
        seen.add(name)
        i += 1
        body: list[str] = []
        while i < len(lines) and not END_RE.match(lines[i]):
            if _is_any_header(lines[i]):
                raise MergeError(f"bloco {label} sem END")
            body.append(lines[i])
            i += 1
        if i >= len(lines):
            raise MergeError(f"bloco {label} sem END")
        content = "\n".join(body).strip()
        if not content:
            raise MergeError(f"{label} vazio: {name}")
        blocks.append((name, content))
        i += 1
    if not blocks:
        raise MergeError(f"nenhum bloco {label}")
    return blocks


def _reject_workdir_scripts(wd: str, input_path: str) -> None:
    wd_abs = os.path.abspath(wd)
    input_abs = os.path.abspath(input_path)
    if input_abs == wd_abs or input_abs.startswith(wd_abs + os.sep):
        if input_abs.lower().endswith(SCRIPT_EXTENSIONS):
            rel = os.path.relpath(input_abs, wd_abs).replace("\\", "/")
            raise MergeError(f"script temporário proibido como input do merge: {rel}")
    scripts: list[str] = []
    if os.path.isdir(wd):
        for dirpath, dirnames, filenames in os.walk(wd):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".state.lock"}]
            for filename in filenames:
                if filename.lower().endswith(SCRIPT_EXTENSIONS):
                    rel = os.path.relpath(os.path.join(dirpath, filename), wd).replace("\\", "/")
                    scripts.append(rel)
    if scripts:
        raise MergeError("script temporário proibido no workdir .codescan: " + ", ".join(sorted(scripts)[:10]))


def _reject_duplicate_artifacts(wd: str, items: list[str]) -> None:
    seen: dict[str, str] = {}
    for item in items:
        artifact = os.path.abspath(_module_artifact(wd, item))
        if artifact in seen:
            raise MergeError(f"MODULE duplicado para artifact {os.path.basename(artifact)}: {seen[artifact]}, {item}")
        seen[artifact] = item


def _content_sha256(content: str) -> str:
    """sha256 dos bytes exatamente como `_write_text` grava (rstrip + \\n)."""
    import hashlib

    return hashlib.sha256((content.rstrip() + "\n").encode("utf-8")).hexdigest()


def _detect_content_duplicates(
    dest_dir: str, pending: list[tuple[str, str, str]]
) -> list[dict]:
    """Detecta artefatos com conteúdo sha256-idêntico sob nome diferente.

    FIX 2 do lote D: `_reject_duplicate_artifacts` só pega duplicata DENTRO
    da mesma chamada de `merge_agent_output` (mesmo slug pedido duas vezes).
    Não havia checagem cross-call por hash de conteúdo — numa execução real
    isso deixou `modules/` com 42 arquivos para 21 módulos reais, pares
    byte-idênticos sob slugs diferentes (criados manualmente pelo operador
    como workaround de um bug de path alheio a este fix).

    `pending` é a lista de (artifact_path, item, content) prestes a ser
    gravada nesta chamada. Compara cada uma contra o que já existe em
    `dest_dir` (chamadas anteriores) e contra as demais entradas do próprio
    `pending` (mesma chamada, slugs diferentes). NUNCA bloqueia nem apaga
    arquivo — só reporta como aviso estruturado no resultado do merge. Quem
    quiser limpar os pares precisa fazer isso explicitamente (fora de
    escopo aqui: nenhuma exclusão automática sem flag).
    """

    existing_hashes: dict[str, str] = {}
    if os.path.isdir(dest_dir):
        for filename in sorted(os.listdir(dest_dir)):
            path = os.path.join(dest_dir, filename)
            if not os.path.isfile(path):
                continue
            try:
                existing_hashes[st_mod.sha256_file(path)] = filename
            except OSError:
                continue

    warnings: list[dict] = []
    seen_in_call: dict[str, str] = {}
    for artifact_path, item, content in pending:
        filename = os.path.basename(artifact_path)
        digest = _content_sha256(content)
        match = existing_hashes.get(digest) or seen_in_call.get(digest)
        if match is not None and match != filename:
            warnings.append({
                "tipo": "artefato_duplicado_por_conteudo",
                "item": item,
                "novo_arquivo": filename,
                "arquivo_existente": match,
                "sha256": digest,
            })
        seen_in_call.setdefault(digest, filename)
    return warnings


def _state_item_from_stage(stage_state: dict, item: str) -> str:
    wanted = _normalize_item(item)
    for key in ("pending", "done", "blocked", "failed", "degraded"):
        for candidate in stage_state.get(key) or []:
            if _normalize_item(str(candidate)) == wanted:
                return str(candidate)
    return item


def _mark_items_done_transaction(wd: str, stage: str, items: list[str]) -> None:
    if not items:
        return
    with st_mod._lock(wd):
        st = st_mod.load(wd) or {}
        s = st.setdefault("stages", {}).setdefault(stage, {"done": [], "pending": []})
        resolved = [_state_item_from_stage(s, item) for item in items]
        done = set(s.get("done") or [])
        pending = set(s.get("pending") or [])
        problem = {key: set(s.get(key) or []) for key in st_mod.ITEM_PROBLEM_STATUSES}
        for item in resolved:
            done.add(item)
            pending.discard(item)
            for values in problem.values():
                values.discard(item)
        s["done"] = sorted(done)
        s["pending"] = sorted(pending)
        for key, values in problem.items():
            s[key] = sorted(values)
        s["items_complete"] = bool(done and not pending and not any(problem.values()))
        if not s["items_complete"]:
            s.pop("finalized", None)
        s["status"] = st_mod._derive_item_stage_status(s, stage)
        st_mod.save(wd, st)


# FIX 3 do lote D: custo do pipeline era inauditável — `_record_agent_run`
# só gravava bytes/sha256, então a análise de custo teve que estimar por
# `bytes/3.6` a partir dos arquivos em disco. Duplicamos a razão char/token
# aqui (em vez de importar scripts/sbindex/tokens.py:16) porque codescan não
# pode depender de sbindex dentro do .pyz empacotado sem risco de
# dependência circular/ausência de módulo no build; se algum dia isso deixar
# de ser um risco, troque esta constante por um import direto de lá.
CHARS_PER_TOKEN = 3.6

AGENT_MODEL_ENV_VAR = "WK_AGENT_MODEL"

# Preços ILUSTRATIVOS em USD por 1.000.000 de tokens (input, output). Não são
# consultados de nenhuma API — atualize esta tabela manualmente para bater
# com a tabela de preços vigente do provedor do modelo antes de confiar em
# `estimated_cost_usd`. Ausência de uma chave aqui só faz o custo ficar
# `None` (o campo é opcional, conforme FIX 3).
MODEL_PRICES_USD_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "claude-opus-4.5": (5.0, 25.0),
    "claude-sonnet-4.5": (3.0, 15.0),
    "claude-haiku-4.5": (1.0, 5.0),
}

# BUG B4 (validação E2E, lote D): ancorado no fim da string (`$`), este regex
# não casava identificadores como `modules-b01-model-test` (sufixo extra
# depois do batch). Sem `$`, casa `-b<NN>` em qualquer posição — cobre tanto
# a convenção pura (`modules-b01`) quanto variações com sufixo.
AGENT_BATCH_SUFFIX_RE = re.compile(r"-b(\d{1,4})", re.I)


def _estimate_tokens(char_count: int | None) -> int | None:
    """Mesma razão de scripts/sbindex/tokens.py:16 (`CHARS_PER_TOKEN`),
    duplicada localmente — ver comentário acima de `CHARS_PER_TOKEN`."""
    if char_count is None:
        return None
    if char_count <= 0:
        return 0
    return int(char_count / CHARS_PER_TOKEN) + 1


def _agent_pack_path_for(wd: str, stage: str, agent: str | None) -> tuple[str | None, str | None]:
    """Localiza o agent-pack correspondente a este run, pelo número de batch
    embutido no identificador `--agent` (convenção do projeto, ex.:
    `modules-b01` -> `agent-packs/modules-batch-01.json`; `-b<NN>` pode
    aparecer em qualquer posição do identificador, ex.:
    `modules-b01-model-test` — BUG B4).

    Devolve `(path, motivo_se_nao_resolvido)`: quando não dá para resolver,
    `path` é `None` e `motivo` explica por quê (agente ausente, sem padrão
    `-b<NN>`, ou pack ausente em disco) — nunca inventa um número, mas
    também nunca fica silencioso: o motivo vai para
    `tokens.input_unresolved_reason` em `_estimate_run_tokens`."""
    if not agent or not agent.strip():
        return None, "nenhum --agent informado"
    agent = agent.strip()
    match = AGENT_BATCH_SUFFIX_RE.search(agent)
    if not match:
        return None, f"agent-pack não localizado para o agente '{agent}': nome não contém o padrão -b<NN>"
    batch = int(match.group(1))
    path = os.path.join(wd, "agent-packs", f"{stage}-batch-{batch:02d}.json")
    if not os.path.isfile(path):
        return None, (
            f"agent-pack não localizado para o agente '{agent}': "
            f"{os.path.basename(path)} não existe em agent-packs/"
        )
    return path, None


def _resolve_agent_model(explicit: str | None) -> str | None:
    """Resolve o identificador do modelo do subagente para este run.

    Ordem: parâmetro explícito (futuro `--model`, ver TODO em
    `merge_agent_output`) > env var `WK_AGENT_MODEL` > `None`."""
    if explicit and explicit.strip():
        return explicit.strip()
    value = os.environ.get(AGENT_MODEL_ENV_VAR)
    return value.strip() if value and value.strip() else None


def _estimate_cost_usd(
    input_tokens: int | None, output_tokens: int | None, model: str | None
) -> float | None:
    if model is None or input_tokens is None or output_tokens is None:
        return None
    prices = MODEL_PRICES_USD_PER_MILLION_TOKENS.get(model)
    if prices is None:
        return None
    input_price, output_price = prices
    cost = (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price
    return round(cost, 6)


def _estimate_run_tokens(
    wd: str, stage: str, agent: str | None, output_text: str | None
) -> dict:
    output_tokens = _estimate_tokens(len(output_text) if output_text is not None else None)
    pack_path, unresolved_reason = _agent_pack_path_for(wd, stage, agent)
    input_tokens = None
    if pack_path is not None:
        try:
            with open(pack_path, encoding="utf-8-sig", errors="replace") as f:
                input_tokens = _estimate_tokens(len(f.read()))
        except OSError as exc:
            input_tokens = None
            unresolved_reason = f"agent-pack '{pack_path}' encontrado mas falhou ao ler: {exc}"
    tokens: dict = {
        "input_estimated": input_tokens,
        "output_estimated": output_tokens,
        "chars_per_token": CHARS_PER_TOKEN,
        "agent_pack_used": pack_path,
        "note": "estimativa local por chars/token (mesma razão de scripts/sbindex/tokens.py:16); não é contagem exata",
    }
    # BUG B4 (validação E2E, lote D): quando input_estimated fica null, o
    # motivo precisa ficar explícito no manifesto — o mesmo tipo de silêncio
    # (rejeitar/zerar sem dizer por quê) que motivou o FIX 1 deste lote.
    if input_tokens is None:
        tokens["input_unresolved_reason"] = unresolved_reason or "motivo desconhecido"
    return tokens


def _record_agent_run(
    wd: str,
    stage: str,
    *,
    input_path: str,
    artifacts: list[MergedArtifact],
    agent: str | None = None,
    output_text: str | None = None,
    model: str | None = None,
) -> str:
    root = os.path.join(wd, "agent-runs")
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{stage}.json")
    previous: dict = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                previous = json.load(f)
        except Exception:
            previous = {}
    runs = previous.get("runs") if isinstance(previous.get("runs"), list) else []
    input_abs = os.path.abspath(input_path)
    items_payload = []
    for artifact in artifacts:
        artifact_payload = [
            {
                "path": artifact_path,
                "sha256": st_mod.sha256_file(artifact_path),
                "bytes": os.path.getsize(artifact_path),
            }
            for artifact_path in artifact.artifacts
        ]
        items_payload.append({"item": artifact.item, "artifacts": artifact_payload})
    resolved_model = _resolve_agent_model(model)
    tokens = _estimate_run_tokens(wd, stage, agent, output_text)
    runs.append(
        {
            "stage": stage,
            "input": input_abs,
            "input_sha256": st_mod.sha256_file(input_abs),
            "input_bytes": os.path.getsize(input_abs),
            "agent": agent,
            "model": resolved_model,
            "tokens": tokens,
            "estimated_cost_usd": _estimate_cost_usd(
                tokens["input_estimated"], tokens["output_estimated"], resolved_model
            ),
            "items": items_payload,
            "items_count": len(artifacts),
            "artifacts_count": sum(len(artifact.artifacts) for artifact in artifacts),
            "created_at": st_mod._now(),
        }
    )
    out = {"schema": "wiki-ai.agent-runs.v2", "stage": stage, "runs": runs}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def merge_agent_output(
    wd: str, stage: str, input_path: str, *, agent: str | None = None, model: str | None = None
) -> dict:
    """`model` (FIX 3 do lote D): identificador do modelo do subagente, para
    auditar custo em `agent-runs/<stage>.json`. Se `None`, `_record_agent_run`
    cai para a env var `WK_AGENT_MODEL` (ver `_resolve_agent_model`).

    TODO (fora do escopo deste lote — exige editar cli.py, que está fora dos
    arquivos autorizados aqui): expor um `--model <id>` em
    `merge-agent-output` que popule este parâmetro; até lá, a única forma de
    informar o modelo pela CLI é a env var acima.
    """

    _reject_workdir_scripts(wd, input_path)
    with open(input_path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    _reject_noise(text)
    merged: list[MergedArtifact] = []
    generated: list[str] = []
    blockers: list[str] = []
    warnings: list[dict] = []
    if stage == "modules":
        blocks = _parse_modules(text)
        _reject_duplicate_artifacts(wd, [item for item, _content in blocks])
        planned = [(_module_artifact(wd, item), item, content) for item, content in blocks]
        warnings = _detect_content_duplicates(os.path.join(wd, "modules"), planned)
        overrides = {path: content for path, _item, content in planned}
        for artifact, item, content in planned:
            _write_text(artifact, content)
            merged.append(MergedArtifact(item=item, artifacts=[artifact]))
        docs = _module_docs(wd, overrides)
        generated, blockers = _generate_module_sdd(wd, docs)
        manifest = _record_agent_run(
            wd, stage, input_path=input_path, artifacts=merged, agent=agent, output_text=text, model=model
        )
        _mark_items_done_transaction(wd, stage, [item for _artifact, item, _content in planned])
    elif stage == "specs":
        units, named_docs = _parse_specs(text)
        for item, files in units:
            root = _spec_dir(wd, item)
            artifacts = []
            for filename, content in files.items():
                artifact = os.path.join(root, filename)
                _write_text(artifact, content)
                artifacts.append(artifact)
            merged.append(MergedArtifact(item=item, artifacts=artifacts))
        for name, content in named_docs:
            artifact = _specs_doc_artifact(wd, name)
            _write_text(artifact, content)
            merged.append(MergedArtifact(item=name, artifacts=[artifact]))
        manifest = _record_agent_run(
            wd, stage, input_path=input_path, artifacts=merged, agent=agent, output_text=text, model=model
        )
        # Só units entram no estado do stage: documentos nomeados (gaps,
        # confidence-report, matrizes, user-stories, openapi) não são "itens"
        # de specs e quebrariam _validate_item_done se marcados como done.
        _mark_items_done_transaction(wd, stage, [item for item, _files in units])
    elif stage in NAMED_STAGE_HEADER_RE:
        blocks = _parse_named_blocks(stage, text)
        for name, content in blocks:
            artifact = _named_artifact(wd, name)
            _write_text(artifact, content)
            merged.append(MergedArtifact(item=name, artifacts=[artifact]))
        manifest = _record_agent_run(
            wd, stage, input_path=input_path, artifacts=merged, agent=agent, output_text=text, model=model
        )
        _mark_items_done_transaction(wd, stage, [name for name, _content in blocks])
    else:
        raise MergeError(f"stage não suportado: {stage}")
    return {
        "stage": stage,
        "items": len(merged),
        "artifacts": sum(len(item.artifacts) for item in merged),
        "generated": generated,
        "blockers": blockers,
        "warnings": warnings,
        "manifest": manifest,
    }
