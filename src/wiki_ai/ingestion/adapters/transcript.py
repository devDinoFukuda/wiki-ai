from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from wiki_ai.ingestion.adapters import documents
from wiki_ai.ingestion.adapters.blocks import BlockBuilder
from wiki_ai.ingestion.source import BlockKind, SourceDocument, SourceKind

__all__ = [
    "Utterance",
    "adapt",
    "parse_vtt",
    "parse_srt",
    "parse_plain",
    "UNKNOWN_SPEAKER",
    "EMPTY_TRANSCRIPT",
]

UNKNOWN_SPEAKER = "unknown"
EMPTY_TRANSCRIPT = "empty_transcript"

_CUE_TIME = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)"
)
_VOICE = re.compile(r"<v\s+(?P<speaker>[^>]+)>(?P<text>.*?)(?:</v>)?\s*$", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_BRACKET_TIME = re.compile(
    r"^\[(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<speaker>[^:]{1,80}):\s*(?P<text>.*)$"
)
_PAREN_TIME = re.compile(
    r"^(?P<speaker>[^()]{1,80}?)\s*\((?P<time>\d{1,2}:\d{2}(?::\d{2})?)\)\s*:\s*(?P<text>.*)$"
)
_SPEAKER_ONLY = re.compile(r"^(?P<speaker>[^:]{1,80}):\s*(?P<text>.+)$")
_INLINE_SPEAKER = re.compile(r"^(?P<speaker>[^:]{1,80}):\s*(?P<text>.*)$")
_TIME_ONLY = re.compile(r"^(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s*$")


@dataclass(frozen=True)
class Utterance:
    speaker: str
    start: str
    end: str
    text: str


def _normalize_time(raw: str) -> str:
    if not raw:
        return ""
    value = raw.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        parts = ["00"] + parts
    seconds = parts[-1]
    whole, _, fraction = seconds.partition(".")
    normalized = f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(whole):02d}"
    return f"{normalized}.{fraction[:3]}" if fraction else normalized


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub("", text)).strip()


def _cue_utterance(times: tuple[str, str], lines: list[str]) -> Utterance | None:
    body = " ".join(line for line in lines if line.strip())
    if not body.strip():
        return None
    voice = _VOICE.match(body.strip())
    if voice is not None:
        return Utterance(
            speaker=voice.group("speaker").strip(),
            start=times[0],
            end=times[1],
            text=_strip_tags(voice.group("text")),
        )
    plain = _strip_tags(body)
    inline = _INLINE_SPEAKER.match(plain)
    if inline is not None and inline.group("text").strip():
        return Utterance(
            speaker=inline.group("speaker").strip(),
            start=times[0],
            end=times[1],
            text=inline.group("text").strip(),
        )
    return Utterance(speaker=UNKNOWN_SPEAKER, start=times[0], end=times[1], text=plain)


def _parse_cues(text: str) -> list[Utterance]:
    found: list[Utterance] = []
    times: tuple[str, str] | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        match = _CUE_TIME.search(stripped)
        if match is not None:
            if times is not None:
                candidate = _cue_utterance(times, buffer)
                if candidate is not None:
                    found.append(candidate)
            times = (
                _normalize_time(match.group("start")),
                _normalize_time(match.group("end")),
            )
            buffer = []
            continue
        if times is None:
            continue
        if not stripped:
            candidate = _cue_utterance(times, buffer)
            if candidate is not None:
                found.append(candidate)
            times = None
            buffer = []
            continue
        if stripped.isdigit() and not buffer:
            continue
        buffer.append(stripped)
    if times is not None:
        candidate = _cue_utterance(times, buffer)
        if candidate is not None:
            found.append(candidate)
    return found


def parse_vtt(text: str) -> list[Utterance]:
    return _parse_cues(text)


def parse_srt(text: str) -> list[Utterance]:
    return _parse_cues(text)


def parse_plain(text: str) -> list[Utterance]:
    found: list[Utterance] = []
    pending_time = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        clock = _TIME_ONLY.match(stripped)
        if clock is not None:
            pending_time = _normalize_time(clock.group("time"))
            continue
        bracket = _BRACKET_TIME.match(stripped)
        if bracket is not None:
            found.append(
                Utterance(
                    speaker=bracket.group("speaker").strip(),
                    start=_normalize_time(bracket.group("time")),
                    end="",
                    text=bracket.group("text").strip(),
                )
            )
            pending_time = ""
            continue
        paren = _PAREN_TIME.match(stripped)
        if paren is not None:
            found.append(
                Utterance(
                    speaker=paren.group("speaker").strip(),
                    start=_normalize_time(paren.group("time")),
                    end="",
                    text=paren.group("text").strip(),
                )
            )
            pending_time = ""
            continue
        speaker = _SPEAKER_ONLY.match(stripped)
        if speaker is not None:
            found.append(
                Utterance(
                    speaker=speaker.group("speaker").strip(),
                    start=pending_time,
                    end="",
                    text=speaker.group("text").strip(),
                )
            )
            pending_time = ""
            continue
        if found:
            last = found[-1]
            found[-1] = Utterance(
                speaker=last.speaker,
                start=last.start,
                end=last.end,
                text=f"{last.text} {stripped}".strip(),
            )
        else:
            found.append(
                Utterance(
                    speaker=UNKNOWN_SPEAKER, start=pending_time, end="", text=stripped
                )
            )
            pending_time = ""
    return found


def group_consecutive(utterances: list[Utterance]) -> list[Utterance]:
    grouped: list[Utterance] = []
    for utterance in utterances:
        if grouped and grouped[-1].speaker == utterance.speaker:
            previous = grouped[-1]
            grouped[-1] = Utterance(
                speaker=previous.speaker,
                start=previous.start,
                end=utterance.end or previous.end,
                text=f"{previous.text} {utterance.text}".strip(),
            )
            continue
        grouped.append(utterance)
    return grouped


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _flavour(text: str, suffix: str) -> str:
    head = text.lstrip("﻿")
    if head.startswith("WEBVTT") or suffix == ".vtt":
        return "vtt"
    if suffix == ".srt":
        return "srt"
    if _CUE_TIME.search(head[:4000]):
        return "srt"
    return "plain"


def adapt(
    path: str | Path, payload: bytes, captured_at: datetime | None = None
) -> SourceDocument:
    target = Path(path)
    text = _decode(payload)
    flavour = _flavour(text, target.suffix.lower())
    if flavour == "vtt":
        utterances = parse_vtt(text)
    elif flavour == "srt":
        utterances = parse_srt(text)
    else:
        utterances = parse_plain(text)
    utterances = group_consecutive([u for u in utterances if u.text])

    metadata: dict[str, Any] = {
        "source_type": SourceKind.TRANSCRIPT.value,
        "title": target.name,
        "participants": sorted({u.speaker for u in utterances if u.speaker != UNKNOWN_SPEAKER}),
    }
    source, metadata_diagnostics = documents.build_source(
        target, SourceKind.TRANSCRIPT, payload, metadata, captured_at
    )
    builder = BlockBuilder(source.id)
    builder.extend_diagnostics(metadata_diagnostics)

    if not utterances:
        builder.warn(
            EMPTY_TRANSCRIPT,
            f"{target.name} yields no utterance under the {flavour} shape",
            {"speaker": UNKNOWN_SPEAKER, "time": "00:00:00"},
        )

    for index, utterance in enumerate(utterances, start=1):
        builder.add(
            BlockKind.UTTERANCE,
            utterance.text,
            {
                "speaker": utterance.speaker,
                "time_start": utterance.start,
                "time_end": utterance.end,
                "time": utterance.start or "00:00:00",
                "index": index,
            },
            attributes={"flavour": flavour},
        )

    return SourceDocument(
        source=source, blocks=builder.blocks, diagnostics=builder.diagnostics
    )
