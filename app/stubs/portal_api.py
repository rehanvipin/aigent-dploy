"""Stub provider records portal.

Stands in for a medical-records provider's web app. The agent's portal tool
submits a records request here (the 'browser automation' path) and checks its
status. Releases are scripted per provider phone number so the demo is
repeatable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/stubs/portal", tags=["portal"])


def _conn() -> sqlite3.Connection:
    path = settings.database_url.removeprefix("sqlite:///")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


class RecordsRequestIn(BaseModel):
    provider_key: str          # stable key for the provider (we use phone number)
    client_name: str
    case_number: str
    hipaa_on_file: bool = True
    firm_id: int = 0
    case_id: int = 0


@router.post("/requests")
def submit_request(body: RecordsRequestIn):
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM portal_requests WHERE provider_key = ? AND case_number = ? AND status != 'cancelled'",
            (body.provider_key, body.case_number),
        ).fetchone()
        if existing:
            return {"request_id": existing["id"], "status": "already_open"}
        cur = conn.execute(
            """INSERT INTO portal_requests
               (provider_key, client_name, case_number, hipaa_on_file, status, firm_id, case_id, created_at)
               VALUES (?, ?, ?, ?, 'submitted', ?, ?, ?)""",
            (body.provider_key, body.client_name, body.case_number, int(body.hipaa_on_file),
             body.firm_id, body.case_id, now),
        )
        conn.commit()
        return {"request_id": cur.lastrowid, "status": "submitted"}


@router.get("/requests/{request_id}")
def check_request(request_id: int):
    """Advance the scripted release schedule for this provider on each check."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM portal_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "request not found")
        scenario = conn.execute(
            "SELECT script, step FROM stub_scenarios WHERE name = ?", (f"portal:{row['provider_key']}",)
        ).fetchone()
        status = row["status"]
        if scenario is not None and status not in ("released",):
            script = json.loads(scenario["script"])
            step = scenario["step"]
            turn = script[min(step, len(script) - 1)]
            status = turn.get("portal_status", status)
            if step < len(script) - 1:
                conn.execute(
                    "UPDATE stub_scenarios SET step = ? WHERE name = ?",
                    (step + 1, f"portal:{row['provider_key']}"),
                )
            if status != row["status"]:
                conn.execute(
                    "UPDATE portal_requests SET status = ? WHERE id = ?", (status, request_id)
                )
            conn.commit()
        return {
            "request_id": row["id"],
            "provider_key": row["provider_key"],
            "case_number": row["case_number"],
            "status": status,
        }
