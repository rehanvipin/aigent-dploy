"""Admin dashboard: goals, what agents are doing, what needs a human, memory."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.platform.db import get_db
from app.platform.models import (
    AgentRun, Communication, Escalation, EscalationStatus, Goal,
)

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


def _cms_json(path: str):
    try:
        resp = httpx.get(f"{settings.cms_base_url}/cms/api{path}", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        return None


def _cases_by_ref() -> dict:
    cases = {}
    for firm in _cms_json("/firms") or []:
        for case in _cms_json(f"/firms/{firm['id']}/cases") or []:
            cases[str(case["id"])] = case
    return cases


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    runs = db.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(100)).all()
    goals = db.scalars(select(Goal).order_by(Goal.id.desc()).limit(100)).all()
    escalations = db.scalars(
        select(Escalation).where(Escalation.status == EscalationStatus.OPEN).order_by(Escalation.id.desc())
    ).all()
    comms = db.scalars(select(Communication).order_by(Communication.id.desc()).limit(25)).all()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "runs": runs,
            "goals": goals,
            "escalations": escalations,
            "comms": comms,
            "cases_by_ref": _cases_by_ref(),
        },
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, db: Session = Depends(get_db)):
    run = db.get(AgentRun, run_id)
    if not run:
        return HTMLResponse("run not found", status_code=404)
    case = _cms_json(f"/cases/{run.case_ref}")
    comms = db.scalars(
        select(Communication).where(Communication.run_id == run.id).order_by(Communication.id)
    ).all()
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {"run": run, "case": case, "comms": comms},
    )
