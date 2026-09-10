from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from wiki_ai.knowledge import (
    CodeContent,
    CodeLocator,
    Confidence,
    Entity,
    EntityId,
    Evidence,
    KnowledgeRepository,
    KnowledgeState,
    Relation,
    SourceVersion,
    make_evidence,
)
from wiki_ai.knowledge.repository import DATABASE_FILENAME

CAPTURED = "2026-01-01T00:00:00+00:00"
CODE_SOURCE = "src_repo"
CODE_HASH = "codehash1"

CAPABILITY_EXCERPT = (
    "class Renovacao:\n"
    "    def renew(self, contrato):\n"
    "        resposta = self.billing_api.charge(contrato)\n"
    "        self.eventos.publish(RenewalCompleted(contrato.id))\n"
    "        return resposta\n"
)

RULE_EXCERPT = (
    "A renovacao nao aceita cliente com debito aberto. "
    "O limite e de 3 tentativas de cobranca por contrato."
)

BLANK_EXCERPT = ""

GRAPH_CAPABILITY_EXCERPT = (
    "class Renovacao:\n"
    "    def renew(self, contrato):\n"
    "        resposta = self.billing_api.charge(contrato)\n"
    "        self.eventos.publish('renewal-events', RenewalCompleted(contrato.id))\n"
    "        return resposta"
)

INFERRED_STATEMENT = "OrderService deletes every order after 7 retries"

INFERRED_EXCERPT = (
    "class OrderService:\n"
    "    def close(self, order):\n"
    "        self.repository.save(order)\n"
    "        return order\n"
)


@dataclass(frozen=True)
class GroundingGraph:
    repository: KnowledgeRepository
    nodes: Mapping[str, Entity]
    evidences: Mapping[str, Evidence]

    def id(self, name: str) -> EntityId:
        return self.nodes[name].id

    def evidence_id(self, name: str) -> str:
        return self.evidences[name].id


def _version() -> SourceVersion:
    return SourceVersion(
        source_id=CODE_SOURCE,
        version_hash=CODE_HASH,
        locator_root="/repo",
        captured_at=CAPTURED,
    )


def _evidence(path: str, symbol: str, excerpt: str) -> Evidence:
    return make_evidence(
        CODE_SOURCE,
        CODE_HASH,
        CodeLocator(
            path=path,
            line_start=1,
            line_end=40,
            symbol=symbol,
            content=CodeContent.EXECUTABLE,
        ),
        excerpt,
        CAPTURED,
    )


def _blank_evidence(path: str, symbol: str) -> Evidence:
    populated = _evidence(path, symbol, "conteudo capturado fora do store")
    return Evidence(
        id=populated.id,
        source_id=populated.source_id,
        version_hash=populated.version_hash,
        locator=populated.locator,
        excerpt_hash=populated.excerpt_hash,
        captured_at=populated.captured_at,
        excerpt=BLANK_EXCERPT,
    )


def build_with_excerpt(tmp_path, excerpt: str) -> GroundingGraph:
    return build(tmp_path, capability_excerpt=excerpt)


def enrich_capability_excerpt(graph) -> str:
    entity_id = graph.id("capability")
    stored = graph.repository.evidence_for(entity_id)[0]
    version = graph.code_version
    enriched = make_evidence(
        version.source_id,
        version.version_hash,
        stored.locator,
        GRAPH_CAPABILITY_EXCERPT,
        CAPTURED,
    )
    with graph.repository.begin_revision("pipeline", "excerpt real") as revision:
        revision.put_evidence(enriched, [entity_id])
    return enriched.id


def build(tmp_path, capability_excerpt: str = CAPABILITY_EXCERPT) -> GroundingGraph:
    repository = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    nodes = {
        "capability": Entity.create(
            kind="capability", name="Renovacao", stable_key="cap/renewal"
        ),
        "integration": Entity.create(
            kind="integration",
            name="Billing API",
            stable_key="int/billing",
            attributes={"direction": "outbound", "protocol": "http"},
        ),
        "rule": Entity.create(
            kind="business_rule",
            name="Elegibilidade de renovacao",
            stable_key="rule/eligibility",
            attributes={
                "statement": "cliente adimplente pode renovar",
                "conditions": ["sem debito"],
                "effects": ["renovacao aprovada"],
            },
        ),
        "silent": Entity.create(
            kind="capability", name="Cancelamento", stable_key="cap/cancel"
        ),
        "inferred": Entity.create(
            kind="business_rule",
            name="OrderService",
            stable_key="rule/order-service",
            attributes={
                "statement": INFERRED_STATEMENT,
                "conditions": ["after 7 retries"],
                "effects": ["deletes every order"],
            },
            state=KnowledgeState.DECLARED,
            confidence=Confidence.INFERRED,
        ),
    }
    evidences = {
        "capability": _evidence("src/renewal.py", "renew", capability_excerpt),
        "rule": _evidence("src/rules.py", "eligible", RULE_EXCERPT),
        "silent": _blank_evidence("src/cancel.py", "cancel"),
        "inferred": _evidence("src/orders.py", "close", INFERRED_EXCERPT),
    }
    with repository.begin_revision("pipeline", "grafo de grounding") as revision:
        revision.put_source_version(_version())
        for node in nodes.values():
            revision.put_entity(node)
        revision.put_relation(
            Relation.create(
                "calls", nodes["capability"].id, nodes["integration"].id
            )
        )
        revision.put_relation(
            Relation.create("belongs_to", nodes["rule"].id, nodes["capability"].id)
        )
        revision.put_evidence(evidences["capability"], [nodes["capability"].id])
        revision.put_evidence(evidences["rule"], [nodes["rule"].id])
        revision.put_evidence(evidences["silent"], [nodes["silent"].id])
        revision.put_evidence(evidences["inferred"], [nodes["inferred"].id])
    return GroundingGraph(
        repository=repository, nodes=nodes, evidences=evidences
    )
