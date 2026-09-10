from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from wiki_ai.agent.session import AgentRun, AgentSession, Budget
from wiki_ai.agent.providers.broker import ToolBrokerServer

__all__ = [
    "CliError",
    "BinaryMissing",
    "Outcome",
    "CliResult",
    "PASSTHROUGH_ENV",
    "minimal_env",
    "empty_workspace",
    "turns_for",
    "bridge_command",
    "bridge_path",
    "CliRunner",
    "settle",
    "run_with_broker",
]

PASSTHROUGH_ENV: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "PYTHONPATH",
)

_BRIDGE_LAUNCH = (
    "import sys;"
    "sys.path.insert(0, sys.argv.pop(1));"
    "from wiki_ai.agent.providers.bridge import main;"
    "raise SystemExit(main())"
)

_TURN_MULTIPLIER = 2
_TURN_HEADROOM = 4
_TERMINATE_GRACE = 5.0


class CliError(Exception):
    pass


class BinaryMissing(CliError):
    def __init__(self, binary: str) -> None:
        super().__init__(f"agent binary not found: {binary}")
        self.binary = binary


class Outcome(Enum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class CliResult:
    outcome: Outcome
    exit_code: int | None
    stdout: str
    stderr: str


def minimal_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        name: os.environ[name] for name in PASSTHROUGH_ENV if name in os.environ
    }
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def empty_workspace(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def turns_for(budget: Budget) -> int:
    return budget.max_tool_calls * _TURN_MULTIPLIER + _TURN_HEADROOM


def bridge_path() -> str:
    return str(Path(__file__).resolve().parents[3])


def bridge_command(host: str, port: int, timeout: float) -> list[str]:
    return [
        sys.executable,
        "-c",
        _BRIDGE_LAUNCH,
        bridge_path(),
        "--host",
        host,
        "--port",
        str(port),
        "--timeout",
        f"{timeout}",
    ]


class CliRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = False

    @staticmethod
    def locate(binary: str) -> str:
        found = shutil.which(binary)
        if found is None:
            raise BinaryMissing(binary)
        return found

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            process = self._process
        if process is not None and process.poll() is None:
            self._kill(process)

    @staticmethod
    def _kill(process: subprocess.Popen[str]) -> None:
        process.terminate()
        try:
            process.wait(timeout=_TERMINATE_GRACE)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_TERMINATE_GRACE)

    def probe(
        self,
        argv: Sequence[str],
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> CliResult:
        workspace = empty_workspace("wiki-probe-")
        try:
            return self.execute(argv, cwd=workspace, timeout=timeout, env=env)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def execute(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout: float,
        env: Mapping[str, str] | None = None,
        stdin_text: str | None = None,
    ) -> CliResult:
        with self._lock:
            if self._cancelled:
                return CliResult(Outcome.CANCELLED, None, "", "")
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=str(cwd),
                env=minimal_env(env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            return CliResult(Outcome.FAILED, None, "", str(exc))
        with self._lock:
            self._process = process
            cancelled = self._cancelled
        if cancelled:
            self._kill(process)
            return CliResult(Outcome.CANCELLED, process.poll(), "", "")
        try:
            stdout, stderr = process.communicate(input=stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(process)
            stdout, stderr = process.communicate()
            return CliResult(Outcome.TIMEOUT, process.poll(), stdout or "", stderr or "")
        finally:
            with self._lock:
                self._process = None
        with self._lock:
            if self._cancelled:
                return CliResult(Outcome.CANCELLED, process.returncode, stdout, stderr)
        outcome = Outcome.COMPLETED if process.returncode == 0 else Outcome.FAILED
        return CliResult(outcome, process.returncode, stdout, stderr)


def run_with_broker(
    session: AgentSession,
    runner: CliRunner,
    build_argv: object,
    workspace_prefix: str,
    timeout: float,
) -> AgentRun:
    broker = ToolBrokerServer(session)
    broker.start()
    workspace = empty_workspace(workspace_prefix)
    try:
        argv, env, stdin_text = build_argv(broker, workspace)
        result = runner.execute(
            argv,
            cwd=workspace,
            timeout=timeout,
            env=env,
            stdin_text=stdin_text,
        )
    finally:
        broker.stop()
        shutil.rmtree(workspace, ignore_errors=True)
    return settle(session, broker, result)


def settle(
    session: AgentSession,
    broker: ToolBrokerServer,
    result: CliResult,
) -> AgentRun:
    if broker.budget_exhausted:
        return session.budget_exhausted("budget_exhausted")
    if result.outcome is Outcome.CANCELLED:
        return session.cancelled("cancelled")
    if result.outcome is Outcome.TIMEOUT:
        return session.fail("timeout")
    if not broker.finish.delivered:
        if result.outcome is Outcome.FAILED:
            return session.fail("agent_failed")
        return session.fail("findings_not_delivered")
    if result.outcome is Outcome.FAILED:
        return session.fail("agent_failed")
    return session.finish(broker.finish.findings)
