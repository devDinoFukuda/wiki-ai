from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from wiki_ai.knowledge.gaps import GAP_QUESTION, blocking_gaps
from wiki_ai.knowledge.model import Entity, GraphPolicy
from wiki_ai.knowledge.repository import KnowledgeRepository
from wiki_ai.knowledge.taxonomy import RelationKind

from .docx.errors import DocxError
from .docx.inspect import read_package
from .manifest import MANIFEST_FILENAME, ArtifactKind, ManifestInvalid, Manifest
from .model import GAPS_SECTION_TITLE
from .release import read_manifest, ReleaseError
from .validate import validate_package

__all__ = [
    "PublishingRule",
    "PublishingViolation",
    "check",
]


class PublishingRule(str, enum.Enum):
    MANIFEST_UNREADABLE = "manifest_unreadable"
    DOCX_MISSING = "docx_missing"
    DOCX_INVALID = "docx_invalid"
    MANIFEST_DIVERGES = "manifest_diverges"
    REQUIRED_ARTIFACT_MISSING = "required_artifact_missing"
    DIAGRAM_IMAGE_WITHOUT_TEXT = "diagram_image_without_text"
    COMPLETENESS_HIDES_BLOCKING_GAPS = "completeness_hides_blocking_gaps"


@dataclass(frozen=True)
class PublishingViolation:
    rule: PublishingRule
    relative_path: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule.value,
            "relative_path": self.relative_path,
            "detail": self.detail,
        }


def _docx_problems(path: Path) -> list[str]:
    try:
        summary = read_package(path)
    except DocxError as exc:
        return [str(exc)]
    if summary.heading_count < 1:
        return [f"{path.name}: documento sem títulos"]
    return []


_PACKAGE_RULES: dict[str, PublishingRule] = {
    "missing_required_artifact": PublishingRule.REQUIRED_ARTIFACT_MISSING,
    "missing_required_docx": PublishingRule.DOCX_MISSING,
    "diagram_image_without_text": PublishingRule.DIAGRAM_IMAGE_WITHOUT_TEXT,
    "docx_structure": PublishingRule.DOCX_INVALID,
}


def _package_violations(
    release_dir: Path, manifest: Manifest
) -> list[PublishingViolation]:
    found: list[PublishingViolation] = []
    for item in validate_package(
        release_dir, manifest, docx_validator=_docx_problems
    ):
        rule = _PACKAGE_RULES.get(item.code.value, PublishingRule.MANIFEST_DIVERGES)
        found.append(
            PublishingViolation(
                rule=rule, relative_path=item.relative_path, detail=item.message
            )
        )
    return found


def _subjects_of(knowledge: KnowledgeRepository, gap: Entity) -> tuple[str, ...]:
    names: list[str] = []
    for relation in knowledge.relations_of(
        gap.id,
        "out",
        (RelationKind.AFFECTS.value,),
        policy=GraphPolicy.ALL,
    ):
        subject = knowledge.get_entity(relation.target_id)
        if subject is not None:
            names.append(subject.name)
    return tuple(names)


def _concerns(document_subject: str, subjects: Sequence[str]) -> bool:
    if not subjects:
        return True
    declared = document_subject.strip().lower()
    return any(name.strip().lower() == declared for name in subjects)


def _mentioned(text: str, gap: Entity) -> bool:
    question = str(gap.attributes.get(GAP_QUESTION, gap.name)).strip().lower()
    return bool(question) and question in text.lower()


def _completeness_violations(
    release_dir: Path, manifest: Manifest, knowledge: KnowledgeRepository
) -> list[PublishingViolation]:
    gaps = blocking_gaps(knowledge)
    if not gaps:
        return []
    scoped = tuple((gap, _subjects_of(knowledge, gap)) for gap in gaps)
    found: list[PublishingViolation] = []
    for artifact in manifest.of_kind(ArtifactKind.DOCX):
        path = release_dir / artifact.relative_path
        if not path.is_file():
            continue
        try:
            summary = read_package(path)
        except DocxError:
            continue
        relevant = tuple(
            gap
            for gap, subjects in scoped
            if _concerns(summary.properties.subject, subjects)
        )
        if not relevant:
            continue
        if GAPS_SECTION_TITLE not in summary.text:
            found.append(
                PublishingViolation(
                    rule=PublishingRule.COMPLETENESS_HIDES_BLOCKING_GAPS,
                    relative_path=artifact.relative_path,
                    detail=(
                        f"documento sem seção {GAPS_SECTION_TITLE!r} enquanto existem "
                        f"{len(relevant)} lacuna(s) bloqueante(s) sobre o assunto"
                    ),
                )
            )
            continue
        hidden = tuple(gap for gap in relevant if not _mentioned(summary.text, gap))
        if hidden:
            found.append(
                PublishingViolation(
                    rule=PublishingRule.COMPLETENESS_HIDES_BLOCKING_GAPS,
                    relative_path=artifact.relative_path,
                    detail=(
                        "lacuna bloqueante sobre o assunto ausente do texto: "
                        + "; ".join(
                            str(gap.attributes.get(GAP_QUESTION, gap.name))
                            for gap in hidden
                        )
                    ),
                )
            )
    return found


def check(
    release_dir: Path | str, knowledge: KnowledgeRepository | None = None
) -> tuple[PublishingViolation, ...]:
    root = Path(release_dir)
    try:
        manifest = read_manifest(root)
    except (ReleaseError, ManifestInvalid) as exc:
        return (
            PublishingViolation(
                rule=PublishingRule.MANIFEST_UNREADABLE,
                relative_path=MANIFEST_FILENAME,
                detail=str(exc),
            ),
        )
    found = _package_violations(root, manifest)
    if knowledge is not None:
        found.extend(_completeness_violations(root, manifest, knowledge))
    return tuple(
        sorted(found, key=lambda item: (item.relative_path, item.rule.value, item.detail))
    )
