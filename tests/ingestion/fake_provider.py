from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from wiki_ai.agent.protocol import ToolCall
from wiki_ai.agent.session import AgentRun, AgentSession

Step = tuple[str, Mapping[str, Any]]
Payloads = dict[str, Any]
FindingBuilder = Callable[[Payloads], Sequence[Mapping[str, Any]]]


@dataclass
class Script:
    steps: tuple[Step, ...] = ()
    findings: Sequence[Mapping[str, Any]] = ()
    build_findings: FindingBuilder | None = None
    fail_with: str = ""


@dataclass
class FakeProvider:
    scripts: list[Script] = field(default_factory=list)
    seen_objectives: list[str] = field(default_factory=list)
    seen_schemas: list[Mapping[str, Any]] = field(default_factory=list)
    seen_tools: list[tuple[str, ...]] = field(default_factory=list)
    captures: list[Mapping[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    calls: int = 0

    def run(self, session: AgentSession) -> AgentRun:
        index = self.calls
        self.calls += 1
        self.seen_objectives.append(session.objective)
        self.seen_schemas.append(session.finding_schema)
        self.seen_tools.append(session.tool_names())
        script = self.scripts[index] if index < len(self.scripts) else Script()
        payloads: Payloads = {}
        for name, arguments in script.steps:
            result = session.invoke(ToolCall(name=name, arguments=arguments))
            if not result.ok:
                self.errors.append(result.error)
                continue
            payload = dict(result.payload)
            if name == "evidence.capture":
                self.captures.append(payload)
                payloads.setdefault("captures", []).append(payload)
                payloads[payload["capture_id"]] = payload
            else:
                payloads.setdefault(name, []).append(payload)
        if script.fail_with:
            return session.fail(script.fail_with)
        findings = (
            script.build_findings(payloads)
            if script.build_findings is not None
            else script.findings
        )
        return session.finish(list(findings))


def last_capture(payloads: Payloads) -> Mapping[str, Any]:
    return payloads["captures"][-1]


def evidence_of(payloads: Payloads, index: int = -1) -> list[dict[str, Any]]:
    capture = payloads["captures"][index]
    return [
        {
            "capture_id": capture["capture_id"],
            "excerpt_hash": capture["excerpt_hash"],
        }
    ]
