"""Platform data model.

Design rules (see PLATFORM.md):
- The CMS owns business context (cases, work items, contacts, chat); the
  platform reaches it through a connector and stores only **opaque refs**
  (`case_ref` / `task_ref` strings), never assumptions about the CMS's shape.
- The platform DB stores what agents did and what the platform decided about
  them: goals, runs, the follow-up schedule, an append-only event log per run,
  escalations, triggers, and the communications archive (a private, firm-tagged
  copy of the comms the agent initiated or participated in).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.platform.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, enum.Enum):
    PENDING = "pending"            # created, not yet due
    WAITING = "waiting"            # did a unit of work, scheduled next attempt
    ESCALATED = "escalated"        # parked, waiting on a human
    DONE = "done"                  # goal reached
    FAILED = "failed"              # gave up (max attempts, unrecoverable error)


class GoalStatus(str, enum.Enum):
    ACTIVE = "active"
    ACHIEVED = "achieved"          # goal reached
    ABANDONED = "abandoned"        # gave up / staff stopped it


class GoalHorizon(str, enum.Enum):
    SHORT = "short"                # e.g. get the records in this task
    LONG = "long"                  # e.g. update the client till the case closes


class EscalationStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class EscalationKind(str, enum.Enum):
    QUESTION = "question"          # resolution = an answer
    TASK = "task"                  # a human must go do work; resolution = done + note


class PlatformFirm(Base):
    """The platform's own record of a firm: which connector serves it.

    Seed of the future credential vault; `config_json` holds connector-specific
    settings (base URLs today, credential references in production).
    """

    __tablename__ = "platform_firms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    connector_key: Mapped[str] = mapped_column(String(100), default="stub_cms")
    cms_firm_ref: Mapped[str] = mapped_column(String(200), default="")  # firm id in the CMS
    config_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    agent_configs: Mapped[list[AgentConfig]] = relationship(back_populates="firm")


class AgentConfig(Base):
    """Per-firm configuration of an agent type (the multi-tenant seam).

    The agent *type* lives in code (instructions, tool universe, defaults);
    this row binds it to a firm with the staff-facing handle, the skill
    allow-list, the optional guardrail focus, and cadence overrides.
    """

    __tablename__ = "agent_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("platform_firms.id"), index=True)
    agent_name: Mapped[str] = mapped_column(String(100))      # registered agent type
    handle: Mapped[str] = mapped_column(String(100), default="")   # e.g. "@records-agent"
    skills_json: Mapped[str] = mapped_column(Text, default="[]")   # JSON list of skill names
    guardrail_focus: Mapped[str] = mapped_column(Text, default="")  # non-empty enables the guardrail
    guardrail_tools_json: Mapped[str] = mapped_column(Text, default="")  # optional JSON override list
    cadence_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    firm: Mapped[PlatformFirm] = relationship(back_populates="agent_configs")

    def skills(self) -> list[str]:
        import json
        try:
            return list(json.loads(self.skills_json or "[]"))
        except ValueError:
            return []


class Goal(Base):
    """Durable intent on a case: 'get the records', 'update the client till
    the case closes'. Goals are case-scoped (task_ref is optional, for CMSs
    without a task subconcept) and survive their runs. Triggers bind to goals;
    runs are the execution unit toward them."""

    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(Integer, index=True)
    case_ref: Mapped[str] = mapped_column(String(200), index=True)
    task_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    agent_config_id: Mapped[int | None] = mapped_column(ForeignKey("agent_configs.id"), nullable=True)
    agent_name: Mapped[str] = mapped_column(String(100))
    brief: Mapped[str] = mapped_column(Text, default="")      # what the goal is, in words
    horizon: Mapped[GoalHorizon] = mapped_column(Enum(GoalHorizon), default=GoalHorizon.SHORT)
    status: Mapped[GoalStatus] = mapped_column(Enum(GoalStatus), default=GoalStatus.ACTIVE, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    runs: Mapped[list["AgentRun"]] = relationship(back_populates="goal_row", order_by="AgentRun.id")


class AgentRun(Base):
    """One contiguous execution toward a goal.

    Long-running behaviour is modelled as recurring scheduled invocations:
    a run wakes, does one unit of work, and either reschedules itself
    (next_run_at), escalates, or completes. Nothing sleeps in memory.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(Integer, index=True)       # firm scoping, shared infra
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"), nullable=True, index=True)
    case_ref: Mapped[str] = mapped_column(String(200), index=True)  # opaque CMS refs
    task_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    agent_name: Mapped[str] = mapped_column(String(100))
    agent_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.PENDING, index=True)
    goal: Mapped[str] = mapped_column(Text, default="")             # copy of the goal's brief
    scratchpad: Mapped[str] = mapped_column(Text, default="")       # agent's working notes between invocations
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    goal_row: Mapped[Goal | None] = relationship("Goal", back_populates="runs")
    events: Mapped[list[RunEvent]] = relationship(back_populates="run", order_by="RunEvent.id")
    escalations: Mapped[list[Escalation]] = relationship(back_populates="run", order_by="Escalation.id")


class RunEvent(Base):
    """Audit trail entry: everything an agent did, and every platform decision
    about the run (tool_call / note / schedule / status / trigger / guardrail /
    skill_load / comms / goal), in order."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    firm_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(50))
    summary: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")           # JSON payload, transcripts, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[AgentRun] = relationship(back_populates="events")


class Escalation(Base):
    """A request for human help: either a question (resolution = an answer) or
    a task (a human must go do work; resolution = done + note). Surfaced on the
    firm's staff channel (CMS chat when the connector supports it, otherwise
    email) and always on the admin dashboard."""

    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    firm_id: Mapped[int] = mapped_column(Integer, index=True)
    case_ref: Mapped[str] = mapped_column(String(200), default="")
    task_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    kind: Mapped[EscalationKind] = mapped_column(Enum(EscalationKind), default=EscalationKind.QUESTION)
    status: Mapped[EscalationStatus] = mapped_column(Enum(EscalationStatus), default=EscalationStatus.OPEN, index=True)
    question: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="")          # conversation so far, for the human
    answer: Mapped[str] = mapped_column(Text, default="")
    cms_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # chat thread in the CMS
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[AgentRun] = relationship(back_populates="escalations")


class Trigger(Base):
    """Triggers are data. A standing trigger (goal_id null, agent_config_id
    set) opens a new goal when its event matches; an instance trigger (goal_id
    set) wakes one specific goal's run — e.g. minted when the agent sends
    outbound comms so replies route back by conversation_key."""

    __tablename__ = "triggers"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(Integer, index=True)
    agent_config_id: Mapped[int | None] = mapped_column(ForeignKey("agent_configs.id"), nullable=True, index=True)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(50))      # staff_message | inbound_email
    match_json: Mapped[str] = mapped_column(Text, default="{}")  # e.g. {"handle": "@records-agent"}
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def match(self) -> dict:
        import json
        try:
            return dict(json.loads(self.match_json or "{}"))
        except ValueError:
            return {}


class Communication(Base):
    """The communications archive: a private, firm-tagged, derivative copy of
    the calls/emails/faxes the agent initiated or participated in. Not a system
    of record — outcomes still get written back to the CMS; this exists as
    agent context (too verbose for the CMS, valuable for recall)."""

    __tablename__ = "communications"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(Integer, index=True)
    case_ref: Mapped[str] = mapped_column(String(200), default="", index=True)
    goal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(20))         # voice | email | fax | portal
    direction: Mapped[str] = mapped_column(String(10), default="outbound")  # outbound | inbound
    counterparty: Mapped[str] = mapped_column(String(300), default="")
    conversation_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    subject: Mapped[str] = mapped_column(String(500), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")   # transcript / email body
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
