from __future__ import annotations

from enum import Enum

__all__ = [
    "IngestionStatus",
    "StructuralFault",
    "SemanticFault",
    "Gap",
    "BLOCKING_FAULTS",
    "PARTIAL_GAPS",
]


class IngestionStatus(str, Enum):
    COMPLETE = "complete"
    STRUCTURAL_ONLY = "structural_only"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class StructuralFault(str, Enum):
    UNSUPPORTED_FORMAT = "unsupported_format"
    UNREADABLE_SOURCE = "unreadable_source"
    MALFORMED_DOCUMENT = "malformed_document"
    PDF_LIBRARY_UNAVAILABLE = "pdf_library_unavailable"
    LOCATOR_INCOMPLETE = "locator_incomplete"


class SemanticFault(str, Enum):
    PROVIDER_UNAVAILABLE = "agent_provider_unavailable"
    PROVIDER_RUN_ABORTED = "agent_run_aborted"
    BUDGET_EXHAUSTED = "semantic_investigation_budget_exhausted"
    IDENTITY_COLLISION = "identity_collision"
    VERSION_HASH_MISMATCH = "version_hash_mismatch"


class Gap(str, Enum):
    IMAGE_CONTENT_NOT_INTERPRETED = "image_content_not_interpreted"
    OCR_UNAVAILABLE = "ocr_unavailable"
    NO_TEXT_LAYER = "no_text_layer"


BLOCKING_FAULTS: frozenset[StructuralFault] = frozenset(
    {
        StructuralFault.UNSUPPORTED_FORMAT,
        StructuralFault.UNREADABLE_SOURCE,
        StructuralFault.PDF_LIBRARY_UNAVAILABLE,
    }
)

PARTIAL_GAPS: frozenset[Gap] = frozenset(
    {
        Gap.IMAGE_CONTENT_NOT_INTERPRETED,
        Gap.OCR_UNAVAILABLE,
        Gap.NO_TEXT_LAYER,
    }
)
