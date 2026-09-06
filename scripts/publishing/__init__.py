"""Publicação orientada ao consumo (plano §10, onda W6).

Uma representação intermediária, dois renderizadores independentes:

    from publishing.planner import plan
    from publishing.markdown import render, render_manifest, render_plan

    p = plan(repo, revision_id, namespace="acme/pagamentos")
    arquivos = render_plan(p)          # caminho -> Markdown
    manifesto = render_manifest(p)     # unit_id -> localização/estado/fatos

`document.py` é a fonte comum (§10.1): Markdown e Word projetam a MESMA
`KnowledgeDocument`. Conversão de um formato no outro é proibida pelo plano,
então nenhum renderizador importa o outro.

Este pacote depende apenas de stdlib e de `knowledge`. `word.py`,
`validate.py` e `release.py` são módulos irmãos com donos próprios e não são
importados aqui: um import de conveniência no `__init__` faria `publishing`
inteiro falhar quando qualquer módulo irmão estiver em manutenção.
"""

from .document import (
    Belonging,
    CrossRef,
    DocKind,
    EvidenceRef,
    KnowledgeDocument,
    PublishingError,
    RelationRef,
    RevisionScope,
    SemanticUnit,
    Statement,
    UnitState,
)
from .markdown import (
    document_path,
    render,
    render_manifest,
    render_manifest_envelope,
    render_plan,
)
from .planner import PublicationPlan, SkippedItem, plan

__all__ = [
    "Belonging",
    "CrossRef",
    "DocKind",
    "EvidenceRef",
    "KnowledgeDocument",
    "PublicationPlan",
    "PublishingError",
    "RelationRef",
    "RevisionScope",
    "SemanticUnit",
    "SkippedItem",
    "Statement",
    "UnitState",
    "document_path",
    "plan",
    "render",
    "render_manifest",
    "render_manifest_envelope",
    "render_plan",
]
