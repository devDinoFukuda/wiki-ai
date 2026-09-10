from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from tests.ingestion import fixtures
from tests.ingestion.fake_provider import FakeProvider as DocumentProvider
from tests.ingestion.fake_provider import Payloads
from tests.ingestion.fake_provider import Script as DocumentScript
from tests.ingestion.fake_provider import evidence_of
from tests.investigation.fake_provider import FakeProvider, Script
from tests.repository.fixtures_repos import java_repo
from wiki_ai.agent.protocol import AgentCapabilities, ToolCall
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.agent.session import AgentRun, AgentSession
from wiki_ai.app import api
from wiki_ai.app.commands import EXIT_BLOCKED, EXIT_OK, main
from wiki_ai.app.session import Session
from wiki_ai.app.wiring import DETAIL_KEYS, InvestigationAdapter, Wiring
from wiki_ai.ingestion import pipeline
from wiki_ai.investigation.orchestrator import Investigator
from wiki_ai.ingestion.harness import DocumentHarness
from wiki_ai.ingestion.integration import PROVIDER_UNAVAILABLE as SKIPPED_NO_PROVIDER
from wiki_ai.knowledge.model import KnowledgeState
from wiki_ai.publishing.release import CURRENT_POINTER, RELEASES_DIRNAME

BASE = "src/main/java/com/acme/order"
CONTROLLER = f"{BASE}/OrderController.java"
SERVICE = f"{BASE}/OrderService.java"
REPOSITORY = f"{BASE}/OrderRepository.java"
PRODUCER = f"{BASE}/OrderProducer.java"
CONFIG = "src/main/resources/application.yml"
OBJECTIVE = "describe how the order flow works"
QUESTION = "Como funciona place order?"
FORBIDDEN = ("store", "lease", "binding", "envelope", "objective_id")


def _ref(path: str, start: int, end: int) -> dict[str, Any]:
    return {"path": path, "line_start": start, "line_end": end}


def _capture(path: str, start: int, end: int, symbol: str | None = None):
    arguments: dict[str, Any] = {"path": path, "line_start": start, "line_end": end}
    if symbol is not None:
        arguments["symbol"] = symbol
    return ("evidence.capture", arguments)


def _script() -> Script:
    return Script(
        steps=(
            ("repo.inventory", {}),
            ("repo.read", {"path": SERVICE}),
            _capture(CONTROLLER, 15, 17, "place"),
            _capture(SERVICE, 10, 12, "place"),
            _capture(REPOSITORY, 3, 4, "save"),
            _capture(PRODUCER, 10, 12, "emit"),
            _capture(CONFIG, 4, 6),
        ),
        findings=[
            {
                "type": "capability",
                "subject": "place order",
                "statement": "OrderController place delegates to OrderService place",
                "evidence": [_ref(CONTROLLER, 15, 17)],
                "confidence": "supported",
                "relations": [
                    {
                        "kind": "belongs_to",
                        "target_subject": "order module",
                        "target_type": "module",
                    }
                ],
            },
            {
                "type": "entry_point",
                "subject": "OrderController place",
                "statement": "OrderController place is the entry to service place",
                "attributes": {"mechanism": "http", "location": "OrderController.place"},
                "evidence": [_ref(CONTROLLER, 15, 17)],
                "confidence": "supported",
                "relations": [
                    {
                        "kind": "implements",
                        "target_subject": "place order",
                        "target_type": "capability",
                    }
                ],
            },
            {
                "type": "business_rule",
                "subject": "order reference is persisted",
                "statement": "OrderService place saves the reference in the repository",
                "conditions": ["a reference is supplied"],
                "effects": ["the reference reaches OrderRepository save"],
                "evidence": [_ref(SERVICE, 10, 12)],
                "confidence": "supported",
                "relations": [
                    {
                        "kind": "validates",
                        "target_subject": "place order",
                        "target_type": "capability",
                    }
                ],
            },
            {
                "type": "persistence",
                "subject": "OrderRepository save",
                "statement": "OrderRepository save stores the order reference",
                "evidence": [_ref(REPOSITORY, 3, 4)],
                "confidence": "supported",
                "relations": [
                    {
                        "kind": "persists_to",
                        "target_subject": "place order",
                        "target_type": "capability",
                    }
                ],
            },
            {
                "type": "integration",
                "subject": "orders kafka producer",
                "statement": "OrderProducer emit sends the payload to the producer",
                "attributes": {"direction": "outbound", "protocol": "kafka"},
                "evidence": [_ref(PRODUCER, 10, 12)],
                "confidence": "supported",
                "relations": [
                    {
                        "kind": "calls",
                        "target_subject": "place order",
                        "target_type": "capability",
                    }
                ],
            },
            {
                "type": "configuration",
                "subject": "order topic",
                "statement": "the order topic is configured as orders-v1",
                "attributes": {"key": "order.topic"},
                "evidence": [_ref(CONFIG, 4, 6)],
                "confidence": "supported",
            },
        ],
    )


class _ScriptedProvider(FakeProvider):
    def connect(self) -> None:
        return None

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            tools=("repo.inventory", "repo.read", "evidence.capture")
        )

    def cancel(self) -> None:
        return None

    def run(self, session: AgentSession) -> AgentRun:
        return super().run(session)


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register("scripted", lambda: _ScriptedProvider(scripts=[_script()]))
    return registry


_CHANGED_SERVICE = """package com.acme.order;

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

DECISION = {
    "type": "decision_record",
    "subject": "corte no dia 5",
    "statement": "Decidimos manter o corte no dia 5",
}
RULE = {
    "type": "business_rule",
    "subject": "desconto A",
    "statement": "Desconto A aplica quando o valor passa de 100",
    "conditions": ["valor > 100"],
    "effects": ["aplicar desconto"],
}


class _UpdateProvider(FakeProvider):
    read_paths: list[dict[str, Any]]

    def __init__(self, scripts: list[Script]) -> None:
        super().__init__(scripts=scripts)
        self.read_paths = []

    def connect(self) -> None:
        return None

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(tools=("repo.read", "evidence.capture"))

    def cancel(self) -> None:
        return None

    def run(self, session: AgentSession) -> AgentRun:
        if self.calls < len(self.scripts):
            for name, arguments in self.scripts[self.calls].steps:
                if name in ("repo.read", "evidence.capture") and "path" in arguments:
                    self.read_paths.append(dict(arguments))
        return super().run(session)


def _update_script() -> Script:
    return Script(
        steps=(
            ("repo.read", {"path": SERVICE}),
            _capture(SERVICE, 10, 15, "place"),
        ),
        findings=[
            {
                "type": "business_rule",
                "subject": "order reference is persisted",
                "statement": "OrderService place rejects a null reference before saving",
                "conditions": ["a reference is supplied"],
                "effects": ["the trimmed reference reaches OrderRepository save"],
                "evidence": [_ref(SERVICE, 10, 15)],
                "confidence": "supported",
            }
        ],
    )


def _tracking_registry() -> tuple[ProviderRegistry, _ScriptedProvider]:
    provider = _ScriptedProvider(scripts=[_script()])
    registry = ProviderRegistry()
    registry.register("scripted", lambda: provider)
    return registry, provider


class _DocumentBackedProvider(DocumentProvider):
    def connect(self) -> None:
        return None

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(tools=("doc.blocks", "evidence.capture"))

    def cancel(self) -> None:
        return None

    def run(self, session: AgentSession) -> AgentRun:
        return super().run(session)


def _document_registry(source: Path, finding: dict[str, Any]) -> ProviderRegistry:
    harness = DocumentHarness(pipeline.ingest(source).document)
    identifier = harness.invoke("doc.blocks", {})["blocks"][0]["block_id"]

    def build(payloads: Payloads) -> list[dict[str, Any]]:
        return [{**finding, "evidence": evidence_of(payloads)}]

    provider = _DocumentBackedProvider(
        scripts=[
            DocumentScript(
                steps=(("evidence.capture", {"block_ids": [identifier]}),),
                build_findings=build,
            )
        ]
    )
    registry = ProviderRegistry()
    registry.register("scripted", lambda: provider)
    return registry


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    java_repo(root)
    return root


def _cli(*argv: str, registry: ProviderRegistry | None = None) -> tuple[int, dict]:
    stream = io.StringIO()
    code = main(list(argv), stream=stream, registry=registry)
    return code, json.loads(stream.getvalue())


def test_analyze_populates_knowledge_with_supported_evidence(repo: Path) -> None:
    report = api.analyze(repo, OBJECTIVE, registry=_registry())
    assert report.status == "partial"
    assert report.analysis_status == "partial"
    assert report.analyzed_digest == ""
    assert report.details["entities_written"] > 0
    assert report.details["evidence_written"] > 0
    assert set(report.details["details"]) <= set(DETAIL_KEYS)
    with Session.open(repo).open_knowledge() as knowledge:
        supported = [
            entity
            for entity in knowledge.find_entities()
            if entity.confidence.value == "supported"
        ]
        assert supported
        evidence = knowledge.evidence_for(supported[0].id)
        assert evidence
        assert evidence[0].version_hash == report.snapshot_digest


def test_ask_answers_from_the_written_knowledge(repo: Path) -> None:
    api.analyze(repo, OBJECTIVE, registry=_registry())
    report = api.ask(QUESTION, repo, wiring=Wiring(ProviderRegistry()))
    assert report.status == "ok"
    assert report.evidence_ids
    assert report.mode == "deterministic_fallback"
    assert "place order" in report.answer


def test_publish_writes_a_release_and_points_current(repo: Path) -> None:
    registry = _registry()
    api.analyze(repo, OBJECTIVE, registry=registry)
    report = api.publish(repo, registry=registry)
    assert report.status == "ok"
    assert report.manifest_hash
    publications = Session.open(repo).publications_dir
    release = publications / RELEASES_DIRNAME / report.publication_id
    assert release.is_dir()
    produced = sorted(item.name for item in release.glob("*.docx"))
    assert produced
    assert set(report.artifacts) >= set(produced)
    pointer = (publications / CURRENT_POINTER).read_text(encoding="utf-8").strip()
    assert pointer == report.publication_id


def test_a_second_publish_reuses_the_same_publication(repo: Path) -> None:
    registry = _registry()
    api.analyze(repo, OBJECTIVE, registry=registry)
    first = api.publish(repo, registry=registry)
    second = api.publish(repo, registry=registry)
    assert first.publication_id == second.publication_id
    assert first.manifest_hash == second.manifest_hash


def test_status_reflects_the_whole_flow(repo: Path) -> None:
    registry = _registry()
    analyzed = api.analyze(repo, OBJECTIVE, registry=registry)
    published = api.publish(repo, registry=registry)
    report = api.status(repo, registry=registry)
    assert report.status == "ok"
    assert report.snapshot_digest == analyzed.snapshot_digest
    assert report.entities > 0
    assert report.evidence > 0
    assert report.last_publication == published.publication_id
    assert report.publication_artifacts == len(published.artifacts)
    assert report.provider_available is True


def test_analyze_without_a_provider_is_blocked_with_the_documented_payload(
    repo: Path,
) -> None:
    report = api.analyze(repo, OBJECTIVE, wiring=Wiring(ProviderRegistry()))
    assert report.to_dict()["status"] == "blocked"
    assert report.to_dict()["reason"] == "agent_provider_unavailable"
    assert report.to_dict()["action"] == "configure a supported provider"


def test_publish_without_knowledge_is_blocked(repo: Path) -> None:
    report = api.publish(repo, registry=ProviderRegistry())
    assert report.status == "blocked"
    assert report.reason == "nothing_to_publish"


def test_the_command_line_runs_the_whole_flow_with_clean_json(repo: Path) -> None:
    registry = _registry()
    codes: list[int] = []
    payloads: list[dict] = []
    for argv, used in (
        (("analyze", str(repo), "--objective", OBJECTIVE), registry),
        (("ask", QUESTION, "--repo", str(repo)), ProviderRegistry()),
        (("publish", "--repo", str(repo)), registry),
        (("status", "--repo", str(repo)), registry),
    ):
        code, payload = _cli(*argv, registry=used)
        codes.append(code)
        payloads.append(payload)
    assert codes == [EXIT_OK, EXIT_OK, EXIT_OK, EXIT_OK]
    assert payloads[0]["command"] == "analyze"
    assert payloads[1]["evidence_ids"]
    assert payloads[2]["publication_id"]
    assert payloads[3]["last_publication"] == payloads[2]["publication_id"]
    for payload in payloads:
        text = json.dumps(payload, ensure_ascii=False).lower()
        for word in FORBIDDEN:
            assert word not in text


def test_the_command_line_blocks_analyze_without_a_provider(repo: Path) -> None:
    code, payload = _cli("analyze", str(repo), registry=ProviderRegistry())
    assert code == EXIT_BLOCKED
    assert payload["status"] == "blocked"
    assert payload["reason"] == "agent_provider_unavailable"
    assert payload["action"] == "configure a supported provider"


def test_ingest_without_a_provider_is_partial_with_an_honest_diagnostic(
    repo: Path, tmp_path: Path
) -> None:
    source = tmp_path / "reuniao.vtt"
    source.write_text(fixtures.VTT, encoding="utf-8")
    report = api.ingest(source, repo, wiring=Wiring(ProviderRegistry()))
    assert report.status == "partial"
    assert report.ingestion_status == "structural_only"
    assert report.registered is True
    assert report.details["blocks"] > 0
    assert report.details["entities_written"] == 0
    assert report.details["diagnostics"] == [SKIPPED_NO_PROVIDER]


def test_ingest_of_a_transcript_writes_declared_entities_with_document_evidence(
    repo: Path, tmp_path: Path
) -> None:
    source = tmp_path / "reuniao.vtt"
    source.write_text(fixtures.VTT, encoding="utf-8")
    report = api.ingest(source, repo, registry=_document_registry(source, DECISION))
    assert report.status == "ok"
    assert report.details["entities_written"] == 1
    assert report.details["diagnostics"] == []
    with Session.open(repo).open_knowledge() as knowledge:
        decision = knowledge.find_entities("decision_record")[0]
        assert decision.state is KnowledgeState.DECLARED
        evidence = knowledge.evidence_for(decision.id)
        assert evidence
        assert evidence[0].version_hash == report.details["version_hash"]


def test_ingest_of_a_spreadsheet_range_writes_a_declared_rule(
    repo: Path, tmp_path: Path
) -> None:
    source = fixtures.write_xlsx(tmp_path / "regras.xlsx")
    report = api.ingest(source, repo, registry=_document_registry(source, RULE))
    assert report.status == "ok"
    assert report.details["entities_written"] == 1
    with Session.open(repo).open_knowledge() as knowledge:
        rule = knowledge.find_entities("business_rule")[0]
        assert rule.state is KnowledgeState.DECLARED
        assert knowledge.evidence_for(rule.id)


def test_ingest_of_an_image_only_pdf_is_partial(repo: Path, tmp_path: Path) -> None:
    source = tmp_path / "digitalizado.pdf"
    source.write_bytes(fixtures.image_only_pdf())
    report = api.ingest(source, repo, wiring=Wiring(ProviderRegistry()))
    assert report.status == "partial"
    assert report.ingestion_status == "structural_only"
    assert report.reason == "structural_only"


def test_a_second_analyze_without_changes_is_up_to_date_without_a_provider_call(
    repo: Path,
) -> None:
    registry, provider = _tracking_registry()
    api.analyze(repo, OBJECTIVE, registry=registry)
    before = provider.calls
    assert before > 0
    report = api.analyze(repo, OBJECTIVE, registry=registry)
    assert report.reason != "up_to_date"
    assert report.status == "partial"
    assert provider.calls > before


def test_analyze_after_a_change_updates_only_the_touched_path(repo: Path) -> None:
    registry = _registry()
    first = api.analyze(repo, OBJECTIVE, registry=registry)
    with Session.open(repo).open_knowledge() as knowledge:
        before = {
            entity.id: entity.confidence.value
            for entity in knowledge.find_entities()
        }
    (repo / SERVICE).write_text(_CHANGED_SERVICE, encoding="utf-8")
    updater = _UpdateProvider(scripts=[_update_script()])
    update_registry = ProviderRegistry()
    update_registry.register("scripted", lambda: updater)
    report = api.analyze(repo, OBJECTIVE, registry=update_registry)
    assert report.status in {"ok", "partial"}
    assert report.snapshot_digest != first.snapshot_digest
    assert report.details["diff"]["changed"] == [SERVICE]
    assert report.details["invalidated"] > 0
    assert report.details["reinvestigated"]["entities_written"] > 0
    assert {call["path"] for call in updater.read_paths} == {SERVICE}
    with Session.open(repo).open_knowledge() as knowledge:
        still_supported = [
            entity
            for entity in knowledge.find_entities()
            if entity.id in before and entity.confidence.value == "supported"
        ]
    assert still_supported


def test_analyze_after_a_change_without_a_provider_invalidates_and_blocks(
    repo: Path,
) -> None:
    api.analyze(repo, OBJECTIVE, registry=_registry())
    (repo / SERVICE).write_text(_CHANGED_SERVICE, encoding="utf-8")
    report = api.analyze(repo, OBJECTIVE, wiring=Wiring(ProviderRegistry()))
    assert report.status == "blocked"
    assert report.reason == "agent_provider_unavailable"
    assert report.details["invalidated"] > 0


def test_status_reports_sources_by_kind_and_a_pending_update(
    repo: Path, tmp_path: Path
) -> None:
    registry = _registry()
    api.analyze(repo, OBJECTIVE, registry=registry)
    source = tmp_path / "reuniao.vtt"
    source.write_text(fixtures.VTT, encoding="utf-8")
    api.ingest(source, repo, wiring=Wiring(ProviderRegistry()))
    settled = api.status(repo, registry=registry)
    assert settled.observed_digest == settled.analyzed_digest or settled.pending_update
    assert dict(settled.sources_by_kind)["transcript"] == 1
    assert dict(settled.sources_by_kind)["codebase"] >= 1
    (repo / SERVICE).write_text(_CHANGED_SERVICE, encoding="utf-8")
    pending = api.status(repo, registry=registry)
    assert pending.pending_update is True
    assert pending.observed_digest != settled.observed_digest


def test_the_command_line_reports_an_update_with_clean_json(repo: Path) -> None:
    registry = _registry()
    _cli("analyze", str(repo), "--objective", OBJECTIVE, registry=registry)
    (repo / SERVICE).write_text(_CHANGED_SERVICE, encoding="utf-8")
    update_registry = ProviderRegistry()
    update_registry.register("scripted", lambda: _UpdateProvider(scripts=[_update_script()]))
    code, payload = _cli(
        "analyze", str(repo), "--objective", OBJECTIVE, registry=update_registry
    )
    assert code == EXIT_OK
    assert payload["details"]["invalidated"] > 0
    status_code, status_payload = _cli("status", "--repo", str(repo), registry=registry)
    assert status_code == EXIT_OK
    assert status_payload["pending_update"] is False
    text = json.dumps([payload, status_payload], ensure_ascii=False).lower()
    for word in FORBIDDEN:
        assert word not in text


class _FocusProbingProvider(FakeProvider):
    searched: list[dict[str, Any]]

    def __init__(self, scripts: list[Script]) -> None:
        super().__init__(scripts=scripts)
        self.searched = []

    def connect(self) -> None:
        return None

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(tools=("repo.search", "repo.read", "evidence.capture"))

    def cancel(self) -> None:
        return None

    def run(self, session: AgentSession) -> AgentRun:
        if not self.searched:
            result = session.invoke(
                ToolCall(name="repo.search", arguments={"pattern": "place"})
            )
            self.searched.append(dict(result.payload))
            session.invoke(ToolCall(name="repo.read", arguments={"path": CONTROLLER}))
        return super().run(session)


def test_an_update_focuses_the_reinvestigation_on_the_changed_path(repo: Path) -> None:
    api.analyze(repo, OBJECTIVE, registry=_registry())
    (repo / SERVICE).write_text(_CHANGED_SERVICE, encoding="utf-8")
    provider = _FocusProbingProvider(scripts=[_update_script()])
    registry = ProviderRegistry()
    registry.register("scripted", lambda: provider)
    report = api.analyze(repo, OBJECTIVE, registry=registry)
    assert report.status == "ok"
    assert set(provider.searched[0]["paths"]) == {SERVICE}
    reinvestigated = report.details["reinvestigated"]["details"]
    assert reinvestigated["focus_paths"] == [SERVICE]
    assert reinvestigated["outside_focus_reads"] == 1
    assert set(reinvestigated["files_read"]) == {SERVICE, CONTROLLER}


class _ShortBudgetWiring(Wiring):
    def investigation_runner(self) -> Any:
        return InvestigationAdapter(Investigator(round_budget=1, total_tool_calls=1))


def test_a_short_budget_reports_partial_and_never_marks_the_snapshot_analyzed(
    repo: Path,
) -> None:
    wiring = _ShortBudgetWiring(_registry())
    report = api.analyze(repo, OBJECTIVE, wiring=wiring)
    assert report.status == "partial"
    assert report.analysis_status == "partial"
    assert report.analyzed_digest == ""
    assert report.reason
    state = Session.open(repo).analysis_state()
    assert state.observed_digest == report.snapshot_digest
    assert state.analyzed_digest == ""
    repeated = api.analyze(repo, OBJECTIVE, wiring=wiring)
    assert repeated.reason != "up_to_date"
    assert repeated.analysis_status == "partial"
