from __future__ import annotations

from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app.ports import (
    CapabilityUnavailable,
    IngestionRunner,
    InvestigationRunner,
    PublicationRunner,
    QueryRunner,
)

__all__ = [
    "INVESTIGATION_UNAVAILABLE",
    "INGESTION_UNAVAILABLE",
    "QUERY_UNAVAILABLE",
    "PUBLICATION_UNAVAILABLE",
    "Wiring",
    "default_wiring",
]

INVESTIGATION_UNAVAILABLE = (
    "investigation_engine_unavailable",
    "install a build that provides deep analysis",
)
INGESTION_UNAVAILABLE = (
    "ingestion_pipeline_unavailable",
    "install a build that reads this document format",
)
QUERY_UNAVAILABLE = (
    "query_engine_unavailable",
    "install a build that answers questions",
)
PUBLICATION_UNAVAILABLE = (
    "publication_engine_unavailable",
    "install a build that produces publications",
)


class Wiring:
    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self._registry = registry if registry is not None else ProviderRegistry()

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    def investigation_runner(self) -> InvestigationRunner:
        raise CapabilityUnavailable(*INVESTIGATION_UNAVAILABLE)

    def ingestion_runner(self) -> IngestionRunner:
        raise CapabilityUnavailable(*INGESTION_UNAVAILABLE)

    def query_runner(self) -> QueryRunner:
        raise CapabilityUnavailable(*QUERY_UNAVAILABLE)

    def publication_runner(self) -> PublicationRunner:
        raise CapabilityUnavailable(*PUBLICATION_UNAVAILABLE)


def default_wiring() -> Wiring:
    return Wiring()
