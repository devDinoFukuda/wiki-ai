from __future__ import annotations

from typing import Mapping

from wiki_ai.knowledge import (
    CodeContent,
    CodeLocator,
    Confidence,
    Relation,
    make_evidence,
)
from wiki_ai.knowledge.repository import KnowledgeRepository

CAPTURED = "2026-01-01T00:00:00+00:00"


def _relation_evidence(repository: KnowledgeRepository, relation: Relation):
    version = repository.source_versions()[0]
    locator = CodeLocator(
        path=f"src/{relation.kind}/{relation.id[:12]}.py",
        line_start=1,
        line_end=20,
        symbol=relation.kind,
        content=CodeContent.EXECUTABLE,
    )
    excerpt = (
        f"def {relation.kind}(self, target):\n"
        f"    return self.{relation.kind}_target.handle(target)\n"
    )
    return make_evidence(
        version.source_id, version.version_hash, locator, excerpt, CAPTURED
    )


def promote_relations(
    repository: KnowledgeRepository,
    overrides: Mapping[tuple[str, str, str], Confidence] | None = None,
) -> dict[tuple[str, str, str], Relation]:
    chosen = dict(overrides or {})
    rebuilt: dict[tuple[str, str, str], Relation] = {}
    with repository.begin_revision("pipeline", "confianca das relacoes") as revision:
        for relation in repository.find_relations():
            key = (
                relation.kind,
                relation.source_id.value,
                relation.target_id.value,
            )
            wanted = chosen.get(key, Confidence.SUPPORTED)
            updated = relation.with_confidence(wanted)
            revision.put_relation(updated)
            if wanted is Confidence.SUPPORTED:
                revision.put_evidence(
                    _relation_evidence(repository, updated),
                    (),
                    (updated.id,),
                )
            rebuilt[key] = updated
    return rebuilt


def set_relation_confidence(
    repository: KnowledgeRepository,
    kind: str,
    source_id: str,
    target_id: str,
    confidence: Confidence,
) -> Relation:
    for relation in repository.find_relations(kind):
        if (
            relation.source_id.value == source_id
            and relation.target_id.value == target_id
        ):
            updated = relation.with_confidence(confidence)
            with repository.begin_revision("pipeline", "ajusta confianca") as revision:
                revision.put_relation(updated)
                if confidence is Confidence.SUPPORTED:
                    revision.put_evidence(
                        _relation_evidence(repository, updated), (), (updated.id,)
                    )
            return updated
    raise AssertionError(f"relacao ausente: {kind} {source_id} -> {target_id}")
