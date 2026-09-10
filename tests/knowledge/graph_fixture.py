from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from wiki_ai.knowledge import (
    CodeContent,
    CodeLocator,
    Confidence,
    DocumentLocator,
    Entity,
    EntityId,
    EpistemicStatus,
    KnowledgeRepository,
    Relation,
    SourceVersion,
    make_evidence,
)
from wiki_ai.knowledge.repository import DATABASE_FILENAME

CAPTURED = "2026-01-01T00:00:00+00:00"
CODE_SOURCE = "src_repo"
DOC_SOURCE = "src_doc"
CODE_HASH = "codehash1"
DOC_HASH = "dochash1"


@dataclass(frozen=True)
class Graph:
    repository: KnowledgeRepository
    nodes: Mapping[str, Entity]
    code_version: SourceVersion
    doc_version: SourceVersion

    def id(self, name: str) -> EntityId:
        return self.nodes[name].id


def code_version() -> SourceVersion:
    return SourceVersion(
        source_id=CODE_SOURCE,
        version_hash=CODE_HASH,
        locator_root="/repo",
        captured_at=CAPTURED,
    )


def doc_version() -> SourceVersion:
    return SourceVersion(
        source_id=DOC_SOURCE,
        version_hash=DOC_HASH,
        locator_root="/docs",
        captured_at=CAPTURED,
    )


def code_evidence(version: SourceVersion, path: str, symbol: str | None = None):
    locator = CodeLocator(
        path=path,
        line_start=1,
        line_end=40,
        symbol=symbol,
        content=CodeContent.EXECUTABLE,
    )
    return make_evidence(
        version.source_id, version.version_hash, locator, f"codigo:{path}", CAPTURED
    )


def doc_evidence(version: SourceVersion, block: str):
    return make_evidence(
        version.source_id,
        version.version_hash,
        DocumentLocator(block_id=block),
        f"texto:{block}",
        CAPTURED,
    )


def entity(
    key: str,
    kind: str,
    name: str,
    attributes: Mapping[str, Any] | None = None,
    epistemic: EpistemicStatus = EpistemicStatus.IMPLEMENTED,
    confidence: Confidence = Confidence.INFERRED,
) -> Entity:
    return Entity.create(
        kind=kind,
        name=name,
        stable_key=key,
        attributes=dict(attributes or {}),
        epistemic=epistemic,
        confidence=confidence,
    )


def catalog() -> dict[str, Entity]:
    return {
        "system": entity("sys/renewal", "system", "Plataforma"),
        "module": entity("mod/billing", "module", "Billing"),
        "capability": entity("cap/renewal", "capability", "Renovacao"),
        "entry_point": entity(
            "ep/post-renewal",
            "entry_point",
            "POST /renewals",
            {"mechanism": "http", "location": "POST /renewals"},
        ),
        "flow": entity("flow/renewal", "flow", "Fluxo de renovacao"),
        "step_validate": entity(
            "step/validate", "flow_step", "Validar payload", {"ordinal": 1}
        ),
        "step_rule": entity(
            "step/eligibility", "flow_step", "Checar elegibilidade", {"ordinal": 2}
        ),
        "step_persist": entity(
            "step/persist", "flow_step", "Gravar renovacao", {"ordinal": 3}
        ),
        "rule": entity(
            "rule/eligibility",
            "business_rule",
            "Elegibilidade de renovacao",
            {
                "statement": "cliente adimplente pode renovar",
                "conditions": ["sem debito"],
                "effects": ["renovacao aprovada"],
            },
        ),
        "table": entity("tbl/renewals", "table", "renewals", {"schema": "public"}),
        "integration": entity(
            "int/billing",
            "integration",
            "Billing API",
            {"direction": "outbound", "protocol": "http"},
        ),
        "topic": entity("topic/renewal", "topic", "renewal-events"),
        "event": entity("evt/renewed", "event", "RenewalCompleted"),
        "failure": entity(
            "fail/billing-timeout",
            "failure_mode",
            "Timeout no billing",
            {"trigger": "billing lento", "effect": "renovacao pendente"},
        ),
        "retry": entity(
            "retry/billing", "retry_policy", "Retry billing", {"attempts": 3}
        ),
        "fallback": entity(
            "fb/manual", "fallback", "Fila manual", {"behaviour": "enfileira revisao"}
        ),
        "test": entity(
            "test/renewal-happy",
            "test_scenario",
            "Renovacao feliz",
            {"scenario": "cliente adimplente renova"},
            epistemic=EpistemicStatus.IMPLEMENTED,
        ),
        "edge_case": entity(
            "edge/no-history",
            "edge_case",
            "Cliente sem historico",
            {"condition": "sem historico", "expected": "recusa"},
        ),
        "state_active": entity("state/active", "state", "Ativa"),
        "state_renewed": entity("state/renewed", "state", "Renovada"),
        "input": entity("in/renewal-request", "input", "RenewalRequest"),
        "output": entity("out/renewal-response", "output", "RenewalResponse"),
        "requirement": entity(
            "req/auto-renewal",
            "requirement",
            "Renovacao automatica",
            {"statement": "sistema deve renovar automaticamente"},
            epistemic=EpistemicStatus.DECLARED,
        ),
        "capability_declared": entity(
            "cap/auto-renewal",
            "capability",
            "Renovacao automatica",
            epistemic=EpistemicStatus.DECLARED,
        ),
        "proposal": entity(
            "prop/salesforce",
            "proposal",
            "Migrar renovacao para Salesforce",
            {"statement": "mover renovacao para Salesforce"},
            epistemic=EpistemicStatus.PROPOSED,
        ),
        "proposal_old": entity(
            "prop/inhouse",
            "proposal",
            "Manter renovacao interna",
            {"statement": "manter renovacao interna"},
            epistemic=EpistemicStatus.HISTORICAL,
        ),
        "decision": entity(
            "adr/001",
            "decision_record",
            "ADR 001",
            {"decision": "adotar Salesforce"},
            epistemic=EpistemicStatus.PROPOSED,
        ),
        "source_a": entity(
            "src/manual-a", "source", "Manual v1", {"origin": "sharepoint"},
            epistemic=EpistemicStatus.DECLARED,
        ),
        "source_b": entity(
            "src/manual-b", "source", "Manual v2", {"origin": "sharepoint"},
            epistemic=EpistemicStatus.DECLARED,
        ),
        "gap": entity(
            "gap/renewal-window",
            "gap",
            "Qual a janela de renovacao?",
            {"question": "Qual a janela de renovacao?", "blocking": True, "status": "open"},
            epistemic=EpistemicStatus.DECLARED,
            confidence=Confidence.UNRESOLVED,
        ),
        "gap_soft": entity(
            "gap/owner",
            "gap",
            "Quem e o dono?",
            {"question": "Quem e o dono?", "blocking": False, "status": "open"},
            epistemic=EpistemicStatus.DECLARED,
            confidence=Confidence.UNRESOLVED,
        ),
        "dependency": entity(
            "dep/billing-sdk", "dependency", "billing-sdk", {"identifier": "billing-sdk@2"}
        ),
    }


EDGES: tuple[tuple[str, str, str], ...] = (
    ("belongs_to", "module", "system"),
    ("belongs_to", "capability", "module"),
    ("belongs_to", "entry_point", "capability"),
    ("belongs_to", "flow", "capability"),
    ("belongs_to", "rule", "capability"),
    ("belongs_to", "integration", "capability"),
    ("belongs_to", "failure", "capability"),
    ("belongs_to", "retry", "capability"),
    ("belongs_to", "fallback", "capability"),
    ("belongs_to", "edge_case", "capability"),
    ("belongs_to", "state_active", "capability"),
    ("belongs_to", "state_renewed", "capability"),
    ("belongs_to", "input", "capability"),
    ("belongs_to", "output", "capability"),
    ("belongs_to", "event", "capability"),
    ("belongs_to", "dependency", "capability"),
    ("belongs_to", "step_validate", "flow"),
    ("belongs_to", "step_rule", "flow"),
    ("belongs_to", "step_persist", "flow"),
    ("belongs_to", "topic", "integration"),
    ("triggers", "entry_point", "flow"),
    ("triggers", "step_validate", "step_rule"),
    ("triggers", "step_rule", "step_persist"),
    ("validates", "rule", "step_rule"),
    ("persists_to", "step_persist", "table"),
    ("writes", "capability", "table"),
    ("calls", "capability", "integration"),
    ("publishes", "capability", "event"),
    ("publishes", "integration", "topic"),
    ("consumes", "capability", "input"),
    ("publishes", "capability", "output"),
    ("handles", "capability", "failure"),
    ("handles", "capability", "edge_case"),
    ("retries", "retry", "integration"),
    ("falls_back_to", "capability", "fallback"),
    ("transitions_to", "state_active", "state_renewed"),
    ("tests", "test", "capability"),
    ("tests", "test", "rule"),
    ("depends_on", "capability", "dependency"),
    ("depends_on", "module", "capability"),
    ("affects", "gap", "capability"),
    ("affects", "gap_soft", "capability"),
    ("declares", "requirement", "capability_declared"),
    ("proposes_change_to", "proposal", "rule"),
    ("supersedes", "decision", "proposal_old"),
    ("contradicts", "source_a", "source_b"),
)


def build(tmp_path) -> Graph:
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    nodes = catalog()
    code = code_version()
    docs = doc_version()
    with repo.begin_revision("pipeline", "grafo fixture") as revision:
        revision.put_source_version(code)
        revision.put_source_version(docs)
        for node in nodes.values():
            revision.put_entity(node)
        for kind, source, target in EDGES:
            revision.put_relation(
                Relation.create(kind, nodes[source].id, nodes[target].id)
            )
        revision.put_evidence(
            code_evidence(code, "src/renewal.py", "renew"), [nodes["capability"].id]
        )
        revision.put_evidence(
            code_evidence(code, "src/rules.py", "eligible"), [nodes["rule"].id]
        )
        revision.put_evidence(
            doc_evidence(docs, "b-requirement"), [nodes["requirement"].id]
        )
    return Graph(repository=repo, nodes=nodes, code_version=code, doc_version=docs)
