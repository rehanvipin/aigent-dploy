"""Stub CMS API + minimal task-board UI.

Stands in for Filevine. The platform talks to it over HTTP exactly like it
would talk to a real CMS, so swapping the stub for a real integration later
does not change platform code.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.db import get_db
from app.stubs.cms_models import Case, ChatMessage, ChatThread, Contact, Firm, Task

router = APIRouter(prefix="/cms", tags=["cms"])
templates = Jinja2Templates(directory="app/templates")

# Display default for the task-board UI. Real routing is data-driven: the
# platform matches standing triggers on whatever handle the firm's agent
# config declares (see app/platform/triggers.py).
AGENT_HANDLE = "@records-agent"


def _mentions_agent(body: str) -> bool:
    return any(part.startswith("@") for part in body.split())


# ---------- schemas ----------

class MessageIn(BaseModel):
    author: str = "staff"
    body: str


class TaskUpdateIn(BaseModel):
    status: str | None = None
    notes: str | None = None


class TaskCreateIn(BaseModel):
    title: str
    notes: str = ""
    status: str = "open"


# ---------- helpers ----------

def _get_or_create_thread(db: Session, task_id: int) -> ChatThread:
    thread = db.scalar(select(ChatThread).where(ChatThread.task_id == task_id))
    if thread is None:
        thread = ChatThread(task_id=task_id)
        db.add(thread)
        db.commit()
        db.refresh(thread)
    return thread


def _fire_agent_webhook(task: Task, thread: ChatThread, message: ChatMessage) -> None:
    """The 'Zapier': when staff tags the agent in chat, POST to the platform."""
    from app.config import settings

    payload = {
        "firm_id": task.case.firm_id,
        "case_id": str(task.case_id),   # opaque refs on the platform side
        "task_id": str(task.id),
        "thread_id": thread.id,
        "message": message.body,
        "author": message.author,
    }
    try:
        httpx.post(f"{settings.cms_base_url}/api/webhooks/cms-chat", json=payload, timeout=5)
    except httpx.HTTPError:
        pass  # fire-and-forget stub; the run can also be started from the dashboard


# ---------- API ----------

@router.get("/api/firms")
def list_firms(db: Session = Depends(get_db)):
    firms = db.scalars(select(Firm)).all()
    return [{"id": f.id, "name": f.name} for f in firms]


@router.get("/api/firms/{firm_id}/cases")
def list_cases(firm_id: int, db: Session = Depends(get_db)):
    cases = db.scalars(select(Case).where(Case.firm_id == firm_id)).all()
    return [
        {
            "id": c.id,
            "case_number": c.case_number,
            "client_name": c.client_name,
            "status": c.status,
            "summary": c.summary,
        }
        for c in cases
    ]


@router.get("/api/cases/{case_id}")
def get_case(case_id: int, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    return {
        "id": case.id,
        "firm_id": case.firm_id,
        "case_number": case.case_number,
        "client_name": case.client_name,
        "status": case.status,
        "summary": case.summary,
        "contacts": [
            {
                "id": ct.id,
                "role": ct.role,
                "name": ct.name,
                "phone": ct.phone,
                "email": ct.email,
                "fax": ct.fax,
                "details": ct.details,
            }
            for ct in case.contacts
        ],
        "tasks": [
            {"id": t.id, "title": t.title, "status": t.status, "notes": t.notes}
            for t in case.tasks
        ],
    }


@router.get("/api/tasks/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return {
        "id": task.id,
        "case_id": task.case_id,
        "firm_id": task.case.firm_id,
        "title": task.title,
        "status": task.status,
        "notes": task.notes,
    }


@router.patch("/api/tasks/{task_id}")
def update_task(task_id: int, body: TaskUpdateIn, db: Session = Depends(get_db)):
    """Agent write-back: status changes and outcome notes land on the CMS task."""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    if body.status is not None:
        task.status = body.status
    if body.notes is not None:
        task.notes = (task.notes + "\n" + body.notes).strip() if task.notes else body.notes
    db.commit()
    return {"id": task.id, "status": task.status, "notes": task.notes}


@router.post("/api/cases/{case_id}/tasks")
def create_task(case_id: int, body: TaskCreateIn, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    task = Task(case_id=case_id, title=body.title, status=body.status, notes=body.notes)
    db.add(task)
    db.commit()
    db.refresh(task)
    return {"id": task.id, "case_id": task.case_id, "title": task.title, "status": task.status, "notes": task.notes}


@router.post("/api/tasks/{task_id}/messages")
def post_message(task_id: int, body: MessageIn, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    thread = _get_or_create_thread(db, task_id)
    mentions = _mentions_agent(body.body)
    msg = ChatMessage(thread_id=thread.id, author=body.author, body=body.body, mentions_agent=mentions)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    if body.author != "agent":
        # every staff message is forwarded; the platform decides what to do with it
        # (start a run if the agent is tagged, answer an open escalation, or ignore)
        _fire_agent_webhook(task, thread, msg)
    return {"id": msg.id, "thread_id": thread.id, "mentions_agent": mentions}


@router.get("/api/tasks/{task_id}/messages")
def list_messages(task_id: int, db: Session = Depends(get_db)):
    thread = db.scalar(select(ChatThread).where(ChatThread.task_id == task_id))
    if not thread:
        return {"thread_id": None, "messages": []}
    return {
        "thread_id": thread.id,
        "messages": [
            {"id": m.id, "author": m.author, "body": m.body, "created_at": m.created_at.isoformat()}
            for m in thread.messages
        ],
    }


@router.get("/api/threads/{thread_id}/messages")
def list_thread_messages(thread_id: int, db: Session = Depends(get_db)):
    thread = db.get(ChatThread, thread_id)
    if not thread:
        raise HTTPException(404, "thread not found")
    return [
        {"id": m.id, "author": m.author, "body": m.body, "created_at": m.created_at.isoformat()}
        for m in thread.messages
    ]


# ---------- task board UI ----------

@router.get("/board", response_class=HTMLResponse)
def board(request: Request, db: Session = Depends(get_db)):
    firms = db.scalars(select(Firm)).all()
    cases = db.scalars(select(Case)).all()
    tasks = db.scalars(select(Task)).all()
    threads = {t.task_id: t for t in db.scalars(select(ChatThread)).all()}
    return templates.TemplateResponse(
        request,
        "cms_board.html",
        {
            "firms": firms,
            "cases": cases,
            "tasks": tasks,
            "threads": threads,
            "agent_handle": AGENT_HANDLE,
        },
    )


@router.get("/board/tasks/{task_id}", response_class=HTMLResponse)
def board_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    thread = db.scalar(select(ChatThread).where(ChatThread.task_id == task_id))
    messages = thread.messages if thread else []
    return templates.TemplateResponse(
        request,
        "cms_task.html",
        {
            "task": task,
            "case": task.case,
            "messages": messages,
            "agent_handle": AGENT_HANDLE,
        },
    )
