from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from .xmltools import XML_DECLARATION, escape_text, local_name, parse_xml

APPLICATION_NAME = "wiki-ai"
KEYWORD_SEPARATOR = "; "
MAX_STEM_LENGTH = 120

_W3CDTF = "%Y-%m-%dT%H:%M:%SZ"
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FORBIDDEN_FILENAME_CHARS = re.compile(r'["*:<>?/\|~#%&{}\s]')
_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul", "lock", "desktop.ini"}
    | {f"com{n}" for n in range(10)}
    | {f"lpt{n}" for n in range(10)}
)
_SHAREPOINT_MARKER = "_vti_"


@dataclass(frozen=True, slots=True)
class CoreProperties:
    title: str = ""
    subject: str = ""
    creator: str = ""
    keywords: tuple[str, ...] = ()
    description: str = ""
    created: datetime | None = None
    modified: datetime | None = None
    revision: int = 1
    category: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "keywords", tuple(self.keywords))


def format_timestamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        return moment.strftime(_W3CDTF)
    return moment.astimezone(timezone.utc).strftime(_W3CDTF)


def parse_timestamp(value: str) -> datetime | None:
    text = (value or "").strip()
    if _DATE_ONLY.match(text):
        text = f"{text}T00:00:00Z"
    try:
        parsed = datetime.strptime(text, _W3CDTF)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def join_keywords(keywords: Sequence[str]) -> str:
    return KEYWORD_SEPARATOR.join(k.strip() for k in keywords if k.strip())


def split_keywords(value: str) -> tuple[str, ...]:
    parts = re.split(r"[;,]", value or "")
    return tuple(p.strip() for p in parts if p.strip())


def sharepoint_safe_stem(stem: str) -> str:
    text = (stem or "").lower()
    if text.startswith("~$"):
        text = text[2:]
    text = _FORBIDDEN_FILENAME_CHARS.sub("-", text)
    text = re.sub(r"\.{2,}", "-", text)
    text = re.sub(r"-+", "-", text).strip("- .")
    if not text:
        text = "documento"
    if text in _RESERVED_STEMS or _SHAREPOINT_MARKER in text:
        text = f"{text}-doc"
    if len(text) > MAX_STEM_LENGTH:
        text = f"{text[:60]}-{text[-59:]}"
    return text


def sharepoint_safe_filename(stem: str) -> str:
    return f"{sharepoint_safe_stem(stem)}.docx"


def _element(tag: str, value: str) -> str:
    return f"<{tag}>{escape_text(value)}</{tag}>"


def _optional(tag: str, value: str) -> str:
    return _element(tag, value) if value else ""


def _timestamp_element(tag: str, moment: datetime | None) -> str:
    if moment is None:
        return ""
    return (
        f'<{tag} xsi:type="dcterms:W3CDTF">'
        f"{escape_text(format_timestamp(moment))}"
        f"</{tag}>"
    )


def core_xml(properties: CoreProperties) -> str:
    parts = (
        _optional("cp:category", properties.category),
        _timestamp_element("dcterms:created", properties.created),
        _optional("dc:creator", properties.creator),
        _optional("dc:description", properties.description),
        _optional("cp:keywords", join_keywords(properties.keywords)),
        _timestamp_element("dcterms:modified", properties.modified),
        _element("cp:revision", str(properties.revision)),
        _optional("dc:subject", properties.subject),
        _optional("dc:title", properties.title),
    )
    return (
        XML_DECLARATION
        + "<cp:coreProperties"
        ' xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:dcterms="http://purl.org/dc/terms/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        + "".join(parts)
        + "</cp:coreProperties>"
    )


def app_xml(properties: CoreProperties) -> str:
    company = _element("Company", properties.creator) if properties.creator else ""
    return (
        XML_DECLARATION
        + "<Properties"
        ' xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"'
        ' xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        + _element("Application", APPLICATION_NAME)
        + "<DocSecurity>0</DocSecurity>"
        + "<ScaleCrop>false</ScaleCrop>"
        + company
        + "<LinksUpToDate>false</LinksUpToDate>"
        + "<SharedDoc>false</SharedDoc>"
        + "<HyperlinksChanged>false</HyperlinksChanged>"
        + "<AppVersion>1.0000</AppVersion>"
        + "</Properties>"
    )


def parse_core_xml(data: bytes) -> CoreProperties:
    root = parse_xml("docProps/core.xml", data)
    values = {local_name(child.tag): (child.text or "") for child in root}
    created = parse_timestamp(values.get("created", ""))
    modified = parse_timestamp(values.get("modified", ""))
    revision = values.get("revision", "1").strip()
    return CoreProperties(
        title=values.get("title", ""),
        subject=values.get("subject", ""),
        creator=values.get("creator", ""),
        keywords=split_keywords(values.get("keywords", "")),
        description=values.get("description", ""),
        created=created,
        modified=modified,
        revision=int(revision) if revision.isdigit() else 1,
        category=values.get("category", ""),
    )
