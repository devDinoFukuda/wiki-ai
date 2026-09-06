"""Montagem e orçamento de pacotes de contexto (plano §7.3.1, F05, onda W4).

Este módulo é o único responsável por decidir **o que cabe** no pacote que o
`coordinator.py` vizinho entrega ao executor de uma tarefa. Ele não decide
*quem* executa, nem *como* o resultado volta — só monta `objetivo + schema +
referências + trechos necessários + limites conhecidos`, mede o payload real
e recusa (nunca trunca em silêncio) o que não cabe.

Regra do plano -> onde é aplicada em código executável:

| Regra do plano (§7.3.1 / F05)                                          | Onde é aplicada |
|---------------------------------------------------------------------- |------------------|
| Pacote nunca carrega histórico integral de execução                    | `_objective_envelope()` só copia campos de uma lista fechada (objetivo/schema/limites); nenhum campo desconhecido do `objective_dict` é repassado |
| Teto conta instruções+schemas (overhead) e reserva de saída, não só evidência | `Budget.effective_max_bytes` / `Budget.effective_max_tokens` |
| Comparação SEMPRE contra o payload serializado real                    | `serialized_bytes()`, chamada a cada tentativa em `_fit_to_budget()` |
| Token estimado com margem conservadora explícita, nunca exato          | `estimate_tokens()` (~3 chars/token) + `Package.exact_tokens` sempre `False` |
| Corte nunca separa `else`/`except`/`finally`/guard-clause do efeito subsequente citado | `_shrink_compound()` (Python, via `ast`, nó inteiro ou nada) e `_prune_generic_braces()` (chaves balanceadas, nunca antes de `else`/`catch`/`finally`) |
| Se o trecho mínimo não couber, repartir preservando referência cruzada | `_suggest_partition()` alimenta `BudgetExceeded.suggestion` |
| Cache por versão da entrada; versão diferente invalida                 | `PackageCache` / `build_package_cached()`, chave `(objective_id, input_versions_hash)` |
| Dedup por path+faixa; fontes distintas com mesmo conteúdo NUNCA se fundem | `build_package()` deduplica por `(path, start, end, commit)`, nunca por hash de conteúdo isolado |
| Nenhum pacote acima do teto sai sem erro                                | `_fit_to_budget()` só retorna dentro do teto; do contrário `BudgetExceeded` |

Imports: stdlib + `knowledge` (indireto, via `analysis`) + `analysis`
(`investigation.InvestigationObjective`/`ReadingNeed`, e o `resolver` que quem
despacha injeta é tipicamente um wrapper de `analysis.snapshot.resolve_evidence`).
Nada de `wk`/`codescan`/`sbindex` — este módulo não sabe que essas árvores
existem.

Limitações declaradas (nunca escondidas):

- `tiktoken` (ou qualquer tokenizer compatível) não está disponível nesta
  build; `token_estimate` é sempre estimativa por bytes/`CHARS_PER_TOKEN_CONSERVADOR`
  e `Package.exact_tokens` é sempre `False`.
- O corte semântico por AST cobre Python; para as demais linguagens o corte é
  por linhas com chaves balanceadas (heurística léxica, documentada em
  `_prune_generic_braces`) — arquivo de uma única linha (ex.: JSON minificado,
  dump de 1 linha) não tem ponto de corte seguro e, se for grande demais
  sozinho, força `BudgetExceeded` em vez de corte arriscado.
- `_shrink_compound` usa `node.lineno`/`node.end_lineno` (Python ≥ 3.8); um
  cabeçalho decorado com `@decorator` em versões antigas do parser pode ficar
  fora do texto reconstruído — não é o caso das versões suportadas aqui.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from analysis.investigation import InvestigationObjective, ReadingNeed

__all__ = [
    "CHARS_PER_TOKEN_CONSERVADOR",
    "Budget",
    "BudgetExceeded",
    "Package",
    "PackagePart",
    "PackageCache",
    "ResolverFn",
    "build_package",
    "build_package_cached",
    "build_package_from_objective",
    "serialized_bytes",
    "estimate_tokens",
    "truncate_snippet",
    "compute_versions_hash",
]

#: Razão conservadora chars/token quando não há tokenizer compatível
#: disponível (`tiktoken` etc. — nenhum está presente nesta build). ~3.0 é
#: deliberadamente MENOR que a média usual de código em inglês (~4), o que
#: SUPERESTIMA tokens: a margem sempre erra para o lado seguro (§7.3.1).
CHARS_PER_TOKEN_CONSERVADOR = 3.0

#: Teto de passes de corte semântico por chamada — proteção contra loop
#: patológico; na prática o corte converge muito antes (cada parte só é
#: tentada de novo depois de efetivamente encolher).
_MAX_TRUNCATION_PASSES = 500

#: Continuações que dependem sintaticamente do bloco anterior — nunca podem
#: virar o início de um corte no modo de linhas/chaves balanceadas.
_CONTINUATION_KEYWORDS = ("else", "elif", "except", "catch", "finally")

_EXT_LANGUAGE: Mapping[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".c": "c",
    ".h": "c",
    ".cpp": "c",
    ".hpp": "c",
    ".cs": "c",
    ".go": "c",
    ".kt": "java",
}


# --------------------------------------------------------------------------
# Medição — bytes reais, tokens estimados com margem
# --------------------------------------------------------------------------


def serialized_bytes(payload: Any) -> int:
    """Tamanho REAL do payload serializado — a única medida que conta (§7.3.1).

    `json.dumps(..., ensure_ascii=False)` é o mesmo formato em que o pacote
    seria de fato transmitido; `sort_keys=True` só estabiliza a saída para
    comparação determinística, não afeta o tamanho de forma material (chaves
    são as mesmas, apenas em outra ordem).
    """
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def estimate_tokens(payload_bytes: int) -> int:
    """`ceil(bytes / CHARS_PER_TOKEN_CONSERVADOR)` — nunca contagem exata.

    Não há tokenizer compatível nesta build (`tiktoken` indisponível); esta é
    a estimativa com margem conservadora explícita exigida pelo plano. Quem
    usa o resultado NUNCA deve tratá-lo como contagem exata — por isso
    `Package.exact_tokens` é sempre `False` em todo caminho deste módulo.
    """
    if payload_bytes <= 0:
        return 0
    return math.ceil(payload_bytes / CHARS_PER_TOKEN_CONSERVADOR)


# --------------------------------------------------------------------------
# Orçamento
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """Teto de envio (§7.3.1): bytes, tokens, overhead e reserva de saída.

    O teto EFETIVO desconta overhead (instruções + schemas de ferramentas) e
    reserva de saída — nunca compara o corpo de evidências contra o teto
    bruto. `overhead_bytes` também consome uma fatia de `max_tokens` (via
    `overhead_token_estimate`): overhead não é só bytes, é contexto que o
    modelo também paga em tokens.
    """

    max_bytes: int
    max_tokens: int
    overhead_bytes: int = 0
    output_reserve_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_tokens", "overhead_bytes", "output_reserve_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Budget.{name} deve ser inteiro >= 0, recebido {value!r}")

    @property
    def effective_max_bytes(self) -> int:
        return max(0, self.max_bytes - self.overhead_bytes)

    @property
    def overhead_token_estimate(self) -> int:
        return estimate_tokens(self.overhead_bytes)

    @property
    def effective_max_tokens(self) -> int:
        return max(0, self.max_tokens - self.output_reserve_tokens - self.overhead_token_estimate)

    def fits(self, payload_bytes: int, token_estimate: int) -> bool:
        return payload_bytes <= self.effective_max_bytes and token_estimate <= self.effective_max_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "max_bytes": self.max_bytes,
            "max_tokens": self.max_tokens,
            "overhead_bytes": self.overhead_bytes,
            "output_reserve_tokens": self.output_reserve_tokens,
        }

    @classmethod
    def coerce(cls, value: "Budget | Mapping[str, Any]") -> "Budget":
        """Aceita tanto `Budget` quanto o `dict` que `runtime.tasks` persiste
        (`task.budget: dict[str, Any]`, vindo de `budget_json`) — o coordenador
        vizinho chama `build_package(objective, task.budget, resolver)` com um
        `Mapping` puro, não com esta dataclass."""
        if isinstance(value, Budget):
            return value
        if isinstance(value, Mapping):
            return cls(
                max_bytes=int(value.get("max_bytes", 0)),
                max_tokens=int(value.get("max_tokens", 0)),
                overhead_bytes=int(value.get("overhead_bytes", 0)),
                output_reserve_tokens=int(value.get("output_reserve_tokens", 0)),
            )
        raise TypeError(f"budget deve ser Budget ou Mapping, recebido {type(value).__name__}")


class BudgetExceeded(Exception):
    """§7.3.1/F05 — pacote não coube no teto mesmo após corte semântico legal.

    `.suggestion` é a partição proposta (lista de sub-objetivos/refs) para que
    quem despacha (runtime W4) repartir o objetivo em vez de enviar o pacote
    inteiro ou cortar às cegas. `.error_class = "budget"` casa direto com
    `runtime.recovery.ErrorClass.BUDGET`, sem depender de `isinstance` entre
    pacotes que podem não se importar mutuamente (ver `coordinator._classify_exception`).
    """

    def __init__(self, message: str, suggestion: Sequence[Mapping[str, Any]]) -> None:
        super().__init__(message)
        self.suggestion = list(suggestion)
        self.error_class = "budget"


# --------------------------------------------------------------------------
# Partes do pacote
# --------------------------------------------------------------------------


@dataclass
class PackagePart:
    """Um trecho de código citável, deduplicado por `(path, start, end, versão)`.

    `refs` lista os `ref_id` (evidence_ref/reading_need/campo do contrato) que
    apontam para este MESMO trecho — é assim que a dedup (§7.3.1, regra 6)
    inclui o conteúdo 1x com referências múltiplas, sem nunca fundir fontes
    cujo `path`/faixa são distintos mesmo com conteúdo igual (essas viram
    partes SEPARADAS, uma por chave `(path, start, end)`).
    """

    part_id: str
    path: str
    line_start: int
    line_end: int
    language: str
    snippet: str
    locator: Mapping[str, Any] | None
    content_hash: str
    truncated: bool = False
    omitted_ranges: list[tuple[int, int]] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Texto ORIGINAL (nunca mutado) resolvido para esta parte. Todo corte
    #: parte SEMPRE daqui — nunca do `snippet` já cortado de uma tentativa
    #: anterior. Reprocessar a própria saída já cortada (que já contém
    #: marcadores `# [orcamento] ...` e cuja numeração de linha já não é a do
    #: arquivo original) corrompe a contagem de linhas de `omitted_ranges` e
    #: pode fazer um marcador de corte anterior desaparecer silenciosamente
    #: na reconstrução seguinte. Não entra em `to_dict()`: não é enviado, só
    #: apoia a poda determinística a partir de uma base estável.
    source_snippet: str = ""

    def __post_init__(self) -> None:
        if not self.source_snippet:
            self.source_snippet = self.snippet

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_id": self.part_id,
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "language": self.language,
            "snippet": self.snippet,
            "locator": dict(self.locator) if self.locator else None,
            "content_hash": self.content_hash,
            "truncated": self.truncated,
            "omitted_ranges": [list(r) for r in self.omitted_ranges],
            "refs": list(self.refs),
            "notes": list(self.notes),
        }


@dataclass
class Package:
    """O pacote pronto para despacho: objetivo/schema/limites (em `refs[0]`),
    demais referências e trechos necessários — nunca histórico de execução.

    `payload_bytes`/`token_estimate` são medidos sobre `content_payload()`
    (objective_id + refs + parts), NÃO sobre `to_json()` inteiro — não faz
    sentido o pacote se auto-medir incluindo o próprio número de bytes que
    ainda não existe no momento da medição.
    """

    objective_id: str
    refs: list[dict[str, Any]]
    parts: list[PackagePart]
    payload_bytes: int
    token_estimate: int
    exact_tokens: bool

    def content_payload(self) -> dict[str, Any]:
        return _content_payload(self.objective_id, self.refs, self.parts)

    def to_json(self) -> dict[str, Any]:
        out = self.content_payload()
        out["payload_bytes"] = self.payload_bytes
        out["token_estimate"] = self.token_estimate
        out["exact_tokens"] = self.exact_tokens
        return out


def _content_payload(
    objective_id: str, refs: Sequence[Mapping[str, Any]], parts: Sequence[PackagePart]
) -> dict[str, Any]:
    return {
        "objective_id": objective_id,
        "refs": [dict(r) for r in refs],
        "parts": [p.to_dict() for p in parts],
    }


#: Assinatura do resolvedor injetado por quem despacha — tipicamente
#: `lambda path, start, end: analysis.snapshot.resolve_evidence(snapshot, path, start, end)`.
#: Contrato de retorno: `{"snippet": str, "locator": Mapping | None}`.
ResolverFn = Callable[[str, int, int], Mapping[str, Any]]


# --------------------------------------------------------------------------
# Detecção de linguagem (só para escolher a estratégia de corte)
# --------------------------------------------------------------------------


def _detect_language(path: str) -> str:
    _, ext = os.path.splitext(path)
    return _EXT_LANGUAGE.get(ext.lower(), "text")


# --------------------------------------------------------------------------
# Corte semântico — Python via AST (nó inteiro ou nada)
# --------------------------------------------------------------------------

#: Nós com corpo próprio onde é seguro recursar (o cabeçalho e TODAS as
#: cláusulas — else/except/finally — são sempre preservados; só o CONTEÚDO
#: interno de cada cláusula pode ser podado).
_COMPOUND_TYPES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)


def _stmt_source(node: ast.AST, lines: Sequence[str]) -> str:
    end = getattr(node, "end_lineno", None) or node.lineno
    return "\n".join(lines[node.lineno - 1 : end])


def _find_header_line(lines: Sequence[str], before_lineno_1based: int) -> str:
    """Linha de cabeçalho (`else:`/`finally:`) logo antes do 1º stmt do bloco.

    Python não guarda a posição da palavra `else`/`finally` como nó próprio
    (diferente de `if`/`except`, cujo `lineno` já é a palavra-chave certa);
    por isso ela é recuperada varrendo para trás até a última linha não-vazia.
    """
    i = before_lineno_1based - 2  # índice 0-based da linha imediatamente anterior
    while i >= 0 and not lines[i].strip():
        i -= 1
    return lines[i] if i >= 0 else ""


def _render_stmts(
    stmts: Sequence[ast.stmt], lines: Sequence[str], budget_chars: int, omitted: list[tuple[int, int]]
) -> str:
    """Renderiza uma lista de statements: cada um inteiro, encolhido (se
    composto) ou OMITIDO INTEIRO com um marcador — nunca partido ao meio.

    Esta é a regra que impede "remover o else/except/finally do intervalo
    citado" (§7.3.1): a única unidade que pode desaparecer é um statement
    completo (com todas as suas próprias cláusulas, porque `_shrink_compound`
    delas cuida por dentro); nunca uma fatia arbitrária de linhas.
    """
    pieces: list[str] = []
    remaining = budget_chars
    for stmt in stmts:
        src = _stmt_source(stmt, lines)
        if len(src) <= max(remaining, 0):
            pieces.append(src)
            remaining -= len(src) + 1
            continue
        if isinstance(stmt, _COMPOUND_TYPES):
            shrunk = _shrink_compound(stmt, lines, max(remaining, 0), omitted)
            if shrunk is not None and len(shrunk) < len(src):
                pieces.append(shrunk)
                remaining -= len(shrunk) + 1
                continue
        end = getattr(stmt, "end_lineno", None) or stmt.lineno
        omitted.append((stmt.lineno, end))
        placeholder = f"# [orcamento] linhas {stmt.lineno}-{end} omitidas (no completo preservado, nada partido ao meio)"
        pieces.append(placeholder)
        remaining -= len(placeholder) + 1
    return "\n".join(pieces)


def _is_omission_only(text: str) -> bool:
    """`True` quando `text` não contém nenhuma linha de código real — só
    marcador(es) de omissão (`# [orcamento] ...`) e/ou linhas em branco.

    Existe para impedir um bug real: colar um corpo assim sob um cabeçalho
    (`def ...:`, `if ...:`, `except ...:`) produz Python SINTATICAMENTE
    INVÁLIDO — uma suite precisa de ao menos uma statement real; comentário
    sozinho não conta como corpo (`IndentationError: expected an indented
    block`). Quem chama trata um corpo assim como "não redutível por este
    caminho" e deixa o nó INTEIRO (cabeçalho + corpo) virar um único
    placeholder de omissão no nível ACIMA — nunca um cabeçalho com corpo
    vazio.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return False
    return True


def _shrink_compound(
    node: ast.stmt, lines: Sequence[str], budget_chars: int, omitted: list[tuple[int, int]]
) -> str | None:
    """Reconstrói um nó composto podando SÓ o conteúdo de cada cláusula.

    Cabeçalho (`if ...:`, `try:`), cada `except ...:`, `else:` e `finally:`
    saem VERBATIM do texto-fonte — nunca reconstruídos por `ast.unparse` (que
    reformataria e perderia fidelidade) e nunca omitidos como cláusula: só o
    que está DENTRO de cada bloco pode virar placeholder, um statement por
    vez, via `_render_stmts`. É esta garantia que barra "else sem if" e
    "except sem try" no resultado final.

    Cada cláusula cujo corpo renderizado ficaria SÓ com marcador de omissão
    (`_is_omission_only`) aborta esta reconstrução inteira (`return None`,
    desfazendo em `omitted` o que essa tentativa descartada chegou a
    registrar): um cabeçalho seguido de corpo vazio é Python inválido, e o
    nó nunca pode ficar "parcialmente" reduzido dessa forma — quem chama
    trata `None` como "não redutível por este caminho" e omite o nó INTEIRO
    (cabeçalho incluso) como um único placeholder no nível de cima.
    """
    body = getattr(node, "body", None)
    if not body:
        return None
    if body[0].lineno == node.lineno:
        # composto de uma linha só (`if x: y`): nada para subdividir com segurança.
        return None

    omitted_checkpoint = len(omitted)

    header = "\n".join(lines[node.lineno - 1 : body[0].lineno - 1])
    used = len(header) + 1
    body_src = _render_stmts(body, lines, budget_chars - used, omitted)
    if _is_omission_only(body_src):
        del omitted[omitted_checkpoint:]
        return None
    pieces = [header, body_src]
    used += len(body_src) + 1

    if isinstance(node, ast.Try):
        for handler in node.handlers:
            h_header = "\n".join(lines[handler.lineno - 1 : handler.body[0].lineno - 1])
            h_body = _render_stmts(handler.body, lines, budget_chars - used - len(h_header) - 1, omitted)
            if _is_omission_only(h_body):
                del omitted[omitted_checkpoint:]
                return None
            pieces.append(h_header)
            pieces.append(h_body)
            used += len(h_header) + len(h_body) + 2
        for block in (node.orelse, node.finalbody):
            if not block:
                continue
            block_header = _find_header_line(lines, block[0].lineno)
            block_body = _render_stmts(block, lines, budget_chars - used - len(block_header) - 1, omitted)
            if _is_omission_only(block_body):
                del omitted[omitted_checkpoint:]
                return None
            pieces.append(block_header)
            pieces.append(block_body)
            used += len(block_header) + len(block_body) + 2
    elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)) and node.orelse:
        is_elif = (
            isinstance(node, ast.If)
            and len(node.orelse) == 1
            and isinstance(node.orelse[0], ast.If)
            and node.orelse[0].lineno > node.lineno
            and lines[node.orelse[0].lineno - 1].lstrip().startswith("elif")
        )
        if is_elif:
            sub = _shrink_compound(node.orelse[0], lines, max(budget_chars - used, 0), omitted)
            pieces.append(sub if sub is not None else _stmt_source(node.orelse[0], lines))
        else:
            block_header = _find_header_line(lines, node.orelse[0].lineno)
            block_body = _render_stmts(
                node.orelse, lines, budget_chars - used - len(block_header) - 1, omitted
            )
            if _is_omission_only(block_body):
                del omitted[omitted_checkpoint:]
                return None
            pieces.append(block_header)
            pieces.append(block_body)

    return "\n".join(pieces)


def _prune_python(
    snippet: str, target_max_chars: int
) -> tuple[str, list[tuple[int, int]], bool] | None:
    """Poda um trecho Python preservando toda cláusula de todo composto citado.

    Retorna `None` quando o trecho não é Python válido isolado (mesmo após
    `textwrap.dedent`) — nesse caso não há forma segura de podar por bloco, e
    quem chama trata como "não redutível" (parte fica candidata a
    `BudgetExceeded`, nunca a corte arriscado por linha).
    """
    dedented = textwrap.dedent(snippet)
    try:
        tree = ast.parse(dedented)
    except SyntaxError:
        return None
    lines = dedented.splitlines()
    omitted: list[tuple[int, int]] = []
    new_src = _render_stmts(tree.body, lines, target_max_chars, omitted)
    changed = bool(omitted) and new_src.strip() != dedented.strip()
    return new_src, omitted, changed


# --------------------------------------------------------------------------
# Corte semântico genérico — linhas com chaves balanceadas
# --------------------------------------------------------------------------


def _prune_generic_braces(
    snippet: str, target_max_chars: int
) -> tuple[str, list[tuple[int, int]], bool]:
    """Corte por linha para linguagens sem parser próprio aqui (Java/JS/C-like).

    Só corta em uma linha onde a profundidade de `{`/`}` volta a 0 E a
    próxima linha não é `}`/`else`/`catch`/`finally`/`elif` — nunca separa um
    guard de chaves do seu fechamento nem uma cláusula de continuação do seu
    bloco anterior. Heurística léxica (não conta `{`/`}` dentro de strings ou
    comentários) — limitação declarada, não escondida.

    Um trecho de UMA linha só (ex.: JSON minificado, dump de 1 linha) não tem
    NENHUM ponto de corte seguro: devolve `changed=False`, e quem chama deixa
    a parte intocada — se ela sozinha estourar o teto, o resultado correto é
    `BudgetExceeded`, nunca corte arriscado no meio da linha (F05: "linha
    longa não é exceção ao teto").
    """
    lines = snippet.splitlines()
    if len(lines) < 2:
        return snippet, [], False

    depth = 0
    safe_cuts: list[int] = []
    for i, line in enumerate(lines[:-1]):
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            nxt = lines[i + 1].strip()
            first_word = nxt.split("(")[0].split(":")[0].split()[:1]
            starts_continuation = bool(first_word) and first_word[0] in _CONTINUATION_KEYWORDS
            if nxt and not nxt.startswith("}") and not starts_continuation:
                safe_cuts.append(i)
    if not safe_cuts:
        return snippet, [], False

    chosen = None
    for i in safe_cuts:
        if len("\n".join(lines[: i + 1])) <= target_max_chars:
            chosen = i
    if chosen is None:
        chosen = safe_cuts[0]  # melhor esforço: o menor corte seguro disponível
    if chosen + 1 >= len(lines):
        return snippet, [], False

    kept = "\n".join(lines[: chosen + 1])
    marker = f"// [orcamento] linhas {chosen + 2}-{len(lines)} omitidas (corte em ponto de chaves balanceadas)"
    return kept + "\n" + marker, [(chosen + 2, len(lines))], True


def truncate_snippet(
    snippet: str, language: str, target_max_chars: int
) -> tuple[str, list[tuple[int, int]], bool]:
    """Corte semântico único de um trecho: Python via AST, resto por chaves.

    `(novo_trecho, faixas_omitidas, mudou)`. `mudou=False` significa "nenhum
    corte seguro encontrado com este alvo" — quem chama NUNCA interpreta isso
    como "coube", só como "esta parte não encolhe mais por este caminho".

    Garantia de monotonicidade: só é reportado `mudou=True` quando o
    resultado é ESTRITAMENTE menor que o original. Sem isto, um trecho já
    pequeno (ex.: função de 1 linha) poderia "encolher" para um marcador de
    omissão mais comprido que o próprio conteúdo substituído (o comentário
    `# [orcamento] linhas ...` tem overhead fixo) — o chamador entraria num
    loop que CRESCE o pacote em vez de reduzi-lo. Preservar a cláusula nunca
    pode custar mais bytes do que já existiam.
    """
    if language == "python":
        result = _prune_python(snippet, max(target_max_chars, 0))
        if result is not None:
            new_src, omitted, changed = result
            if changed and len(new_src) < len(snippet):
                return new_src, omitted, True
        return snippet, [], False
    new_snippet, omitted, changed = _prune_generic_braces(snippet, max(target_max_chars, 0))
    if changed and len(new_snippet) < len(snippet):
        return new_snippet, omitted, True
    return snippet, [], False


# --------------------------------------------------------------------------
# Envelope do objetivo (schema + limites conhecidos) — nunca histórico
# --------------------------------------------------------------------------


def _objective_envelope(objective_dict: Mapping[str, Any]) -> dict[str, Any]:
    """`refs[0]`: objetivo + schema de retorno + limites conhecidos.

    Whitelist explícita de campos — não um passthrough do dict recebido. É
    isto que garante que um `objective_dict` futuro com, por exemplo, um
    campo de log de execução NUNCA vaze para o pacote: só o que está
    nomeado aqui embaixo entra.
    """
    contract = objective_dict.get("contract", {}) or {}
    schema = [
        {
            "name": name,
            "label": fld.get("label", ""),
            "status": fld.get("status", "pending"),
            "content": fld.get("content", ""),
            "motivo": fld.get("motivo", ""),
            "impacto": fld.get("impacto", ""),
        }
        for name, fld in contract.items()
    ]
    matrix = objective_dict.get("matrix") or {}
    known_limits = {
        "notes": list(objective_dict.get("notes", ()) or ()),
        "accounting": dict(objective_dict.get("accounting", {}) or {}),
        "unresolved_fields": [n for n, f in contract.items() if f.get("status") == "unresolved"],
        "matrix_summary": matrix.get("summary"),
        "state": objective_dict.get("state"),
        "closure": objective_dict.get("closure"),
    }
    return {
        "kind": "objective",
        "ref_id": "objective",
        "objective_id": objective_dict.get("objective_id"),
        "objective_kind": objective_dict.get("kind"),
        "capability_id": objective_dict.get("capability_id"),
        "name": objective_dict.get("name"),
        "entry_keys": list(objective_dict.get("entry_keys", ()) or ()),
        "symbols": list(objective_dict.get("symbols", ()) or ()),
        "schema": schema,
        "known_limits": known_limits,
    }


def _evidence_source_key(ev: Mapping[str, Any]) -> tuple[str, int, int] | None:
    path = ev.get("path")
    start = ev.get("line_start")
    end = ev.get("line_end")
    if not path or not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        return None
    return (path, start, end)


# --------------------------------------------------------------------------
# Montagem do pacote
# --------------------------------------------------------------------------


def build_package(
    objective_dict: Mapping[str, Any],
    budget: "Budget | Mapping[str, Any]",
    resolver: ResolverFn,
) -> Package:
    """Monta o pacote de um objetivo dentro do orçamento (§7.3.1, F05).

    `objective_dict`: `InvestigationObjective.to_dict()` (ou equivalente —
    este módulo só exige as chaves usadas abaixo, nunca importa a classe para
    validar o formato: o coordenador vizinho despacha `dict`, não o objeto).

    `resolver(path, start, end)` é injetado por quem despacha — nunca este
    módulo lê arquivo por conta própria. Contrato de retorno esperado:
    `{"snippet": str, "locator": Mapping | None}` (o mesmo formato de
    `analysis.snapshot.resolve_evidence`). Uma exceção do resolver (arquivo
    fora do snapshot, conteúdo mudou sob a citação etc.) NUNCA fabrica um
    trecho: a referência correspondente sai com `unavailable_reason`, sem
    `part_id`.

    Fontes de trecho citável reunidas (nunca só o topo do objetivo):
    `evidence_refs` do objetivo, `evidence_refs` de CADA campo do contrato
    (`identidade`/`gatilho`/`dependencias`/`lacunas`/`verificacao` etc.),
    `evidence` de cada `reading_need` ainda ABERTO (satisfeito/dispensado não
    precisa de trecho novo), e a justificativa de cada célula da matriz
    §6.5 marcada. Deduplicadas por `(path, start, end)` — múltiplas
    referências para a MESMA faixa viram uma só `PackagePart` com vários
    `refs`; faixas diferentes (mesmo com texto igual) NUNCA se fundem.
    """
    objective_id = str(objective_dict.get("objective_id", ""))
    if not objective_id:
        raise ValueError("objective_dict sem objective_id: pacote precisa de identidade")
    b = Budget.coerce(budget)

    sources: list[dict[str, Any]] = []

    for idx, ev in enumerate(objective_dict.get("evidence_refs", ()) or ()):
        sources.append({"ref_id": f"evidence_ref:{idx}", "ev": ev, "kind": "evidence_ref"})

    contract = objective_dict.get("contract", {}) or {}
    for field_name, fld in contract.items():
        for idx, ev in enumerate(fld.get("evidence_refs", ()) or ()):
            sources.append(
                {"ref_id": f"field:{field_name}:{idx}", "ev": ev, "kind": "field_evidence", "field": field_name}
            )

    for need in objective_dict.get("reading_needs", ()) or ():
        ev = need.get("evidence")
        is_open = not need.get("satisfied") and not need.get("waived_reason")
        if ev and is_open:
            sources.append(
                {"ref_id": str(need.get("need_id")), "ev": ev, "kind": "reading_need", "need": need}
            )

    matrix = objective_dict.get("matrix") or {}
    for cell in matrix.get("cells", ()) or ():
        just = cell.get("justification")
        if just:
            ref_id = f"matrix:{cell.get('family')}/{cell.get('item')}"
            sources.append({"ref_id": ref_id, "ev": just, "kind": "matrix_justification", "cell": cell})

    parts_by_key: dict[tuple[str, int, int], PackagePart] = {}
    key_errors: dict[tuple[str, int, int], str] = {}
    key_of_ref: dict[str, tuple[str, int, int] | None] = {}
    refs_of_key: dict[tuple[str, int, int], list[str]] = {}
    resolve_errors: dict[str, str] = {}

    for src in sources:
        ref_id = src["ref_id"]
        key = _evidence_source_key(src["ev"])
        key_of_ref[ref_id] = key
        if key is None:
            resolve_errors[ref_id] = "evidence sem path/linhas resolviveis: sem trecho citavel nesta execucao"
            continue
        refs_of_key.setdefault(key, []).append(ref_id)
        if key in parts_by_key or key in key_errors:
            continue
        path, start, end = key
        try:
            resolved = resolver(path, start, end)
            snippet = resolved["snippet"]
            locator = resolved.get("locator")
        except Exception as exc:  # resolver externo: nunca fabricar trecho no lugar do erro
            key_errors[key] = f"{type(exc).__name__}: {exc}"
            continue
        commit = (locator or {}).get("commit", "")
        part_id = "part_" + hashlib.sha256(f"{path}:{start}:{end}:{commit}".encode("utf-8")).hexdigest()[:20]
        parts_by_key[key] = PackagePart(
            part_id=part_id,
            path=path,
            line_start=start,
            line_end=end,
            language=_detect_language(path),
            snippet=snippet,
            locator=locator,
            content_hash=hashlib.sha256(snippet.encode("utf-8")).hexdigest(),
        )

    for key, part in parts_by_key.items():
        part.refs = list(refs_of_key.get(key, ()))
    for ref_id, key in key_of_ref.items():
        if key is not None and key in key_errors and ref_id not in resolve_errors:
            resolve_errors[ref_id] = key_errors[key]

    refs: list[dict[str, Any]] = [_objective_envelope(objective_dict)]
    for src in sources:
        ref_id = src["ref_id"]
        key = key_of_ref.get(ref_id)
        part_id = parts_by_key[key].part_id if key is not None and key in parts_by_key else None
        ev = src["ev"]
        entry: dict[str, Any] = {
            "kind": src["kind"],
            "ref_id": ref_id,
            "role": ev.get("role", ""),
            "symbol": ev.get("symbol"),
            "path": ev.get("path"),
            "part_id": part_id,
        }
        if src["kind"] == "reading_need":
            need = src["need"]
            entry.update(
                {
                    "target": need.get("target"),
                    "need_kind": need.get("kind"),
                    "trigger": need.get("trigger"),
                    "motivo": need.get("motivo"),
                    "priority": need.get("priority"),
                }
            )
        elif src["kind"] == "field_evidence":
            entry["field"] = src["field"]
        elif src["kind"] == "matrix_justification":
            entry["cell"] = {"family": src["cell"].get("family"), "item": src["cell"].get("item")}
        error = resolve_errors.get(ref_id)
        if error:
            entry["unavailable_reason"] = error
        refs.append(entry)

    parts = list(parts_by_key.values())
    return _fit_to_budget(objective_id, refs, parts, b)


def build_package_from_objective(
    objective: InvestigationObjective, budget: "Budget | Mapping[str, Any]", resolver: ResolverFn
) -> Package:
    """Conveniência: `build_package(objective.to_dict(), budget, resolver)`."""
    return build_package(objective.to_dict(), budget, resolver)


# --------------------------------------------------------------------------
# Ajuste ao orçamento — corte iterativo, nunca overflow silencioso
# --------------------------------------------------------------------------


def _fit_to_budget(
    objective_id: str, refs: list[dict[str, Any]], parts: list[PackagePart], budget: Budget
) -> Package:
    """Corta partes até caber, sempre a partir do texto-fonte imutável.

    Cada tentativa relê `target.source_snippet` (nunca `target.snippet` da
    tentativa anterior) com um alvo de tamanho que só encolhe a cada nova
    tentativa da MESMA parte (`next_target`) — isso é o que garante que
    `omitted_ranges` continua correspondendo às linhas do trecho ORIGINAL
    citado (nunca a uma renumeração de um resultado já cortado) e que um
    marcador de omissão de uma tentativa anterior nunca "some" ao ser
    reprocessado por engano junto com o texto já reduzido.

    Aceite é sempre por MEDIÇÃO REAL do payload inteiro, nunca por
    `len(snippet)` isolado: `omitted_ranges` também vai para o JSON, e um
    alvo mais agressivo produz uma lista de faixas omitidas MAIOR (mais
    granular) mesmo com `snippet` menor — o ganho em `snippet` pode custar
    menos do que a lista de faixas cresce, e o pacote total *cresceria* se a
    troca fosse aceita só porque o texto encolheu. Por isso cada tentativa é
    medida (`serialized_bytes` do payload inteiro) e revertida se não reduzir
    o total — nunca aceita por uma proxy parcial.
    """
    exhausted: set[str] = set()
    next_target_chars: dict[str, int] = {}
    payload_bytes = 0
    token_estimate = 0
    for _ in range(_MAX_TRUNCATION_PASSES):
        content = _content_payload(objective_id, refs, parts)
        payload_bytes = serialized_bytes(content)
        token_estimate = estimate_tokens(payload_bytes)
        if budget.fits(payload_bytes, token_estimate):
            return Package(
                objective_id=objective_id,
                refs=refs,
                parts=parts,
                payload_bytes=payload_bytes,
                token_estimate=token_estimate,
                exact_tokens=False,
            )
        candidates = sorted(
            (p for p in parts if p.part_id not in exhausted), key=lambda p: len(p.snippet), reverse=True
        )
        if not candidates:
            break
        target = candidates[0]
        current_target = next_target_chars.get(target.part_id, len(target.source_snippet))
        new_target_chars = max(current_target // 2, 0)
        next_target_chars[target.part_id] = new_target_chars
        new_snippet, omitted_relative, changed = truncate_snippet(
            target.source_snippet, target.language, new_target_chars
        )
        if not changed or len(new_snippet) >= len(target.snippet):
            exhausted.add(target.part_id)
            continue

        # Tentativa: aplica, MEDE o payload inteiro, mantém só se reduziu de
        # fato — nunca confia em `len(snippet)` como proxy do efeito total.
        prev_snippet = target.snippet
        prev_omitted = target.omitted_ranges
        prev_hash = target.content_hash
        prev_truncated = target.truncated
        target.snippet = new_snippet
        # Linhas relativas ao início do PRÓPRIO source_snippet (1-based) ->
        # linhas absolutas do arquivo citado, usando `line_start` da parte.
        target.omitted_ranges = [
            (target.line_start + s - 1, target.line_start + e - 1) for (s, e) in omitted_relative
        ]
        target.content_hash = hashlib.sha256(new_snippet.encode("utf-8")).hexdigest()
        target.truncated = True
        new_payload_bytes = serialized_bytes(_content_payload(objective_id, refs, parts))
        if new_payload_bytes >= payload_bytes:
            target.snippet = prev_snippet
            target.omitted_ranges = prev_omitted
            target.content_hash = prev_hash
            target.truncated = prev_truncated
            exhausted.add(target.part_id)

    raise BudgetExceeded(
        f"pacote do objetivo {objective_id!r} excede o orcamento mesmo apos corte semantico "
        f"completo: {payload_bytes} bytes (~{token_estimate} tokens estimados) contra teto "
        f"efetivo de {budget.effective_max_bytes} bytes / {budget.effective_max_tokens} tokens",
        suggestion=_suggest_partition(objective_id, refs, budget),
    )


def _suggest_partition(
    objective_id: str, refs: Sequence[Mapping[str, Any]], budget: Budget
) -> list[dict[str, Any]]:
    """Partição sugerida quando nem o corte semântico legal coube (§7.3.1).

    Agrupa as obrigações de leitura (`reading_need`) em lotes cujo tamanho
    serializado aproximado respeita o teto efetivo, preservando prioridade de
    despacho e apontando (`cross_refs`) quais alvos aparecem em mais de um
    lote — para quem repartir não perder a referência cruzada entre partes.
    """
    need_refs = [r for r in refs if r.get("kind") == "reading_need"]
    if not need_refs:
        return [
            {
                "objective_id": objective_id,
                "note": (
                    "sem obrigacoes de leitura para repartir: o proprio envelope do objetivo "
                    "(ou uma unica evidencia) ja excede o teto — reduzir o escopo do objetivo "
                    "ou aumentar o orcamento antes de despachar"
                ),
            }
        ]
    limit = max(1, budget.effective_max_bytes // 4)
    groups: list[list[Mapping[str, Any]]] = [[]]
    sizes = [0]
    for r in sorted(need_refs, key=lambda x: (x.get("priority", 50), str(x.get("target", "")))):
        cost = len(json.dumps(dict(r), ensure_ascii=False))
        if sizes[-1] + cost > limit and groups[-1]:
            groups.append([])
            sizes.append(0)
        groups[-1].append(r)
        sizes[-1] += cost

    target_to_groups: dict[str, set[int]] = {}
    for gi, group in enumerate(groups):
        for r in group:
            target_to_groups.setdefault(str(r.get("target")), set()).add(gi)

    suggestion: list[dict[str, Any]] = []
    for gi, group in enumerate(groups):
        cross_refs = sorted(
            t for t, gset in target_to_groups.items() if len(gset) > 1 and gi in gset
        )
        suggestion.append(
            {
                "objective_id": objective_id,
                "part_index": gi,
                "part_count": len(groups),
                "reading_need_ids": [r.get("ref_id") for r in group],
                "cross_refs": cross_refs,
            }
        )
    return suggestion


# --------------------------------------------------------------------------
# Cache por versão (§7.3.1, regra 5)
# --------------------------------------------------------------------------


class PackageCache:
    """Reaproveita o pacote já montado para `(objective_id, input_versions_hash)`.

    `input_versions_hash` é responsabilidade de quem despacha (o coordenador
    já conhece a versão das entradas — tipicamente `Snapshot.snapshot_id` ou
    um hash agregado das versões consumidas pelo `resolver`); este módulo não
    o deriva por conta própria porque isso exigiria resolver tudo de novo só
    para descobrir se podia ter reaproveitado. Mudar qualquer entrada usada
    pelo `resolver` deve mudar este hash — é isso que invalida o cache
    automaticamente quando a versão muda (nunca é preciso "limpar" nada à
    mão para uma versão nova ser recalculada).
    """

    def __init__(self) -> None:
        self._store: MutableMapping[tuple[str, str], Package] = {}

    def get(self, objective_id: str, input_versions_hash: str) -> Package | None:
        return self._store.get((objective_id, input_versions_hash))

    def put(self, objective_id: str, input_versions_hash: str, package: Package) -> None:
        self._store[(objective_id, input_versions_hash)] = package

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


def compute_versions_hash(versions: Mapping[str, str]) -> str:
    """Hash determinístico de `{path: versão}` — utilitário para quem monta
    `input_versions_hash` a partir de múltiplas fontes (ex.: vários snapshots)."""
    blob = json.dumps(dict(sorted(versions.items())), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_package_cached(
    objective_dict: Mapping[str, Any],
    budget: "Budget | Mapping[str, Any]",
    resolver: ResolverFn,
    cache: PackageCache,
    input_versions_hash: str,
) -> Package:
    """`build_package` com reaproveitamento por versão (§7.3.1, regra 5).

    Acerto de cache pula TODA a resolução e o corte semântico de novo — só é
    seguro porque a chave inclui `input_versions_hash`: uma entrada com
    versão diferente (código mudou, config mudou, contrato mudou) tem chave
    diferente e nunca reaproveita um pacote de uma versão anterior.
    """
    objective_id = str(objective_dict.get("objective_id", ""))
    cached = cache.get(objective_id, input_versions_hash)
    if cached is not None:
        return cached
    package = build_package(objective_dict, budget, resolver)
    cache.put(objective_id, input_versions_hash, package)
    return package
