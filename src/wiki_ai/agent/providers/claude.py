from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from wiki_ai.agent.protocol import AgentCapabilities
from wiki_ai.agent.providers import prompt as prompt_builder
from wiki_ai.agent.providers.bridge import FINISH_TOOL, SERVER_NAME
from wiki_ai.agent.providers.broker import ToolBrokerServer
from wiki_ai.agent.providers.cli_common import (
    CliRunner,
    bridge_command,
    empty_workspace,
    settle,
    turns_for,
)
from wiki_ai.agent.session import AgentRun, AgentSession

__all__ = [
    "PROVIDER_NAME",
    "BINARY",
    "DISALLOWED_TOOLS",
    "ClaudeProvider",
    "install",
]

PROVIDER_NAME = "claude"
BINARY = "claude"
_WORKSPACE_PREFIX = "wiki-claude-"
_CONFIG_PREFIX = "wiki-claude-config-"
_CONFIG_NAME = "mcp-config.json"
DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
)
_PROBE_TIMEOUT = 30.0
_DEFAULT_TIMEOUT = 900.0


def _mcp_config(host: str, port: int, timeout: float) -> dict[str, Any]:
    argv = bridge_command(host, port, timeout)
    return {
        "mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": argv[0],
                "args": argv[1:],
                "env": {},
            }
        }
    }


def _qualified(tool: str) -> str:
    return f"mcp__{SERVER_NAME}__{tool}"


def allowed_tools(session: AgentSession) -> tuple[str, ...]:
    names = [*session.tool_names(), FINISH_TOOL]
    return tuple(_qualified(name) for name in names)


class ClaudeProvider:
    def __init__(
        self,
        binary: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        model: str | None = None,
        env: Mapping[str, str] | None = None,
        launcher: Sequence[str] = (),
    ) -> None:
        self._launcher = tuple(launcher)
        self._binary = binary
        self._timeout = timeout
        self._model = model
        self._env = dict(env or {})
        self._runner = CliRunner()
        self._resolved: str | None = None

    def connect(self) -> None:
        binary = self._binary or CliRunner.locate(BINARY)
        result = self._runner.probe(
            [*self._launcher, binary, "--version"], timeout=_PROBE_TIMEOUT
        )
        if result.exit_code != 0:
            raise OSError(f"{BINARY} --version failed: {result.stderr.strip()}")
        self._resolved = binary

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(tools=(FINISH_TOOL,))

    def cancel(self) -> None:
        self._runner.cancel()

    def _argv(
        self, session: AgentSession, settings: Path, broker: ToolBrokerServer
    ) -> list[str]:
        config = settings / _CONFIG_NAME
        config.write_text(
            json.dumps(_mcp_config(broker.host, broker.port, self._timeout)),
            encoding="utf-8",
        )
        argv = [
            *self._launcher,
            self._resolved or self._binary or BINARY,
            "-p",
            prompt_builder.build(session),
            "--output-format",
            "json",
            "--mcp-config",
            str(config),
            "--allowedTools",
            *allowed_tools(session),
            "--disallowedTools",
            *DISALLOWED_TOOLS,
            "--max-turns",
            str(turns_for(session.budget)),
        ]
        if self._model:
            argv.extend(["--model", self._model])
        return argv

    def run(self, session: AgentSession) -> AgentRun:
        broker = ToolBrokerServer(session)
        broker.start()
        workspace = empty_workspace(_WORKSPACE_PREFIX)
        settings = empty_workspace(_CONFIG_PREFIX)
        try:
            argv = self._argv(session, settings, broker)
            result = self._runner.execute(
                argv, cwd=workspace, timeout=self._timeout, env=self._env
            )
        finally:
            broker.stop()
            shutil.rmtree(workspace, ignore_errors=True)
            shutil.rmtree(settings, ignore_errors=True)
        return settle(session, broker, result)


def install(registry: Any) -> None:
    registry.register(PROVIDER_NAME, ClaudeProvider)
