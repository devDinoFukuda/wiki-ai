from __future__ import annotations

import pytest

from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app.ports import (
    AnswerOutcome,
    IngestionOutcome,
    IngestionRunner,
    InvestigationOutcome,
    InvestigationRunner,
    PublicationOutcome,
    PublicationRunner,
    QueryRunner,
    UpdateRunner,
)
from wiki_ai.app.wiring import Wiring, default_wiring

WIRED_RUNNERS = (
    ("investigation_runner", InvestigationRunner),
    ("ingestion_runner", IngestionRunner),
    ("update_runner", UpdateRunner),
    ("query_runner", QueryRunner),
    ("publication_runner", PublicationRunner),
)


@pytest.mark.parametrize("attribute,protocol", WIRED_RUNNERS)
def test_wiring_provides_every_wired_capability(attribute: str, protocol: type) -> None:
    runner = getattr(default_wiring(), attribute)()
    assert isinstance(runner, protocol)


def test_wiring_owns_a_provider_registry() -> None:
    registry = ProviderRegistry()
    wiring = Wiring(registry)
    assert wiring.registry is registry
    assert set(default_wiring().registry.registered()) >= {"claude", "codex"}


def test_runner_protocols_are_runtime_checkable() -> None:
    class _Anything:
        def run(self, *args: object, **kwargs: object) -> None:
            return None

    candidate = _Anything()
    assert isinstance(candidate, InvestigationRunner)
    assert isinstance(candidate, IngestionRunner)
    assert isinstance(candidate, QueryRunner)
    assert isinstance(candidate, PublicationRunner)
    assert not isinstance(object(), InvestigationRunner)


def test_outcomes_are_serializable() -> None:
    investigation = InvestigationOutcome(
        objective="goal",
        entities_written=2,
        relations_written=1,
        evidence_written=3,
        unresolved=("q",),
    )
    assert investigation.to_dict()["evidence_written"] == 3
    ingestion = IngestionOutcome(
        source_id="s", version_hash="h", blocks=4, entities_written=1
    )
    assert ingestion.to_dict()["blocks"] == 4
    answer = AnswerOutcome(question="q", answer="a", evidence_ids=("e1",))
    assert answer.to_dict()["evidence_ids"] == ["e1"]
    assert answer.to_dict()["status"] == "answered"
    blocked = AnswerOutcome(question="q", answer="", status="blocked")
    assert blocked.to_dict()["status"] == "blocked"
    publication = PublicationOutcome(
        publication_id="p1", artifacts=("a.docx",), manifest_hash="0" * 64
    )
    assert publication.to_dict()["artifacts"] == ["a.docx"]
