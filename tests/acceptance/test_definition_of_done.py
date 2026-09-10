from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.acceptance.reachability import modules_of, source_root, unreachable
from tests.acceptance.scenarios import (
    CAPABILITY,
    LEGACY_EIGHT,
    MIGRATION_DOCX_BODY,
    MODERN_TWENTY_ONE,
    NAMESPACE_OBJECTIVE,
    SERVICE,
    assert_settled,
    cobol_scripts,
    java_acceptance_repo,
    java_scripts,
    mainframe_acceptance_repo,
    profile_of,
    scripted_registry,
)
from tests.acceptance.test_inception import (
    DECISION,
    DIAGRAM_INTEGRATION,
    PROPOSAL,
    QUESTION,
    SHEET_RULE,
    _document_registry,
)
from tests.app.test_end_to_end import _UpdateProvider, _update_script
from tests.ingestion import fixtures
from wiki_ai.agent.providers import PROVIDER_MODULES
from wiki_ai.agent.providers.claude import DISALLOWED_TOOLS
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app import api
from wiki_ai.app.commands import COMMANDS, main
from wiki_ai.app.ports import (
    IngestionRunner,
    InvestigationRunner,
    PublicationRunner,
    QueryRunner,
    UpdateRunner,
)
from wiki_ai.app.session import STATE_DIR_NAME, Session
from wiki_ai.app.wiring import Wiring
from wiki_ai.ingestion.adapters import registry as adapters
from wiki_ai.ingestion import pipeline
from wiki_ai.ingestion.source import BlockKind, SourceKind
from wiki_ai.ingestion.toolspec import TOOL_NAMES as DOCUMENT_TOOLS
from wiki_ai.knowledge import gate as knowledge_gate
from wiki_ai.knowledge.model import Confidence
from wiki_ai.knowledge.taxonomy import EntityKind, RelationKind
from wiki_ai.publishing.docx.inspect import read_package
from wiki_ai.publishing.manifest import ArtifactKind
from wiki_ai.publishing.model import CAPABILITY_SECTIONS, DocumentKind
from wiki_ai.publishing.release import RELEASES_DIRNAME, read_manifest
from wiki_ai.quality import architecture, source_hygiene
from wiki_ai.repository.harness import RepositoryHarness
from wiki_ai.repository.schemas import TOOL_NAMES as REPOSITORY_TOOLS
from wiki_ai.repository.snapshot import SnapshotSpec, take_snapshot

FORBIDDEN_NAMES: tuple[str, ...] = ("legacy", "legado", "compat", "deprecated")
FORBIDDEN_PATHS: tuple[str, ...] = ("codescan", "sbindex", "wk", "scripts")
WRITE_TOOL_MARKERS: tuple[str, ...] = ("write", "edit", "delete", "patch", "apply")

CHANGED_SERVICE = """package com.acme.order;

import com.acme.order.OrderRepository;

public class OrderService {
    private final OrderRepository repository;
    public OrderService(OrderRepository repository) {
        this.repository = repository;
    }
    public String place(String reference) {
        if (reference == null) {
            throw new IllegalArgumentException("reference is required");
        }
        return repository.save(reference.trim());
    }
}
"""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture()
def java(tmp_path: Path) -> tuple[Path, ProviderRegistry]:
    repo = java_acceptance_repo(tmp_path / "java")
    registry = scripted_registry(java_scripts())
    report = api.analyze(repo, NAMESPACE_OBJECTIVE, registry=registry)
    assert_settled(report)
    return repo, registry


def test_a_single_codebase_analyzer_serves_the_application() -> None:
    wiring = Wiring()
    runner = wiring.investigation_runner()
    assert isinstance(runner, InvestigationRunner)
    assert type(wiring.investigation_runner()) is type(runner)
    implementations = _implementations("InvestigationRunner")
    assert len(implementations) == 1, implementations


def test_a_single_ingestion_pipeline_serves_the_application() -> None:
    assert isinstance(Wiring().ingestion_runner(), IngestionRunner)
    assert len(_implementations("IngestionRunner")) == 1


def test_a_single_knowledge_model_backs_every_area() -> None:
    modules = modules_of(source_root())
    repositories = [name for name in modules if name.endswith("knowledge.repository")]
    assert repositories == ["wiki_ai.knowledge.repository"]
    assert len(_implementations("UpdateRunner")) == 1
    assert isinstance(Wiring().update_runner(), UpdateRunner)


def test_a_single_publishing_pipeline_and_a_single_docx_path_exist() -> None:
    assert isinstance(Wiring().publication_runner(), PublicationRunner)
    assert isinstance(Wiring().query_runner(), QueryRunner)
    assert len(_implementations("PublicationRunner")) == 1
    modules = modules_of(source_root())
    packagers = [name for name in modules if name.endswith("docx.package")]
    assert packagers == ["wiki_ai.publishing.docx.package"]


def _implementations(protocol: str) -> tuple[str, ...]:
    root = _project_root() / "src" / "wiki_ai" / "app" / "wiring.py"
    text = root.read_text(encoding="utf-8")
    suffix = protocol.replace("Runner", "Adapter")
    return tuple(
        line.split("class ", 1)[1].split(":", 1)[0].strip()
        for line in text.splitlines()
        if line.startswith(f"class {suffix}")
    )


def test_no_legacy_or_compat_name_survives_the_gate() -> None:
    violations = architecture.check(_project_root() / "src")
    assert [str(item) for item in violations] == []
    for path in modules_of(source_root()).values():
        lowered = path.read_text(encoding="utf-8").lower()
        for word in FORBIDDEN_NAMES:
            assert word not in lowered, f"{path}: {word}"


def test_no_comment_or_docstring_survives_the_gate() -> None:
    root = _project_root()
    violations = source_hygiene.scan([root / "src", root / "tests"])
    assert [str(item) for item in violations] == []


def test_no_codescan_no_sbindex_and_no_monolithic_command_line() -> None:
    root = _project_root()
    for name in FORBIDDEN_PATHS:
        assert not (root / name).exists(), name
        assert not (root / "src" / "wiki_ai" / name).exists(), name
    assert COMMANDS == (
        "analyze",
        "ingest",
        "ask",
        "publish",
        "status",
        "version",
        "inspect",
        "provider",
    )
    surface = root / "src" / "wiki_ai" / "app" / "commands.py"
    assert len(surface.read_text(encoding="utf-8").splitlines()) < 500


def test_the_agent_investigates_the_repository_dynamically(
    java: tuple[Path, ProviderRegistry]
) -> None:
    repo, _ = java
    report = api.analyze(repo, NAMESPACE_OBJECTIVE, registry=scripted_registry(java_scripts()))
    assert_settled(report)
    with Session.open(repo).open_knowledge() as knowledge:
        assert knowledge.find_entities(EntityKind.CAPABILITY.value)
    assert "repo.search" in REPOSITORY_TOOLS
    assert "repo.read" in REPOSITORY_TOOLS
    assert "repo.inventory" in REPOSITORY_TOOLS


def test_the_agent_has_no_arbitrary_write(tmp_path: Path) -> None:
    for name in REPOSITORY_TOOLS + DOCUMENT_TOOLS:
        for marker in WRITE_TOOL_MARKERS:
            assert marker not in name.lower(), name
    assert "evidence.capture" in REPOSITORY_TOOLS
    assert "evidence.capture" in DOCUMENT_TOOLS
    for tool in ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert tool in DISALLOWED_TOOLS


def test_the_repository_harness_never_writes_into_the_snapshot(
    java: tuple[Path, ProviderRegistry]
) -> None:
    repo, _ = java
    harness = RepositoryHarness(
        take_snapshot(
            SnapshotSpec(root=repo, excludes=(STATE_DIR_NAME,)),
            store=Session.open(repo).snapshot_store,
        )
    )
    before = {
        item.relative_to(repo).as_posix(): item.stat().st_mtime
        for item in repo.rglob("*")
        if item.is_file() and ".wiki-ai" not in item.parts
    }
    assert {spec.name for spec in harness.specs()} == set(REPOSITORY_TOOLS)
    harness.invoke("repo.inventory", {})
    harness.invoke("repo.read", {"path": SERVICE})
    after = {
        item.relative_to(repo).as_posix(): item.stat().st_mtime
        for item in repo.rglob("*")
        if item.is_file() and ".wiki-ai" not in item.parts
    }
    assert before == after


def test_evidence_is_mandatory_for_a_supported_behaviour(
    java: tuple[Path, ProviderRegistry]
) -> None:
    repo, _ = java
    with Session.open(repo).open_knowledge() as knowledge:
        supported = [
            entity
            for entity in knowledge.find_entities()
            if entity.confidence is Confidence.SUPPORTED
        ]
        assert supported
        for entity in supported:
            assert knowledge.evidence_for(entity.id), entity.name
        assert knowledge_gate.check(knowledge) == ()


def test_the_analysis_works_without_a_language_specific_parser(
    tmp_path: Path,
) -> None:
    repo = mainframe_acceptance_repo(tmp_path / "mainframe")
    report = api.analyze(
        repo, NAMESPACE_OBJECTIVE, registry=scripted_registry(cobol_scripts())
    )
    assert_settled(report)
    assert report.details["entities_written"] > 0
    assert "aborted" not in report.details


def test_java_is_analysable_regardless_of_the_language_version(
    java: tuple[Path, ProviderRegistry]
) -> None:
    repo, _ = java
    eight = (repo / LEGACY_EIGHT).read_text(encoding="utf-8")
    twenty_one = (repo / MODERN_TWENTY_ONE).read_text(encoding="utf-8")
    assert "Collectors.toList()" in eight
    assert "record OrderSummary" in twenty_one
    assert "switch (status)" in twenty_one
    with Session.open(repo).open_knowledge() as knowledge:
        profile = profile_of(knowledge, CAPABILITY)
        assert profile is not None
        paths = {
            evidence.locator.to_dict()["path"]
            for rule in profile.rules
            for evidence in knowledge.evidence_for(rule.id)
        }
    assert {LEGACY_EIGHT, MODERN_TWENTY_ONE, SERVICE} <= paths


def test_cobol_and_jcl_have_an_investigative_fallback(tmp_path: Path) -> None:
    repo = mainframe_acceptance_repo(tmp_path / "mainframe")
    api.analyze(repo, NAMESPACE_OBJECTIVE, registry=scripted_registry(cobol_scripts()))
    with Session.open(repo).open_knowledge() as knowledge:
        procedures = {
            entity.name
            for entity in knowledge.find_entities(EntityKind.PROCEDURE.value)
        }
        entries = {
            entity.name
            for entity in knowledge.find_entities(EntityKind.ENTRY_POINT.value)
        }
    assert "PAYRUN" in procedures
    assert "PAYJOB STEP01" in entries


def test_the_spreadsheet_the_diagram_and_the_transcript_have_real_adapters() -> None:
    supported = set(adapters.kinds())
    assert {SourceKind.XLSX, SourceKind.DRAWIO, SourceKind.TRANSCRIPT} <= supported
    assert adapters.EXTENSION_KINDS[".xlsx"] is SourceKind.XLSX
    assert adapters.EXTENSION_KINDS[".drawio"] is SourceKind.DRAWIO
    assert adapters.EXTENSION_KINDS[".vtt"] is SourceKind.TRANSCRIPT


def test_the_spreadsheet_adapter_reads_real_cells(tmp_path: Path) -> None:
    ingested = pipeline.ingest(fixtures.write_xlsx(tmp_path / "regras.xlsx"))
    assert ingested.source.kind is SourceKind.XLSX
    cells = [block for block in ingested.blocks if block.kind is BlockKind.CELL]
    tables = [block for block in ingested.blocks if block.kind is BlockKind.TABLE]
    assert cells or tables
    text = " ".join(block.text for block in ingested.blocks)
    assert "Desconto A" in text
    assert "Desconto B" in text


def test_the_drawio_adapter_interprets_the_graph(tmp_path: Path) -> None:
    diagram = tmp_path / "arquitetura.drawio"
    diagram.write_text(fixtures.drawio_document(), encoding="utf-8")
    ingested = pipeline.ingest(diagram)
    assert ingested.source.kind is SourceKind.DRAWIO
    nodes = [block for block in ingested.blocks if block.kind is BlockKind.NODE]
    edges = [block for block in ingested.blocks if block.kind is BlockKind.EDGE]
    assert {block.text for block in nodes} >= {"API Cobranca", "Banco"}
    assert edges
    edge = edges[0]
    assert edge.attributes["source"]
    assert edge.attributes["target"]
    assert edge.text == "grava"


def test_the_transcript_adapter_extracts_speakers_and_time(tmp_path: Path) -> None:
    transcript = tmp_path / "reuniao.vtt"
    transcript.write_text(fixtures.VTT, encoding="utf-8")
    ingested = pipeline.ingest(transcript)
    assert ingested.source.kind is SourceKind.TRANSCRIPT
    utterances = [
        block for block in ingested.blocks if block.kind is BlockKind.UTTERANCE
    ]
    assert len(utterances) == 2
    speakers = {block.locator["speaker"] for block in utterances}
    assert speakers == {"Ana", "Bruno"}
    assert utterances[0].locator["time_end"] > utterances[0].locator["time_start"]
    assert "corte no dia 5" in utterances[0].text


def test_the_docx_is_generated_automatically_and_is_search_oriented(
    java: tuple[Path, ProviderRegistry]
) -> None:
    repo, registry = java
    report = api.publish(repo, registry=registry)
    assert report.status == "ok"
    release = (
        Session.open(repo).publications_dir / RELEASES_DIRNAME / report.publication_id
    )
    manifest = read_manifest(release)
    documents = manifest.of_kind(ArtifactKind.DOCX)
    assert documents
    found = False
    for artifact in documents:
        summary = read_package(release / artifact.relative_path)
        if summary.properties.category != DocumentKind.CAPABILITY.value:
            continue
        found = True
        assert CAPABILITY in summary.text
        for title in CAPABILITY_SECTIONS:
            assert title in summary.text, title
    assert found


def test_the_sharepoint_delivery_needs_no_manual_pipeline(
    java: tuple[Path, ProviderRegistry]
) -> None:
    repo, registry = java
    report = api.publish(repo, registry=registry)
    release = (
        Session.open(repo).publications_dir / RELEASES_DIRNAME / report.publication_id
    )
    declared = set(read_manifest(release).relative_paths)
    produced = {
        item.relative_to(release).as_posix()
        for item in release.rglob("*")
        if item.is_file() and item.name != "manifest.json"
    }
    assert produced == declared


def test_two_real_providers_execute_the_same_protocol() -> None:
    assert PROVIDER_MODULES == (
        "wiki_ai.agent.providers.claude",
        "wiki_ai.agent.providers.codex",
    )
    assert len(ProviderRegistry(adapters=True).registered()) == 2


def test_an_update_invalidates_by_source_version(
    java: tuple[Path, ProviderRegistry]
) -> None:
    repo, _ = java
    (repo / SERVICE).write_text(CHANGED_SERVICE, encoding="utf-8")
    registry = ProviderRegistry()
    registry.register("scripted", lambda: _UpdateProvider(scripts=[_update_script()]))
    report = api.analyze(repo, NAMESPACE_OBJECTIVE, registry=registry)
    assert_settled(report)
    assert report.details["invalidated"] > 0
    assert report.details["diff"]["changed"] == [SERVICE]
    with Session.open(repo).open_knowledge() as knowledge:
        assert knowledge_gate.check(knowledge) == ()


def test_the_inception_questions_correlate_code_and_sources(tmp_path: Path) -> None:
    repo = java_acceptance_repo(tmp_path / "inception")
    api.analyze(repo, NAMESPACE_OBJECTIVE, registry=scripted_registry(java_scripts()))
    transcript = tmp_path / "inception.vtt"
    transcript.write_text(fixtures.VTT, encoding="utf-8")
    sheet = fixtures.write_xlsx(tmp_path / "regras.xlsx")
    diagram = tmp_path / "arquitetura.drawio"
    diagram.write_text(fixtures.drawio_document(), encoding="utf-8")
    document = fixtures.write_docx(tmp_path / "proposta.docx", MIGRATION_DOCX_BODY)
    for source, findings in (
        (transcript, [DECISION]),
        (sheet, [SHEET_RULE]),
        (diagram, [DIAGRAM_INTEGRATION]),
        (document, [PROPOSAL]),
    ):
        report = api.ingest(source, repo, registry=_document_registry(source, findings))
        assert report.status == "ok", report.to_dict()
    answer = api.ask(QUESTION, repo)
    assert answer.status == "ok"
    assert answer.evidence_ids
    with Session.open(repo).open_knowledge() as knowledge:
        kinds = {relation.kind for relation in knowledge.find_relations()}
    assert RelationKind.DECLARES.value in kinds
    assert RelationKind.PROPOSES_CHANGE_TO.value in kinds


def test_no_production_module_is_unreachable() -> None:
    assert unreachable(source_root()) == ()


def test_the_command_line_output_is_always_json(
    java: tuple[Path, ProviderRegistry]
) -> None:
    repo, registry = java
    for argv in (
        ["status", "--repo", str(repo)],
        ["inspect", str(repo)],
        ["version"],
        ["ask", "Como funciona place order?", "--repo", str(repo)],
    ):
        stream = io.StringIO()
        main(argv, stream=stream, registry=registry)
        payload = json.loads(stream.getvalue())
        assert payload["status"] in ("ok", "blocked", "error")
