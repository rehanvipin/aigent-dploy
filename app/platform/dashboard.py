"""Admin dashboard: what agents are tracking, what they did, what needs a human."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.platform.db import get_db
from app.platform.models import AgentRun, Escalation, EscalationStatus

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


def _cms_json(path: str):
    try:
        resp = httpx.get(f"{settings.cms_base_url}/cms/api{path}", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        return None


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    runs = db.scalars(select(AgentRun).order_by(AgentRun.id.desc())).all()
    escalations = db.scalars(
        select(Escalation).where(Escalation.status == EscalationStatus.OPEN).order_by(Escalation.id.desc())
    ).all()
    firms = _cms_json("/firms") or []
    cases_by_id = {}
    for firm in firms:
        for case in _cms_json(f"/firms/{firm['id']}/cases") or []:
            cases_by_id[case["id"]] = case
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "runs": runs,
            "escalations": escalations,
            "firms": firms,
            "cases_by_id": cases_by_id,
        },
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, db: Session = Depends(get_db)):
    run = db.get(AgentRun, run_id)
    if not run:
        return HTMLResponse("run not found", status_code=404)
    case = _cms_json(f"/cases/{run.case_id}")
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {"run": run, "case": case},
    )
