from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from wiki_ai.agent.protocol import AgentCapabilities
from wiki_ai.agent.session import AgentProvider, AgentRun, AgentSession
from wiki_ai.agent.registry import (
    DEFAULT_PROVIDER_MODULES,
    ProviderAlreadyRegistered,
    ProviderRegistry,
    ProviderUnavailable,
    RegistryError,
)


class _Reachable:
    def __init__(self) -> None:
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(tools=("repo.read",))

    def run(self, session: AgentSession) -> AgentRun:
        return session.finish([])

    def cancel(self) -> None:
        return None


class _Unreachable(_Reachable):
    def connect(self) -> None:
        raise OSError("no transport")


def test_default_registry_has_no_provider_modules() -> None:
    assert DEFAULT_PROVIDER_MODULES == ()


def test_empty_registry_reports_nothing_available() -> None:
    registry = ProviderRegistry()
    assert registry.available() == ()
    assert registry.registered() == ()


def test_empty_registry_refuses_to_resolve() -> None:
    registry = ProviderRegistry()
    with pytest.raises(ProviderUnavailable):
        registry.resolve()


def test_registered_reachable_provider_becomes_available() -> None:
    registry = ProviderRegistry()
    registry.register("alpha", _Reachable)
    assert registry.available() == ("alpha",)
    assert isinstance(registry.resolve(), AgentProvider)


def test_unreachable_provider_is_registered_but_not_available() -> None:
    registry = ProviderRegistry()
    registry.register("beta", _Unreachable)
    assert registry.registered() == ("beta",)
    assert registry.available() == ()
    with pytest.raises(ProviderUnavailable):
        registry.resolve()


def test_resolve_honours_preference() -> None:
    registry = ProviderRegistry()
    registry.register("alpha", _Unreachable)
    registry.register("gamma", _Reachable)
    resolved = registry.resolve("gamma")
    assert resolved.capabilities().supports("repo.read")
    with pytest.raises(ProviderUnavailable):
        registry.resolve("alpha")


def test_resolve_rejects_unknown_preference() -> None:
    registry = ProviderRegistry()
    registry.register("alpha", _Reachable)
    with pytest.raises(ProviderUnavailable) as caught:
        registry.resolve("absent")
    assert caught.value.registered == ("alpha",)


def test_duplicate_registration_is_rejected() -> None:
    registry = ProviderRegistry()
    registry.register("alpha", _Reachable)
    with pytest.raises(ProviderAlreadyRegistered):
        registry.register("alpha", _Reachable)


def test_empty_provider_name_is_rejected() -> None:
    registry = ProviderRegistry()
    with pytest.raises(RegistryError):
        registry.register("   ", _Reachable)


def test_discovery_imports_modules_and_calls_install() -> None:
    module_name = "wiki_ai_registry_probe"
    module = types.ModuleType(module_name)

    def install(registry: ProviderRegistry) -> None:
        registry.register("probe", _Reachable)

    module.install = install
    sys.modules[module_name] = module
    try:
        registry = ProviderRegistry((module_name,))
        assert registry.available() == ("probe",)
    finally:
        del sys.modules[module_name]


def test_discovery_ignores_missing_modules() -> None:
    registry = ProviderRegistry(("wiki_ai_registry_absent_module",))
    assert registry.registered() == ()
    assert registry.available() == ()
