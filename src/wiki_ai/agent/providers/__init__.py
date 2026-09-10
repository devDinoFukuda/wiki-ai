from __future__ import annotations

import importlib
from typing import Any

__all__ = ["PROVIDER_MODULES", "install_all"]

PROVIDER_MODULES: tuple[str, ...] = (
    "wiki_ai.agent.providers.claude",
    "wiki_ai.agent.providers.codex",
)


def install_all(registry: Any, modules: tuple[str, ...] = PROVIDER_MODULES) -> None:
    for module_name in modules:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        install = getattr(module, "install", None)
        if callable(install):
            install(registry)
