"""Registro extensível de adaptadores e agregação da extração (§6.2).

O registro existe para que a resposta "essa linguagem não foi analisada" seja
**produzida**, e não deduzida por ausência. Três garantias, todas executáveis:

1. **Nenhum arquivo desaparece.** Todo arquivo do inventário termina em um dos
   três destinos: extraído por um adaptador, contabilizado como falha, ou
   listado num `Diagnostic("no_adapter")` com a linguagem, os caminhos
   afetados e o impacto. `ExtractionResult.assert_accounted()` recalcula essa
   conta e levanta erro se algum arquivo sumir.
2. **Cobertura é declarada por linguagem, com nível de resolução.**
   `coverage[lang].resolution_level` diz se aquilo veio de parser
   (`syntactic`), de varredura (`heuristic`) ou de nada (`none`). Não existe
   selo global de "análise completa": `ExtractionResult.complete_languages`
   lista só quem parseou tudo com parser de verdade.
3. **Exceção de adaptador não derruba a execução nem some.** Cada operação por
   arquivo roda protegida; a falha vira `Diagnostic("extractor_error")` e o
   arquivo entra em `files_failed`.

Linguagem é inferida por extensão (`LANGUAGE_BY_EXTENSION`) mesmo quando não há
adaptador — é isso que permite dizer "há 12 arquivos Go sem adaptador" em vez
de "há 12 arquivos desconhecidos".
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from .base import (
    CodeExtractor,
    ConfigItem,
    DataEntity,
    Diagnostic,
    Entrypoint,
    ExtractionError,
    LanguageCoverage,
    Reference,
    SourceFile,
    Symbol,
    normalize_files,
)

#: Extensão -> linguagem. Serve para nomear a limitação de quem não tem
#: adaptador; não implica que a linguagem seja analisável.
LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".pyx": "cython",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".groovy": "groovy",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".cs": "csharp",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cxx": "cpp",
    ".swift": "swift", ".m": "objc", ".mm": "objc", ".dart": "dart", ".ex": "elixir",
    ".exs": "elixir", ".erl": "erlang", ".clj": "clojure", ".lua": "lua", ".pl": "perl",
    ".r": "r", ".jl": "julia", ".hs": "haskell", ".vb": "vbnet", ".fs": "fsharp",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell", ".bat": "batch",
    ".sql": "sql", ".ddl": "sql", ".plsql": "sql",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini",
    ".xml": "xml", ".xsd": "xml", ".xsl": "xml", ".pom": "xml",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "css", ".less": "css",
    ".vue": "vue", ".svelte": "svelte", ".tf": "terraform", ".proto": "protobuf",
    ".graphql": "graphql", ".gql": "graphql", ".md": "markdown", ".rst": "restructuredtext",
    ".txt": "text", ".csv": "csv", ".env": "dotenv", ".properties": "properties",
    ".dockerfile": "dockerfile", ".mk": "make", ".gradle": "gradle", ".cfg": "ini",
}

#: Basenames sem extensão útil.
LANGUAGE_BY_BASENAME: dict[str, str] = {
    "Dockerfile": "dockerfile",
    "Makefile": "make",
    "Jenkinsfile": "groovy",
    "Pipfile": "toml",
    "requirements.txt": "text",
    "go.mod": "go",
    "Gemfile": "ruby",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
}


def guess_language(path: str) -> str:
    base = os.path.basename(path)
    if base in LANGUAGE_BY_BASENAME:
        return LANGUAGE_BY_BASENAME[base]
    if base.lower().startswith("dockerfile"):
        return "dockerfile"
    ext = os.path.splitext(path)[1].lower()
    return LANGUAGE_BY_EXTENSION.get(ext, "unknown")


def _is_binary(content: str) -> bool:
    return "\x00" in content[:4096]


@dataclass
class ExtractionResult:
    """Agregação por linguagem do que os adaptadores produziram."""

    symbols: list[Symbol] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    entrypoints: list[Entrypoint] = field(default_factory=list)
    configuration: list[ConfigItem] = field(default_factory=list)
    data_entities: list[DataEntity] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    coverage: dict[str, LanguageCoverage] = field(default_factory=dict)
    files_by_language: dict[str, tuple[str, ...]] = field(default_factory=dict)
    files_total: int = 0

    # -- leitura -----------------------------------------------------------
    @property
    def languages_without_adapter(self) -> list[str]:
        return sorted(
            lang for lang, cov in self.coverage.items() if cov.resolution_level == "none"
        )

    @property
    def complete_languages(self) -> list[str]:
        """Linguagens com todos os arquivos parseados por parser da gramática."""
        return sorted(lang for lang, cov in self.coverage.items() if cov.complete)

    @property
    def resolved_references(self) -> list[Reference]:
        return [r for r in self.references if r.resolved]

    def diagnostics_by_code(self, code: str) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.code == code]

    def coverage_table(self) -> list[dict[str, Any]]:
        return [self.coverage[k].as_dict() for k in sorted(self.coverage)]

    def assert_accounted(self) -> None:
        """Verifica que a soma da cobertura devolve o total de arquivos.

        É a checagem que impede o silêncio: se um arquivo não foi extraído, não
        falhou e não está numa limitação declarada, esta chamada estoura.
        """
        counted = sum(c.files_total for c in self.coverage.values())
        if counted != self.files_total:
            raise ExtractionError(
                f"cobertura não fecha: {counted} arquivo(s) na matriz para "
                f"{self.files_total} no inventário — há arquivo sem destino declarado"
            )
        for lang, cov in self.coverage.items():
            if cov.extractor:
                # Há adaptador: falha de arquivo precisa de diagnóstico próprio.
                if cov.files_failed and not any(
                    d.level == "error" and d.language == lang for d in self.diagnostics
                ):
                    raise ExtractionError(
                        f"linguagem {lang!r} com {cov.files_failed} arquivo(s) falhos e nenhum "
                        "Diagnostic de erro: falha silenciosa (§6.1.3)"
                    )
                continue
            if not any(d.code == "no_adapter" and d.language == lang for d in self.diagnostics):
                raise ExtractionError(
                    f"linguagem {lang!r} sem adaptador e sem Diagnostic('no_adapter'): "
                    "exclusão silenciosa (§6.1.3)"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_total": self.files_total,
            "symbols": len(self.symbols),
            "references": len(self.references),
            "references_resolved": len(self.resolved_references),
            "entrypoints": len(self.entrypoints),
            "configuration": len(self.configuration),
            "data_entities": len(self.data_entities),
            "diagnostics": [
                {
                    "level": d.level,
                    "code": d.code,
                    "language": d.language,
                    "message": d.message,
                    "scope_examined": d.scope_examined,
                    "paths": list(d.paths) or ([d.path] if d.path else []),
                    "impact": d.impact,
                }
                for d in self.diagnostics
            ],
            "coverage": self.coverage_table(),
        }


class ExtractorRegistry:
    """Registro de adaptadores. Primeiro registrado que reivindicar o arquivo vence."""

    def __init__(self) -> None:
        self._extractors: list[CodeExtractor] = []

    # -- registro ----------------------------------------------------------
    def register(self, extractor: CodeExtractor) -> CodeExtractor:
        if not isinstance(extractor, CodeExtractor):
            raise ExtractionError(
                f"adaptador deve implementar CodeExtractor, recebido {type(extractor).__name__}"
            )
        if not extractor.language:
            raise ExtractionError("adaptador sem atributo `language` não pode ser registrado")
        self._extractors.append(extractor)
        return extractor

    def unregister(self, language: str) -> int:
        before = len(self._extractors)
        self._extractors = [e for e in self._extractors if e.language != language]
        return before - len(self._extractors)

    @property
    def extractors(self) -> list[CodeExtractor]:
        return list(self._extractors)

    def languages(self) -> list[str]:
        return sorted({e.language for e in self._extractors})

    def for_language(self, language: str) -> CodeExtractor | None:
        """Adaptador registrado para a linguagem, ou `None` (o chamador declara a lacuna)."""
        for extractor in self._extractors:
            if extractor.language == language:
                return extractor
        return None

    def for_path(self, path: str, content: str = "") -> CodeExtractor | None:
        for extractor in self._extractors:
            try:
                if extractor.detect(path, content):
                    return extractor
            except Exception:  # adaptador de terceiro não pode derrubar o registro
                continue
        return None

    # -- execução ----------------------------------------------------------
    def extract_all(self, inventory_files: Iterable[Any]) -> ExtractionResult:
        """Roda todos os adaptadores sobre o inventário e agrega o resultado."""
        sources = normalize_files(inventory_files)
        result = ExtractionResult(files_total=len(sources))

        assigned: dict[int, list[SourceFile]] = {}
        unassigned: list[SourceFile] = []
        binaries: list[SourceFile] = []
        for src in sources:
            if _is_binary(src.content):
                binaries.append(src)
                continue
            extractor = self.for_path(src.path, src.content)
            if extractor is None:
                unassigned.append(src)
            else:
                assigned.setdefault(id(extractor), []).append(src)

        by_id = {id(e): e for e in self._extractors}
        for ext_id, files in assigned.items():
            extractor = by_id[ext_id]
            self._run_extractor(extractor, files, result)

        self._declare_gaps(unassigned, result)
        self._declare_binaries(binaries, result)
        result.assert_accounted()
        return result

    # -- internos ----------------------------------------------------------
    def _run_extractor(
        self, extractor: CodeExtractor, files: list[SourceFile], result: ExtractionResult
    ) -> None:
        try:
            extractor.inventory(files)
        except Exception as exc:
            result.diagnostics.append(
                Diagnostic(
                    level="error",
                    code="extractor_inventory_failed",
                    message=f"{type(extractor).__name__}.inventory falhou: {type(exc).__name__}: {exc}",
                    scope_examined=f"{len(files)} arquivo(s) reivindicados por {extractor.language}",
                    paths=tuple(f.path for f in files),
                    language=extractor.language,
                    impact="resolução cruzada indisponível; itens deste adaptador podem sair sem alvo",
                )
            )

        parsed: set[str] = set()
        failed: set[str] = set()
        for src in files:
            ok = True
            for op, sink in (
                ("symbols", result.symbols),
                ("references", result.references),
                ("entrypoints", result.entrypoints),
                ("configuration", result.configuration),
                ("data_entities", result.data_entities),
            ):
                try:
                    sink.extend(getattr(extractor, op)(src.path, src.content))
                except Exception as exc:
                    ok = False
                    result.diagnostics.append(
                        Diagnostic(
                            level="error",
                            code="extractor_error",
                            message=(
                                f"{type(extractor).__name__}.{op} falhou: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                            scope_examined=f"{op}() sobre {src.path}",
                            path=src.path,
                            paths=(src.path,),
                            language=extractor.language,
                            impact=f"{op} deste arquivo ausente do conhecimento",
                        )
                    )
            (parsed if ok else failed).add(src.path)

        diags = []
        try:
            diags = extractor.diagnostics()
        except Exception as exc:  # pragma: no cover
            result.diagnostics.append(
                Diagnostic(
                    level="error",
                    code="extractor_diagnostics_failed",
                    message=f"{type(extractor).__name__}.diagnostics falhou: {exc}",
                    scope_examined=f"diagnostics() de {extractor.language}",
                    language=extractor.language,
                    impact="limitações deste adaptador não puderam ser declaradas",
                )
            )
        result.diagnostics.extend(diags)

        # Arquivo com erro de parse declarado pelo próprio adaptador conta como falha.
        for diag in diags:
            if diag.level == "error":
                for p in diag.paths or ((diag.path,) if diag.path else ()):
                    if p in parsed:
                        parsed.discard(p)
                        failed.add(p)

        lang = extractor.language
        previous = result.coverage.get(lang)
        total = len(files) + (previous.files_total if previous else 0)
        result.coverage[lang] = LanguageCoverage(
            language=lang,
            files_total=total,
            files_parsed=len(parsed) + (previous.files_parsed if previous else 0),
            files_failed=len(failed) + (previous.files_failed if previous else 0),
            resolution_level=extractor.resolution_level if parsed else "none",
            extractor=type(extractor).__name__,
        )
        result.files_by_language[lang] = tuple(
            list(result.files_by_language.get(lang, ())) + [f.path for f in files]
        )

    def _declare_gaps(self, unassigned: list[SourceFile], result: ExtractionResult) -> None:
        """Linguagem sem adaptador vira limitação nomeada — nunca silêncio (§6.2)."""
        grouped: dict[str, list[str]] = {}
        for src in unassigned:
            grouped.setdefault(guess_language(src.path), []).append(src.path)
        for lang, paths in sorted(grouped.items()):
            result.coverage[lang] = LanguageCoverage(
                language=lang,
                files_total=len(paths),
                files_parsed=0,
                files_failed=0,
                resolution_level="none",
                extractor="",
            )
            result.files_by_language[lang] = tuple(paths)
            result.diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="no_adapter",
                    message=(
                        f"{len(paths)} arquivo(s) de linguagem/formato {lang!r} sem adaptador "
                        f"registrado. Adaptadores disponíveis: {', '.join(self.languages()) or '(nenhum)'}. "
                        "Nenhum fallback por regex foi aplicado: resultado por regex não seria "
                        "resolução e não pode virar selo de cobertura (§6.2)."
                    ),
                    scope_examined=(
                        f"detect() de {len(self._extractors)} adaptador(es) sobre {len(paths)} arquivo(s)"
                    ),
                    paths=tuple(sorted(paths)),
                    language=lang,
                    impact=(
                        "símbolos, chamadas, entradas e configuração desses arquivos estão ausentes; "
                        "qualquer afirmação sobre esse componente carece de evidência estrutural e "
                        "exige tarefa de investigação ou novo adaptador antes de declarar cobertura"
                    ),
                )
            )

    def _declare_binaries(self, binaries: list[SourceFile], result: ExtractionResult) -> None:
        if not binaries:
            return
        paths = [b.path for b in binaries]
        result.coverage["binary"] = LanguageCoverage(
            language="binary",
            files_total=len(paths),
            files_parsed=0,
            files_failed=0,
            resolution_level="none",
            extractor="",
        )
        result.files_by_language["binary"] = tuple(paths)
        result.diagnostics.append(
            Diagnostic(
                level="info",
                code="no_adapter",
                message=(
                    f"{len(paths)} arquivo(s) com conteúdo binário (byte NUL nos primeiros 4 KiB): "
                    "não há extração estrutural de texto aplicável"
                ),
                scope_examined="verificação de byte NUL no início do conteúdo",
                paths=tuple(sorted(paths)),
                language="binary",
                impact="conteúdo não analisado; se for artefato relevante, exige tratamento próprio",
            )
        )


#: Variável de ambiente com extensões de extrator, no MESMO formato de
#: `WIKI_AI_AGENT_ADAPTERS` (`runtime/agents.py`): specs `pacote.modulo:fabrica`
#: separados por `os.pathsep`. A fábrica devolve um extrator ou uma sequência.
EXTRACTOR_EXTENSIONS_ENV = "WIKI_AI_EXTRACTORS"


class ExtractorExtensionError(ExtractionError):
    """Extensão de extrator inválida: spec malformado, import falho ou
    adaptador recusado pelo registro. Sempre nomeia o spec problemático —
    registrar pela metade em silêncio seria pior do que não registrar."""


def load_extractor_extensions(
    registry: "ExtractorRegistry",
    specs: Iterable[str] | None = None,
    *,
    env_var: str = EXTRACTOR_EXTENSIONS_ENV,
) -> list[Any]:
    """Registra extratores de terceiros por `pacote.modulo:fabrica`.

    `specs=None` lê `env_var` do ambiente (separador `os.pathsep`), igual a
    `AgentRegistry.load_extensions`. Qualquer falha vira
    `ExtractorExtensionError` COM o spec — nunca um registro parcial mudo.
    """
    raw = (
        list(specs)
        if specs is not None
        else [s.strip() for s in os.environ.get(env_var, "").split(os.pathsep) if s.strip()]
    )
    registered: list[Any] = []
    for spec in raw:
        spec = spec.strip()
        if not spec:
            continue
        if ":" not in spec:
            raise ExtractorExtensionError(
                f"extensão de extrator inválida {spec!r}: use 'pacote.modulo:fabrica'"
            )
        module_name, _, factory_name = spec.partition(":")
        if not module_name or not factory_name:
            raise ExtractorExtensionError(
                f"extensão de extrator inválida {spec!r}: use 'pacote.modulo:fabrica'"
            )
        try:
            module = importlib.import_module(module_name)
            factory = getattr(module, factory_name)
        except Exception as exc:
            raise ExtractorExtensionError(
                f"extensão de extrator {spec!r} não carregou: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            produced = factory()
        except Exception as exc:
            raise ExtractorExtensionError(
                f"fábrica da extensão {spec!r} falhou: {type(exc).__name__}: {exc}"
            ) from exc
        candidates = produced if isinstance(produced, (list, tuple)) else [produced]
        for extractor in candidates:
            try:
                registered.append(registry.register(extractor))
            except ExtractionError as exc:
                raise ExtractorExtensionError(
                    f"extensão de extrator {spec!r} devolveu adaptador recusado: {exc}"
                ) from exc
    return registered


def default_registry(*, extensions: Iterable[str] = ()) -> ExtractorRegistry:
    """Registro com os adaptadores da primeira leva (§6.2).

    A ordem importa: `TypeScriptExtractor` antes de `JavaScriptExtractor`
    porque ambos reivindicam `.ts`/`.tsx`, e `ManifestExtractor` antes dos
    demais para capturar `pyproject.toml`/`build.gradle` por basename.

    `extensions` (ou, quando vazio, a variável de ambiente
    `WIKI_AI_EXTRACTORS`) acrescenta extratores de terceiros DEPOIS dos nove
    embutidos — a precedência dos embutidos é preservada, e um extrator novo só
    ganha um arquivo que nenhum deles reivindicou. Linguagem sem adaptador
    continua produzindo o `Diagnostic('no_adapter')` de `extract_all`: uma
    extensão que não cobre a linguagem não apaga a lacuna.
    """
    from .java_ext import JavaExtractor
    from .javascript_ext import JavaScriptExtractor, TypeScriptExtractor
    from .python_ext import PythonExtractor
    from .structured_ext import (
        JsonExtractor,
        ManifestExtractor,
        SqlExtractor,
        XmlExtractor,
        YamlExtractor,
    )

    registry = ExtractorRegistry()
    registry.register(PythonExtractor())
    registry.register(TypeScriptExtractor())
    registry.register(JavaScriptExtractor())
    registry.register(JavaExtractor())
    registry.register(ManifestExtractor())
    registry.register(JsonExtractor())
    registry.register(YamlExtractor())
    registry.register(XmlExtractor())
    registry.register(SqlExtractor())
    specs = list(extensions)
    load_extractor_extensions(registry, specs if specs else None)
    return registry


__all__ = [
    "EXTRACTOR_EXTENSIONS_ENV",
    "LANGUAGE_BY_BASENAME",
    "LANGUAGE_BY_EXTENSION",
    "ExtractionResult",
    "ExtractorExtensionError",
    "ExtractorRegistry",
    "default_registry",
    "guess_language",
    "load_extractor_extensions",
]
