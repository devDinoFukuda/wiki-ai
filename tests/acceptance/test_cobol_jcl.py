from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.acceptance.scenarios import (
    COBOL,
    COPYBOOK,
    EMBEDDED_SQL,
    JCL,
    NAMESPACE_OBJECTIVE,
    PAYROLL,
    ScriptedProvider,
    SETTLED_STATUSES,
    assert_settled,
    cobol_scripts,
    entity_named,
    mainframe_acceptance_repo,
    registry_of,
)
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app import api
from wiki_ai.app.commands import EXIT_OK, main
from wiki_ai.app.session import Session
from wiki_ai.knowledge.model import Confidence
from wiki_ai.knowledge.taxonomy import EntityKind

BLOCKING_WORDS: tuple[str, ...] = (
    "unsupported language",
    "not supported",
    "linguagem não suportada",
    "no parser",
    "parser missing",
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return mainframe_acceptance_repo(tmp_path / "repo")


@pytest.fixture()
def provider() -> ScriptedProvider:
    return ScriptedProvider(scripts=cobol_scripts())


@pytest.fixture()
def registry(provider: ScriptedProvider) -> ProviderRegistry:
    return registry_of(provider)


@pytest.fixture()
def analyzed(repo: Path, registry: ProviderRegistry) -> Path:
    report = api.analyze(repo, NAMESPACE_OBJECTIVE, registry=registry)
    assert_settled(report)
    return repo


def test_the_analysis_never_blocks_for_a_missing_parser(
    repo: Path, registry: ProviderRegistry, provider: ScriptedProvider
) -> None:
    report = api.analyze(repo, NAMESPACE_OBJECTIVE, registry=registry)
    assert_settled(report)
    assert "aborted" not in report.details
    assert report.details["entities_written"] > 0
    text = json.dumps(report.to_dict(), ensure_ascii=False).lower()
    text += " ".join(provider.seen_objectives).lower()
    for word in BLOCKING_WORDS:
        assert word not in text


def test_the_programs_are_found(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        names = {
            entity.name
            for entity in knowledge.find_entities(EntityKind.PROCEDURE.value)
        }
        assert {"PAYRUN", "CALC-TOTAL", "BILLRUN"} <= names


def test_call_and_perform_are_followed_by_investigation(
    analyzed: Path, provider: ScriptedProvider
) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        operations = {
            entity.name
            for entity in knowledge.find_entities(EntityKind.OPERATION.value)
        }
        assert "TAXCALC" in operations
        billrun = entity_named(knowledge, EntityKind.PROCEDURE, "BILLRUN")
        called = {
            knowledge.get_entity(relation.target_id).name
            for relation in knowledge.relations_of(billrun.id, "out", ("calls",))
        }
        assert "PAYRUN" in called
    patterns = {
        str(arguments.get("pattern"))
        for script in cobol_scripts()
        for name, arguments in script.steps
        if name == "repo.search"
    }
    assert {"CALL", "PERFORM"} <= patterns


def test_the_copybook_is_associated_with_the_program(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        contract = entity_named(
            knowledge, EntityKind.DATA_CONTRACT, "PAYREC copybook"
        )
        paths = {
            evidence.locator.to_dict()["path"]
            for evidence in knowledge.evidence_for(contract.id)
        }
        assert COPYBOOK in paths
        assert COBOL in paths


def test_the_relevant_jcl_is_identified(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        entries = knowledge.find_entities(EntityKind.ENTRY_POINT.value)
        by_name = {entity.name: entity for entity in entries}
        assert {"PAYJOB STEP01", "PAYJOB STEP02"} <= set(by_name)
        assert by_name["PAYJOB STEP01"].attributes["mechanism"] == "jcl"
        paths = {
            evidence.locator.to_dict()["path"]
            for entity in by_name.values()
            for evidence in knowledge.evidence_for(entity.id)
        }
        assert JCL in paths
        dataset = entity_named(knowledge, EntityKind.INPUT, "PROD.PAYROLL.MASTER")
        assert knowledge.evidence_for(dataset.id)


def test_the_embedded_sql_is_captured(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        query = entity_named(knowledge, EntityKind.QUERY, "BILLRUN gross select")
        assert "PAYROLL_MASTER" in str(query.attributes["statement"])
        paths = {
            evidence.locator.to_dict()["path"]
            for evidence in knowledge.evidence_for(query.id)
        }
        assert EMBEDDED_SQL in paths


def test_every_finding_carries_its_evidence(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        supported = [
            entity
            for entity in knowledge.find_entities()
            if entity.confidence is Confidence.SUPPORTED
        ]
        assert supported
        for entity in supported:
            evidence = knowledge.evidence_for(entity.id)
            assert evidence, entity.name
            assert evidence[0].version_hash
            assert evidence[0].locator.to_dict()["path"]


def test_the_capability_is_reconstructed_from_the_mainframe_sources(
    analyzed: Path,
) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        capability = entity_named(knowledge, EntityKind.CAPABILITY, PAYROLL)
        assert capability.confidence is Confidence.SUPPORTED
        rules = {
            entity.name
            for entity in knowledge.find_entities(EntityKind.BUSINESS_RULE.value)
        }
        assert "gross is hours times rate" in rules
        assert knowledge.find_entities(EntityKind.FLOW.value)


def test_the_command_line_reports_the_mainframe_analysis_as_json(
    repo: Path, registry: ProviderRegistry
) -> None:
    stream = io.StringIO()
    code = main(
        ["analyze", str(repo), "--objective", NAMESPACE_OBJECTIVE],
        stream=stream,
        registry=registry,
    )
    payload = json.loads(stream.getvalue())
    assert code == EXIT_OK
    assert payload["status"] in SETTLED_STATUSES
    assert payload["analysis_status"] in ("complete", "partial")
    assert payload["details"]["entities_written"] > 0
