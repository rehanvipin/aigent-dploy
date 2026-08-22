"""Platform data model.

Design rules (from README):
- The CMS owns business context (firms, cases, tasks, contacts, chat).
- The platform DB only stores what the agents did: runs, events, escalations,
  and the follow-up schedule. Case context is fetched from the CMS at run time
  and outcomes are written back to the CMS.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.platform.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, enum.Enum):
    PENDING = "pending"            # created, not yet due
    WAITING = "waiting"            # did a unit of work, scheduled next attempt
    ESCALATED = "escalated"        # parked, waiting for a human
    DONE = "done"                  # goal reached
    FAILED = "failed"              # gave up (max attempts, unrecoverable error)


class EscalationStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class AgentRun(Base):
    """One agent assigned to one task within one case.

    Long-running behaviour is modelled as recurring scheduled invocations:
    a run wakes, does one unit of work, and either reschedules itself
    (next_run_at), escalates, or completes. Nothing sleeps in memory.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(Integer, index=True)       # firm scoping, shared infra
    case_id: Mapped[int] = mapped_column(Integer, index=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    agent_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.PENDING, index=True)
    goal: Mapped[str] = mapped_column(Text, default="")             # what the staff member asked for
    scratchpad: Mapped[str] = mapped_column(Text, default="")       # agent's working notes between invocations
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    events: Mapped[list[RunEvent]] = relationship(back_populates="run", order_by="RunEvent.id")
    escalations: Mapped[list[Escalation]] = relationship(back_populates="run", order_by="Escalation.id")


class RunEvent(Base):
    """Audit trail entry: every thing an agent did, in order."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    firm_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(50))                   # tool_call / tool_result / note / schedule / status
    summary: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")           # JSON payload, transcripts, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[AgentRun] = relationship(back_populates="events")


class Escalation(Base):
    """A request for human help. Surfaced in the CMS chat and the admin dashboard."""

    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    firm_id: Mapped[int] = mapped_column(Integer, index=True)
    case_id: Mapped[int] = mapped_column(Integer)
    task_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[EscalationStatus] = mapped_column(Enum(EscalationStatus), default=EscalationStatus.OPEN, index=True)
    question: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="")          # conversation so far, for the human
    answer: Mapped[str] = mapped_column(Text, default="")
    cms_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # chat thread in the CMS
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[AgentRun] = relationship(back_populates="escalations")
