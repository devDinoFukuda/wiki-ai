from __future__ import annotations

from pathlib import Path
from typing import Mapping

from tests.benchmark.ground_truth.sheets import GeneratedCorpus, Sheet, mark, write
from tests.benchmark.ground_truth.truth import (
    EdgeCaseTruth,
    EntryPointTruth,
    FailureModeTruth,
    IntegrationTruth,
    InvariantTruth,
    RepositoryTruth,
    RuleTruth,
    StateTruth,
    TransitionTruth,
)

__all__ = [
    "PYTHON_API_PATH",
    "PYTHON_STATE_PATH",
    "PYTHON_VALIDATION_PATH",
    "PYTHON_IDEMPOTENCY_PATH",
    "PYTHON_QUEUE_PATH",
    "build_python",
]


PYTHON_API_PATH = "orders/api.py"
PYTHON_STATE_PATH = "orders/state.py"
PYTHON_VALIDATION_PATH = "orders/validation.py"
PYTHON_IDEMPOTENCY_PATH = "orders/idempotency.py"
PYTHON_QUEUE_PATH = "orders/dispatch.py"

_PYTHON_STATE = (
    "from __future__ import annotations",
    "",
    "from enum import Enum",
    "",
    "",
    mark("enum", "class OrderState(str, Enum):"),
    mark("draft", '    DRAFT = "draft"'),
    mark("placed", '    PLACED = "placed"'),
    mark("paid", '    PAID = "paid"'),
    mark("cancelled", '    CANCELLED = "cancelled"'),
    "",
    "",
    mark("table", "TRANSITIONS: dict[OrderState, tuple[OrderState, ...]] = {"),
    mark("from_draft", "    OrderState.DRAFT: (OrderState.PLACED, OrderState.CANCELLED),"),
    mark("from_placed", "    OrderState.PLACED: (OrderState.PAID, OrderState.CANCELLED),"),
    mark("from_paid", "    OrderState.PAID: (),"),
    mark("from_cancelled", "    OrderState.CANCELLED: (),"),
    "}",
    "",
    "",
    "class TransitionRejected(Exception):",
    "    def __init__(self, current: OrderState, target: OrderState) -> None:",
    '        super().__init__(f"{current.value} -> {target.value}")',
    "        self.current = current",
    "        self.target = target",
    "",
    "",
    mark("advance", "def advance(current: OrderState, target: OrderState) -> OrderState:"),
    mark("guard", "    if target not in TRANSITIONS[current]:"),
    mark("reject", "        raise TransitionRejected(current, target)"),
    "    return target",
)

_PYTHON_VALIDATION = (
    "from __future__ import annotations",
    "",
    "from decimal import Decimal",
    "from typing import Any, Mapping",
    "",
    mark("max_items", "MAX_ITEMS = 50"),
    mark("min_total", 'MIN_TOTAL = Decimal("0.01")'),
    "",
    "",
    "class ValidationFailed(Exception):",
    "    def __init__(self, reason: str) -> None:",
    "        super().__init__(reason)",
    "        self.reason = reason",
    "",
    "",
    mark("validate", "def validate_order(payload: Mapping[str, Any]) -> Decimal:"),
    mark("items", '    items = payload.get("items") or []',),
    mark("empty", "    if not items:"),
    mark("empty_effect", '        raise ValidationFailed("order_without_items")'),
    mark("too_many", "    if len(items) > MAX_ITEMS:"),
    mark("too_many_effect", '        raise ValidationFailed("too_many_items")'),
    mark("total", '    total = sum((Decimal(str(item["price"])) * int(item["quantity"]) for item in items), Decimal("0"))'),
    mark("min_check", "    if total < MIN_TOTAL:"),
    mark("min_effect", '        raise ValidationFailed("total_below_minimum")'),
    "    return total",
)

_PYTHON_IDEMPOTENCY = (
    "from __future__ import annotations",
    "",
    "from typing import Any, Mapping, MutableMapping",
    "",
    "",
    mark("store", "class IdempotencyStore:"),
    "    def __init__(self) -> None:",
    "        self._entries: MutableMapping[str, Mapping[str, Any]] = {}",
    "",
    mark("lookup", "    def replay(self, key: str) -> Mapping[str, Any] | None:"),
    mark("get", "        return self._entries.get(key)"),
    "",
    mark("remember", "    def remember(self, key: str, response: Mapping[str, Any]) -> None:"),
    mark("write", "        self._entries[key] = dict(response)"),
)

_PYTHON_QUEUE = (
    "from __future__ import annotations",
    "",
    "from typing import Any, Mapping, Protocol",
    "",
    "",
    "class DispatchUnavailable(Exception):",
    "    pass",
    "",
    "",
    "class FulfilmentClient(Protocol):",
    "    def submit(self, order: Mapping[str, Any]) -> str: ...",
    "",
    "",
    mark("queue", "class PendingQueue:"),
    "    def __init__(self) -> None:",
    "        self._pending: list[Mapping[str, Any]] = []",
    "",
    mark("enqueue", "    def enqueue(self, order: Mapping[str, Any]) -> None:"),
    mark("append", "        self._pending.append(dict(order))"),
    "",
    "    def size(self) -> int:",
    "        return len(self._pending)",
    "",
    "",
    mark("dispatch", "def dispatch(order: Mapping[str, Any], client: FulfilmentClient, queue: PendingQueue) -> str:"),
    "    try:",
    mark("submit", "        return client.submit(order)"),
    mark("catch", "    except DispatchUnavailable:"),
    mark("fallback", "        queue.enqueue(order)"),
    mark("fallback_effect", '        return "queued"'),
)

_PYTHON_API = (
    "from __future__ import annotations",
    "",
    "from typing import Any, Mapping, MutableMapping",
    "",
    "from orders.dispatch import FulfilmentClient, PendingQueue, dispatch",
    "from orders.idempotency import IdempotencyStore",
    "from orders.state import OrderState, TransitionRejected, advance",
    "from orders.validation import ValidationFailed, validate_order",
    "",
    "",
    mark("api", "class OrderApi:"),
    "    def __init__(self, client: FulfilmentClient) -> None:",
    "        self._orders: MutableMapping[str, dict[str, Any]] = {}",
    "        self._idempotency = IdempotencyStore()",
    "        self._queue = PendingQueue()",
    "        self._client = client",
    "",
    mark("place", "    def place_order(self, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:"),
    mark("replay", "        replayed = self._idempotency.replay(idempotency_key)"),
    mark("replay_return", "        if replayed is not None:"),
    mark("replay_effect", "            return replayed"),
    "        try:",
    mark("validate_call", "            total = validate_order(payload)"),
    mark("invalid", "        except ValidationFailed as failure:"),
    mark("invalid_effect", '            return {"status": "rejected", "reason": failure.reason}'),
    mark("transition", "        state = advance(OrderState.DRAFT, OrderState.PLACED)"),
    mark("record", '        order = {"id": payload["id"], "state": state.value, "total": str(total)}'),
    mark("store_order", '        self._orders[str(payload["id"])] = order'),
    mark("dispatch_call", "        outcome = dispatch(order, self._client, self._queue)"),
    mark("response", '        response = {"status": "accepted", "order": order, "dispatch": outcome}'),
    mark("remember", "        self._idempotency.remember(idempotency_key, response)"),
    "        return response",
    "",
    mark("pay", "    def pay_order(self, order_id: str) -> Mapping[str, Any]:"),
    "        order = self._orders[order_id]",
    "        try:",
    mark("pay_transition", "            state = advance(OrderState(order[\"state\"]), OrderState.PAID)"),
    mark("pay_reject", "        except TransitionRejected as rejected:"),
    mark("pay_reject_effect", '            return {"status": "rejected", "reason": "invalid_transition", "from": rejected.current.value}'),
    '        order["state"] = state.value',
    "        return order",
    "",
    mark("cancel", "    def cancel_order(self, order_id: str) -> Mapping[str, Any]:"),
    "        order = self._orders[order_id]",
    mark("cancel_transition", "        state = advance(OrderState(order[\"state\"]), OrderState.CANCELLED)"),
    '        order["state"] = state.value',
    "        return order",
)

_PYTHON_SUPPORT: Mapping[str, tuple[str, ...]] = {
    "orders/__init__.py": ("",),
    "pyproject.toml": (
        "[project]",
        'name = "orders-api"',
        'version = "1.0.0"',
    ),
}


def build_python(root: Path) -> GeneratedCorpus:
    api = Sheet(PYTHON_API_PATH, _PYTHON_API)
    state = Sheet(PYTHON_STATE_PATH, _PYTHON_STATE)
    validation = Sheet(PYTHON_VALIDATION_PATH, _PYTHON_VALIDATION)
    idempotency = Sheet(PYTHON_IDEMPOTENCY_PATH, _PYTHON_IDEMPOTENCY)
    queue = Sheet(PYTHON_QUEUE_PATH, _PYTHON_QUEUE)
    for sheet in (api, state, validation, idempotency, queue):
        write(root / sheet.path, sheet.text())
    for path, lines in _PYTHON_SUPPORT.items():
        write(root / path, "\n".join(lines) + "\n")
    truth = RepositoryTruth(
        name="python",
        language="python",
        business_rules=(
            RuleTruth(
                key="order_requires_items",
                statement="an order without items is rejected",
                key_terms=("items", "order_without_items", "ValidationFailed"),
                conditions=("items list is empty",),
                effects=("ValidationFailed with reason order_without_items",),
                anchors=(validation.span("empty", "empty_effect"),),
            ),
            RuleTruth(
                key="order_total_minimum",
                statement="an order whose total is below the minimum is rejected",
                key_terms=("MIN_TOTAL", "total_below_minimum", "total"),
                conditions=("computed total lower than MIN_TOTAL",),
                effects=("ValidationFailed with reason total_below_minimum",),
                anchors=(
                    validation.line("min_total"),
                    validation.span("min_check", "min_effect"),
                ),
            ),
            RuleTruth(
                key="idempotent_place_order",
                statement="placing an order twice with the same idempotency key replays the first response",
                key_terms=("idempotency_key", "replay", "remember"),
                conditions=("idempotency key already stored",),
                effects=("the stored response is returned without creating a new order",),
                anchors=(
                    api.span("replay", "replay_effect"),
                    idempotency.span("lookup", "get"),
                ),
            ),
        ),
        edge_cases=(
            EdgeCaseTruth(
                key="too_many_items",
                statement="an order above the item limit is rejected",
                key_terms=("MAX_ITEMS", "too_many_items", "len"),
                condition="number of items greater than MAX_ITEMS",
                expected="ValidationFailed with reason too_many_items",
                anchors=(
                    validation.line("max_items"),
                    validation.span("too_many", "too_many_effect"),
                ),
            ),
            EdgeCaseTruth(
                key="paying_a_cancelled_order",
                statement="paying an order that is not placed is rejected as an invalid transition",
                key_terms=("TransitionRejected", "invalid_transition", "advance"),
                condition="current state has no transition to PAID",
                expected="rejected with reason invalid_transition",
                anchors=(api.span("pay_transition", "pay_reject_effect"),),
            ),
        ),
        invariants=(
            InvariantTruth(
                key="transition_table_is_closed",
                statement="only transitions declared in the transition table are allowed",
                key_terms=("TRANSITIONS", "advance", "TransitionRejected"),
                anchors=(
                    state.span("table", "from_cancelled"),
                    state.span("advance", "reject"),
                ),
            ),
            InvariantTruth(
                key="terminal_states_have_no_successor",
                statement="paid and cancelled are terminal states",
                key_terms=("PAID", "CANCELLED", "TRANSITIONS"),
                anchors=(state.span("from_paid", "from_cancelled"),),
            ),
        ),
        integrations=(
            IntegrationTruth(
                key="fulfilment_client",
                statement="orders are submitted to the fulfilment client",
                key_terms=("FulfilmentClient", "submit", "dispatch"),
                direction="outbound",
                protocol="rpc",
                anchors=(queue.span("dispatch", "submit"),),
            ),
        ),
        entrypoints=(
            EntryPointTruth(
                key="place_order",
                statement="place_order is the entry point that creates an order",
                key_terms=("place_order", "OrderApi", "idempotency_key"),
                mechanism="api",
                anchors=(api.span("place", "replay"),),
            ),
            EntryPointTruth(
                key="pay_order",
                statement="pay_order is the entry point that settles an order",
                key_terms=("pay_order", "OrderApi", "PAID"),
                mechanism="api",
                anchors=(api.span("pay", "pay_transition"),),
            ),
        ),
        states=(
            StateTruth(
                key="state_draft",
                statement="draft is the initial order state",
                key_terms=("DRAFT", "draft", "OrderState"),
                anchors=(state.line("draft"),),
            ),
            StateTruth(
                key="state_placed",
                statement="placed is the state of an accepted order",
                key_terms=("PLACED", "placed", "OrderState"),
                anchors=(state.line("placed"),),
            ),
            StateTruth(
                key="state_paid",
                statement="paid is the state of a settled order",
                key_terms=("PAID", "paid", "OrderState"),
                anchors=(state.line("paid"),),
            ),
            StateTruth(
                key="state_cancelled",
                statement="cancelled is the state of an abandoned order",
                key_terms=("CANCELLED", "cancelled", "OrderState"),
                anchors=(state.line("cancelled"),),
            ),
        ),
        transitions=(
            TransitionTruth(
                key="draft_to_placed",
                statement="a draft order becomes placed when it is accepted",
                key_terms=("DRAFT", "PLACED", "TRANSITIONS"),
                from_state="draft",
                to_state="placed",
                anchors=(state.line("from_draft"), api.line("transition")),
            ),
            TransitionTruth(
                key="placed_to_paid",
                statement="a placed order becomes paid when it is settled",
                key_terms=("PLACED", "PAID", "TRANSITIONS"),
                from_state="placed",
                to_state="paid",
                anchors=(state.line("from_placed"), api.line("pay_transition")),
            ),
            TransitionTruth(
                key="placed_to_cancelled",
                statement="a placed order can be cancelled",
                key_terms=("PLACED", "CANCELLED", "TRANSITIONS"),
                from_state="placed",
                to_state="cancelled",
                anchors=(state.line("from_placed"), api.line("cancel_transition")),
            ),
        ),
        failure_modes=(
            FailureModeTruth(
                key="dispatch_fallback_to_queue",
                statement="when the fulfilment client is unavailable the order is queued",
                key_terms=("DispatchUnavailable", "enqueue", "queued"),
                trigger="DispatchUnavailable raised by the fulfilment client",
                effect="the order is enqueued and the dispatch result is queued",
                anchors=(queue.span("catch", "fallback_effect"),),
            ),
        ),
        symbols_by_path={
            PYTHON_API_PATH: ("OrderApi", "place_order", "pay_order", "cancel_order"),
            PYTHON_STATE_PATH: ("OrderState", "TRANSITIONS", "advance"),
            PYTHON_VALIDATION_PATH: ("validate_order", "MAX_ITEMS", "MIN_TOTAL"),
            PYTHON_IDEMPOTENCY_PATH: ("IdempotencyStore", "replay", "remember"),
            PYTHON_QUEUE_PATH: ("PendingQueue", "dispatch", "DispatchUnavailable"),
        },
    )
    return GeneratedCorpus(root=root, truth=truth)
