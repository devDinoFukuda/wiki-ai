"""Contratos dos adaptadores de extração estrutural (§6.2).

Este módulo define **apenas** o contrato e os utilitários léxicos comuns. Ele
não conhece nenhuma linguagem: quem sabe ler Python é `python_ext`, quem sabe
varrer JS/TS é `javascript_ext`, e assim por diante. O registro
(`registry.py`) é quem junta tudo.

Três regras do plano estão implementadas **aqui**, em código executável, e não
apenas descritas:

1. **Comentário e docstring nunca viram símbolo, referência ou regra** (§5.4).
   `Symbol`/`Reference`/`Entrypoint` carregam `content_kind`, e o
   `__post_init__` **rejeita** qualquer valor em `NON_STRUCTURAL_CONTENT`. Não
   existe caminho de código que produza um símbolo a partir de um comentário:
   a construção do objeto levanta `ValueError`.
2. **Heurística não vira resolução** (§6.2). `resolution` é campo obrigatório
   de cada item; `Reference.__post_init__` recusa `resolved=True` combinado com
   `resolution="heuristic"`. Um adaptador sem parser sintático não consegue,
   estruturalmente, emitir uma chamada confirmada.
3. **Localizador de evidência válido** (§5.4). `code_locator` produz exatamente
   os campos que `knowledge.evidence.validate_locator` exige para
   `SourceKind.CODE` (`repo`, `commit`, `path`, `start_line`, `end_line`,
   `snippet_hash`, mais `symbol`/`language` opcionais). `Symbol.locator()` e
   `Reference.locator()` delegam para ele.

Strings executáveis (SQL, templates, mensagens) **não** são descartadas: elas
saem como `ConfigItem`/`DataEntity`, que é o canal previsto no §5.4 para
"literais usados pelo programa continuam sendo código/dados relevantes".

Apenas stdlib. Nenhuma dependência nativa (§6.2, empacotamento `.pyz`).
"""

from __future__ import annotations

import abc
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

# --------------------------------------------------------------------------
# Vocabulários fechados
# --------------------------------------------------------------------------

#: Espécies de símbolo. As seis primeiras são as exigidas pelo §6.2; as demais
#: existem porque Java/TS têm construções que não cabem em `class`/`function`
#: sem perder informação (um `interface` não é uma `class`).
SYMBOL_KINDS: frozenset[str] = frozenset(
    {
        "module",
        "class",
        "function",
        "method",
        "variable",
        "const",
        "interface",
        "enum",
        "record",
        "field",
        "property",
        "package",
    }
)

#: Espécies de referência (§6.2 exige call/import/attribute/inherit).
REFERENCE_KINDS: frozenset[str] = frozenset(
    {
        "call",
        "import",
        "attribute",
        "inherit",
        "implement",
        "export",
        "annotation",
    }
)

#: Espécies de ponto de entrada (§6.1.6).
ENTRYPOINT_KINDS: frozenset[str] = frozenset(
    {"cli", "http", "rpc", "job", "event", "callback", "public_api", "main"}
)

#: Nível de resolução declarado por item e por linguagem (§6.2).
#:
#: - ``syntactic``: veio de um parser da gramática da linguagem (ex.: `ast`).
#: - ``heuristic``: veio de varredura léxica/regex. Nunca é resolução.
#: - ``none``: não houve extração.
RESOLUTION_LEVELS: tuple[str, ...] = ("syntactic", "heuristic", "none")

DIAGNOSTIC_LEVELS: frozenset[str] = frozenset({"info", "warning", "error"})

#: `content_kind` alinhado a `knowledge.models.ContentKind`.
CONTENT_KINDS: frozenset[str] = frozenset(
    {"executable", "config_value", "comment", "docstring", "markdown", "prose"}
)

#: §5.4 — o que NÃO sustenta comportamento implementado e, portanto, o que
#: nunca pode virar `Symbol`/`Reference`/`Entrypoint`.
NON_STRUCTURAL_CONTENT: frozenset[str] = frozenset(
    {"comment", "docstring", "markdown", "prose"}
)

VISIBILITIES: frozenset[str] = frozenset(
    {"public", "protected", "private", "internal", "package"}
)


class ExtractionError(ValueError):
    """Contrato de extração violado (kind desconhecido, intervalo inválido...)."""


# --------------------------------------------------------------------------
# Localizador compatível com knowledge.evidence
# --------------------------------------------------------------------------


def snippet_hash(text: str) -> str:
    """SHA-256 do trecho citado.

    Mesmo algoritmo de `knowledge.evidence.snippet_hash`; reimplementado para
    que `extractors` não dependa de `knowledge` em tempo de import (o pacote é
    consumido também por ferramentas que só carregam a análise).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slice_lines(content: str, line_start: int, line_end: int) -> str:
    """Trecho 1-indexado, inclusivo nos dois extremos — o mesmo que o localizador cita."""
    lines = content.splitlines()
    if line_start < 1:
        raise ExtractionError(f"line_start deve ser >= 1, recebido {line_start}")
    return "\n".join(lines[line_start - 1 : line_end])


def code_locator(
    path: str,
    line_start: int,
    line_end: int,
    *,
    repo: str,
    commit: str,
    content: str | None = None,
    hash_value: str | None = None,
    symbol: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Localizador `SourceKind.CODE` pronto para `knowledge.evidence`.

    Os campos são exatamente os obrigatórios (`repo`, `commit`, `path`,
    `start_line`, `end_line`, `snippet_hash`) mais os opcionais aceitos
    (`symbol`, `language`). Qualquer campo extra faria `validate_locator`
    rejeitar o localizador (schema fechado, §13.1) — por isso a função não
    aceita `**extra`.

    `snippet_hash` sai do conteúdo real das linhas citadas quando `content` é
    fornecido; assim o hash detecta que a fonte mudou sob a citação.
    """
    if line_start < 1 or line_end < line_start:
        raise ExtractionError(f"intervalo inválido para {path}: {line_start}..{line_end}")
    if hash_value is None:
        if content is None:
            raise ExtractionError("code_locator exige content ou hash_value")
        hash_value = snippet_hash(slice_lines(content, line_start, line_end))
    loc: dict[str, Any] = {
        "repo": repo,
        "commit": commit,
        "path": path,
        "start_line": line_start,
        "end_line": line_end,
        "snippet_hash": hash_value,
    }
    if symbol:
        loc["symbol"] = symbol
    if language:
        loc["language"] = language
    return loc


# --------------------------------------------------------------------------
# Itens extraídos
# --------------------------------------------------------------------------


def _check_span(path: str, line_start: int, line_end: int) -> None:
    if not isinstance(line_start, int) or not isinstance(line_end, int):
        raise ExtractionError(f"linhas devem ser inteiras em {path}")
    if line_start < 1 or line_end < line_start:
        raise ExtractionError(f"intervalo inválido em {path}: {line_start}..{line_end}")


def _check_structural_content(content_kind: str, what: str) -> None:
    """§5.4 — barreira executável contra comentário/docstring virando estrutura."""
    if content_kind not in CONTENT_KINDS:
        raise ExtractionError(f"content_kind desconhecido: {content_kind!r}")
    if content_kind in NON_STRUCTURAL_CONTENT:
        raise ExtractionError(
            f"{what} não pode ter content_kind={content_kind!r}: comentário, docstring, "
            "markdown e prosa não sustentam estrutura de comportamento (§5.4)"
        )


@dataclass(frozen=True)
class Symbol:
    """Módulo, classe, função, método, variável ou constante encontrada no código.

    `content_kind` é sempre `executable`: um `Symbol` só existe para código
    que o programa executa. A validação está em `__post_init__`, não em
    convenção de chamador.
    """

    name: str
    kind: str
    path: str
    line_start: int
    line_end: int
    parent: str | None = None
    visibility: str = "public"
    language: str = ""
    qualname: str = ""
    signature: str = ""
    decorators: tuple[str, ...] = ()
    resolution: str = "syntactic"
    content_kind: str = "executable"
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in SYMBOL_KINDS:
            raise ExtractionError(
                f"kind de símbolo desconhecido: {self.kind!r} (aceitos: {sorted(SYMBOL_KINDS)})"
            )
        if self.visibility not in VISIBILITIES:
            raise ExtractionError(f"visibility desconhecida: {self.visibility!r}")
        if self.resolution not in RESOLUTION_LEVELS:
            raise ExtractionError(f"resolution desconhecida: {self.resolution!r}")
        _check_span(self.path, self.line_start, self.line_end)
        _check_structural_content(self.content_kind, "Symbol")
        if not self.qualname:
            object.__setattr__(
                self, "qualname", f"{self.parent}.{self.name}" if self.parent else self.name
            )

    def locator(
        self, *, repo: str, commit: str, content: str | None = None, hash_value: str | None = None
    ) -> dict[str, Any]:
        return code_locator(
            self.path,
            self.line_start,
            self.line_end,
            repo=repo,
            commit=commit,
            content=content,
            hash_value=hash_value,
            symbol=self.qualname or self.name,
            language=self.language or None,
        )


@dataclass(frozen=True)
class Reference:
    """Aresta candidata: chamada, import, acesso a atributo ou herança.

    `resolved=True` significa **alvo existente no conjunto analisado**. O
    `__post_init__` recusa `resolved=True` sob `resolution="heuristic"`: é a
    tradução em código de "regex não constitui resolução de chamadas" (§6.2) e
    de "despacho dinâmico não resolvido não vira chamada confirmada" (§5.5).
    """

    from_symbol: str
    to_name: str
    kind: str
    path: str
    line: int
    resolved: bool = False
    target: str | None = None
    line_end: int | None = None
    language: str = ""
    resolution: str = "syntactic"
    content_kind: str = "executable"
    reason: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in REFERENCE_KINDS:
            raise ExtractionError(
                f"kind de referência desconhecido: {self.kind!r} (aceitos: {sorted(REFERENCE_KINDS)})"
            )
        if self.resolution not in RESOLUTION_LEVELS:
            raise ExtractionError(f"resolution desconhecida: {self.resolution!r}")
        end = self.line_end if self.line_end is not None else self.line
        _check_span(self.path, self.line, end)
        if self.line_end is None:
            object.__setattr__(self, "line_end", self.line)
        _check_structural_content(self.content_kind, "Reference")
        if self.resolved and self.resolution != "syntactic":
            raise ExtractionError(
                f"referência {self.from_symbol}->{self.to_name} marcada resolved=True com "
                f"resolution={self.resolution!r}: heurística não resolve chamadas nem tipos (§6.2)"
            )
        if self.resolved and not self.target:
            raise ExtractionError(
                f"referência {self.from_symbol}->{self.to_name} resolved=True sem target"
            )

    def locator(
        self, *, repo: str, commit: str, content: str | None = None, hash_value: str | None = None
    ) -> dict[str, Any]:
        return code_locator(
            self.path,
            self.line,
            self.line_end or self.line,
            repo=repo,
            commit=commit,
            content=content,
            hash_value=hash_value,
            symbol=self.from_symbol or None,
            language=self.language or None,
        )


@dataclass(frozen=True)
class Entrypoint:
    """Porta de entrada do sistema (§6.1.6): CLI, HTTP, RPC, job, evento, callback, API pública, main."""

    kind: str
    name: str
    path: str
    line: int
    framework: str | None = None
    detail: str = ""
    line_end: int | None = None
    symbol: str | None = None
    language: str = ""
    resolution: str = "syntactic"
    content_kind: str = "executable"
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ENTRYPOINT_KINDS:
            raise ExtractionError(
                f"kind de entrypoint desconhecido: {self.kind!r} (aceitos: {sorted(ENTRYPOINT_KINDS)})"
            )
        if self.resolution not in RESOLUTION_LEVELS:
            raise ExtractionError(f"resolution desconhecida: {self.resolution!r}")
        end = self.line_end if self.line_end is not None else self.line
        _check_span(self.path, self.line, end)
        if self.line_end is None:
            object.__setattr__(self, "line_end", self.line)
        _check_structural_content(self.content_kind, "Entrypoint")

    def locator(
        self, *, repo: str, commit: str, content: str | None = None, hash_value: str | None = None
    ) -> dict[str, Any]:
        return code_locator(
            self.path,
            self.line,
            self.line_end or self.line,
            repo=repo,
            commit=commit,
            content=content,
            hash_value=hash_value,
            symbol=self.symbol or self.name,
            language=self.language or None,
        )


@dataclass(frozen=True)
class ConfigItem:
    """Valor de configuração ou literal executável relevante.

    É o canal do §5.4 para "strings executáveis, SQL, expressões, mensagens e
    templates continuam sendo código/dados relevantes". Diferente de `Symbol`,
    aqui `content_kind` pode ser `config_value`.
    """

    keypath: str
    value: Any
    path: str
    line_start: int
    line_end: int
    kind: str = "config"  # config | sql_literal | template | dependency | literal
    source_format: str = ""
    language: str = ""
    masked: bool = False
    resolution: str = "syntactic"
    content_kind: str = "config_value"
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_span(self.path, self.line_start, self.line_end)
        if self.resolution not in RESOLUTION_LEVELS:
            raise ExtractionError(f"resolution desconhecida: {self.resolution!r}")
        if self.content_kind not in CONTENT_KINDS:
            raise ExtractionError(f"content_kind desconhecido: {self.content_kind!r}")


@dataclass(frozen=True)
class DataEntity:
    """Candidata a entidade de dados vinda de DDL (CREATE TABLE/INDEX/VIEW)."""

    name: str
    kind: str  # table | index | view
    path: str
    line_start: int
    line_end: int
    columns: tuple[Mapping[str, Any], ...] = ()
    resolution: str = "syntactic"
    detail: str = ""

    def __post_init__(self) -> None:
        _check_span(self.path, self.line_start, self.line_end)
        if self.resolution not in RESOLUTION_LEVELS:
            raise ExtractionError(f"resolution desconhecida: {self.resolution!r}")


@dataclass(frozen=True)
class Diagnostic:
    """Limitação declarada. Nunca há silêncio: o que não foi extraído aparece aqui.

    `scope_examined` diz **o que foi olhado** para chegar a esta conclusão —
    sem isso, "não encontrei" é indistinguível de "não procurei".
    """

    level: str
    code: str
    message: str
    scope_examined: str
    path: str | None = None
    paths: tuple[str, ...] = ()
    language: str = ""
    impact: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in DIAGNOSTIC_LEVELS:
            raise ExtractionError(f"level de diagnóstico desconhecido: {self.level!r}")
        if not self.code:
            raise ExtractionError("Diagnostic exige code")
        if not self.scope_examined:
            raise ExtractionError(
                f"Diagnostic {self.code!r} sem scope_examined: 'não encontrei' sem dizer "
                "onde procurou não é limitação declarada"
            )


@dataclass(frozen=True)
class LanguageCoverage:
    """Cobertura declarada por linguagem (§6.1.3 — exclusão nunca é silenciosa)."""

    language: str
    files_total: int
    files_parsed: int
    files_failed: int
    resolution_level: str
    extractor: str = ""

    def __post_init__(self) -> None:
        if self.resolution_level not in RESOLUTION_LEVELS:
            raise ExtractionError(f"resolution_level desconhecido: {self.resolution_level!r}")

    @property
    def files_skipped(self) -> int:
        return max(0, self.files_total - self.files_parsed - self.files_failed)

    @property
    def complete(self) -> bool:
        """Cobertura completa exige parse de todos e resolução sintática."""
        return (
            self.files_total > 0
            and self.files_parsed == self.files_total
            and self.resolution_level == "syntactic"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "files_total": self.files_total,
            "files_parsed": self.files_parsed,
            "files_failed": self.files_failed,
            "files_skipped": self.files_skipped,
            "resolution_level": self.resolution_level,
            "extractor": self.extractor,
        }


@dataclass(frozen=True)
class SourceFile:
    """Arquivo normalizado do inventário.

    O inventário (`analysis/inventory.py`) é de outro módulo; aqui só se exige
    `path` e `content`. `normalize_files` aceita `str`, `os.PathLike`, tupla
    `(path, content)`, `Mapping` e qualquer objeto com atributos `path`/
    `content` — o adaptador não fica acoplado ao formato do produtor.
    """

    path: str
    content: str
    language: str = ""
    encoding: str = "utf-8"
    extra: Mapping[str, Any] = field(default_factory=dict)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def normalize_files(files: Iterable[Any]) -> list[SourceFile]:
    """Converte a entrada do inventário em `SourceFile`, lendo do disco se preciso."""
    out: list[SourceFile] = []
    for item in files:
        if isinstance(item, SourceFile):
            out.append(item)
            continue
        path: Any = None
        content: Any = None
        language = ""
        if isinstance(item, (str, os.PathLike)):
            path = os.fspath(item)
        elif isinstance(item, tuple) and len(item) == 2:
            path, content = item
            path = os.fspath(path)
        elif isinstance(item, Mapping):
            path = item.get("path") or item.get("file")
            content = item.get("content") or item.get("text")
            language = item.get("language") or ""
        else:
            path = getattr(item, "path", None)
            content = getattr(item, "content", None) or getattr(item, "text", None)
            language = getattr(item, "language", "") or ""
        if path is None:
            raise ExtractionError(f"item de inventário sem path: {item!r}")
        path = os.fspath(path)
        if content is None:
            try:
                content = _read_text(path)
            except OSError as exc:
                raise ExtractionError(f"não foi possível ler {path}: {exc}") from exc
        out.append(SourceFile(path=str(path), content=str(content), language=str(language)))
    return out


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------


class CodeExtractor(abc.ABC):
    """Adaptador de extração estrutural para uma linguagem/família de formatos.

    Ciclo de uso (o registro respeita esta ordem):

    1. `detect(path, content)` — o adaptador reivindica o arquivo?
    2. `inventory(files)` — recebe **todos** os arquivos reivindicados de uma
       vez. É aqui que um adaptador com resolução real (Python) constrói o
       índice global de módulos/símbolos; sem esse passo, `resolved` só poderia
       ser `False`.
    3. `symbols` / `references` / `entrypoints` / `configuration` por arquivo.
    4. `diagnostics()` — limitações acumuladas nos passos anteriores.

    `diagnostics()` não recebe argumentos (contrato do §6.2), logo o adaptador
    é **stateful**; `reset()` limpa o estado entre execuções.
    """

    #: Nome da linguagem reportado na matriz de cobertura.
    language: str = ""
    #: Extensões reivindicadas (minúsculas, com ponto).
    extensions: tuple[str, ...] = ()
    #: Nível máximo que este adaptador pode declarar.
    resolution_level: str = "heuristic"

    def __init__(self) -> None:
        self._diagnostics: list[Diagnostic] = []

    # -- estado ------------------------------------------------------------
    def reset(self) -> None:
        self._diagnostics = []

    def add_diagnostic(self, diag: Diagnostic) -> None:
        self._diagnostics.append(diag)

    def diagnostics(self) -> list[Diagnostic]:
        return list(self._diagnostics)

    # -- contrato ----------------------------------------------------------
    def detect(self, path: str, content: str = "") -> bool:
        """Reivindica o arquivo. Padrão: por extensão."""
        return os.path.splitext(path)[1].lower() in self.extensions

    def inventory(self, files: Iterable[Any]) -> list[SourceFile]:
        """Recebe os arquivos reivindicados e prepara índices internos."""
        return normalize_files(files)

    @abc.abstractmethod
    def symbols(self, path: str, content: str) -> list[Symbol]:
        ...

    @abc.abstractmethod
    def references(self, path: str, content: str) -> list[Reference]:
        ...

    @abc.abstractmethod
    def entrypoints(self, path: str, content: str) -> list[Entrypoint]:
        ...

    @abc.abstractmethod
    def configuration(self, path: str, content: str) -> list[ConfigItem]:
        ...

    # -- opcional ----------------------------------------------------------
    def data_entities(self, path: str, content: str) -> list[DataEntity]:
        """DDL encontrada no arquivo. Padrão: nenhuma."""
        return []

    def file_resolution(self, path: str) -> str:
        """Nível efetivo para o arquivo. Padrão: o do adaptador."""
        return self.resolution_level


# --------------------------------------------------------------------------
# Utilitários léxicos compartilhados (JS/TS e Java)
# --------------------------------------------------------------------------

#: Substituto de caractere removido. Espaço, e não remoção, para que **todos**
#: os offsets e números de linha continuem válidos depois da limpeza — é o que
#: mantém o localizador correto num adaptador sem parser.
_BLANK = " "


def scrub_c_like(
    text: str,
    *,
    template_literals: bool = False,
    regex_literals: bool = False,
    keep_strings: bool = True,
) -> tuple[str, list[tuple[int, int, str]]]:
    """Remove comentários de código C-like preservando posições.

    Devolve `(texto_limpo, literais)`:

    - `texto_limpo` tem o mesmo comprimento do original; comentários viraram
      espaços (quebras de linha preservadas). Toda extração de JS/TS e Java
      roda **sobre este texto**, o que torna impossível um comentário virar
      símbolo — ele já não está lá quando a varredura começa (§5.4).
    - `literais` são as strings executáveis encontradas, como
      `(offset_inicial, offset_final, conteúdo)`. Elas não são jogadas fora:
      §5.4 proíbe descartar literais usados pelo programa; SQL e templates
      saem por esse canal.

    Com `keep_strings=True` o corpo das strings continua no texto limpo (é
    código válido); com `False` vira espaço, útil para não confundir a busca de
    palavras-chave com uma string que contenha `class` ou `function`.

    `regex_literals` liga a heurística de literal de regex de JavaScript: uma
    `/` só inicia regex quando o último token significativo não pode terminar
    uma expressão. Sem isso, `a / b / c` seria lido como regex e engoliria o
    resto da linha.
    """
    out = list(text)
    literals: list[tuple[int, int, str]] = []
    i = 0
    n = len(text)
    last_significant = ""

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = _BLANK

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        # Comentário de linha
        if ch == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            blank(i, j)
            i = j
            continue

        # Comentário de bloco (cobre Javadoc/JSDoc: também é comentário)
        if ch == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            blank(i, j)
            i = j
            continue

        # Strings
        if ch in ("'", '"') or (template_literals and ch == "`"):
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    break
                if quote != "`" and text[j] == "\n":
                    break  # string não terminada na linha: aborta sem consumir o arquivo
                j += 1
            end = min(j + 1, n)
            literals.append((i, end, text[i + 1 : j]))
            if not keep_strings:
                blank(i + 1, min(j, n))
            i = end
            last_significant = quote
            continue

        # Literal de regex (apenas JS/TS)
        if regex_literals and ch == "/" and last_significant not in _REGEX_FORBIDDEN_PREV:
            j = i + 1
            in_class = False
            closed = False
            while j < n and text[j] != "\n":
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    closed = True
                    break
                j += 1
            if closed:
                blank(i, j + 1)
                i = j + 1
                last_significant = "/"
                continue

        if not ch.isspace():
            last_significant = ch
        i += 1

    return "".join(out), literals


#: Se o último caractere significativo é um destes, a `/` seguinte é divisão,
#: não início de regex.
_REGEX_FORBIDDEN_PREV: frozenset[str] = frozenset(
    set(")]}") | set("0123456789") | set("abcdefghijklmnopqrstuvwxyz")
    | set("ABCDEFGHIJKLMNOPQRSTUVWXYZ") | {"_", "$", "'", '"', "`"}
)


def line_of(text: str, offset: int) -> int:
    """Linha 1-indexada de um offset — a ponte entre varredura léxica e localizador."""
    if offset < 0:
        return 1
    return text.count("\n", 0, offset) + 1


def build_line_index(text: str) -> list[int]:
    """Offsets de início de cada linha, para conversões repetidas em O(log n)."""
    starts = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            starts.append(idx + 1)
    return starts


def line_from_index(starts: Sequence[int], offset: int) -> int:
    import bisect

    return max(1, bisect.bisect_right(starts, offset))


def match_block_end(text: str, open_offset: int, opener: str = "{", closer: str = "}") -> int:
    """Offset do fechamento equilibrado a partir de `open_offset`.

    Espera texto já passado por `scrub_c_like` (sem comentários). Devolve o
    offset do `}` correspondente, ou `len(text)-1` se o bloco não fecha — um
    arquivo truncado gera intervalo largo, nunca exceção.
    """
    depth = 0
    i = open_offset
    n = len(text)
    while i < n:
        c = text[i]
        if c in ("'", '"', "`"):
            quote = c
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    break
                i += 1
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return max(open_offset, n - 1)


def iter_words(text: str) -> Iterator[tuple[int, str]]:
    """Palavras identificadoras com offset — base das varreduras heurísticas."""
    buf: list[str] = []
    start = 0
    for idx, ch in enumerate(text):
        if ch.isalnum() or ch in ("_", "$"):
            if not buf:
                start = idx
            buf.append(ch)
        elif buf:
            yield start, "".join(buf)
            buf = []
    if buf:
        yield start, "".join(buf)


#: Palavras que caracterizam um literal SQL executável. A checagem exige
#: verbo + complemento (`SELECT ... FROM`, `CREATE TABLE`), para que uma frase
#: em prosa contendo "update" não seja classificada como SQL.
SQL_SIGNATURES: tuple[tuple[str, ...], ...] = (
    ("select", "from"),
    ("insert", "into"),
    ("update", "set"),
    ("delete", "from"),
    ("create", "table"),
    ("create", "index"),
    ("create", "unique"),
    ("create", "view"),
    ("alter", "table"),
    ("drop", "table"),
    ("pragma",),
)


def looks_like_sql(value: str) -> bool:
    """O literal é SQL executável?

    Exige que os termos da assinatura apareçam **na ordem** e como palavras
    inteiras. `"update the fact set"` em prosa não passa por acaso: passaria
    se a prosa realmente contivesse a sequência, e por isso o resultado é
    sempre marcado `heuristic` no `ConfigItem` que o carrega.
    """
    if not value or len(value) < 6:
        return False
    words = [w.lower() for _, w in iter_words(value)]
    if not words:
        return False
    for signature in SQL_SIGNATURES:
        pos = 0
        ok = True
        for term in signature:
            try:
                pos = words.index(term, pos) + 1
            except ValueError:
                ok = False
                break
        if ok:
            return True
    return False


__all__ = [
    "CONTENT_KINDS",
    "CodeExtractor",
    "ConfigItem",
    "DataEntity",
    "DIAGNOSTIC_LEVELS",
    "Diagnostic",
    "ENTRYPOINT_KINDS",
    "Entrypoint",
    "ExtractionError",
    "LanguageCoverage",
    "NON_STRUCTURAL_CONTENT",
    "REFERENCE_KINDS",
    "RESOLUTION_LEVELS",
    "Reference",
    "SQL_SIGNATURES",
    "SYMBOL_KINDS",
    "SourceFile",
    "Symbol",
    "VISIBILITIES",
    "build_line_index",
    "code_locator",
    "iter_words",
    "line_from_index",
    "line_of",
    "looks_like_sql",
    "match_block_end",
    "normalize_files",
    "scrub_c_like",
    "slice_lines",
    "snippet_hash",
]
