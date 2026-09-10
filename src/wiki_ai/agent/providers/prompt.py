from __future__ import annotations

import json

from wiki_ai.agent.providers.bridge import FINISH_TOOL
from wiki_ai.agent.session import AgentSession

__all__ = ["build"]

_HEADER = "Objective:"
_TOOLS = "Available tools (the only ones you may use):"
_RULES = (
    "Rules:",
    "- Investigate by calling the tools; do not guess.",
    "- The tools are read-only and are your only access to the sources.",
    "- Every claim must carry evidence with source_id, version and locator.",
    f"- When the objective is answered, call {FINISH_TOOL} once with the findings.",
    f"- {FINISH_TOOL} is the only way to deliver a result.",
)
_SCHEMA = "Each finding must match this JSON schema:"


def build(session: AgentSession) -> str:
    lines = [_HEADER, session.objective, "", _TOOLS]
    for name in session.tool_names():
        spec = session.tool(name)
        lines.append(f"- {spec.name}: {spec.description}")
        if spec.input_schema:
            lines.append(f"  input: {json.dumps(dict(spec.input_schema), sort_keys=True)}")
    lines.append(f"- {FINISH_TOOL}: deliver the findings and end the investigation")
    lines.extend(("", *_RULES, "", _SCHEMA))
    lines.append(json.dumps(dict(session.finding_schema), sort_keys=True))
    return "\n".join(lines)
