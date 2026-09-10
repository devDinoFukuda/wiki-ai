from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.acceptance.scenarios import (
    CAPABILITY,
    CONTROLLER,
    LEGACY_EIGHT,
    MODERN_TWENTY_ONE,
    NAMESPACE_OBJECTIVE,
    entity_named,
    java_acceptance_repo,
    java_scripts,
    names_of,
    profile_of,
    scripted_registry,
)
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app import api
from wiki_ai.app.session import Session
from wiki_ai.knowledge.evidence import CodeContent, CodeLocator
from wiki_ai.knowledge.model import Confidence, KnowledgeState
from wiki_ai.knowledge.query import KnowledgeQuery
from wiki_ai.knowledge.taxonomy import EntityKind
from wiki_ai.publishing.release import RELEASES_DIRNAME

MISSING_EVIDENCE_QUESTION = "Quais pontos desta migração ainda não possuem evidência?"

EVIDENCE_QUESTIONS: tuple[str, ...] = (
    "Analise profundamente este sistema.",
    "Como funciona place order?",
    "Quais regras de negócio participam deste fluxo?",
    "Quais são os inputs, outputs, invariantes e edge cases?",
    "Quais integrações são críticas?",
    "O que pode quebrar se every placed order is saved mudar?",
    "Compare o comportamento implementado com a decisão tomada na inception.",
    "Gere o material atualizado para publicação no SharePoint.",
)

QUESTIONS: tuple[str, ...] = EVIDENCE_QUESTIONS + (MISSING_EVIDENCE_QUESTION,)

INTERNAL_WORDS: tuple[str, ...] = (
    "store",
    "lease",
    "binding",
    "envelope",
    "objective_id",
    "revision_id",
    "capture_id",
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return java_acceptance_repo(tmp_path / "repo")


@pytest.fixture()
def registry(repo: Path) -> ProviderRegistry:
    return scripted_registry(java_scripts())


@pytest.fixture()
def analyzed(repo: Path, registry: ProviderRegistry) -> Path:
    report = api.analyze(repo, NAMESPACE_OBJECTIVE, registry=registry)
    assert report.status == "ok", report.to_dict()
    return repo


def test_analyze_through_the_app_writes_knowledge_with_evidence(analyzed: Path) -> None:
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


def test_the_entrypoints_are_identified(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        profile = profile_of(knowledge, CAPABILITY)
        assert profile is not None
        assert names_of(profile.entrypoints) == {"OrderController place"}
        entry = profile.entrypoints[0]
        assert entry.attributes["mechanism"] == "http"
        assert entry.attributes["location"] == "OrderController.place"


def test_the_business_rules_are_reconstructed_with_conditions_and_effects(
    analyzed: Path,
) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        profile = profile_of(knowledge, CAPABILITY)
        assert profile is not None
        rule = [
            item
            for item in profile.rules
            if item.name == "every placed order is saved"
        ][0]
        assert rule.attributes["conditions"] == ["place is called with a reference"]
        assert rule.attributes["effects"] == [
            "OrderRepository save receives the reference"
        ]
        assert rule.state is KnowledgeState.IMPLEMENTED
        assert knowledge.evidence_for(rule.id)


def test_the_inputs_and_outputs_are_identified(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        profile = profile_of(knowledge, CAPABILITY)
        assert profile is not None
        assert "order reference" in names_of(profile.inputs)
        assert "saved order reference" in names_of(profile.outputs)
        assert "place always returns what save returned" in names_of(profile.invariants)


def test_the_persistence_is_identified(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        profile = profile_of(knowledge, CAPABILITY)
        assert profile is not None
        assert "order repository" in names_of(profile.persistence)


def test_the_events_and_integrations_are_identified(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        profile = profile_of(knowledge, CAPABILITY)
        assert profile is not None
        kafka = [
            item
            for item in profile.integrations
            if item.name == "orders kafka producer"
        ][0]
        assert kafka.attributes["protocol"] == "kafka"
        assert kafka.attributes["direction"] == "outbound"
        assert "orders-v1 message" in names_of(profile.events)


def test_the_failure_modes_are_identified(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        profile = profile_of(knowledge, CAPABILITY)
        assert profile is not None
        failure = [
            item
            for item in profile.failures
            if item.name == "producer send receives null"
        ][0]
        assert failure.attributes["trigger"] == "emit is called"
        assert failure.attributes["effect"] == "send receives null instead of a record"


def test_the_edge_cases_are_identified(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        profile = profile_of(knowledge, CAPABILITY)
        assert profile is not None
        edge = [
            item
            for item in profile.edge_cases
            if item.name == "place with a null reference"
        ][0]
        assert edge.attributes["condition"] == "reference is null"
        assert edge.attributes["expected"] == "save receives null"


def test_a_flow_with_ordered_steps_is_generated(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        flows = knowledge.find_entities(EntityKind.FLOW.value)
        assert flows
        ordered = KnowledgeQuery(knowledge).flow(flows[0].id)
        ordinals = [view.entity.attributes["ordinal"] for view in ordered]
        assert ordinals == sorted(ordinals)
        subjects = [view.entity.attributes["subject"] for view in ordered]
        assert subjects[0] == "OrderController place"
        assert "OrderService place" in subjects
        assert "order repository" in subjects


def test_every_supported_item_presents_executable_evidence(analyzed: Path) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        entry = entity_named(
            knowledge, EntityKind.ENTRY_POINT, "OrderController place"
        )
        evidence = knowledge.evidence_for(entry.id)
        assert isinstance(evidence[0].locator, CodeLocator)
        assert evidence[0].locator.content is CodeContent.EXECUTABLE
        assert evidence[0].locator.path == CONTROLLER


def test_the_java_version_never_limits_the_reading_of_the_source(
    analyzed: Path,
) -> None:
    with Session.open(analyzed).open_knowledge() as knowledge:
        profile = profile_of(knowledge, CAPABILITY)
        assert profile is not None
        rules = names_of(profile.rules)
        assert "raw references are trimmed and emptied entries dropped" in rules
        assert "an order summary describes its status" in rules
        paths = {
            evidence.locator.to_dict()["path"]
            for rule in profile.rules
            for evidence in knowledge.evidence_for(rule.id)
        }
        assert LEGACY_EIGHT in paths
        assert MODERN_TWENTY_ONE in paths


@pytest.mark.parametrize("question", EVIDENCE_QUESTIONS)
def test_each_product_question_is_answered_with_evidence(
    analyzed: Path, registry: ProviderRegistry, question: str
) -> None:
    report = api.ask(question, analyzed, registry=registry)
    assert report.status == "ok"
    assert report.answer.strip()
    assert report.evidence_ids
    lowered = report.answer.lower()
    for word in INTERNAL_WORDS:
        assert word not in lowered
    for identifier in report.entity_ids + report.evidence_ids:
        assert identifier not in report.answer


def test_the_question_about_missing_evidence_lists_the_open_points(
    analyzed: Path, registry: ProviderRegistry
) -> None:
    report = api.ask(MISSING_EVIDENCE_QUESTION, analyzed, registry=registry)
    assert report.status == "ok"
    assert report.answer.strip()
    assert report.unresolved
    lowered = report.answer.lower()
    for word in INTERNAL_WORDS:
        assert word not in lowered
    for identifier in report.entity_ids:
        assert identifier not in report.answer


def test_no_answer_payload_exposes_internal_identifiers(
    analyzed: Path, registry: ProviderRegistry
) -> None:
    for question in QUESTIONS:
        payload = api.ask(question, analyzed, registry=registry).to_dict()
        text = json.dumps({"answer": payload.get("answer", "")}, ensure_ascii=False)
        for word in INTERNAL_WORDS:
            assert word not in text.lower()


def test_publish_produces_a_searchable_docx(
    analyzed: Path, registry: ProviderRegistry
) -> None:
    report = api.publish(analyzed, registry=registry)
    assert report.status == "ok"
    release = (
        Session.open(analyzed).publications_dir
        / RELEASES_DIRNAME
        / report.publication_id
    )
    produced = sorted(item.name for item in release.glob("*.docx"))
    assert produced
    assert set(report.artifacts) >= set(produced)
    assert report.manifest_hash
