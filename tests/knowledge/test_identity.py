from __future__ import annotations

import hashlib

import pytest

from wiki_ai.knowledge import InvalidIdentity, content_hash
from wiki_ai.knowledge.identity import (
    ID_LENGTH,
    canonical_json,
    entity_id,
    evidence_id,
    new_revision_id,
    relation_id,
    source_version_id,
)


def test_content_hash_matches_sha256_of_utf8():
    assert content_hash("abc") == hashlib.sha256(b"abc").hexdigest()


def test_content_hash_accepts_bytes_and_str_equivalently():
    assert content_hash(b"linha") == content_hash("linha")


def test_content_hash_rejects_other_types():
    with pytest.raises(InvalidIdentity):
        content_hash(42)


def test_content_hash_is_stable_across_calls():
    assert content_hash("mesmo") == content_hash("mesmo")
    assert content_hash("mesmo") != content_hash("outro")


def test_canonical_json_is_order_independent():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_entity_id_is_content_addressed_and_prefixed():
    value = entity_id("component", "svc/a")
    assert value.startswith("ent_")
    assert len(value) == len("ent_") + ID_LENGTH
    assert value == entity_id("component", "svc/a")


def test_entity_id_requires_stable_key():
    with pytest.raises(InvalidIdentity):
        entity_id("component", "  ")


def test_entity_id_requires_kind():
    with pytest.raises(InvalidIdentity):
        entity_id("", "svc/a")


def test_relation_id_depends_on_direction():
    forward = relation_id("calls", "ent_a", "ent_b")
    backward = relation_id("calls", "ent_b", "ent_a")
    assert forward.startswith("rel_")
    assert forward != backward


def test_source_version_id_depends_on_hash():
    first = source_version_id("src_a", "hash1")
    second = source_version_id("src_a", "hash2")
    assert first.startswith("srv_")
    assert first != second


def test_evidence_id_is_stable_under_key_order():
    left = evidence_id("src_a", "hash1", {"kind": "code", "path": "a.py", "line_start": 1})
    right = evidence_id("src_a", "hash1", {"line_start": 1, "path": "a.py", "kind": "code"})
    assert left == right
    assert left.startswith("evd_")


def test_evidence_id_changes_with_locator():
    base = evidence_id("src_a", "hash1", {"kind": "code", "path": "a.py"})
    other = evidence_id("src_a", "hash1", {"kind": "code", "path": "b.py"})
    assert base != other


def test_revision_ids_are_unique():
    ids = {new_revision_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(value.startswith("rev_") for value in ids)
