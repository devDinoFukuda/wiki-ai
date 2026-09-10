from __future__ import annotations

import inspect

import pytest

from tests.investigation.fake_provider import FakeProvider
from wiki_ai.agent.providers.claude import ClaudeProvider
from wiki_ai.agent.providers.codex import CodexProvider
from wiki_ai.agent.session import AgentProvider, AgentRun, AgentSession
from wiki_ai.investigation.orchestrator import SessionProvider

CONCRETE = (ClaudeProvider, CodexProvider)


@pytest.mark.parametrize("factory", CONCRETE)
def test_every_shipped_provider_satisfies_the_agent_protocol(factory) -> None:
    assert isinstance(factory(), AgentProvider)


@pytest.mark.parametrize("factory", CONCRETE)
def test_every_shipped_provider_satisfies_the_investigation_protocol(factory) -> None:
    assert isinstance(factory(), SessionProvider)


def test_the_scripted_provider_satisfies_the_investigation_protocol() -> None:
    provider = FakeProvider()
    assert isinstance(provider, SessionProvider)
    assert not isinstance(provider, AgentProvider)


@pytest.mark.parametrize("factory", CONCRETE)
def test_the_declared_run_signature_matches_the_protocol(factory) -> None:
    declared = inspect.signature(AgentProvider.run)
    concrete = inspect.signature(factory.run)
    assert list(concrete.parameters) == list(declared.parameters)
    hints = inspect.get_annotations(factory.run, eval_str=True)
    assert hints["session"] is AgentSession
    assert hints["return"] is AgentRun


def test_the_protocol_declares_the_session_run_contract() -> None:
    hints = inspect.get_annotations(AgentProvider.run, eval_str=True)
    assert hints["session"] is AgentSession
    assert hints["return"] is AgentRun
