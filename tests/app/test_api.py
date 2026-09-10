from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wiki_ai.agent.envelope import ResultEnvelope
from wiki_ai.agent.protocol import AgentCapabilities
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app import api
from wiki_ai.app.ports import InvestigationOutcome
from wiki_ai.app.session import LATEST_FILE, STATE_DIR_NAME, Session
from wiki_ai.app.wiring import Wiring
from wiki_ai.ingestion.source import SourceKind


class _Provider:
    def connect(self) -> None:
        return None

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(tools=("repo.read",))

    def run(self, session: Any) -> ResultEnvelope:
        return ResultEnvelope("env", "t", "s", "0" * 64, 0.0)

    def cancel(self) -> None:
        return None


class _Investigation:
    def run(
        self,
        objective: str,
        snapshot: Any,
        knowledge: Any,
        provider: Any,
        namespace: str,
    ) -> InvestigationOutcome:
        return InvestigationOutcome(
            objective=objective,
            entities_written=1,
            relations_written=0,
            evidence_written=1,
        )


class _WiringWithProvider(Wiring):
    def __init__(self) -> None:
        registry = ProviderRegistry()
        registry.register("probe", _Provider)
        super().__init__(registry)


class _WiringWithInvestigation(_WiringWithProvider):
    def investigation_runner(self) -> Any:
        return _Investigation()


def _repo(root: Path) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "README.md").write_text("title\n", encoding="utf-8")
    return root


def test_analyze_without_a_provider_is_blocked_with_the_documented_payload(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    report = api.analyze(tmp_path, wiring=Wiring())
    payload = report.to_dict()
    assert payload["status"] == "blocked"
    assert payload["reason"] == "agent_provider_unavailable"
    assert payload["action"] == "configure a supported provider"


def test_analyze_persists_the_snapshot_and_latest_pointer(tmp_path: Path) -> None:
    _repo(tmp_path)
    report = api.analyze(tmp_path, wiring=Wiring())
    state = tmp_path / STATE_DIR_NAME
    assert (state / "format.json").is_file()
    stored = state / "snapshots" / f"{report.snapshot_digest}.json"
    assert stored.is_file()
    latest = json.loads((state / "snapshots" / LATEST_FILE).read_text(encoding="utf-8"))
    assert latest["digest"] == report.snapshot_digest
    assert Session.open(tmp_path).current_snapshot_id() == report.snapshot_digest


def test_analyze_with_a_provider_reaches_the_wired_investigation(tmp_path: Path) -> None:
    _repo(tmp_path)
    report = api.analyze(tmp_path, wiring=_WiringWithProvider())
    assert report.status == "ok"
    assert report.details["objective"] == api.DEFAULT_OBJECTIVE


def test_analyze_delegates_to_the_investigation_runner(tmp_path: Path) -> None:
    _repo(tmp_path)
    report = api.analyze(tmp_path, "how does renewal work", _WiringWithInvestigation())
    assert report.status == "ok"
    assert report.details["objective"] == "how does renewal work"
    assert report.analyzable_files >= 1


def test_analyze_uses_a_default_objective(tmp_path: Path) -> None:
    _repo(tmp_path)
    report = api.analyze(tmp_path, "   ", _WiringWithInvestigation())
    assert report.details["objective"] == api.DEFAULT_OBJECTIVE


def test_analyze_excludes_the_state_directory_from_the_snapshot(tmp_path: Path) -> None:
    _repo(tmp_path)
    first = api.analyze(tmp_path, wiring=Wiring())
    second = api.analyze(tmp_path, wiring=Wiring())
    assert first.snapshot_digest == second.snapshot_digest


def test_ingest_without_a_provider_is_ok_and_honest(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    source = tmp_path / "notes.md"
    source.write_text("# renewal\n", encoding="utf-8")
    report = api.ingest(source, repo, Wiring())
    assert report.status == "ok"
    assert report.reason == ""
    assert (
        "semantic_investigation_skipped:agent_provider_unavailable"
        in report.details["diagnostics"]
    )
    assert report.details["entities_written"] == 0
    assert report.registered is True
    assert report.kind == SourceKind.MARKDOWN.value
    assert len(report.version_hash) == 64
    with Session.open(repo).open_knowledge() as knowledge:
        versions = knowledge.source_versions()
    assert versions
    assert report.version_hash in {item.version_hash for item in versions}


def test_ingest_reports_a_missing_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with pytest.raises(api.SourceNotFound):
        api.ingest(tmp_path / "absent.md", repo, Wiring())


@pytest.mark.parametrize(
    "name,kind",
    [
        ("a.docx", SourceKind.DOCX),
        ("a.xlsx", SourceKind.XLSX),
        ("a.drawio", SourceKind.DRAWIO),
        ("a.pdf", SourceKind.PDF),
        ("a.vtt", SourceKind.TRANSCRIPT),
        ("a.md", SourceKind.MARKDOWN),
        ("a.html", SourceKind.HTML),
        ("a.xml", SourceKind.XML),
        ("a.json", SourceKind.JSON),
    ],
)
def test_source_kind_follows_the_extension(name: str, kind: SourceKind) -> None:
    assert api.source_kind_for(Path(name)) is kind


def test_source_kind_of_a_directory_is_a_codebase(tmp_path: Path) -> None:
    assert api.source_kind_for(tmp_path) is SourceKind.CODEBASE


def test_ask_on_empty_knowledge_is_blocked(tmp_path: Path) -> None:
    _repo(tmp_path)
    report = api.ask("how does renewal work", tmp_path, Wiring())
    assert report.to_dict() == {
        "status": "blocked",
        "question": "how does renewal work",
        "reason": "knowledge_empty",
        "action": "run analyze or ingest first",
    }


def test_ask_after_ingest_reaches_the_wired_query_engine(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    source = tmp_path / "notes.md"
    source.write_text("# renewal\n", encoding="utf-8")
    api.ingest(source, repo, Wiring())
    report = api.ask("what changed", repo, Wiring())
    assert report.status == "ok"
    assert report.answer


def test_publish_on_empty_knowledge_is_blocked(tmp_path: Path) -> None:
    _repo(tmp_path)
    report = api.publish(tmp_path, Wiring())
    assert report.to_dict() == {
        "status": "blocked",
        "reason": "nothing_to_publish",
        "action": "run analyze or ingest first",
    }


def test_status_of_a_new_repository(tmp_path: Path) -> None:
    _repo(tmp_path)
    report = api.status(tmp_path, Wiring())
    payload = report.to_dict()
    assert payload["status"] == "ok"
    assert payload["snapshot_digest"] is None
    assert payload["entities"] == 0
    assert payload["relations"] == 0
    assert payload["evidence"] == 0
    assert payload["sources"] == {}
    assert payload["source_versions"] == 0
    assert payload["pending_update"] is False
    assert payload["last_publication"] is None
    assert payload["provider_available"] is False


def test_status_reflects_analyze_and_ingest(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    analyzed = api.analyze(repo, wiring=Wiring())
    source = tmp_path / "notes.md"
    source.write_text("# renewal\n", encoding="utf-8")
    api.ingest(source, repo, Wiring())
    report = api.status(repo, _WiringWithProvider())
    assert report.snapshot_digest == analyzed.snapshot_digest
    assert report.sources == 1
    assert dict(report.sources_by_kind)["markdown"] == 1
    assert report.pending_update is False
    assert report.provider_available is True


def test_every_operation_rejects_an_outdated_store(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / ".codescan").mkdir()
    for call in (
        lambda: api.analyze(tmp_path, wiring=Wiring()),
        lambda: api.ask("q", tmp_path, Wiring()),
        lambda: api.publish(tmp_path, Wiring()),
        lambda: api.status(tmp_path, Wiring()),
        lambda: api.inspect(tmp_path),
    ):
        with pytest.raises(api.OutdatedStore):
            call()


def test_every_operation_rejects_a_missing_repository(tmp_path: Path) -> None:
    absent = tmp_path / "absent"
    for call in (
        lambda: api.analyze(absent, wiring=Wiring()),
        lambda: api.ask("q", absent, Wiring()),
        lambda: api.publish(absent, Wiring()),
        lambda: api.status(absent, Wiring()),
    ):
        with pytest.raises(api.RepositoryNotFound):
            call()
