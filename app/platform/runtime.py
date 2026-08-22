"""Agent runtime: picks due runs, executes one unit of work, persists outcomes.

Long-running behaviour = recurring scheduled invocations, never in-memory
processes. A run wakes, does one unit of work, and either:
  - reschedules itself (WAITING + next_run_at),
  - parks on a human (ESCALATED + Escalation row + CMS chat message),
  - completes (DONE) or fails (FAILED).
Every step lands in the audit trail and outcomes are written back to the CMS.
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
from app.platform.models import AgentRun, Escalation, EscalationStatus, RunEvent, RunStatus
from app.platform.tools import Toolset, _post

log = logging.getLogger("runtime")

# in-process guard: a run is executed by exactly one caller at a time
# (the webhook and the scheduler can both pick up the same new run)
_executing: set[int] = set()
_executing_lock = threading.Lock()


def start_run(firm_id: int, case_id: int, task_id: int, agent_name: str, goal: str) -> AgentRun:
    with SessionLocal() as db:
        run = AgentRun(
            firm_id=firm_id, case_id=case_id, task_id=task_id,
            agent_name=agent_name, goal=goal, status=RunStatus.PENDING,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        db.add(RunEvent(run_id=run.id, firm_id=firm_id, kind="status",
                        summary=f"run created for agent {agent_name!r}: {goal}"))
        db.commit()
        return run


def _escalate(db, run: AgentRun, result: StepResult) -> None:
    run.status = RunStatus.ESCALATED
    esc = Escalation(
        run_id=run.id, firm_id=run.firm_id, case_id=run.case_id, task_id=run.task_id,
        question=result.escalation_question, context=result.escalation_context,
    )
    db.add(esc)
    db.commit()
    db.refresh(esc)
    db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="status",
                    summary=f"ESCALATED to staff: {result.escalation_question}"))
    db.commit()
    # Surface in the CMS task chat - staff answer there, the answer resumes the run.
    try:
        resp = _post(
            f"{settings.cms_base_url}/cms/api/tasks/{run.task_id}/messages",
            {
                "author": "agent",
                "body": (f"[escalation #{esc.id}] I need help: {result.escalation_question}\n"
                         f"Reply here and I'll continue."),
            },
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
        toolset = Toolset(run)

        try:
            case = toolset.cms_get_case(run.case_id)
            task = toolset.cms_get_task(run.task_id)
        except Exception as exc:
            db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="status",
                            summary=f"could not load CMS context: {exc}"))
            run.status = RunStatus.FAILED
            db.commit()
            return

        ctx = RunContext(run=run, case=case, task=task, tools=toolset)
        run.attempt += 1
        try:
            result = agent.step(ctx)
        except Exception as exc:
            log.exception("agent step failed (run %s)", run.id)
            db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="status",
                            summary=f"agent error: {exc}"))
            run.status = RunStatus.FAILED
            db.commit()
            return

        if result.note:
            db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="note", summary=result.note))

        if result.action == "done":
            run.status = RunStatus.DONE
            toolset.cms_write_task(run.task_id, status="done", note=f"[agent] {result.note}")
        elif result.action == "escalate":
            _escalate(db, run, result)
        elif result.action == "fail" or run.attempt >= agent.max_attempts:
            run.status = RunStatus.FAILED
            toolset.cms_write_task(run.task_id, note=f"[agent] gave up after {run.attempt} attempts: {result.note}")
        else:  # wait
            run.status = RunStatus.WAITING
            run.next_run_at = datetime.now(timezone.utc) + timedelta(days=result.wait_days)
            db.add(RunEvent(run_id=run.id, firm_id=run.firm_id, kind="schedule",
                            summary=f"next wake at {run.next_run_at.isoformat()} "
                                    f"(in {result.wait_days} day(s), cadence={agent.cadence_days}d)"))
        db.commit()


def resolve_escalation(escalation_id: int, answer: str) -> Escalation | None:
    """Human answered (dashboard or CMS chat): record it and resume the run."""
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
