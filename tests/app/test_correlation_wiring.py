from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_end_to_end import (
    OBJECTIVE,
    _document_registry,
    _registry,
    _update_script,
    _UpdateProvider,
)
from tests.repository.fixtures_repos import java_repo
from tests.ingestion import fixtures
from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app import api
from wiki_ai.app.api import CORRELATION_DETAIL
from wiki_ai.app.commands import EXIT_OK, main
from wiki_ai.app.session import Session
from wiki_ai.app.wiring import Wiring
from wiki_ai.knowledge.taxonomy import RelationKind

DECISION = {
    "type": "decision_record",
    "subject": "place order",
    "statement": "Decidimos manter place order como a capacidade central de pedidos",
    "attributes": {"decision": "place order permanece a capacidade central"},
}

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

SERVICE = "src/main/java/com/acme/order/OrderService.java"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    java_repo(root)
    return root


def _relations(repo: Path, kind: RelationKind) -> tuple[Any, ...]:
    with Session.open(repo).open_knowledge() as knowledge:
        return tuple(knowledge.find_relations(kind.value))


def test_analyze_reports_a_correlation_block_without_internal_ids(repo: Path) -> None:
    report = api.analyze(repo, OBJECTIVE, registry=_registry())
    assert report.status == "ok"
    correlation = report.details[CORRELATION_DETAIL]
    assert set(correlation) == {"relations", "gaps", "counts", "threshold"}
    for item in correlation["relations"]:
        assert set(item) == {"kind", "basis", "score"}


def test_analyze_without_entities_does_not_report_correlation(repo: Path) -> None:
    report = api.analyze(repo, OBJECTIVE, wiring=Wiring(ProviderRegistry()))
    assert report.status == "blocked"
    assert CORRELATION_DETAIL not in report.details


def test_ingest_with_entities_correlates_the_document_against_the_code(
    repo: Path, tmp_path: Path
) -> None:
    api.analyze(repo, OBJECTIVE, registry=_registry())
    source = tmp_path / "inception.vtt"
    source.write_text(fixtures.VTT, encoding="utf-8")
    report = api.ingest(source, repo, registry=_document_registry(source, DECISION))
    assert report.status == "ok"
    assert report.details["entities_written"] == 1
    correlation = report.details[CORRELATION_DETAIL]
    assert correlation["counts"].get(RelationKind.DECLARES.value, 0) >= 1
    assert _relations(repo, RelationKind.DECLARES)


def test_ingest_without_entities_does_not_report_correlation(
    repo: Path, tmp_path: Path
) -> None:
    source = tmp_path / "reuniao.vtt"
    source.write_text(fixtures.VTT, encoding="utf-8")
    report = api.ingest(source, repo, wiring=Wiring(ProviderRegistry()))
    assert report.status == "ok"
    assert CORRELATION_DETAIL not in report.details


def test_a_repeated_ingest_keeps_the_same_correlated_relations(
    repo: Path, tmp_path: Path
) -> None:
    api.analyze(repo, OBJECTIVE, registry=_registry())
    source = tmp_path / "inception.vtt"
    source.write_text(fixtures.VTT, encoding="utf-8")
    first = api.ingest(source, repo, registry=_document_registry(source, DECISION))
    before = {relation.id for relation in _relations(repo, RelationKind.DECLARES)}
    second = api.ingest(source, repo, registry=_document_registry(source, DECISION))
    after = {relation.id for relation in _relations(repo, RelationKind.DECLARES)}
    assert first.details[CORRELATION_DETAIL] == second.details[CORRELATION_DETAIL]
    assert before == after


def test_a_repeated_analyze_over_the_same_snapshot_keeps_correlation_stable(
    repo: Path,
) -> None:
    registry = _registry()
    api.analyze(repo, OBJECTIVE, registry=registry)
    with Session.open(repo).open_knowledge() as knowledge:
        before = knowledge.relation_count()
    repeated = api.analyze(repo, OBJECTIVE, registry=registry)
    assert repeated.reason == "up_to_date"
    with Session.open(repo).open_knowledge() as knowledge:
        assert knowledge.relation_count() == before


def test_an_update_that_writes_entities_reports_correlation(repo: Path) -> None:
    api.analyze(repo, OBJECTIVE, registry=_registry())
    (repo / SERVICE).write_text(_CHANGED_SERVICE, encoding="utf-8")
    registry = ProviderRegistry()
    registry.register("scripted", lambda: _UpdateProvider(scripts=[_update_script()]))
    report = api.analyze(repo, OBJECTIVE, registry=registry)
    assert report.status == "ok"
    assert report.details["reinvestigated"]["entities_written"] > 0
    assert CORRELATION_DETAIL in report.details


def test_an_update_without_a_provider_does_not_report_correlation(repo: Path) -> None:
    api.analyze(repo, OBJECTIVE, registry=_registry())
    (repo / SERVICE).write_text(_CHANGED_SERVICE, encoding="utf-8")
    report = api.analyze(repo, OBJECTIVE, wiring=Wiring(ProviderRegistry()))
    assert report.status == "blocked"
    assert CORRELATION_DETAIL not in report.details


def test_the_command_line_reports_correlation_without_internal_ids(repo: Path) -> None:
    stream = io.StringIO()
    code = main(
        ["analyze", str(repo), "--objective", OBJECTIVE],
        stream=stream,
        registry=_registry(),
    )
    payload = json.loads(stream.getvalue())
    assert code == EXIT_OK
    correlation = payload["details"][CORRELATION_DETAIL]
    assert set(correlation) == {"relations", "gaps", "counts", "threshold"}
    text = json.dumps(correlation, ensure_ascii=False)
    for word in ("ent_", "rel_", "ev_", "srv_", "rev_"):
        assert word not in text
