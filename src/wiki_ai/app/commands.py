from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence, TextIO

from wiki_ai.app import api

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
COMMANDS = ("version", "inspect")
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2


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
    subparsers.add_parser(COMMANDS[0], add_help=False)
    inspect_parser = subparsers.add_parser(COMMANDS[1], add_help=False)
    inspect_parser.add_argument("repo")
    return parser


def _emit(payload: Mapping[str, Any], code: int, stream: TextIO) -> int:
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return code


def _run_version(stream: TextIO) -> int:
    payload = {"status": "ok", "command": COMMANDS[0], "version": api.version()}
    return _emit(payload, EXIT_OK, stream)


def _run_inspect(repo: Path, stream: TextIO) -> int:
    try:
        report = api.inspect(repo)
    except api.OutdatedStore:
        payload = {
            "status": "blocked",
            "reason": "outdated_store",
            "action": "run a new analysis",
        }
        return _emit(payload, EXIT_BLOCKED, stream)
    except api.ApiError as exc:
        payload = {"status": "error", "reason": "inspect_failed", "detail": str(exc)}
        return _emit(payload, EXIT_ERROR, stream)
    payload = {"status": "ok", "command": COMMANDS[1]}
    payload.update(report.to_dict())
    return _emit(payload, EXIT_OK, stream)


def main(argv: Sequence[str] | None = None, stream: TextIO | None = None) -> int:
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
    if parsed.command == COMMANDS[0]:
        return _run_version(output)
    return _run_inspect(Path(parsed.repo), output)
