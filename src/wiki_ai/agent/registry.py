from __future__ import annotations

import importlib
from typing import Callable

from wiki_ai.agent.session import AgentProvider

__all__ = [
    "RegistryError",
    "ProviderAlreadyRegistered",
    "ProviderUnavailable",
    "ProviderFactory",
    "DEFAULT_PROVIDER_MODULES",
    "ADAPTER_PACKAGE",
    "ProviderRegistry",
]

ProviderFactory = Callable[[], AgentProvider]

DEFAULT_PROVIDER_MODULES: tuple[str, ...] = ()

ADAPTER_PACKAGE = "wiki_ai.agent.providers"
_INSTALL_ALL = "install_all"


class RegistryError(Exception):
    pass


class ProviderAlreadyRegistered(RegistryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"provider already registered: {name}")
        self.name = name


class ProviderUnavailable(RegistryError):
    def __init__(self, preference: str | None, registered: tuple[str, ...]) -> None:
        super().__init__(
            f"no reachable provider for preference {preference!r}; "
            f"registered: {list(registered)}"
        )
        self.preference = preference
        self.registered = registered


class ProviderRegistry:
    def __init__(
        self,
        modules: tuple[str, ...] = DEFAULT_PROVIDER_MODULES,
        adapters: bool = False,
    ) -> None:
        self._factories: dict[str, ProviderFactory] = {}
        self._modules = tuple(modules)
        self._adapters = adapters
        self._loaded = False

    def register(self, name: str, factory: ProviderFactory) -> None:
        key = name.strip()
        if not key:
            raise RegistryError("provider name is empty")
        if key in self._factories:
            raise ProviderAlreadyRegistered(key)
        self._factories[key] = factory

    def registered(self) -> tuple[str, ...]:
        self._discover()
        return tuple(sorted(self._factories))

    def _install_adapters(self) -> None:
        try:
            package = importlib.import_module(ADAPTER_PACKAGE)
        except ImportError:
            return
        install_all = getattr(package, _INSTALL_ALL, None)
        if callable(install_all):
            install_all(self)

    def _discover(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._adapters:
            self._install_adapters()
        for module_name in self._modules:
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            install = getattr(module, "install", None)
            if callable(install):
                install(self)

    def _connected(self, name: str) -> AgentProvider | None:
        factory = self._factories.get(name)
        if factory is None:
            return None
        try:
            provider = factory()
            provider.connect()
        except Exception:
            return None
        return provider

    def available(self) -> tuple[str, ...]:
        self._discover()
        found: list[str] = []
        for name in sorted(self._factories):
            if self._connected(name) is not None:
                found.append(name)
        return tuple(found)

    def resolve(self, preference: str | None = None) -> AgentProvider:
        self._discover()
        if preference is not None:
            wanted = preference.strip()
            provider = self._connected(wanted) if wanted else None
            if provider is None:
                raise ProviderUnavailable(preference, tuple(sorted(self._factories)))
            return provider
        for name in sorted(self._factories):
            provider = self._connected(name)
            if provider is not None:
                return provider
        raise ProviderUnavailable(preference, tuple(sorted(self._factories)))
