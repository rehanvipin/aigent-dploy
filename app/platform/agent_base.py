"""Agent definition API.

An agent is a small Python module that declares:
  - name / description
  - tools it is allowed to use
  - cadence: how often it follows up
  - step(ctx): one unit of work, given the run + CMS context

The platform owns everything else: triggers, scheduling, persistence,
audit trail, escalation plumbing, the dashboard. Writing a new agent should
mean writing only this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.platform import tools
from app.platform.models import AgentRun


@dataclass
class StepResult:
    """What the agent decided after one unit of work."""
    action: str                     # "wait" | "done" | "escalate" | "fail"
    note: str = ""                  # human-readable explanation, goes to audit trail + CMS write-back
    wait_days: float = 1.0          # for action="wait": when to wake again
    escalation_question: str = ""   # for action="escalate"
    escalation_context: str = ""
    escalation_kind: str = "question"  # "question" (answer it) | "task" (a human must do work)


@dataclass
class RunContext:
    """Everything an agent needs for one invocation."""
    run: AgentRun
    case: dict                      # fetched live through the firm's CMS connector
    task: dict                      # {} for CMSs without a task subconcept
    tools: tools.Toolset
    skills: list[str] = field(default_factory=list)   # skill keys the config allows

    @property
    def scratchpad(self) -> dict:
        import json
        if not self.run.scratchpad:
            return {}
        try:
            return json.loads(self.run.scratchpad)
        except ValueError:
            return {}

    def save_scratchpad(self, data: dict) -> None:
        import json
        self.run.scratchpad = json.dumps(data)


@dataclass
class AgentDefinition:
    name: str
    description: str
    tools: list[str]
    instructions: str = ""             # system prompt for LLM-driven agents
    cadence_days: float = 7.0
    max_attempts: int = 12
    step_fn: Callable[[RunContext], StepResult] = field(default=None)

    def step(self, ctx: RunContext) -> StepResult:
        if self.step_fn is not None:
            return self.step_fn(ctx)
        # LLM-driven: dispatch to the platform's Mistral tool-calling loop
        from app.platform.llm import run_llm_step
        return run_llm_step(self, ctx)

    def next_wake(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=self.cadence_days)


_REGISTRY: dict[str, AgentDefinition] = {}


def register(defn: AgentDefinition) -> AgentDefinition:
    _REGISTRY[defn.name] = defn
    return defn


def get(name: str) -> AgentDefinition:
    if name not in _REGISTRY:
        raise KeyError(f"no agent registered under {name!r}")
    return _REGISTRY[name]


def all_agents() -> list[AgentDefinition]:
    return list(_REGISTRY.values())
