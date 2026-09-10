from __future__ import annotations

import json

import pytest

from wiki_ai.knowledge.errors import PayloadInvalid
from wiki_ai.knowledge.grounding import (
    CALLEE_PREFIX,
    RELATION_PREDICATES_PATH,
    STRUCTURAL_KINDS,
    STRUCTURAL_MARKERS,
    check_relation_predicate,
    excerpt_vocabulary,
    invocation_targets,
    relation_predicate_lexicon,
)
from wiki_ai.knowledge.taxonomy import RelationKind

FORBIDDEN_TERMS: frozenset[str] = frozenset(
    {
        "return",
        "returns",
        "run",
        "runs",
        "request",
        "requests",
        "execute",
        "executes",
        "executa",
        "class",
        "state",
        "status",
        "module",
        "modulo",
        "package",
        "pacote",
        "get",
        "gets",
        "set",
        "sets",
        "use",
        "uses",
        "usa",
        "new",
        "if",
        "check",
        "checks",
        "checa",
        "def",
        "func",
        "method",
        "next",
        "else",
        "given",
        "when",
        "should",
        "default",
        "process",
        "processa",
        "part",
        "parte",
        "loop",
        "value",
        "data",
        "put",
        "puts",
        "attempt",
        "change",
        "move",
        "provide",
        "provides",
        "raise",
        "throw",
        "catch",
        "except",
    }
)

GETTER_EXCERPT = (
    "class OrderService {\n"
    "    OrderRepository repository;\n"
    "    OrderRepository repository() { return repository; }\n"
    "}"
)

SAVE_EXCERPT = (
    "class OrderService:\n"
    "    def place(self, order):\n"
    "        self.repository.save(order)"
)


def raw_payload() -> dict[str, dict[str, list[str]]]:
    return json.loads(RELATION_PREDICATES_PATH.read_text(encoding="utf-8"))


def test_no_relation_kind_carries_a_generic_token() -> None:
    offenders: dict[str, list[str]] = {}
    for kind, entry in raw_payload().items():
        hits = sorted(
            term for term in entry["strong"] if term.lower() in FORBIDDEN_TERMS
        )
        if hits:
            offenders[kind] = hits

    assert offenders == {}


def test_every_relation_kind_declares_strong_and_structural() -> None:
    payload = raw_payload()

    assert sorted(payload) == sorted(kind.value for kind in RelationKind)
    for kind, entry in payload.items():
        assert entry["strong"], kind
        assert set(entry) == {"strong", "structural"}
        assert set(entry["structural"]) <= STRUCTURAL_MARKERS, kind


def test_structural_kinds_declare_at_least_one_marker() -> None:
    payload = raw_payload()

    for kind in STRUCTURAL_KINDS:
        assert payload[kind]["structural"], kind


def test_lexicon_exposes_structural_before_strong() -> None:
    lexicon = relation_predicate_lexicon(RelationKind.CALLS.value)

    assert lexicon.structural == ("!invocation",)
    assert lexicon.terms[0] == "!invocation"
    assert lexicon.demands_structure is True


def test_documental_kind_does_not_demand_structure() -> None:
    assert relation_predicate_lexicon(RelationKind.DECLARES.value).demands_structure is False


def test_getter_with_calls_and_statement_is_rejected() -> None:
    check = check_relation_predicate(
        RelationKind.CALLS.value,
        "OrderService calls OrderRepository",
        excerpt_vocabulary(GETTER_EXCERPT),
    )

    assert check.ok is False
    assert check.terms_found == ()


def test_method_invocation_with_statement_is_accepted() -> None:
    check = check_relation_predicate(
        RelationKind.CALLS.value,
        "OrderService delega persistencia a OrderRepository.save",
        excerpt_vocabulary(SAVE_EXCERPT),
    )

    assert check.ok is True
    assert "!invocation" in check.terms_found


@pytest.mark.parametrize("statement", ["", "   ", "\n\t"])
def test_blank_statement_is_never_grounded(statement: str) -> None:
    for kind in RelationKind:
        check = check_relation_predicate(
            kind.value, statement, excerpt_vocabulary(SAVE_EXCERPT)
        )

        assert check.ok is False, kind.value


def test_single_strong_term_does_not_sustain_a_structural_kind() -> None:
    excerpt = "the pipeline will dispatch the order later"

    check = check_relation_predicate(
        RelationKind.CALLS.value,
        "the pipeline dispatch the order",
        excerpt_vocabulary(excerpt),
    )

    assert check.ok is False
    assert check.terms_found == ("dispatch", "dispatches")


def test_two_distinct_strong_terms_sustain_a_structural_kind() -> None:
    excerpt = "the pipeline will dispatch and invoke the order handler"

    check = check_relation_predicate(
        RelationKind.CALLS.value,
        "the pipeline dispatch and invoke the order handler",
        excerpt_vocabulary(excerpt),
    )

    assert check.ok is True
    assert len(check.terms_found) >= 2


def test_repeated_stem_counts_as_one_strong_term() -> None:
    excerpt = "the worker will invoke, invokes and invoked the queue"

    check = check_relation_predicate(
        RelationKind.CALLS.value,
        "the worker invokes the queue",
        excerpt_vocabulary(excerpt),
    )

    assert check.ok is False


def test_single_strong_term_sustains_a_documental_kind() -> None:
    excerpt = "the proposal declares the settlement window"

    check = check_relation_predicate(
        RelationKind.DECLARES.value,
        "the proposal declares the settlement window",
        excerpt_vocabulary(excerpt),
    )

    assert check.ok is True


def test_sql_read_marker_sustains_reads() -> None:
    vocabulary = excerpt_vocabulary("cursor.execute('select id from orders')")

    assert "!sql_read" in vocabulary


def test_sql_write_marker_sustains_writes() -> None:
    vocabulary = excerpt_vocabulary("cursor.execute('insert into orders values (1)')")

    assert "!sql_write" in vocabulary


def test_assignment_marker_is_emitted() -> None:
    assert "!assignment" in excerpt_vocabulary("total = price * quantity")
    assert "!assignment" not in excerpt_vocabulary("compare(a == b)")


def test_io_send_and_receive_markers_are_emitted() -> None:
    assert "!io_send" in excerpt_vocabulary("bus.publish(event)")
    assert "!io_receive" in excerpt_vocabulary("bus.subscribe(handler)")
    assert "!io_send" not in excerpt_vocabulary("total = price")


def test_callee_marker_names_the_invoked_symbol() -> None:
    vocabulary = excerpt_vocabulary(SAVE_EXCERPT)

    assert f"{CALLEE_PREFIX}save" in vocabulary
    assert f"{CALLEE_PREFIX}place" not in vocabulary


def test_persisted_call_sustains_writes_through_the_callee() -> None:
    check = check_relation_predicate(
        RelationKind.WRITES.value,
        "OrderService saves the order to the repository",
        excerpt_vocabulary(SAVE_EXCERPT),
    )

    assert check.ok is True
    assert f"{CALLEE_PREFIX}save" in check.terms_found


COBOL_CALL = (
    "       PROCEDURE DIVISION.\n"
    "       BILL-LOGIC SECTION.\n"
    "           CALL 'PAYRUN' USING WS-GROSS\n"
    "           STOP RUN."
)

INVOKING_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("cobol_literal", COBOL_CALL, "payrun"),
    ("cobol_identifier", "CALL WS-PGM", "ws-pgm"),
    ("cobol_perform", "PERFORM 2000-PROCESS", "2000-process"),
    ("cics_link", "EXEC CICS LINK PROGRAM(TAXCALC)", "taxcalc"),
    ("jcl_pgm", "//STEP01  EXEC PGM=PAYRUN", "payrun"),
    ("jcl_proc", "//STEP02  EXEC PROC=PAYPROC", "payproc"),
    ("jcl_bare_proc", "//STEP03  EXEC PAYPROC", "payproc"),
    ("fortran_call", "      call compute_tax(gross)", "compute_tax"),
    ("direct_call", "computeTax(gross)", "computetax"),
    ("receiver_call", "self.repository.save(order)", "save"),
)

NON_INVOKING_SOURCES: tuple[tuple[str, str], ...] = (
    ("if", "if (x) { }"),
    ("while", "while (n > 0) {"),
    ("for", "for (i = 0; i < n; i++) {"),
    ("switch", "switch (status) {"),
    ("catch", "catch (Exception e) {"),
    ("python_def", "def place(self, order):"),
    ("js_function", "function handle(req) {"),
    ("java_method", "public void place(Order order) {"),
    ("class", "class OrderService {"),
    ("return", "return (a + b);"),
    ("assignment", "WS-TOTAL = WS-A + WS-B"),
)


@pytest.mark.parametrize(
    ("label", "source", "target"),
    INVOKING_SOURCES,
    ids=[item[0] for item in INVOKING_SOURCES],
)
def test_every_invocation_form_emits_the_markers(label, source, target) -> None:
    vocabulary = excerpt_vocabulary(source)

    assert "!invocation" in vocabulary, label
    assert f"{CALLEE_PREFIX}{target}" in vocabulary, label


@pytest.mark.parametrize(
    ("label", "source", "target"),
    INVOKING_SOURCES,
    ids=[item[0] for item in INVOKING_SOURCES],
)
def test_every_invocation_form_can_sustain_calls(label, source, target) -> None:
    check = check_relation_predicate(
        RelationKind.CALLS.value,
        f"PAYJOB chama {target}",
        excerpt_vocabulary(source),
    )

    assert check.ok is True, label
    assert "!invocation" in check.terms_found, label


@pytest.mark.parametrize(
    ("label", "source"),
    NON_INVOKING_SOURCES,
    ids=[item[0] for item in NON_INVOKING_SOURCES],
)
def test_control_flow_and_definitions_never_invoke(label, source) -> None:
    vocabulary = excerpt_vocabulary(source)

    assert "!invocation" not in vocabulary, label


@pytest.mark.parametrize(
    ("label", "source"),
    NON_INVOKING_SOURCES,
    ids=[item[0] for item in NON_INVOKING_SOURCES],
)
def test_control_flow_and_definitions_never_sustain_calls(label, source) -> None:
    check = check_relation_predicate(
        RelationKind.CALLS.value,
        "OrderService calls OrderRepository",
        excerpt_vocabulary(source),
    )

    assert check.ok is False, label


def test_invocation_targets_lists_every_reachable_name() -> None:
    source = "//STEP01 EXEC PGM=PAYRUN\n       CALL 'TAXCALC' USING WS-GROSS"

    assert set(invocation_targets(source.lower())) == {"payrun", "taxcalc"}


def test_invocation_targets_skips_keywords_and_short_names() -> None:
    assert invocation_targets("if (x)") == ()
    assert invocation_targets("call if") == ()
    assert invocation_targets("perform 2000") == ()


def test_unknown_structural_marker_in_the_payload_is_rejected(tmp_path, monkeypatch) -> None:
    broken = tmp_path / "relation_predicates.json"
    broken.write_text(
        json.dumps({"calls": {"strong": ["invoke"], "structural": ["!nonsense"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "wiki_ai.knowledge.grounding.RELATION_PREDICATES_PATH", broken
    )
    from wiki_ai.knowledge.grounding import _relation_predicates

    _relation_predicates.cache_clear()
    try:
        with pytest.raises(PayloadInvalid):
            _relation_predicates()
    finally:
        _relation_predicates.cache_clear()


def test_kind_without_strong_terms_is_rejected(tmp_path, monkeypatch) -> None:
    broken = tmp_path / "relation_predicates.json"
    broken.write_text(
        json.dumps({"calls": {"strong": [], "structural": ["!invocation"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "wiki_ai.knowledge.grounding.RELATION_PREDICATES_PATH", broken
    )
    from wiki_ai.knowledge.grounding import _relation_predicates

    _relation_predicates.cache_clear()
    try:
        with pytest.raises(PayloadInvalid):
            _relation_predicates()
    finally:
        _relation_predicates.cache_clear()
