"""Aigent-Dploy POC application.

One process runs everything:
  - the stub CMS (Filevine stand-in) with a task board UI,
  - the stub comms channels (voice / email / fax) and provider portal,
  - the agent platform: triggers, scheduler, runtime, dashboard.

Run:  uv run uvicorn app.main:app --reload
Seed: uv run python scripts/seed.py
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form
from fastapi.responses import RedirectResponse

from app.config import settings
from app.platform.db import Base, engine
from app.platform.runtime import scheduler_tick
from app.stubs.cms_models import CmsBase

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("app")

STUB_DDL = """
CREATE TABLE IF NOT EXISTS stub_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stub_scenarios (
    name TEXT PRIMARY KEY,
    script TEXT NOT NULL,
    step INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS portal_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_key TEXT NOT NULL,
    client_name TEXT NOT NULL,
    case_number TEXT NOT NULL,
    hipaa_on_file INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'submitted',
    firm_id INTEGER NOT NULL DEFAULT 0,
    case_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    Base.metadata.create_all(engine)
    CmsBase.metadata.create_all(engine)
    path = settings.database_url.removeprefix("sqlite:///")
    with sqlite3.connect(path) as conn:
        conn.executescript(STUB_DDL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    async def scheduler_loop():
        while True:
            await asyncio.sleep(settings.scheduler_interval_seconds)
            try:
                # off the event loop: scheduler_tick makes blocking HTTP+DB calls
                n = await asyncio.to_thread(scheduler_tick)
                if n:
                    log.info("scheduler executed %s due run(s)", n)
            except Exception:
                log.exception("scheduler tick failed")

    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="Aigent-Dploy Platform POC", lifespan=lifespan)

from app.platform.api import router as platform_router          # noqa: E402
from app.platform.dashboard import router as dashboard_router    # noqa: E402
from app.stubs.cms_api import router as cms_router               # noqa: E402
from app.stubs.comms_api import router as comms_router           # noqa: E402
from app.stubs.portal_api import router as portal_router         # noqa: E402

app.include_router(platform_router)
app.include_router(dashboard_router)
app.include_router(cms_router)
app.include_router(comms_router)
app.include_router(portal_router)


# -- tiny form handlers so the HTML pages work without JS --------------------

@app.post("/ui/tasks/{task_id}/message")
def ui_post_message(task_id: int, body: str = Form(...)):
    import httpx
    httpx.post(
        f"{settings.cms_base_url}/cms/api/tasks/{task_id}/messages",
        json={"author": "staff", "body": body}, timeout=10,
    )
    return RedirectResponse(f"/cms/board/tasks/{task_id}", status_code=303)


@app.post("/ui/escalations/{escalation_id}/answer")
def ui_answer_escalation(escalation_id: int, answer: str = Form(...)):
    import httpx
    httpx.post(
        f"{settings.cms_base_url}/api/escalations/{escalation_id}/answer",
        json={"answer": answer}, timeout=10,
    )
    return RedirectResponse("/", status_code=303)


@app.post("/ui/runs/{run_id}/run-now")
def ui_run_now(run_id: int):
    import httpx
    httpx.post(f"{settings.cms_base_url}/api/runs/{run_id}/run-now", timeout=15)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/ui/simulate-inbound-email")
def ui_simulate_inbound_email(firm_id: int = Form(...), sender: str = Form(...),
                              subject: str = Form(""), body: str = Form(...),
                              conversation_key: str = Form(""),
                              task_ref: str = Form("")):
    """Demo affordance: the outside world sends the platform an email."""
    import httpx
    httpx.post(
        f"{settings.cms_base_url}/stubs/email/inbound",
        json={"firm_id": firm_id, "sender": sender, "subject": subject,
              "body": body, "conversation_key": conversation_key,
              "task_ref": task_ref or None},
        timeout=30,
    )
    return RedirectResponse("/", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}
