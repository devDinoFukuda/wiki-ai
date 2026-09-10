from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Sequence, TextIO

from wiki_ai.agent.registry import ProviderRegistry
from wiki_ai.app import api
from wiki_ai.app.wiring import Wiring

__all__ = [
    "CommandLineInvalid",
    "PROGRAM_NAME",
    "COMMANDS",
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_BLOCKED",
    "build_parser",
    "main",
]

PROGRAM_NAME = "wiki-ai"
COMMANDS = ("analyze", "ingest", "ask", "publish", "status", "version", "inspect")
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2

_REPO_OPTION = "--repo"
_DEFAULT_REPO = "."


class CommandLineInvalid(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CommandLineInvalid(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise CommandLineInvalid(message or f"exit status {status}")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROGRAM_NAME, add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser(COMMANDS[0], add_help=False)
    analyze_parser.add_argument("repo", nargs="?", default=_DEFAULT_REPO)
    analyze_parser.add_argument("--objective", default=None)

    ingest_parser = subparsers.add_parser(COMMANDS[1], add_help=False)
    ingest_parser.add_argument("source")
    ingest_parser.add_argument(_REPO_OPTION, default=_DEFAULT_REPO)

    ask_parser = subparsers.add_parser(COMMANDS[2], add_help=False)
    ask_parser.add_argument("question")
    ask_parser.add_argument(_REPO_OPTION, default=_DEFAULT_REPO)

    publish_parser = subparsers.add_parser(COMMANDS[3], add_help=False)
    publish_parser.add_argument(_REPO_OPTION, default=_DEFAULT_REPO)

    status_parser = subparsers.add_parser(COMMANDS[4], add_help=False)
    status_parser.add_argument(_REPO_OPTION, default=_DEFAULT_REPO)

    subparsers.add_parser(COMMANDS[5], add_help=False)

    inspect_parser = subparsers.add_parser(COMMANDS[6], add_help=False)
    inspect_parser.add_argument("repo", nargs="?", default=_DEFAULT_REPO)

    return parser


def _emit(payload: Mapping[str, Any], code: int, stream: TextIO) -> int:
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return code


def _exit_code(payload: Mapping[str, Any]) -> int:
    if payload.get("status") == "blocked":
        return EXIT_BLOCKED
    if payload.get("status") == "error":
        return EXIT_ERROR
    return EXIT_OK


def _guarded(
    command: str, operation: Callable[[], Mapping[str, Any]], stream: TextIO
) -> int:
    try:
        payload = dict(operation())
    except api.OutdatedStore:
        return _emit(
            {
                "status": "blocked",
                "reason": "outdated_store",
                "action": "run a new analysis",
            },
            EXIT_BLOCKED,
            stream,
        )
    except api.ApiError as exc:
        return _emit(
            {"status": "error", "reason": f"{command}_failed", "detail": str(exc)},
            EXIT_ERROR,
            stream,
        )
    payload["command"] = command
    return _emit(payload, _exit_code(payload), stream)


def _run(
    command: str,
    parsed: argparse.Namespace,
    stream: TextIO,
    registry: ProviderRegistry | None,
) -> int:
    wiring = Wiring() if registry is None else Wiring(registry)
    if command == COMMANDS[0]:
        repo = Path(parsed.repo)
        return _guarded(
            command, lambda: api.analyze(repo, parsed.objective, wiring).to_dict(), stream
        )
    if command == COMMANDS[1]:
        source = Path(parsed.source)
        repo = Path(parsed.repo)
        return _guarded(
            command, lambda: api.ingest(source, repo, wiring).to_dict(), stream
        )
    if command == COMMANDS[2]:
        repo = Path(parsed.repo)
        return _guarded(
            command, lambda: api.ask(parsed.question, repo, wiring).to_dict(), stream
        )
    if command == COMMANDS[3]:
        repo = Path(parsed.repo)
        return _guarded(command, lambda: api.publish(repo, wiring).to_dict(), stream)
    if command == COMMANDS[4]:
        repo = Path(parsed.repo)
        return _guarded(command, lambda: api.status(repo, wiring).to_dict(), stream)
    if command == COMMANDS[5]:
        return _emit(
            {"status": "ok", "command": command, "version": api.version()},
            EXIT_OK,
            stream,
        )
    repo = Path(parsed.repo)
    return _guarded(
        command, lambda: {"status": "ok", **api.inspect(repo).to_dict()}, stream
    )


def main(
    argv: Sequence[str] | None = None,
    stream: TextIO | None = None,
    registry: ProviderRegistry | None = None,
) -> int:
    output = stream if stream is not None else sys.stdout
    arguments = list(argv) if argv is not None else sys.argv[1:]
    try:
        parsed = build_parser().parse_args(arguments)
    except CommandLineInvalid as exc:
        payload = {
            "status": "error",
            "reason": "invalid_arguments",
            "detail": str(exc),
            "commands": list(COMMANDS),
        }
        return _emit(payload, EXIT_ERROR, output)
    return _run(str(parsed.command), parsed, output, registry)
