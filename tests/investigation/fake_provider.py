from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from wiki_ai.agent.protocol import ToolCall
from wiki_ai.agent.session import AgentRun, AgentSession

Step = tuple[str, Mapping[str, Any]]
FindingBuilder = Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]]


@dataclass
class Script:
    steps: tuple[Step, ...] = ()
    findings: Sequence[Mapping[str, Any]] = ()
    build_findings: FindingBuilder | None = None
    fail_with: str = ""
    exhaust_budget: bool = False


@dataclass
class FakeProvider:
    scripts: list[Script] = field(default_factory=list)
    seen_objectives: list[str] = field(default_factory=list)
    seen_schemas: list[Mapping[str, Any]] = field(default_factory=list)
    captures: list[Mapping[str, Any]] = field(default_factory=list)
    on_round: Callable[[int], None] | None = None
    calls: int = 0

    def run(self, session: AgentSession) -> AgentRun:
        index = self.calls
        self.calls += 1
        self.seen_objectives.append(session.objective)
        self.seen_schemas.append(session.finding_schema)
        if self.on_round is not None:
            self.on_round(index)
        script = self.scripts[index] if index < len(self.scripts) else Script()
        capture_map: dict[str, Any] = {}
        for name, arguments in script.steps:
            result = session.invoke(ToolCall(name=name, arguments=arguments))
            if name == "evidence.capture" and result.ok:
                payload = dict(result.payload)
                self.captures.append(payload)
                capture_map[payload["locator"]] = payload
        if script.fail_with:
            return session.fail(script.fail_with)
        if script.exhaust_budget:
            return session.budget_exhausted("round budget spent before conclusion")
        findings = (
            script.build_findings(capture_map)
            if script.build_findings is not None
            else script.findings
        )
        return session.finish(list(findings))
