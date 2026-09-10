from __future__ import annotations

import pytest

from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app.ports import (
    AnswerOutcome,
    CapabilityUnavailable,
    IngestionOutcome,
    IngestionRunner,
    InvestigationOutcome,
    InvestigationRunner,
    PublicationOutcome,
    PublicationRunner,
    QueryRunner,
)
from wiki_ai.app.wiring import Wiring, default_wiring

RUNNER_FACTORIES = (
    ("investigation_runner", "investigation_engine_unavailable"),
    ("ingestion_runner", "ingestion_pipeline_unavailable"),
    ("query_runner", "query_engine_unavailable"),
    ("publication_runner", "publication_engine_unavailable"),
)


@pytest.mark.parametrize("attribute,reason", RUNNER_FACTORIES)
def test_wiring_declares_every_capability_unavailable(attribute: str, reason: str) -> None:
    wiring = default_wiring()
    with pytest.raises(CapabilityUnavailable) as caught:
        getattr(wiring, attribute)()
    assert caught.value.reason == reason
    assert caught.value.action


def test_wiring_owns_a_provider_registry() -> None:
    registry = ProviderRegistry()
    wiring = Wiring(registry)
    assert wiring.registry is registry
    assert default_wiring().registry.available() == ()


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
    publication = PublicationOutcome(
        publication_id="p1", artifacts=("a.docx",), manifest_hash="0" * 64
    )
    assert publication.to_dict()["artifacts"] == ["a.docx"]
