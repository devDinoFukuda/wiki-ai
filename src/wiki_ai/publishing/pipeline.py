from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.repository import KnowledgeRepository

from .docx.errors import DocxError
from .docx.inspect import read_package
from .gate import PublishingViolation
from .gate import check as gate_check
from .manifest import (
    Artifact,
    ArtifactKind,
    Manifest,
    artifact_for_bytes,
    canonical_hash,
)
from .model import (
    GAPS_SECTION_TITLE,
    NarrativeDocument,
    PublicationPlan,
)
from .narrative import NarrativeBuilder, NarrativeEnricher
from .planner import plan as build_plan
from .release import (
    RELEASES_DIRNAME,
    STAGING_DIRNAME,
    Publication,
    current,
    promote,
    rollback,
    stage,
)
from .render_docx import docx_filename, render_document
from .validate import PackageViolation, validate_package

__all__ = [
    "PUBLICATION_PREFIX",
    "BlockReason",
    "PublicationBlocked",
    "PublicationOutcome",
    "QueryFactory",
    "Publisher",
    "docx_structure_problems",
]

PUBLICATION_PREFIX = "pub"
_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)

QueryFactory = Callable[[KnowledgeRepository], KnowledgeQuery]


class BlockReason(str, enum.Enum):
    EMPTY_PLAN = "semantic_plan_empty"
    DOCX_INVALID = "docx_structurally_invalid"
    DOCX_MISSING = "docx_missing"
    DIAGRAM_WITHOUT_TEXT = "diagram_without_textual_equivalent"
    MANIFEST_DIVERGES = "manifest_diverges_from_package"
    REQUIRED_DOCUMENT_MISSING = "required_document_missing"
    GATE_REFUSED = "publishing_gate_refused"


@dataclass(frozen=True)
class PublicationOutcome:
    publication_id: str
    artifacts: tuple[str, ...]
    manifest_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "artifacts": list(self.artifacts),
            "manifest_hash": self.manifest_hash,
        }


class PublicationBlocked(Exception):
    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(str(item) for item in reasons)
        super().__init__("; ".join(self.reasons) or "publicação bloqueada")


def docx_structure_problems(path: Path) -> list[str]:
    try:
        summary = read_package(path)
    except DocxError as exc:
        return [str(exc)]
    if summary.heading_count < 1:
        return [f"{path.name}: documento sem títulos"]
    if summary.paragraph_count < 1:
        return [f"{path.name}: documento sem parágrafos"]
    return []


def _snapshot_hash(knowledge: KnowledgeRepository, namespace: str) -> str:
    return canonical_hash(
        {
            "namespace": str(namespace),
            "revision": knowledge.head_revision_id() or "",
            "entities": knowledge.entity_count(),
            "relations": knowledge.relation_count(),
        }
    )


def _publication_id(snapshot_hash: str, plan_hash: str) -> str:
    digest = canonical_hash({"snapshot": snapshot_hash, "plan": plan_hash})
    return f"{PUBLICATION_PREFIX}-{digest[:24]}"


def _plan_hash(plan: PublicationPlan) -> str:
    return canonical_hash(
        {
            "documents": [
                {
                    "document_id": document.document_id,
                    "kind": document.kind.value,
                    "subject": document.subject_entity_id,
                    "sections": list(document.section_titles()),
                }
                for document in plan.documents
            ]
        }
    )


def _diagram_reasons(documents: Sequence[NarrativeDocument]) -> list[str]:
    found: list[str] = []
    for document in documents:
        for spec in document.diagrams:
            if not spec.textual_equivalent:
                found.append(
                    f"{BlockReason.DIAGRAM_WITHOUT_TEXT.value}: "
                    f"{document.document_id}/{spec.title}"
                )
    return found


def _section_reasons(documents: Sequence[NarrativeDocument]) -> list[str]:
    return [
        f"{BlockReason.REQUIRED_DOCUMENT_MISSING.value}: "
        f"{document.document_id} sem seção {GAPS_SECTION_TITLE!r}"
        for document in documents
        if GAPS_SECTION_TITLE not in document.section_titles()
    ]


class Publisher:
    def __init__(
        self,
        query_factory: QueryFactory = KnowledgeQuery,
        enricher: NarrativeEnricher | None = None,
    ) -> None:
        self._query_factory = query_factory
        self._enricher = enricher

    def run(
        self,
        knowledge: KnowledgeRepository,
        publications_dir: Path,
        namespace: str,
    ) -> PublicationOutcome:
        query = self._query_factory(knowledge)
        plan = build_plan(query, namespace)
        if plan.is_empty:
            raise PublicationBlocked((BlockReason.EMPTY_PLAN.value,))

        snapshot = _snapshot_hash(knowledge, namespace)
        publication_id = _publication_id(snapshot, _plan_hash(plan))
        existing = _existing(publications_dir, publication_id)
        if existing is not None:
            return PublicationOutcome(
                publication_id=existing.publication_id,
                artifacts=existing.manifest.relative_paths,
                manifest_hash=existing.manifest.manifest_hash,
            )

        builder = NarrativeBuilder(query, self._enricher)
        documents = tuple(builder.build(entry) for entry in plan.documents)
        reasons = _diagram_reasons(documents) + _section_reasons(documents)
        if reasons:
            raise PublicationBlocked(reasons)

        root = Path(publications_dir)
        workspace = root / STAGING_DIRNAME / f"{publication_id}.build"
        files, artifacts, render_reasons = self._render_all(documents, workspace)
        if render_reasons:
            _discard(workspace)
            raise PublicationBlocked(render_reasons)

        manifest = Manifest(
            publication_id=publication_id,
            revision=knowledge.head_revision_id() or "",
            created_at=_EPOCH,
            source_snapshot_hash=snapshot,
            artifacts=artifacts,
        )
        staged = stage(root, manifest, files)
        _discard(workspace)
        violations = validate_package(
            staged.directory, manifest, docx_validator=docx_structure_problems
        )
        if violations:
            raise PublicationBlocked(_violation_reasons(violations))
        breaches = gate_check(staged.directory, knowledge)
        if breaches:
            raise PublicationBlocked(_breach_reasons(breaches))
        published = promote(staged)
        return PublicationOutcome(
            publication_id=published.publication_id,
            artifacts=published.manifest.relative_paths,
            manifest_hash=published.manifest.manifest_hash,
        )

    def _render_all(
        self, documents: Sequence[NarrativeDocument], workspace: Path
    ) -> tuple[Mapping[str, bytes], tuple[Artifact, ...], list[str]]:
        workspace.mkdir(parents=True, exist_ok=True)
        files: dict[str, bytes] = {}
        artifacts: list[Artifact] = []
        reasons: list[str] = []
        for document in documents:
            filename = docx_filename(document)
            target = workspace / filename
            try:
                render_document(document, target)
            except DocxError as exc:
                reasons.append(f"{BlockReason.DOCX_INVALID.value}: {filename}: {exc}")
                continue
            problems = docx_structure_problems(target)
            if problems:
                reasons.extend(
                    f"{BlockReason.DOCX_INVALID.value}: {problem}"
                    for problem in problems
                )
                continue
            payload = target.read_bytes()
            files[filename] = payload
            artifacts.append(
                artifact_for_bytes(
                    filename, payload, kind=ArtifactKind.DOCX, required=True
                )
            )
        if not artifacts and not reasons:
            reasons.append(BlockReason.DOCX_MISSING.value)
        return files, tuple(artifacts), reasons


def _violation_reasons(violations: Sequence[PackageViolation]) -> list[str]:
    return [
        f"{BlockReason.MANIFEST_DIVERGES.value}: {item.code.value}: "
        f"{item.relative_path}: {item.message}"
        for item in violations
    ]


def _breach_reasons(breaches: Sequence[PublishingViolation]) -> list[str]:
    return [
        f"{BlockReason.GATE_REFUSED.value}: {item.rule.value}: "
        f"{item.relative_path}: {item.detail}"
        for item in breaches
    ]


def _existing(publications_dir: Path, publication_id: str) -> Publication | None:
    directory = Path(publications_dir) / RELEASES_DIRNAME / publication_id
    if not directory.is_dir():
        return None
    published = current(publications_dir)
    if published is not None and published.publication_id == publication_id:
        return published
    return rollback(publications_dir, publication_id)


def _discard(workspace: Path) -> None:
    if not workspace.is_dir():
        return
    for entry in sorted(workspace.rglob("*"), reverse=True):
        if entry.is_file():
            entry.unlink()
        else:
            entry.rmdir()
    workspace.rmdir()
