"""Inventário integral do escopo capturado por `snapshot.py` (plano §6.1 itens 1-3).

Substitui `codescan/surface.py::scan(module_min_files=3)` como critério de
inclusão. Aquele `scan` descartava um diretório inteiro se tivesse menos de
`module_min_files` arquivos (surface.py:569) — um diretório com uma única
regra de negócio simplesmente não aparecia no inventário. Aqui não existe
filtro por tamanho de diretório: `build()` classifica TODO arquivo presente
no snapshot (não deletado), e só deixa de fora o que está numa exclusão
EXPLÍCITA e nomeada (`_EXCLUDED_DIR_NAMES`), sempre registrada com
`{path, motivo, impacto}` — nunca um descarte silencioso por convenção.

Classificação é por CONTEÚDO/EXTENSÃO/SHEBANG, nunca pela pasta em que o
arquivo está: um `.sql` é classificado pelo que ele contém (migração?
manifesto? código estruturado?), não por estar dentro de uma pasta chamada
`migrations/`. Diretório é pista para o heurístico de nome de arquivo (ex.:
`migrations/0007_add_index.sql`), nunca fronteira que decide sozinha.

`unsupported` nunca é silencioso: todo arquivo que cai nessa classe gera uma
entrada em `Inventory.limitations` (aceite W2: "linguagem não suportada é
exposta como limitação, não ignorada").

`doc` (Markdown, texto livre, RST...) é classificado e listado normalmente,
mas com `evidence_grade=False` — é a aplicação executável de §5.4: comentário,
docstring, README e plano não sustentam fato de comportamento implementado.
"""

from __future__ import annotations

import enum
import os
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .profile import matches_any as _matches_any
from .snapshot import FileState, Snapshot

__all__ = [
    "FileClass",
    "Language",
    "FileClassification",
    "Exclusion",
    "Limitation",
    "Inventory",
    "build",
]


# --------------------------------------------------------------------------
# Vocabulário
# --------------------------------------------------------------------------


class FileClass(str, enum.Enum):
    CODE = "code"
    CONFIG = "config"
    TEST = "test"
    MANIFEST = "manifest"
    MIGRATION = "migration"
    GENERATED = "generated"
    BINARY = "binary"
    UNSUPPORTED = "unsupported"
    DATA = "data"
    DOC = "doc"


class Language(str, enum.Enum):
    """Linguagem de um `FileClass.CODE`.

    O conjunto é AMPLO de propósito. Um repositório COBOL/JCL, Sybase, Delphi
    ou ABAP não pode cair em "não reconhecido" e sumir do plano: linguagem sem
    extrator continua sendo código, e é `investigation.plan()` que abre
    objetivo de DESCOBERTA para ela. `UNKNOWN` é o piso — texto que nenhuma
    extensão mapeou continua sendo candidato a código, com limitação
    registrada, nunca descartado.
    """

    PYTHON = "python"
    JAVA = "java"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    SQL = "sql"
    JSON = "json"
    YAML = "yaml"
    XML = "xml"
    TOML = "toml"
    SHELL = "shell"
    # -- mainframe / legado corporativo
    COBOL = "cobol"
    JCL = "jcl"
    PLI = "pli"
    ASSEMBLER = "assembler"
    REXX = "rexx"
    ABAP = "abap"
    NATURAL = "natural"
    # -- compiladas / gerenciadas
    GO = "go"
    RUST = "rust"
    C = "c"
    CPP = "cpp"
    CSHARP = "csharp"
    KOTLIN = "kotlin"
    SCALA = "scala"
    SWIFT = "swift"
    OBJC = "objc"
    DART = "dart"
    PASCAL = "pascal"
    ADA = "ada"
    FORTRAN = "fortran"
    VBNET = "vbnet"
    FSHARP = "fsharp"
    NIM = "nim"
    ZIG = "zig"
    # -- dinâmicas / script
    PHP = "php"
    RUBY = "ruby"
    PERL = "perl"
    LUA = "lua"
    R = "r"
    JULIA = "julia"
    GROOVY = "groovy"
    POWERSHELL = "powershell"
    BATCH = "batch"
    MATLAB = "matlab"
    TCL = "tcl"
    # -- funcionais
    HASKELL = "haskell"
    ELIXIR = "elixir"
    ERLANG = "erlang"
    CLOJURE = "clojure"
    OCAML = "ocaml"
    LISP = "lisp"
    SCHEME = "scheme"
    # -- front-end / marcação executável
    HTML = "html"
    CSS = "css"
    VUE = "vue"
    SVELTE = "svelte"
    # -- infra como código / esquemas
    TERRAFORM = "terraform"
    PROTOBUF = "protobuf"
    GRAPHQL = "graphql"
    SOLIDITY = "solidity"
    MAKE = "make"
    GRADLE = "gradle"
    DOCKERFILE = "dockerfile"
    # -- pisos
    OUTRO = "outro"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FileClassification:
    path: str
    file_class: FileClass
    language: Language | None
    evidence_grade: bool
    reason: str


@dataclass(frozen=True)
class Exclusion:
    """Exclusão explícita e nomeada — nunca um filtro silencioso por convenção."""

    path: str
    motivo: str
    impacto: str


@dataclass(frozen=True)
class Limitation:
    """Elemento descoberto que não pôde ser classificado/analisado por completo."""

    path: str
    detalhe: str


@dataclass(frozen=True)
class Inventory:
    snapshot_id: str
    files: tuple[FileClassification, ...]
    exclusions: tuple[Exclusion, ...]
    limitations: tuple[Limitation, ...]
    summary: Mapping[str, object]


# --------------------------------------------------------------------------
# Exclusões explícitas (SOMENTE estas; nunca por tamanho de diretório)
# --------------------------------------------------------------------------

#: nome de diretório -> (motivo, impacto). Casa em QUALQUER profundidade —
#: `dirname == segmento_do_caminho`, não prefixo do repo inteiro.
_EXCLUDED_DIR_NAMES: dict[str, tuple[str, str]] = {
    ".git": (
        "diretório interno do controle de versão git",
        "nenhum: metadado de VCS, nunca contém código do produto",
    ),
    "__pycache__": (
        "bytecode Python gerado pelo interpretador",
        "nenhum: artefato derivado, recompilável a partir da fonte `.py`",
    ),
    "node_modules": (
        "dependências JavaScript/Node instaladas por gerenciador de pacotes",
        "nenhum: reproduzível a partir do manifest (package.json/lockfile)",
    ),
    ".venv": (
        "ambiente virtual Python instalado localmente",
        "nenhum: reproduzível a partir do manifest de dependências",
    ),
    "venv": (
        "ambiente virtual Python instalado localmente",
        "nenhum: reproduzível a partir do manifest de dependências",
    ),
    ".pytest_cache": (
        "cache de execução do pytest",
        "nenhum: artefato de execução, não é código-fonte",
    ),
    ".mypy_cache": (
        "cache de execução do mypy",
        "nenhum: artefato de execução, não é código-fonte",
    ),
    ".tox": (
        "ambientes de teste isolados gerenciados pelo tox",
        "nenhum: reproduzível a partir da configuração declarada",
    ),
    "dist": (
        "diretório de build/distribuição declarado",
        "nenhum: saída de build, reproduzível a partir da fonte",
    ),
    "build": (
        "diretório de build declarado",
        "nenhum: saída de build, reproduzível a partir da fonte",
    ),
}


#: Motivo/impacto atribuídos a um diretório excluído PELO PERFIL (não pelo
#: default do módulo). Fica separado para que a origem da exclusão apareça no
#: `Exclusion.motivo` — quem lê o inventário distingue convenção de decisão.
_PROFILE_DIR_REASON = (
    "diretório excluído pelo perfil de análise deste sistema (extra_excluded_dirs)",
    "conteúdo não classificado nem extraído: o perfil declarou que não descreve "
    "comportamento deste sistema",
)
_PROFILE_PATH_REASON = (
    "caminho excluído pelo perfil de análise deste sistema (exclude_paths)",
    "conteúdo não classificado nem extraído: o perfil declarou que não descreve "
    "comportamento deste sistema",
)


def _excluded_dir_hit(
    path: str, excluded_dirs: Mapping[str, tuple[str, str]] = _EXCLUDED_DIR_NAMES
) -> tuple[str, str] | None:
    """`(nome_do_diretório, caminho_ate_o_diretório)` se algum segmento de
    diretório (não o arquivo final) casar com `excluded_dirs`.

    `excluded_dirs` é parâmetro para que o perfil possa ACRESCENTAR nomes numa
    cópia local; `_EXCLUDED_DIR_NAMES` nunca é mutado.
    """
    segments = path.split("/")
    for i, seg in enumerate(segments[:-1]):
        if seg in excluded_dirs:
            return seg, "/".join(segments[: i + 1])
    return None


# --------------------------------------------------------------------------
# Sinais de classificação
# --------------------------------------------------------------------------

_DATA_EXTENSIONS = {".csv", ".tsv", ".parquet", ".jsonl", ".ndjson", ".db", ".sqlite", ".sqlite3", ".avro"}

_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".zip", ".gz", ".tgz", ".tar", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".pyc", ".pyo", ".class", ".jar",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".mov", ".avi", ".wav",
}

_DOC_EXTENSIONS = {".md", ".markdown", ".rst", ".adoc", ".txt", ".rtf", ".docx", ".pdf"}

_GENERATED_LOCK_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "pipfile.lock", "cargo.lock", "composer.lock", "go.sum",
}
_GENERATED_NAME_SUFFIXES = (".min.js", ".min.css", ".g.dart", ".pb.go", ".pb.cc", ".pb.h")
_GENERATED_CONTENT_MARKERS = (
    b"@generated", b"DO NOT EDIT", b"Code generated by", b"This file was automatically generated",
    b"<auto-generated", b"AUTO-GENERATED FILE",
)

_MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "pipfile", "go.mod", "pom.xml",
    "build.gradle", "build.gradle.kts", "cargo.toml", "composer.json",
    "gemfile", "mix.exs",
}
_MANIFEST_SUFFIXES = (".csproj", ".vbproj", ".sln")

_CONFIG_FILENAMES = {
    ".gitignore", ".gitattributes", ".dockerignore", ".npmrc", ".editorconfig",
    ".flake8", ".env", ".env.example", "dockerfile", "makefile",
    "wrangler.toml", "wrangler.jsonc",
}
_CONFIG_EXTENSIONS = {".ini", ".cfg", ".conf", ".properties"}

_TEST_NAME_PATTERNS = (
    re.compile(r"^test_.*\.py$"),
    re.compile(r".*_test\.py$"),
    re.compile(r"^conftest\.py$"),
    re.compile(r".*\.test\.[jt]sx?$"),
    re.compile(r".*\.spec\.[jt]sx?$"),
    re.compile(r"^Test[A-Z].*\.java$"),
    re.compile(r".*Tests?\.java$"),
)
_TEST_CONTENT_MARKERS = (
    b"import pytest", b"import unittest", b"from unittest",
    b"@Test", b"@pytest.mark", b"describe(", b"it('", b'it("',
)

_MIGRATION_NAME_PATTERNS = (
    re.compile(r"^V\d+(\.\d+)*__.+\.sql$"),          # Flyway
    re.compile(r"^\d{3,}[_-].*\.sql$", re.IGNORECASE),  # convenção numérica comum
)
_MIGRATION_CONTENT_MARKERS = (
    (b"down_revision", b"revision"),        # Alembic: precisa dos DOIS
    (b"migrations.Migration",),             # Django
)

#: Extensão -> linguagem. Mapa ÚNICO e amplo: não existe mais uma lista
#: separada de "outras extensões de código sem adaptador dedicado" — ter
#: extrator e ser código são coisas independentes, e confundi-las é o que
#: fazia um repositório COBOL ou Delphi virar `UNSUPPORTED`.
_LANGUAGE_BY_EXT: dict[str, Language] = {
    # Python / JVM / JS
    ".py": Language.PYTHON, ".pyi": Language.PYTHON, ".pyw": Language.PYTHON,
    ".java": Language.JAVA,
    ".js": Language.JAVASCRIPT, ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT, ".cjs": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT, ".tsx": Language.TYPESCRIPT,
    ".mts": Language.TYPESCRIPT, ".cts": Language.TYPESCRIPT,
    ".kt": Language.KOTLIN, ".kts": Language.KOTLIN,
    ".scala": Language.SCALA, ".sc": Language.SCALA,
    ".groovy": Language.GROOVY, ".gvy": Language.GROOVY,
    # SQL e dialetos de banco (Sybase/T-SQL/PL-SQL: procedure, trigger, view)
    ".sql": Language.SQL, ".ddl": Language.SQL, ".dml": Language.SQL,
    ".prc": Language.SQL, ".sp": Language.SQL, ".trg": Language.SQL,
    ".vw": Language.SQL, ".viw": Language.SQL, ".fnc": Language.SQL,
    ".pks": Language.SQL, ".pkb": Language.SQL, ".pls": Language.SQL,
    ".plsql": Language.SQL, ".tsql": Language.SQL,
    # Mainframe
    ".cbl": Language.COBOL, ".cob": Language.COBOL, ".cobol": Language.COBOL,
    ".cpy": Language.COBOL, ".ccp": Language.COBOL,
    ".jcl": Language.JCL, ".proc": Language.JCL, ".prm": Language.JCL,
    ".pli": Language.PLI, ".pl1": Language.PLI,
    ".asm": Language.ASSEMBLER, ".s": Language.ASSEMBLER, ".mac": Language.ASSEMBLER,
    ".rexx": Language.REXX, ".rex": Language.REXX,
    ".abap": Language.ABAP,
    ".nsp": Language.NATURAL, ".nsn": Language.NATURAL,
    # C / C++ / Rust / Go / .NET
    ".c": Language.C, ".h": Language.C,
    ".cc": Language.CPP, ".cpp": Language.CPP, ".cxx": Language.CPP,
    ".hpp": Language.CPP, ".hxx": Language.CPP, ".hh": Language.CPP, ".ipp": Language.CPP,
    ".rs": Language.RUST,
    ".go": Language.GO,
    ".cs": Language.CSHARP, ".csx": Language.CSHARP,
    ".vb": Language.VBNET, ".bas": Language.VBNET, ".cls": Language.VBNET,
    ".frm": Language.VBNET, ".vbs": Language.VBNET,
    ".fs": Language.FSHARP, ".fsx": Language.FSHARP, ".fsi": Language.FSHARP,
    # Delphi / Pascal / Ada / Fortran
    ".pas": Language.PASCAL, ".dpr": Language.PASCAL, ".dfm": Language.PASCAL,
    ".pp": Language.PASCAL, ".lpr": Language.PASCAL, ".dpk": Language.PASCAL,
    ".ada": Language.ADA, ".adb": Language.ADA, ".ads": Language.ADA,
    ".f": Language.FORTRAN, ".f77": Language.FORTRAN, ".f90": Language.FORTRAN,
    ".f95": Language.FORTRAN, ".for": Language.FORTRAN,
    # Apple / mobile
    ".swift": Language.SWIFT,
    ".m": Language.OBJC, ".mm": Language.OBJC,
    ".dart": Language.DART,
    # Dinâmicas
    ".php": Language.PHP, ".phtml": Language.PHP, ".php5": Language.PHP,
    ".rb": Language.RUBY, ".rake": Language.RUBY, ".gemspec": Language.RUBY,
    ".erb": Language.RUBY,
    ".pl": Language.PERL, ".pm": Language.PERL, ".t": Language.PERL,
    ".lua": Language.LUA,
    ".r": Language.R,
    ".jl": Language.JULIA,
    ".tcl": Language.TCL,
    ".mat": Language.MATLAB,
    # Funcionais
    ".hs": Language.HASKELL, ".lhs": Language.HASKELL,
    ".ex": Language.ELIXIR, ".exs": Language.ELIXIR,
    ".erl": Language.ERLANG, ".hrl": Language.ERLANG,
    ".clj": Language.CLOJURE, ".cljs": Language.CLOJURE, ".cljc": Language.CLOJURE,
    ".ml": Language.OCAML, ".mli": Language.OCAML,
    ".lisp": Language.LISP, ".el": Language.LISP,
    ".scm": Language.SCHEME, ".ss": Language.SCHEME,
    ".nim": Language.NIM,
    ".zig": Language.ZIG,
    # Shell / Windows
    ".sh": Language.SHELL, ".bash": Language.SHELL, ".zsh": Language.SHELL,
    ".ksh": Language.SHELL, ".csh": Language.SHELL,
    ".ps1": Language.POWERSHELL, ".psm1": Language.POWERSHELL, ".psd1": Language.POWERSHELL,
    ".bat": Language.BATCH, ".cmd": Language.BATCH,
    # Front-end
    ".html": Language.HTML, ".htm": Language.HTML,
    ".css": Language.CSS, ".scss": Language.CSS, ".less": Language.CSS,
    ".vue": Language.VUE,
    ".svelte": Language.SVELTE,
    # Infra / esquemas
    ".tf": Language.TERRAFORM, ".tfvars": Language.TERRAFORM,
    ".proto": Language.PROTOBUF,
    ".graphql": Language.GRAPHQL, ".gql": Language.GRAPHQL,
    ".sol": Language.SOLIDITY,
    ".mk": Language.MAKE,
    ".gradle": Language.GRADLE,
    # Estruturados
    ".json": Language.JSON, ".jsonc": Language.JSON,
    ".yaml": Language.YAML, ".yml": Language.YAML,
    ".xml": Language.XML, ".xsd": Language.XML, ".xsl": Language.XML, ".wsdl": Language.XML,
    ".toml": Language.TOML,
}

#: Arquivos SEM extensão que são documento por convenção universal. Existem
#: para que `LICENSE`/`CHANGELOG` não entrem como código candidato — a regra
#: geral (texto não mapeado É código) precisa dessa exceção NOMEADA, não de um
#: filtro silencioso por heurística de conteúdo.
_EXTENSIONLESS_DOC_NAMES = {
    "license", "licence", "copying", "notice", "authors", "contributors",
    "changelog", "changes", "readme", "todo", "codeowners",
}


def _peek(snapshot: Snapshot, path: str, limit: int = 8192) -> bytes:
    full = os.path.join(snapshot.repo, *path.split("/"))
    try:
        with open(full, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def _has_null_byte(prefix: bytes) -> bool:
    return b"\x00" in prefix


def _shebang_language(prefix: bytes) -> Language | None:
    if not prefix.startswith(b"#!"):
        return None
    first_line = prefix.split(b"\n", 1)[0].decode("utf-8", "replace").lower()
    if "python" in first_line:
        return Language.PYTHON
    if "bash" in first_line or first_line.rstrip().endswith("sh"):
        return Language.SHELL
    return None


def _matches_migration_content(prefix: bytes) -> bool:
    return any(all(marker in prefix for marker in group) for group in _MIGRATION_CONTENT_MARKERS)


def _classify(snapshot: Snapshot, path: str) -> FileClassification:
    name = path.rsplit("/", 1)[-1]
    lower_name = name.lower()
    ext = os.path.splitext(name)[1].lower()
    prefix = _peek(snapshot, path)

    if ext in _DATA_EXTENSIONS:
        return FileClassification(path, FileClass.DATA, None, True, f"extensão de dado tabular/binário reconhecida ({ext})")

    if ext in _BINARY_EXTENSIONS or (prefix and _has_null_byte(prefix)):
        return FileClassification(path, FileClass.BINARY, None, True, "extensão binária conhecida ou byte nulo no conteúdo")

    if lower_name in _GENERATED_LOCK_NAMES or lower_name.endswith(_GENERATED_NAME_SUFFIXES) or any(m in prefix for m in _GENERATED_CONTENT_MARKERS):
        return FileClassification(path, FileClass.GENERATED, None, True, "lockfile/sufixo/marcador de geração automática reconhecido")

    if lower_name in _MANIFEST_NAMES or lower_name.endswith(_MANIFEST_SUFFIXES):
        return FileClassification(path, FileClass.MANIFEST, None, True, f"nome de manifesto de dependências reconhecido ({name})")

    if lower_name in _CONFIG_FILENAMES or ext in _CONFIG_EXTENSIONS or lower_name.startswith("docker-compose"):
        return FileClassification(path, FileClass.CONFIG, None, True, f"nome/extensão de configuração reconhecido ({name})")

    if any(p.match(name) for p in _TEST_NAME_PATTERNS) or any(m in prefix for m in _TEST_CONTENT_MARKERS):
        return FileClassification(path, FileClass.TEST, None, True, "nome de arquivo de teste ou marcador de framework de teste no conteúdo")

    if any(p.match(name) for p in _MIGRATION_NAME_PATTERNS) or _matches_migration_content(prefix):
        return FileClassification(path, FileClass.MIGRATION, None, True, "nome de migração de schema ou marcador de ferramenta de migração no conteúdo")

    if ext in _DOC_EXTENSIONS:
        return FileClassification(path, FileClass.DOC, None, False, f"extensão de documento/texto livre ({ext}) — não sustenta comportamento implementado (§5.4)")

    if ext in _LANGUAGE_BY_EXT:
        return FileClassification(path, FileClass.CODE, _LANGUAGE_BY_EXT[ext], True, f"extensão de linguagem/formato estruturado reconhecida ({ext})")

    if not ext:
        shebang_lang = _shebang_language(prefix)
        if shebang_lang is not None:
            return FileClassification(path, FileClass.CODE, shebang_lang, True, "shebang de interpretador reconhecido, sem extensão")
        if lower_name in _EXTENSIONLESS_DOC_NAMES:
            return FileClassification(
                path, FileClass.DOC, None, False,
                f"nome de documento por convenção sem extensão ({name}) — texto livre não "
                "sustenta comportamento implementado (§5.4)",
            )

    # Piso: texto não-binário que nenhuma extensão mapeou. NÃO é descartado —
    # entra como CÓDIGO com linguagem `unknown` e continua gerando `Limitation`
    # (o registro explícito de que a linguagem não foi reconhecida). É isto que
    # permite a `investigation.plan()` abrir objetivo de DESCOBERTA sobre ele:
    # o modelo lê o arquivo mesmo quando nenhum classificador o entende.
    return FileClassification(
        path, FileClass.CODE, Language.UNKNOWN, True,
        f"extensão/conteúdo não reconhecidos por nenhum classificador (ext={ext or '(nenhuma)'}): "
        "admitido como código de linguagem 'unknown' e encaminhado a objetivo de descoberta; "
        "a limitação é registrada, o arquivo não é ignorado (§6.2)",
    )


# --------------------------------------------------------------------------
# Construção
# --------------------------------------------------------------------------


def build(
    snapshot: Snapshot,
    *,
    extra_excluded_dirs: Iterable[str] = (),
    exclude_paths: Iterable[str] = (),
) -> "Inventory":
    """Classifica todo arquivo do `snapshot` (exceto deletado — sem conteúdo
    a classificar) e registra exclusões/limitações explicitamente.

    Não existe piso de tamanho de diretório: um arquivo sozinho numa pasta
    própria entra normalmente, contanto que não esteja sob um diretório de
    `_EXCLUDED_DIR_NAMES`.

    `extra_excluded_dirs` (nomes de diretório) e `exclude_paths` (prefixos OU
    globs, mesma semântica de `snapshot.capture`) vêm do perfil de análise do
    sistema. Ambos compõem uma cópia LOCAL das exclusões — `_EXCLUDED_DIR_NAMES`
    permanece intocado — e cada acerto vira `Exclusion` com motivo e impacto,
    exatamente como as exclusões default: nada some sem registro (§6.1.3).
    """
    files: list[FileClassification] = []
    limitations: list[Limitation] = []
    exclusion_hits: dict[str, dict[str, object]] = {}

    extra_dirs = tuple(d.strip().strip("/") for d in extra_excluded_dirs if (d or "").strip())
    excluded_dirs: dict[str, tuple[str, str]] = dict(_EXCLUDED_DIR_NAMES)
    for name in extra_dirs:
        excluded_dirs.setdefault(name, _PROFILE_DIR_REASON)
    profile_dirs = frozenset(n for n in extra_dirs if n not in _EXCLUDED_DIR_NAMES)
    path_patterns = tuple(p for p in exclude_paths if (p or "").strip())
    path_hits: dict[str, int] = {}

    for entry in sorted(snapshot.files, key=lambda e: e.path):
        if entry.state is FileState.DELETED:
            continue  # sem conteúdo em disco: nada para classificar (não é exclusão)

        hit = _excluded_dir_hit(entry.path, excluded_dirs)
        if hit is not None:
            dirname, boundary = hit
            bucket = exclusion_hits.setdefault(
                boundary,
                {"dirname": dirname, "count": 0, "by_profile": dirname in profile_dirs},
            )
            bucket["count"] = int(bucket["count"]) + 1
            continue

        if path_patterns and _matches_any(entry.path, path_patterns):
            matched = next(
                p for p in path_patterns if _matches_any(entry.path, (p,))
            )
            path_hits[matched] = path_hits.get(matched, 0) + 1
            continue

        classification = _classify(snapshot, entry.path)
        files.append(classification)
        if classification.file_class is FileClass.UNSUPPORTED or (
            classification.language is Language.UNKNOWN
        ):
            # Linguagem não reconhecida continua sendo limitação DECLARADA
            # mesmo agora que o arquivo entra como código: o que mudou foi o
            # destino (descoberta em vez de descarte), não o registro.
            limitations.append(Limitation(path=entry.path, detalhe=classification.reason))

    exclusions_list = [
        Exclusion(
            path=boundary,
            motivo=excluded_dirs[str(info["dirname"])][0],
            impacto=(
                f"{info['count']} arquivo(s) fora da classificação — "
                f"{excluded_dirs[str(info['dirname'])][1]}"
            ),
        )
        for boundary, info in sorted(exclusion_hits.items())
    ]
    exclusions_list.extend(
        Exclusion(
            path=pattern,
            motivo=_PROFILE_PATH_REASON[0],
            impacto=f"{count} arquivo(s) fora da classificação — {_PROFILE_PATH_REASON[1]}",
        )
        for pattern, count in sorted(path_hits.items())
    )
    exclusions = tuple(exclusions_list)

    summary = _summarize(files, exclusions, limitations)
    return Inventory(
        snapshot_id=snapshot.snapshot_id,
        files=tuple(files),
        exclusions=exclusions,
        limitations=tuple(limitations),
        summary=summary,
    )


def _summarize(
    files: list[FileClassification], exclusions: tuple[Exclusion, ...], limitations: list[Limitation]
) -> dict[str, object]:
    by_class: dict[str, int] = {}
    by_language: dict[str, int] = {}
    for f in files:
        by_class[f.file_class.value] = by_class.get(f.file_class.value, 0) + 1
        if f.language is not None:
            by_language[f.language.value] = by_language.get(f.language.value, 0) + 1
    return {
        "total_files": len(files),
        "by_class": dict(sorted(by_class.items())),
        "by_language": dict(sorted(by_language.items())),
        "exclusions_count": len(exclusions),
        "exclusions": [{"path": e.path, "motivo": e.motivo, "impacto": e.impacto} for e in exclusions],
        "limitations_count": len(limitations),
        "limitations": [{"path": l.path, "detalhe": l.detalhe} for l in limitations],
    }
