"""Adaptadores de extração estrutural por linguagem e formato (§6.2).

Uso típico::

    from analysis.extractors import default_registry

    registry = default_registry()
    result = registry.extract_all(inventory_files)   # paths, tuplas ou objetos
    result.assert_accounted()                        # nenhum arquivo sem destino
    for row in result.coverage_table():
        print(row["language"], row["resolution_level"], row["files_parsed"])

O que cada peça garante:

| Módulo | Garantia |
|---|---|
| `base` | Comentário/docstring não vira símbolo; heurística não vira `resolved`; localizador compatível com `knowledge.evidence` |
| `registry` | Nenhum arquivo sem destino; linguagem sem adaptador vira `Diagnostic("no_adapter")`; cobertura por linguagem com nível de resolução |
| `python_ext` | Único `syntactic`: resolve import e chamada contra o conjunto analisado |
| `javascript_ext` | Estrutural sem parser; sempre `heuristic` |
| `java_ext` | Estrutural sem parser; anotações viram entrada com framework anotado |
| `structured_ext` | JSON/YAML/XML/SQL/manifests; XML com DTD e entidade externa recusadas |

Somente stdlib. Nenhuma dependência nativa é exigida pelo empacotamento `.pyz`.
"""

from __future__ import annotations

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
    code_locator,
    normalize_files,
    snippet_hash,
)
from .java_ext import JavaExtractor
from .javascript_ext import JavaScriptExtractor, TypeScriptExtractor
from .python_ext import PythonExtractor
from .registry import (
    ExtractionResult,
    ExtractorRegistry,
    default_registry,
    guess_language,
)
from .structured_ext import (
    JsonExtractor,
    ManifestExtractor,
    SqlExtractor,
    XmlExtractor,
    YamlExtractor,
    parse_sql_ddl,
    parse_yaml_min,
)

__all__ = [
    "CodeExtractor",
    "ConfigItem",
    "DataEntity",
    "Diagnostic",
    "Entrypoint",
    "ExtractionError",
    "ExtractionResult",
    "ExtractorRegistry",
    "JavaExtractor",
    "JavaScriptExtractor",
    "JsonExtractor",
    "LanguageCoverage",
    "ManifestExtractor",
    "PythonExtractor",
    "Reference",
    "SourceFile",
    "SqlExtractor",
    "Symbol",
    "TypeScriptExtractor",
    "XmlExtractor",
    "YamlExtractor",
    "code_locator",
    "default_registry",
    "guess_language",
    "normalize_files",
    "parse_sql_ddl",
    "parse_yaml_min",
    "snippet_hash",
]
