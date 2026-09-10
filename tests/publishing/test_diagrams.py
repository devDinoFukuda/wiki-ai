from __future__ import annotations

import re

from wiki_ai.publishing.diagrams import (
    dependency,
    diagrams_for,
    flowchart,
    sequence,
    state_machine,
)
from wiki_ai.publishing.model import DiagramKind, DiagramSpec

_INTERNAL_ID = re.compile(r"(ent_|rel_|evd_)[0-9a-f]{8,}")


def test_flowchart_follows_ordered_steps(query, graph):
    spec = flowchart(query, graph.nodes["capability"])
    assert spec is not None
    assert spec.kind is DiagramKind.FLOWCHART
    assert spec.mermaid_text.startswith("flowchart TD")
    assert spec.textual_equivalent[0].endswith("leva a Validar payload")
    assert spec.textual_equivalent[-1].endswith("leva a Gravar renovacao")


def test_sequence_links_entrypoint_capability_and_partners(query, graph):
    spec = sequence(query, graph.nodes["capability"])
    assert spec is not None
    assert spec.mermaid_text.startswith("sequenceDiagram")
    assert "POST /renewals aciona Renovacao" in spec.textual_equivalent
    assert "Renovacao chama Billing API" in spec.textual_equivalent


def test_state_diagram_lists_transitions(query, graph):
    spec = state_machine(query, graph.nodes["capability"])
    assert spec is not None
    assert spec.mermaid_text.startswith("stateDiagram-v2")
    assert "o estado Ativa vai para Renovada" in spec.textual_equivalent


def test_dependency_needs_module_scope(query, graph):
    assert dependency(query, graph.nodes["capability"]) is None


def test_every_diagram_has_textual_equivalent(query, graph):
    for subject in (graph.nodes["capability"], graph.nodes["system"]):
        for spec in diagrams_for(query, subject):
            assert spec.textual_equivalent


def test_mermaid_nodes_never_leak_internal_ids(query, graph):
    for spec in diagrams_for(query, graph.nodes["capability"]):
        assert not _INTERNAL_ID.search(spec.mermaid_text)


def test_diagram_without_textual_equivalent_is_refused():
    try:
        DiagramSpec(
            kind=DiagramKind.FLOWCHART,
            title="vazio",
            mermaid_text="flowchart TD",
            textual_equivalent=(),
        )
    except ValueError as exc:
        assert "equivalente textual" in str(exc)
    else:
        raise AssertionError("DiagramSpec aceitou diagrama sem equivalente textual")


def test_diagrams_are_deterministic(query, graph):
    first = diagrams_for(query, graph.nodes["capability"])
    second = diagrams_for(query, graph.nodes["capability"])
    assert [s.mermaid_text for s in first] == [s.mermaid_text for s in second]
