"""Platform API: triggers (webhooks), run inspection, escalation resolution."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.definitions import all_agents, get as get_agent
from app.config import settings
from app.platform.db import get_db, SessionLocal
from app.platform.models import AgentRun, Escalation, EscalationStatus, RunEvent, RunStatus
from app.platform.runtime import execute_run, resolve_escalation, start_run

router = APIRouter(prefix="/api", tags=["platform"])


class CmsChatWebhook(BaseModel):
    firm_id: int
    case_id: int
    task_id: int
    thread_id: int
    message: str
    author: str


@router.post("/webhooks/cms-chat")
def cms_chat_webhook(body: CmsChatWebhook, db: Session = Depends(get_db)):
    """Trigger: staff tagged the agent in a CMS task chat (the Zapier webhook)."""
    open_esc = db.scalar(
        select(Escalation).where(
            Escalation.task_id == body.task_id,
            Escalation.status == EscalationStatus.OPEN,
        )
    )
    if open_esc is not None and body.author != "agent":
        # a reply on a task with an open escalation is treated as the answer
        resolve_escalation(open_esc.id, body.message)
        return {"handled": "escalation_answered", "escalation_id": open_esc.id}

    existing = db.scalar(
        select(AgentRun).where(
            AgentRun.task_id == body.task_id,
            AgentRun.status.in_([RunStatus.PENDING, RunStatus.WAITING, RunStatus.ESCALATED]),
        )
    )
    if existing is not None:
        return {"handled": "already_running", "run_id": existing.id}

    run = start_run(
        firm_id=body.firm_id, case_id=body.case_id, task_id=body.task_id,
        agent_name="medical-record-agent", goal=body.message,
    )
    execute_run(run.id)
    return {"handled": "run_started", "run_id": run.id}


class RunIn(BaseModel):
    firm_id: int
    case_id: int
    task_id: int
    agent_name: str = "medical-record-agent"
    goal: str = ""


@router.post("/runs")
def create_run(body: RunIn):
    get_agent(body.agent_name)  # 404s naturally via KeyError -> handled below
    run = start_run(body.firm_id, body.case_id, body.task_id, body.agent_name, body.goal)
    execute_run(run.id)
    return {"run_id": run.id, "status": run.status}


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


@router.get("/runs")
def list_runs(firm_id: int | None = None, db: Session = Depends(get_db)):
    q = select(AgentRun).order_by(AgentRun.id.desc())
    if firm_id is not None:
        q = q.where(AgentRun.firm_id == firm_id)
    runs = db.scalars(q).all()
    return [
        {
            "id": r.id, "firm_id": r.firm_id, "case_id": r.case_id, "task_id": r.task_id,
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
        "case_id": run.case_id,
        "task_id": run.task_id,
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
                "id": esc.id, "status": esc.status.value, "question": esc.question,
                "context": esc.context, "answer": esc.answer,
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
            "id": e.id, "run_id": e.run_id, "firm_id": e.firm_id, "case_id": e.case_id,
            "task_id": e.task_id, "status": e.status.value, "question": e.question,
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
