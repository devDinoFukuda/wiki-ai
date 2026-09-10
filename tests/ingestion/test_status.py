from __future__ import annotations

import ast
from pathlib import Path

import pytest

from wiki_ai.ingestion.integration import IngestionEngine, derive_status
from wiki_ai.ingestion.outcome import (
    BLOCKING_FAULTS,
    Gap,
    IngestionStatus,
    PARTIAL_GAPS,
    SemanticFault,
    StructuralFault,
)
from wiki_ai.ingestion.pipeline import IngestStatus
from wiki_ai.knowledge.repository import KnowledgeRepository

from tests.ingestion import fixtures
from tests.ingestion.fake_provider import FakeProvider, Script

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "wiki_ai" / "ingestion"


@pytest.fixture()
def knowledge(tmp_path: Path) -> KnowledgeRepository:
    repository = KnowledgeRepository.open(str(tmp_path / "knowledge.sqlite3"))
    yield repository
    repository.close()


def test_the_status_enum_is_the_contract_the_application_consumes() -> None:
    assert [item.value for item in IngestionStatus] == [
        "complete",
        "structural_only",
        "partial",
        "blocked",
        "failed",
    ]


def test_a_missing_provider_is_structural_only_not_complete() -> None:
    status, reason = derive_status(faults=(), gaps=(), provider_present=False)
    assert status is IngestionStatus.STRUCTURAL_ONLY
    assert reason == SemanticFault.PROVIDER_UNAVAILABLE.value


def test_an_image_gap_with_a_provider_is_partial() -> None:
    status, reason = derive_status(
        faults=(), gaps=(Gap.IMAGE_CONTENT_NOT_INTERPRETED,), provider_present=True
    )
    assert status is IngestionStatus.PARTIAL
    assert reason == Gap.IMAGE_CONTENT_NOT_INTERPRETED.value


def test_a_missing_library_blocks() -> None:
    status, reason = derive_status(
        faults=(StructuralFault.PDF_LIBRARY_UNAVAILABLE,),
        gaps=(),
        provider_present=True,
    )
    assert status is IngestionStatus.BLOCKED
    assert reason == StructuralFault.PDF_LIBRARY_UNAVAILABLE.value


def test_an_unsupported_format_blocks() -> None:
    status, reason = derive_status(
        faults=(StructuralFault.UNSUPPORTED_FORMAT,), gaps=(), provider_present=True
    )
    assert status is IngestionStatus.BLOCKED
    assert reason == StructuralFault.UNSUPPORTED_FORMAT.value


def test_a_malformed_document_fails() -> None:
    status, reason = derive_status(
        faults=(StructuralFault.MALFORMED_DOCUMENT,), gaps=(), provider_present=True
    )
    assert status is IngestionStatus.FAILED
    assert reason == StructuralFault.MALFORMED_DOCUMENT.value


def test_an_aborted_provider_run_fails() -> None:
    status, reason = derive_status(
        faults=(),
        gaps=(),
        provider_present=True,
        semantic_faults=(SemanticFault.PROVIDER_RUN_ABORTED,),
    )
    assert status is IngestionStatus.FAILED
    assert reason == SemanticFault.PROVIDER_RUN_ABORTED.value


def test_an_exhausted_budget_is_partial() -> None:
    status, reason = derive_status(
        faults=(),
        gaps=(),
        provider_present=True,
        semantic_faults=(SemanticFault.BUDGET_EXHAUSTED,),
    )
    assert status is IngestionStatus.PARTIAL
    assert reason == SemanticFault.BUDGET_EXHAUSTED.value


def test_a_clean_run_with_a_provider_is_complete() -> None:
    status, reason = derive_status(faults=(), gaps=(), provider_present=True)
    assert status is IngestionStatus.COMPLETE
    assert reason == ""


def test_a_blocking_fault_wins_over_a_gap() -> None:
    status, _ = derive_status(
        faults=(StructuralFault.UNREADABLE_SOURCE,),
        gaps=(Gap.OCR_UNAVAILABLE,),
        provider_present=True,
    )
    assert status is IngestionStatus.BLOCKED


def test_every_blocking_fault_and_partial_gap_is_a_declared_member() -> None:
    assert BLOCKING_FAULTS <= set(StructuralFault)
    assert PARTIAL_GAPS <= set(Gap)


def test_an_unreadable_source_is_reported_as_failed_not_raised(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    def explode(path: Path, metadata: dict[str, object]) -> None:
        raise OSError("disk went away")

    engine = IngestionEngine(ingest=explode)
    result = engine.run(tmp_path / "sumiu.vtt", "", knowledge, "reuniao")
    assert result.status is IngestionStatus.FAILED
    assert result.reason == StructuralFault.UNREADABLE_SOURCE.value


def test_a_transcript_without_a_provider_reports_structural_only(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = tmp_path / "reuniao.vtt"
    source.write_text(fixtures.VTT, encoding="utf-8")
    result = IngestionEngine().run(
        source, "", knowledge, "reuniao"
    )
    assert result.status is IngestionStatus.STRUCTURAL_ONLY
    assert result.reason == SemanticFault.PROVIDER_UNAVAILABLE.value
    assert result.blocks > 0


def test_an_image_only_pdf_with_a_provider_reports_partial(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    pytest.importorskip("pypdf")
    source = tmp_path / "digitalizado.pdf"
    source.write_bytes(fixtures.image_only_pdf())
    engine = IngestionEngine()
    result = engine.run(
        source, "", knowledge, "digitalizado", provider=FakeProvider(scripts=[Script()])
    )
    assert result.status is IngestionStatus.PARTIAL
    assert result.reason in {item.value for item in PARTIAL_GAPS}


def test_an_unsupported_binary_with_a_provider_reports_blocked(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = tmp_path / "imagem.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    engine = IngestionEngine()
    result = engine.run(
        source, "", knowledge, "imagem", provider=FakeProvider(scripts=[Script()])
    )
    assert result.status is IngestionStatus.BLOCKED
    assert result.reason == StructuralFault.UNSUPPORTED_FORMAT.value


def test_a_readable_transcript_with_a_provider_reports_complete(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = tmp_path / "reuniao.vtt"
    source.write_text(fixtures.VTT, encoding="utf-8")
    engine = IngestionEngine()
    result = engine.run(
        source, "", knowledge, "reuniao", provider=FakeProvider(scripts=[Script()])
    )
    assert result.status is IngestionStatus.COMPLETE
    assert result.reason == ""


def test_the_serialized_result_carries_the_status_and_reason(
    tmp_path: Path, knowledge: KnowledgeRepository
) -> None:
    source = tmp_path / "reuniao.vtt"
    source.write_text(fixtures.VTT, encoding="utf-8")
    payload = (
        IngestionEngine()
        .run(source, "", knowledge, "reuniao")
        .to_dict()
    )
    assert payload["status"] == "structural_only"
    assert payload["reason"] == SemanticFault.PROVIDER_UNAVAILABLE.value


def test_the_structural_status_of_the_pipeline_is_an_enum() -> None:
    assert [item.value for item in IngestStatus] == ["complete", "partial", "failed"]


STATE_MODULES: tuple[str, ...] = ("integration.py", "pipeline.py", "semantic.py")


def _string_membership_tests(tree: ast.AST) -> list[int]:
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            continue
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            found.append(node.lineno)
    return found


def test_no_state_deriving_module_reads_a_diagnostic_substring() -> None:
    offenders: list[str] = []
    for name in STATE_MODULES:
        path = SOURCE_ROOT / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(f"{name}:{line}" for line in _string_membership_tests(tree))
    assert offenders == []


def test_derive_status_reads_only_typed_facts() -> None:
    tree = ast.parse((SOURCE_ROOT / "integration.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "derive_status"
    )
    literals = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert literals == []
