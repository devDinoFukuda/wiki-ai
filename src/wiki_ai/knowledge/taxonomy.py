from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from .errors import InvalidRelationPair, MissingRequiredAttribute, UnknownKind


class EntityKind(str, Enum):
    SYSTEM = "system"
    MODULE = "module"
    CAPABILITY = "capability"
    ENTRY_POINT = "entry_point"
    BUSINESS_RULE = "business_rule"
    INVARIANT = "invariant"
    PRECONDITION = "precondition"
    POSTCONDITION = "postcondition"
    EDGE_CASE = "edge_case"
    FLOW = "flow"
    FLOW_STEP = "flow_step"
    DECISION = "decision"
    STATE = "state"
    STATE_TRANSITION = "state_transition"
    INPUT = "input"
    OUTPUT = "output"
    DATA_CONTRACT = "data_contract"
    DATA_FIELD = "data_field"
    VALIDATION = "validation"
    INTEGRATION = "integration"
    OPERATION = "operation"
    ENDPOINT = "endpoint"
    PROTOCOL = "protocol"
    EVENT = "event"
    TOPIC = "topic"
    QUEUE = "queue"
    PERSISTENCE = "persistence"
    DATA_ENTITY = "data_entity"
    TABLE = "table"
    QUERY = "query"
    PROCEDURE = "procedure"
    TRANSACTION = "transaction"
    FAILURE_MODE = "failure_mode"
    RETRY_POLICY = "retry_policy"
    FALLBACK = "fallback"
    TIMEOUT_POLICY = "timeout_policy"
    IDEMPOTENCY_POLICY = "idempotency_policy"
    CONFIGURATION = "configuration"
    DEPENDENCY = "dependency"
    TEST_SCENARIO = "test_scenario"
    INITIATIVE = "initiative"
    REQUIREMENT = "requirement"
    PROPOSAL = "proposal"
    DECISION_RECORD = "decision_record"
    SOURCE = "source"
    GAP = "gap"


class RelationKind(str, Enum):
    IMPLEMENTS = "implements"
    CALLS = "calls"
    CONSUMES = "consumes"
    PUBLISHES = "publishes"
    READS = "reads"
    WRITES = "writes"
    VALIDATES = "validates"
    DEPENDS_ON = "depends_on"
    TRIGGERS = "triggers"
    TRANSITIONS_TO = "transitions_to"
    HANDLES = "handles"
    RETRIES = "retries"
    FALLS_BACK_TO = "falls_back_to"
    PERSISTS_TO = "persists_to"
    TESTS = "tests"
    DECLARES = "declares"
    PROPOSES_CHANGE_TO = "proposes_change_to"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    AFFECTS = "affects"
    BELONGS_TO = "belongs_to"


K = EntityKind
R = RelationKind

BEHAVIOURAL_HOLDERS: tuple[EntityKind, ...] = (
    K.SYSTEM,
    K.MODULE,
    K.CAPABILITY,
    K.ENTRY_POINT,
    K.FLOW,
    K.FLOW_STEP,
    K.OPERATION,
    K.ENDPOINT,
    K.PROCEDURE,
    K.QUERY,
)

STORAGE_TARGETS: tuple[EntityKind, ...] = (
    K.PERSISTENCE,
    K.DATA_ENTITY,
    K.TABLE,
    K.QUEUE,
    K.TOPIC,
)

DECIDING_KINDS: tuple[EntityKind, ...] = (
    K.DECISION_RECORD,
    K.PROPOSAL,
    K.REQUIREMENT,
    K.INITIATIVE,
)

DOCUMENT_KINDS: tuple[EntityKind, ...] = (
    K.INITIATIVE,
    K.REQUIREMENT,
    K.PROPOSAL,
    K.DECISION_RECORD,
    K.SOURCE,
)


def _pairs(
    sources: tuple[EntityKind, ...] | tuple[None],
    targets: tuple[EntityKind, ...] | tuple[None],
) -> frozenset[tuple[EntityKind | None, EntityKind | None]]:
    return frozenset((source, target) for source in sources for target in targets)


ANY: tuple[None] = (None,)

_ALLOWED_PAIRS: dict[
    RelationKind, frozenset[tuple[EntityKind | None, EntityKind | None]]
] = {
    R.IMPLEMENTS: _pairs(
        (K.MODULE, K.CAPABILITY, K.ENTRY_POINT, K.OPERATION, K.ENDPOINT, K.PROCEDURE, K.FLOW),
        (K.REQUIREMENT, K.CAPABILITY, K.BUSINESS_RULE, K.DATA_CONTRACT, K.PROTOCOL, K.INITIATIVE),
    ),
    R.CALLS: _pairs(
        BEHAVIOURAL_HOLDERS,
        BEHAVIOURAL_HOLDERS + (K.INTEGRATION, K.DEPENDENCY),
    ),
    R.CONSUMES: _pairs(
        BEHAVIOURAL_HOLDERS + (K.INTEGRATION,),
        (K.EVENT, K.TOPIC, K.QUEUE, K.INPUT, K.DATA_CONTRACT, K.ENDPOINT, K.INTEGRATION),
    ),
    R.PUBLISHES: _pairs(
        BEHAVIOURAL_HOLDERS + (K.INTEGRATION,),
        (K.EVENT, K.TOPIC, K.QUEUE, K.OUTPUT, K.DATA_CONTRACT),
    ),
    R.READS: _pairs(
        BEHAVIOURAL_HOLDERS,
        STORAGE_TARGETS + (K.QUERY, K.CONFIGURATION, K.DATA_FIELD, K.INPUT),
    ),
    R.WRITES: _pairs(
        BEHAVIOURAL_HOLDERS,
        STORAGE_TARGETS + (K.DATA_FIELD, K.OUTPUT, K.STATE),
    ),
    R.VALIDATES: _pairs(
        (K.VALIDATION, K.BUSINESS_RULE, K.PRECONDITION, K.INVARIANT, K.POSTCONDITION),
        (
            K.INPUT,
            K.OUTPUT,
            K.DATA_CONTRACT,
            K.DATA_FIELD,
            K.STATE,
            K.CAPABILITY,
            K.ENTRY_POINT,
            K.FLOW_STEP,
            K.TRANSACTION,
        ),
    ),
    R.DEPENDS_ON: _pairs(ANY, ANY),
    R.TRIGGERS: _pairs(
        BEHAVIOURAL_HOLDERS + (K.EVENT, K.DECISION, K.STATE_TRANSITION, K.FAILURE_MODE),
        BEHAVIOURAL_HOLDERS
        + (K.EVENT, K.STATE_TRANSITION, K.FAILURE_MODE, K.FALLBACK, K.RETRY_POLICY),
    ),
    R.TRANSITIONS_TO: _pairs((K.STATE,), (K.STATE,)),
    R.HANDLES: _pairs(
        BEHAVIOURAL_HOLDERS + (K.FALLBACK, K.RETRY_POLICY),
        (K.FAILURE_MODE, K.EDGE_CASE, K.EVENT, K.INPUT),
    ),
    R.RETRIES: _pairs(
        (K.RETRY_POLICY,),
        BEHAVIOURAL_HOLDERS + (K.INTEGRATION, K.FAILURE_MODE, K.TRANSACTION),
    ),
    R.FALLS_BACK_TO: _pairs(
        BEHAVIOURAL_HOLDERS + (K.INTEGRATION, K.FAILURE_MODE, K.RETRY_POLICY),
        (K.FALLBACK,) + BEHAVIOURAL_HOLDERS,
    ),
    R.PERSISTS_TO: _pairs(ANY, (K.TABLE, K.DATA_ENTITY, K.PERSISTENCE)),
    R.TESTS: _pairs((K.TEST_SCENARIO,), ANY),
    R.DECLARES: _pairs(DOCUMENT_KINDS, ANY),
    R.PROPOSES_CHANGE_TO: _pairs((K.PROPOSAL, K.INITIATIVE), ANY),
    R.CONTRADICTS: frozenset((kind, kind) for kind in EntityKind),
    R.SUPERSEDES: frozenset((kind, kind) for kind in EntityKind)
    | _pairs(DECIDING_KINDS, DECIDING_KINDS),
    R.AFFECTS: _pairs(
        (K.PROPOSAL, K.INITIATIVE, K.DECISION_RECORD, K.REQUIREMENT, K.GAP),
        ANY,
    ),
    R.BELONGS_TO: _pairs((K.MODULE,), (K.SYSTEM,))
    | _pairs((K.CAPABILITY,), (K.MODULE, K.SYSTEM))
    | _pairs(
        (
            K.ENTRY_POINT,
            K.FLOW,
            K.BUSINESS_RULE,
            K.INVARIANT,
            K.PRECONDITION,
            K.POSTCONDITION,
            K.EDGE_CASE,
            K.DECISION,
            K.STATE,
            K.INPUT,
            K.OUTPUT,
            K.DATA_CONTRACT,
            K.VALIDATION,
            K.INTEGRATION,
            K.EVENT,
            K.PERSISTENCE,
            K.FAILURE_MODE,
            K.RETRY_POLICY,
            K.FALLBACK,
            K.TIMEOUT_POLICY,
            K.IDEMPOTENCY_POLICY,
            K.CONFIGURATION,
            K.DEPENDENCY,
            K.TEST_SCENARIO,
            K.GAP,
        ),
        (K.CAPABILITY, K.MODULE, K.SYSTEM),
    )
    | _pairs((K.FLOW_STEP,), (K.FLOW,))
    | _pairs((K.STATE_TRANSITION,), (K.STATE, K.FLOW, K.CAPABILITY))
    | _pairs((K.DATA_FIELD,), (K.DATA_CONTRACT, K.DATA_ENTITY, K.TABLE))
    | _pairs((K.TABLE, K.DATA_ENTITY, K.QUERY, K.PROCEDURE, K.TRANSACTION), (K.PERSISTENCE,))
    | _pairs((K.OPERATION, K.ENDPOINT, K.PROTOCOL), (K.INTEGRATION,))
    | _pairs((K.TOPIC, K.QUEUE), (K.INTEGRATION, K.PERSISTENCE, K.SYSTEM))
    | _pairs((K.REQUIREMENT, K.PROPOSAL, K.DECISION_RECORD), (K.INITIATIVE,)),
}

ALLOWED_PAIRS: Mapping[
    RelationKind, frozenset[tuple[EntityKind | None, EntityKind | None]]
] = MappingProxyType(dict(_ALLOWED_PAIRS))

_REQUIRED_ATTRIBUTES: dict[EntityKind, tuple[str, ...]] = {
    K.BUSINESS_RULE: ("statement", "conditions", "effects"),
    K.INVARIANT: ("statement",),
    K.PRECONDITION: ("statement",),
    K.POSTCONDITION: ("statement",),
    K.EDGE_CASE: ("condition", "expected"),
    K.ENTRY_POINT: ("mechanism", "location"),
    K.FLOW_STEP: ("ordinal",),
    K.DECISION: ("criteria",),
    K.STATE_TRANSITION: ("from_state", "to_state"),
    K.DATA_FIELD: ("data_type",),
    K.VALIDATION: ("rule",),
    K.INTEGRATION: ("direction", "protocol"),
    K.OPERATION: ("verb",),
    K.ENDPOINT: ("address",),
    K.PROTOCOL: ("name",),
    K.TABLE: ("schema",),
    K.QUERY: ("statement",),
    K.FAILURE_MODE: ("trigger", "effect"),
    K.RETRY_POLICY: ("attempts",),
    K.FALLBACK: ("behaviour",),
    K.TIMEOUT_POLICY: ("duration",),
    K.IDEMPOTENCY_POLICY: ("key",),
    K.CONFIGURATION: ("key",),
    K.DEPENDENCY: ("identifier",),
    K.TEST_SCENARIO: ("scenario",),
    K.REQUIREMENT: ("statement",),
    K.PROPOSAL: ("statement",),
    K.DECISION_RECORD: ("decision",),
    K.SOURCE: ("origin",),
    K.GAP: ("question", "blocking"),
}

REQUIRED_ATTRIBUTES: Mapping[EntityKind, tuple[str, ...]] = MappingProxyType(
    dict(_REQUIRED_ATTRIBUTES)
)

BOOLEAN_ATTRIBUTES: Mapping[EntityKind, tuple[str, ...]] = MappingProxyType(
    {K.GAP: ("blocking",)}
)

ENTITY_KIND_VALUES: frozenset[str] = frozenset(kind.value for kind in EntityKind)
RELATION_KIND_VALUES: frozenset[str] = frozenset(kind.value for kind in RelationKind)


def entity_kind(value: str) -> EntityKind:
    try:
        return EntityKind(value)
    except ValueError:
        raise UnknownKind(
            f"kind de entidade {value!r} fora da taxonomia; permitidos: "
            f"{', '.join(sorted(ENTITY_KIND_VALUES))}"
        ) from None


def relation_kind(value: str) -> RelationKind:
    try:
        return RelationKind(value)
    except ValueError:
        raise UnknownKind(
            f"kind de relação {value!r} fora da taxonomia; permitidos: "
            f"{', '.join(sorted(RELATION_KIND_VALUES))}"
        ) from None


def pair_allowed(
    relation: RelationKind, source: EntityKind, target: EntityKind
) -> bool:
    allowed = ALLOWED_PAIRS[relation]
    candidates = (
        (source, target),
        (source, None),
        (None, target),
        (None, None),
    )
    return any(candidate in allowed for candidate in candidates)


def validate_pair(
    relation_kind_value: str | RelationKind,
    source_kind: str | EntityKind,
    target_kind: str | EntityKind,
) -> tuple[RelationKind, EntityKind, EntityKind]:
    relation = (
        relation_kind_value
        if isinstance(relation_kind_value, RelationKind)
        else relation_kind(str(relation_kind_value))
    )
    source = (
        source_kind if isinstance(source_kind, EntityKind) else entity_kind(str(source_kind))
    )
    target = (
        target_kind if isinstance(target_kind, EntityKind) else entity_kind(str(target_kind))
    )
    if not pair_allowed(relation, source, target):
        raise InvalidRelationPair(
            f"relação {relation.value} não admite {source.value} -> {target.value}"
        )
    return relation, source, target


def validate_attributes(
    kind_value: str | EntityKind, attributes: Mapping[str, Any]
) -> EntityKind:
    kind = (
        kind_value if isinstance(kind_value, EntityKind) else entity_kind(str(kind_value))
    )
    missing = [
        name
        for name in REQUIRED_ATTRIBUTES.get(kind, ())
        if name not in attributes or _is_blank(attributes[name])
    ]
    if missing:
        raise MissingRequiredAttribute(
            f"entidade {kind.value} exige atributo(s): {', '.join(missing)}"
        )
    for name in BOOLEAN_ATTRIBUTES.get(kind, ()):
        if not isinstance(attributes.get(name), bool):
            raise MissingRequiredAttribute(
                f"entidade {kind.value}.{name} exige booleano, recebido "
                f"{attributes.get(name)!r}"
            )
    return kind


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False
