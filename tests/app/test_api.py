from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from wiki_ai.agent.protocol import AgentCapabilities
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.agent.session import AgentRun, BudgetUsage, RunStatus
from wiki_ai.app import api
from wiki_ai.app.ports import InvestigationOutcome, OutcomeStatus
from wiki_ai.app.session import (
    ANALYSIS_CONTRACT_VERSION,
    ANALYSIS_STATE_FILE,
    CODESCAN_DIRECTORY,
    COMPANION_DIRECTORIES,
    IGNORE_FILE,
    LATEST_FILE,
    STATE_DIR_NAME,
    AnalysisStatus,
    Session,
    objective_hash,
)
from wiki_ai.app.wiring import Wiring
from wiki_ai.ingestion.source import SourceKind


class _Provider:
    def connect(self) -> None:
        return None

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(tools=("repo.read",))

    def run(self, session: Any) -> AgentRun:
        return AgentRun(
            status=RunStatus.COMPLETED,
            findings=(),
            transcript=(),
            usage=BudgetUsage(tool_calls=0, tokens=0, seconds=0.0),
        )

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
    report = api.analyze(tmp_path, wiring=_WiringWithInvestigation())
    assert report.status == "ok"
    assert report.analysis_status == "complete"
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


def test_ingest_without_a_provider_is_partial_and_honest(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    source = tmp_path / "notes.md"
    source.write_text("# renewal\n", encoding="utf-8")
    report = api.ingest(source, repo, Wiring())
    assert report.status == "partial"
    assert report.reason == "structural_only"
    assert report.ingestion_status == "structural_only"
    assert "agent_provider_unavailable" in report.details["diagnostics"]
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
        "provider": "",
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
    assert payload["analysis_status"] == AnalysisStatus.NEVER.value
    assert payload["analyzed_digest"] == ""
    assert len(payload["observed_digest"]) == 64
    assert payload["last_publication"] is None
    assert payload["provider_available"] is False


def test_status_reflects_analyze_and_ingest(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    analyzed = api.analyze(repo, wiring=_WiringWithInvestigation())
    source = tmp_path / "notes.md"
    source.write_text("# renewal\n", encoding="utf-8")
    api.ingest(source, repo, Wiring())
    report = api.status(repo, _WiringWithProvider())
    assert report.snapshot_digest == analyzed.snapshot_digest
    assert report.analysis_status == AnalysisStatus.COMPLETE.value
    assert report.analyzed_digest == analyzed.snapshot_digest
    assert report.sources == 1
    assert dict(report.sources_by_kind)["markdown"] == 1
    assert report.pending_update is False
    assert report.provider_available is True


def test_status_after_a_blocked_analysis_never_claims_an_analyzed_digest(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    api.analyze(repo, wiring=Wiring())
    report = api.status(repo, Wiring())
    assert report.analysis_status == AnalysisStatus.BLOCKED.value
    assert report.analyzed_digest == ""
    assert report.pending_update is True


def test_status_reports_a_pending_update_after_a_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    api.analyze(repo, wiring=_WiringWithInvestigation())
    (repo / "src" / "extra.py").write_text("other = 2\n", encoding="utf-8")
    report = api.status(repo, Wiring())
    assert report.pending_update is True
    assert report.observed_digest != report.analyzed_digest


def test_every_operation_rejects_an_outdated_store(tmp_path: Path) -> None:
    _repo(tmp_path)
    marker = tmp_path / CODESCAN_DIRECTORY
    marker.mkdir()
    (marker / "state.db").write_bytes(b"")
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


class _PartialInvestigation:
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
            entities_written=0,
            relations_written=0,
            evidence_written=0,
            status=OutcomeStatus.PARTIAL,
            reason="budget_exhausted_with_open_frontier",
        )


class _WiringWithPartialInvestigation(_WiringWithProvider):
    def investigation_runner(self) -> Any:
        return _PartialInvestigation()


class _NoProviderPartial(Wiring):
    def investigation_runner(self) -> Any:
        return _PartialInvestigation()


def test_analyze_blocked_without_a_provider_never_records_an_analyzed_digest(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    report = api.analyze(tmp_path, wiring=Wiring())
    assert report.status == "blocked"
    assert report.analysis_status == AnalysisStatus.BLOCKED.value
    assert report.analyzed_digest == ""
    state = Session.open(tmp_path).analysis_state()
    assert state.observed_digest == report.snapshot_digest
    assert state.analyzed_digest == ""


def test_a_provider_appearing_after_a_block_triggers_a_real_investigation(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    blocked = api.analyze(tmp_path, wiring=Wiring())
    assert blocked.status == "blocked"
    second = api.analyze(tmp_path, wiring=_WiringWithInvestigation())
    assert second.reason != api.UP_TO_DATE
    assert second.status == "ok"
    assert second.analysis_status == AnalysisStatus.COMPLETE.value
    assert second.analyzed_digest == second.snapshot_digest
    assert second.snapshot_digest == blocked.snapshot_digest
    assert second.details["objective"] == api.DEFAULT_OBJECTIVE


def test_a_second_analyze_after_a_complete_run_is_up_to_date(tmp_path: Path) -> None:
    _repo(tmp_path)
    first = api.analyze(tmp_path, wiring=_WiringWithInvestigation())
    assert first.status == "ok"
    second = api.analyze(tmp_path, wiring=_WiringWithInvestigation())
    assert second.status == "ok"
    assert second.reason == api.UP_TO_DATE
    assert second.analyzed_digest == first.snapshot_digest


def test_a_partial_investigation_never_becomes_up_to_date(tmp_path: Path) -> None:
    _repo(tmp_path)
    first = api.analyze(tmp_path, wiring=_WiringWithPartialInvestigation())
    assert first.status == "partial"
    assert first.analysis_status == AnalysisStatus.PARTIAL.value
    assert first.analyzed_digest == ""
    assert first.reason == "budget_exhausted_with_open_frontier"
    second = api.analyze(tmp_path, wiring=_WiringWithPartialInvestigation())
    assert second.reason != api.UP_TO_DATE
    assert second.status == "partial"


def test_update_without_a_provider_is_blocked_and_keeps_the_analyzed_digest(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    analyzed = api.analyze(tmp_path, wiring=_WiringWithInvestigation())
    (tmp_path / "src" / "extra.py").write_text("other = 2\n", encoding="utf-8")
    report = api.analyze(tmp_path, wiring=Wiring())
    assert report.status == "blocked"
    assert report.reason == "agent_provider_unavailable"
    assert report.analysis_status == AnalysisStatus.BLOCKED.value
    assert report.analyzed_digest == analyzed.snapshot_digest
    assert report.snapshot_digest != analyzed.snapshot_digest


def test_the_analysis_state_file_holds_the_documented_fields(tmp_path: Path) -> None:
    _repo(tmp_path)
    report = api.analyze(tmp_path, wiring=_WiringWithInvestigation())
    payload = json.loads(
        (tmp_path / STATE_DIR_NAME / ANALYSIS_STATE_FILE).read_text(encoding="utf-8")
    )
    assert set(payload) == {
        "observed_digest",
        "analyzed_digest",
        "analysis_status",
        "analyzed_at",
        "reason",
        "analyzed_objective_hash",
        "contract_version",
    }
    assert payload["analyzed_digest"] == report.snapshot_digest
    assert payload["observed_digest"] == report.snapshot_digest
    assert payload["analysis_status"] == AnalysisStatus.COMPLETE.value
    assert payload["analyzed_at"]
    assert payload["analyzed_objective_hash"] == objective_hash(api.DEFAULT_OBJECTIVE)
    assert payload["contract_version"] == ANALYSIS_CONTRACT_VERSION


def test_analyze_materializes_the_snapshot_in_the_session_store(tmp_path: Path) -> None:
    _repo(tmp_path)
    report = api.analyze(tmp_path, wiring=Wiring())
    store = Session.open(tmp_path).snapshot_store
    restored = store.load(report.snapshot_digest)
    assert restored.materialized is True
    assert restored.read_bytes("src/app.py").strip() == b"value = 1"


def test_a_repository_with_raw_and_wiki_directories_is_analyzable(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    for name in COMPANION_DIRECTORIES:
        (tmp_path / name).mkdir()
        (tmp_path / name / "note.md").write_text("note\n", encoding="utf-8")
    report = api.analyze(tmp_path, wiring=Wiring())
    assert report.status == "blocked"
    assert report.reason == "agent_provider_unavailable"


def test_the_state_directory_leaves_git_status_clean(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    if subprocess.run(["git", "init", str(repo)], capture_output=True).returncode != 0:
        pytest.skip("git is unavailable")
    api.analyze(repo, wiring=_WiringWithInvestigation())
    assert (repo / STATE_DIR_NAME / IGNORE_FILE).is_file()
    completed = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    tracked = [
        line for line in completed.stdout.splitlines() if STATE_DIR_NAME in line
    ]
    assert tracked == []


def test_an_external_home_writes_nothing_inside_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo")
    home = tmp_path / "home"
    monkeypatch.setenv("WIKI_AI_HOME", str(home))
    report = api.analyze(repo, wiring=_WiringWithInvestigation())
    assert report.status == "ok"
    assert not (repo / STATE_DIR_NAME).exists()
    assert sorted(item.name for item in repo.iterdir()) == ["README.md", "src"]
    assert home.is_dir()


def test_ask_reports_the_answer_mode(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    source = tmp_path / "notes.md"
    source.write_text("# renewal\n", encoding="utf-8")
    api.ingest(source, repo, Wiring())
    report = api.ask("what changed", repo, Wiring())
    assert report.status == "ok"
    assert report.to_dict().get("mode", "") == report.mode


def test_a_different_objective_reruns_the_investigation(tmp_path: Path) -> None:
    _repo(tmp_path)
    first = api.analyze(tmp_path, "how does renewal work", _WiringWithInvestigation())
    assert first.status == "ok"
    assert first.reason != api.UP_TO_DATE
    repeated = api.analyze(tmp_path, "how does renewal work", _WiringWithInvestigation())
    assert repeated.reason == api.UP_TO_DATE
    other = api.analyze(tmp_path, "how does billing work", _WiringWithInvestigation())
    assert other.reason != api.UP_TO_DATE
    assert other.details["objective"] == "how does billing work"


def test_the_default_objective_has_its_own_analysis_key(tmp_path: Path) -> None:
    _repo(tmp_path)
    api.analyze(tmp_path, wiring=_WiringWithInvestigation())
    repeated = api.analyze(tmp_path, wiring=_WiringWithInvestigation())
    assert repeated.reason == api.UP_TO_DATE
    explicit = api.analyze(tmp_path, api.DEFAULT_OBJECTIVE, _WiringWithInvestigation())
    assert explicit.reason == api.UP_TO_DATE
    other = api.analyze(tmp_path, "how does renewal work", _WiringWithInvestigation())
    assert other.reason != api.UP_TO_DATE


def test_a_state_without_the_analysis_contract_fields_is_not_current(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    api.analyze(tmp_path, "how does renewal work", _WiringWithInvestigation())
    state_path = tmp_path / STATE_DIR_NAME / ANALYSIS_STATE_FILE
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    del payload["analyzed_objective_hash"]
    del payload["contract_version"]
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    report = api.analyze(tmp_path, "how does renewal work", _WiringWithInvestigation())
    assert report.reason != api.UP_TO_DATE


def test_a_contract_version_bump_reinvestigates(tmp_path: Path) -> None:
    _repo(tmp_path)
    api.analyze(tmp_path, "how does renewal work", _WiringWithInvestigation())
    state_path = tmp_path / STATE_DIR_NAME / ANALYSIS_STATE_FILE
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["contract_version"] = ANALYSIS_CONTRACT_VERSION + "0"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    report = api.analyze(tmp_path, "how does renewal work", _WiringWithInvestigation())
    assert report.reason != api.UP_TO_DATE
    assert report.status == "ok"


def test_analyze_reports_the_resolved_provider(tmp_path: Path) -> None:
    _repo(tmp_path)
    report = api.analyze(tmp_path, "how does renewal work", _WiringWithInvestigation())
    assert report.provider == "probe"
    assert report.to_dict()["provider"] == "probe"


def test_analyze_without_a_provider_reports_an_empty_provider(tmp_path: Path) -> None:
    _repo(tmp_path)
    report = api.analyze(tmp_path, wiring=Wiring())
    assert report.provider == ""
    assert report.to_dict()["provider"] == ""


def test_ingest_reports_the_resolved_provider(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    source = tmp_path / "notes.md"
    source.write_text("# renewal\n", encoding="utf-8")
    report = api.ingest(source, repo, _WiringWithProvider())
    assert report.provider == "probe"
    assert report.to_dict()["provider"] == "probe"


def test_ask_reports_the_resolved_provider(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    source = tmp_path / "notes.md"
    source.write_text("# renewal\n", encoding="utf-8")
    api.ingest(source, repo, Wiring())
    report = api.ask("what changed", repo, _WiringWithProvider())
    assert report.provider == "probe"
    assert report.to_dict()["provider"] == "probe"


def test_the_provider_preference_is_read_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    monkeypatch.setenv("WIKI_AI_PROVIDER", "absent")
    report = api.analyze(tmp_path, wiring=_WiringWithInvestigation())
    assert report.status == "blocked"
    assert report.reason == "agent_provider_unavailable"
    assert report.provider == ""


def test_an_explicit_wiring_preference_outranks_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    monkeypatch.setenv("WIKI_AI_PROVIDER", "absent")
    registry = ProviderRegistry()
    registry.register("probe", _Provider)
    report = api.provider_show(tmp_path, Wiring(registry, provider="probe"))
    assert report.provider == "probe"
    assert report.source == "argument"


def test_provider_set_persists_and_is_honoured(tmp_path: Path) -> None:
    _repo(tmp_path)
    registry = ProviderRegistry()
    registry.register("probe", _Provider)
    written = api.provider_set("probe", tmp_path, Wiring(registry))
    assert written.status == "ok"
    assert written.provider == "probe"
    shown = api.provider_show(tmp_path, Wiring(registry))
    assert shown.provider == "probe"
    assert shown.source == "preferences"
    assert "probe" in shown.registered


def test_provider_set_rejects_an_unregistered_name(tmp_path: Path) -> None:
    _repo(tmp_path)
    registry = ProviderRegistry()
    registry.register("probe", _Provider)
    report = api.provider_set("absent", tmp_path, Wiring(registry))
    assert report.status == "error"
    assert report.reason == "unknown_provider"
    assert report.registered == ("probe",)
    assert api.provider_show(tmp_path, Wiring(registry)).provider == ""


def test_the_stored_preference_selects_the_provider_used_by_analyze(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    registry = ProviderRegistry()
    registry.register("probe", _Provider)
    registry.register("aardvark", _Provider)
    api.provider_set("probe", tmp_path, Wiring(registry))

    class _Wired(Wiring):
        def investigation_runner(self) -> Any:
            return _Investigation()

    report = api.analyze(tmp_path, "how does renewal work", _Wired(registry))
    assert report.provider == "probe"
