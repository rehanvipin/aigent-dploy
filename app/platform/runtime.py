"""Agent runtime: opens goals, executes runs, persists outcomes.

A **goal** is the durable intent ("get the records", "update the client till
the case closes"); a **run** is one contiguous execution toward it. Long-running
behaviour = recurring scheduled invocations, never in-memory processes. A run
wakes, does one unit of work, and either:
  - reschedules itself (WAITING + next_run_at),
  - parks on a human (ESCALATED + Escalation row + staff-channel message),
  - completes (DONE -> goal achieved) or fails (FAILED -> goal abandoned).
Every step lands in the audit trail and outcomes are written back to the CMS
through the firm's connector.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.agents.definitions import get as get_agent
from app.config import settings
from app.platform.agent_base import RunContext, StepResult
from app.platform.db import SessionLocal
from app.platform.models import (
    AgentConfig, AgentRun, Escalation, EscalationKind, EscalationStatus, Goal,
    GoalHorizon, GoalStatus, PlatformFirm, RunEvent, RunStatus,
)
from app.platform.tools import Toolset

log = logging.getLogger("runtime")

# in-process guard: a run is executed by exactly one caller at a time
# (the webhook and the scheduler can both pick up the same new run)
_executing: set[int] = set()
_executing_lock = threading.Lock()


def _connector_for_firm(db, firm_id: int):
    from app.platform.connectors import get_connector

    firm = db.get(PlatformFirm, firm_id)
    if firm is None:
        # unbound firm (shouldn't happen once seeded); fall back to the stub
        return get_connector("stub_cms")
    try:
        config = json.loads(firm.config_json) if firm.config_json else {}
    except ValueError:
        config = {}
    return get_connector(firm.connector_key, config)


def open_goal(firm_id: int, case_ref: str, task_ref: str | None,
              agent_config_id: int, brief: str,
              horizon: GoalHorizon = GoalHorizon.SHORT) -> tuple[Goal, AgentRun]:
    """Open a goal and its first run. Triggers (standing) call this."""
    with SessionLocal() as db:
        cfg = db.get(AgentConfig, agent_config_id)
        if cfg is None:
            raise KeyError(f"no agent config #{agent_config_id}")
        goal = Goal(
            firm_id=firm_id, case_ref=case_ref, task_ref=task_ref,
            agent_config_id=agent_config_id, agent_name=cfg.agent_name,
            brief=brief, horizon=horizon, status=GoalStatus.ACTIVE,
        )
        db.add(goal)
        db.flush()
        run = AgentRun(
            firm_id=firm_id, goal_id=goal.id, case_ref=case_ref, task_ref=task_ref,
            agent_name=cfg.agent_name, agent_config_id=agent_config_id,
            goal=brief, status=RunStatus.PENDING,
        )
        db.add(run)
        db.flush()
        db.add(RunEvent(run_id=run.id, firm_id=firm_id, kind="goal",
                        summary=f"goal #{goal.id} opened for {cfg.agent_name!r}: {brief[:200]}"))
        db.commit()
        db.refresh(goal)
        db.refresh(run)
        return goal, run


def _escalate(db, run: AgentRun, result: StepResult) -> None:
    run.status = RunStatus.ESCALATED
    kind = (EscalationKind.TASK if result.escalation_kind == "task"
            else EscalationKind.QUESTION)
    esc = Escalation(
        run_id=run.id, firm_id=run.firm_id, case_ref=run.case_ref, task_ref=run.task_ref,
        kind=kind, question=result.escalation_question, context=result.escalation_context,
    )
    db.add(esc)
    db.commit()
    db.refresh(esc)
    label = "needs a human to do work" if kind == EscalationKind.TASK else "ESCALATED to staff"
    db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="status",
                    summary=f"{label}: {result.escalation_question}"))
    db.commit()
    # Surface where the firm's staff work: CMS chat if the connector supports
    # it, otherwise by email; always on the dashboard.
    connector = _connector_for_firm(db, run.firm_id)
    verb = "I need someone to do this" if kind == EscalationKind.TASK else "I need help"
    if connector.capabilities.get("chat") and run.task_ref:
        try:
            resp = connector.post_message(
                run.task_ref,
                (f"[escalation #{esc.id}] {verb}: {result.escalation_question}\n"
                 f"Reply here, by email, or on the dashboard and I'll continue."),
            )
            esc.cms_thread_id = resp.get("thread_id")
            db.commit()
        except Exception:
            log.exception("could not post escalation to CMS chat (run %s)", run.id)


def execute_run(run_id: int) -> None:
    with _executing_lock:
        if run_id in _executing:
            return
        _executing.add(run_id)
    try:
        _execute_run(run_id)
    finally:
        with _executing_lock:
            _executing.discard(run_id)


def _execute_run(run_id: int) -> None:
    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        if run is None or run.status in (RunStatus.DONE, RunStatus.FAILED, RunStatus.ESCALATED):
            return
        agent = get_agent(run.agent_name)
        connector = _connector_for_firm(db, run.firm_id)
        toolset = Toolset(run, connector=connector)

        cfg = db.get(AgentConfig, run.agent_config_id) if run.agent_config_id else None
        skill_keys = (cfg.skills() if cfg else [])

        try:
            case = toolset.cms_get_case()
            task = toolset.cms_get_task()
        except Exception as exc:
            db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="status",
                            summary=f"could not load CMS context: {exc}"))
            run.status = RunStatus.FAILED
            _close_goal(db, run, GoalStatus.ABANDONED)
            db.commit()
            return

        ctx = RunContext(run=run, case=case, task=task, tools=toolset, skills=skill_keys)
        run.attempt += 1
        try:
            result = agent.step(ctx)
        except Exception as exc:
            log.exception("agent step failed (run %s)", run.id)
            db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="status",
                            summary=f"agent error: {exc}"))
            run.status = RunStatus.FAILED
            _close_goal(db, run, GoalStatus.ABANDONED)
            db.commit()
            return

        if result.note:
            db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="note", summary=result.note))

        cadence = cfg.cadence_days if (cfg and cfg.cadence_days) else agent.cadence_days
        if result.action == "done":
            run.status = RunStatus.DONE
            _close_goal(db, run, GoalStatus.ACHIEVED)
            if run.task_ref:
                toolset.cms_write_task(status="done", note=f"[agent] {result.note}")
        elif result.action == "escalate":
            _escalate(db, run, result)
        elif result.action == "fail" or run.attempt >= agent.max_attempts:
            run.status = RunStatus.FAILED
            _close_goal(db, run, GoalStatus.ABANDONED)
            if run.task_ref:
                toolset.cms_write_task(note=f"[agent] gave up after {run.attempt} attempts: {result.note}")
        else:  # wait
            run.status = RunStatus.WAITING
            run.next_run_at = datetime.now(timezone.utc) + timedelta(days=result.wait_days)
            db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="schedule",
                            summary=f"next wake at {run.next_run_at.isoformat()} "
                                    f"(in {result.wait_days} day(s), cadence={cadence}d)"))
        db.commit()


def _close_goal(db, run: AgentRun, status: GoalStatus) -> None:
    if not run.goal_id:
        return
    goal = db.get(Goal, run.goal_id)
    if goal is not None and goal.status == GoalStatus.ACTIVE:
        goal.status = status
        goal.closed_at = datetime.now(timezone.utc)
        db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="goal",
                        summary=f"goal #{goal.id} {status.value}"))


def resolve_escalation(escalation_id: int, answer: str) -> Escalation | None:
    """Human answered (dashboard, CMS chat, or email): record it and resume."""
    with SessionLocal() as db:
        esc = db.get(Escalation, escalation_id)
        if esc is None or esc.status == EscalationStatus.RESOLVED:
            return None
        esc.status = EscalationStatus.RESOLVED
        esc.answer = answer
        esc.resolved_at = datetime.now(timezone.utc)
        run = db.get(AgentRun, esc.run_id)
        run.status = RunStatus.WAITING
        run.next_run_at = datetime.now(timezone.utc)
        # the agent sees the answer on its next invocation via the scratchpad
        scratch = json.loads(run.scratchpad) if run.scratchpad else {}
        scratch.setdefault("staff_answers", []).append(answer)
        run.scratchpad = json.dumps(scratch)
        db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="status",
                        summary=f"escalation #{esc.id} resolved by staff: {answer}"))
        db.commit()
        db.refresh(esc)
        return esc


def scheduler_tick() -> int:
    """Execute all due runs. Called on an interval by the app lifespan."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        due_ids = db.scalars(
            select(AgentRun.id).where(
                AgentRun.status.in_([RunStatus.PENDING, RunStatus.WAITING]),
                AgentRun.next_run_at <= now,
            )
        ).all()
    for run_id in due_ids:
        execute_run(run_id)
    return len(due_ids)
