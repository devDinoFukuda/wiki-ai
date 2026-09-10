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
)
from wiki_ai.agent.session import AgentRun, AgentSession

__all__ = ["PROVIDER_NAME", "BINARY", "CodexProvider", "install"]

PROVIDER_NAME = "codex"
BINARY = "codex"
_WORKSPACE_PREFIX = "wiki-codex-"
_HOME_PREFIX = "wiki-codex-home-"
_CONFIG_NAME = "config.toml"
_HOME_ENV = "CODEX_HOME"
_SANDBOX = "read-only"
_PROBE_TIMEOUT = 30.0
_DEFAULT_TIMEOUT = 900.0


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(item) for item in values) + "]"


def config_document(host: str, port: int, timeout: float, tools: tuple[str, ...]) -> str:
    argv = bridge_command(host, port, timeout)
    lines = [
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {_toml_string(argv[0])}",
        f"args = {_toml_array(tuple(argv[1:]))}",
        "enabled = true",
        f"startup_timeout_sec = {int(timeout)}",
        f"enabled_tools = {_toml_array(tools)}",
        "",
        "[shell_environment_policy]",
        'inherit = "none"',
    ]
    return "\n".join(lines) + "\n"


def enabled_tools(session: AgentSession) -> tuple[str, ...]:
    return (*session.tool_names(), FINISH_TOOL)


class CodexProvider:
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
        self, session: AgentSession, workspace: Path, home: Path, broker: ToolBrokerServer
    ) -> list[str]:
        (home / _CONFIG_NAME).write_text(
            config_document(
                broker.host, broker.port, self._timeout, enabled_tools(session)
            ),
            encoding="utf-8",
        )
        argv = [
            *self._launcher,
            self._resolved or self._binary or BINARY,
            "exec",
            "--sandbox",
            _SANDBOX,
            "-C",
            str(workspace),
            "--skip-git-repo-check",
            "--json",
        ]
        if self._model:
            argv.extend(["--model", self._model])
        argv.append(prompt_builder.build(session))
        return argv

    def run(self, session: AgentSession) -> AgentRun:
        broker = ToolBrokerServer(session)
        broker.start()
        workspace = empty_workspace(_WORKSPACE_PREFIX)
        home = empty_workspace(_HOME_PREFIX)
        try:
            argv = self._argv(session, workspace, home, broker)
            result = self._runner.execute(
                argv,
                cwd=workspace,
                timeout=self._timeout,
                env={**self._env, _HOME_ENV: str(home)},
            )
        finally:
            broker.stop()
            shutil.rmtree(workspace, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)
        return settle(session, broker, result)


def install(registry: Any) -> None:
    registry.register(PROVIDER_NAME, CodexProvider)
