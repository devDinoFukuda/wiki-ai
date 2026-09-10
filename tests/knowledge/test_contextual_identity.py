from __future__ import annotations

import pytest

from wiki_ai.knowledge import (
    Entity,
    EntityId,
    IdentityCollision,
    KnowledgeRepository,
    KnowledgeState,
    canonical_name,
    contextual_key,
    entity_id,
)
from wiki_ai.knowledge.identity import EXPLICIT_PREFIX, GLOBAL_OWNER, KEY_SEPARATOR
from wiki_ai.knowledge.errors import InvalidIdentity
from wiki_ai.knowledge.repository import DATABASE_FILENAME

RULE_KIND = "business_rule"
CAPABILITY_KIND = "capability"
NAMESPACE = "acme"


def rule_attributes(statement: str) -> dict[str, object]:
    return {
        "statement": statement,
        "conditions": ["contrato ativo"],
        "effects": ["libera"],
    }


@pytest.fixture()
def repository(tmp_path):
    repo = KnowledgeRepository.open(str(tmp_path / DATABASE_FILENAME))
    yield repo
    repo.close()


def owner_entity(name: str) -> Entity:
    return Entity.owned(NAMESPACE, CAPABILITY_KIND, name)


def test_flat_stable_key_makes_two_owners_share_one_id():
    first = Entity.create(
        kind=RULE_KIND,
        name="Eligibility",
        stable_key=f"{RULE_KIND}::eligibility",
        attributes=rule_attributes("Renovacao exige contrato ativo"),
    )
    second = Entity.create(
        kind=RULE_KIND,
        name="Eligibility",
        stable_key=f"{RULE_KIND}::eligibility",
        attributes=rule_attributes("Cobranca exige cartao valido"),
    )
    assert first.id == second.id


def test_flat_stable_key_lets_the_second_rule_overwrite_the_first(repository):
    renewal = Entity.create(
        kind=RULE_KIND,
        name="Eligibility",
        stable_key=f"{RULE_KIND}::eligibility",
        attributes=rule_attributes("Renovacao exige contrato ativo"),
    )
    billing = Entity.create(
        kind=RULE_KIND,
        name="Eligibility",
        stable_key=f"{RULE_KIND}::eligibility",
        attributes=rule_attributes("Cobranca exige cartao valido"),
    )
    with repository.begin_revision("pipeline", "renewal") as revision:
        revision.put_entity(renewal)
    with repository.begin_revision("pipeline", "billing") as revision:
        revision.put_entity(billing)
    stored = repository.get_entity(renewal.id)
    assert repository.entity_count() == 1
    assert stored is not None
    assert stored.attributes["statement"] == "Cobranca exige cartao valido"


def test_contextual_key_separates_the_same_name_under_two_owners():
    renewal = contextual_key(NAMESPACE, RULE_KIND, "Renewal", "Eligibility")
    billing = contextual_key(NAMESPACE, RULE_KIND, "Billing", "Eligibility")
    assert renewal != billing
    assert entity_id(RULE_KIND, renewal) != entity_id(RULE_KIND, billing)


def test_contextual_keys_of_distinct_owners_survive_the_same_repository(repository):
    renewal_owner = owner_entity("Renewal")
    billing_owner = owner_entity("Billing")
    renewal_rule = Entity.owned(
        NAMESPACE,
        RULE_KIND,
        "Eligibility",
        owner=renewal_owner,
        attributes=rule_attributes("Renovacao exige contrato ativo"),
    )
    billing_rule = Entity.owned(
        NAMESPACE,
        RULE_KIND,
        "Eligibility",
        owner=billing_owner,
        attributes=rule_attributes("Cobranca exige cartao valido"),
    )
    with repository.begin_revision("pipeline", "ambas") as revision:
        revision.put_entity(renewal_owner)
        revision.put_entity(billing_owner)
        revision.put_entity(renewal_rule)
        revision.put_entity(billing_rule)
    assert repository.entity_count() == 4
    assert repository.get_entity(renewal_rule.id).attributes["statement"] == (
        "Renovacao exige contrato ativo"
    )
    assert repository.get_entity(billing_rule.id).attributes["statement"] == (
        "Cobranca exige cartao valido"
    )


def test_contextual_key_normalizes_case_accents_and_punctuation():
    assert contextual_key(NAMESPACE, RULE_KIND, "Renewal", "Elegibilidade  Ativa!") == (
        contextual_key(NAMESPACE, RULE_KIND, "renewal", "ELEGIBILIDADE-ativa")
    )
    assert contextual_key(NAMESPACE, RULE_KIND, None, "Restrição") == (
        contextual_key(NAMESPACE, RULE_KIND, "", "restricao")
    )


def test_contextual_key_is_deterministic_across_calls():
    first = contextual_key(NAMESPACE, RULE_KIND, "Renewal", "Eligibility")
    second = contextual_key(NAMESPACE, RULE_KIND, "Renewal", "Eligibility")
    assert first == second


def test_contextual_key_without_owner_uses_the_global_anchor():
    key = contextual_key(NAMESPACE, RULE_KIND, None, "Eligibility")
    assert key.split(KEY_SEPARATOR) == [NAMESPACE, RULE_KIND, GLOBAL_OWNER, "eligibility"]


def test_contextual_key_separates_namespaces():
    assert contextual_key("acme", RULE_KIND, "Renewal", "Eligibility") != (
        contextual_key("other", RULE_KIND, "Renewal", "Eligibility")
    )


def test_contextual_key_separates_kinds():
    assert contextual_key(NAMESPACE, RULE_KIND, "Renewal", "Eligibility") != (
        contextual_key(NAMESPACE, "invariant", "Renewal", "Eligibility")
    )


def test_explicit_id_prevails_over_namespace_kind_owner_and_name():
    left = contextual_key(NAMESPACE, RULE_KIND, "Renewal", "Eligibility", "RULE-17")
    right = contextual_key("other", "invariant", "Billing", "Outro nome", "RULE-17")
    assert left == right == f"{EXPLICIT_PREFIX}{KEY_SEPARATOR}RULE-17"


def test_explicit_id_is_trimmed_before_prevailing():
    assert contextual_key(NAMESPACE, RULE_KIND, "Renewal", "X", "  RULE-17  ") == (
        contextual_key(NAMESPACE, RULE_KIND, "Renewal", "X", "RULE-17")
    )


def test_blank_explicit_id_falls_back_to_the_contextual_parts():
    assert contextual_key(NAMESPACE, RULE_KIND, "Renewal", "Eligibility", "   ") == (
        contextual_key(NAMESPACE, RULE_KIND, "Renewal", "Eligibility")
    )


def test_contextual_key_requires_namespace_kind_and_name():
    with pytest.raises(InvalidIdentity):
        contextual_key("  ", RULE_KIND, "Renewal", "Eligibility")
    with pytest.raises(InvalidIdentity):
        contextual_key(NAMESPACE, "  ", "Renewal", "Eligibility")
    with pytest.raises(InvalidIdentity):
        contextual_key(NAMESPACE, RULE_KIND, "Renewal", "  ")


def test_contextual_key_rejects_a_name_without_alphanumerics():
    with pytest.raises(InvalidIdentity):
        contextual_key(NAMESPACE, RULE_KIND, "Renewal", "---")


def test_canonical_name_folds_accents_case_and_separators():
    assert canonical_name("Renovação  de-Contrato!") == "renovacao_de_contrato"
    assert canonical_name("") == ""


def test_entity_owned_records_the_owner_id():
    owner = owner_entity("Renewal")
    rule = Entity.owned(
        NAMESPACE,
        RULE_KIND,
        "Eligibility",
        owner=owner,
        attributes=rule_attributes("S"),
    )
    assert rule.owner_id == owner.id


def test_entity_without_owner_has_no_owner_id():
    assert Entity.owned(NAMESPACE, CAPABILITY_KIND, "Renewal").owner_id is None


def test_owner_id_round_trips_through_the_repository(repository):
    owner = owner_entity("Renewal")
    rule = Entity.owned(
        NAMESPACE,
        RULE_KIND,
        "Eligibility",
        owner=owner,
        attributes=rule_attributes("S"),
    )
    with repository.begin_revision("pipeline", "grava") as revision:
        revision.put_entity(owner)
        revision.put_entity(rule)
    stored = repository.get_entity(rule.id)
    assert stored is not None
    assert stored.owner_id == owner.id
    assert repository.get_entity(owner.id).owner_id is None


def test_owner_id_survives_reopening_the_database(tmp_path):
    path = str(tmp_path / DATABASE_FILENAME)
    owner = owner_entity("Renewal")
    rule = Entity.owned(
        NAMESPACE,
        RULE_KIND,
        "Eligibility",
        owner=owner,
        attributes=rule_attributes("S"),
    )
    with KnowledgeRepository.open(path) as repo:
        with repo.begin_revision("pipeline", "grava") as revision:
            revision.put_entity(owner)
            revision.put_entity(rule)
    with KnowledgeRepository.open(path) as reopened:
        assert reopened.get_entity(rule.id).owner_id == owner.id


def test_entity_to_dict_and_from_dict_round_trip_the_owner():
    owner = owner_entity("Renewal")
    rule = Entity.owned(
        NAMESPACE,
        RULE_KIND,
        "Eligibility",
        owner=owner,
        attributes=rule_attributes("S"),
        state=KnowledgeState.IMPLEMENTED,
    )
    payload = rule.to_dict()
    assert payload["owner_id"] == owner.id.value
    assert Entity.from_dict(payload) == rule


def test_entity_to_dict_reports_no_owner_as_null():
    payload = owner_entity("Renewal").to_dict()
    assert payload["owner_id"] is None
    assert Entity.from_dict(payload).owner_id is None


def test_entity_canonical_name_follows_identity_normalization():
    assert owner_entity("Renovação Ativa").canonical_name == "renovacao_ativa"


def test_put_entity_raises_identity_collision_on_a_different_owner(repository):
    renewal_owner = owner_entity("Renewal")
    billing_owner = owner_entity("Billing")
    rule = Entity.owned(
        NAMESPACE,
        RULE_KIND,
        "Eligibility",
        owner=renewal_owner,
        attributes=rule_attributes("Renovacao"),
    )
    impostor = Entity(
        id=rule.id,
        kind=rule.kind,
        name=rule.name,
        attributes=rule_attributes("Cobranca"),
        owner_id=billing_owner.id,
    )
    with repository.begin_revision("pipeline", "primeira") as revision:
        revision.put_entity(renewal_owner)
        revision.put_entity(billing_owner)
        revision.put_entity(rule)
    with pytest.raises(IdentityCollision) as failure:
        with repository.begin_revision("pipeline", "colide") as revision:
            revision.put_entity(impostor)
    assert failure.value.existing.owner_id == renewal_owner.id.value
    assert failure.value.incoming.owner_id == billing_owner.id.value


def test_put_entity_raises_identity_collision_on_a_different_kind(repository):
    rule = Entity.owned(
        NAMESPACE, RULE_KIND, "Eligibility", attributes=rule_attributes("S")
    )
    impostor = Entity(
        id=rule.id, kind="invariant", name=rule.name, attributes={"statement": "S"}
    )
    with repository.begin_revision("pipeline", "primeira") as revision:
        revision.put_entity(rule)
    with pytest.raises(IdentityCollision) as failure:
        with repository.begin_revision("pipeline", "colide") as revision:
            revision.put_entity(impostor)
    assert failure.value.existing.kind == RULE_KIND
    assert failure.value.incoming.kind == "invariant"


def test_put_entity_raises_identity_collision_on_a_different_canonical_name(repository):
    rule = Entity.owned(
        NAMESPACE, RULE_KIND, "Eligibility", attributes=rule_attributes("S")
    )
    impostor = Entity(
        id=rule.id,
        kind=rule.kind,
        name="Outra regra",
        attributes=rule_attributes("S"),
    )
    with repository.begin_revision("pipeline", "primeira") as revision:
        revision.put_entity(rule)
    with pytest.raises(IdentityCollision) as failure:
        with repository.begin_revision("pipeline", "colide") as revision:
            revision.put_entity(impostor)
    assert failure.value.existing.canonical_name == "eligibility"
    assert failure.value.incoming.canonical_name == "outra_regra"


def test_a_name_that_differs_only_in_case_or_accent_is_not_a_collision(repository):
    rule = Entity.owned(
        NAMESPACE, RULE_KIND, "Elegibilidade", attributes=rule_attributes("S")
    )
    restated = Entity(
        id=rule.id,
        kind=rule.kind,
        name="ELEGIBILIDADE",
        attributes=rule_attributes("S2"),
    )
    with repository.begin_revision("pipeline", "primeira") as revision:
        revision.put_entity(rule)
    with repository.begin_revision("pipeline", "reescreve") as revision:
        revision.put_entity(restated)
    assert repository.get_entity(rule.id).attributes["statement"] == "S2"


def test_identity_collision_rolls_back_everything_in_the_transaction(repository):
    rule = Entity.owned(
        NAMESPACE, RULE_KIND, "Eligibility", attributes=rule_attributes("S")
    )
    impostor = Entity(
        id=rule.id, kind="invariant", name=rule.name, attributes={"statement": "S"}
    )
    companion = Entity.owned(NAMESPACE, CAPABILITY_KIND, "Companion")
    with repository.begin_revision("pipeline", "primeira") as revision:
        revision.put_entity(rule)
    before = repository.entity_count()
    revisions_before = repository.revision_count()
    with pytest.raises(IdentityCollision):
        with repository.begin_revision("pipeline", "colide") as revision:
            revision.put_entity(companion)
            revision.put_entity(impostor)
    assert repository.entity_count() == before
    assert repository.get_entity(companion.id) is None
    assert repository.revision_count() == revisions_before
    assert repository.get_entity(rule.id).kind == RULE_KIND


def test_reingesting_the_identical_entity_stays_idempotent(repository):
    owner = owner_entity("Renewal")
    rule = Entity.owned(
        NAMESPACE,
        RULE_KIND,
        "Eligibility",
        owner=owner,
        attributes=rule_attributes("S"),
    )
    for summary in ("primeira", "segunda", "terceira"):
        with repository.begin_revision("pipeline", summary) as revision:
            revision.put_entity(owner)
            revision.put_entity(rule)
    assert repository.entity_count() == 2
    assert repository.get_entity(rule.id).owner_id == owner.id


def test_moving_an_entity_to_another_owner_is_refused_not_silently_applied(repository):
    renewal_owner = owner_entity("Renewal")
    rule = Entity.owned(
        NAMESPACE,
        RULE_KIND,
        "Eligibility",
        owner=renewal_owner,
        attributes=rule_attributes("S"),
    )
    with repository.begin_revision("pipeline", "primeira") as revision:
        revision.put_entity(renewal_owner)
        revision.put_entity(rule)
    moved = Entity(
        id=rule.id,
        kind=rule.kind,
        name=rule.name,
        attributes=rule_attributes("S"),
        owner_id=EntityId("ent_outra_capability"),
    )
    with pytest.raises(IdentityCollision):
        with repository.begin_revision("pipeline", "move") as revision:
            revision.put_entity(moved)
    assert repository.get_entity(rule.id).owner_id == renewal_owner.id
