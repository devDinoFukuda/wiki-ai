from __future__ import annotations

import pytest

from wiki_ai.knowledge import Entity, KnowledgeRepository
from wiki_ai.knowledge.correlation import (
    BASIS_CROSS_OWNER,
    CORRELATED_BY,
    CROSS_OWNER_PENALTY,
    SAME_OWNER,
    SCORE,
    correlate,
)
from wiki_ai.knowledge.matching import score_names
from wiki_ai.knowledge.model import Confidence, KnowledgeState
from wiki_ai.knowledge.repository import DATABASE_FILENAME
from wiki_ai.knowledge.taxonomy import RelationKind

NAMESPACE = "acme"


@pytest.fixture()
def repository(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    yield repo
    repo.close()


def capability(name: str) -> Entity:
    return Entity.owned(
        NAMESPACE,
        "capability",
        name,
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.INFERRED,
    )


def rule(name: str, owner: Entity, statement: str) -> Entity:
    return Entity.owned(
        NAMESPACE,
        "business_rule",
        name,
        owner=owner,
        attributes={
            "statement": statement,
            "conditions": ["contrato ativo"],
            "effects": ["libera"],
        },
        state=KnowledgeState.IMPLEMENTED,
        confidence=Confidence.INFERRED,
    )


def requirement(name: str, owner: Entity | None = None) -> Entity:
    return Entity.owned(
        NAMESPACE,
        "requirement",
        name,
        owner=owner,
        attributes={"statement": f"o sistema deve tratar {name}"},
        state=KnowledgeState.DECLARED,
        confidence=Confidence.INFERRED,
    )


def relation_between(repository, kind, source, target):
    for item in repository.find_relations(kind.value):
        if item.source_id == source.id and item.target_id == target.id:
            return item
    return None


def test_two_rules_named_alike_under_different_owners_stay_distinct(repository):
    renewal = capability("Renewal")
    billing = capability("Billing")
    renewal_rule = rule("Eligibility", renewal, "Renovacao exige contrato ativo")
    billing_rule = rule("Eligibility", billing, "Cobranca exige cartao valido")
    with repository.begin_revision("pipeline", "carga") as revision:
        for entity in (renewal, billing, renewal_rule, billing_rule):
            revision.put_entity(entity)
    assert renewal_rule.id != billing_rule.id
    assert repository.entity_count() == 4


def test_correlation_matches_a_requirement_to_the_rule_of_the_same_owner(repository):
    renewal = capability("Renewal")
    renewal_rule = rule("Eligibility", renewal, "Renovacao exige contrato ativo")
    demand = requirement("Eligibility", renewal)
    with repository.begin_revision("pipeline", "carga") as revision:
        for entity in (renewal, renewal_rule, demand):
            revision.put_entity(entity)
    correlate(repository, NAMESPACE)
    declared = relation_between(
        repository, RelationKind.DECLARES, demand, renewal_rule
    )
    assert declared is not None
    assert declared.attributes[SAME_OWNER] is True


def test_a_cross_owner_match_is_correlated_with_a_lower_score(repository):
    renewal = capability("Renewal")
    billing = capability("Billing")
    billing_rule = rule("Eligibility", billing, "Cobranca exige cartao valido")
    demand = requirement("Eligibility", renewal)
    with repository.begin_revision("pipeline", "carga") as revision:
        for entity in (renewal, billing, billing_rule, demand):
            revision.put_entity(entity)
    correlate(repository, NAMESPACE)
    declared = relation_between(
        repository, RelationKind.DECLARES, demand, billing_rule
    )
    assert declared is not None
    assert declared.attributes[SAME_OWNER] is False
    assert declared.attributes[CORRELATED_BY] == BASIS_CROSS_OWNER
    assert declared.attributes[SCORE] == pytest.approx(
        round(score_names("Eligibility", "eligibility") * CROSS_OWNER_PENALTY, 4)
    )


def test_the_same_owner_wins_over_a_cross_owner_candidate(repository):
    renewal = capability("Renewal")
    billing = capability("Billing")
    renewal_rule = rule("Eligibility", renewal, "Renovacao exige contrato ativo")
    billing_rule = rule("Eligibility", billing, "Cobranca exige cartao valido")
    demand = requirement("Eligibility", renewal)
    with repository.begin_revision("pipeline", "carga") as revision:
        for entity in (renewal, billing, renewal_rule, billing_rule, demand):
            revision.put_entity(entity)
    correlate(repository, NAMESPACE)
    assert (
        relation_between(repository, RelationKind.DECLARES, demand, renewal_rule)
        is not None
    )
    assert (
        relation_between(repository, RelationKind.DECLARES, demand, billing_rule)
        is None
    )


def test_a_cross_owner_match_below_the_penalized_threshold_is_dropped(repository):
    renewal = capability("Renewal")
    billing = capability("Billing")
    billing_rule = rule("Eligibility", billing, "Cobranca exige cartao valido")
    demand = requirement("Eligibility", renewal)
    with repository.begin_revision("pipeline", "carga") as revision:
        for entity in (renewal, billing, billing_rule, demand):
            revision.put_entity(entity)
    correlate(repository, NAMESPACE, threshold=0.9)
    assert (
        relation_between(repository, RelationKind.DECLARES, demand, billing_rule)
        is None
    )


def test_correlation_matches_on_the_canonical_name_not_the_raw_spelling(repository):
    renewal = capability("Renewal")
    renewal_rule = rule("Elegibilidade Ativa", renewal, "Renovacao exige contrato")
    demand = requirement("ELEGIBILIDADE-ATIVA", renewal)
    with repository.begin_revision("pipeline", "carga") as revision:
        for entity in (renewal, renewal_rule, demand):
            revision.put_entity(entity)
    correlate(repository, NAMESPACE)
    declared = relation_between(
        repository, RelationKind.DECLARES, demand, renewal_rule
    )
    assert declared is not None
    assert declared.attributes[SCORE] == pytest.approx(1.0)
