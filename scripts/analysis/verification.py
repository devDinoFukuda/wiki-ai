"""Verificação de sustentação: localização válida NÃO é sustentação (§15.1, §5.3, F03).

O baseline (`codescan/evidence.py::verify_markdown`) checava uma única coisa:
que `arquivo:linha` citado existe. Com isso, uma citação perfeitamente real
aprova uma afirmação falsa — basta apontar para um trecho que diz o contrário
do que a afirmação diz. É o achado F03.

Este módulo separa os dois eixos e só o segundo aprova:

| Eixo | Função | Pergunta |
|---|---|---|
| Localização | `check_location` | o localizador resolve, o hash bate, o conteúdo é executável? |
| Sustentação | `check_support` | o trecho citado DIZ o que a afirmação afirma? |

Três invariantes estão em código executável, não em prosa:

1. **Existência de citação nunca aprova.** `check_support` só devolve
   `outcome="supported"` quando ao menos uma checagem mecânica de predicado,
   efeito ou literal foi *aplicável* e passou. Sem checagem aplicável o
   resultado é `insufficient`, e `verify_claim` nunca emite
   `EpistemicStatus.SUPPORTED` a partir dele.
2. **Polaridade invertida vira `disputed`, não silêncio.** Quando o trecho
   contém a mesma comparação com operador/valor diferentes do afirmado, o
   resultado é `contradicted` e o veredito descreve a divergência
   (regressão §15.2 nº1).
3. **Comentário/docstring nunca sustenta `implemented`.** O `content_kind` é
   DETECTADO do trecho (não aceito do declarante) e passa por
   `knowledge.evidence.supports_implemented` — a mesma função que o
   repositório usa.

`support_recorded_by` sai daqui como `"pipeline:verification"`: é a origem
`pipeline` que `knowledge.repository._check_support` exige para `supported`, e
ela é diferente do `asserted_by` do claim, de modo que uma saída de LLM não
consegue registrar a própria sustentação (§5.3). Quando `asserted_by` já é o
próprio registrador, o veredito é rebaixado em vez de aprovado.

Somente stdlib + `knowledge` + `analysis.snapshot`/`analysis.extractors`.
Nenhum import de `codescan`, `wk` ou `sbindex`.
"""

from __future__ import annotations

import ast
import hashlib
import re
import textwrap
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from knowledge.evidence import snippet_hash as _snippet_hash, supports_implemented
from knowledge.models import ContentKind, EpistemicStatus, Evidence, FactNature, SourceKind

from .snapshot import (
    EvidenceRangeInvalid,
    PathNotInSnapshot,
    Snapshot,
    SnapshotError,
    SnapshotStale,
    resolve_evidence,
)

__all__ = [
    "SUPPORT_RECORDER",
    "PREDICATE_KINDS",
    "STATEMENT_FIELDS",
    "Check",
    "Claim",
    "ClaimEvidence",
    "Inconsistency",
    "LocationResult",
    "SupportResult",
    "Verdict",
    "apply_inconsistencies",
    "check_consistency",
    "check_location",
    "check_support",
    "classify_content_kind",
    "detect_mocks",
    "extract_comparisons",
    "extract_effects",
    "reread_obligations",
    "verification_summary",
    "verify_claim",
]

#: Origem que registra sustentação. Prefixo `pipeline:` é o que
#: `knowledge.repository` aceita em `support_recorded_by` (§5.3).
SUPPORT_RECORDER = "pipeline:verification"

#: Naturezas de predicado aceitas em `Claim.predicate_kind`.
PREDICATE_KINDS: frozenset[str] = frozenset({"behavior", "config", "contract", "data", "test"})

#: Campos reconhecidos em `Claim.statement_fields`. Schema fechado (§13.1):
#: campo desconhecido é rejeitado em vez de ignorado em silêncio.
STATEMENT_FIELDS: frozenset[str] = frozenset(
    {
        "condition",
        "comparator",
        "value",
        "effect",
        "exception",
        "negated",
        "literals",
        "after",
        "exception_to",
    }
)

#: Nomes das checagens mecânicas (§15.1). `by_check` do resumo usa estas chaves.
CHECK_NAMES: tuple[str, ...] = (
    "symbol_present",
    "predicate_polarity",
    "complement_condition",
    "effect_present",
    "exception_present",
    "literal_preserved",
    "mock_boundary",
)


class VerificationError(ValueError):
    """Contrato de verificação violado (claim malformado, campo desconhecido)."""


# --------------------------------------------------------------------------
# Vocabulário léxico
# --------------------------------------------------------------------------

_OP_SYM: dict[type, str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
}

#: Normalização de comparadores escritos pelo declarante.
_CMP_ALIASES: dict[str, str] = {
    "=": "==",
    "==": "==",
    "===": "==",
    "eq": "==",
    "igual": "==",
    "<>": "!=",
    "!=": "!=",
    "!==": "!=",
    "ne": "!=",
    "diferente": "!=",
    "<": "<",
    "lt": "<",
    "menor": "<",
    "<=": "<=",
    "le": "<=",
    ">": ">",
    "gt": ">",
    "maior": ">",
    ">=": ">=",
    "ge": ">=",
    "is": "is",
    "is not": "is not",
    "in": "in",
    "not in": "not in",
}

#: Operador oposto (negação lógica: NOT(a op b) == a _OPPOSITE[op] b). Serve a
#: dois papéis: DESCREVER a divergência encontrada quando é contradição real,
#: e (Onda 11-T1) RECONHECER a condição COMPLEMENTAR exata de um `if` — mesmos
#: operandos, operador negado — que não é contradição.
_OPPOSITE: dict[str, str] = {
    "==": "!=",
    "!=": "==",
    "<": ">=",
    ">=": "<",
    ">": "<=",
    "<=": ">",
    "is": "is not",
    "is not": "is",
    "in": "not in",
    "not in": "in",
}

#: Comparador espelhado quando os operandos trocam de lado — MESMA comparação,
#: não o oposto (`a > b` ≡ `b < a`; `a >= b` ≡ `b <= a`). Onda 11-T1: sem isto,
#: um claim `total > 1000` sobre um trecho `1000 < total` não batia com
#: nenhuma comparação extraída e caía em `inferred` por falta de checagem
#: aplicável. Mesmo mapeamento que `knowledge.integrate._MIRROR_CMP`,
#: reproduzido aqui porque este módulo não importa `knowledge.integrate`.
_MIRROR_CMP: dict[str, str] = {
    ">": "<",
    "<": ">",
    ">=": "<=",
    "<=": ">=",
    "==": "==",
    "!=": "!=",
}

#: Helpers de teste que EXPRESSAM uma comparação. Sem isto, um claim sobre
#: `assertEqual(code, 1)` não teria nenhuma checagem de predicado aplicável e
#: cairia em `insufficient` — que é seguro, mas cego.
_ASSERT_CMP: dict[str, tuple[str, str | None]] = {
    "assertEqual": ("==", None),
    "assertEquals": ("==", None),
    "assertNotEqual": ("!=", None),
    "assertIs": ("is", None),
    "assertIsNot": ("is not", None),
    "assertIn": ("in", None),
    "assertNotIn": ("not in", None),
    "assertGreater": (">", None),
    "assertGreaterEqual": (">=", None),
    "assertLess": ("<", None),
    "assertLessEqual": ("<=", None),
    "assertIsNone": ("is", "None"),
    "assertIsNotNone": ("is not", "None"),
    "assertTrue": ("is", "True"),
    "assertFalse": ("is", "False"),
}

_EFFECT_ALIASES: dict[str, str] = {
    "return": "return", "returns": "return", "retorna": "return", "devolve": "return",
    "raise": "raise", "raises": "raise", "throw": "raise", "throws": "raise",
    "levanta": "raise", "lanca": "raise", "lança": "raise",
    "call": "call", "calls": "call", "chama": "call", "invoke": "call", "invoca": "call",
    "write": "write", "writes": "write", "escreve": "write", "grava": "write",
    "read": "read", "reads": "read", "le": "read", "lê": "read",
    "publish": "publish", "publishes": "publish", "publica": "publish", "emit": "publish",
}

_WRITE_VERBS: frozenset[str] = frozenset(
    {"write", "writelines", "execute", "executemany", "executescript", "insert", "update",
     "delete", "save", "commit", "put", "post", "set", "add", "append", "mkdir", "makedirs",
     "rename", "remove", "unlink", "dump", "store", "persist", "flush"}
)
_READ_VERBS: frozenset[str] = frozenset(
    {"read", "readline", "readlines", "get", "fetch", "select", "load", "query", "find",
     "fetchone", "fetchall", "list", "scan"}
)
_PUBLISH_VERBS: frozenset[str] = frozenset(
    {"publish", "emit", "send", "produce", "dispatch", "notify", "enqueue", "broadcast"}
)

#: Marcadores de mock/dublê (§5.3). Detectá-los não invalida o teste: registra
#: a FRONTEIRA — o cenário modelado, não o serviço externo.
_MOCK_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pat), label)
    for pat, label in (
        (r"\bunittest\.mock\b", "unittest.mock"),
        (r"\bfrom\s+unittest\.mock\s+import\b", "unittest.mock import"),
        (r"\bmock\.patch\b", "mock.patch"),
        (r"@patch\b", "@patch"),
        (r"\bpatch\s*\(", "patch()"),
        (r"\bMagicMock\b", "MagicMock"),
        (r"\bAsyncMock\b", "AsyncMock"),
        (r"\bMock\s*\(", "Mock()"),
        (r"\bmonkeypatch\b", "monkeypatch"),
        (r"\bjest\.(mock|fn|spyOn)\b", "jest"),
        (r"\bsinon\.(stub|mock|fake|spy)\b", "sinon"),
        (r"\bMockito\b", "Mockito"),
        (r"@Mock(Bean)?\b", "@Mock"),
        (r"\bthenReturn\s*\(", "Mockito.thenReturn"),
    )
)

_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(spec|test)\.[jt]sx?$")
_ASSERT_MARK = re.compile(r"\b(assert|assertEqual|assertTrue|assertRaises|expect|should|it\()")

_COMMENT_PREFIX: dict[str, tuple[str, ...]] = {
    "python": ("#",),
    "shell": ("#",),
    "yaml": ("#",),
    "ruby": ("#",),
    "javascript": ("//", "*", "/*"),
    "typescript": ("//", "*", "/*"),
    "java": ("//", "*", "/*"),
    "go": ("//", "*", "/*"),
    "c": ("//", "*", "/*"),
    "sql": ("--",),
}

_EXT_LANG: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".go": "go", ".c": "c", ".h": "c", ".cpp": "c",
    ".sh": "shell", ".bash": "shell",
    ".rb": "ruby",
    ".sql": "sql",
    ".yml": "yaml", ".yaml": "yaml",
}

_PROSE_EXT: frozenset[str] = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})
_CONFIG_EXT: frozenset[str] = frozenset({".json", ".toml", ".ini", ".cfg", ".properties", ".env"})


# --------------------------------------------------------------------------
# Modelo de entrada
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimEvidence:
    """Onde o claim diz que está a prova.

    `content_kind` aqui é o que o DECLARANTE alega; `check_location` detecta o
    real e o declarado nunca prevalece sobre a detecção quando esta aponta
    para conteúdo não-implementante (§5.4).
    """

    path: str
    start_line: int
    end_line: int
    content_kind: ContentKind = ContentKind.EXECUTABLE
    source_kind: SourceKind = SourceKind.CODE
    snippet_hash: str | None = None
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        if not (self.path or "").strip():
            raise VerificationError("ClaimEvidence sem path")
        if not isinstance(self.start_line, int) or not isinstance(self.end_line, int):
            raise VerificationError(f"linhas devem ser inteiras em {self.path}")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise VerificationError(
                f"intervalo inválido em {self.path}: {self.start_line}..{self.end_line}"
            )

    @property
    def span(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class Claim:
    """Afirmação estruturada a verificar.

    `statement_fields` é o que torna a verificação MECÂNICA possível: sem
    `comparator`/`value`/`effect`/`literals` não existe checagem (b)-(d)
    aplicável e o claim não pode ser aprovado, por mais bem escrito que esteja
    o `statement` em prosa.

    `asserted_by` é preservado intacto no veredito: é o que permite ao
    repositório recusar `support_recorded_by == asserted_by` (§5.3).
    """

    claim_id: str
    subject: str
    predicate_kind: str
    statement_fields: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Sequence[ClaimEvidence] = ()
    asserted_by: str = ""
    statement: str = ""
    symbol: str | None = None
    scope: str = ""

    def __post_init__(self) -> None:
        if not (self.claim_id or "").strip():
            raise VerificationError("Claim sem claim_id")
        if self.predicate_kind not in PREDICATE_KINDS:
            raise VerificationError(
                f"predicate_kind desconhecido: {self.predicate_kind!r} "
                f"(aceitos: {sorted(PREDICATE_KINDS)})"
            )
        unknown = sorted(set(self.statement_fields) - STATEMENT_FIELDS)
        if unknown:
            raise VerificationError(
                f"claim {self.claim_id}: campos não previstos em statement_fields: "
                f"{', '.join(unknown)} (aceitos: {', '.join(sorted(STATEMENT_FIELDS))})"
            )
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    def field(self, name: str) -> Any:
        return self.statement_fields.get(name)

    @property
    def cited_symbol(self) -> str:
        return (self.symbol or self.subject or "").strip()

    @property
    def implies_implemented(self) -> bool:
        """`test` afirma expectativa de teste; o resto afirma o sistema real."""
        return self.predicate_kind != "test"


# --------------------------------------------------------------------------
# Modelo de saída
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """Uma checagem mecânica e o que ela viu.

    `applicable=False` NÃO é aprovação: é a ausência de base para verificar.
    `resolution` diz se veio de parser da gramática (`syntactic`) ou de
    varredura léxica (`heuristic`) — §6.2 exige que a diferença apareça.
    """

    name: str
    applicable: bool
    passed: bool | None
    detail: str
    resolution: str = "none"
    contradicted: bool = False
    scope_examined: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "applicable": self.applicable,
            "passed": self.passed,
            "contradicted": self.contradicted,
            "resolution": self.resolution,
            "detail": self.detail,
            "scope_examined": self.scope_examined,
        }


@dataclass(frozen=True)
class LocationResult:
    """Resultado do eixo LOCALIZAÇÃO. `ok=True` não diz nada sobre sustentação."""

    ok: bool
    path: str
    start_line: int
    end_line: int
    reason: str = ""
    snippet: str = ""
    locator: Mapping[str, Any] | None = None
    content_kind: ContentKind = ContentKind.PROSE
    declared_content_kind: ContentKind = ContentKind.EXECUTABLE
    executable: bool = False
    hash_ok: bool | None = None
    stale: bool = False
    test_scope: bool = False
    snapshot_id: str = ""
    language: str = ""

    @property
    def span(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "span": self.span,
            "reason": self.reason,
            "content_kind": self.content_kind.value,
            "declared_content_kind": self.declared_content_kind.value,
            "executable": self.executable,
            "hash_ok": self.hash_ok,
            "stale": self.stale,
            "test_scope": self.test_scope,
            "snapshot_id": self.snapshot_id,
            "language": self.language,
        }


@dataclass(frozen=True)
class SupportResult:
    """Resultado do eixo SUSTENTAÇÃO.

    `outcome`:

    - `supported`: alguma checagem (b)-(d) aplicável passou e nenhuma falhou.
    - `contradicted`: o trecho diz o oposto/diferente do afirmado.
    - `insufficient`: nada mecânico aplicável, ou checagem aplicável não passou.
    """

    outcome: str
    checks: tuple[Check, ...] = ()
    reasons: tuple[str, ...] = ()
    divergences: tuple[str, ...] = ()
    mocked: bool = False
    mock_markers: tuple[str, ...] = ()
    scope_examined: str = ""

    @property
    def applicable_checks(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.applicable)

    @property
    def mechanical_checks(self) -> tuple[Check, ...]:
        """Checagens (b)-(d): só elas aprovam. `symbol_present` sozinha não."""
        return tuple(
            c for c in self.checks
            if c.applicable and c.name in
            ("predicate_polarity", "effect_present", "exception_present", "literal_preserved")
        )


@dataclass(frozen=True)
class Verdict:
    """Veredito de um claim. `support_recorded_by` só é preenchido em `supported`."""

    claim_id: str
    epistemic: EpistemicStatus
    checks: tuple[Check, ...] = ()
    reasons: tuple[str, ...] = ()
    locations: tuple[LocationResult, ...] = ()
    support: SupportResult | None = None
    nature: FactNature | None = None
    mocked: bool = False
    external_behavior_supported: bool = False
    insufficient: bool = False
    asserted_by: str = ""
    support_recorded_by: str | None = None
    scope_examined: str = ""
    predicate_kind: str = ""

    @property
    def divergences(self) -> tuple[str, ...]:
        return self.support.divergences if self.support else ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "epistemic": self.epistemic.value,
            "nature": self.nature.value if self.nature else None,
            "mocked": self.mocked,
            "external_behavior_supported": self.external_behavior_supported,
            "insufficient": self.insufficient,
            "asserted_by": self.asserted_by,
            "support_recorded_by": self.support_recorded_by,
            "scope_examined": self.scope_examined,
            "reasons": list(self.reasons),
            "divergences": list(self.divergences),
            "checks": [c.as_dict() for c in self.checks],
            "locations": [loc.as_dict() for loc in self.locations],
        }


@dataclass(frozen=True)
class Inconsistency:
    """Divergência entre claims (§15.1). `disputed` diz quem deve cair."""

    kind: str
    claim_ids: tuple[str, ...]
    detail: str
    disputed: tuple[str, ...] = ()
    determinable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "claim_ids": list(self.claim_ids),
            "detail": self.detail,
            "disputed": list(self.disputed),
            "determinable": self.determinable,
        }


# --------------------------------------------------------------------------
# Léxico auxiliar
# --------------------------------------------------------------------------


def _ext(path: str) -> str:
    base = (path or "").rsplit("/", 1)[-1]
    return ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""


def _language_of(path: str) -> str:
    return _EXT_LANG.get(_ext(path), "")


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _norm_operand(text: Any) -> str:
    """Operando comparável: sem espaços, sem aspas externas."""
    s = str(text if text is not None else "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return re.sub(r"\s+", "", s)


def _as_number(text: str) -> float | None:
    try:
        return float(text.replace("_", ""))
    except (TypeError, ValueError):
        return None


def _operand_equal(a: str, b: str) -> bool:
    """Igualdade de operandos, tolerante a `self.x` vs `x` e a `0` vs `0.0`."""
    na, nb = _norm_operand(a), _norm_operand(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    fa, fb = _as_number(na), _as_number(nb)
    if fa is not None and fb is not None:
        return fa == fb
    return na.endswith("." + nb) or nb.endswith("." + na)


def _norm_comparator(token: Any) -> str | None:
    if token is None:
        return None
    raw = _norm_ws(str(token)).lower()
    return _CMP_ALIASES.get(raw) or _CMP_ALIASES.get(raw.replace(" ", ""))


def _identifiers(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text or ""))


def _word_in(token: str, text: str) -> bool:
    if not token:
        return False
    return re.search(r"(?<![A-Za-z_0-9])" + re.escape(token) + r"(?![A-Za-z_0-9])", text) is not None


# --------------------------------------------------------------------------
# Classificação de conteúdo (§5.4) — DETECTADA, não declarada
# --------------------------------------------------------------------------


def _kind_supports_implemented(kind: ContentKind) -> bool:
    """Delegação executável a `knowledge.evidence.supports_implemented`.

    A regra "comentário/docstring/markdown/prosa não sustenta implementado"
    mora numa função só, a mesma que o repositório consulta antes de aceitar
    `nature=implemented` + `supported`. Aqui apenas a exercitamos com uma
    evidência-sonda.
    """
    probe = Evidence(
        evidence_id="probe",
        namespace="",
        source_kind=SourceKind.CODE,
        content_kind=kind,
        source_version_id="",
        locator={},
    )
    return supports_implemented(probe)


def _is_python_docstring(snippet: str) -> bool:
    text = textwrap.dedent(snippet).strip()
    if not text:
        return False
    tree = _parse_python(text)
    if tree is not None and getattr(tree, "body", None):
        body = tree.body
        return all(
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
            for n in body
        )
    # Fragmento que não fecha (citação parcial de docstring): decidir pelo
    # delimitador, nunca assumindo executável.
    stripped = re.sub(r'^[rRbBuUfF]{0,2}', "", text)
    return stripped.startswith('"""') or stripped.startswith("'''")


def _parse_python(text: str) -> ast.AST | None:
    """AST de um FRAGMENTO. Devolve `None` quando nada é parseável."""
    dedented = textwrap.dedent(text)
    for candidate in (
        dedented,
        "def __frag__():\n" + textwrap.indent(dedented, "    "),
        "class __Frag__:\n" + textwrap.indent(dedented, "    "),
    ):
        try:
            return ast.parse(candidate)
        except (SyntaxError, ValueError, RecursionError):
            continue
    return None


def classify_content_kind(
    snippet: str, path: str, declared: ContentKind = ContentKind.EXECUTABLE
) -> tuple[ContentKind, str]:
    """`content_kind` REAL do trecho citado, com o motivo da classificação.

    O declarado só é usado para ENDURECER: se o declarante já admitiu
    comentário/docstring, a detecção não pode promover para executável.
    """
    lang = _language_of(path)
    ext = _ext(path)
    detected: ContentKind
    why: str

    lines = [ln for ln in (snippet or "").splitlines() if ln.strip()]
    prefixes = _COMMENT_PREFIX.get(lang, ("#", "//"))

    if not lines:
        detected, why = ContentKind.PROSE, "trecho vazio: nada a sustentar"
    elif ext in _PROSE_EXT:
        detected, why = ContentKind.MARKDOWN, f"extensão {ext} é documento, não código executável"
    elif all(ln.strip().startswith(prefixes) for ln in lines):
        detected, why = ContentKind.COMMENT, "todas as linhas citadas são comentário"
    elif lang == "python" and _is_python_docstring(snippet):
        detected, why = ContentKind.DOCSTRING, "trecho citado é literal de docstring, não código executado"
    elif lang in ("javascript", "typescript", "java", "go", "c") and _norm_ws(snippet).startswith("/*"):
        detected, why = ContentKind.COMMENT, "trecho citado é bloco de comentário"
    elif ext in _CONFIG_EXT:
        detected, why = ContentKind.CONFIG_VALUE, f"extensão {ext} é configuração"
    elif _TEST_PATH.search(path or "") and _ASSERT_MARK.search(snippet or ""):
        detected, why = (
            ContentKind.TEST_ASSERTION,
            "trecho em arquivo de teste com assertion: sustenta expectativa de teste (§5.3)",
        )
    else:
        detected, why = ContentKind.EXECUTABLE, "trecho com código executado"

    if _kind_supports_implemented(detected) and not _kind_supports_implemented(declared):
        return declared, (
            f"detecção diria {detected.value}, mas o declarante já admitiu "
            f"{declared.value}: prevalece o mais restritivo (§5.4)"
        )
    return detected, why


def detect_mocks(snippet: str) -> tuple[str, ...]:
    """Marcadores de dublê de teste presentes no trecho (§5.3)."""
    found: list[str] = []
    for pattern, label in _MOCK_MARKERS:
        if pattern.search(snippet or "") and label not in found:
            found.append(label)
    return tuple(found)


# --------------------------------------------------------------------------
# Extração de predicados e efeitos do TRECHO
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Comparison:
    left: str
    op: str
    right: str
    negated: bool = False
    origin: str = "syntactic"
    #: Texto REAL do trecho quando esta comparação é a forma espelhada
    #: (operandos trocados) de uma comparação extraída — `None` na forma
    #: original. Só existe para que a mensagem cite o que está de fato escrito
    #: no trecho, nunca a forma reescrita (Onda 11-T1).
    mirror_of: str | None = None

    def render(self) -> str:
        core = f"{self.left} {self.op} {self.right}"
        return f"not ({core})" if self.negated else core

    @property
    def display(self) -> str:
        """Texto a citar em mensagens: o literal do trecho, mesmo quando o
        casamento veio pela forma espelhada."""
        return self.mirror_of if self.mirror_of is not None else self.render()


def _src(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError, TypeError):  # pragma: no cover - stdlib garante unparse em 3.9+
        return ""


def _call_name(node: ast.Call) -> str:
    return _src(node.func)


_LEX_CMP = re.compile(
    r"(?P<left>[A-Za-z_][A-Za-z_0-9\.\[\]'\"]*)\s*"
    r"(?P<op>===|!==|==|!=|<=|>=|<|>)\s*"
    r"(?P<right>[-A-Za-z_0-9\.\"']+)"
)
_LEX_IS = re.compile(
    r"(?P<left>[A-Za-z_][A-Za-z_0-9\.\[\]'\"]*)\s+(?P<op>is\s+not|is|not\s+in|in)\s+(?P<right>[-A-Za-z_0-9\.\"']+)"
)


def extract_comparisons(snippet: str, path: str = "") -> tuple[tuple[_Comparison, ...], str]:
    """Comparações presentes no trecho e o nível de resolução usado.

    Prefere a gramática (`ast`); cai para varredura léxica quando o fragmento
    não é Python parseável. O nível volta junto para que a checagem declare de
    onde veio (§6.2: heurística não é resolução).
    """
    lang = _language_of(path)
    out: list[_Comparison] = []
    if lang in ("python", ""):
        tree = _parse_python(snippet or "")
        if tree is not None:
            _walk_comparisons(tree, False, out)
            if out:
                return tuple(out), "syntactic"
            return (), "syntactic"
    for match in _LEX_CMP.finditer(snippet or ""):
        op = match.group("op").replace("===", "==").replace("!==", "!=")
        out.append(_Comparison(match.group("left"), op, match.group("right"), origin="heuristic"))
    for match in _LEX_IS.finditer(snippet or ""):
        op = _norm_ws(match.group("op"))
        out.append(_Comparison(match.group("left"), op, match.group("right"), origin="heuristic"))
    return tuple(out), ("heuristic" if out else "none")


def _walk_comparisons(node: ast.AST, negated: bool, out: list[_Comparison]) -> None:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _walk_comparisons(node.operand, not negated, out)
        return
    if isinstance(node, ast.Compare):
        left = _src(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            sym = _OP_SYM.get(type(op))
            right = _src(comparator)
            if sym:
                out.append(_Comparison(left, sym, right, negated))
            left = right
    if isinstance(node, ast.Call):
        base = _call_name(node).rsplit(".", 1)[-1]
        spec = _ASSERT_CMP.get(base)
        if spec is not None:
            op, fixed_right = spec
            args = [a for a in node.args if not isinstance(a, ast.Starred)]
            if fixed_right is not None and args:
                out.append(_Comparison(_src(args[0]), op, fixed_right, negated))
            elif fixed_right is None and len(args) >= 2:
                out.append(_Comparison(_src(args[0]), op, _src(args[1]), negated))
    for child in ast.iter_child_nodes(node):
        _walk_comparisons(child, negated, out)


def _mirror_comparison(c: _Comparison) -> _Comparison | None:
    """A MESMA comparação vista com os operandos trocados, ou `None` quando o
    operador não tem espelho definido (`is`/`in` não são de ordem)."""
    mirrored_op = _MIRROR_CMP.get(c.op)
    if mirrored_op is None:
        return None
    return _Comparison(
        left=c.right, op=mirrored_op, right=c.left, negated=c.negated,
        origin=c.origin, mirror_of=c.render(),
    )


def _with_mirrors(comparisons: Sequence[_Comparison]) -> list[_Comparison]:
    """Comparações extraídas + suas formas espelhadas, para casamento de
    predicado tolerante a operandos trocados (Onda 11-T1)."""
    out = list(comparisons)
    for c in comparisons:
        mirrored = _mirror_comparison(c)
        if mirrored is not None and mirrored not in out:
            out.append(mirrored)
    return out


@dataclass(frozen=True)
class _Effects:
    returns: tuple[str, ...] = ()
    returns_all_literal: bool = False
    raises: tuple[str, ...] = ()
    raises_all_named: bool = False
    calls: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    reads: frozenset[str] = frozenset()
    publishes: frozenset[str] = frozenset()
    resolution: str = "none"

    def of_kind(self, kind: str) -> frozenset[str]:
        return {
            "call": self.calls,
            "write": self.writes,
            "read": self.reads,
            "publish": self.publishes,
        }.get(kind, frozenset())


def extract_effects(snippet: str, path: str = "") -> _Effects:
    """Efeitos observáveis no trecho: return, raise, call, write, read, publish."""
    lang = _language_of(path)
    if lang in ("python", ""):
        tree = _parse_python(snippet or "")
        if tree is not None:
            return _ast_effects(tree)
    return _lex_effects(snippet or "")


def _ast_effects(tree: ast.AST) -> _Effects:
    returns: list[str] = []
    literal_flags: list[bool] = []
    raises: list[str] = []
    named_flags: list[bool] = []
    calls: set[str] = set()
    writes: set[str] = set()
    reads: set[str] = set()
    publishes: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            value = node.value
            returns.append(_src(value) if value is not None else "")
            literal_flags.append(isinstance(value, ast.Constant))
        elif isinstance(node, ast.Raise):
            exc = node.exc
            if exc is None:
                raises.append("")
                named_flags.append(False)
            elif isinstance(exc, ast.Call):
                raises.append(_call_name(exc))
                named_flags.append(True)
            else:
                raises.append(_src(exc))
                named_flags.append(isinstance(exc, (ast.Name, ast.Attribute)))
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if not name:
                continue
            calls.add(name)
            base = name.rsplit(".", 1)[-1]
            calls.add(base)
            if base in _WRITE_VERBS:
                writes.update({name, base})
            if base in _READ_VERBS:
                reads.update({name, base})
            if base in _PUBLISH_VERBS:
                publishes.update({name, base})
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, (ast.Attribute, ast.Subscript)):
                    writes.add(_src(target))

    return _Effects(
        returns=tuple(returns),
        returns_all_literal=bool(returns) and all(literal_flags),
        raises=tuple(raises),
        raises_all_named=bool(raises) and all(named_flags),
        calls=frozenset(calls),
        writes=frozenset(writes),
        reads=frozenset(reads),
        publishes=frozenset(publishes),
        resolution="syntactic",
    )


_LEX_RETURN = re.compile(r"\breturn\b[ \t]*(?P<value>[^;\n]*)")
_LEX_THROW = re.compile(r"\bthrow\s+(?:new\s+)?(?P<exc>[A-Za-z_][\w\.]*)")
_LEX_CALL = re.compile(r"(?P<name>[A-Za-z_][\w\.]*)\s*\(")


def _lex_effects(snippet: str) -> _Effects:
    returns = tuple(m.group("value").strip() for m in _LEX_RETURN.finditer(snippet))
    raises = tuple(m.group("exc") for m in _LEX_THROW.finditer(snippet))
    calls: set[str] = set()
    writes: set[str] = set()
    reads: set[str] = set()
    publishes: set[str] = set()
    for match in _LEX_CALL.finditer(snippet):
        name = match.group("name")
        base = name.rsplit(".", 1)[-1]
        calls.update({name, base})
        if base in _WRITE_VERBS:
            writes.update({name, base})
        if base in _READ_VERBS:
            reads.update({name, base})
        if base in _PUBLISH_VERBS:
            publishes.update({name, base})
    return _Effects(
        returns=returns,
        returns_all_literal=False,  # léxico não distingue literal de expressão
        raises=raises,
        raises_all_named=bool(raises),
        calls=frozenset(calls),
        writes=frozenset(writes),
        reads=frozenset(reads),
        publishes=frozenset(publishes),
        resolution="heuristic" if (returns or raises or calls) else "none",
    )


def _parse_effect(text: Any) -> tuple[str, str]:
    """`"return True"` -> `("return", "True")`; desconhecido -> `("mention", texto)`."""
    raw = _norm_ws(str(text or ""))
    if not raw:
        return "", ""
    head, _, rest = raw.partition(":") if ":" in raw.split(" ", 1)[0] else raw.partition(" ")
    kind = _EFFECT_ALIASES.get(head.strip().lower())
    if kind:
        return kind, rest.strip()
    return "mention", raw


# --------------------------------------------------------------------------
# Eixo 1 — LOCALIZAÇÃO
# --------------------------------------------------------------------------


def check_location(claim_evidence: ClaimEvidence, snapshot: Snapshot) -> LocationResult:
    """O localizador resolve, o conteúdo ainda é o capturado e é executável?

    Nenhuma destas perguntas aprova a afirmação — é exatamente a confusão que
    F03 registra. `ok=True` significa apenas "há um trecho real e estável para
    LER"; quem lê é `check_support`.
    """
    base = LocationResult(
        ok=False,
        path=claim_evidence.path,
        start_line=claim_evidence.start_line,
        end_line=claim_evidence.end_line,
        declared_content_kind=claim_evidence.content_kind,
        snapshot_id=snapshot.snapshot_id,
        language=_language_of(claim_evidence.path),
    )

    if claim_evidence.source_kind is not SourceKind.CODE:
        return replace(
            base,
            reason=(
                f"fonte {claim_evidence.source_kind.value} não é resolvível por snapshot de "
                "código; localização não verificada aqui"
            ),
        )

    try:
        resolved = resolve_evidence(
            snapshot, claim_evidence.path, claim_evidence.start_line, claim_evidence.end_line
        )
    except SnapshotStale as exc:
        return replace(base, stale=True, reason=f"snapshot obsoleto: {exc}")
    except (PathNotInSnapshot, EvidenceRangeInvalid) as exc:
        return replace(base, reason=f"localizador não resolve: {exc}")
    except SnapshotError as exc:  # pragma: no cover - rede de segurança
        return replace(base, reason=f"falha ao resolver localizador: {exc}")

    snippet = resolved["snippet"]
    locator = resolved["locator"]

    hash_ok: bool | None = None
    if claim_evidence.snippet_hash:
        hash_ok = claim_evidence.snippet_hash == _snippet_hash(snippet)
        if not hash_ok:
            return replace(
                base,
                snippet=snippet,
                locator=locator,
                hash_ok=False,
                reason=(
                    "hash do trecho citado não bate com o conteúdo atual: a fonte mudou sob a "
                    "citação"
                ),
            )

    content_kind, why = classify_content_kind(snippet, claim_evidence.path, claim_evidence.content_kind)
    executable = _kind_supports_implemented(content_kind)
    test_scope = bool(_TEST_PATH.search(claim_evidence.path or "")) or content_kind is ContentKind.TEST_ASSERTION

    return LocationResult(
        ok=True,
        path=claim_evidence.path,
        start_line=claim_evidence.start_line,
        end_line=claim_evidence.end_line,
        reason=why,
        snippet=snippet,
        locator=locator,
        content_kind=content_kind,
        declared_content_kind=claim_evidence.content_kind,
        executable=executable,
        hash_ok=hash_ok,
        stale=False,
        test_scope=test_scope,
        snapshot_id=snapshot.snapshot_id,
        language=_language_of(claim_evidence.path),
    )


# --------------------------------------------------------------------------
# Eixo 2 — SUSTENTAÇÃO
# --------------------------------------------------------------------------


def check_support(
    claim: Claim,
    snippet: str,
    extraction: Any = None,
    *,
    location: LocationResult | None = None,
) -> SupportResult:
    """O trecho citado DIZ o que o claim afirma? (§15.1, checagens mecânicas)

    `extraction` é um `analysis.extractors.registry.ExtractionResult` opcional:
    quando presente, símbolos e referências resolvidas sintaticamente entram
    como fonte de checagem em vez da varredura léxica.

    Regra que fecha F03: `outcome="supported"` exige ao menos uma checagem
    (b)-(d) APLICÁVEL e passando. Presença da citação não é checagem.
    """
    path = location.path if location else ""
    scope = location.span if location else (claim.evidence_refs[0].span if claim.evidence_refs else "")
    checks: list[Check] = []
    reasons: list[str] = []
    divergences: list[str] = []

    markers = detect_mocks(snippet)
    if markers:
        checks.append(
            Check(
                name="mock_boundary",
                applicable=True,
                passed=True,
                contradicted=False,
                resolution="heuristic",
                detail=(
                    f"dublê de teste no trecho ({', '.join(markers)}): sustenta o cenário "
                    "modelado, não o comportamento real do serviço externo (§5.3)"
                ),
                scope_examined=scope,
            )
        )

    checks.append(_check_symbol(claim, snippet, extraction, location, scope))
    checks.append(_check_predicate(claim, snippet, path, scope))
    checks.append(_check_effect(claim, snippet, path, extraction, location, scope))
    checks.append(_check_exception(claim, snippet, path, scope))
    checks.append(_check_literals(claim, snippet, scope))

    contradicted = [c for c in checks if c.contradicted]
    for c in contradicted:
        divergences.append(f"{c.name}: {c.detail}")

    mechanical = [
        c for c in checks
        if c.applicable and c.name in
        ("predicate_polarity", "effect_present", "exception_present", "literal_preserved")
    ]
    failed = [c for c in checks if c.applicable and c.passed is False]

    if contradicted:
        outcome = "contradicted"
        reasons.append(
            "trecho citado diverge do afirmado: citação real não confirma afirmação invertida (F03)"
        )
    elif not mechanical:
        outcome = "insufficient"
        reasons.append(
            "nenhuma checagem mecânica de predicado/efeito/literal aplicável ao claim; "
            "existência da citação não sustenta (F03)"
        )
    elif failed:
        outcome = "insufficient"
        reasons.append(
            "checagem aplicável não passou: " + "; ".join(f"{c.name} ({c.detail})" for c in failed)
        )
    else:
        outcome = "supported"
        reasons.append(
            "checagens mecânicas aplicáveis passaram: "
            + ", ".join(f"{c.name}[{c.resolution}]" for c in mechanical)
        )

    return SupportResult(
        outcome=outcome,
        checks=tuple(checks),
        reasons=tuple(reasons),
        divergences=tuple(divergences),
        mocked=bool(markers),
        mock_markers=markers,
        scope_examined=scope,
    )


def _check_symbol(
    claim: Claim,
    snippet: str,
    extraction: Any,
    location: LocationResult | None,
    scope: str,
) -> Check:
    """(a) o símbolo citado existe no trecho ou no escopo extraído?"""
    token = claim.cited_symbol
    if not token:
        return Check(
            "symbol_present", False, None,
            "claim sem símbolo/sujeito citado: nada a localizar", scope_examined=scope,
        )
    leaf = token.rsplit(".", 1)[-1]

    symbols = list(getattr(extraction, "symbols", ()) or ())
    if symbols and location is not None:
        overlapping = [
            s for s in symbols
            if _same_path(s.path, location.path)
            and s.line_start <= location.end_line
            and s.line_end >= location.start_line
            and (s.name == leaf or s.qualname == token or s.qualname.endswith("." + leaf))
        ]
        if overlapping:
            best = overlapping[0]
            return Check(
                "symbol_present", True, True,
                f"símbolo {best.qualname} extraído em {best.path}:{best.line_start}-{best.line_end} "
                f"cobre o intervalo citado",
                resolution=best.resolution, scope_examined=scope,
            )
        elsewhere = [
            s for s in symbols
            if _same_path(s.path, location.path)
            and (s.name == leaf or s.qualname == token or s.qualname.endswith("." + leaf))
        ]
        if elsewhere and not _word_in(leaf, snippet):
            s = elsewhere[0]
            return Check(
                "symbol_present", True, False,
                f"símbolo {s.qualname} existe em {s.path}:{s.line_start}-{s.line_end}, FORA do "
                "intervalo citado: a citação não aponta para ele",
                resolution=s.resolution, contradicted=False, scope_examined=scope,
            )

    if _word_in(leaf, snippet):
        return Check(
            "symbol_present", True, True,
            f"identificador {leaf!r} presente no trecho citado",
            resolution="heuristic", scope_examined=scope,
        )
    return Check(
        "symbol_present", True, False,
        f"identificador {leaf!r} ausente do trecho citado e do escopo extraído",
        resolution="heuristic", scope_examined=scope,
    )


def _same_path(a: str, b: str) -> bool:
    na, nb = (a or "").replace("\\", "/").strip("/"), (b or "").replace("\\", "/").strip("/")
    return bool(na) and bool(nb) and (na == nb or na.endswith("/" + nb) or nb.endswith("/" + na))


def _check_predicate(claim: Claim, snippet: str, path: str, scope: str) -> Check:
    """(b) comparador/valor/negação afirmados aparecem com a MESMA polaridade?

    É a checagem que fecha a regressão §15.2 nº1: claim `x > 0` com trecho
    `return x < 0` encontra a mesma dupla de operandos sob operador diferente e
    devolve `contradicted`, não silêncio.

    Onda 11-T1, dois refinos sobre a mesma checagem:

    1. Operandos trocados com operador espelhado (`a > b` ≡ `b < a`) são a
       MESMA comparação, não uma ausência de casamento — ver `_with_mirrors`.
    2. Quando nenhum candidato bate exatamente mas o trecho contém a condição
       COMPLEMENTAR exata do afirmado (mesmos operandos, operador de negação
       lógica de `_OPPOSITE`, mesmo valor), isso NÃO é contradição: é o outro
       ramo do MESMO `if`. Devolve `complement_condition`, neutro
       (`passed=None`), e deixa a sustentação para quem enxerga o ramo
       (`knowledge.integrate._branch_association`, que já resolve
       `complement`). Só a divergência REAL — mesmo operador com valor
       diferente, ou operador oposto que não bate no valor — continua caindo
       no `contradicted` abaixo.
    """
    comparator = _norm_comparator(claim.field("comparator"))
    condition = claim.field("condition")
    value = claim.field("value")
    negated_claim = bool(claim.field("negated"))

    if comparator is None and value is None:
        return Check(
            "predicate_polarity", False, None,
            "claim sem comparator/value: predicado não é verificável mecanicamente",
            scope_examined=scope,
        )

    comparisons, resolution = extract_comparisons(snippet, path)
    if not comparisons:
        return Check(
            "predicate_polarity", True, False,
            "nenhuma comparação encontrada no trecho citado para confrontar o predicado afirmado",
            resolution=resolution, scope_examined=scope,
        )

    candidates = _with_mirrors(comparisons)
    left_matches = [
        c for c in candidates
        if (condition is not None and _operand_equal(c.left, str(condition)))
        or (condition is None and value is not None and _operand_equal(c.right, str(value)))
    ]
    if not left_matches:
        return Check(
            "predicate_polarity", True, False,
            f"operando afirmado ({condition or value!r}) não aparece em nenhuma comparação do "
            f"trecho; o trecho compara: {', '.join(sorted({c.render() for c in comparisons}))}",
            resolution=resolution, scope_examined=scope,
        )

    for c in left_matches:
        op_ok = comparator is None or c.op == comparator
        val_ok = value is None or _operand_equal(c.right, str(value))
        pol_ok = c.negated == negated_claim
        if op_ok and val_ok and pol_ok:
            return Check(
                "predicate_polarity", True, True,
                f"trecho contém `{c.display}`, com a mesma polaridade do afirmado",
                resolution=resolution, scope_examined=scope,
            )

    # Nenhum candidato bate exatamente. Antes de acusar contradição: é o
    # caminho COMPLEMENTAR do mesmo `if` (mesmos operandos, operador negado,
    # mesmo valor)? Só reconhecido quando o claim não afirma negação própria
    # — combinar `negated` com complemento é ambiguidade que não afrouxamos.
    if comparator is not None and value is not None and not negated_claim:
        negated_op = _OPPOSITE.get(comparator)
        complement_matches = [
            c for c in left_matches
            if c.op == negated_op and not c.negated and _operand_equal(c.right, str(value))
        ]
        if negated_op is not None and complement_matches:
            c = complement_matches[0]
            return Check(
                "complement_condition", True, None,
                detail=(
                    f"trecho contém `{c.display}`, condição COMPLEMENTAR de "
                    f"`{condition} {comparator} {value}` afirmado (mesmo valor, operador negado "
                    f"{c.op!r} de {comparator!r}): não é contradição — é o outro ramo do mesmo "
                    "`if`; sustentação depende de onde o efeito afirmado está (associação de "
                    "ramo em knowledge.integrate)"
                ),
                resolution=resolution, scope_examined=scope,
            )

    found = ", ".join(sorted({c.display for c in left_matches}))
    asserted = f"{condition} {comparator or '?'} {value}".strip()
    if negated_claim:
        asserted = f"not ({asserted})"
    opposite_note = ""
    if comparator and any(c.op == _OPPOSITE.get(comparator) for c in left_matches):
        opposite_note = f" — o trecho usa o operador OPOSTO de {comparator!r}"
    return Check(
        "predicate_polarity", True, False, contradicted=True,
        detail=(
            f"afirmado `{asserted}`, trecho citado contém `{found}`{opposite_note}: "
            "citação real NÃO confirma a afirmação"
        ),
        resolution=resolution, scope_examined=scope,
    )


def _check_effect(
    claim: Claim,
    snippet: str,
    path: str,
    extraction: Any,
    location: LocationResult | None,
    scope: str,
) -> Check:
    """(c) o efeito afirmado (write/call/raise/return) está no trecho ou na referência?"""
    kind, target = _parse_effect(claim.field("effect"))
    if not kind:
        return Check(
            "effect_present", False, None,
            "claim sem effect: nenhum efeito a confrontar", scope_examined=scope,
        )

    effects = extract_effects(snippet, path)

    if kind == "return":
        if not effects.returns:
            return Check(
                "effect_present", True, False,
                "claim afirma retorno, mas o trecho citado não tem nenhum `return`",
                resolution=effects.resolution, scope_examined=scope,
            )
        if not target:
            return Check(
                "effect_present", True, True,
                f"trecho contém {len(effects.returns)} retorno(s)",
                resolution=effects.resolution, scope_examined=scope,
            )
        if any(_operand_equal(r, target) for r in effects.returns):
            return Check(
                "effect_present", True, True,
                f"trecho retorna `{target}`", resolution=effects.resolution, scope_examined=scope,
            )
        found = ", ".join(f"`{r}`" for r in effects.returns if r) or "`return` sem valor"
        return Check(
            "effect_present", True, False, contradicted=effects.returns_all_literal,
            detail=(
                f"claim afirma retorno `{target}`; trecho retorna {found}"
                + (" — todos literais, portanto divergência" if effects.returns_all_literal else "")
            ),
            resolution=effects.resolution, scope_examined=scope,
        )

    if kind == "raise":
        return _match_raise(target, effects, scope, "effect_present", "efeito de exceção")

    if kind == "mention":
        present = _word_in(target.rsplit(".", 1)[-1], snippet)
        return Check(
            "effect_present", True, present,
            f"efeito afirmado {target!r} {'presente' if present else 'ausente'} no trecho como "
            "menção textual (sem forma de efeito reconhecida)",
            resolution="heuristic", scope_examined=scope,
        )

    observed = effects.of_kind(kind)
    leaf = target.rsplit(".", 1)[-1] if target else ""
    if leaf and any(_operand_equal(o, target) or o.rsplit(".", 1)[-1] == leaf for o in observed):
        return Check(
            "effect_present", True, True,
            f"efeito {kind} sobre {target!r} presente no trecho",
            resolution=effects.resolution, scope_examined=scope,
        )

    if extraction is not None and location is not None and leaf:
        refs = [
            r for r in (getattr(extraction, "references", ()) or ())
            if r.kind == "call"
            and _same_path(r.path, location.path)
            and location.start_line <= r.line <= location.end_line
            and (r.to_name == target or r.to_name.rsplit(".", 1)[-1] == leaf)
        ]
        if refs:
            ref = refs[0]
            return Check(
                "effect_present", True, True,
                f"referência {ref.from_symbol}->{ref.to_name} em {ref.path}:{ref.line} "
                f"(resolved={ref.resolved}) confirma o efeito {kind}",
                resolution=ref.resolution, scope_examined=scope,
            )

    listing = ", ".join(sorted(observed)) or "nenhum"
    return Check(
        "effect_present", True, False,
        f"efeito {kind} sobre {target!r} não encontrado; trecho tem: {listing}",
        resolution=effects.resolution, scope_examined=scope,
    )


def _check_exception(claim: Claim, snippet: str, path: str, scope: str) -> Check:
    exception = claim.field("exception")
    if not exception:
        return Check(
            "exception_present", False, None,
            "claim sem exception: nada a confrontar", scope_examined=scope,
        )
    effects = extract_effects(snippet, path)
    return _match_raise(str(exception), effects, scope, "exception_present", "exceção afirmada")


def _match_raise(target: str, effects: _Effects, scope: str, name: str, label: str) -> Check:
    if not effects.raises:
        return Check(
            name, True, False,
            f"{label} {target!r}, mas o trecho citado não levanta nenhuma exceção",
            resolution=effects.resolution, scope_examined=scope,
        )
    leaf = target.rsplit(".", 1)[-1]
    if any(r == target or r.rsplit(".", 1)[-1] == leaf for r in effects.raises if r):
        return Check(
            name, True, True, f"trecho levanta {target}",
            resolution=effects.resolution, scope_examined=scope,
        )
    found = ", ".join(sorted(r for r in effects.raises if r)) or "re-raise sem nome"
    return Check(
        name, True, False, contradicted=effects.raises_all_named,
        detail=(
            f"{label} {target!r}; trecho levanta {found}"
            + (" — divergência de exceção" if effects.raises_all_named else "")
        ),
        resolution=effects.resolution, scope_examined=scope,
    )


def _check_literals(claim: Claim, snippet: str, scope: str) -> Check:
    """(d) literais citados (SQL, mensagens, templates) foram preservados?"""
    literals = claim.field("literals")
    if not literals:
        return Check(
            "literal_preserved", False, None,
            "claim sem literals: nada a preservar", scope_examined=scope,
        )
    if isinstance(literals, (str, bytes)):
        literals = [literals]
    normalized_snippet = _norm_ws(snippet)
    collapsed = re.sub(r"\s+", "", snippet or "")
    missing: list[str] = []
    for literal in literals:
        text = str(literal)
        if text in (snippet or ""):
            continue
        if _norm_ws(text) and _norm_ws(text) in normalized_snippet:
            continue
        if re.sub(r"\s+", "", text) and re.sub(r"\s+", "", text) in collapsed:
            continue
        if hashlib.sha256(text.encode("utf-8")).hexdigest() == _snippet_hash(snippet):
            continue
        missing.append(text)
    if missing:
        return Check(
            "literal_preserved", True, False,
            "literais afirmados ausentes do trecho citado: "
            + "; ".join(repr(m[:80]) for m in missing),
            resolution="syntactic", scope_examined=scope,
        )
    return Check(
        "literal_preserved", True, True,
        f"{len(list(literals))} literal(is) afirmado(s) presentes no trecho, por substring normalizada",
        resolution="syntactic", scope_examined=scope,
    )


# --------------------------------------------------------------------------
# Veredito
# --------------------------------------------------------------------------


def verify_claim(claim: Claim, snapshot: Snapshot, extraction: Any = None) -> Verdict:
    """Localização + sustentação -> `EpistemicStatus`.

    `SUPPORTED` exige, simultaneamente: localização válida, conteúdo que
    sustenta a natureza afirmada (§5.4), ao menos uma checagem mecânica
    aplicável passando, nenhuma contradição, e `asserted_by` diferente do
    registrador do suporte (§5.3). Qualquer buraco vira
    `inferred`/`unresolved`; qualquer divergência vira `disputed`.
    """
    nature = FactNature.IMPLEMENTED if claim.implies_implemented else FactNature.TEST_EXPECTATION
    scope_examined = "; ".join(e.span for e in claim.evidence_refs) or "(nenhuma evidência citada)"

    if not claim.evidence_refs:
        return Verdict(
            claim_id=claim.claim_id,
            epistemic=EpistemicStatus.UNRESOLVED,
            reasons=("claim sem evidence_refs: não há fonte primária para verificar",),
            nature=None,
            insufficient=True,
            asserted_by=claim.asserted_by,
            scope_examined=scope_examined,
            predicate_kind=claim.predicate_kind,
        )

    locations = tuple(check_location(ref, snapshot) for ref in claim.evidence_refs)
    valid = [loc for loc in locations if loc.ok]
    reasons: list[str] = []

    if not valid:
        stale = any(loc.stale for loc in locations)
        reasons.append(
            "nenhuma citação resolve no snapshot: "
            + "; ".join(f"{loc.span} -> {loc.reason}" for loc in locations)
        )
        if stale:
            reasons.append("conteúdo mudou sob a citação: revisão/snapshot invalidados (§15.2)")
        return Verdict(
            claim_id=claim.claim_id,
            epistemic=EpistemicStatus.UNRESOLVED,
            reasons=tuple(reasons),
            locations=locations,
            nature=None,
            insufficient=True,
            asserted_by=claim.asserted_by,
            scope_examined=scope_examined,
            predicate_kind=claim.predicate_kind,
        )

    supports: list[tuple[LocationResult, SupportResult]] = [
        (loc, check_support(claim, loc.snippet, extraction, location=loc)) for loc in valid
    ]
    all_checks = tuple(c for _, s in supports for c in s.checks)
    mocked = any(s.mocked for _, s in supports)
    mock_markers = sorted({m for _, s in supports for m in s.mock_markers})

    contradicting = [(loc, s) for loc, s in supports if s.outcome == "contradicted"]
    if contradicting:
        loc, sup = contradicting[0]
        reasons.append(
            f"divergência entre claim e trecho citado em {loc.span}: " + "; ".join(sup.divergences)
        )
        reasons.append(
            "citação real sustentando afirmação divergente NÃO confirma o claim (F03, §15.2 nº1)"
        )
        return Verdict(
            claim_id=claim.claim_id,
            epistemic=EpistemicStatus.DISPUTED,
            checks=all_checks,
            reasons=tuple(reasons),
            locations=locations,
            support=sup,
            nature=nature,
            mocked=mocked,
            external_behavior_supported=False,
            insufficient=False,
            asserted_by=claim.asserted_by,
            scope_examined=scope_examined,
            predicate_kind=claim.predicate_kind,
        )

    # Conteúdo que sustenta a natureza afirmada (§5.4).
    def _content_ok(loc: LocationResult) -> bool:
        if claim.implies_implemented:
            return loc.executable
        return loc.content_kind in (ContentKind.TEST_ASSERTION, ContentKind.EXECUTABLE)

    passing = [(loc, s) for loc, s in supports if s.outcome == "supported"]
    eligible = [(loc, s) for loc, s in passing if _content_ok(loc)]
    best_support = (eligible or passing or supports)[0][1]

    if passing and not eligible:
        loc = passing[0][0]
        reasons.append(
            f"trecho citado em {loc.span} é {loc.content_kind.value} ({loc.reason}): "
            "não sustenta comportamento implementado (§5.4), por mais que a citação resolva"
        )
        return Verdict(
            claim_id=claim.claim_id,
            epistemic=EpistemicStatus.INFERRED,
            checks=all_checks,
            reasons=tuple(reasons),
            locations=locations,
            support=best_support,
            nature=None,
            mocked=mocked,
            external_behavior_supported=False,
            insufficient=False,
            asserted_by=claim.asserted_by,
            scope_examined=scope_examined,
            predicate_kind=claim.predicate_kind,
        )

    if not eligible:
        insufficient = all(s.outcome == "insufficient" for _, s in supports)
        any_mechanical = any(s.mechanical_checks for _, s in supports)
        for loc, s in supports:
            reasons.extend(f"{loc.span}: {r}" for r in s.reasons)
        non_impl = [loc for loc in valid if not _content_ok(loc)]
        for loc in non_impl:
            reasons.append(
                f"{loc.span} é {loc.content_kind.value}: não sustenta a natureza afirmada (§5.4)"
            )
        status = EpistemicStatus.INFERRED if any_mechanical else EpistemicStatus.UNRESOLVED
        reasons.append(
            "sem checagem mecânica aprovada: claim permanece "
            f"{status.value} — ambiguidade não vira certeza (§15.1)"
        )
        return Verdict(
            claim_id=claim.claim_id,
            epistemic=status,
            checks=all_checks,
            reasons=tuple(reasons),
            locations=locations,
            support=best_support,
            nature=None,
            mocked=mocked,
            external_behavior_supported=False,
            insufficient=insufficient,
            asserted_by=claim.asserted_by,
            scope_examined=scope_examined,
            predicate_kind=claim.predicate_kind,
        )

    # Candidato a supported. Última porta: quem afirma não registra o próprio suporte.
    loc, sup = eligible[0]
    if (claim.asserted_by or "").strip() == SUPPORT_RECORDER:
        reasons.append(
            f"asserted_by={claim.asserted_by!r} é o próprio registrador de suporte "
            f"({SUPPORT_RECORDER}): quem afirma não registra a própria sustentação (§5.3)"
        )
        return Verdict(
            claim_id=claim.claim_id,
            epistemic=EpistemicStatus.INFERRED,
            checks=all_checks,
            reasons=tuple(reasons),
            locations=locations,
            support=sup,
            nature=None,
            mocked=mocked,
            external_behavior_supported=False,
            asserted_by=claim.asserted_by,
            scope_examined=scope_examined,
            predicate_kind=claim.predicate_kind,
        )

    reasons.extend(sup.reasons)
    reasons.append(
        f"localização válida em {loc.span} ({loc.content_kind.value}) + checagens mecânicas "
        "aplicáveis aprovadas"
    )
    if mocked:
        reasons.append(
            f"dublê de teste presente ({', '.join(mock_markers)}): sustenta o cenário modelado; "
            "comportamento do serviço externo permanece NÃO sustentado (§5.3)"
        )
    if nature is FactNature.TEST_EXPECTATION:
        reasons.append(
            "natureza test_expectation: teste sustenta expectativa, não comportamento implementado"
        )

    return Verdict(
        claim_id=claim.claim_id,
        epistemic=EpistemicStatus.SUPPORTED,
        checks=all_checks,
        reasons=tuple(reasons),
        locations=locations,
        support=sup,
        nature=nature,
        mocked=mocked,
        external_behavior_supported=(nature is FactNature.IMPLEMENTED) and not mocked,
        insufficient=False,
        asserted_by=claim.asserted_by,
        support_recorded_by=SUPPORT_RECORDER,
        scope_examined=scope_examined,
        predicate_kind=claim.predicate_kind,
    )


# --------------------------------------------------------------------------
# Consistência entre claims (§15.1)
# --------------------------------------------------------------------------


def check_consistency(
    claims: Sequence[Claim], verdicts: Sequence[Verdict] = ()
) -> list[Inconsistency]:
    """Resultado vs condições, regra vs exceção, efeito vs ordem.

    `verdicts` é opcional e só é usado pela regra de ORDEM, que precisa das
    linhas reais do trecho. Sem eles, a ordem simplesmente não é declarada —
    "não determinável" nunca vira "consistente".
    """
    items = list(claims)
    by_id = {c.claim_id: c for c in items}
    found: list[Inconsistency] = []

    found.extend(_inconsistent_effects(items))
    found.extend(_orphan_exceptions(items, by_id))
    found.extend(_effect_order(items, verdicts))
    return found


def _inconsistent_effects(claims: Sequence[Claim]) -> list[Inconsistency]:
    buckets: dict[tuple[str, str, str, str], list[Claim]] = {}
    for claim in claims:
        condition = claim.field("condition")
        comparator = _norm_comparator(claim.field("comparator")) or ""
        value = claim.field("value")
        effect = claim.field("effect")
        if effect is None or (condition is None and not comparator):
            continue
        key = (
            _norm_operand(claim.subject),
            _norm_operand(condition),
            comparator,
            _norm_operand(value),
        )
        buckets.setdefault(key, []).append(claim)

    out: list[Inconsistency] = []
    for key, group in buckets.items():
        effects = {_norm_ws(str(c.field("effect"))).lower() for c in group}
        if len(group) < 2 or len(effects) < 2:
            continue
        ids = tuple(c.claim_id for c in group)
        detail = (
            f"mesma condição `{key[1]} {key[2]} {key[3]}` sobre {key[0]!r} com efeitos "
            "contraditórios: "
            + "; ".join(
                f"{c.claim_id} [{c.predicate_kind}] -> {_norm_ws(str(c.field('effect')))}"
                for c in group
            )
        )
        kinds = {c.predicate_kind for c in group}
        kind = "rule_vs_test" if {"behavior", "test"} <= kinds or {"contract", "test"} <= kinds else "contradictory_effect"
        out.append(Inconsistency(kind=kind, claim_ids=ids, detail=detail, disputed=ids))
    return out


def _orphan_exceptions(
    claims: Sequence[Claim], by_id: Mapping[str, Claim]
) -> list[Inconsistency]:
    out: list[Inconsistency] = []
    for claim in claims:
        rule_id = claim.field("exception_to")
        if not rule_id:
            continue
        rule = by_id.get(str(rule_id))
        if rule is None:
            out.append(
                Inconsistency(
                    kind="exception_without_rule",
                    claim_ids=(claim.claim_id,),
                    detail=(
                        f"exceção {claim.claim_id} declara exception_to={rule_id!r}, "
                        "regra inexistente no conjunto verificado"
                    ),
                    disputed=(claim.claim_id,),
                )
            )
            continue
        exc_tokens = _identifiers(str(claim.field("condition") or "")) | _identifiers(claim.subject)
        rule_tokens = (
            _identifiers(str(rule.field("condition") or ""))
            | _identifiers(rule.subject)
            | _identifiers(str(rule.field("value") or ""))
        )
        if not exc_tokens:
            out.append(
                Inconsistency(
                    kind="exception_without_condition",
                    claim_ids=(claim.claim_id, rule.claim_id),
                    detail=f"exceção {claim.claim_id} não declara condição própria a confrontar com a regra",
                    disputed=(claim.claim_id,),
                    determinable=False,
                )
            )
            continue
        if not (exc_tokens & rule_tokens):
            out.append(
                Inconsistency(
                    kind="exception_condition_absent",
                    claim_ids=(claim.claim_id, rule.claim_id),
                    detail=(
                        f"exceção {claim.claim_id} cita condição ({sorted(exc_tokens)}) que não "
                        f"aparece na regra {rule.claim_id} ({sorted(rule_tokens)})"
                    ),
                    disputed=(claim.claim_id,),
                )
            )
    return out


def _effect_order(claims: Sequence[Claim], verdicts: Sequence[Verdict]) -> list[Inconsistency]:
    snippets: dict[str, LocationResult] = {}
    for verdict in verdicts:
        for loc in verdict.locations:
            if loc.ok:
                snippets[verdict.claim_id] = loc
                break

    out: list[Inconsistency] = []
    for claim in claims:
        after = claim.field("after")
        effect = claim.field("effect")
        if not after or not effect:
            continue
        loc = snippets.get(claim.claim_id)
        if loc is None:
            out.append(
                Inconsistency(
                    kind="effect_order",
                    claim_ids=(claim.claim_id,),
                    detail=(
                        f"ordem `{effect}` após `{after}` não determinável: sem trecho resolvido "
                        "para este claim"
                    ),
                    disputed=(),
                    determinable=False,
                )
            )
            continue
        _, effect_target = _parse_effect(effect)
        effect_token = (effect_target or str(effect)).rsplit(".", 1)[-1]
        after_token = str(after).rsplit(".", 1)[-1]
        effect_line = _first_line_with(loc.snippet, effect_token)
        after_line = _first_line_with(loc.snippet, after_token)
        if effect_line is None or after_line is None:
            out.append(
                Inconsistency(
                    kind="effect_order",
                    claim_ids=(claim.claim_id,),
                    detail=(
                        f"ordem `{effect_token}` após `{after_token}` não determinável pelas "
                        f"linhas de {loc.span}: token ausente"
                    ),
                    disputed=(),
                    determinable=False,
                )
            )
            continue
        if effect_line < after_line:
            out.append(
                Inconsistency(
                    kind="effect_order",
                    claim_ids=(claim.claim_id,),
                    detail=(
                        f"claim afirma `{effect_token}` APÓS `{after_token}`, mas em {loc.span} "
                        f"`{effect_token}` está na linha {loc.start_line + effect_line} e "
                        f"`{after_token}` na linha {loc.start_line + after_line}: ordem inversa"
                    ),
                    disputed=(claim.claim_id,),
                )
            )
    return out


def _first_line_with(snippet: str, token: str) -> int | None:
    """Primeira linha (0-based) em que o token OCORRE COMO EFEITO.

    Prefere a forma de chamada (`token(`): sem isso, uma anotação de tipo
    (`-> Evidence:`) ou uma menção em comentário passaria por "o efeito
    aconteceu aqui" e a regra de ordem produziria divergência falsa. Só quando
    nenhuma chamada existe é que a ocorrência textual, fora de comentário,
    é aceita.
    """
    if not token:
        return None
    lines = (snippet or "").splitlines()
    call_form = re.compile(r"(?<![A-Za-z_0-9])" + re.escape(token) + r"\s*\(")
    for index, line in enumerate(lines):
        if call_form.search(line):
            return index
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("#", "//", "*")):
            continue
        if _word_in(token, line):
            return index
    return None


def apply_inconsistencies(
    verdicts: Sequence[Verdict], inconsistencies: Sequence[Inconsistency]
) -> list[Verdict]:
    """Rebaixa a `disputed` os vereditos apontados por uma inconsistência.

    É o passo que impede que "regra e teste divergem" termine em aprovação:
    a divergência vira estado no veredito, não nota de rodapé.
    """
    by_claim: dict[str, list[Inconsistency]] = {}
    for inc in inconsistencies:
        for claim_id in inc.disputed:
            by_claim.setdefault(claim_id, []).append(inc)

    out: list[Verdict] = []
    for verdict in verdicts:
        hits = by_claim.get(verdict.claim_id)
        if not hits:
            out.append(verdict)
            continue
        reasons = verdict.reasons + tuple(f"inconsistência {h.kind}: {h.detail}" for h in hits)
        out.append(
            replace(
                verdict,
                epistemic=EpistemicStatus.DISPUTED,
                reasons=reasons,
                support_recorded_by=None,
                external_behavior_supported=False,
            )
        )
    return out


# --------------------------------------------------------------------------
# Releitura independente focalizada (§15.1)
# --------------------------------------------------------------------------


def reread_obligations(verdicts: Sequence[Verdict]) -> list[dict[str, Any]]:
    """Obrigações de releitura da FONTE PRIMÁRIA, com faixa exata.

    Não chama LLM: W4 despacha. E não existe campo de confiança — §15.1 exige
    justificativa verificável, não voto de confiança, então o que sai aqui são
    as perguntas mecânicas que ficaram abertas.
    """
    out: list[dict[str, Any]] = []
    for verdict in verdicts:
        triggers: list[str] = []
        if verdict.epistemic is EpistemicStatus.DISPUTED:
            triggers.append("veredito disputed: divergência entre claim e trecho")
        if verdict.epistemic is EpistemicStatus.UNRESOLVED and any(l.ok for l in verdict.locations):
            triggers.append("localização válida sem checagem mecânica aprovada")
        if verdict.epistemic is EpistemicStatus.INFERRED and verdict.predicate_kind in (
            "behavior", "contract", "data", "config"
        ):
            triggers.append(
                f"claim de impacto ({verdict.predicate_kind}) sustentado apenas por inferência"
            )
        if len([l for l in verdict.locations if l.ok]) > 1:
            triggers.append("multi-evidência: leitura conjunta das faixas citadas")
        if verdict.mocked and verdict.predicate_kind != "test":
            triggers.append("dublê de teste citado para claim que não é de teste")
        if not triggers:
            continue

        primary = next((l for l in verdict.locations if l.ok), None) or (
            verdict.locations[0] if verdict.locations else None
        )
        questions = [
            f"{c.name}: {c.detail}"
            for c in verdict.checks
            if c.applicable and (c.passed is False or c.contradicted)
        ]
        if not questions:
            # Sem checagem falha: o motivo veio de conteúdo não-implementante,
            # de inconsistência entre claims ou de ausência de campo verificável.
            questions = [
                r for r in verdict.reasons
                if r.startswith("inconsistência") or "§5.4" in r or "não sustenta" in r
            ] or list(verdict.divergences)
        if not questions:
            questions = [
                "nenhuma checagem mecânica foi aplicável: quais campos de statement_fields "
                "(comparator/value/effect/literals) faltam para tornar o claim verificável?"
            ]

        out.append(
            {
                "claim_id": verdict.claim_id,
                "kind": "reread_primary_source",
                "triggers": triggers,
                "epistemic": verdict.epistemic.value,
                "primary_source": (
                    {
                        "path": primary.path,
                        "start_line": primary.start_line,
                        "end_line": primary.end_line,
                        "snapshot_id": primary.snapshot_id,
                        "content_kind": primary.content_kind.value,
                    }
                    if primary
                    else None
                ),
                "range_exact": primary.span if primary else None,
                "all_ranges": [l.span for l in verdict.locations],
                "questions": questions,
                "divergences": list(verdict.divergences),
                "scope_examined": verdict.scope_examined,
                "dispatch": "pending",
                "dispatcher": "W4",
                "confidence_vote": None,
            }
        )
    return out


# --------------------------------------------------------------------------
# Cobertura
# --------------------------------------------------------------------------


def verification_summary(verdicts: Sequence[Verdict]) -> dict[str, Any]:
    """Contagem por estado e por checagem.

    Deliberadamente SEM score de qualidade documental: nada de métrica por
    idioma, tamanho ou número de seções (W3 remove esse sinal). O que sai aqui
    é o que foi mecanicamente verificado e o que ficou sem verificação.
    """
    counts = {status.value: 0 for status in EpistemicStatus}
    insufficient = 0
    mocked = 0

    def _row() -> dict[str, int]:
        # `neutral` é o balde do `complement_condition` (Onda 11-T1):
        # `applicable=True`, `passed=None`, `contradicted=False` — nem passou
        # nem falhou, a resolução foi deferida para a associação de ramo do
        # consumidor. Contá-lo como `failed` seria um falso-negativo no
        # resumo; `neutral` mantém a distinção visível.
        return {"applicable": 0, "passed": 0, "failed": 0, "contradicted": 0, "neutral": 0}

    by_check: dict[str, dict[str, int]] = {name: _row() for name in CHECK_NAMES}

    for verdict in verdicts:
        counts[verdict.epistemic.value] += 1
        if verdict.insufficient:
            insufficient += 1
        if verdict.mocked:
            mocked += 1
        for check in verdict.checks:
            row = by_check.setdefault(check.name, _row())
            if not check.applicable:
                continue
            row["applicable"] += 1
            if check.contradicted:
                row["contradicted"] += 1
            elif check.passed is True:
                row["passed"] += 1
            elif check.passed is None:
                row["neutral"] += 1
            else:
                row["failed"] += 1

    total = len(verdicts)
    return {
        "total": total,
        "supported": counts[EpistemicStatus.SUPPORTED.value],
        "inferred": counts[EpistemicStatus.INFERRED.value],
        "unresolved": counts[EpistemicStatus.UNRESOLVED.value],
        "disputed": counts[EpistemicStatus.DISPUTED.value],
        "insufficient": insufficient,
        "insufficient_rate": (insufficient / total) if total else 0.0,
        "mocked": mocked,
        "by_check": by_check,
        "support_recorded_by": SUPPORT_RECORDER,
    }
