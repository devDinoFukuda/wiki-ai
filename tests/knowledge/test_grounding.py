from __future__ import annotations

from wiki_ai.knowledge.grounding import (
    NEGATION_MARKER,
    GroundingCheck,
    check_component,
    excerpt_vocabulary,
    key_terms,
    mandatory_terms,
    symbol_defined_or_referenced,
)

DELETION_CLAIM = "after three failures the repository permanently deletes the reference"
SAVE_EXCERPT = "repository.save(reference)"
DELETE_EXCERPT = "if failures >= 3:\n    repository.delete(reference)"


def test_claim_about_deletion_is_not_grounded_in_a_save_excerpt() -> None:
    check = check_component(
        "statement", DELETION_CLAIM, excerpt_vocabulary(SAVE_EXCERPT)
    )

    assert check.ok is False
    assert "3" in check.required_missing
    assert "deletes" in check.required_missing


def test_numeric_literal_of_the_claim_must_appear_in_the_excerpt() -> None:
    excerpt = "if failures >= 5:\n    repository.delete(reference)"

    check = check_component(
        "statement",
        "after three failures the repository deletes the reference",
        excerpt_vocabulary(excerpt),
    )

    assert check.ok is False
    assert check.required_missing == ("3",)


def test_missing_negation_in_the_excerpt_blocks_grounding() -> None:
    excerpt = "def purge(self):\n    repository.delete(reference)"

    check = check_component(
        "statement",
        "the repository does not delete the reference",
        excerpt_vocabulary(excerpt),
    )

    assert check.ok is False
    assert check.required_missing == (NEGATION_MARKER,)


def test_negated_claim_is_grounded_when_the_excerpt_negates_too() -> None:
    excerpt = "if not allowed:\n    return\nrepository.delete(reference)"

    check = check_component(
        "statement",
        "the repository does not delete the reference",
        excerpt_vocabulary(excerpt),
    )

    assert check.ok is True
    assert check.required_missing == ()


def test_faithful_claim_is_grounded() -> None:
    check = check_component(
        "statement",
        "after three failures the repository deletes the reference",
        excerpt_vocabulary(DELETE_EXCERPT),
    )

    assert check.ok is True
    assert check.required_terms == ("3", "deletes")
    assert check.terms_missing == ()


def test_operator_of_the_claim_is_mandatory() -> None:
    excerpt = "if attempts == 3:\n    repository.delete(reference)"

    check = check_component(
        "statement",
        "when attempts >= 3 the repository deletes the reference",
        excerpt_vocabulary(excerpt),
    )

    assert check.ok is False
    assert ">=" in check.required_missing


def test_half_of_the_optional_terms_is_enough_when_mandatory_terms_match() -> None:
    excerpt = "def delete(reference):\n    registry.drop(reference)"

    check = check_component(
        "statement", "the orchestrator deletes the reference", excerpt_vocabulary(excerpt)
    )

    assert check.required_missing == ()
    assert check.ok is True
    assert "orchestrator" in check.terms_missing


def test_a_single_matching_optional_term_is_no_longer_enough() -> None:
    excerpt = "reference = build()"

    check = check_component(
        "statement",
        "the dispatcher forwards the reference to the ledger and the auditor",
        excerpt_vocabulary(excerpt),
    )

    assert check.ok is False


def test_mandatory_terms_ignore_auxiliary_verbs() -> None:
    assert mandatory_terms("the repository must delete the reference") == ("delete",)


def test_mandatory_terms_collect_quoted_literals() -> None:
    assert "blocked" in mandatory_terms('the api returns "blocked" for the caller')


def test_key_terms_keep_the_public_shape() -> None:
    assert key_terms(DELETION_CLAIM) == (
        "3",
        "failures",
        "repository",
        "permanently",
        "deletes",
        "reference",
    )


def test_key_terms_honour_the_exclude_list() -> None:
    assert "repository" not in key_terms(DELETION_CLAIM, exclude=("repository",))


def test_bare_symbol_needs_every_term_in_the_excerpt() -> None:
    check = symbol_defined_or_referenced(
        "OrderRepository", excerpt_vocabulary("class OrderRepository:")
    )

    assert check.ok is True
    assert check.required_terms == ("orderrepository",)

    absent = symbol_defined_or_referenced(
        "OrderRepository", excerpt_vocabulary("class Ledger:")
    )
    assert absent.ok is False


def test_grounding_check_serializes_required_terms() -> None:
    check = check_component(
        "statement", DELETION_CLAIM, excerpt_vocabulary(SAVE_EXCERPT)
    )

    payload = check.to_dict()

    assert payload["ok"] is False
    assert payload["required_terms"] == list(check.required_terms)
    assert payload["required_missing"] == list(check.required_missing)


def test_grounding_check_defaults_required_terms_to_empty() -> None:
    check = GroundingCheck(
        component="statement", terms_required=("a",), terms_found=("a",), ok=True
    )

    assert check.required_terms == ()
    assert check.required_missing == ()


def test_empty_claim_is_never_grounded() -> None:
    assert check_component("statement", "", excerpt_vocabulary(DELETE_EXCERPT)).ok is False


def test_portuguese_negation_is_mandatory() -> None:
    check = check_component(
        "statement",
        "o repositorio nao apaga a referencia",
        excerpt_vocabulary("repositorio.apaga(referencia)"),
    )

    assert check.ok is False
    assert NEGATION_MARKER in check.required_missing
