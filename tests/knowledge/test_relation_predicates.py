from __future__ import annotations

from wiki_ai.knowledge.grounding import (
    RELATION_PREDICATE_COMPONENT,
    check_relation_predicate,
    excerpt_vocabulary,
    relation_predicate_terms,
)
from wiki_ai.knowledge.taxonomy import RelationKind

SAVE_EXCERPT = (
    "class OrderService:\n"
    "    def place(self, order):\n"
    "        self.repository.save(order)"
)
CONFIGURE_EXCERPT = (
    "class OrderService:\n"
    "    def __init__(self, repository):\n"
    "        self.settings = configures"
)
SELECT_EXCERPT = "def load(self, key):\n    return cursor.execute('select id from orders')"


def test_every_relation_kind_has_a_lexicon() -> None:
    empty = [kind.value for kind in RelationKind if not relation_predicate_terms(kind.value)]

    assert empty == []


def test_unknown_kind_has_no_lexicon() -> None:
    assert relation_predicate_terms("no_such_relation") == ()


def test_kind_without_lexicon_is_never_grounded() -> None:
    check = check_relation_predicate(
        "no_such_relation", "OrderService saves the order", excerpt_vocabulary(SAVE_EXCERPT)
    )

    assert check.ok is False
    assert check.terms_required == ()


def test_check_reports_the_relation_predicate_component() -> None:
    check = check_relation_predicate(
        RelationKind.WRITES.value,
        "OrderService saves the order to the repository",
        excerpt_vocabulary(SAVE_EXCERPT),
    )

    assert check.component == RELATION_PREDICATE_COMPONENT


def test_configures_excerpt_does_not_sustain_calls() -> None:
    check = check_relation_predicate(
        RelationKind.CALLS.value,
        "OrderService configures OrderRepository",
        excerpt_vocabulary(CONFIGURE_EXCERPT),
    )

    assert check.ok is False


def test_method_invocation_sustains_calls() -> None:
    check = check_relation_predicate(
        RelationKind.CALLS.value,
        "OrderService delegates persistence to OrderRepository.save",
        excerpt_vocabulary(SAVE_EXCERPT),
    )

    assert check.ok is True


def test_save_call_sustains_writes() -> None:
    check = check_relation_predicate(
        RelationKind.WRITES.value,
        "OrderService saves the order to the repository",
        excerpt_vocabulary(SAVE_EXCERPT),
    )

    assert check.ok is True
    assert "save" in check.terms_found


def test_select_only_excerpt_does_not_sustain_writes() -> None:
    check = check_relation_predicate(
        RelationKind.WRITES.value,
        "OrderService writes the order",
        excerpt_vocabulary(SELECT_EXCERPT),
    )

    assert check.ok is False
    assert check.terms_found == ()


def test_select_only_excerpt_sustains_reads() -> None:
    check = check_relation_predicate(
        RelationKind.READS.value,
        "OrderService loads the order from the orders table",
        excerpt_vocabulary(SELECT_EXCERPT),
    )

    assert check.ok is True


def test_statement_with_absent_number_is_not_grounded() -> None:
    check = check_relation_predicate(
        RelationKind.CALLS.value,
        "OrderService calls the repository 7 times",
        excerpt_vocabulary(SAVE_EXCERPT),
    )

    assert check.ok is False


def test_statement_number_present_in_the_excerpt_is_grounded() -> None:
    excerpt = "for attempt in range(7):\n    self.repository.save(order)"

    check = check_relation_predicate(
        RelationKind.CALLS.value,
        "OrderService calls the repository save 7 times",
        excerpt_vocabulary(excerpt),
    )

    assert check.ok is True


def test_empty_statement_is_never_grounded() -> None:
    grounded = check_relation_predicate(
        RelationKind.CALLS.value, "", excerpt_vocabulary(SAVE_EXCERPT)
    )
    ungrounded = check_relation_predicate(
        RelationKind.CALLS.value, "", excerpt_vocabulary(CONFIGURE_EXCERPT)
    )
    blank = check_relation_predicate(
        RelationKind.WRITES.value, "   ", excerpt_vocabulary(SAVE_EXCERPT)
    )

    assert grounded.ok is False
    assert ungrounded.ok is False
    assert blank.ok is False


def test_documental_kinds_have_their_own_lexicon() -> None:
    excerpt = "the proposal declares that every order must define a settlement window"
    statement = "the proposal declares the settlement window of an order"

    declares = check_relation_predicate(
        RelationKind.DECLARES.value, statement, excerpt_vocabulary(excerpt)
    )
    calls = check_relation_predicate(
        RelationKind.CALLS.value, statement, excerpt_vocabulary(excerpt)
    )

    assert declares.ok is True
    assert calls.ok is False


def test_retries_lexicon_needs_retry_vocabulary() -> None:
    excerpt = "for attempt in range(3):\n    backoff(attempt)"

    check = check_relation_predicate(
        RelationKind.RETRIES.value,
        "retries with backoff up to 3 attempts",
        excerpt_vocabulary(excerpt),
    )

    assert check.ok is True


def test_falls_back_to_lexicon_needs_fallback_vocabulary() -> None:
    statement = "on Timeout the remote call degrades to the fallback queue"
    grounded = check_relation_predicate(
        RelationKind.FALLS_BACK_TO.value,
        statement,
        excerpt_vocabulary(
            "try:\n    remote()\nexcept Timeout:\n    degrades_to(fallback_queue)"
        ),
    )
    ungrounded = check_relation_predicate(
        RelationKind.FALLS_BACK_TO.value, statement, excerpt_vocabulary(SAVE_EXCERPT)
    )

    assert grounded.ok is True
    assert ungrounded.ok is False


def test_bare_except_no_longer_sustains_falls_back_to() -> None:
    check = check_relation_predicate(
        RelationKind.FALLS_BACK_TO.value,
        "on Timeout the remote call returns the default",
        excerpt_vocabulary("try:\n    remote()\nexcept Timeout:\n    return default"),
    )

    assert check.ok is False
