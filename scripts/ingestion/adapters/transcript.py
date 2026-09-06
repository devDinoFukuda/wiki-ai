"""Adapter de transcrição: SRT, VTT e TXT com falas (§8.1).

Produz blocos `timestamped_utterance` com localizador `SourceKind.TRANSCRIPT`:
arquivo/versão, bloco, intervalo de tempo quando disponível e interlocutor
quando identificado (§5.4).

O que este adapter NÃO faz: converter transcrição em fato. Afirmações,
decisões, dúvidas e ações são responsabilidade de `extract.py`; aqui a
transcrição sai preservada, com o contexto de cada fala intacto.

Interlocutor só é preenchido quando há marca explícita — `<v Nome>` no VTT,
`Nome:` no início da fala. Fala sem marca fica sem `speaker`, e não com um
palpite; o mesmo vale para o intervalo de tempo, que é all-or-nothing.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..normalize import (
    Block,
    BlockKind,
    ContentKind,
    Diagnostic,
    Preserved,
    Severity,
    SourceDocument,
    SourceKind,
    build_document,
    decode_text,
    make_block,
    preserve,
    version_label,
)

NAME = "transcript"
EXTENSIONS = frozenset({".srt", ".vtt"})

#: `00:01:02,500 --> 00:01:07,000` (SRT) e `00:01:02.500 --> 00:01:07.000` (VTT).
_CUE_TIME = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}|\d{1,2}:\d{2}(?::\d{2})?)"
    r"\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}|\d{1,2}:\d{2}(?::\d{2})?)"
)
#: `<v Nome da Pessoa>` (WebVTT voice span).
_VOICE = re.compile(r"<v(?:\.[^\s>]+)*\s+([^>]+)>")
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
#: `[00:12:33] Nome: fala` — timestamp entre colchetes/parênteses.
_TS_LINE = re.compile(
    r"^\s*[\[(]?(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)[\])]?\s*[-–]?\s*"
    r"(?:(?P<speaker>[^:\n]{1,60}?)\s*:)?\s*(?P<text>.*)$"
)
#: `Nome: fala` sem timestamp. Nome curto, sem pontuação de frase.
_SPEAKER_LINE = re.compile(r"^\s*(?P<speaker>[^\s:][^:\n]{0,58}?)\s*:\s+(?P<text>\S.*)$")
_SPEAKER_REJECT = re.compile(r"[.!?;]|https?$|^\d+$")


def detect(path: str, head_bytes: bytes) -> bool:
    """SRT/VTT pela extensão ou cabeçalho; TXT só com padrão de fala visível."""
    ext = os.path.splitext(path)[1].lower()
    head = head_bytes[:4096]
    try:
        text = head.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - decode com replace não levanta
        return False
    if ext in EXTENSIONS:
        return True
    if text.lstrip("﻿").startswith("WEBVTT"):
        return True
    if ext not in (".txt", ".text", ""):
        return False
    if _CUE_TIME.search(text):
        return True
    # TXT: exige ao menos duas falas reconhecíveis, para não capturar um
    # documento comum que por acaso tem uma linha `Nota: ...`.
    hits = 0
    for line in text.split("\n")[:80]:
        if _timestamped_speech(line) or _plain_speech(line):
            hits += 1
    return hits >= 2


def _timestamped_speech(line: str) -> tuple[str, str, str] | None:
    """`[hh:mm:ss] Nome: fala` → (timestamp, interlocutor, texto)."""
    match = _TS_LINE.match(line)
    if not match or not match.group("text").strip():
        return None
    speaker = (match.group("speaker") or "").strip()
    if speaker and _SPEAKER_REJECT.search(speaker):
        return None
    return match.group("ts"), speaker, match.group("text").strip()


def _plain_speech(line: str) -> tuple[str, str] | None:
    """`Nome: fala` → (interlocutor, texto)."""
    match = _SPEAKER_LINE.match(line)
    if not match:
        return None
    speaker = match.group("speaker").strip()
    if _SPEAKER_REJECT.search(speaker) or " " in speaker.strip() and len(speaker.split()) > 5:
        return None
    return speaker, match.group("text").strip()


def normalize_time(value: str) -> str:
    """Normaliza para `hh:mm:ss.mmm`, sem inventar precisão ausente."""
    value = value.strip().replace(",", ".")
    head, _, frac = value.partition(".")
    parts = head.split(":")
    if len(parts) == 2:
        parts = ["00"] + parts
    parts = [p.zfill(2) for p in parts]
    base = ":".join(parts)
    return f"{base}.{frac.ljust(3, '0')[:3]}" if frac else base


def _split_speaker(text: str) -> tuple[str, str]:
    """Extrai `Nome:` do início da fala, quando presente."""
    voice = _VOICE.search(text)
    if voice:
        speaker = voice.group(1).strip()
        return speaker, _TAG.sub("", text).strip()
    plain = _plain_speech(text.split("\n", 1)[0])
    if plain:
        speaker, first = plain
        remainder = text.split("\n", 1)[1] if "\n" in text else ""
        body = (first + ("\n" + remainder if remainder else "")).strip()
        return speaker, body
    return "", text.strip()


def parse_cues(text: str, file_name: str, version: str) -> tuple[list[Block], list[Diagnostic]]:
    """SRT/VTT: cada cue vira um `timestamped_utterance`."""
    blocks: list[Block] = []
    diags: list[Diagnostic] = []
    body = text.lstrip("﻿")
    if body.startswith("WEBVTT"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    chunks = re.split(r"\r?\n\s*\r?\n", body)
    for chunk in chunks:
        lines = [line.rstrip("\r") for line in chunk.split("\n") if line.strip()]
        if not lines:
            continue
        if lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        time_match = None
        cue_body_start = 0
        for offset, line in enumerate(lines[:3]):
            found = _CUE_TIME.search(line)
            if found:
                time_match = found
                cue_body_start = offset + 1
                break
        cue_text = "\n".join(lines[cue_body_start:]).strip()
        if not cue_text:
            if time_match:
                diags.append(
                    Diagnostic(
                        code="transcript.empty_cue",
                        severity=Severity.WARNING,
                        message=f"cue sem texto em {time_match.group('start')}",
                        unavailable=(f"fala em {time_match.group('start')}",),
                    )
                )
            continue
        speaker, spoken = _split_speaker(cue_text)
        if time_match:
            start = normalize_time(time_match.group("start"))
            end = normalize_time(time_match.group("end"))
        else:
            start = end = None
            diags.append(
                Diagnostic(
                    code="transcript.cue_without_time",
                    severity=Severity.WARNING,
                    message=f"bloco sem intervalo de tempo reconhecível: {spoken[:60]!r}",
                    unavailable=("intervalo de tempo do bloco",),
                )
            )
        blocks.append(
            make_block(
                len(blocks),
                BlockKind.TIMESTAMPED_UTTERANCE,
                spoken,
                source_kind=SourceKind.TRANSCRIPT,
                content_kind=ContentKind.TRANSCRIPT_BLOCK,
                version=version,
                file=file_name,
                time_start=start,
                time_end=end,
                speaker=speaker or None,
            )
        )
    return blocks, diags


def parse_plain(text: str, file_name: str, version: str) -> tuple[list[Block], list[Diagnostic]]:
    """TXT: `[hh:mm:ss] Nome: fala` e `Nome: fala`.

    Sem timestamp de fim, o intervalo é fechado com o início da fala seguinte;
    a última fala fica sem intervalo em vez de receber um fim inventado.
    """
    blocks: list[Block] = []
    diags: list[Diagnostic] = []
    pending: list[tuple[str | None, str, list[str]]] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        stamped = _timestamped_speech(line)
        if stamped:
            ts, speaker, spoken = stamped
            pending.append((normalize_time(ts), speaker, [spoken]))
            continue
        plain = _plain_speech(line)
        if plain:
            speaker, spoken = plain
            pending.append((None, speaker, [spoken]))
            continue
        if pending:
            pending[-1][2].append(line.strip())
        else:
            pending.append((None, "", [line.strip()]))
    starts = [p[0] for p in pending]
    for position, (start, speaker, parts) in enumerate(pending):
        spoken = "\n".join(p for p in parts if p).strip()
        if not spoken:
            continue
        end = None
        if start is not None:
            following = next((s for s in starts[position + 1:] if s is not None), None)
            end = following
            if end is None:
                diags.append(
                    Diagnostic(
                        code="transcript.open_interval",
                        severity=Severity.INFO,
                        message=(
                            f"fala iniciada em {start} sem fim declarado nem fala seguinte; "
                            "registrada sem intervalo"
                        ),
                        unavailable=(f"fim do intervalo da fala em {start}",),
                    )
                )
                start = None
        if not speaker:
            diags.append(
                Diagnostic(
                    code="transcript.speaker_unknown",
                    severity=Severity.INFO,
                    message=f"bloco sem interlocutor identificável: {spoken[:60]!r}",
                    unavailable=("interlocutor do bloco",),
                )
            )
        blocks.append(
            make_block(
                len(blocks),
                BlockKind.TIMESTAMPED_UTTERANCE,
                spoken,
                source_kind=SourceKind.TRANSCRIPT,
                content_kind=ContentKind.TRANSCRIPT_BLOCK,
                version=version,
                file=file_name,
                time_start=start,
                time_end=end,
                speaker=speaker or None,
            )
        )
    return blocks, diags


def extract(path: str, preserved: Preserved | None = None) -> SourceDocument:
    """SRT/VTT/TXT com falas → `SourceDocument` de transcrição."""
    pres = preserved or preserve(path)
    text, diags = decode_text(pres.raw)
    version = version_label(pres.bytes_sha256)
    file_name = os.path.basename(pres.path_original)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".vtt" or text.lstrip("﻿").startswith("WEBVTT"):
        kind = "vtt"
        blocks, parse_diags = parse_cues(text, file_name, version)
    elif ext == ".srt" or _CUE_TIME.search(text[:4096]):
        kind = "srt"
        blocks, parse_diags = parse_cues(text, file_name, version)
    else:
        kind = "transcript_text"
        blocks, parse_diags = parse_plain(text, file_name, version)
    diags.extend(parse_diags)
    metadata: dict[str, Any] = {"source_type": "transcript"}
    speakers = sorted({b.locator.get("speaker", "") for b in blocks if b.locator.get("speaker")})
    if speakers:
        metadata["participants"] = speakers
    if not blocks:
        diags.append(
            Diagnostic(
                code="transcript.empty",
                severity=Severity.ERROR,
                message="nenhuma fala reconhecível na transcrição",
                unavailable=("todas as falas",),
                path=pres.path_original,
            )
        )
    return build_document(
        pres,
        kind=kind,
        adapter=NAME,
        blocks=blocks,
        raw_metadata=metadata,
        diagnostics=diags,
    )
