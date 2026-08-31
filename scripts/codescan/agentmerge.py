"""Deterministic merge for subagent outputs."""

from __future__ import annotations

import json
import os
import re
import unicodedata
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
# F-41: o contrato do estágio manda o subagente devolver `=== FAILED MODULE: <id> ===`
# quando o item é impossível (`cli._handoff_prompt`). O parser de merge não aceita
# esse bloco (ele cai em "prosa fora de bloco MODULE"), mas o <id> dele PRECISA ser
# validado contra o batch do plano igual ao do bloco MODULE — senão o operador
# recebe "prosa fora de bloco" para um id inventado e não descobre a causa real.
FAILED_MODULE_RE = re.compile(r"^=== FAILED MODULE:\s*(.+?)\s*===\s*$")
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

# F-42: `IDENTIFIER_RE` só reconhecia identificador ENTRE CRASES, mas o contrato
# de saída do subagente PROÍBE eco de código — os dois se contradizem e o efeito
# prático é `sdd/data-dictionary.md` nunca ser gerado (numa execução real: 21
# módulos, 312 citações arquivo:linha, 0 entidades reconhecidas). A extração
# passa a aceitar também tipo capitalizado FORA de crase, mas só dentro da seção
# "Estruturas de dados" e só com forma conservadora de identificador — ver
# `_looks_like_type_word`.
DICT_CAMEL_WORD_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{3,}\b")
DICT_CAMEL_INNER_RE = re.compile(r"[a-z][A-Z]")
# Sufixos que, sozinhos, já provam que a palavra é nome de tipo (mesmo vocabulário
# usado por `_entity_kind` para classificar a entidade).
DICT_TYPE_SUFFIXES = (
    "dto", "request", "response", "entity", "model", "event", "repository",
    "gateway", "client", "rule", "rules", "exception", "error", "properties",
    "configuration", "config", "service", "controller", "handler", "policy",
    "command", "query", "factory", "mapper", "adapter", "aggregate", "payload",
)
# Palavras PT comuns que aparecem capitalizadas por início de frase/rótulo e não
# são nome de tipo. Comparadas sem acento e em casefold (`_fold`).
DICT_PT_STOPWORDS = frozenset("""
    estrutura estruturas dados campo campos tipo tipos classe classes registro
    registros objeto objetos entidade entidades valor valores lista listas mapa
    mapas conjunto conjuntos nenhum nenhuma nada apenas somente todos todas cada
    este esta esse essa aquele aquela quando onde como porque para pela pelo
    pelos pelas sobre entre depois antes durante contudo porem ainda talvez
    possivelmente inferido inferida inferidos inferidas observado observada
    confirmado confirmada presente ausente ausencia contrato contratos fluxo
    fluxos responsabilidade responsabilidades dependencia dependencias
    rastreabilidade lacuna lacunas evidencia evidencias citacao citacoes arquivo
    arquivos linha linhas modulo modulos pacote pacotes servico servicos metodo
    metodos funcao funcoes interface interfaces enum record records validacao
    validacoes regra regras persistencia configuracao implementacao requisicao
    resposta retorno entrada saida erro erros excecao excecoes sistema aplicacao
    dominio infraestrutura camada camadas banco tabela tabelas coluna colunas
    chave chaves indice consulta consultas cliente clientes usuario usuarios nome
    nomes data datas hora numero texto booleano inteiro decimal total tamanho
    limite limites estado estados nivel ordem grupo grupos item itens parte
    partes nota notas observacao observacoes detalhe detalhes exemplo exemplos
    caso casos ponto pontos etapa etapas passo passos acao acoes evento eventos
    atributo atributos propriedade propriedades opcional obrigatorio obrigatoria
    padrao versao versoes suporte tambem assim entao segundo terceiro primeiro
    primeira segunda outro outra outros outras mesmo mesma muito pouco maior
    menor melhor pior novo nova antigo atual atuais proprio propria varios varias
    alguns algumas qualquer quaisquer sempre nunca
    constante constantes falha falhas justificativa justificativas mensagem
    mensagens sobrecarga sobrecargas resultado resultados motivo motivos decisao
    decisoes chamada chamadas leitura escrita criacao atualizacao remocao
    publicacao assinatura assinaturas colecao colecoes sequencia tentativa
    tentativas politica politicas depende dependem contem existe existem
    retorna recebe envia grava usa usam deve devem pode podem dois tres quatro
    cinco
""".split())
# Seção do artefato de módulo onde o contrato manda declarar entidades/tipos.
DICT_DATA_SECTIONS = frozenset({
    "estruturas de dados",
    "estrutura de dados",
    "estruturas de dados/entidades",
    "estruturas de dados / entidades",
    "entidades e estruturas de dados",
    "data structures",
    "data structures/entities",
})
# Demais seções canônicas do artefato de módulo (`sdd.COMPACT_OUTPUT_CONTRACTS`)
# — servem só para saber ONDE a seção de dados termina.
DICT_SECTION_TITLES = DICT_DATA_SECTIONS | frozenset({
    "responsabilidade", "responsabilidades", "fluxo", "fluxos", "fluxo principal",
    "dependencia", "dependencias", "rastreabilidade", "lacuna", "lacunas",
    "risco", "riscos", "riscos e lacunas", "evidencia", "evidencias", "escopo",
    "arquivos", "citacoes", "observacoes", "resumo", "visao geral", "regras",
    "contratos", "seguranca", "testes", "metricas",
})
DICT_HEADING_STRIP_RE = re.compile(r"^\s*#{1,6}\s*")
# `- 🟢 CatalogUpdatedEvent (record): eventId, eventVersion, occurredAt. Foo.java:9`
DICT_STRUCT_LEAD_RE = re.compile(r"^[^A-Za-z0-9]*")
DICT_STRUCT_DECL_RE = re.compile(r"^([A-Z][A-Za-z0-9]{3,})\s*(?:\([^)]*\))?\s*:\s*(.+)$")
DICT_FIELD_TOKEN_RE = re.compile(r"^\s*([a-z][A-Za-z0-9_]{2,})\b")
FIELD_RESERVED = frozenset({
    "public", "private", "return", "record", "class", "final", "static",
    "string", "integer", "boolean", "optional", "list", "map", "bigdecimal",
    "instant", "duration", "uuid", "null", "true", "false",
})


def _fold(value: str) -> str:
    """casefold sem acento — comparação determinística de rótulo PT-BR."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


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
    fields: list[str] = []
    for word in FIELD_WORD_RE.findall(text):
        if word.lower() in FIELD_RESERVED:
            continue
        if word not in fields:
            fields.append(word)
    return fields[:6]


def _heading_label(line: str) -> str | None:
    """Rótulo de seção da linha (sem `#`, sem `**`, sem `:`), já normalizado por
    `_fold`; `None` quando a linha é conteúdo (bullet, tabela, citação, prosa
    longa) em vez de cabeçalho."""
    raw = line.strip()
    if not raw:
        return None
    if raw[0] in "-*+|>" and not raw.startswith("**"):
        return None
    label = DICT_HEADING_STRIP_RE.sub("", raw).strip().strip("*_").strip().rstrip(":").strip()
    if not label or len(label) > 64:
        return None
    return _fold(label)


def _data_section_lines(content: str) -> set[int]:
    """Índices (0-based) das linhas dentro da seção "Estruturas de dados".

    Só essa seção libera o reconhecimento de tipo fora de crase (F-42): o resto
    do artefato é prosa PT-BR e produziria entidade inventada.
    """
    inside = False
    lines: set[int] = set()
    for idx, line in enumerate(content.splitlines()):
        label = _heading_label(line)
        if label is not None and label in DICT_SECTION_TITLES:
            inside = label in DICT_DATA_SECTIONS
            continue
        if label is not None and line.strip().startswith("#"):
            inside = False  # qualquer outro cabeçalho markdown encerra a seção
            continue
        if inside:
            lines.add(idx)
    return lines


def _looks_like_type_word(word: str, *, line_has_citation: bool) -> bool:
    """Palavra CamelCase >= 4 chars que é, conservadoramente, nome de tipo.

    Exige pelo menos uma minúscula (descarta siglas `JSON`/`HTTP`), rejeita
    palavra PT comum capitalizada por início de frase e, além disso, exige UMA
    das provas: transição camelCase interna (`QuoteReceivedEvent`), sufixo
    típico de tipo (`...Rule`, `...Dto`, `...Gateway`) ou uma citação
    `arquivo:linha` na MESMA linha (a linha já está ancorada em evidência).
    """
    if len(word) < 4 or not any(ch.islower() for ch in word):
        return False
    folded = _fold(word)
    if folded in DICT_PT_STOPWORDS:
        return False
    if DICT_CAMEL_INNER_RE.search(word):
        return True
    if folded.endswith(DICT_TYPE_SUFFIXES):
        return True
    return line_has_citation


def _section_type_candidates(line: str) -> list[str]:
    line_has_citation = bool(ev_mod.citations(line))
    out: list[str] = []
    for word in DICT_CAMEL_WORD_RE.findall(line):
        if word in out:
            continue
        if _looks_like_type_word(word, line_has_citation=line_has_citation):
            out.append(word)
    return out


def _struct_declaration(line: str) -> tuple[str, list[str]] | None:
    """`Entidade (record): campoA, campoB(...)` -> `("Entidade", ["campoA", ...])`.

    Deliberadamente estreito: só dispara quando o nome do tipo abre a linha e é
    seguido de `:`. Sem isso, varrer a linha inteira atrás de campos transformaria
    prosa PT-BR ("inferido", "como", "delega") em atributo do dicionário.
    """
    body = DICT_STRUCT_LEAD_RE.sub("", line)
    match = DICT_STRUCT_DECL_RE.match(body)
    if not match:
        return None
    entity = match.group(1)
    if not _looks_like_type_word(entity, line_has_citation=bool(ev_mod.citations(line))):
        return None
    fields: list[str] = []
    for chunk in match.group(2).split(","):
        token = DICT_FIELD_TOKEN_RE.match(chunk)
        if not token:
            continue
        field = token.group(1)
        if field.lower() in FIELD_RESERVED or _fold(field) in DICT_PT_STOPWORDS:
            continue
        if field in fields:
            continue
        fields.append(field)
    if not fields:
        return None
    return entity, fields[:12]


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
        lines = content.splitlines()
        data_lines = _data_section_lines(content)
        candidates: list[str] = []
        for raw in IDENTIFIER_RE.findall(content):
            for part in re.split(r"[, ]+", raw):
                name = _clean_identifier(part)
                if TYPE_WORD_RE.fullmatch(name) and len(name) >= 3:
                    candidates.append(name)
        # F-42: tipos capitalizados FORA de crase, restritos à seção
        # "Estruturas de dados" — é assim que o subagente declara entidade sem
        # violar a proibição de eco de código.
        for idx in sorted(data_lines):
            candidates.extend(_section_type_candidates(lines[idx]))
        for idx in sorted(data_lines):
            declaration = _struct_declaration(lines[idx])
            if declaration is None:
                continue
            entity, declared_fields = declaration
            line_citations = [_citation_text(c) for c in ev_mod.citations(lines[idx])]
            line_citation = line_citations[0] if line_citations else module_citation
            for field in declared_fields:
                fields.append({
                    "entity": entity,
                    "field": field,
                    "type": "campo declarado em Estruturas de dados",
                    "module": module,
                    "citation": line_citation,
                })
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
    # F-42: o blocker antigo ("exige entidades/tipos e ao menos 2 citações")
    # não dizia QUAL das duas metades reprovou — com 312 citações e 0 entidades
    # o operador lia "faltam citações" e reescrevia o artefato inteiro à toa.
    reprovou: list[str] = []
    if not entities:
        reprovou.append("entidades/tipos (nenhuma reconhecida na seção `Estruturas de dados`)")
    if len(citations) < 2:
        reprovou.append(f"citações arquivo:linha (mínimo 2, obtidas {len(citations)})")
    if reprovou:
        return None, (
            "dados insuficientes para data-dictionary.md: "
            f"entidades={len(entities)}, citacoes={len(citations)}; "
            "reprovou: " + " e ".join(reprovou)
        )
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

# F-36: o regex PRECISA ficar ancorado no fim do identificador (`$`).
#
# Histórico: o BUG B4 (lote D) removeu a âncora para aceitar sufixos extras
# depois do batch (`modules-b01-model-test`). O efeito colateral é pior que o
# bug original: sem `$`, qualquer `-b<dígito>` no MEIO do nome vira "número do
# batch". `--agent web-b2b-team` casa `-b2` e o run passa a ser contabilizado
# contra `agent-packs/<stage>-batch-02.json` — custo, tokens e sha atribuídos
# ao batch errado, silenciosamente.
#
# Contrato atual: o número do batch é o SUFIXO do identificador
# (`<stage>-b<NN>`, exatamente como `cli._run_stage_agent_slot` emite). Nome
# que não termine em `-b<NN>` não resolve pack — e o motivo vai explícito em
# `tokens.input_unresolved_reason` (nunca silencioso).
AGENT_BATCH_SUFFIX_RE = re.compile(r"-b(\d{1,4})$", re.I)


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
    no SUFIXO do identificador `--agent` (convenção do projeto, ex.:
    `modules-b01` -> `agent-packs/modules-batch-01.json`).

    F-36: o `-b<NN>` precisa ser o FIM do identificador. Aceitar `-b<NN>` no
    meio do nome (como fazia o BUG B4) faz `--agent web-b2b-team` casar `-b2`
    e atribuir custo/tokens ao batch 02, que nem é dele.

    Devolve `(path, motivo_se_nao_resolvido)`: quando não dá para resolver,
    `path` é `None` e `motivo` explica por quê (agente ausente, sem sufixo
    `-b<NN>`, ou pack ausente em disco) — nunca inventa um número, mas
    também nunca fica silencioso: o motivo vai para
    `tokens.input_unresolved_reason` em `_estimate_run_tokens`."""
    if not agent or not agent.strip():
        return None, "nenhum --agent informado"
    agent = agent.strip()
    match = AGENT_BATCH_SUFFIX_RE.search(agent)
    if not match:
        return None, (
            f"agent-pack não localizado para o agente '{agent}': "
            "nome não termina no padrão -b<NN> (ex.: modules-b01)"
        )
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


def _agent_runs_path(wd: str, stage: str) -> str:
    return os.path.join(wd, "agent-runs", f"{stage}.json")


def _agent_runs_entries(wd: str, stage: str) -> list[dict]:
    """Runs já gravados em `agent-runs/<stage>.json` (lista vazia se o
    manifesto não existe ou está ilegível — ausência de manifesto nunca é
    tratada como conflito)."""
    path = _agent_runs_path(wd, stage)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    runs = data.get("runs")
    if not isinstance(runs, list):
        return []
    return [run for run in runs if isinstance(run, dict)]


def _run_is_current(run: dict) -> bool:
    """Mesma semântica de `sdd._run_status`: run sem `status` (schema v2
    legado, nunca tocado por `redo`) conta como `current`."""
    status_fn = getattr(sdd_mod, "_run_status", None)
    if status_fn is not None:
        try:
            return status_fn(run) == "current"
        except Exception:  # pragma: no cover - manifesto corrompido
            pass
    return run.get("status") in (None, "current")


def _manifest_path_key(wd: str, value) -> str | None:
    """Chave comparável para um caminho de artefato do manifesto (absoluto ou
    relativo ao workdir; case-insensitive no Windows)."""
    if not isinstance(value, str) or not value.strip():
        return None
    path = value.strip()
    if not os.path.isabs(path):
        path = os.path.join(wd, *path.replace("\\", "/").split("/"))
    return os.path.normcase(os.path.abspath(path))


def _scan_current_runs(wd: str, stage: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Varre os runs `current` do manifesto e devolve `(por_item, por_artefato)`.

    `por_item`: item normalizado -> `{"run", "agent", "artifacts"}`.
    `por_artefato`: chave de caminho -> `{"run", "agent", "item", "artefato"}`.

    Run posterior sobrescreve o anterior nos dois mapas: o dono vigente de um
    artefato é sempre o run `current` mais recente que o gravou.
    """
    by_item: dict[str, dict] = {}
    by_artifact: dict[str, dict] = {}
    for idx, run in enumerate(_agent_runs_entries(wd, stage), start=1):
        if not _run_is_current(run):
            continue
        agent = run.get("agent")
        agent = agent.strip() if isinstance(agent, str) and agent.strip() else None
        for entry in run.get("items") or []:
            if not isinstance(entry, dict):
                continue
            raw_item = str(entry.get("item") or "").strip()
            if not raw_item:
                continue
            item = raw_item.replace("\\", "/").strip("/")
            paths: list[str] = []
            for artifact in entry.get("artifacts") or []:
                value = artifact.get("path") if isinstance(artifact, dict) else artifact
                key = _manifest_path_key(wd, value)
                if key is None:
                    continue
                paths.append(str(value))
                by_artifact[key] = {
                    "run": idx,
                    "agent": agent,
                    "item": item,
                    "artefato": str(value),
                }
            by_item[item] = {"run": idx, "agent": agent, "artifacts": paths}
    return by_item, by_artifact


def current_run_owners(wd: str, stage: str) -> dict[str, dict]:
    """item -> `{"run": <id 1-based>, "agent": <str|None>, "artifacts": [...]}`
    do run `current` mais recente que cobre o item.

    Complementa `sdd.current_run_items` (que só devolve o id do run): a
    mensagem de conflito de re-merge (F-08, em `cli._merge_redo_conflict_error`)
    precisa dizer QUEM é o dono — run E agent — de cada item conflitante."""
    by_item, _by_artifact = _scan_current_runs(wd, stage)
    return by_item


def _agent_batch_number(agent: str | None) -> int | None:
    if not agent or not agent.strip():
        return None
    match = AGENT_BATCH_SUFFIX_RE.search(agent.strip())
    return int(match.group(1)) if match else None


def _same_agent_identity(left: str | None, right: str | None) -> bool:
    """Mesmo agent/batch? Compara o identificador literal e, em fallback, o
    número de batch do sufixo (`-b<NN>`, F-36) — dois nomes diferentes para o
    MESMO batch não são "outro batch"."""
    a = (left or "").strip().lower()
    b = (right or "").strip().lower()
    if a == b:
        return True
    batch_a, batch_b = _agent_batch_number(left), _agent_batch_number(right)
    return batch_a is not None and batch_a == batch_b


MAX_REPORTED_PLAN_ID_ERRORS = 10
MAX_LISTED_PLAN_ITEMS = 40
PLAN_ID_ACTION = "use exatamente o path do campo ITENS/items do seu batch"


def _plan_manifest(wd: str, stage: str) -> dict | None:
    """`agent-runs/<stage>-plan.json` (o manifesto de fan-out escrito por
    `cli.cmd_run_stage`). Ausente/ilegível devolve `None` — a validação de id
    então NÃO acontece: merge fora do fluxo de fan-out (testes, uso manual,
    workdir legado) continua valendo."""
    path = os.path.join(wd, "agent-runs", f"{stage}-plan.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _plan_batch_items(wd: str, stage: str, agent: str | None) -> tuple[list[str] | None, str | None]:
    """Itens canônicos do batch deste `--agent`, ou `(None, motivo)`.

    O batch é identificado pelo `agent_slot` do plano (match literal) e, em
    fallback, pelo número de batch do sufixo `-b<NN>` (`AGENT_BATCH_SUFFIX_RE`,
    F-36) — dois nomes para o mesmo batch resolvem o mesmo item set.
    """
    plan = _plan_manifest(wd, stage)
    if plan is None:
        return None, f"agent-runs/{stage}-plan.json ausente ou ilegível"
    batches = plan.get("batches")
    if not isinstance(batches, list) or not batches:
        return None, f"agent-runs/{stage}-plan.json sem batches"
    wanted = (agent or "").strip()
    wanted_key = wanted.casefold()
    number = _agent_batch_number(wanted)
    chosen: dict | None = None
    if wanted_key:
        for batch in batches:
            if isinstance(batch, dict) and str(batch.get("agent_slot") or "").strip().casefold() == wanted_key:
                chosen = batch
                break
    if chosen is None and number is not None:
        for batch in batches:
            if not isinstance(batch, dict):
                continue
            slot_number = _agent_batch_number(str(batch.get("agent_slot") or ""))
            batch_number = batch.get("batch") if isinstance(batch.get("batch"), int) else None
            if number in (slot_number, batch_number):
                chosen = batch
                break
    if chosen is None:
        return None, (
            f"--agent {wanted or '(vazio)'} não corresponde a nenhum batch de "
            f"agent-runs/{stage}-plan.json"
        )
    items = [str(value).strip() for value in (chosen.get("items") or []) if str(value).strip()]
    if not items:
        return None, f"batch de agent-runs/{stage}-plan.json sem items"
    return items, None


def _plan_item_key(value: str) -> str:
    """Chave comparável de um ITEM do plano: só `\\`->`/` (mesma normalização de
    `_normalize_item`), casefold. Ponto NÃO vira barra aqui — path de item pode
    conter ponto legítimo e o item do plano é a verdade, não o palpite."""
    return re.sub(r"/+", "/", value.strip().replace("\\", "/")).strip("/").casefold()


def _block_id_forms(value: str) -> list[str]:
    """Formas comparáveis do `<id>` que veio no cabeçalho do bloco: a
    normalizada (`\\`->`/`) e, adicionalmente, a com pontos virados barra
    (`domain.event` -> `domain/event`) — foi exatamente assim que a LLM
    encurtou o path do item numa execução real."""
    base = re.sub(r"/+", "/", value.strip().replace("\\", "/")).strip("/")
    forms: list[str] = []
    for candidate in (base, base.replace(".", "/")):
        normalized = re.sub(r"/+", "/", candidate).strip("/").casefold()
        if normalized and normalized not in forms:
            forms.append(normalized)
    return forms


def _match_plan_item(block_id: str, items: list[str]) -> tuple[list[str], str]:
    """`(itens_do_batch_que_casam, modo)` com `modo` em `exato`/`sufixo`.

    Sufixo casa só em fronteira de segmento (`.../domain/event` casa
    `domain/event` e `domain.event`, mas nunca `event` colado em `subevent`).
    """
    forms = _block_id_forms(block_id)
    if not forms:
        return [], "exato"
    keys = [(item, _plan_item_key(item)) for item in items]
    exact = sorted({item for item, key in keys if key in forms})
    if exact:
        return exact, "exato"
    suffix = sorted({
        item for item, key in keys
        if any(key.endswith("/" + form) for form in forms)
    })
    return suffix, "sufixo"


def _module_header_ids(text: str) -> list[tuple[str, str]]:
    """`(id_bruto, rótulo_do_bloco)` de cada cabeçalho MODULE/FAILED MODULE."""
    found: list[tuple[str, str]] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = MODULE_RE.match(line)
        if match:
            found.append((match.group(1).strip(), "MODULE"))
            continue
        match = FAILED_MODULE_RE.match(line)
        if match:
            found.append((match.group(1).strip(), "FAILED MODULE"))
    return found


def _validate_module_block_ids(
    wd: str, stage: str, text: str, agent: str | None
) -> tuple[dict[str, str], list[dict]]:
    """F-41: o `<id>` de cada bloco tem que ser um item DO BATCH deste `--agent`.

    Falha real: a LLM escreveu `=== MODULE: domain.event ===` (nome inventado, o
    item era `quote-service\\src\\...\\domain\\event`). O merge aceitava qualquer
    string, gravava `modules/domain-event.md` e marcava `domain.event` como
    `done` — item fantasma. O item REAL nunca saía de `pending`, então
    `items_complete` nunca virava `True` e o estágio ficava travado no blocker
    "módulos não resolvidos", sem nada apontando a causa.

    Devolve `(mapeamento_id_normalizado -> item_canônico, mapeados)`. O
    mapeamento tolerante é determinístico e só cobre sufixo ÚNICO: id que não
    casa com item nenhum, ou casa com mais de um, vira `MergeError` com a lista
    dos itens válidos do batch. `FAILED MODULE` segue a mesma regra.

    Sem plano de fan-out resolvível (arquivo ausente, batch não identificado
    pelo `--agent`) a validação é no-op — merge manual/legado não regride.
    """
    items, _reason = _plan_batch_items(wd, stage, agent)
    if items is None:
        return {}, []
    headers = _module_header_ids(text)
    if not headers:
        return {}, []
    mapping: dict[str, str] = {}
    mapeados: list[dict] = []
    violacoes: list[dict] = []
    for raw, kind in headers:
        matches, mode = _match_plan_item(raw, items)
        if len(matches) == 1:
            canonical = matches[0]
            if mode == "sufixo":
                mapping[_normalize_item(raw)] = _normalize_item(canonical)
                mapeados.append({
                    "bloco": kind,
                    "informado": raw,
                    "item": canonical,
                    "modo": "sufixo",
                    "agent": (agent or "").strip() or None,
                })
            continue
        violacoes.append({
            "tipo": "id_de_bloco_ambiguo" if matches else "id_de_bloco_fora_do_batch",
            "bloco": kind,
            "informado": raw,
            "candidatos": matches,
            "agent": (agent or "").strip() or None,
        })
    if not violacoes:
        return mapping, mapeados
    total = len(violacoes)
    shown = violacoes[:MAX_REPORTED_PLAN_ID_ERRORS]
    lines = [
        "id de bloco fora do batch do plano: "
        + ", ".join(violacao["informado"] for violacao in shown)
    ]
    if total > len(shown):
        lines[0] += f" (+{total - len(shown)} outro(s), total {total})"
    for violacao in shown:
        if violacao["candidatos"]:
            lines.append(
                f"- `{violacao['informado']}` ({violacao['bloco']}) casa com mais de um item "
                "do batch: " + ", ".join(f"`{item}`" for item in violacao["candidatos"])
            )
        else:
            lines.append(
                f"- `{violacao['informado']}` ({violacao['bloco']}) não corresponde a nenhum "
                "item do batch"
            )
    if total > len(shown):
        lines.append(f"... {total - len(shown)} id(s) omitido(s) (total {total})")
    lines.append(f"itens válidos do batch (--agent {(agent or '').strip() or 'não informado'}):")
    lines.extend(f"  - {item}" for item in items[:MAX_LISTED_PLAN_ITEMS])
    if len(items) > MAX_LISTED_PLAN_ITEMS:
        lines.append(f"  ... {len(items) - MAX_LISTED_PLAN_ITEMS} item(ns) omitido(s)")
    lines.append(f"acao: {PLAN_ID_ACTION}")
    raise MergeError(
        "\n".join(lines),
        violacoes=shown,
        violacoes_total=total,
        acao=PLAN_ID_ACTION,
    )


MAX_REPORTED_OWNER_CONFLICTS = 10


def _reject_foreign_artifact_overwrite(
    wd: str, stage: str, planned: list[tuple[str, str]], agent: str | None
) -> None:
    """F-24: recusa sobrescrever artefato que pertence a OUTRO batch.

    Cenário real: dois batches reivindicam o mesmo bloco (slug colidente, item
    renomeado, output copiado entre batches). Como `_write_text` gravava
    incondicionalmente e `_detect_content_duplicates` só AVISA, o segundo merge
    sobrescrevia o artefato do primeiro sem erro — e o manifesto ficava com dois
    runs `current` apontando para o mesmo arquivo, com o sha do primeiro já
    inválido (`P0: artefato alterado após o merge`).

    Rejeita quando as DUAS condições valem para um artefato de destino:
      1. ele já é artefato de um run `current` de outro agent/batch; e
      2. o item DONO desse artefato não está no conjunto deste merge.

    Se o item dono está neste merge, o caso é re-merge do próprio item e segue
    pelo fluxo de F-08 (`cli._merge_redo_conflict_error` -> `redo`); aqui não
    se duplica esse bloqueio.
    """

    if not planned:
        return
    _by_item, owners = _scan_current_runs(wd, stage)
    if not owners:
        return
    mine = {_normalize_item(item) for _path, item in planned}
    conflicts: list[dict] = []
    for path, item in planned:
        owner = owners.get(_manifest_path_key(wd, path) or "")
        if owner is None:
            continue
        if owner["item"] in mine:
            continue  # re-merge do próprio item -> F-08 (redo)
        if _same_agent_identity(owner["agent"], agent):
            continue  # mesmo batch reescrevendo o próprio artefato
        conflicts.append({
            "tipo": "artefato_de_outro_batch",
            "item": item,
            "artefato": os.path.relpath(os.path.abspath(path), os.path.abspath(wd)).replace("\\", "/"),
            "dono_item": owner["item"],
            "dono_agent": owner["agent"],
            "dono_run": owner["run"],
            "agent": (agent or "").strip() or None,
        })
    if not conflicts:
        return
    total = len(conflicts)
    shown = conflicts[:MAX_REPORTED_OWNER_CONFLICTS]
    first_owner = shown[0]["dono_item"]
    acao = (
        f"não sobrescreva o artefato de outro batch: rode `redo {stage} --item <item-do-dono>` "
        f"(ex.: `redo {stage} --item {first_owner}`) para superar o run dono antes de re-mergear, "
        "OU corrija o output deste batch para não reivindicar esse artefato"
    )
    lines = [
        "artefato já pertence a outro batch: "
        + ", ".join(conflict["artefato"] for conflict in shown)
    ]
    if total > len(shown):
        lines[0] += f" (+{total - len(shown)} outro(s), total {total})"
    for conflict in shown:
        lines.append(
            f"- {conflict['artefato']}: dono atual = run #{conflict['dono_run']} "
            f"(agent {conflict['dono_agent'] or 'não informado'}, item `{conflict['dono_item']}`); "
            f"este merge (agent {conflict['agent'] or 'não informado'}) tentaria gravá-lo "
            f"como item `{conflict['item']}`"
        )
    if total > len(shown):
        lines.append(f"... {total - len(shown)} conflito(s) omitido(s) (total {total})")
    lines.append(f"acao: {acao}")
    raise MergeError(
        "\n".join(lines),
        violacoes=shown,
        violacoes_total=total,
        acao=acao,
    )


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
    path = _agent_runs_path(wd, stage)
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
    mapeados: list[dict] = []
    if stage == "modules":
        # F-41: valida os ids ANTES de parsear/gravar qualquer coisa — id
        # inventado nunca chega a virar artefato nem item `done` fantasma.
        id_mapping, mapeados = _validate_module_block_ids(wd, stage, text, agent)
        blocks = _parse_modules(text)
        if id_mapping:
            blocks = [(id_mapping.get(item, item), content) for item, content in blocks]
        _reject_duplicate_artifacts(wd, [item for item, _content in blocks])
        planned = [(_module_artifact(wd, item), item, content) for item, content in blocks]
        _reject_foreign_artifact_overwrite(
            wd, stage, [(path, item) for path, item, _content in planned], agent
        )
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
        # F-24: resolve TODOS os destinos antes de gravar qualquer um — o
        # bloqueio por dono precisa acontecer com o disco ainda intacto.
        planned_units = [
            (item, [(os.path.join(_spec_dir(wd, item), filename), content)
                    for filename, content in files.items()])
            for item, files in units
        ]
        planned_docs = [(name, _specs_doc_artifact(wd, name), content) for name, content in named_docs]
        _reject_foreign_artifact_overwrite(
            wd,
            stage,
            [(path, item) for item, files in planned_units for path, _content in files]
            + [(path, name) for name, path, _content in planned_docs],
            agent,
        )
        for item, files in planned_units:
            artifacts = []
            for artifact, content in files:
                _write_text(artifact, content)
                artifacts.append(artifact)
            merged.append(MergedArtifact(item=item, artifacts=artifacts))
        for name, artifact, content in planned_docs:
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
        planned_named = [(_named_artifact(wd, name), name, content) for name, content in blocks]
        _reject_foreign_artifact_overwrite(
            wd, stage, [(path, name) for path, name, _content in planned_named], agent
        )
        for artifact, name, content in planned_named:
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
        "mapeados": mapeados,
        "manifest": manifest,
    }
