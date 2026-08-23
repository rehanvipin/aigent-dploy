"""Platform API: trigger webhooks, goals, runs, escalations, memory inspection."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.definitions import all_agents
from app.config import settings
from app.platform.db import get_db
from app.platform.models import (
    AgentConfig, AgentRun, Communication, Escalation, EscalationStatus, Goal,
    RunStatus, Trigger,
)
from app.platform.runtime import execute_run, open_goal, resolve_escalation
from app.platform.triggers import route_event

router = APIRouter(prefix="/api", tags=["platform"])


class CmsChatWebhook(BaseModel):
    firm_id: int
    case_id: str | int
    task_id: str | int | None = None
    thread_id: int | None = None
    message: str
    author: str


@router.post("/webhooks/cms-chat")
def cms_chat_webhook(body: CmsChatWebhook):
    """Trigger: staff posted in a CMS task chat (the Zapier webhook). Routed
    through the trigger pipeline (escalation > instance > active > standing)."""
    return route_event("staff_message", {
        "firm_id": body.firm_id,
        "case_ref": str(body.case_id),
        "task_ref": str(body.task_id) if body.task_id is not None else None,
        "message": body.message,
        "author": body.author,
    })


class InboundEmailWebhook(BaseModel):
    firm_id: int
    sender: str
    subject: str = ""
    body: str
    conversation_key: str = ""
    case_ref: str | None = None
    task_ref: str | None = None


@router.post("/webhooks/inbound-email")
def inbound_email_webhook(body: InboundEmailWebhook):
    """Trigger: an email arrived for the platform (staff answer or provider
    reply). Matched to a goal by conversation_key, else treated as a staff
    message on the given refs."""
    return route_event("inbound_email", {
        "firm_id": body.firm_id,
        "case_ref": body.case_ref,
        "task_ref": body.task_ref,
        "message": f"{body.subject}\n{body.body}".strip(),
        "sender": body.sender,
        "conversation_key": body.conversation_key,
    })


class GoalIn(BaseModel):
    firm_id: int
    case_ref: str
    task_ref: str | None = None
    agent_config_id: int
    brief: str = ""
    horizon: str = "short"


@router.post("/goals")
def create_goal(body: GoalIn):
    """Open a goal directly (dashboard / API path; staff chat tags go through
    the trigger pipeline)."""
    from app.platform.models import GoalHorizon
    goal, run = open_goal(
        firm_id=body.firm_id, case_ref=body.case_ref, task_ref=body.task_ref,
        agent_config_id=body.agent_config_id, brief=body.brief,
        horizon=GoalHorizon.LONG if body.horizon == "long" else GoalHorizon.SHORT,
    )
    execute_run(run.id)
    return {"goal_id": goal.id, "run_id": run.id, "status": run.status.value}


@router.get("/goals")
def list_goals(firm_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Goal).order_by(Goal.id.desc())
    if firm_id is not None:
        q = q.where(Goal.firm_id == firm_id)
    goals = db.scalars(q).all()
    return [
        {
            "id": g.id, "firm_id": g.firm_id, "case_ref": g.case_ref, "task_ref": g.task_ref,
            "agent_name": g.agent_name, "brief": g.brief, "horizon": g.horizon.value,
            "status": g.status.value, "runs": len(g.runs),
            "created_at": g.created_at.isoformat(),
        }
        for g in goals
    ]


@router.get("/goals/{goal_id}")
def get_goal(goal_id: int, db: Session = Depends(get_db)):
    goal = db.get(Goal, goal_id)
    if not goal:
        raise HTTPException(404, "goal not found")
    return {
        "id": goal.id, "firm_id": goal.firm_id, "case_ref": goal.case_ref,
        "task_ref": goal.task_ref, "agent_name": goal.agent_name, "brief": goal.brief,
        "horizon": goal.horizon.value, "status": goal.status.value,
        "runs": [
            {"id": r.id, "status": r.status.value, "attempt": r.attempt,
             "created_at": r.created_at.isoformat()}
            for r in goal.runs
        ],
    }


class RunIn(BaseModel):
    firm_id: int
    case_ref: str
    task_ref: str | None = None
    agent_config_id: int
    brief: str = ""


@router.post("/runs")
def create_run(body: RunIn):
    """Compatibility shim: open a goal + first run."""
    goal, run = open_goal(
        firm_id=body.firm_id, case_ref=body.case_ref, task_ref=body.task_ref,
        agent_config_id=body.agent_config_id, brief=body.brief,
    )
    execute_run(run.id)
    return {"goal_id": goal.id, "run_id": run.id, "status": run.status.value}


@router.get("/agents")
def list_agents():
    return [
        {
            "name": a.name,
            "description": a.description,
            "tools": a.tools,
            "cadence_days": a.cadence_days,
            "max_attempts": a.max_attempts,
        }
        for a in all_agents()
    ]


@router.get("/agent-configs")
def list_agent_configs(firm_id: int | None = None, db: Session = Depends(get_db)):
    q = select(AgentConfig).order_by(AgentConfig.id)
    if firm_id is not None:
        q = q.where(AgentConfig.firm_id == firm_id)
    cfgs = db.scalars(q).all()
    return [
        {
            "id": c.id, "firm_id": c.firm_id, "agent_name": c.agent_name,
            "handle": c.handle, "skills": c.skills(),
            "guardrail_enabled": bool(c.guardrail_focus),
            "cadence_days": c.cadence_days, "enabled": c.enabled,
        }
        for c in cfgs
    ]


@router.get("/triggers")
def list_triggers(firm_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Trigger).order_by(Trigger.id.desc())
    if firm_id is not None:
        q = q.where(Trigger.firm_id == firm_id)
    rows = db.scalars(q).all()
    return [
        {
            "id": t.id, "firm_id": t.firm_id, "agent_config_id": t.agent_config_id,
            "goal_id": t.goal_id, "event_type": t.event_type, "match": t.match(),
            "enabled": t.enabled,
        }
        for t in rows
    ]


@router.get("/communications")
def list_communications(firm_id: int | None = None, case_ref: str | None = None,
                        db: Session = Depends(get_db)):
    q = select(Communication).order_by(Communication.id.desc()).limit(200)
    if firm_id is not None:
        q = q.where(Communication.firm_id == firm_id)
    if case_ref:
        q = q.where(Communication.case_ref == case_ref)
    rows = db.scalars(q).all()
    return [
        {
            "id": c.id, "firm_id": c.firm_id, "case_ref": c.case_ref, "run_id": c.run_id,
            "channel": c.channel, "direction": c.direction, "counterparty": c.counterparty,
            "subject": c.subject, "summary": c.summary, "content": c.content,
            "conversation_key": c.conversation_key,
            "occurred_at": c.occurred_at.isoformat(),
        }
        for c in rows
    ]


@router.get("/runs")
def list_runs(firm_id: int | None = None, db: Session = Depends(get_db)):
    q = select(AgentRun).order_by(AgentRun.id.desc())
    if firm_id is not None:
        q = q.where(AgentRun.firm_id == firm_id)
    runs = db.scalars(q).all()
    return [
        {
            "id": r.id, "firm_id": r.firm_id, "goal_id": r.goal_id,
            "case_ref": r.case_ref, "task_ref": r.task_ref,
            "agent_name": r.agent_name, "status": r.status.value, "attempt": r.attempt,
            "goal": r.goal, "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in runs
    ]


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(AgentRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return {
        "id": run.id,
        "firm_id": run.firm_id,
        "goal_id": run.goal_id,
        "case_ref": run.case_ref,
        "task_ref": run.task_ref,
        "agent_name": run.agent_name,
        "status": run.status.value,
        "goal": run.goal,
        "attempt": run.attempt,
        "scratchpad": run.scratchpad,
        "next_run_at": run.next_run_at.isoformat() if run.next_run_at else None,
        "events": [
            {
                "id": e.id, "kind": e.kind, "summary": e.summary,
                "detail": e.detail, "created_at": e.created_at.isoformat(),
            }
            for e in run.events
        ],
        "escalations": [
            {
                "id": esc.id, "kind": esc.kind.value, "status": esc.status.value,
                "question": esc.question, "context": esc.context, "answer": esc.answer,
                "created_at": esc.created_at.isoformat(),
            }
            for esc in run.escalations
        ],
    }


@router.post("/runs/{run_id}/run-now")
def run_now(run_id: int, db: Session = Depends(get_db)):
    """Nudge a waiting run to wake immediately (staff action / demo fast-forward)."""
    run = db.get(AgentRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if run.status not in (RunStatus.PENDING, RunStatus.WAITING):
        raise HTTPException(409, f"run is {run.status.value}, cannot be nudged")
    run.next_run_at = datetime.now(timezone.utc)
    db.commit()
    execute_run(run.id)
    db.refresh(run)
    return {"run_id": run.id, "status": run.status.value, "attempt": run.attempt}


@router.get("/escalations")
def list_escalations(status: str = "open", db: Session = Depends(get_db)):
    q = select(Escalation).order_by(Escalation.id.desc())
    if status == "open":
        q = q.where(Escalation.status == EscalationStatus.OPEN)
    escs = db.scalars(q).all()
    return [
        {
            "id": e.id, "run_id": e.run_id, "firm_id": e.firm_id,
            "case_ref": e.case_ref, "task_ref": e.task_ref, "kind": e.kind.value,
            "status": e.status.value, "question": e.question,
            "context": e.context, "answer": e.answer, "created_at": e.created_at.isoformat(),
        }
        for e in escs
    ]


class AnswerIn(BaseModel):
    answer: str


@router.post("/escalations/{escalation_id}/answer")
def answer_escalation(escalation_id: int, body: AnswerIn):
    esc = resolve_escalation(escalation_id, body.answer)
    if esc is None:
        raise HTTPException(404, "escalation not found or already resolved")
    execute_run(esc.run_id)  # due immediately after the human answers
    return {"id": esc.id, "status": esc.status.value, "answer": esc.answer}


@router.get("/stub-log")
def stub_log(channel: str | None = None):
    path = settings.database_url.removeprefix("sqlite:///")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if channel:
        rows = conn.execute(
            "SELECT * FROM stub_log WHERE channel = ? ORDER BY id DESC", (channel,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM stub_log ORDER BY id DESC").fetchall()
    return [
        {
            "id": r["id"], "channel": r["channel"],
            "payload": json.loads(r["payload"]), "created_at": r["created_at"],
        }
        for r in rows
    ]
