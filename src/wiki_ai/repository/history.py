from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Sequence

from wiki_ai.repository.snapshot import RepositorySnapshot, normalize_path

__all__ = [
    "Commit",
    "BlameSpan",
    "Rename",
    "HistoryReport",
    "DEFAULT_HISTORY_LIMIT",
    "GIT_TIMEOUT_SECONDS",
    "history",
]

DEFAULT_HISTORY_LIMIT = 20
GIT_TIMEOUT_SECONDS = 30
_MAX_BLAME_LINES = 400
_FIELD = "\x1f"
_RECORD = "\x1e"
_FORMAT = _RECORD + _FIELD.join(["%H", "%an", "%aI", "%s"])


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    date: str
    subject: str
    paths: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "sha": self.sha,
            "author": self.author,
            "date": self.date,
            "subject": self.subject,
            "paths": list(self.paths),
        }


@dataclass(frozen=True)
class BlameSpan:
    path: str
    line: int
    sha: str
    author: str
    date: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "sha": self.sha,
            "author": self.author,
            "date": self.date,
        }


@dataclass(frozen=True)
class Rename:
    sha: str
    old_path: str
    new_path: str

    def to_dict(self) -> dict[str, object]:
        return {"sha": self.sha, "old_path": self.old_path, "new_path": self.new_path}


@dataclass(frozen=True)
class HistoryReport:
    available: bool
    reason: str | None
    head: str | None
    path: str | None
    commits: tuple[Commit, ...]
    blame: tuple[BlameSpan, ...]
    renames: tuple[Rename, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "reason": self.reason,
            "head": self.head,
            "path": self.path,
            "commits": [item.to_dict() for item in self.commits],
            "blame": [item.to_dict() for item in self.blame],
            "renames": [item.to_dict() for item in self.renames],
        }


def _unavailable(reason: str, head: str | None, path: str | None) -> HistoryReport:
    return HistoryReport(
        available=False,
        reason=reason,
        head=head,
        path=path,
        commits=(),
        blame=(),
        renames=(),
    )


def _run(root: str, arguments: Sequence[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", root, *arguments],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return completed.returncode, completed.stdout


def _parse_log(raw: str) -> tuple[tuple[Commit, ...], tuple[Rename, ...]]:
    commits: list[Commit] = []
    renames: list[Rename] = []
    for block in raw.split(_RECORD):
        chunk = block.strip("\n")
        if not chunk.strip():
            continue
        header, _, remainder = chunk.partition("\n")
        fields = header.split(_FIELD)
        if len(fields) < 4:
            continue
        sha, author, date, subject = fields[0], fields[1], fields[2], fields[3]
        paths: list[str] = []
        for line in remainder.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0]
            if status.startswith("R") and len(parts) >= 3:
                renames.append(Rename(sha=sha, old_path=parts[1], new_path=parts[2]))
                paths.append(parts[2])
            else:
                paths.append(parts[1])
        commits.append(
            Commit(
                sha=sha,
                author=author,
                date=date,
                subject=subject,
                paths=tuple(paths),
            )
        )
    return tuple(commits), tuple(renames)


def _parse_blame(path: str, raw: str) -> tuple[BlameSpan, ...]:
    spans: list[BlameSpan] = []
    sha = ""
    author = ""
    date = ""
    line_number = 0
    for line in raw.splitlines():
        if line.startswith("author "):
            author = line[len("author ") :].strip()
            continue
        if line.startswith("author-time "):
            date = line[len("author-time ") :].strip()
            continue
        if line.startswith("\t"):
            if sha:
                spans.append(
                    BlameSpan(
                        path=path, line=line_number, sha=sha, author=author, date=date
                    )
                )
            continue
        parts = line.split(" ")
        if len(parts) >= 3 and len(parts[0]) == 40 and all(
            character in "0123456789abcdef" for character in parts[0]
        ):
            sha = parts[0]
            try:
                line_number = int(parts[2])
            except ValueError:
                line_number = 0
    return tuple(spans[:_MAX_BLAME_LINES])


def _escapes_root(relative: str) -> bool:
    if PurePosixPath(relative).is_absolute():
        return True
    return any(segment == ".." for segment in relative.split("/"))


def history(
    snapshot: RepositorySnapshot,
    path: str | None = None,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> HistoryReport:
    relative: str | None = None
    if path is not None:
        relative = normalize_path(path)
        if not relative or relative not in snapshot.file_map():
            return _unavailable("path_not_in_snapshot", snapshot.git_head, relative)
        if _escapes_root(relative):
            return _unavailable("path_outside_snapshot", snapshot.git_head, relative)
    if not snapshot.git_head:
        return _unavailable("not_a_git_repository", None, relative)
    if limit <= 0:
        return _unavailable("limit_must_be_positive", snapshot.git_head, relative)
    log_arguments = [
        "log",
        f"--max-count={int(limit)}",
        f"--format={_FORMAT}",
        "--name-status",
        "--find-renames",
        "--no-color",
    ]
    if relative is not None:
        log_arguments.extend(["--follow", "--", relative])
    code, raw = _run(snapshot.root, log_arguments)
    if code != 0:
        return _unavailable("git_command_failed", snapshot.git_head, relative)
    commits, renames = _parse_log(raw)
    blame: tuple[BlameSpan, ...] = ()
    if relative is not None:
        blame_code, blame_raw = _run(
            snapshot.root,
            ["blame", "--line-porcelain", "HEAD", "--", relative],
        )
        if blame_code == 0:
            blame = _parse_blame(relative, blame_raw)
    return HistoryReport(
        available=True,
        reason=None,
        head=snapshot.git_head,
        path=relative,
        commits=commits,
        blame=blame,
        renames=renames,
    )
